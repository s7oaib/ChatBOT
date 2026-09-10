import json
import re
import urllib.parse
from unittest.mock import patch

from camellia import app
from camellia.config import config
from camellia.database import db
from camellia.responder import responder
from camellia.session_memory import session_memory
from camellia.whatsapp_adapter import whatsapp_adapter


def test_delivery_area_followup_confirms_a_serviceable_area_and_keeps_area_flow_active():
    user_id = "client_delivery_followup"
    session_memory.clear_session(user_id, "whatsapp")

    responder.match_and_respond("Where do you deliver?", user_id=user_id, channel="whatsapp")
    assert session_memory.get_facts(user_id, "whatsapp")["awaiting_area_confirmation"] is True

    reply = responder.match_and_respond(
        "Delhi university north campus", user_id=user_id, channel="whatsapp"
    )
    assert reply[0] == (
        "Yes, we deliver to Delhi university north campus — courier charges on actuals "
        "from Sushant Lok, Gurgaon."
    )
    assert reply[2] == "delivery_area_confirmation"
    assert session_memory.get_facts(user_id, "whatsapp")["awaiting_area_confirmation"] is True


def test_delivery_area_flow_confirms_two_serviceable_localities_in_a_row():
    user_id = "client_two_delivery_areas"
    session_memory.clear_session(user_id, "whatsapp")

    responder.match_and_respond("Where do you deliver?", user_id=user_id, channel="whatsapp")
    chatra_marg = responder.match_and_respond("Chatra Marg", user_id=user_id, channel="whatsapp")
    lajpat_nagar = responder.match_and_respond("Lajpat Nagar", user_id=user_id, channel="whatsapp")

    assert chatra_marg[0] == (
        "Yes, we deliver to Chatra Marg — courier charges on actuals from Sushant Lok, Gurgaon."
    )
    assert lajpat_nagar[0] == (
        "Yes, we deliver to Lajpat Nagar — courier charges on actuals from Sushant Lok, Gurgaon."
    )
    assert chatra_marg[2] == lajpat_nagar[2] == "delivery_area_confirmation"
    assert session_memory.get_facts(user_id, "whatsapp")["awaiting_area_confirmation"] is True


def test_delivery_area_flow_confirms_faridabad_and_ghaziabad_localities():
    for area in ("Neelam Bagh", "Raj Nagar Extension"):
        user_id = f"client_serviceable_{area.lower().replace(' ', '_')}"
        session_memory.clear_session(user_id, "whatsapp")
        responder.match_and_respond("Where do you deliver?", user_id=user_id, channel="whatsapp")

        reply = responder.match_and_respond(area, user_id=user_id, channel="whatsapp")
        assert reply[0] == (
            f"Yes, we deliver to {area} — courier charges on actuals from Sushant Lok, Gurgaon."
        )
        assert reply[2] == "delivery_area_confirmation"


def test_delivery_area_flow_fails_closed_for_mumbai_and_bijapur():
    for area in ("Mumbai", "Bijapur"):
        user_id = f"client_out_of_area_{area.lower()}"
        session_memory.clear_session(user_id, "whatsapp")
        responder.match_and_respond("Where do you deliver?", user_id=user_id, channel="whatsapp")

        reply = responder.match_and_respond(area, user_id=user_id, channel="whatsapp")
        assert reply[2] == "delivery_out_of_area"
        assert "unable to ship our sondesh outside Delhi NCR" in reply[0]
        assert "Yes, we deliver" not in reply[0]
        assert session_memory.get_facts(user_id, "whatsapp")["awaiting_area_confirmation"] is False


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


def test_instagram_escalation_offers_a_whatsapp_link_instead_of_asking_for_a_number():
    """
    OLD REQUIREMENT: an Instagram customer whose question needed a human was
    asked to type their WhatsApp number, then asked to confirm it, before an
    escalation was ever recorded — three round trips, a number typed by hand,
    and staff calling the customer back.

    NEW REQUIREMENT: Instagram never asks for a phone number. It creates an
    opaque handoff reference immediately and offers a WhatsApp click-to-chat
    link the customer opens themselves — one round trip, no typed number
    stored on Instagram, no confusion about who initiates contact.

    WHY CHANGED: explicit client channel-policy requirement — Instagram AI,
    WhatsApp human-only, connected by a handoff link rather than a collected
    phone number.
    """
    user_id = "client_instagram_handoff"
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
    assert "WhatsApp" in reply_text
    assert "https://wa.me/919800000000" in reply_text

    ref_match = re.search(r"CAM-\d{5,6}", reply_text)
    assert ref_match, f"no opaque handoff reference in {reply_text!r}"
    assert f"Ref:%20{ref_match.group(0)}" in reply_text or f"Ref: {ref_match.group(0)}" in urllib.parse.unquote(reply_text)


def test_repeated_human_requests_reuse_the_same_handoff_reference():
    """A customer saying "human" three times gets ONE reference, not three —
    the active handoff is looked up and reused rather than re-created."""
    user_id = "client_instagram_repeat_handoff"
    session_memory.clear_session(user_id, "instagram")

    with patch.object(
        app.responder,
        "match_and_respond",
        return_value=("standard escalation", False, None, None),
    ), patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ), patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app._process_instagram_event(user_id, "I want to speak to someone", "ad-1")
        first_ref = re.search(r"CAM-\d{5,6}", send_reply.call_args.args[1]).group(0)

        app._process_instagram_event(user_id, "human please", "ad-1")
        second_ref = re.search(r"CAM-\d{5,6}", send_reply.call_args.args[1]).group(0)

        app._process_instagram_event(user_id, "is anyone there", "ad-1")
        third_ref = re.search(r"CAM-\d{5,6}", send_reply.call_args.args[1]).group(0)

    assert first_ref == second_ref == third_ref
    rows_for_this_user = [
        h for h in db.get_handoffs(limit=200) if h["source_user_id"] == user_id
    ]
    assert len(rows_for_this_user) == 1


def test_known_instagram_details_reach_the_handoff_without_being_re_asked():
    """"50 boxes of Jolbhora for a wedding in Delhi" already told the bot the
    product, quantity, event and area — the handoff must carry them, not make
    the customer repeat them before generating the WhatsApp link."""
    user_id = "client_instagram_known_details"
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

    reference = re.search(r"CAM-\d{5,6}", send_reply.call_args.args[1]).group(0)
    handoff = db.get_handoff_by_ref(reference)
    known = json.loads(handoff["known_entities"])
    assert known["product"] == "Jolbhora"
    assert known["quantity"] == 50
    assert known["event"] == "wedding"
    assert known["location"] == "Delhi"
    assert "Jolbhora" in handoff["summary"]
    assert "50" in handoff["summary"]


def test_the_handoff_link_carries_no_conversation_or_personal_data():
    """The public wa.me URL holds only the opaque reference — no product
    details, no customer data, no conversation contents."""
    user_id = "client_instagram_opaque_link"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["last_entities"] = {"product": "Jolbhora", "location": "Delhi", "event": "wedding"}

    with patch.object(
        app.responder,
        "match_and_respond",
        return_value=("standard escalation", False, None, None),
    ), patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ), patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app._process_instagram_event(user_id, "50 boxes of Jolbhora for a wedding in Delhi", "ad-1")

    reply_text = send_reply.call_args.args[1]
    link = re.search(r"https://wa\.me/\S+", reply_text).group(0)
    decoded = urllib.parse.unquote(link)
    assert "Jolbhora" not in decoded
    assert "Delhi" not in decoded
    assert "wedding" not in decoded
    assert user_id not in decoded


def test_savoury_ad_context_routes_a_vague_opening_but_explicit_sweet_wins():
    user_id = "client_savoury_ad"
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app, "send_instagram_reply") as send_reply:
        app._process_instagram_event(
            user_id, "Hi", "ad-savoury",
            app.EntryPoint(ad_id="ad-savoury", ad_title="Fresh Bengali Savoury Meals"),
        )
        assert "Bengali food" in send_reply.call_args.args[1]

        app._process_instagram_event(user_id, "Sondesh price", "ad-savoury")
        assert "Sondesh" in send_reply.call_args.args[1]


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


def test_bare_price_question_after_meals_topic_still_asks_about_the_weekly_menu():
    user_id = "client_meals_then_price"
    session_memory.clear_session(user_id, "whatsapp")

    responder.match_and_respond("combo", user_id=user_id, channel="whatsapp")
    reply = responder.match_and_respond("What's the price?", user_id=user_id, channel="whatsapp")

    assert reply[2] == "context_meal_price_followup"


def test_whatsapp_never_escalates_or_replies_it_is_already_a_human_channel():
    """
    OLD REQUIREMENT: a WhatsApp message that the chatbot could not answer
    triggered escalator.handle_escalation and a bot-composed reply — the
    same escalation path Instagram uses.

    NEW REQUIREMENT: WhatsApp has no chatbot to escalate FROM any more. A
    human is already reading the message directly, so there is nothing to
    hand off and nothing to auto-reply with.

    WHY CHANGED: explicit client channel-policy requirement.
    """
    with patch(
        "camellia.whatsapp_adapter.responder.match_and_respond",
        return_value=("standard escalation", False, None, None),
    ) as match_and_respond, patch(
        "camellia.whatsapp_adapter.escalator.handle_escalation"
    ) as escalate, patch.object(whatsapp_adapter, "send_message") as send_message:
        result = whatsapp_adapter.process_incoming_message("919876543210", "I need help")

    assert not escalate.called
    assert not match_and_respond.called
    assert not send_message.called
    assert result["bot_replied"] is False


def test_a_whatsapp_message_naming_a_handoff_reference_still_gets_no_bot_reply():
    """A customer pasting "Ref: CAM-48371" is recorded for staff tracking —
    the reference is marked connected — but still receives no automated
    reply. Nothing about a handoff reference makes WhatsApp intelligent."""
    with patch(
        "camellia.whatsapp_adapter.mark_whatsapp_connected",
    ) as mark_connected, patch(
        "camellia.whatsapp_adapter.responder.match_and_respond"
    ) as match_and_respond, patch.object(whatsapp_adapter, "send_message") as send_message:
        result = whatsapp_adapter.process_incoming_message(
            "919876543210", "Hi, I'm continuing my Camellia Instagram enquiry. Ref: CAM-48371",
        )

    assert result["bot_replied"] is False
    assert result["handoff_ref"] == "CAM-48371"
    mark_connected.assert_called_once_with("CAM-48371")
    match_and_respond.assert_not_called()
    send_message.assert_not_called()


def test_asking_about_delivery_outside_ncr_lists_the_covered_cities():
    # The client's report: ~50 people asked this, then named Gurgaon, Noida and
    # Faridabad. The bot used to answer with the out-of-area refusal ("we are
    # unable to ship outside Delhi NCR"), so a customer in Gurgaon was told no
    # and naturally replied with their city to correct it.
    user_id = "client_outside_ncr_question"
    session_memory.clear_session(user_id, "whatsapp")

    text, _matched, template_id, _ = responder.match_and_respond(
        "Do you deliver outside Delhi NCR?", user_id=user_id, channel="whatsapp"
    )

    assert template_id == "delivery_area"
    assert "unable to ship our sondesh outside Delhi NCR" not in text
    for city in ("Delhi", "Gurgaon", "Noida", "Ghaziabad", "Faridabad"):
        assert city in text
    assert "Which area are you in?" in text


def test_the_zone_question_then_a_city_confirms_instead_of_repeating():
    # Naming the zone is not naming your own area. Treating the "Delhi" in
    # "outside Delhi NCR" as the customer's location suppressed the area
    # question, so their next message re-matched the template and repeated it.
    user_id = "client_outside_ncr_then_city"
    session_memory.clear_session(user_id, "whatsapp")

    first, _m1, _t1, _ = responder.match_and_respond(
        "Do you deliver outside Delhi NCR?", user_id=user_id, channel="whatsapp"
    )
    second, _m2, template_id, _ = responder.match_and_respond(
        "Gurgaon", user_id=user_id, channel="whatsapp"
    )

    assert template_id == "delivery_area_confirmation"
    assert second != first
    assert "Yes, we deliver to Gurgaon" in second


def test_a_genuinely_out_of_zone_city_still_gets_the_approved_refusal():
    for message in ("do you deliver to bangalore", "can you ship to mumbai"):
        user_id = f"client_out_of_zone_{message.split()[-1]}"
        session_memory.clear_session(user_id, "whatsapp")

        text, _matched, template_id, _ = responder.match_and_respond(
            message, user_id=user_id, channel="whatsapp"
        )

        assert template_id == "delivery_out_of_area"
        assert "unable to ship our sondesh outside Delhi NCR" in text


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
