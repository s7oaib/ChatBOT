"""Phase 3B client-confirmed content and precedence contract."""

import uuid

import pytest

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


def test_sondesh_to_savoury_crosssell_is_exact_and_attached_once():
    user_id = f"crosssell_{uuid.uuid4().hex[:10]}"
    session_memory.clear_session(user_id, "whatsapp")

    first = responder.match_and_respond(
        "What is the sondesh price?", user_id=user_id, channel="whatsapp"
    )
    second = responder.match_and_respond(
        "Do you also cook meals?", user_id=user_id, channel="whatsapp"
    )
    third = responder.match_and_respond(
        "How long to dispatch sondesh?", user_id=user_id, channel="whatsapp"
    )

    assert SONDESH_CROSSSELL in second[0]
    assert second[3] is not None
    second_image = second[3][0] if isinstance(second[3], list) else second[3]
    second_image_url = second_image.get("url", "") if isinstance(second_image, dict) else second_image
    assert "/images/" in second_image_url
    assert SONDESH_CROSSSELL not in third[0]
    assert "We also cook fresh Bengali meals for Delhi NCR" in first[0]


def test_unrelated_query_does_not_trigger_sondesh_to_savoury_crosssell():
    text, matched, template_id, images = _ask("what is the kitchen address")
    assert matched is True
    assert template_id == "GUARD_pickup_address_hours"
    assert SONDESH_CROSSSELL not in text
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
    assert text.startswith(BOOKING_TIME)


@pytest.mark.parametrize("message", [
    "how long will delivery take?",
    "how long does delivery take",
    "what is the delivery time?",
])
def test_generic_delivery_time_variants_use_savoury_exact_copy(message):
    text, matched, template_id, _images = _ask(message)
    assert matched is True
    assert template_id == "savoury_delivery_time"
    assert text.startswith(DELIVERY_TIME)


def test_sondesh_delivery_time_remains_separate():
    _text, matched, template_id, _images = _ask("how long to deliver sondesh")
    assert matched is True
    assert template_id == "sondesh_dispatch_time"


def test_subscription_context_remains_separate_from_savoury_booking_time():
    user_id = f"subscription_{uuid.uuid4().hex[:10]}"
    session_memory.clear_session(user_id, "whatsapp")
    responder.match_and_respond(
        "Tell me about the subscription", user_id=user_id, channel="whatsapp"
    )
    _text, matched, template_id, _images = responder.match_and_respond(
        "when should I book?", user_id=user_id, channel="whatsapp"
    )
    assert matched is True
    assert template_id == "subscription_booking_steps"


def test_out_of_area_response_is_verbatim_client_copy():
    text, matched, template_id, images = _ask("do you deliver to Bangalore")
    assert matched is True
    assert template_id == "delivery_out_of_area"
    assert text == OUT_OF_AREA
    assert images is None


def test_kolkata_response_has_only_approved_facts_and_manager_email():
    text, matched, template_id, _images = _ask(
        "who runs the kitchen now that you're moving to kolkata"
    )
    assert matched is True
    assert template_id == "kolkata_expansion_info"
    assert "Guru Dayal Singh" in text
    assert "camellia.artisanal.gurgaon@gmail.com" in text
    assert "confirmed relocation date" not in text.lower()
    assert "new address" not in text.lower()


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
