"""One current weekly authority; canonical text survives optional assets."""
import copy
import json
import uuid
from datetime import date, datetime, timezone

import pytest

from camellia import client_qa, menu_catalogue, menu_dates, menu_images, menu_review, order_intake
from camellia.config import config
from camellia.database import db
from camellia.session_memory import session_memory
from test_qa_menu1 import respond
from test_qa_menu2 import replacement_chain
from test_weekly_savoury_lifecycle import weekly_env


@pytest.fixture
def current(replacement_chain):
    clock, versions = replacement_chain
    clock.instant = datetime(2026, 10, 1, 18, 30, tzinfo=timezone.utc)
    return clock, versions


BROWSE = [
    "show me the weekly menu", "give me the weekly menu", "show weekly menu",
    "weekly menu please", "what is the weekly menu", "what's the weekly menu",
    "what's on this week's menu", "what is on this week's menu",
    "show me this week's menu", "give me this week's menu", "menu for this week",
    "show current weekly menu", "show me the savoury menu", "give me savoury menu",
    "savoury menu please", "what is the savoury menu", "what's on the savoury menu",
    "show me the menu", "give me the menu", "menu please", "SHOW  ME THE weekly MENU!!!",
]


@pytest.mark.parametrize("query", BROWSE)
def test_browse_one_intent_preserves_draft(current, query):
    uid = uuid.uuid4().hex
    facts = session_memory.get_facts(uid, "instagram")
    draft = order_intake.new_order_draft()
    order_intake.update_order_draft(draft, "2 boxes Sondesh Gurgaon Friday dinner")
    facts.update(order_intake_draft=draft, last_topic="bengali_home_table")
    saved = copy.deepcopy(draft)
    answer = respond(query, uid)
    assert answer == ("", True, "approved_menu", [menu_images.current_weekly_url()])
    assert facts["last_intents"] == [menu_review.WEEKLY_SAVOURY_BROWSE]
    assert draft == saved
    assert respond("show me my order", uid)[2] == "order_intake_draft"
    assert facts["order_intake_draft"] == saved


@pytest.mark.parametrize("query,qa_id", [
    ("tell me about bento", "bento_details_client"),
    ("tell me about Bengali Bento", "bento_details_client"),
    ("Bengali Bento details", "bento_details_client"),
    ("what are your bento options", "bento_details_client"),
    ("bento price", "bento_price_client"),
    ("Chicken Kosha price?", "chicken_kosha_price_client"),
])
@pytest.mark.parametrize("available", [True, False])
def test_approved_text_optional_weekly_asset(current, query, qa_id, available):
    clock, _ = current
    if not available:
        clock.instant = datetime(2026, 10, 8, 18, 30, tzinfo=timezone.utc)
    expected = client_qa.effective_entry(qa_id)
    uid = uuid.uuid4().hex
    answer = respond(query, uid)
    assert answer[0] == expected.answer
    assert session_memory.get_facts(uid, "instagram")["client_qa_id"] == qa_id
    assert answer[3] == ([menu_images.current_weekly_url()] if available else None)
    assert "finalising this week" not in answer[0]


@pytest.mark.parametrize("query,qa_id", [
    ("whats the BEST TIME TO BOOK", "best_time_to_book"),
    ("when should I book?", "best_time_to_book"),
    ("when should I reserve?", "best_time_to_book"),
    ("best time to pre-order?", "best_time_to_book"),
    ("when is the best time to book?", "best_time_to_book"),
    ("WHATS THE DELIVERY TIME", "savoury_delivery_time"),
    ("what's the delivery time?", "savoury_delivery_time"),
    ("how long does delivery take?", "savoury_delivery_time"),
    ("how long will savoury food delivery take?", "savoury_delivery_time"),
    ("how much time for food delivery", "savoury_delivery_time"),
    ("how long for sondesh delivery?", "sondesh_delivery_time"),
])
def test_supported_service_precedes_purchase_and_old_context(current, monkeypatch, query, qa_id):
    from camellia.rag_fallback import rag_fallback_engine
    from camellia.ai_responder import ai_responder
    def forbidden(*args, **kwargs):
        raise AssertionError("Supported canonical service must not reach RAG/Groq")
    monkeypatch.setattr(rag_fallback_engine, "retrieve_approved_menu", forbidden)
    monkeypatch.setattr(ai_responder, "_query_groq", forbidden)
    uid = uuid.uuid4().hex
    facts = session_memory.get_facts(uid, "instagram")
    facts.update(active_product_context="bento", last_topic="bento")
    answer = respond(query, uid)
    assert answer[0] == client_qa.effective_entry(qa_id).answer
    assert facts["client_qa_id"] == qa_id
    assert facts["last_route_source"] == "EXACT_APPROVED"
    assert facts["last_llm_called"] is False
    assert "wa.me" not in answer[0]


@pytest.mark.parametrize("query", [
    "how do i prebook", "how can i order", "what are the next steps to pre book",
    "steps to place an order", "what details do you need to place my order",
    "how can I place an order?", "how to order from a menu",
])
def test_prebook_exact_answer_one_existing_human_continuation(current, query):
    uid = uuid.uuid4().hex
    facts = session_memory.get_facts(uid, "instagram")
    draft = order_intake.new_order_draft()
    order_intake.update_order_draft(draft, "2 boxes Sondesh Gurgaon Friday dinner")
    facts["order_intake_draft"] = draft
    saved = copy.deepcopy(draft)
    answer = respond(query, uid)
    assert facts["client_qa_id"] in {"order_prebook", "alacarte_prebook"}
    expected = client_qa.effective_entry(facts["client_qa_id"]).answer
    assert answer[0].startswith(expected + "\n\nContinue on WhatsApp:\n")
    assert answer[0].count("https://wa.me/") == 1
    assert order_intake.whatsapp_draft_link(draft) in answer[0]
    assert "Please help me complete" in __import__("urllib.parse", fromlist=["unquote"]).unquote(answer[0])
    assert facts["order_intake_draft"] == saved
    assert sum(b.get("text", "").count("https://wa.me/") for b in facts["ordered_response_blocks"]) == 1


def projection():
    return next(row for row in menu_catalogue.workspace_summary(menu_catalogue.list_menus())["workspace_rows"] if row["menu_id"] == "bento")


def test_projection_tracks_weekly_without_catalogue_write(replacement_chain):
    clock, versions = replacement_chain
    before = db.get_menu_catalogue_entry("bento")
    upcoming = projection()
    assert upcoming["display_status"] == "UPCOMING"
    assert upcoming["image_url"] == json.loads(versions[-1]["approval_json"])["image_path"]
    assert menu_images.current_url("bento") is None
    clock.instant = datetime(2026, 10, 1, 18, 30, tzinfo=timezone.utc)
    row = projection()
    assert row["display_status"] == "CURRENT"
    assert row["read_only"] and not any(row["actions"].values())
    assert row["current_version"] == versions[-1]["id"]
    assert row["item_count"] == 1
    assert row["valid_from"] == "2026-10-02" and row["valid_until"] == "2026-10-08"
    assert row["image_url"] == menu_images.current_weekly_url() == menu_images.current_url("bento")
    assert db.get_menu_catalogue_entry("bento") == before
    clock.instant = datetime(2026, 10, 8, 18, 30, tzinfo=timezone.utc)
    assert projection()["display_status"] == "NO CURRENT MENU"
    assert projection()["image_url"] is None
    assert menu_images.current_url("bento") is None


def test_generic_optional_weekly_menu_does_not_suppress_supported_text(current, monkeypatch):
    from dataclasses import replace
    clock, _ = current
    clock.instant = datetime(2026, 10, 8, 18, 30, tzinfo=timezone.utc)
    original = client_qa.match("Can you tell me more about this", {})
    entry = replace(original.entry, answer="Approved general text.",
                    blocks=(client_qa.ResponseBlock("TEXT", "Approved general text."),
                            client_qa.ResponseBlock("MENU", "weekly_menu")),
                    asset_keys=("weekly_menu",))
    match = replace(original, entry=entry, asset_keys=("weekly_menu",))
    monkeypatch.setattr(client_qa, "match", lambda *_: match)
    answer = respond("Can you tell me more about this")
    assert answer[0] == "Approved general text."
    assert answer[1] is True and answer[3] is None


def test_pending_weekly_replacement_does_not_suppress_bento_canonical_text(current):
    from test_weekly_savoury_lifecycle import _draft
    _draft("2 Oct - 8 Oct 2026", 160, "Dhokar Dalna")
    answer = respond("tell me about bento")
    assert answer[0] == client_qa.effective_entry("bento_details_client").answer
    assert answer[3] is None


def test_live_projection_follows_new_approved_replacement(current):
    import io
    from PIL import Image
    from camellia import weekly_savoury
    from test_weekly_savoury_lifecycle import _publish
    before = projection()
    stored = db.get_menu_catalogue_entry("bento")
    buffer = io.BytesIO()
    Image.new("RGB", (120, 160), "purple").save(buffer, format="PNG")
    draft = menu_review.extract(weekly_savoury.WEEKLY_KEY, "admin", data=buffer.getvalue(), mime="image/png", weekly=True)
    published = _publish(draft, replace_version_id=before["current_version"])
    after = projection()
    assert after["current_version"] == published["version_id"] != before["current_version"]
    assert after["image_url"] != before["image_url"]
    assert after["image_url"] == menu_images.current_weekly_url()
    assert menu_images.card("bento")["image_url"] == after["image_url"]
    assert menu_images.cards(["bento"])["bento"]["image_url"] == after["image_url"]
    assert next(m for m in menu_images.list_menus() if m["menu_key"] == "bento")["image_url"] == after["image_url"]
    assert db.get_menu_catalogue_entry("bento") == stored
    assert db.get_menu_version(before["current_version"]) is not None


def test_bento_projection_has_no_independent_editor_or_publish_entry():
    from camellia.admin_menu_review import REVIEW_SCRIPT
    assert "menu.workspace!=='weekly'&&!menu.read_only" in REVIEW_SCRIPT
    assert "Open weekly workspace" in REVIEW_SCRIPT
    assert "?.read_only){mrShowCatalogue();await wsLoad();return;}" in REVIEW_SCRIPT


def test_standing_bento_cannot_become_second_publish_authority(current):
    before = db.get_menu_catalogue_entry("bento")
    with pytest.raises(menu_review.ReviewConflict, match="read-only"):
        menu_review.extract("bento", "admin", text="Bento 250g ₹100")
    with pytest.raises(menu_review.ReviewConflict, match="read-only"):
        menu_review.publish_approved_menu("bento", "old-draft", 1, "admin")
    with pytest.raises(ValueError, match="read-only"):
        menu_images.publish("weekly.bengali_bento")
    with pytest.raises(ValueError, match="read-only"):
        menu_images.save_draft("bento", b"not-an-image", "image/png")
    assert db.get_menu_catalogue_entry("bento") == before


def test_current_weekly_bento_rows_survive_retirement_of_standing_authority(current):
    from camellia import weekly_savoury
    from test_weekly_savoury_lifecycle import _draft, _publish
    current_id = projection()["current_version"]
    draft = _draft("2 Oct - 8 Oct 2026", 160, "Dhokar Dalna")
    rows = copy.deepcopy(draft["rows"])
    rows.append({**rows[0], "row_id": "weekly-bento", "category": "bengali_bento", "item": "Veg Bento", "price_inr": 170})
    edited = menu_review.edit(weekly_savoury.WEEKLY_KEY, draft["draft_id"], draft["revision"], rows, "admin")
    _publish(edited, replace_version_id=current_id)
    canonical = menu_review.canonical_menu()
    assert canonical["weekly_menus"][-1]["bengali_bento"] == [{"item": "Veg Bento", "quantity": "500g", "price_inr": 170, "description": ""}]
    assert "bengali_bento" not in canonical.get("order_types", {})
    from camellia.rag_fallback import rag_fallback_engine
    assert rag_fallback_engine.retrieve_approved_menu("What is the price of Veg Bento?").auto_response == "Veg Bento 500g is ₹170."
