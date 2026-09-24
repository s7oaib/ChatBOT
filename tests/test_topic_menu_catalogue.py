"""
Phase 2: the Topic catalogue and the dynamic Menu catalogue.

Instagram Content Routing simplified to: paste link -> select Topic -> save.
Selecting a Topic pre-fills the route's menu, priority products and
knowledge scope; the client can still override any of them. Topics and
Menus are both created dynamically (no hardcoded lists) and both routing
matching precedence and the reel/post pairing work already proven remain
untouched.
"""
import io
import os

os.environ.setdefault("DB_PATH", "test_camellia.db")

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import camellia.app as app_module
import camellia.database as db_module
from camellia.database import Database
from camellia import menu_catalogue
from camellia import instagram_routing
from camellia.instagram_routing import (
    InstagramTopicError,
    create_topic,
    migrate_legacy_routing_topics,
    route_for_inbound,
    update_topic,
    validate_route_payload,
)
from camellia.menu_catalogue import MenuCatalogueError
import camellia.menu_images as menu_images_module
import camellia.responder as responder_module
from camellia.responder import compose_topic_response, responder
from camellia.session_memory import session_memory


def _png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def catalogue_db(tmp_path, monkeypatch):
    """A fresh Database — init_db() seeds the five built-in menus automatically."""
    database = Database(str(tmp_path / "catalogue.db"))
    monkeypatch.setattr(db_module, "db", database)
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(instagram_routing, "db", database, raising=False)
    monkeypatch.setattr(responder_module, "db", database)
    monkeypatch.setattr(menu_catalogue, "db", database)
    monkeypatch.setattr(menu_images_module, "db", database)
    monkeypatch.setattr(menu_images_module.weekly_flyer, "uploads_dir", lambda: str(tmp_path / "uploads"))
    return database


@pytest.fixture
def admin_client(catalogue_db, monkeypatch):
    db_module.db.init_db()
    # This contract suite exercises authenticated Admin APIs. Keep it
    # self-contained instead of depending on a developer's local .env file.
    monkeypatch.setattr(app_module.config, "ADMIN_API_KEY", "catalogue-test-key")
    client = TestClient(app_module.app)
    headers = {"X-Admin-Key": app_module.config.ADMIN_API_KEY} if app_module.config.ADMIN_API_KEY else {}
    return client, headers


# --- Menu catalogue ---------------------------------------------------

def test_builtin_menus_are_seeded_automatically(catalogue_db):
    menus = {m["menu_id"] for m in catalogue_db.list_menu_catalogue()}
    assert menus == {"aamar_mishti", "signature", "weekly_vegetarian", "weekly_non_vegetarian", "bento"}


def test_create_new_menu_without_any_code_change(catalogue_db):
    entry = menu_catalogue.create_menu(
        "Meal Subscription", "Subscription",
        "Weekly Bengali home-style meal subscription with rotating menus.",
    )
    assert entry["menu_id"] == "meal_subscription"
    assert entry["is_builtin"] == 0
    again = menu_catalogue.get_menu("meal_subscription")
    assert again["name"] == "Meal Subscription"


def test_duplicate_menu_name_rejected_safely(catalogue_db):
    menu_catalogue.create_menu("Meal Subscription", "Subscription", "First one.")
    with pytest.raises(MenuCatalogueError):
        menu_catalogue.create_menu("Meal Subscription", "Subscription", "A second one.")


def test_menu_without_name_or_description_rejected(catalogue_db):
    with pytest.raises(MenuCatalogueError):
        menu_catalogue.create_menu("", "Other", "Has a description.")
    with pytest.raises(MenuCatalogueError):
        menu_catalogue.create_menu("No Description", "Other", "")


def test_new_menu_appears_in_the_catalogue_listing_immediately(catalogue_db):
    menu_catalogue.create_menu("Meal Subscription", "Subscription", "Weekly rotating meals.")
    names = {m["name"] for m in menu_catalogue.list_menus()}
    assert "Meal Subscription" in names
    assert "Aamar Mishti Collection" in names  # built-ins still present


def test_menu_metadata_edit_does_not_replace_its_image(catalogue_db, monkeypatch):
    from camellia import menu_images

    monkeypatch.setattr(menu_images.weekly_flyer, "uploads_dir", lambda: str(catalogue_db.db_path) + "-metadata")
    entry = menu_catalogue.create_menu("Editable Menu", "Other", "Original description.")
    menu_images.save_draft(entry["menu_id"], _png(), "image/png", "tester")
    published = menu_images.publish(entry["menu_id"])

    updated = menu_catalogue.update_menu(
        entry["menu_id"], name="Renamed Menu", category="Event",
        description="Updated description.", active=False,
    )

    assert updated["name"] == "Renamed Menu"
    assert updated["category"] == "Event"
    assert updated["description"] == "Updated description."
    assert updated["active"] == 0
    assert menu_images.current_url(entry["menu_id"]) == published["image_path"]


def test_menu_metadata_edit_rejects_blank_description(catalogue_db):
    entry = menu_catalogue.create_menu("Editable Menu", "Other", "Original description.")
    with pytest.raises(MenuCatalogueError):
        menu_catalogue.update_menu(entry["menu_id"], description="   ")


# --- Menu image lifecycle on a dynamic menu -----------------------------

def test_upload_replace_archive_restore_on_a_new_menu(catalogue_db, monkeypatch):
    from camellia import menu_images
    monkeypatch.setattr(menu_images.weekly_flyer, "uploads_dir", lambda: str(catalogue_db.db_path) + "-uploads")
    entry = menu_catalogue.create_menu("Meal Subscription", "Subscription", "Weekly rotating meals.")
    menu_id = entry["menu_id"]

    draft = menu_images.save_draft(menu_id, _png(), "image/png", "tester")
    assert draft["status"] == "draft"
    published = menu_images.publish(menu_id)
    assert published["status"] == "current"

    replaced_draft = menu_images.save_draft(menu_id, _png("blue"), "image/png", "tester")
    republished = menu_images.publish(menu_id)
    assert republished["image_path"] != published["image_path"]

    archived = menu_images.archive(menu_id)
    assert archived["status"] == "archived"
    history = menu_images.history(menu_id)
    assert len(history) >= 2
    restored = menu_images.restore(menu_id, published["id"])
    assert restored["status"] == "current"
    assert menu_images.current_url(menu_id) == published["image_path"]


def test_new_menu_image_survives_a_fresh_database_connection(tmp_path, monkeypatch):
    """Durability: a new Database() pointed at the same file sees the same image."""
    from camellia import menu_images
    db_path = str(tmp_path / "durable.db")
    uploads = str(tmp_path / "uploads")
    first = Database(db_path)
    monkeypatch.setattr(menu_images, "db", first)
    monkeypatch.setattr(menu_images.weekly_flyer, "uploads_dir", lambda: uploads)
    entry = menu_catalogue_create_on(first, "Meal Subscription", "Subscription", "Weekly rotating meals.")
    menu_images.save_draft(entry["menu_id"], _png(), "image/png", "tester")
    published = menu_images.publish(entry["menu_id"])

    second = Database(db_path)  # simulates a process restart
    monkeypatch.setattr(menu_images, "db", second)
    assert menu_images.current_url(entry["menu_id"]) == published["image_path"]


def menu_catalogue_create_on(database, name, category, description):
    return database.create_menu_catalogue_entry(
        menu_catalogue._slugify(name), name, category, description, active=True, is_builtin=False,
    )


def test_no_ocr_no_fact_extraction_from_the_uploaded_image(catalogue_db, monkeypatch):
    """The create-menu endpoint stores exactly the text typed, nothing derived from the image."""
    from camellia import menu_images
    monkeypatch.setattr(menu_images.weekly_flyer, "uploads_dir", lambda: str(catalogue_db.db_path) + "-up2")
    entry = menu_catalogue.create_menu("Meal Subscription", "Subscription", "Weekly rotating meals.")
    menu_images.save_draft(entry["menu_id"], _png(), "image/png", "tester")
    menu_images.publish(entry["menu_id"])
    stored = menu_catalogue.get_menu(entry["menu_id"])
    assert stored["description"] == "Weekly rotating meals."
    assert "price" not in stored["description"].lower()
    assert "₹" not in stored["description"]


# --- Topic catalogue -----------------------------------------------------

def test_create_topic_with_default_menu_and_priorities(catalogue_db):
    topic = create_topic(
        "Wedding / Event Sondesh",
        "Explore handcrafted Bengali Sondesh suitable for weddings, events and celebrations.",
        default_menu_id="aamar_mishti",
        priority_products=["jolbhora", "kanchagolla", "aamar_mishti"],
    )
    assert topic["topic_id"] == "wedding_event_sondesh"
    assert topic["default_menu_id"] == "aamar_mishti"


def test_duplicate_topic_name_rejected(catalogue_db):
    create_topic("Meal Subscription", "Weekly rotating meals.")
    with pytest.raises(InstagramTopicError):
        create_topic("Meal Subscription", "A different description.")


def test_topic_requires_name_and_description(catalogue_db):
    with pytest.raises(InstagramTopicError):
        create_topic("", "Has a description.")
    with pytest.raises(InstagramTopicError):
        create_topic("No Description", "")


def test_topic_with_unknown_default_menu_rejected(catalogue_db):
    with pytest.raises(InstagramTopicError):
        create_topic("Some Topic", "A description.", default_menu_id="not_a_real_menu")


def test_inactive_menus_cannot_be_new_topic_defaults_or_route_overrides(catalogue_db):
    menu = menu_catalogue.create_menu("Inactive Menu", "Other", "Not currently selectable.", active=False)
    with pytest.raises(InstagramTopicError, match="must be active"):
        create_topic("Inactive Default", "A description.", default_menu_id=menu["menu_id"])

    topic = create_topic("Active Topic", "A description.")
    with pytest.raises(ValueError, match="inactive"):
        validate_route_payload({
            "source_type": "story", "media_id": "story-inactive-menu",
            "topic_id": topic["topic_id"], "menu_mode": "SELECTED",
            "menu_id": menu["menu_id"], "active": True,
        })


def test_topic_description_is_editable(catalogue_db):
    topic = create_topic("Meal Subscription", "Original description.")
    updated = update_topic(topic["topic_id"], description="Updated, clearer description.")
    assert updated["description"] == "Updated, clearer description."
    assert update_topic(topic["topic_id"])["name"] == "Meal Subscription"  # no-op update is safe


def test_topic_response_order_defaults_and_persists(catalogue_db):
    topic = create_topic("Reply Order Topic", "A description.")
    assert topic["response_order"] == "description_first"
    updated = update_topic(topic["topic_id"], response_order="menu_first")
    assert updated["response_order"] == "menu_first"
    assert instagram_routing.decode_topic(catalogue_db.get_instagram_topic(topic["topic_id"]))["response_order"] == "menu_first"


def test_topic_and_exact_source_mappings_survive_database_reopen(tmp_path, monkeypatch):
    """A Topic and Story/Post/Reel mappings must survive a process restart."""
    db_path = str(tmp_path / "topic-restart.db")
    first = Database(db_path)
    monkeypatch.setattr(db_module, "db", first)
    monkeypatch.setattr(app_module, "db", first)
    monkeypatch.setattr(instagram_routing, "db", first, raising=False)
    monkeypatch.setattr(responder_module, "db", first)

    topic = create_topic("Restart Topic", "Persisted Topic description.")
    common = {
        "topic_id": topic["topic_id"],
        "routing_topic": topic["name"],
        "priority_products": ["jolbhora"],
        "knowledge_scope": [],
        "menu_mode": "NONE",
        "active": True,
    }
    first.save_instagram_source_route({
        **common, "source_type": "story", "media_id": "story-meta-exact",
    })
    first.save_instagram_source_route({
        **common, "source_type": "post", "media_id": "post-meta-exact",
        "canonical_url": "https://www.instagram.com/p/PostExact123/",
        "shortcode": "PostExact123",
    })
    first.save_instagram_source_route({
        **common, "source_type": "reel", "canonical_url": "https://www.instagram.com/reel/ReelExact123/",
        "shortcode": "ReelExact123",
    })

    second = Database(db_path)
    monkeypatch.setattr(db_module, "db", second)
    monkeypatch.setattr(app_module, "db", second)
    monkeypatch.setattr(instagram_routing, "db", second, raising=False)
    monkeypatch.setattr(responder_module, "db", second)

    persisted_topic = second.get_instagram_topic(topic["topic_id"])
    assert persisted_topic["name"] == "Restart Topic"
    assert len(second.list_instagram_topics()) == 1
    persisted_routes = second.list_instagram_source_routes()
    assert {route["media_id"] for route in persisted_routes if route["media_id"]} == {"story-meta-exact", "post-meta-exact"}
    assert {route["shortcode"] for route in persisted_routes if route["shortcode"]} == {"ReelExact123", "PostExact123"}
    assert all(route["topic_id"] == topic["topic_id"] for route in persisted_routes)

    story, story_method = route_for_inbound(ad_id=None, media_id="story-meta-exact", source_type="story_reply")
    post, post_method = route_for_inbound(ad_id=None, media_id=None, post_media_id="post-meta-exact", source_type="post")
    reel, reel_method = route_for_inbound(ad_id=None, media_id=None, shortcode="ReelExact123", source_type="reel")
    assert story["topic_id"] == topic["topic_id"] and story_method == "story_media_id"
    assert post["topic_id"] == topic["topic_id"] and post_method == "shared_post_media_id"
    assert reel["topic_id"] == topic["topic_id"] and reel_method == "shared_media_url"


def test_shared_topic_response_renderer_keeps_both_orders_explicit():
    description = "Topic description."
    menu = "Approved menu details."
    first = compose_topic_response("Sondesh", description, menu, "description_first")
    second = compose_topic_response("Sondesh", description, menu, "menu_first")
    assert first.index(description) < first.index(menu)
    assert second.index(menu) < second.index(description)


def _set_source_route_for_test(topic, *, source_type, menu_mode, menu_id=None, priority_products=None):
    return {
        "source_type": source_type,
        "topic_id": topic["topic_id"],
        "routing_topic": topic["name"],
        "menu_id": menu_id,
        "menu_mode": menu_mode,
        "priority_products": priority_products or [],
        "active": True,
    }


@pytest.mark.parametrize("source_type", ["post", "reel", "ad"])
def test_explicit_none_sends_description_only_for_every_instagram_source(catalogue_db, bypass_ad_gate, source_type):
    topic = create_topic(
        "Sondesh", "TEST DESCRIPTION ONLY", default_menu_id="aamar_mishti", priority_products=["jolbhora"],
    )
    uid = f"none_{source_type}"
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    facts["source_route"] = _set_source_route_for_test(
        topic, source_type=source_type, menu_mode="NONE", priority_products=["jolbhora"],
    )
    facts["source_match_method"] = "test"

    text, matched, template_id, _images = responder.match_and_respond(
        "Tell me more", user_id=uid, channel="instagram",
    )

    assert matched is True
    assert template_id == "source_topic_description_only"
    assert text == "TEST DESCRIPTION ONLY"
    assert "Jolbhora" not in text
    assert "₹" not in text


def test_selected_menu_does_not_turn_priority_product_into_an_answer(catalogue_db, bypass_ad_gate):
    topic = create_topic(
        "Sondesh", "TEST DESCRIPTION ONLY", default_menu_id="aamar_mishti", priority_products=["jolbhora"],
    )
    uid = "selected_sondesh_menu"
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    facts["source_route"] = _set_source_route_for_test(
        topic, source_type="post", menu_mode="SELECTED", menu_id="aamar_mishti", priority_products=["jolbhora"],
    )
    facts["source_match_method"] = "test"

    text, matched, _template_id, _images = responder.match_and_respond(
        "Tell me more", user_id=uid, channel="instagram",
    )

    assert matched is True
    assert text == "TEST DESCRIPTION ONLY"
    assert "Jolbhora" not in text


def test_topic_identity_does_not_inherit_menu_when_none_is_explicit(catalogue_db, bypass_ad_gate):
    topic = create_topic(
        "Another Topic", "ANOTHER DESCRIPTION", default_menu_id="signature", priority_products=["kanchagolla"],
    )
    uid = "none_other_topic"
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    facts["source_route"] = _set_source_route_for_test(
        topic, source_type="reel", menu_mode="NONE", priority_products=["kanchagolla"],
    )
    facts["source_match_method"] = "test"

    text, _matched, _template_id, _images = responder.match_and_respond(
        "Tell me more", user_id=uid, channel="instagram",
    )

    assert text == "ANOTHER DESCRIPTION"
    assert "Kanchagolla" not in text
    assert "Signature Collection" not in text


def test_preview_and_runtime_use_identical_none_composition(catalogue_db, admin_client, bypass_ad_gate):
    client, headers = admin_client
    topic = create_topic(
        "Preview NONE Topic", "PREVIEW DESCRIPTION ONLY", default_menu_id="aamar_mishti", priority_products=["jolbhora"],
    )
    preview = client.get(
        f"/admin/instagram-routes/preview?topic_id={topic['topic_id']}&menu_mode=NONE&product=jolbhora",
        headers=headers,
    )
    assert preview.status_code == 200
    preview_text = preview.json()["approved_response"]

    uid = "preview_runtime_none"
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    facts["source_route"] = _set_source_route_for_test(
        topic, source_type="post", menu_mode="NONE", priority_products=["jolbhora"],
    )
    facts["source_match_method"] = "test"
    runtime_text, _matched, _template_id, _images = responder.match_and_respond(
        "Tell me more", user_id=uid, channel="instagram",
    )

    assert preview_text == runtime_text == "PREVIEW DESCRIPTION ONLY"


def test_topic_response_order_applies_to_reel_post_and_ad_contexts(catalogue_db, bypass_ad_gate):
    topic = create_topic("Ordered Topic", "Description from the Topic.", default_menu_id="aamar_mishti", response_order="menu_first", priority_products=["jolbhora"])
    for source_type in ("reel", "post", "ad"):
        uid = f"ordered_{source_type}"
        session_memory.clear_session(uid, "instagram")
        facts = session_memory.get_facts(uid, "instagram")
        facts["source_route"] = {"source_type": source_type, "topic_id": topic["topic_id"], "routing_topic": topic["name"], "menu_id": "aamar_mishti", "priority_products": ["jolbhora"], "active": True}
        facts["source_match_method"] = "test"
        text, matched, _template_id, _images = responder.match_and_respond("Tell me more", user_id=uid, channel="instagram")
        assert matched is True
        assert text == "Description from the Topic."
        assert "Jolbhora" not in text


def test_existing_topic_without_reply_order_uses_description_first(catalogue_db):
    with catalogue_db.get_connection() as conn:
        conn.execute("INSERT INTO instagram_topics (topic_id, name, description, priority_products, knowledge_scope, active) VALUES (?, ?, ?, ?, ?, ?)", ("legacy_order", "Legacy Order", "Legacy description.", "[]", "[]", 1))
        conn.commit()
    assert instagram_routing.decode_topic(catalogue_db.get_instagram_topic("legacy_order"))["response_order"] == "description_first"


def test_topic_appears_dynamically_in_admin_listing(admin_client):
    client, headers = admin_client
    if not headers:
        pytest.skip("ADMIN_API_KEY not configured")
    create_topic("Meal Subscription", "Weekly rotating meals.")
    resp = client.get("/admin/instagram-topics", headers=headers)
    names = {t["name"] for t in resp.json()["topics"]}
    assert "Meal Subscription" in names


def test_topic_priority_products_cross_the_api_as_a_real_list_not_a_json_string(admin_client):
    """
    Found live in the browser: the admin JS did
    `topic.priority_products.length` expecting an array, but the raw DB row
    stores it as a JSON-encoded TEXT column — decode_route() already unpacks
    this for routes; decode_topic() has to do the same for topics, or the
    frontend silently iterates individual characters of the JSON string.
    """
    client, headers = admin_client
    if not headers:
        pytest.skip("ADMIN_API_KEY not configured")
    resp = client.post("/admin/instagram-topics", headers=headers, json={
        "name": "Wedding / Event Sondesh",
        "description": "A description.",
        "priority_products": ["jolbhora", "kanchagolla"],
        "knowledge_scope": ["prices"],
    })
    assert resp.status_code == 200
    topic = resp.json()["topic"]
    assert topic["priority_products"] == ["jolbhora", "kanchagolla"]
    assert topic["knowledge_scope"] == ["prices"]
    assert isinstance(topic["active"], bool)

    listed = client.get("/admin/instagram-topics", headers=headers).json()["topics"][0]
    assert listed["priority_products"] == ["jolbhora", "kanchagolla"]

    updated = client.patch(
        f"/admin/instagram-topics/{topic['topic_id']}", headers=headers,
        json={"priority_products": ["aamar_mishti"]},
    ).json()["topic"]
    assert updated["priority_products"] == ["aamar_mishti"]


def test_topic_reply_order_crosses_create_and_edit_api(admin_client):
    client, headers = admin_client
    if not headers:
        pytest.skip("ADMIN_API_KEY not configured")
    created = client.post("/admin/instagram-topics", headers=headers, json={
        "name": "API Reply Order", "description": "A description.", "response_order": "menu_first",
    })
    assert created.status_code == 200
    topic = created.json()["topic"]
    assert topic["response_order"] == "menu_first"
    edited = client.patch(f"/admin/instagram-topics/{topic['topic_id']}", headers=headers, json={"response_order": "description_first"})
    assert edited.status_code == 200
    assert edited.json()["topic"]["response_order"] == "description_first"


def test_topic_preview_uses_saved_reply_order(admin_client):
    client, headers = admin_client
    if not headers:
        pytest.skip("ADMIN_API_KEY not configured")
    created = client.post("/admin/instagram-topics", headers=headers, json={
        "name": "Preview Reply Order", "description": "Preview description.",
        "default_menu_id": "aamar_mishti", "priority_products": ["jolbhora"], "response_order": "menu_first",
    }).json()["topic"]
    preview = client.get(f"/admin/instagram-routes/preview?topic_id={created['topic_id']}", headers=headers)
    assert preview.status_code == 200
    data = preview.json()
    assert data["response_order"] == "menu_first"
    assert data["approved_response"].index(data["menu_response"].splitlines()[0]) < data["approved_response"].index("Preview description.")


# --- Route <-> Topic -------------------------------------------------

def test_route_stores_topic_reference_and_inherits_defaults(catalogue_db):
    topic = create_topic(
        "Wedding / Event Sondesh", "Handcrafted Sondesh for weddings and events.",
        default_menu_id="aamar_mishti", priority_products=["jolbhora", "kanchagolla"],
    )
    clean = validate_route_payload({"url": "https://www.instagram.com/reel/DdAnO6MTok1/", "topic_id": topic["topic_id"]})
    assert clean["topic_id"] == topic["topic_id"]
    assert clean["menu_id"] == "aamar_mishti"
    assert clean["menu_mode"] == "INHERIT"
    assert clean["priority_products"] == ["jolbhora", "kanchagolla"]
    assert clean["routing_topic"] == "Wedding / Event Sondesh"


def test_route_can_override_topic_priority_products(catalogue_db):
    topic = create_topic(
        "Wedding / Event Sondesh", "Handcrafted Sondesh for weddings and events.",
        priority_products=["jolbhora"],
    )
    clean = validate_route_payload({
        "url": "https://www.instagram.com/reel/DdAnO6MTok1/", "topic_id": topic["topic_id"],
        "priority_products": ["kanchagolla"],
    })
    assert clean["priority_products"] == ["kanchagolla"]


def test_route_can_override_topic_default_menu(catalogue_db):
    topic = create_topic("Wedding / Event Sondesh", "A description.", default_menu_id="aamar_mishti")
    clean = validate_route_payload({
        "url": "https://www.instagram.com/reel/DdAnO6MTok1/", "topic_id": topic["topic_id"], "menu_id": "signature",
    })
    assert clean["menu_id"] == "signature"
    assert clean["menu_mode"] == "SELECTED"


def test_media_preview_metadata_is_persisted_without_changing_route_identity(catalogue_db):
    route = catalogue_db.save_instagram_source_route({
        "source_type": "reel",
        "canonical_url": "https://www.instagram.com/reel/DdAnO6MTok1/",
        "shortcode": "DdAnO6MTok1",
        "routing_topic": "Sondesh",
        "menu_mode": "NONE",
        "preview_url": "https://cdn.example/reel-cover.jpg",
        "media_type": "VIDEO",
        "active": True,
    })
    metadata_only = catalogue_db.update_instagram_source_route_metadata(
        route["routing_id"], preview_url="https://cdn.example/refreshed-cover.jpg", media_type="VIDEO",
    )
    assert metadata_only["canonical_url"] == route["canonical_url"]
    assert metadata_only["shortcode"] == route["shortcode"]
    assert metadata_only["menu_mode"] == "NONE"
    assert metadata_only["preview_url"] == "https://cdn.example/refreshed-cover.jpg"
    matched, method = route_for_inbound(ad_id=None, media_id=None, shortcode="DdAnO6MTok1")
    assert matched["routing_id"] == route["routing_id"]


def test_admin_preview_fallback_is_separate_from_route_identity(catalogue_db, admin_client):
    client, headers = admin_client
    route = catalogue_db.save_instagram_source_route({
        "source_type": "post",
        "canonical_url": "https://www.instagram.com/p/PreviewOnly123/",
        "shortcode": "PreviewOnly123",
        "routing_topic": "Preview Topic",
        "menu_mode": "NONE",
        "active": True,
    })
    original_identity = (route["routing_id"], route["media_id"], route["canonical_url"], route["menu_mode"])
    response = client.post(
        f"/admin/instagram-routes/{route['routing_id']}/preview",
        files={"file": ("preview.png", _png(), "image/png")},
        headers=headers,
    )
    assert response.status_code == 200
    listed = client.get("/admin/instagram-routes", headers=headers).json()["routes"]
    saved = next(item for item in listed if item["routing_id"] == route["routing_id"])
    assert saved["admin_preview_url"].startswith("/uploads/instagram-previews/")
    assert (saved["routing_id"], saved["media_id"], saved["canonical_url"], saved["menu_mode"]) == original_identity
    removed = client.delete(f"/admin/instagram-routes/{route['routing_id']}/preview", headers=headers)
    assert removed.status_code == 200
    listed = client.get("/admin/instagram-routes", headers=headers).json()["routes"]
    assert next(item for item in listed if item["routing_id"] == route["routing_id"])["admin_preview_url"] is None


def test_admin_preview_validates_and_compacts_large_images(catalogue_db, admin_client, tmp_path):
    client, headers = admin_client
    route = catalogue_db.save_instagram_source_route({
        "source_type": "reel",
        "canonical_url": "https://www.instagram.com/reel/CoverCompact123/",
        "shortcode": "CoverCompact123",
        "routing_topic": "Cover Topic",
        "menu_mode": "NONE",
        "active": True,
    })
    invalid = client.post(
        f"/admin/instagram-routes/{route['routing_id']}/preview",
        files={"file": ("cover.txt", b"not-an-image", "text/plain")},
        headers=headers,
    )
    assert invalid.status_code == 400

    noisy = Image.frombytes("RGB", (1800, 1800), os.urandom(1800 * 1800 * 3))
    stream = io.BytesIO()
    noisy.save(stream, "JPEG", quality=100)
    assert len(stream.getvalue()) > 2 * 1024 * 1024
    uploaded = client.post(
        f"/admin/instagram-routes/{route['routing_id']}/preview",
        files={"file": ("large.jpg", stream.getvalue(), "image/jpeg")},
        headers=headers,
    )
    assert uploaded.status_code == 200
    saved = catalogue_db.get_instagram_route_preview(route["routing_id"])
    assert saved["content_type"] == "image/jpeg"
    stored = str(tmp_path / "uploads" / "instagram-previews" / saved["image_path"].rsplit("/", 1)[-1])
    with Image.open(stored) as image:
        assert max(image.size) <= 640
    assert catalogue_db.get_instagram_source_route(route["routing_id"])["routing_id"] == route["routing_id"]


def test_route_can_save_explicit_none_without_topic_default(catalogue_db):
    topic = create_topic("Sondesh NONE", "Description.", default_menu_id="aamar_mishti")
    clean = validate_route_payload({
        "url": "https://www.instagram.com/reel/DdAnO6MTok1/",
        "topic_id": topic["topic_id"], "menu_id": "", "menu_mode": "NONE",
    })
    assert clean["menu_id"] is None
    assert clean["menu_mode"] == "NONE"


def test_unknown_topic_id_rejected(catalogue_db):
    with pytest.raises(ValueError):
        validate_route_payload({"url": "https://www.instagram.com/reel/DdAnO6MTok1/", "topic_id": "not_real"})


# --- Migration -------------------------------------------------------

def test_migration_backfills_topic_id_from_free_text(catalogue_db):
    row = catalogue_db.save_instagram_source_route({
        "source_type": "reel", "canonical_url": "https://www.instagram.com/reel/DdAnO6MTok1/",
        "shortcode": "DdAnO6MTok1", "routing_topic": "Wedding / Event Sondesh",
        "priority_products": ["jolbhora", "kanchagolla"], "knowledge_scope": [], "active": True,
    })
    migrated = migrate_legacy_routing_topics()
    assert migrated == 1
    updated = catalogue_db.get_instagram_source_route(row["routing_id"])
    assert updated["topic_id"] is not None
    topic = catalogue_db.get_instagram_topic(updated["topic_id"])
    assert topic["name"] == "Wedding / Event Sondesh"


def test_migration_reuses_an_existing_topic_with_the_same_name(catalogue_db):
    create_topic("Wedding / Event Sondesh", "Already described by an admin.")
    row = catalogue_db.save_instagram_source_route({
        "source_type": "reel", "canonical_url": "https://www.instagram.com/reel/DdAnO6MTok1/",
        "shortcode": "DdAnO6MTok1", "routing_topic": "Wedding / Event Sondesh",
        "priority_products": [], "knowledge_scope": [], "active": True,
    })
    migrate_legacy_routing_topics()
    updated = catalogue_db.get_instagram_source_route(row["routing_id"])
    topic = catalogue_db.get_instagram_topic(updated["topic_id"])
    assert topic["description"] == "Already described by an admin."  # not overwritten


def test_migration_is_idempotent_and_does_not_duplicate(catalogue_db):
    catalogue_db.save_instagram_source_route({
        "source_type": "reel", "canonical_url": "https://www.instagram.com/reel/DdAnO6MTok1/",
        "shortcode": "DdAnO6MTok1", "routing_topic": "Wedding / Event Sondesh",
        "priority_products": [], "knowledge_scope": [], "active": True,
    })
    migrate_legacy_routing_topics()
    second_run = migrate_legacy_routing_topics()
    assert second_run == 0
    assert len(catalogue_db.list_instagram_topics()) == 1


def test_migration_never_unarchives_an_archived_route(catalogue_db):
    row = catalogue_db.save_instagram_source_route({
        "source_type": "reel", "canonical_url": "https://www.instagram.com/reel/DdAnO6MTok1/",
        "shortcode": "DdAnO6MTok1", "routing_topic": "Old Campaign",
        "priority_products": [], "knowledge_scope": [], "active": True,
    })
    catalogue_db.archive_instagram_source_route(row["routing_id"])
    migrate_legacy_routing_topics()
    updated = catalogue_db.get_instagram_source_route(row["routing_id"])
    assert updated["archived_at"] is not None  # still archived
    assert updated["topic_id"] is not None  # but topic_id was still backfilled


def test_migration_does_not_touch_a_route_with_no_topic_text(catalogue_db):
    catalogue_db.save_instagram_source_route({
        "source_type": "ad", "ad_id": "ad_1", "routing_topic": "",
        "priority_products": [], "knowledge_scope": [], "active": True,
    })
    assert migrate_legacy_routing_topics() == 0


# --- Conversational context: Topic description + linked menu -------------

@pytest.fixture
def bypass_ad_gate(monkeypatch):
    monkeypatch.setattr(app_module.config, "BYPASS_AD_GATE", True)
    return True


def test_tell_me_more_surfaces_the_topic_description(catalogue_db, bypass_ad_gate):
    topic = create_topic(
        "Wedding / Event Sondesh", "Explore handcrafted Bengali Sondesh for weddings and celebrations.",
        default_menu_id="aamar_mishti", priority_products=["jolbhora"],
    )
    route = catalogue_db.save_instagram_source_route({
        "source_type": "reel", "canonical_url": "https://www.instagram.com/reel/DdAnO6MTok1/",
        "shortcode": "DdAnO6MTok1", "routing_topic": topic["name"], "topic_id": topic["topic_id"],
        "menu_id": "aamar_mishti", "priority_products": ["jolbhora"], "knowledge_scope": [], "active": True,
    })
    uid = "topic_ctx_user"
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    facts["source_route"] = instagram_routing.decode_route(route)
    facts["source_match_method"] = "shared_media_url"

    text, matched, template_id, _images = responder.match_and_respond("Tell me more", user_id=uid, channel="instagram")
    assert "Explore handcrafted Bengali Sondesh" in text


def test_explicit_intent_still_overrides_topic_context(catalogue_db, bypass_ad_gate):
    topic = create_topic(
        "Wedding / Event Sondesh", "Explore handcrafted Bengali Sondesh for weddings.",
        priority_products=["jolbhora"],
    )
    route = catalogue_db.save_instagram_source_route({
        "source_type": "reel", "canonical_url": "https://www.instagram.com/reel/DdAnO6MTok1/",
        "shortcode": "DdAnO6MTok1", "routing_topic": topic["name"], "topic_id": topic["topic_id"],
        "priority_products": ["jolbhora"], "knowledge_scope": [], "active": True,
    })
    uid = "topic_explicit_user"
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    facts["source_route"] = instagram_routing.decode_route(route)
    facts["source_match_method"] = "shared_media_url"

    text, matched, template_id, _images = responder.match_and_respond(
        "What about Kanchagolla?", user_id=uid, channel="instagram",
    )
    used = bool(session_memory.get_facts(uid, "instagram").get("source_context_used"))
    assert used is False
    assert "jolbhora" not in (template_id or "")


def test_priority_product_safety_is_unaffected_by_topics(catalogue_db, bypass_ad_gate):
    """Delivery intent still overrides source/topic context."""
    topic = create_topic("Wedding / Event Sondesh", "A description.", priority_products=["jolbhora"])
    route = catalogue_db.save_instagram_source_route({
        "source_type": "reel", "canonical_url": "https://www.instagram.com/reel/DdAnO6MTok1/",
        "shortcode": "DdAnO6MTok1", "routing_topic": topic["name"], "topic_id": topic["topic_id"],
        "priority_products": ["jolbhora"], "knowledge_scope": [], "active": True,
    })
    uid = "topic_delivery_user"
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    facts["source_route"] = instagram_routing.decode_route(route)
    facts["source_match_method"] = "shared_media_url"

    responder.match_and_respond("Do you deliver to Delhi?", user_id=uid, channel="instagram")
    used = bool(session_memory.get_facts(uid, "instagram").get("source_context_used"))
    assert used is False


# --- Regression: existing routing must remain unaffected -----------------

def test_route_with_no_topic_id_behaves_exactly_as_before(catalogue_db):
    """A route created the old way (free-text routing_topic, no topic_id) still validates fine."""
    clean = validate_route_payload({
        "url": "https://www.instagram.com/reel/DdAnO6MTok1/",
        "routing_topic": "Some free-text topic",
        "priority_products": ["jolbhora"],
    })
    assert clean["topic_id"] is None
    assert clean["routing_topic"] == "Some free-text topic"
    assert clean["menu_id"] is None


def test_shared_reel_routing_still_resolves(catalogue_db):
    catalogue_db.save_instagram_source_route({
        "source_type": "reel", "canonical_url": "https://www.instagram.com/reel/DdAnO6MTok1/",
        "shortcode": "DdAnO6MTok1", "routing_topic": "Wedding / Event Sondesh",
        "priority_products": ["jolbhora"], "knowledge_scope": [], "active": True,
    })
    route, method = route_for_inbound(ad_id=None, media_id=None, shortcode="DdAnO6MTok1")
    assert method == "shared_media_url"
    assert route["routing_topic"] == "Wedding / Event Sondesh"


def test_ad_id_routing_still_resolves(catalogue_db):
    catalogue_db.save_instagram_source_route({
        "source_type": "ad", "ad_id": "ad_999", "routing_topic": "Ad campaign",
        "priority_products": [], "knowledge_scope": [], "active": True,
    })
    route, method = route_for_inbound(ad_id="ad_999", media_id=None)
    assert method == "ad_id"


def test_post_pairing_still_unaffected_by_topic_changes(catalogue_db):
    route = catalogue_db.save_instagram_source_route({
        "source_type": "post", "canonical_url": "https://www.instagram.com/p/Dc2PmrQE3Y4/",
        "shortcode": "Dc2PmrQE3Y4", "routing_topic": "norem pak sondesh",
        "priority_products": [], "knowledge_scope": [], "active": True,
    })
    pairing = catalogue_db.start_post_pairing(route["routing_id"], ttl_seconds=600, started_by="admin")
    bound = catalogue_db.bind_post_pairing(pairing["id"], "17930882778146779")
    assert bound["status"] == "bound"
    matched, method = route_for_inbound(ad_id=None, media_id=None, post_media_id="17930882778146779")
    assert method == "shared_post_media_id"


# --- Admin auth on new endpoints ------------------------------------------

def test_new_endpoints_require_admin_auth():
    from fastapi.testclient import TestClient as TC
    import camellia.database as dbm
    dbm.db.init_db()
    client = TC(app_module.app)
    assert client.get("/admin/menu-catalogue").status_code in (401, 403, 503)
    assert client.get("/admin/instagram-topics").status_code in (401, 403, 503)
    assert client.post("/admin/instagram-topics").status_code in (401, 403, 503)
    assert client.get("/admin/menu-manager/images").status_code in (401, 403, 503)
    assert client.get("/admin/instagram-routes/preview").status_code in (401, 403, 503)
