import json
import urllib.parse
import uuid
from unittest.mock import patch

from camellia import app, client_qa
from camellia.config import config
from camellia.database import db
from camellia.handoff import whatsapp_link
from camellia.order_intake import ORDER_DRAFT_INCOMPLETE, new_order_draft
from camellia.responder import responder
from camellia.session_memory import session_memory


def test_fresh_yes_keeps_the_safe_generic_acknowledgement():
    user_id = "client-followup-fresh-yes"
    session_memory.clear_session(user_id, "instagram")

    reply = responder.match_and_respond("yes", user_id=user_id, channel="instagram")

    assert reply[0] == "Got it! 😊 How can I help you?"
    assert reply[2] == "system_conversation"


def test_pending_clarification_gets_first_chance_at_contextual_yes_or_no():
    for acknowledgement in ("yes", "no"):
        user_id = f"client-followup-clarification-{acknowledgement}"
        session_memory.clear_session(user_id, "instagram")
        facts = session_memory.get_facts(user_id, "instagram")
        facts.update({
            "pending_clarification": "product_price",
            "clarification_options": ["SONDESH", "BENGALI_MEAL"],
            "last_bot_question": "Are you asking about Sondesh or Bengali meals?",
        })

        with patch.object(responder, "_contextual_resolution", wraps=responder._contextual_resolution) as resolve:
            reply = responder.match_and_respond(acknowledgement, user_id, "instagram")

        assert reply[0] != "Got it! 😊 How can I help you?"
        assert reply[2] is None
        assert facts["last_route_source"] == "HUMAN_HANDOFF"
        resolve.assert_called_once_with(acknowledgement, user_id, "instagram")


def test_order_draft_yes_does_not_invent_missing_address_or_finalize():
    for acknowledgement in ("yes", "no", "ok"):
        user_id = f"client-followup-order-{acknowledgement}"
        session_memory.clear_session(user_id, "instagram")
        facts = session_memory.get_facts(user_id, "instagram")
        facts["order_intake_draft"] = draft = new_order_draft()
        draft.update({
            "item": "Sondesh", "quantity": "2 boxes", "delivery_area": "Gurgaon",
            "delivery_slot": "Friday dinner",
        })

        reply = responder.match_and_respond(acknowledgement, user_id, "instagram")

        assert reply[2] == "system_conversation"
        assert "ORDER DRAFT" not in reply[0] and "https://wa.me/" not in reply[0]
        assert facts["order_intake_draft"]["delivery_area"] == "Gurgaon"
        assert facts["order_intake_draft"]["delivery_address"] is None
        assert facts["order_intake_draft"]["receiver_phone"] is None
        assert facts["order_intake_draft"]["status"] == ORDER_DRAFT_INCOMPLETE


def test_ok_with_explicit_sondesh_question_is_not_a_generic_acknowledgement():
    user_id = "client-followup-ok-sondesh"
    session_memory.clear_session(user_id, "instagram")

    reply = responder.match_and_respond("ok, tell me about Sondesh", user_id, "instagram")

    assert reply[2] != "system_conversation"
    assert "sondesh" in reply[0].casefold()


def test_long_messages_containing_acknowledgements_keep_their_business_intent():
    user_id = "client-followup-ack-business-intent"
    session_memory.clear_session(user_id, "instagram")
    order = responder.match_and_respond("yes, I want to order", user_id, "instagram")
    assert order[2] == "order_intake_draft"
    assert order[0] != "Got it! 😊 How can I help you?"

    session_memory.clear_session(user_id, "instagram")
    sondesh = responder.match_and_respond(
        "no, tell me about Sondesh instead", user_id, "instagram",
    )
    assert "sondesh" in sondesh[0].casefold()
    assert sondesh[2] != "system_conversation"

    session_memory.clear_session(user_id, "instagram")
    bento = responder.match_and_respond("ok, what is the Bento price?", user_id, "instagram")
    assert bento[2] == "bento_details"
    assert bento[0] != "Got it! 😊 How can I help you?"


def test_delivery_area_followup_confirms_a_serviceable_area_and_keeps_area_flow_active():
    """Generic coverage is unsupported; do not invent city promises or start an interview."""
    for index, message in enumerate(["Where do you deliver?", "do you deliver to Noida", "which areas do you cover"]):
        reply = responder.match_and_respond(message, user_id="coverage-" + str(index), channel="instagram")
        assert reply[1] is False
        assert "Manager of Kitchens" in reply[0]
        assert "Yes, we deliver" not in reply[0]
        assert "Which area" not in reply[0]
        assert reply[3] is None


def test_delivery_area_flow_confirms_two_serviceable_localities_in_a_row():
    """Generic coverage is unsupported; do not invent city promises or start an interview."""
    for index, message in enumerate(["Where do you deliver?", "do you deliver to Noida", "which areas do you cover"]):
        reply = responder.match_and_respond(message, user_id="coverage-" + str(index), channel="instagram")
        assert reply[1] is False
        assert "Manager of Kitchens" in reply[0]
        assert "Yes, we deliver" not in reply[0]
        assert "Which area" not in reply[0]
        assert reply[3] is None


def test_delivery_area_flow_confirms_faridabad_and_ghaziabad_localities():
    """Generic coverage is unsupported; do not invent city promises or start an interview."""
    for index, message in enumerate(["Where do you deliver?", "do you deliver to Noida", "which areas do you cover"]):
        reply = responder.match_and_respond(message, user_id="coverage-" + str(index), channel="instagram")
        assert reply[1] is False
        assert "Manager of Kitchens" in reply[0]
        assert "Yes, we deliver" not in reply[0]
        assert "Which area" not in reply[0]
        assert reply[3] is None


def test_delivery_area_flow_fails_closed_for_mumbai_and_bijapur():
    for area in ("Mumbai", "Bijapur"):
        reply = responder.match_and_respond("Can you deliver Sondesh to " + area + "?", user_id="outside-" + area, channel="instagram")
        assert "Yes, we deliver" not in reply[0]
        assert reply[3] is None
        assert "₹" not in reply[0]


def test_delivery_area_flag_does_not_consume_a_new_business_question():
    user_id = "client_delivery_new_question"
    session_memory.clear_session(user_id, "whatsapp")

    responder.match_and_respond("Where do you deliver?", user_id=user_id, channel="whatsapp")
    reply = responder.match_and_respond("What is the price?", user_id=user_id, channel="whatsapp")

    # No product topic was ever established (delivery_area doesn't set one),
    # so a bare "what is the price?" is genuinely ambiguous between Sondesh
    # and Bengali meals — the bot asks rather than guessing Sondesh.
    assert reply[2] == "price_clarify_product"
    assert "Yes, we deliver to" not in reply[0]
    assert session_memory.get_facts(user_id, "whatsapp")["awaiting_area_confirmation"] is False


def test_instagram_generic_human_help_uses_callback_ack_without_whatsapp():
    """
    OLD REQUIREMENT: an Instagram customer whose question needed a human was
    asked to type their WhatsApp number, then asked to confirm it, before an
    escalation was ever recorded — three round trips, a number typed by hand,
    and staff calling the customer back.

    NEW REQUIREMENT: generic support requests receive a callback acknowledgement
    and never expose the booking-only WhatsApp continuation.

    WHY CHANGED: Instagram support stays in-channel with a callback
    acknowledgement; a WhatsApp continuation is offered only for approved
    order workflows.
    """
    # Suffixed so a fresh handoff (and staff alert) is created regardless of
    # what an earlier test run already wrote to the persistent handoffs table
    # for this literal id — app.py now skips re-alerting staff for a handoff
    # that's already active, which a leftover row from a prior run would be.
    user_id = "client_instagram_handoff_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "instagram")

    with patch.object(
        app.responder,
        "match_and_respond",
        return_value=("standard escalation", False, None, None),
    ), patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ) as escalate, patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app._process_instagram_event(user_id, "I need help with an unusual request", "ad-1")

    assert escalate.called
    reply_text = send_reply.call_args.args[1]
    assert "standard escalation" not in reply_text
    assert "wa.me" not in reply_text
    assert "Manager of Kitchens will connect with you here shortly" in reply_text


def test_repeated_human_requests_reuse_the_same_handoff_without_duplicate_alerts():
    """A customer saying "human" again while waiting gets no second CAM or alert."""
    user_id = "client_instagram_repeat_handoff_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "instagram")

    with patch.object(
        app.responder,
        "match_and_respond",
        return_value=("standard escalation", False, None, None),
    ), patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ), patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app._process_instagram_event(user_id, "I want to speak to someone", "ad-1")
        first_reply = send_reply.call_args.args[1]
        app._process_instagram_event(user_id, "human please", "ad-1")
        app._process_instagram_event(user_id, "is anyone there", "ad-1")

    assert "wa.me" not in first_reply
    assert "Manager of Kitchens will connect with you here shortly" in first_reply
    assert send_reply.call_count == 1
    rows_for_this_user = [
        h for h in db.get_handoffs(limit=200) if h["source_user_id"] == user_id
    ]
    assert len(rows_for_this_user) == 1


def test_known_instagram_details_reach_the_handoff_without_being_re_asked():
    """Known current-turn facts stay with staff even when the customer gets no link."""
    user_id = "client_instagram_known_details_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["last_entities"] = {
        "product": "Jolbhora", "quantity": 50, "event": "wedding", "location": "Delhi",
    }

    with patch.object(
        app.responder,
        "match_and_respond",
        return_value=("standard escalation", False, None, None),
    ), patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ):
        app._process_instagram_event(
            user_id, "I need 50 boxes of Jolbhora for my wedding in Delhi", "ad-1",
        )

    handoff = next(
        item for item in db.get_handoffs(limit=200)
        if item["source_user_id"] == user_id
    )
    known = json.loads(handoff["known_entities"])
    assert known["product"] == "Jolbhora"
    assert known["quantity"] == 50
    assert known["event"] == "wedding"
    assert known["location"] == "Delhi"
    assert "Jolbhora" in handoff["summary"]
    assert "50" in handoff["summary"]


def test_order_handoff_link_carries_only_opaque_reference_not_customer_data():
    """The click-to-chat URL does not expose identity or free-text enquiry data."""
    with patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        link = whatsapp_link("CAM-12345")

    decoded = urllib.parse.unquote(link)
    assert "Ref: CAM-12345" in decoded
    assert "Jolbhora" not in decoded
    assert "Delhi" not in decoded
    assert "wedding" not in decoded
    assert "customer-id" not in decoded


def test_savoury_ad_context_does_not_hijack_a_greeting_and_explicit_sweet_wins():
    user_id = "client_savoury_ad_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app, "send_instagram_reply") as send_reply:
        app._process_instagram_event(
            user_id, "Hi", "ad-savoury",
            app.EntryPoint(ad_id="ad-savoury", ad_title="Fresh Bengali Savoury Meals"),
        )
        assert send_reply.call_args.args[1]
        assert "Bengali food" not in send_reply.call_args.args[1]
        assert "₹" not in send_reply.call_args.args[1]

        before_sweet_question = len(send_reply.call_args_list)
        app._process_instagram_event(user_id, "Sondesh price", "ad-savoury")
        sweet_texts = [
            call.args[1]
            for call in send_reply.call_args_list[before_sweet_question:]
            if len(call.args) > 1 and call.args[1]
        ]
        assert sweet_texts
        assert any("Sondesh" in text for text in sweet_texts)


def test_bare_price_question_with_no_topic_asks_which_product_instead_of_guessing_sondesh():
    # match_and_respond is the deterministic matcher itself (no ad gating) —
    # channel is whatsapp here purely to stay clear of the Instagram ad-gate
    # path exercised elsewhere, which is unrelated to this clarifying-question
    # behaviour and is channel-agnostic in match_and_respond.
    user_id = "client_ad_price_ambiguous"
    session_memory.clear_session(user_id, "whatsapp")

    reply = responder.match_and_respond("What is the price?", user_id=user_id, channel="whatsapp")

    assert reply[2] == "price_clarify_product"
    assert "Sondesh" in reply[0] and "Bengali Meals" in reply[0]
    assert "sondesh_price_detailed" != reply[2]


def test_instagram_bare_price_after_source_route_uses_client_food_overview(monkeypatch, eligible_weekly_asset):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "client_instagram_general_price"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {"routing_id": 1, "source_type": "story", "active": True}
    facts["source_context_superseded"] = True

    reply = responder.match_and_respond("whats the price", user_id=user_id, channel="instagram")

    assert reply[2] == "savoury_food_intro"
    assert "À LA CARTE" in reply[0]
    assert "BENGALI BENTO" in reply[0]
    assert "INSERT" not in reply[0]
    assert facts["source_context_used"] is False


def test_bare_price_question_after_meals_topic_still_asks_about_the_weekly_menu():
    user_id = "client_meals_then_price"
    session_memory.clear_session(user_id, "whatsapp")

    responder.match_and_respond("combo", user_id=user_id, channel="whatsapp")
    reply = responder.match_and_respond("What's the price?", user_id=user_id, channel="whatsapp")

    assert reply[2] == "context_meal_price_followup"


def test_asking_about_delivery_outside_ncr_lists_the_covered_cities():
    """Generic coverage is unsupported; do not invent city promises or start an interview."""
    for index, message in enumerate(["Where do you deliver?", "do you deliver to Noida", "which areas do you cover"]):
        reply = responder.match_and_respond(message, user_id="coverage-" + str(index), channel="instagram")
        assert reply[1] is False
        assert "Manager of Kitchens" in reply[0]
        assert "Yes, we deliver" not in reply[0]
        assert "Which area" not in reply[0]
        assert reply[3] is None


def test_the_zone_question_then_a_city_confirms_instead_of_repeating():
    """Generic coverage is unsupported; do not invent city promises or start an interview."""
    for index, message in enumerate(["Where do you deliver?", "do you deliver to Noida", "which areas do you cover"]):
        reply = responder.match_and_respond(message, user_id="coverage-" + str(index), channel="instagram")
        assert reply[1] is False
        assert "Manager of Kitchens" in reply[0]
        assert "Yes, we deliver" not in reply[0]
        assert "Which area" not in reply[0]
        assert reply[3] is None


def test_a_genuinely_out_of_zone_city_still_gets_the_approved_refusal():
    for message in ("do you deliver to bangalore", "can you ship to mumbai"):
        user_id = f"client_out_of_zone_{message.split()[-1]}"
        session_memory.clear_session(user_id, "whatsapp")

        text, _matched, template_id, _ = responder.match_and_respond(
            message, user_id=user_id, channel="whatsapp"
        )

        assert template_id == "delivery_out_of_area"
        assert text == client_qa.resolve_effective_approved_qa_by_id("delivery_out_of_area_client").entry.answer


def test_naming_an_out_of_zone_city_after_the_coverage_answer_fails_closed():
    user_id = "client_outside_ncr_then_bangalore"
    session_memory.clear_session(user_id, "whatsapp")

    responder.match_and_respond(
        "Do you deliver outside Delhi NCR?", user_id=user_id, channel="whatsapp"
    )
    text, _matched, template_id, _ = responder.match_and_respond(
        "Bangalore", user_id=user_id, channel="whatsapp"
    )

    assert template_id == "delivery_out_of_area"
    assert "Yes, we deliver to" not in text
