"""Phase 3A Golden Response routing and media guards."""

import uuid

import pytest

from camellia.responder import responder
from camellia.session_memory import session_memory


def _user(prefix="phase3a"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _ask(message, user_id=None):
    user_id = user_id or _user()
    session_memory.clear_session(user_id, "whatsapp")
    return responder.match_and_respond(message, user_id=user_id, channel="whatsapp")


@pytest.mark.parametrize("message", [
    "How do I pay?",
    "UPI?",
    "card payment",
    "bank transfer",
    "payment details",
    "how 2 pay",
])
def test_payment_variants_use_the_exact_payment_guard(message):
    text, matched, template_id, images = _ask(message)
    assert matched is True
    assert template_id == "GUARD_payment_method"
    assert "9625083171" in text
    assert "Yummraj Culinary Expeditions Pvt Ltd" in text
    assert "50200077136337" in text
    assert "HDFC0000572" in text
    assert "razorpay.me/@camelliaartisanal" in text
    assert "cash on pickup" not in text.lower()
    assert images is None


def test_payment_guard_overrides_a_stale_product_topic():
    user_id = _user("payment_context")
    session_memory.clear_session(user_id, "whatsapp")
    responder.match_and_respond("Tell me about Jolbhora", user_id=user_id, channel="whatsapp")

    text, matched, template_id, _images = responder.match_and_respond(
        "How do I pay?", user_id=user_id, channel="whatsapp"
    )
    assert matched is True
    assert template_id == "GUARD_payment_method"
    assert "9625083171" in text


@pytest.mark.parametrize("message", [
    "What is the price of SUBSCRIPTION - HOME FOOD AWAY FROM HOME",
    "Tell me more about SUBSCRIPTION - HOME FOOD AWAY FROM HOME",
])
def test_subscription_canonical_questions_attach_the_supplied_rate_chart(message):
    text, matched, template_id, images = _ask(message)
    assert matched is True
    assert template_id == "subscription_details"
    assert "Gurgaon only" in text
    assert "Everyday Meal Subscription rate chart" in text
    assert isinstance(images, list)
    assert images == ["/images/Everyday%20Meal%20Subscription%20Rate%20Chart.jpeg"]


def test_subscription_context_price_followup_keeps_subscription_context_and_chart():
    user_id = _user("subscription_context")
    session_memory.clear_session(user_id, "whatsapp")
    responder.match_and_respond(
        "Tell me about the subscription", user_id=user_id, channel="whatsapp"
    )

    text, matched, template_id, images = responder.match_and_respond(
        "price?", user_id=user_id, channel="whatsapp"
    )
    assert matched is True
    assert template_id == "subscription_details"
    assert "Everyday Meal Subscription rate chart" in text
    assert images == ["/images/Everyday%20Meal%20Subscription%20Rate%20Chart.jpeg"]


def test_subscription_old_pricing_and_unapproved_claims_are_absent():
    response = next(
        item["response"]
        for item in responder.qa_data["templates"]
        if item["id"] == "subscription_details"
    )
    for stale in ("₹1,500", "₹1,750", "₹2,000", "luchi", "polao", "11th"):
        assert stale.lower() not in response.lower()


def test_general_savoury_intro_does_not_repeat_retired_subscription_price():
    response = next(
        item["response"]
        for item in responder.qa_data["templates"]
        if item["id"] == "savoury_food_intro"
    )
    assert "₹1,500 for 5 meals" not in response
    assert "rate chart" in response.lower()


def test_home_table_introduction_is_deterministic_and_media_blocked():
    text, matched, template_id, images = _ask("Tell me more about Bengali Home Table")
    assert matched is True
    assert template_id == "bengali_home_table_details"
    assert "Bengali Home Table" in text
    assert images is None
    template = next(
        item for item in responder.qa_data["templates"]
        if item["id"] == "bengali_home_table_details"
    )
    assert template["media_status"] == "BLOCKED_PENDING_CLIENT_ASSETS"


def test_home_table_context_bare_price_reuses_the_contextual_home_table_block():
    user_id = _user("home_table_context")
    session_memory.clear_session(user_id, "whatsapp")
    responder.match_and_respond(
        "Tell me about Bengali Home Table", user_id=user_id, channel="whatsapp"
    )

    text, matched, template_id, images = responder.match_and_respond(
        "What's the price?", user_id=user_id, channel="whatsapp"
    )
    assert matched is True
    assert template_id == "bengali_home_table_details"
    assert "₹1,500" not in text
    assert "₹2,500" not in text
    assert images is None


def test_home_table_explicit_detailed_pricing_wins_over_kolkata_expansion():
    text, matched, template_id, images = _ask(
        "Pricing of Bengali home lunch tables Kolkata"
    )
    assert matched is True
    assert template_id == "bengali_home_table_pricing"
    assert "₹1,500 per person" in text
    assert "₹2,500 per person" in text
    assert "8 dishes" in text
    assert "9 dishes" in text
    assert "GST included" in text
    assert images is None


def test_home_table_payment_is_distinct_from_generic_payment():
    user_id = _user("home_table_payment")
    session_memory.clear_session(user_id, "whatsapp")
    responder.match_and_respond(
        "Tell me about Bengali Home Table", user_id=user_id, channel="whatsapp"
    )

    text, matched, template_id, images = responder.match_and_respond(
        "How to pay", user_id=user_id, channel="whatsapp"
    )
    assert matched is True
    assert template_id == "bengali_home_table_payment"
    assert "Full payment is required" in text
    assert "GST invoice" in text
    assert images is None


def test_explicit_product_switch_clears_home_table_context():
    user_id = _user("home_table_switch")
    session_memory.clear_session(user_id, "whatsapp")
    responder.match_and_respond(
        "Tell me about Bengali Home Table", user_id=user_id, channel="whatsapp"
    )

    _text, matched, template_id, _images = responder.match_and_respond(
        "Tell me about Kanchagolla", user_id=user_id, channel="whatsapp"
    )
    assert matched is True
    assert template_id == "kanchagolla"


@pytest.mark.parametrize("message", [
    "I want to order",
    "I'm ready to place the order",
    "Let's confirm",
    "finalise it",
    "book this",
    "I want two boxes",
    "yes place my order",
    "final selection",
    "confirm my order",
    "place the order",
])
def test_ready_to_order_variants_use_manager_handoff(message):
    text, matched, template_id, images = _ask(message)
    assert matched is False
    assert template_id == "order_next_steps"
    assert "Mr. Guru Dayal Singh" in text
    assert "+91 87965 51990" in text
    assert images is None
