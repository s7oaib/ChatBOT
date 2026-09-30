"""QA-O3 continuity without a second order system or WhatsApp automation."""
import uuid
from unittest.mock import patch
from urllib.parse import unquote

import pytest

from camellia import app as app_module, handoff
from camellia.config import config
from camellia.database import Database
from camellia.order_intake import draft_fingerprint, new_order_draft, update_order_draft
from camellia.responder import responder
from camellia.session_memory import SessionMemoryManager, session_memory


@pytest.fixture
def customer(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "WHATSAPP_BUSINESS_NUMBER", "918796551990")
    database = Database(str(tmp_path / "continuity.db"))
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(handoff, "db", database)
    uid = "o3-" + uuid.uuid4().hex
    session_memory.clear_session(uid, "instagram")
    return uid, session_memory.get_facts(uid, "instagram"), database


def reply(uid, message):
    with patch("camellia.ai_responder.ai_responder._query_groq", side_effect=AssertionError("LLM called")):
        return responder.match_and_respond(message, user_id=uid, channel="instagram")


def start(uid):
    return reply(uid, "2 boxes Sondesh Gurgaon Friday dinner 9876543210")[0]


@pytest.mark.parametrize("message", ["Gurgaon", "Friday dinner", "9876543210", "ok", "thanks", "done"])
def test_duplicates_keep_editable_draft_without_link_spam(customer, message):
    uid, facts, database = customer
    assert "https://wa.me/" in start(uid)
    before = facts["order_intake_draft"].copy()
    text = reply(uid, message)[0]
    assert "ORDER DRAFT" not in text and "https://wa.me/" not in text
    assert facts["order_intake_draft"] == before
    assert not facts["order_intake_referred"]
    assert database.get_active_handoff("instagram", uid) is None
    assert "https://wa.me/" in reply(uid, "actually 3 boxes")[0]


@pytest.mark.parametrize("message", ["send the WhatsApp link again", "where is the WhatsApp link?", "WhatsApp link please", "send link", "how do I continue on WhatsApp?"])
def test_explicit_resend_uses_current_draft(customer, message):
    uid, facts, _database = customer
    start(uid)
    updated = reply(uid, "actually 3 boxes")[0]
    before = facts["order_intake_draft"].copy()
    text = reply(uid, message)[0]
    assert text.split("?text=", 1)[1] == updated.split("?text=", 1)[1]
    assert facts["order_intake_draft"] == before


@pytest.mark.parametrize("message", ["actually 3 boxes", "Saturday dinner instead", "phone is 9811111111", "actually Noida", "change address to Sector 56 Gurgaon"])
def test_meaningful_corrections_update_fingerprint_and_link(customer, message):
    uid, facts, _database = customer
    original = start(uid)
    fingerprint = facts["order_intake_last_presented_fingerprint"]
    updated = reply(uid, message)[0]
    assert "ORDER DRAFT" in updated and "https://wa.me/" in updated
    assert updated != original
    assert facts["order_intake_last_presented_fingerprint"] != fingerprint
    assert facts["order_intake_whatsapp_link_sent_for_fingerprint"] == draft_fingerprint(facts["order_intake_draft"])


@pytest.mark.parametrize("message", ["I sent it on WhatsApp", "sent on whatsapp", "I've messaged you on WhatsApp", "messaged the manager on WhatsApp", "I sent the order to the manager", "done, sent on WhatsApp", "I have sent the order on WhatsApp"])
def test_explicit_referral_retires_snapshot_and_locks_only_order(customer, message):
    uid, facts, database = customer
    start(uid)
    before = facts["order_intake_draft"].copy()
    text = reply(uid, message)[0]
    assert "WhatsApp" in text and "order confirmed" not in text.lower()
    assert facts["order_intake_draft"] is None and facts["order_intake_referred"]
    snapshot = facts["last_referred_order_draft"].copy()
    assert snapshot["item"] == before["item"]
    before["item"] = "changed outside snapshot"
    assert facts["last_referred_order_draft"] == snapshot
    for correction in ["actually make it 3 boxes", "make it Saturday dinner", "Gurgaon", "phone is 9811111111"]:
        response = reply(uid, correction)[0]
        assert "Please send this change there" in response
        assert "https://wa.me/" not in response
        assert facts["order_intake_draft"] is None
        assert facts["last_referred_order_draft"] == snapshot
    assert database.get_active_handoff("instagram", uid) is None
    text, matched, template, _images = reply(uid, "what is Bengali Bento?")
    assert matched and template != "order_intake_draft"
    assert "9876543210" not in text and "ORDER DRAFT" not in text


@pytest.mark.parametrize("message", ["start a new order", "I want to place another order", "new order please", "I need a separate order"])
def test_explicit_new_order_never_preloads_snapshot(customer, message):
    uid, facts, _database = customer
    start(uid)
    reply(uid, "I sent it on WhatsApp")
    text = reply(uid, message)[0]
    assert "https://wa.me/" in text and not facts["order_intake_referred"]
    assert facts["order_intake_draft"] == new_order_draft()
    assert "Items & quantity: Not provided" in text
    reply(uid, "3 boxes Chicken Bento Noida Saturday lunch")
    assert facts["order_intake_draft"]["receiver_phone"] is None


@pytest.mark.parametrize("message", ["human please", "manager please", "can I speak to someone about this order?", "connect me to your manager"])
def test_explicit_human_handoff_carries_unverified_draft_and_deduplicates(customer, message):
    uid, facts, database = customer
    with patch.object(app_module, "send_instagram_reply") as send, patch.object(app_module.escalator, "handle_escalation") as alert:
        app_module._process_instagram_event(uid, "2 boxes Sondesh Gurgaon Friday dinner", "ad-o3")
        assert database.get_active_handoff("instagram", uid) is None
        app_module._process_instagram_event(uid, message, "ad-o3")
        row = database.get_active_handoff("instagram", uid)
        assert row["contact_mode"] == "whatsapp_order"
        assert "CUSTOMER-PROVIDED ORDER DRAFT — UNVERIFIED" in row["summary"]
        assert "2 boxes Sondesh" in row["summary"] and "Receiver phone: Not provided" in row["summary"]
        assert "Estimated Item Total" not in row["summary"]
        link = unquote(send.call_args.args[1])
        assert "2 boxes Sondesh" in link and "Camellia reference: " + row["handoff_ref"] in link
        assert facts["order_intake_draft"] is None and facts["order_intake_referred"]
        for repeat in ["human please", "manager please", "can someone help?", "start a new order"]:
            app_module._process_instagram_event(uid, repeat, "ad-o3")
        assert len(database.get_handoffs(limit=20)) == 1
        assert alert.call_count == 1
        assert facts["order_intake_draft"] is None


def test_generic_human_request_without_intake_keeps_support_callback(customer):
    uid, facts, database = customer
    with patch.object(app_module, "send_instagram_reply") as send, patch.object(app_module.escalator, "handle_escalation"):
        app_module._process_instagram_event(uid, "human please", "ad-o3")
    row = database.get_active_handoff("instagram", uid)
    assert row["contact_mode"] == "support_callback"
    assert "CUSTOMER-PROVIDED ORDER DRAFT" not in row["summary"]
    assert all("wa.me" not in call.args[1] for call in send.call_args_list)
    assert facts["last_referred_order_draft"] is None


def test_retirement_and_verified_authority_remain_separate(customer):
    uid, facts, _database = customer
    start(uid)
    facts["cart"] = [{"collection": "signature", "flavour": "jolbhora", "size": "250g", "quantity": 1, "total_price": 750}]
    facts["source_route"] = {"routing_id": 17}
    facts["home_table_videos_sent"] = True
    result = handoff.get_or_create_handoff(user_id=uid, channel="instagram", reason="Order", user_message="manager please", facts=facts, include_order=True)
    assert result["verified_order"]["estimated_item_total"] == 750
    assert result["customer_order_intake_draft"]["quantity"] == "2 boxes"
    stored = handoff.db.get_handoff_by_ref(result["handoff_ref"])
    assert "UNVERIFIED" in handoff.staff_summary(stored)
    handoff.retire_order_state(uid, "instagram")
    assert facts["cart"] == [] and facts["order_draft"]["flavour"] is None
    assert facts["order_intake_draft"] is None
    assert facts["source_route"] == {"routing_id": 17} and facts["home_table_videos_sent"] is True


def test_fingerprints_and_referral_lock_survive_session_reload(tmp_path):
    database = Database(str(tmp_path / "session.db"))
    first = SessionMemoryManager(persistence_enabled=True, database=database)
    facts = first.get_facts("reload", "instagram")
    facts["order_intake_draft"] = new_order_draft()
    update_order_draft(facts["order_intake_draft"], "2 boxes Sondesh Gurgaon")
    facts["order_intake_last_presented_fingerprint"] = draft_fingerprint(facts["order_intake_draft"])
    facts["order_intake_referred"] = True
    first.add_turn("reload", "instagram", "sent", "acknowledged")
    second = SessionMemoryManager(persistence_enabled=True, database=database)
    restored = second.get_facts("reload", "instagram")
    assert restored["order_intake_referred"]
    assert restored["order_intake_last_presented_fingerprint"] == facts["order_intake_last_presented_fingerprint"]
