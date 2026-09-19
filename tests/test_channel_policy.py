"""Instagram customer AI plus durable manual human handoff policy."""

import json
import re
import urllib.parse
import uuid
from unittest.mock import patch

import pytest

from camellia import app, handoff as handoff_module
from camellia.config import config
from camellia.database import db
from camellia.responder import responder
from camellia.session_memory import session_memory


INSTAGRAM_HUMAN_INTENT_PROBE_MESSAGES = [
    "human", "person", "agent", "I need to speak to someone",
]


@pytest.mark.parametrize("message", INSTAGRAM_HUMAN_INTENT_PROBE_MESSAGES)
def test_instagram_direct_human_request_triggers_whatsapp_handoff(message):
    """These exact bare/short human-intent phrases must all reach the
    Instagram -> WhatsApp handoff, not an unrelated FAQ template — the direct
    human-intent recognition fix (camellia/semantic_fallback.py's
    _ESCALATION_INTENT_RE), verified end-to-end through the real Instagram
    message handler."""
    send_reply, escalate = _ask_instagram(
        "policy_ig_direct_human_" + re.sub(r"\W+", "_", message), message,
    )
    reply_text = send_reply.call_args.args[1]
    assert escalate.called
    assert "wa.me" in reply_text
# ---------------------------------------------------------------------------
# Instagram: the full architecture is unchanged
# ---------------------------------------------------------------------------

def _ask_instagram(user_id, message, ad_id="ad-1"):
    # Suffixed so a fresh handoff is created every run regardless of what an
    # earlier test run already wrote to the persistent handoffs table for
    # this literal id — otherwise a leftover active handoff from a prior run
    # would be silently reused, and app.py now (correctly) skips re-alerting
    # staff for a reused handoff, making these single-call tests order- and
    # run-dependent instead of a fresh escalation each time.
    user_id = f"{user_id}_{uuid.uuid4().hex[:8]}"
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ) as escalate, patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app._process_instagram_event(user_id, message, ad_id)
    return send_reply, escalate


def test_instagram_faq_still_works_no_handoff_offered():
    """"Hi can I get the menu?" is answerable, so no WhatsApp handoff."""
    send_reply, escalate = _ask_instagram("policy_ig_faq", "Hi can I get the menu?")
    reply_text = send_reply.call_args.args[1]
    assert not escalate.called
    assert "wa.me" not in reply_text
    assert "CAM-" not in reply_text


def test_instagram_menu_request_still_works():
    send_reply, escalate = _ask_instagram("policy_ig_menu", "show me the veg menu")
    assert not escalate.called
    assert "wa.me" not in send_reply.call_args.args[1]


def test_instagram_product_price_request_still_works():
    send_reply, escalate = _ask_instagram("policy_ig_price", "what is the price of Jolbhora?")
    assert not escalate.called
    assert "wa.me" not in send_reply.call_args.args[1]


def test_instagram_delivery_request_still_works():
    send_reply, escalate = _ask_instagram("policy_ig_delivery", "do you deliver to Delhi?")
    assert not escalate.called
    assert "wa.me" not in send_reply.call_args.args[1]


def test_instagram_unsupported_topic_escalates_to_whatsapp_with_no_invented_fact():
    """"Do you offer wedding gift wrapping?" — no approved fact exists, so
    knowledge is insufficient and the customer is offered a WhatsApp
    continuation instead of an invented answer."""
    send_reply, escalate = _ask_instagram(
        "policy_ig_giftwrap", "Do you offer wedding gift wrapping?",
    )
    reply_text = send_reply.call_args.args[1]
    assert escalate.called
    assert "wa.me" in reply_text
    assert "gift wrap" not in reply_text.lower()
    assert "cornstarch" not in reply_text.lower()


def test_instagram_human_request_escalates_directly_without_an_unnecessary_faq():
    send_reply, escalate = _ask_instagram("policy_ig_human", "I want to speak to someone")
    reply_text = send_reply.call_args.args[1]
    assert escalate.called
    assert "wa.me" in reply_text


def test_exact_production_human_request_uses_cam_whatsapp_continuation():
    """The production wording must never leak the legacy callback promise."""
    user_id = "policy_ig_exact_agent_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ) as escalate, patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app._process_instagram_event(user_id, "I need an agent", "ad-1")

    reply_text = send_reply.call_args.args[1]
    assert re.search(r"https://wa\.me/919800000000\?text=", reply_text)
    assert re.search(r"CAM-\d{5,6}", urllib.parse.unquote(reply_text))
    assert escalate.call_count == 1
    assert "continue" in reply_text.lower()
    assert "we'll contact you" not in reply_text.lower()
    assert "they'll connect with you" not in reply_text.lower()
    assert "whatsapp/call shortly" not in reply_text.lower()


def test_repeated_explicit_human_requests_reuse_cam_and_alert_once():
    user_id = "policy_ig_exact_agent_repeat_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ) as escalate, patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app._process_instagram_event(user_id, "I need an agent", "ad-1")
        app._process_instagram_event(user_id, "I want to speak to someone", "ad-1")

    replies = [urllib.parse.unquote(call.args[1]) for call in send_reply.call_args_list]
    refs = {match.group(0) for reply in replies if (match := re.search(r"CAM-\d{5,6}", reply))}
    assert len(refs) == 1
    assert escalate.call_count == 1
    assert all("whatsapp/call shortly" not in reply.lower() for reply in replies)


def test_verified_order_receipt_adds_concise_order_handoff_and_reuses_cam():
    user_id = "policy_ig_order_handoff_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ) as escalate, patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app._process_instagram_event(user_id, "I want Jolbhora 250g", "ad-1")
        receipt, order_link = [call.args[1] for call in send_reply.call_args_list[-2:]]
        sent_before_waiting = send_reply.call_count
        app._process_instagram_event(user_id, "I need an agent", "ad-1")

    assert "ORDER DRAFT RECEIPT" in receipt
    assert "Continue on WhatsApp" in receipt
    assert "https://wa.me/" not in receipt
    assert order_link.startswith("Continue on WhatsApp:\nhttps://wa.me/")
    assert "we'll contact you" not in receipt.lower()
    assert "they'll connect with you" not in receipt.lower()
    assert "whatsapp/call shortly" not in receipt.lower()

    link = re.search(r"https://wa\.me/[^\s]+", order_link).group(0)
    decoded = urllib.parse.unquote(link)
    assert "Ref: CAM-" in decoded
    assert "Collection: Aamar Mishti Collection" in decoded
    assert "Item: Jolbhora Sondesh" in decoded
    assert "Pack: 250g" in decoded
    assert "Quantity: 1 box" in decoded
    assert "Estimated Item Total: ₹750" in decoded
    assert "Courier" not in decoded
    assert "Final Total" not in decoded
    assert user_id not in decoded
    assert "address" not in decoded.lower()
    assert escalate.call_count == 1
    assert send_reply.call_count == sent_before_waiting
    waiting_rows = db.get_conversations(user_id=user_id, channel="instagram")
    assert waiting_rows[0]["template_id"] == "waiting_for_human"
    assert waiting_rows[0]["bot_response"] == ""


def test_instagram_bulk_event_request_escalates():
    send_reply, escalate = _ask_instagram(
        "policy_ig_bulk", "I need 50 boxes of Jolbhora for my wedding in Delhi",
    )
    reply_text = send_reply.call_args.args[1]
    assert escalate.called
    assert "wa.me" in reply_text


def test_instagram_order_example_offers_handoff_not_a_whatsapp_order_bot():
    """Detected event/order information is stored on the handoff; WhatsApp
    itself never becomes an order chatbot — verified by the WhatsApp-side
    tests above (zero responder calls) combined with this handoff record."""
    send_reply, _escalate = _ask_instagram(
        "policy_ig_order_example", "I want 50 boxes of Jolbhora for a wedding in Delhi",
    )
    reference = re.search(r"CAM-\d{5,6}", send_reply.call_args.args[1]).group(0)
    known = json.loads(db.get_handoff_by_ref(reference)["known_entities"])
    assert known.get("product") == "Jolbhora"
    assert known.get("event") == "wedding"
    assert known.get("location") == "Delhi"


# ---------------------------------------------------------------------------
# Handoff link shape
# ---------------------------------------------------------------------------

def test_handoff_link_contains_an_opaque_reference():
    """The reference lives inside the wa.me link's URL-encoded prefill text
    (visible in WhatsApp once the customer taps through), not as separate
    plain text in the Instagram message — matching the approved copy, which
    shows only the CTA link, not a bare reference. Decode before matching."""
    send_reply, _escalate = _ask_instagram("policy_ig_opaque", "Do you have valet parking?")
    reply_text = urllib.parse.unquote(send_reply.call_args.args[1])
    assert re.search(r"\bCAM-\d{5,6}\b", reply_text)
    # Opaque means no raw database id leaks either.
    assert not re.search(r"\bid=\d+\b", reply_text)


def test_handoff_link_does_not_expose_the_instagram_transcript():
    send_reply, _escalate = _ask_instagram(
        "policy_ig_no_transcript", "Do you offer wedding gift wrapping for a big event?",
    )
    reply_text = send_reply.call_args.args[1]
    link = re.search(r"https://wa\.me/\S+", reply_text)
    if link:
        decoded = urllib.parse.unquote(link.group(0))
        assert "gift wrapping" not in decoded.lower()
        assert "policy_ig_no_transcript" not in decoded


def test_duplicate_instagram_escalation_does_not_create_duplicate_handoffs():
    user_id = "policy_ig_dedup"
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ):
        app._process_instagram_event(user_id, "I want to talk to a human agent", "ad-1")
        app._process_instagram_event(user_id, "can I please speak to a human", "ad-1")

    refs = {
        match.group(0)
        for call in send_reply.call_args_list
        if (match := re.search(r"CAM-\d{5,6}", urllib.parse.unquote(call.args[1])))
    }
    assert len(refs) == 1
    rows = [h for h in db.get_handoffs(limit=200) if h["source_user_id"] == user_id]
    assert len(rows) == 1


def test_instagram_chatbot_enabled_default_is_true():
    assert config.INSTAGRAM_CHATBOT_ENABLED is True
