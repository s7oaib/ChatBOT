"""Channel policy: Instagram is the only AI chatbot channel; WhatsApp is
human-only.

The client explicitly decided customers must never talk to an AI bot on
WhatsApp. This is enforced in application code, not by instructing an LLM
not to answer — `whatsapp_adapter.process_incoming_message` (the one function
an inbound WhatsApp message reaches) does not call understanding, Groq, RAG,
semantic fallback, or template rendering by default. These tests spy on every
one of those call sites directly, for a representative set of WhatsApp
messages, and assert zero calls — not merely "no reply", but "the machinery
never ran".

Instagram keeps the full architecture from prior phases unchanged; that is
verified here too, briefly, as a regression guard.
"""

import json
import re
import urllib.parse
import uuid
from unittest.mock import patch

import pytest

from camellia import app, handoff as handoff_module
from camellia.ai_responder import ai_responder
from camellia.config import config
from camellia.database import db
from camellia.rag_fallback import rag_fallback_engine
from camellia.responder import responder
from camellia.semantic_fallback import read as semantic_fallback_read
from camellia.session_memory import session_memory
from camellia.understanding import understanding_engine
from camellia.whatsapp_adapter import whatsapp_adapter


# ---------------------------------------------------------------------------
# WhatsApp: no AI machinery ever runs
# ---------------------------------------------------------------------------

WHATSAPP_PROBE_MESSAGES = [
    "Hi",
    "send menu",
    "what's the price",
    "do you deliver?",
    "Jolbhora price",
    "can I get discount?",
    "Ref: CAM-48371",
]


@pytest.mark.parametrize("message", WHATSAPP_PROBE_MESSAGES)
def test_whatsapp_message_calls_no_understanding_no_groq_no_rag_no_templates(message):
    """The exact assertion style requested: every relevant call_count is 0."""
    with patch.object(understanding_engine, "analyze") as analyze, patch.object(
        ai_responder, "_query_groq"
    ) as query_groq, patch.object(
        rag_fallback_engine, "retrieve"
    ) as rag_retrieve, patch.object(
        responder, "match_and_respond"
    ) as match_and_respond, patch.object(
        whatsapp_adapter, "send_message"
    ) as send_message:
        result = whatsapp_adapter.process_incoming_message("919876543210", message)

    assert analyze.call_count == 0
    assert query_groq.call_count == 0
    assert rag_retrieve.call_count == 0
    assert match_and_respond.call_count == 0
    assert send_message.call_count == 0
    assert result["bot_replied"] is False
    assert result["response_text"] is None


WHATSAPP_HUMAN_INTENT_PROBE_MESSAGES = ["human", "menu", "price", "hello", "Ref: CAM-12345"]


@pytest.mark.parametrize("message", WHATSAPP_HUMAN_INTENT_PROBE_MESSAGES)
def test_whatsapp_never_answers_even_a_direct_human_request(message):
    """A bare human-intent word on WhatsApp ("human") must not trigger any
    chatbot machinery either — WhatsApp is already the human destination, so
    there is nothing for the bot to escalate. Same zero-call-count assertion
    style as the general WhatsApp probes above, covering the exact messages
    named in the production-readiness checklist."""
    with patch.object(understanding_engine, "analyze") as analyze, patch.object(
        ai_responder, "_query_groq"
    ) as query_groq, patch.object(
        rag_fallback_engine, "retrieve"
    ) as rag_retrieve, patch.object(
        responder, "match_and_respond"
    ) as match_and_respond, patch.object(
        whatsapp_adapter, "send_message"
    ) as send_message:
        result = whatsapp_adapter.process_incoming_message("919876543210", message)

    assert analyze.call_count == 0
    assert query_groq.call_count == 0
    assert rag_retrieve.call_count == 0
    assert match_and_respond.call_count == 0
    assert send_message.call_count == 0
    assert result["bot_replied"] is False
    assert result["response_text"] is None


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
    assert re.search(r"CAM-\d{5,6}", reply_text)


def test_whatsapp_semantic_fallback_reader_is_never_invoked_either():
    """The offline concept reader is understanding's own fallback, reachable
    only through analyze/deterministic_fallback — confirm the WhatsApp path
    doesn't call it by some other route."""
    with patch("camellia.semantic_fallback.read", wraps=semantic_fallback_read) as reader:
        whatsapp_adapter.process_incoming_message("919876543210", "do you deliver to Delhi?")
    assert reader.call_count == 0


def test_no_qa_template_response_is_composed_for_whatsapp():
    """response_text must be None/empty, not an approved-template answer
    quietly generated some other way."""
    result = whatsapp_adapter.process_incoming_message("919876543210", "what is the price of Jolbhora?")
    assert not result.get("response_text")
    assert result["was_matched"] is False


def test_a_handoff_reference_on_whatsapp_still_produces_no_bot_response():
    result = whatsapp_adapter.process_incoming_message(
        "919876543210", "Hi, I'm continuing my Camellia Instagram enquiry. Ref: CAM-48371",
    )
    assert result["bot_replied"] is False
    assert result["response_text"] is None


def test_whatsapp_webhook_route_never_reaches_the_chatbot_stack(monkeypatch):
    """End-to-end through the actual webhook dispatch function, not just the
    adapter method — the request path a real Meta webhook would take."""
    with patch.object(responder, "match_and_respond") as match_and_respond, patch.object(
        whatsapp_adapter, "send_message"
    ) as send_message:
        app._process_whatsapp_event("919876543210", "Hi, do you deliver to Delhi?")
    assert match_and_respond.call_count == 0
    assert send_message.call_count == 0


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
    refs = {re.search(r"CAM-\d{5,6}", reply).group(0) for reply in replies}
    assert len(refs) == 1
    assert escalate.call_count == 1
    assert all("whatsapp/call shortly" not in reply.lower() for reply in replies)


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
    # Suffixed for the same reason as _ask_instagram above: handoffs persist
    # across suite runs in the shared sqlite file, and a literal fixed id
    # would eventually collide with an already-active handoff from an
    # earlier run.
    user_id = "policy_ig_dedup_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ):
        app._process_instagram_event(user_id, "I want to talk to a human agent", "ad-1")
        app._process_instagram_event(user_id, "can I please speak to a human", "ad-1")

    refs = {
        re.search(r"CAM-\d{5,6}", urllib.parse.unquote(call.args[1])).group(0)
        for call in send_reply.call_args_list
    }
    assert len(refs) == 1
    rows = [h for h in db.get_handoffs(limit=200) if h["source_user_id"] == user_id]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Configuration is protected in code, not just by default value
# ---------------------------------------------------------------------------

def test_whatsapp_chatbot_enabled_default_is_false():
    assert config.WHATSAPP_CHATBOT_ENABLED is False


def test_instagram_chatbot_enabled_default_is_true():
    assert config.INSTAGRAM_CHATBOT_ENABLED is True


def test_disabling_whatsapp_chatbot_is_the_only_way_to_change_the_behavior():
    """Flipping the flag on is an explicit, logged escape hatch — proving the
    code path exists behind it without it being reachable by any other means.
    Both flags must be set: a single flipped boolean is not a decision."""
    with patch.object(config, "WHATSAPP_CHATBOT_ENABLED", True), patch.object(
        config, "WHATSAPP_CHATBOT_ENABLE_CONFIRM", True,
    ), patch.object(
        responder, "match_and_respond", return_value=("x", True, "greeting", None),
    ) as match_and_respond, patch.object(whatsapp_adapter, "send_message"):
        whatsapp_adapter.process_incoming_message("919876543210", "Hi")
    assert match_and_respond.called


def test_whatsapp_chatbot_enabled_alone_fails_safe_to_human_only():
    """A single accidentally-flipped WHATSAPP_CHATBOT_ENABLED=true (a stray
    .env value, a copy-paste mistake) must not be enough to put the AI back
    on WhatsApp — the client's channel policy is enforced in code, not by one
    default. Without the separate WHATSAPP_CHATBOT_ENABLE_CONFIRM flag, the
    message is still handled human-only and no chatbot machinery runs."""
    with patch.object(config, "WHATSAPP_CHATBOT_ENABLED", True), patch.object(
        responder, "match_and_respond",
    ) as match_and_respond, patch.object(whatsapp_adapter, "send_message") as send_message:
        result = whatsapp_adapter.process_incoming_message("919876543210", "Hi")
    assert not match_and_respond.called
    assert not send_message.called
    assert result["bot_replied"] is False
    assert result["response_text"] is None


def test_whatsapp_chatbot_enable_confirm_defaults_false():
    assert config.WHATSAPP_CHATBOT_ENABLE_CONFIRM is False
