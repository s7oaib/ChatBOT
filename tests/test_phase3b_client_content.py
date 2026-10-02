"""Phase 3B client-confirmed content and precedence contract."""

import uuid

import pytest

from camellia import client_qa
from camellia.responder import responder
from camellia.session_memory import session_memory


def _ask(message, user_id=None):
    user_id = user_id or f"phase3b_{uuid.uuid4().hex[:10]}"
    session_memory.clear_session(user_id, "whatsapp")
    return responder.match_and_respond(message, user_id=user_id, channel="whatsapp")


def _templates():
    return {item["id"]: item for item in responder.qa_data["templates"]}


SONDESH_CROSSSELL = (
    "We also prepare and deliver freshly cooked Bengali meals across Delhi NCR. "
    "I've attached this week's menu in case you'd like to have a look. Everything "
    "is cooked fresh in small batches, and some dishes are available only in limited "
    "quantities. If you'd like a recommendation based on whether you prefer "
    "vegetarian, fish or meat, just let me know — I'd be happy to help."
)
OUT_OF_AREA = (
    "Hi, thank you for getting in touch.\n\n"
    "I would have loved to. However, the Norom Pak Sondesh that we make can be "
    "transported from one city to another only through a reliable cold-chain courier "
    "service. Unfortunately, we haven't yet found a partner who can offer this at a "
    "reasonable cost.\n\n"
    "So for now, we're unable to ship our sondesh outside Delhi NCR — we do not "
    "deliver to Bangalore or other cities at this time. We'd love to, though, and "
    "will certainly start as soon as we find a dependable cold-chain delivery option.\n\n"
    "That said, many of our regular customers who live in different parts of India "
    "and the world do buy our sondesh by asking friends or family travelling from "
    "Delhi NCR to carry a few boxes for them. We're happy to deliver the order to "
    "their home, office, hotel, or any other convenient location before they travel.\n\n"
    "Thank you for thinking of camellia.artisanal!"
)
BOOKING_TIME = (
    "We always recommend pre-ordering. It helps us ensure that the dishes you want "
    "are available, exactly as you'd like them. Since we cook everything in small "
    "batches, some dishes may sell out if you order at the last minute. Pre-ordering "
    "is the best way to avoid disappointment."
)
DELIVERY_TIME = (
    "Hi! Thank you for getting in touch.\n\n"
    "We request you to always pre order. So that way the delivery time can be at your "
    "convenience subject to our kitchen timings. If you are unable to pre order, once "
    "your order is confirmed, we usually dispatch it from our kitchen in Sushant Lok, "
    "Gurgaon within 45 minutes.\n\n"
    "Delivery time after that depends on the Porter rider's travel time, which varies "
    "based on traffic and the time of day.\n\n"
    "Our kitchen is open 10:00 AM – 7:30 PM every day. You may also call +91 87965 "
    "51990 or DM to pre order."
)


def test_unregistered_sondesh_crosssell_hands_off_without_legacy_menu_push():
    user_id = f"crosssell_{uuid.uuid4().hex[:10]}"
    session_memory.clear_session(user_id, "instagram")

    first = responder.match_and_respond(
        "What is the sondesh price?", user_id=user_id, channel="instagram"
    )
    second = responder.match_and_respond(
        "Do you also cook meals?", user_id=user_id, channel="instagram"
    )
    third = responder.match_and_respond(
        "How long to dispatch sondesh?", user_id=user_id, channel="instagram"
    )

    assert first[1] is True
    assert second[1] is False
    assert second[2] is None
    assert "manager of kitchens" in second[0].lower()
    assert second[3] is None
    assert SONDESH_CROSSSELL not in second[0]
    assert third[1] is True


def test_unrelated_query_does_not_trigger_sondesh_to_savoury_crosssell():
    text, matched, template_id, images = _ask("where is your kitchen")
    assert matched is False
    assert template_id == "GUARD_pickup_address_hours"
    assert "Manager of Kitchens" in text
    assert "Sushant Lok" not in text
    assert images is None


@pytest.mark.parametrize("message", [
    "when should I book?",
    "when to book",
    "how early should I book",
    "advance booking",
])
def test_savoury_booking_time_variants_use_exact_copy(message):
    text, matched, template_id, _images = _ask(message)
    assert matched is True
    assert template_id == "savoury_best_time_to_book"
    assert text == client_qa.resolve_effective_approved_qa_by_id("best_time_to_book").entry.answer


@pytest.mark.parametrize("message", [
    "how long will delivery take?",
    "how long does delivery take",
    "what is the delivery time?",
])
def test_generic_delivery_time_variants_use_savoury_exact_copy(message):
    text, matched, template_id, _images = _ask(message)
    assert matched is True
    assert template_id == "savoury_delivery_time"
    assert text == client_qa.resolve_effective_approved_qa_by_id("savoury_delivery_time").entry.answer


def test_sondesh_delivery_time_remains_separate():
    _text, matched, template_id, _images = _ask("how long to deliver sondesh")
    assert matched is True
    assert template_id == "sondesh_dispatch_time"


def test_subscription_context_remains_separate_from_savoury_booking_time():
    user_id = f"subscription_{uuid.uuid4().hex[:10]}"
    session_memory.clear_session(user_id, "whatsapp")
    responder.match_and_respond("Tell me about the subscription", user_id=user_id, channel="whatsapp")
    text, matched, template_id, images = responder.match_and_respond("when should I book?", user_id=user_id, channel="whatsapp")
    assert matched is False
    assert template_id == "subscription_booking_steps"
    assert "Manager of Kitchens" in text
    assert "5 hours" not in text
    assert images is None


def test_subscription_delivery_time_does_not_borrow_savoury_duration():
    text, matched, template_id, _images = _ask("subscription delivery time")
    assert matched is True
    assert template_id == "subscription_details"
    assert "45 minutes" not in text
    assert "Price of 5 / 10 / 30 MEALS" in text


def test_out_of_area_response_is_verbatim_client_copy():
    text, matched, template_id, images = _ask("do you deliver to Bangalore")
    assert matched is True
    assert template_id == "delivery_out_of_area"
    assert text == client_qa.resolve_effective_approved_qa_by_id("delivery_out_of_area_client").entry.answer
    assert images is None


def test_unsupported_kolkata_question_hands_off_without_retired_facts():
    text, matched, template_id, images = _ask("who runs the kitchen now that you're moving to kolkata")
    assert matched is False
    assert template_id == "kolkata_expansion_info"
    assert "Manager of Kitchens" in text
    assert "@gmail.com" not in text
    assert images is None


def test_manager_guard_contains_approved_email():
    template = _templates()["GUARD_staff_names_manager_contact"]
    assert "camellia.artisanal.gurgaon@gmail.com" in template["response"]
    assert "CONFLICTED" not in template["response"]


def test_phase3b_canonical_templates_are_exact_repository_entries():
    templates = _templates()
    assert templates["sondesh_crosssell_savoury"]["response"] == SONDESH_CROSSSELL
    assert templates["delivery_out_of_area"]["response"] == OUT_OF_AREA
    assert templates["savoury_best_time_to_book"]["response"] == BOOKING_TIME
    assert templates["savoury_delivery_time"]["response"] == DELIVERY_TIME
