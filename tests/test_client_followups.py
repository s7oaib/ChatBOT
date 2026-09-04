from unittest.mock import patch

from camellia import app
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

    assert reply[2] == "sondesh_price_detailed"
    assert "Yes, we deliver to" not in reply[0]
    assert session_memory.get_facts(user_id, "whatsapp")["awaiting_area_confirmation"] is False


def test_instagram_escalation_requests_and_attaches_whatsapp_number():
    user_id = "client_instagram_contact"
    session_memory.clear_session(user_id, "instagram")

    with patch.object(
        app.responder,
        "match_and_respond",
        return_value=("standard escalation", False, None, None),
    ), patch.object(app, "send_instagram_reply") as send_reply, patch.object(
        app.escalator, "handle_escalation"
    ) as escalate:
        app._process_instagram_event(user_id, "I need help with an unusual request", "ad-1")
        assert send_reply.call_args.args[1] == (
            "Could you share your WhatsApp number so our team can reach you directly?"
        )
        assert not escalate.called

        app._process_instagram_event(user_id, "I do not have it handy", "ad-1")
        assert send_reply.call_args.args[1] == (
            "Could you share your WhatsApp number so our team can reach you directly?"
        )
        assert not escalate.called

        app._process_instagram_event(user_id, "My WhatsApp number is +91 9876543210", "ad-1")
        assert escalate.call_args.kwargs["context_data"]["customer_whatsapp_number"] == "+919876543210"
        assert session_memory.get_facts(user_id, "instagram")["awaiting_instagram_contact"] is False
        assert send_reply.call_args.args[1] == "standard escalation"


def test_whatsapp_escalation_keeps_sender_phone_and_never_requests_contact():
    with patch(
        "camellia.whatsapp_adapter.responder.match_and_respond",
        return_value=("standard escalation", False, None, None),
    ), patch("camellia.whatsapp_adapter.escalator.handle_escalation") as escalate, patch.object(
        whatsapp_adapter, "send_message"
    ) as send_message:
        whatsapp_adapter.process_incoming_message("919876543210", "I need help")

    assert escalate.call_args.kwargs["context_data"]["phone"] == "919876543210"
    assert send_message.call_args.args[1] == "standard escalation"
