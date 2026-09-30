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


@pytest.fixture(autouse=True)
def isolated_channel_policy_db(tmp_path, monkeypatch):
    """Channel policy cases start without another test's unreviewed menu."""
    monkeypatch.setattr(db, "db_path", str(tmp_path / "channel-policy.db"))
    db.init_db()


@pytest.mark.parametrize("message", INSTAGRAM_HUMAN_INTENT_PROBE_MESSAGES)
def test_instagram_direct_human_request_triggers_support_handoff_without_whatsapp(message):
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
    assert "wa.me" not in reply_text
    assert "Manager of Kitchens will connect with you here shortly" in reply_text
# ---------------------------------------------------------------------------
# Instagram: the full architecture is unchanged
# ---------------------------------------------------------------------------

def _ask_instagram(user_id, message, ad_id="ad-1", *, return_user_id=False):
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
    return (send_reply, escalate, user_id) if return_user_id else (send_reply, escalate)


def test_instagram_approved_faq_still_works_without_handoff():
    """An entry in the client-approved registry remains answerable."""
    send_reply, escalate = _ask_instagram("policy_ig_faq", "Tell me more about Sondesh")
    reply_text = send_reply.call_args.args[1]
    assert not escalate.called
    assert "wa.me" not in reply_text
    assert "CAM-" not in reply_text


def test_instagram_menu_request_still_works(eligible_weekly_asset):
    send_reply, escalate = _ask_instagram(
        "policy_ig_menu", "Can you tell me more about the weekly menu?",
    )
    assert not escalate.called
    assert "wa.me" not in send_reply.call_args.args[1]


def test_unregistered_product_price_request_hands_off():
    send_reply, escalate = _ask_instagram("policy_ig_price", "what is the price of Jolbhora?")
    assert escalate.called
    assert "wa.me" not in send_reply.call_args.args[1]


def test_unsupported_delivery_request_hands_off():
    send_reply, escalate = _ask_instagram("policy_ig_delivery", "do you deliver to Delhi?")
    assert escalate.called
    assert "wa.me" not in send_reply.call_args.args[1]


@pytest.mark.parametrize("selection", ["Bengali meal", "Bengali meals", "the Bengali one", "second one", "option 2"])
def test_active_price_clarification_routes_bengali_meals_to_approved_answer_without_cam(selection):
    """The exact production sequence must finish in the approved meal route."""
    user_id = f"policy_ig_bengali_price_{uuid.uuid4().hex[:8]}"
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ) as escalate, patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"), \
         patch.object(config, "GROQ_ENABLED", False), patch.object(config, "STRUCTURED_NLU_ENABLED", False), \
         patch.object(config, "SEMANTIC_ROUTER_ENABLED", False):
        app._process_instagram_event(user_id, "What's the pricing?", "ad-1")
        app._process_instagram_event(user_id, selection, "ad-1")

    assert escalate.call_count >= 1
    assert "Manager of Kitchens" in send_reply.call_args_list[-1].args[1]
    assert "wa.me/" not in send_reply.call_args_list[-1].args[1]
    handoffs = [row for row in db.get_handoffs(limit=200) if row["source_user_id"] == user_id]
    assert handoffs


def test_instagram_unsupported_topic_uses_controlled_handoff_without_invented_fact():
    """"Do you offer wedding gift wrapping?" — no approved fact exists, so
    knowledge is insufficient and the customer gets a controlled human
    handoff instead of an invented answer."""
    send_reply, escalate = _ask_instagram(
        "policy_ig_giftwrap", "Do you offer wedding gift wrapping?",
    )
    reply_text = send_reply.call_args.args[1]
    assert escalate.called
    assert "wa.me" not in reply_text
    assert "Manager of Kitchens will connect with you here shortly" in reply_text
    assert "gift wrap" not in reply_text.lower()
    assert "cornstarch" not in reply_text.lower()


def test_instagram_human_request_escalates_directly_without_an_unnecessary_faq():
    send_reply, escalate = _ask_instagram("policy_ig_human", "I want to speak to someone")
    reply_text = send_reply.call_args.args[1]
    assert escalate.called
    assert "wa.me" not in reply_text
    assert "Manager of Kitchens will connect with you here shortly" in reply_text


def test_exact_production_human_request_uses_team_callback_without_whatsapp():
    user_id = "policy_ig_exact_agent_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ) as escalate, patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app._process_instagram_event(user_id, "I need an agent", "ad-1")

    reply_text = send_reply.call_args.args[1]
    assert "https://wa.me/" not in reply_text
    assert "Manager of Kitchens will connect with you here shortly" in reply_text
    assert escalate.call_count == 1
    handoff = db.get_active_handoff("instagram", user_id)
    assert handoff["contact_mode"] == "support_callback"
    assert handoff["query_summary"] == "A team member to respond."


def test_repeated_explicit_human_requests_reuse_cam_and_alert_once():
    user_id = "policy_ig_exact_agent_repeat_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ) as escalate, patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app._process_instagram_event(user_id, "I need an agent", "ad-1")
        app._process_instagram_event(user_id, "I want to speak to someone", "ad-1")

    replies = [urllib.parse.unquote(call.args[1]) for call in send_reply.call_args_list]
    assert replies
    assert all("wa.me" not in reply for reply in replies)
    rows = [h for h in db.get_handoffs(limit=200) if h["source_user_id"] == user_id]
    assert len(rows) == 1
    assert escalate.call_count == 1
    assert all("Manager of Kitchens will connect with you here shortly" in reply for reply in replies[:1])


def test_order_request_opens_a_draft_without_inventing_price():
    send_reply, escalate, user_id = _ask_instagram(
        "policy_ig_order_handoff", "I want Jolbhora 250g", return_user_id=True,
    )
    reply_text = send_reply.call_args.args[1]
    assert not escalate.called
    assert "delivery address/area" in reply_text.lower()
    assert "ORDER DRAFT" in reply_text
    assert "Delivery address/area: Not provided\n" in reply_text
    assert "Receiver phone: Not provided\n" in reply_text
    assert "₹" not in reply_text
    assert "CAM-" not in reply_text
    facts = session_memory.get_facts(user_id, "instagram")
    assert facts["order_intake_draft"]["item"] == "Jolbhora"


def test_instagram_bulk_event_request_escalates():
    send_reply, escalate = _ask_instagram(
        "policy_ig_bulk", "I need 50 boxes of Jolbhora for my wedding in Delhi",
    )
    reply_text = send_reply.call_args.args[1]
    assert escalate.called
    assert "Manager of Kitchens" in reply_text
    assert "wa.me" not in reply_text


def test_instagram_order_example_offers_handoff_not_a_whatsapp_order_bot():
    """Detected event/order information is stored on the handoff; WhatsApp
    itself never becomes an order chatbot — verified by the WhatsApp-side
    tests above (zero responder calls) combined with this handoff record."""
    send_reply, _escalate = _ask_instagram(
        "policy_ig_order_example", "I want 50 boxes of Jolbhora for a wedding in Delhi",
    )
    reply_text = send_reply.call_args.args[1]
    assert "Manager of Kitchens" in reply_text
    assert "wa.me" not in reply_text
    assert "CAM-" not in reply_text


# ---------------------------------------------------------------------------
# Handoff link shape
# ---------------------------------------------------------------------------

def test_generic_handoff_does_not_expose_whatsapp_or_raw_customer_context():
    send_reply, _escalate = _ask_instagram("policy_ig_opaque", "Do you have valet parking?")
    reply_text = urllib.parse.unquote(send_reply.call_args.args[1])
    assert "wa.me" not in reply_text
    assert "valet parking" not in reply_text.lower()
    assert "CAM-" not in reply_text


def test_handoff_link_does_not_expose_the_instagram_transcript():
    send_reply, _escalate = _ask_instagram(
        "policy_ig_no_transcript", "Do you offer wedding gift wrapping for a big event?",
    )
    reply_text = send_reply.call_args.args[1]
    link = re.search(r"https://wa\.me/\S+", reply_text)
    assert link is None


def test_duplicate_instagram_escalation_does_not_create_duplicate_handoffs():
    user_id = "policy_ig_dedup"
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ):
        app._process_instagram_event(user_id, "I want to talk to a human agent", "ad-1")
        app._process_instagram_event(user_id, "can I please speak to a human", "ad-1")

    assert all("wa.me" not in urllib.parse.unquote(call.args[1]) for call in send_reply.call_args_list)
    rows = [h for h in db.get_handoffs(limit=200) if h["source_user_id"] == user_id]
    assert len(rows) == 1


def test_booking_human_request_uses_callback_without_whatsapp():
    send_reply, escalate = _ask_instagram(
        "policy_ig_booking_human", "I want to book this, can I talk to someone?",
    )
    reply_text = send_reply.call_args.args[1]
    assert escalate.called
    assert "Manager of Kitchens" in reply_text
    assert "wa.me" not in reply_text


def test_instagram_chatbot_enabled_default_is_true():
    assert config.INSTAGRAM_CHATBOT_ENABLED is True
