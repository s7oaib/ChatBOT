"""QA-FINAL1: explicit source assets and customer data stay within authority."""
from copy import deepcopy
from unittest.mock import patch
import uuid

import pytest
from fastapi.testclient import TestClient

from camellia import app as app_module, database, handoff, menu_images, menu_review, menu_catalogue
from camellia import instagram_routing, order_intake, responder as responder_module
from camellia.config import config
from camellia.responder import responder
from camellia.session_memory import session_memory


@pytest.fixture
def customer(tmp_path, monkeypatch):
    db = database.Database(str(tmp_path / "final-routing.db"))
    for module in (database, app_module, handoff, menu_images, menu_review, menu_catalogue, responder_module):
        monkeypatch.setattr(module, "db", db)
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "ADMIN_API_KEY", "final-test-admin")
    uid = "final-" + uuid.uuid4().hex
    session_memory.clear_session(uid, "instagram")
    return uid, session_memory.get_facts(uid, "instagram"), db


def reply(customer, message):
    return responder.match_and_respond(message, user_id=customer[0], channel="instagram")


def source(customer, kind="story", description="", mode="SELECTED"):
    uid, facts, db = customer
    topic = instagram_routing.create_topic("Test Event Topic")
    db.save_menu_image_draft("aamar_mishti", "/uploads/exact-source.jpg", "fixture")
    db.publish_menu_image("aamar_mishti")
    route = {"routing_id": 99, "source_type": kind, "media_id": "exact-source",
             "topic_id": topic["topic_id"], "routing_topic": topic["name"], "active": True,
             "source_description": description, "menu_mode": mode,
             "menu_id": "aamar_mishti" if mode == "SELECTED" else None,
             "response_mode": "CUSTOM", "priority_products": [], "knowledge_scope": []}
    facts["source_route"] = route
    return route, topic


def test_topic_name_only_and_patch_preserve_all_fields(customer):
    _, _, db = customer
    client = TestClient(app_module.app)
    headers = {"x-admin-key": config.ADMIN_API_KEY}
    response = client.post("/admin/instagram-topics", headers=headers, json={"name": "Durga Puja"})
    assert response.status_code == 200
    topic_id = response.json()["topic"]["topic_id"]
    instagram_routing.update_topic(topic_id, description="Authored", default_menu_id="aamar_mishti",
                                   priority_products=["sondesh"], knowledge_scope=[], active=False, response_order="menu_first")
    before = db.get_instagram_topic(topic_id)
    assert client.patch(f"/admin/instagram-topics/{topic_id}", headers=headers, json={"name": "Renamed"}).status_code == 200
    after = db.get_instagram_topic(topic_id)
    for key in ("description", "default_menu_id", "priority_products", "knowledge_scope", "active", "response_order"):
        assert after[key] == before[key]
    assert instagram_routing.update_topic(topic_id, description="")["description"] == ""
    with pytest.raises(instagram_routing.InstagramTopicError):
        instagram_routing.create_topic("Renamed")


@pytest.mark.parametrize("kind", ["story", "post", "reel"])
@pytest.mark.parametrize("description", ["", "Operator-authored exact source text."])
def test_mapping_persists_and_delivers_exact_asset(customer, kind, description):
    route, _topic = source(customer, kind, description)
    client = TestClient(app_module.app)
    headers = {"x-admin-key": config.ADMIN_API_KEY}
    payload = {**route, "routing_id": None}
    created = client.post("/admin/instagram-routes", headers=headers, json=payload)
    assert created.status_code == 200, created.text
    saved = created.json()["route"]
    stored = customer[2].get_instagram_source_route(saved["routing_id"])
    assert stored["topic_id"] == route["topic_id"] and stored["menu_id"] == "aamar_mishti"
    for question in ("show me the menu", "send menu", "menu?", "what menu is this", "can I see the menu", "show me this menu", "what is this post about", "tell me more about this story"):
        text, matched, _template, images = reply(customer, question)
        assert matched and images == ["/uploads/exact-source.jpg"]
        assert text == description
        assert customer[1]["ordered_response_blocks"][-1] == {"kind": "IMAGE", "urls": images}


@pytest.mark.parametrize("mode", ["NONE", "INHERIT"])
def test_only_explicit_topic_default_is_inherited(customer, mode):
    route, topic = source(customer, description="Authored copy", mode=mode)
    instagram_routing.update_topic(topic["topic_id"], default_menu_id="aamar_mishti")
    answer = reply(customer, "tell me more about this story")
    assert answer[0] == "Authored copy"
    assert answer[3] == (["/uploads/exact-source.jpg"] if mode == "INHERIT" else None)


@pytest.mark.parametrize("failure", ["inactive", "missing", "unpublished"])
def test_unavailable_source_asset_never_substitutes_weekly(customer, failure):
    route, _topic = source(customer)
    if failure == "inactive":
        customer[2].update_menu_catalogue_entry("aamar_mishti", active=False)
    elif failure == "missing":
        route["menu_id"] = "not-a-menu"
    else:
        route["menu_id"] = menu_catalogue.create_menu("Event asset", "Other", "Fixture")["menu_id"]
    answer = reply(customer, "show me the menu")
    assert not answer[1] and answer[3] is None
    assert customer[1]["last_route_source"] == "HUMAN_HANDOFF"


@pytest.mark.parametrize("date", ["11 October", "11 Oct", "October 11", "11th October", "11/10", "11-10", "11 October 2026"])
def test_active_order_date_is_customer_data_not_confirmation(customer, date):
    reply(customer, "2 boxes Sondesh Gurgaon Friday dinner")
    before = deepcopy(customer[1]["order_intake_draft"])
    answer = reply(customer, date)
    draft = customer[1]["order_intake_draft"]
    assert draft["delivery_slot"] == date.title()
    assert draft["item"] == before["item"] and draft["delivery_area"] == before["delivery_area"]
    assert "https://wa.me/" in answer[0] and date.title() in answer[0]
    assert "order confirmed" not in answer[0].lower()


def test_bare_date_does_not_start_order(customer):
    reply(customer, "11 October")
    assert not customer[1].get("order_intake_draft")


def test_explicit_draft_review_preserves_fields_and_resends_link(customer):
    reply(customer, "2 boxes Sondesh Gurgaon Friday dinner")
    before = deepcopy(customer[1]["order_intake_draft"])
    answer = reply(customer, "show me my order")
    assert answer[2] == "order_intake_draft" and "https://wa.me/" in answer[0]
    assert customer[1]["order_intake_draft"] == before


@pytest.mark.parametrize("value", ["32 October", "31 February", "11/13"])
def test_invalid_date_is_not_order_data(customer, value):
    reply(customer, "2 boxes Sondesh Gurgaon Friday dinner")
    before = deepcopy(customer[1]["order_intake_draft"])
    assert not order_intake.is_order_detail_message(value)
    order_intake.update_order_draft(customer[1]["order_intake_draft"], value)
    assert customer[1]["order_intake_draft"] == before


def test_none_and_missing_inherit_never_send_generic_weekly(customer):
    route, _topic = source(customer, description="Exact copy", mode="NONE")
    assert reply(customer, "show me the menu") == ("Exact copy", True, "source_menu", None)
    route["menu_mode"] = "INHERIT"
    answer = reply(customer, "show me the menu")
    assert not answer[1] and answer[3] is None


def test_menu_only_requires_current_image_and_empty_mapping_is_rejected(customer):
    route, _topic = source(customer)
    client = TestClient(app_module.app)
    headers = {"x-admin-key": config.ADMIN_API_KEY}
    empty = {**route, "routing_id": None, "menu_id": None, "menu_mode": "NONE"}
    assert client.post("/admin/instagram-routes", headers=headers, json=empty).status_code == 400
    unready = menu_catalogue.create_menu("Unready fixture", "Other", "Fixture")["menu_id"]
    assert client.post("/admin/instagram-routes", headers=headers, json={**empty, "menu_id": unready, "menu_mode": "SELECTED"}).status_code == 400


def test_description_only_uses_own_copy_not_topic(customer):
    route, topic = source(customer, description="Source copy", mode="NONE")
    instagram_routing.update_topic(topic["topic_id"], description="Different Topic description")
    answer = reply(customer, "what is this post about")
    assert answer[0] == "Source copy" and answer[3] is None


def test_empty_legacy_custom_route_cannot_borrow_topic_description(customer):
    route, topic = source(customer, mode="NONE")
    instagram_routing.update_topic(topic["topic_id"], description="Topic classification text")
    answer = reply(customer, "tell me more")
    assert not answer[1] and answer[3] is None
    assert "Topic classification text" not in answer[0]


def test_faq_and_unknown_question_escape_preserve_draft(customer, eligible_weekly_asset):
    reply(customer, "2 boxes Sondesh Gurgaon Friday dinner")
    before = deepcopy(customer[1]["order_intake_draft"])
    assert reply(customer, "What is Bengali Bento?")[1]
    assert customer[1]["order_intake_draft"] == before
    answer = reply(customer, "which part of diamond park are you located")
    assert not answer[1] and answer[3] is None
    assert "draft is unchanged" not in answer[0]
    assert customer[1]["order_intake_draft"] == before


def test_unknown_location_creates_durable_handoff_and_suppresses(customer):
    uid, facts, db = customer
    with patch.object(app_module, "send_instagram_reply") as send, patch.object(app_module.escalator, "handle_escalation"):
        app_module._process_instagram_event(uid, "2 boxes Sondesh Gurgaon Friday dinner", "fixture")
        app_module._process_instagram_event(uid, "which part of diamond park are you located", "fixture")
        active = db.get_active_handoff("instagram", uid)
        assert active and "CUSTOMER-PROVIDED ORDER DRAFT" in active["summary"] and "UNVERIFIED" in active["summary"]
        assert "2 boxes Sondesh" in active["summary"]
        count = send.call_count
        app_module._process_instagram_event(uid, "show me the weekly menu", "fixture")
        assert send.call_count == count and len(db.get_handoffs(limit=20)) == 1


@pytest.mark.parametrize("question", ["menu?", "show me the menu", "what is the menu", "what food will be served", "home table menu", "menu for home table", "what's on the menu", "can I see the menu", "send the menu"])
@pytest.mark.parametrize("context", ["home_table_context", "last_topic", "source"])
def test_home_table_menu_handoff_has_no_assets(customer, question, context):
    if context == "source":
        route, _topic = source(customer)
        route["routing_topic"] = "Bengali Home Table"
    else:
        customer[1][context] = True if context == "home_table_context" else "bengali_home_table"
    answer = reply(customer, question)
    assert not answer[1] and answer[3] is None
    assert customer[1]["ordered_response_blocks"] == []
    assert customer[1]["last_grounding"] == "home_table_menu_requires_human"


def test_source_menu_first_uses_existing_ordered_sender(customer):
    _, topic = source(customer, description="Authored copy")
    instagram_routing.update_topic(topic["topic_id"], response_order="menu_first")
    reply(customer, "show me this menu")
    blocks = customer[1]["ordered_response_blocks"]
    assert [block["kind"] for block in blocks] == ["IMAGE", "TEXT"]
    with patch.object(app_module, "_send_or_queue_instagram_reply", return_value={"status": "sent"}) as send:
        app_module.send_instagram_response_blocks(customer[0], blocks)
    assert send.call_args_list[0].kwargs["image_url"] == ["/uploads/exact-source.jpg"]
    assert send.call_args_list[1].args[1] == "Authored copy"


def test_fresh_exact_non_home_source_supersedes_old_home_flags(customer):
    source(customer, description="New exact source copy")
    customer[1].update(home_table_context=True, last_topic="bengali_home_table", active_topic="bengali_home_table", client_qa_id="home_table_details")
    answer = reply(customer, "show me the menu")
    assert answer == ("New exact source copy", True, "source_menu", ["/uploads/exact-source.jpg"])


@pytest.mark.parametrize("question", ["Is this available?", "What allergens does this menu contain?", "which part of diamond park are you located"])
def test_source_state_cannot_supply_unsupported_business_facts(customer, question):
    source(customer, description="Exact approved source description")
    answer = reply(customer, question)
    assert not answer[1] and answer[3] is None
    assert answer[0] != "Exact approved source description"
