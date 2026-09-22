"""Focused source-context mapping and approved-answer boundary checks."""

from fastapi.testclient import TestClient
import pytest
import sqlite3

from camellia import app as app_module, client_qa
from camellia.config import config
from camellia.database import Database
from camellia.instagram_routing import decode_route, route_for_inbound, validate_route_payload
from camellia.responder import responder
from camellia.session_memory import session_memory


@pytest.fixture
def mapping_db(tmp_path, monkeypatch):
    path = tmp_path / "source-mapping.db"
    database = Database(str(path))
    monkeypatch.setattr("camellia.database.db", database)
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(config, "ADMIN_API_KEY", "source-test-admin-key-123456")
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    return database, path


def _source(source_type, source_id):
    return {
        "source_type": source_type, "media_id": source_id,
        "shortcode": source_id if source_type == "reel" else None,
        "routing_topic": "Subscription", "source_description": f"This {source_type} introduces our subscription.",
        "active": True, "menu_mode": "NONE", "menu_id": None,
        "priority_products": [], "knowledge_scope": [],
    }


@pytest.mark.parametrize("source_type,question", [
    ("post", "What is this post about?"),
    ("reel", "What's this reel about?"),
    ("story", "What is this story about?"),
])
def test_exact_source_description_and_approved_topic_switch(source_type, question):
    user_id = f"mapping-{source_type}"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {**_source(source_type, f"meta-{source_type}"), "routing_id": 41}
    def reply(message):
        return responder.match_and_respond(message, user_id=user_id, channel="instagram")
    assert reply(question)[0] == f"This {source_type} introduces our subscription."
    assert facts["decision_lane"] == "SOURCE_DESCRIPTION"
    assert reply("Tell me more about subscription")[0] == client_qa.REGISTRY_BY_ID["subscription_details_client"].answer
    assert reply("Tell me about Sondesh")[0] == client_qa.REGISTRY_BY_ID["sondesh_details_client"].answer
    assert reply("Price?")[0] == client_qa.REGISTRY_BY_ID["sondesh_price_client"].answer
    assert reply("Do you cater for 500 wedding guests?")[1] is False


def test_source_first_ambiguous_price_uses_approved_subscription_answer():
    user_id = "source-first-price"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {**_source("post", "meta-post"), "routing_id": 42}
    answer, matched, _template, _images = responder.match_and_respond("Price?", user_id=user_id, channel="instagram")
    assert matched and answer == client_qa.REGISTRY_BY_ID["subscription_price"].answer
    assert facts["decision_lane"] == "EXACT_APPROVED"


@pytest.mark.parametrize("question", [
    "Tell me about this post", "What is this?", "What's this about?",
    "Can you explain this reel?", "What are you showing here?",
])
def test_natural_source_references_use_admin_description(question):
    user_id = f"source-natural-{question}"
    session_memory.clear_session(user_id, "instagram")
    session_memory.get_facts(user_id, "instagram")["source_route"] = {
        **_source("reel", "exact-reel-natural"), "routing_id": 45,
    }
    answer, matched, _template, _images = responder.match_and_respond(
        question, user_id=user_id, channel="instagram"
    )
    assert matched and answer == "This reel introduces our subscription."


def test_explicit_subscription_price_keeps_approved_copy_and_relevant_mapped_asset(monkeypatch):
    user_id = "subscription-mapped-chart"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {**_source("reel", "meta-reel"), "routing_id": 44,
                             "menu_mode": "SELECTED", "menu_id": "subscription_rate_chart"}
    monkeypatch.setattr("camellia.responder.current_menu_image", lambda menu_id: "/images/mapped-chart.jpg")
    answer, matched, _template, images = responder.match_and_respond(
        "What is the subscription price?", user_id=user_id, channel="instagram"
    )
    assert matched and answer == client_qa.REGISTRY_BY_ID["subscription_price"].answer
    assert "/images/mapped-chart.jpg" in images
    answer, matched, _template, images = responder.match_and_respond(
        "Tell me about Sondesh", user_id=user_id, channel="instagram"
    )
    assert matched and answer == client_qa.REGISTRY_BY_ID["sondesh_details_client"].answer
    assert "/images/mapped-chart.jpg" not in (images or [])


def test_greeting_does_not_use_source_description():
    user_id = "source-greeting"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {**_source("story", "meta-story"), "routing_id": 43}
    answer, matched, _template, _images = responder.match_and_respond("Hi", user_id=user_id, channel="instagram")
    assert matched and "How can I help" in answer and facts["decision_lane"] == "SYSTEM_CONVERSATION"


@pytest.mark.parametrize("topic,question,approved_id", [
    ("Sondesh", "tell me more", "sondesh_details_client"),
    ("Sondesh", "price?", "sondesh_price_client"),
    ("Sondesh", "how much?", "sondesh_price_client"),
    ("Subscription", "tell me more", "subscription_details_client"),
    ("Subscription", "price?", "subscription_price"),
    ("Bengali Bento", "tell me more", "bento_details_client"),
    ("Bengali Bento", "price?", "bento_price_client"),
])
def test_short_followup_uses_source_topic_approved_answer(topic, question, approved_id):
    user_id = f"short-{topic}-{question}"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {**_source("post", "exact-post"), "routing_id": 51,
                             "routing_topic": topic, "source_description": "Mapped source description."}
    answer, matched, _template, _images = responder.match_and_respond(
        question, user_id=user_id, channel="instagram"
    )
    assert matched and answer == client_qa.REGISTRY_BY_ID[approved_id].answer
    assert facts["decision_lane"] == "EXACT_APPROVED"


def test_bento_source_explicit_sondesh_switch_then_price():
    user_id = "bento-switch-sondesh"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {**_source("post", "exact-post"), "routing_id": 52, "routing_topic": "Bengali Bento"}
    assert responder.match_and_respond("Tell me about Sondesh", user_id=user_id, channel="instagram")[0] == client_qa.REGISTRY_BY_ID["sondesh_details_client"].answer
    assert responder.match_and_respond("price?", user_id=user_id, channel="instagram")[0] == client_qa.REGISTRY_BY_ID["sondesh_price_client"].answer


def test_description_and_attachment_survive_reopen_and_exact_id(mapping_db):
    database, path = mapping_db
    menu = database.list_menu_catalogue()[0]
    source = validate_route_payload({
        **_source("story", "exact-story-123"), "topic_id": None,
        "menu_id": menu["menu_id"], "menu_mode": "SELECTED",
    })
    saved = database.save_instagram_source_route(source)
    reopened = Database(str(path))
    stored = decode_route(reopened.get_instagram_source_route(saved["routing_id"]))
    assert stored["source_description"] == "This story introduces our subscription."
    assert stored["menu_id"] == menu["menu_id"]
    assert stored["active"] is True
    route, method = route_for_inbound(ad_id=None, media_id="exact-story-123", source_type="story_reply")
    assert route["media_id"] == "exact-story-123" and method == "story_media_id"


@pytest.mark.parametrize("source_type", ["story", "post", "reel"])
def test_new_story_post_and_reel_mappings_default_to_context_on(mapping_db, source_type):
    database, _path = mapping_db
    payload = _source(source_type, f"exact-{source_type}-default-on")
    payload.pop("active")
    saved = database.save_instagram_source_route(validate_route_payload(payload))
    assert decode_route(saved)["active"] is True


@pytest.mark.parametrize("source_type", ["story", "post", "reel"])
def test_explicit_context_off_is_preserved_for_legacy_mapping(mapping_db, source_type):
    database, _path = mapping_db
    payload = _source(source_type, f"exact-{source_type}-legacy-off")
    payload["active"] = False
    saved = database.save_instagram_source_route(validate_route_payload(payload))
    assert decode_route(saved)["active"] is False


def test_old_route_table_gains_blank_description_without_losing_row(tmp_path):
    path = tmp_path / "older-routes.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE instagram_source_routes (routing_id INTEGER PRIMARY KEY, source_type TEXT NOT NULL, canonical_url TEXT, shortcode TEXT, media_id TEXT, ad_id TEXT, campaign_id TEXT, routing_topic TEXT NOT NULL DEFAULT '', knowledge_scope TEXT NOT NULL DEFAULT '[]', escalation_policy TEXT NOT NULL DEFAULT 'human_if_unsupported', priority_products TEXT NOT NULL DEFAULT '[]', active INTEGER NOT NULL DEFAULT 1, created_at TEXT, updated_at TEXT, archived_at TEXT, menu_mode TEXT NOT NULL DEFAULT 'INHERIT', preview_url TEXT, media_type TEXT)")
        conn.execute("INSERT INTO instagram_source_routes (routing_id,source_type,media_id,routing_topic) VALUES (7,'story','old-meta-story','Subscription')")
    database = Database(str(path))
    row = database.get_instagram_source_route(7)
    assert row["media_id"] == "old-meta-story"
    assert row["routing_topic"] == "Subscription"
    assert row["source_description"] == ""


@pytest.mark.parametrize("media_type,permalink,shortcode", [
    ("VIDEO", "https://www.instagram.com/reel/ABC456/", "ABC456"),
    ("IMAGE", "https://www.instagram.com/p/POST456/", "POST456"),
])
def test_gallery_mapping_delete_keeps_instagram_media(mapping_db, media_type, permalink, shortcode):
    database, _path = mapping_db
    client = TestClient(app_module.app)
    headers = {"x-admin-key": config.ADMIN_API_KEY}
    topic = client.post("/admin/instagram-topics", headers=headers, json={"name": "Subscription", "description": "Client topic"}).json()["topic"]
    media = database.upsert_instagram_media({"instagram_media_id": "exact-media-456", "media_type": media_type, "permalink": permalink, "shortcode": shortcode})
    menu = database.list_menu_catalogue()[0]
    response = client.put(f"/admin/instagram-media/{media['id']}/mapping", headers=headers, json={
        "topic_id": topic["topic_id"], "source_description": "This reel is about subscription.",
        "menu_id": menu["menu_id"], "menu_mode": "SELECTED", "auto_reply_enabled": True,
    })
    assert response.status_code == 200, response.text
    assert response.json()["route"]["media_id"] == "exact-media-456"
    assert response.json()["route"]["menu_id"] == menu["menu_id"]
    assert response.json()["route"]["source_description"] == "This reel is about subscription."
    assert database.get_instagram_media(media["id"])["route_mode"] == "CUSTOM"
    if media_type == "VIDEO":
        matched_route, method = route_for_inbound(ad_id=None, media_id=None, shortcode=shortcode, source_type="reel")
        assert method == "shared_media_url"
    else:
        matched_route, method = route_for_inbound(ad_id=None, media_id=None, post_media_id="exact-media-456", source_type="post")
        assert method == "shared_post_media_id"
    assert matched_route["media_id"] == "exact-media-456"
    disabled = client.put(f"/admin/instagram-media/{media['id']}/mapping", headers=headers, json={
        "topic_id": topic["topic_id"], "source_description": "This reel is about subscription.",
        "menu_id": menu["menu_id"], "auto_reply_enabled": False,
    })
    assert disabled.status_code == 200
    assert disabled.json()["route"]["active"] is False
    assert database.get_instagram_media(media["id"])["route_mode"] == "DISABLED"
    if media_type == "VIDEO":
        disabled_route, _ = route_for_inbound(ad_id=None, media_id=None, shortcode=shortcode, source_type="reel")
    else:
        disabled_route, _ = route_for_inbound(ad_id=None, media_id=None, post_media_id="exact-media-456", source_type="post")
    assert disabled_route is None
    removed = client.delete(f"/admin/instagram-media/{media['id']}/mapping", headers=headers)
    assert removed.status_code == 200
    assert database.get_instagram_media(media["id"])["instagram_media_id"] == "exact-media-456"
    assert database.get_instagram_media(media["id"])["topic_id"] is None


def test_topic_delete_requires_replacement_and_updates_route_label(mapping_db):
    database, _path = mapping_db
    client = TestClient(app_module.app)
    headers = {"x-admin-key": config.ADMIN_API_KEY}
    old = client.post("/admin/instagram-topics", headers=headers, json={"name": "Old Topic", "description": "Old"}).json()["topic"]
    new = client.post("/admin/instagram-topics", headers=headers, json={"name": "New Topic", "description": "New"}).json()["topic"]
    route = validate_route_payload({**_source("story", "topic-story"), "topic_id": old["topic_id"]})
    saved = database.save_instagram_source_route(route)
    renamed = client.patch(f"/admin/instagram-topics/{old['topic_id']}", headers=headers, json={"name": "Renamed Topic"})
    assert renamed.status_code == 200
    assert database.get_instagram_source_route(saved["routing_id"])["routing_topic"] == "Renamed Topic"
    blocked = client.request("DELETE", f"/admin/instagram-topics/{old['topic_id']}", headers=headers, json={})
    assert blocked.status_code == 409
    deleted = client.request("DELETE", f"/admin/instagram-topics/{old['topic_id']}", headers=headers,
                             json={"replacement_topic_id": new["topic_id"]})
    assert deleted.status_code == 200
    updated = database.get_instagram_source_route(saved["routing_id"])
    assert updated["topic_id"] == new["topic_id"] and updated["routing_topic"] == "New Topic"


def test_client_mapping_ui_hides_route_mode():
    html = TestClient(app_module.app).get("/admin").text
    assert "igMediaRouteMode" not in html
    assert "igMediaCustomRoute" not in html
    assert "igMediaReview" not in html
    assert "igStorySourceDescription" in html
    assert "igMediaDescription" in html
    assert html.count("<b>Use as chatbot context</b>") == 3
    assert "Auto reply from this source" not in html
    assert "This does not send a message automatically." in html
    assert "<b>Chatbot context:</b>" in html
