"""Manual WhatsApp continuation is gated by explicit booked-order state."""

import re
import uuid
from unittest.mock import patch

from camellia import app as app_module
from camellia.config import config
from camellia.database import db
from camellia.handoff import handoff_contact_mode, whatsapp_link_allowed
from camellia.session_memory import session_memory


def _run(message, *, order_state=None):
    user_id = f"order-state-policy-{uuid.uuid4().hex}"
    session_memory.clear_session(user_id, "instagram")
    if order_state:
        session_memory.get_facts(user_id, "instagram")["order_state"] = order_state
    sent = []
    with patch.object(
        app_module, "send_instagram_reply",
        side_effect=lambda recipient, text, **kwargs: sent.append(text),
    ), patch.object(
        app_module, "_lookup_instagram_username", return_value=None,
    ), patch.object(
        app_module.escalator, "handle_escalation", return_value={"escalation_id": 1}
    ) as escalated, patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app_module._process_instagram_event(user_id, message, "order-state-policy-ad")
    return user_id, sent, escalated


def test_wedding_catering_inquiry_is_cam_only_and_waits_for_human():
    user_id, sent, escalated = _run("Can you cater my wedding for 100 people?")

    assert len(sent) == 1
    assert "someone will get back to you shortly" in sent[0]
    assert "wa.me" not in sent[0]
    assert "WhatsApp" not in sent[0]
    assert escalated.called
    handoff = db.get_active_handoff("instagram", user_id)
    assert handoff["status"] == "WAITING_FOR_HUMAN"
    assert handoff["contact_mode"] == "support_callback"


def test_event_and_prebook_inquiries_have_no_manual_whatsapp_link():
    for message in (
        "Can you cater an event for 80 people?",
        "What are the next steps to pre book?",
    ):
        _user_id, sent, _escalated = _run(message)
        assert all("wa.me" not in text for text in sent), message


def test_order_inquiry_is_not_whatsapp_eligible_without_booked_state():
    _user_id, sent, escalated = _run("I want to book this, can I talk to someone?")

    assert escalated.called
    assert all("wa.me" not in text for text in sent)


def test_confirmed_order_allows_approved_manual_whatsapp_continuation():
    _user_id, sent, escalated = _run(
        "I want to book this, can I talk to someone?", order_state="confirmed"
    )

    assert escalated.called
    assert any(re.search(r"https://wa\.me/", text) for text in sent)


def test_finalized_order_allows_manual_whatsapp_continuation():
    _user_id, sent, escalated = _run(
        "I have placed the order", order_state="FINALIZED"
    )

    assert escalated.called
    assert any("wa.me" in text for text in sent)


def test_eligibility_is_deny_by_default_and_ignores_legacy_flags():
    assert not whatsapp_link_allowed()
    assert not whatsapp_link_allowed(facts={"order_status": "finalized"})
    assert not whatsapp_link_allowed(facts={"order_finalized": True})
    assert not whatsapp_link_allowed(facts={"order_state": "PREBOOK"})
    assert whatsapp_link_allowed(facts={"order_state": "BOOKED"})
    assert whatsapp_link_allowed(facts={"order_state": "CONFIRMED"})
    assert whatsapp_link_allowed(facts={"order_state": "FINALIZED"})


def test_confirmed_state_does_not_turn_unrelated_event_handoff_into_a_link():
    assert handoff_contact_mode(
        "Can you cater my wedding for 100 people?",
        template_id="groq_structured_event_escalation",
        facts={"order_state": "CONFIRMED"},
    ) == "support_callback"


def test_waiting_cam_reuse_keeps_the_state_gate_central():
    assert handoff_contact_mode(
        "I need a person about my booked order",
        facts={"order_state": "FINALIZED"},
    ) == "whatsapp_order"
    assert handoff_contact_mode(
        "I need a person about my booked order",
        facts={"order_state": "PENDING"},
    ) == "support_callback"
