"""Phase 3A Golden Response routing and media guards."""

import uuid
from unittest.mock import patch

import pytest

from camellia.config import config
from camellia.responder import responder
from camellia.session_memory import session_memory


CHART_IMAGE = "/images/Everyday%20Meal%20Subscription%20Rate%20Chart.jpeg"


def _normalize(text):
    return " ".join(str(text).split())


PAYMENT_CANONICAL = (
    "You may please pay through UPI at 9625083171 OR bank transfer to "
    "Yummraj Culinary Expeditions Pvt Ltd Account number- 50200077136337 "
    "IFSC - HDFC0000572 Account type - Current OR credit card at "
    "https://razorpay.me/@camelliaartisanal"
)
SUBSCRIPTION_CANONICAL = (
    "Hi! Thank you for asking about our Everyday Meal Subscription. Here is the new "
    "subscription pricing Freshly cooked, home-style balanced food, using good-quality "
    "ingredients, home recipes and no shortcuts. Option 1 Vegetarian (Rice/roti + 2 "
    "sabzis) • Option 2 Veg + Chicken — (vegetarian on 3 days, chicken on 2 days) • "
    "Option 3 Chicken — (Rice/roti + 1 sabzi + chicken) Price of 5 / 10 / 30 MEALS "
    "(including GST) is attached in the chart below. Porter delivery charges at actuals, "
    "depending on fare from our Sushant Lok Gurgaon kitchen to your location Simple, "
    "varied everyday food — as close as we can make it to a meal from a home kitchen. "
    "You may also connect directly with Our Manager of Kitchens, Mr. Guru Dayal Singh "
    "(8796551990), on a call or WhatsApp . He will help you in the purchase process also "
    "and share your invoice. He will be your single point of contact for this order and "
    "will be happy to assist you with any queries."
)
HOME_TABLE_DETAILS_CANONICAL = (
    "Hi! Thank you for connecting. Sharing details of the Camellia.artisanal Bengali "
    "Home Table, Kolkata. 4th & 11th October 2026 | 1–6 pm Diamond Park, Joka, Kolkata "
    "Think of the lunch as a Bengali omakase — you don't choose the menu; the season "
    "does. An elaborate vegetarian & non-vegetarian Bengali home lunch, slow-cooked using "
    "traditional methods, followed by several kinds of mishti made by us, a Darjeeling "
    "tea ceremony and a sit-down storytelling session. Story & lunch theme 4th October — "
    "MALE FOLK DEITIES OF BENGAL 11th October — DURGA PUJO Next steps to book a seat "
    "Please let me know how many guests Which of the two dates would you prefer Sharing 4 "
    "videos for you to take a look at details & also a bit of videos from the 100 Bengali "
    "home tables we did at gurgaon. If this interests you, please let me know. I will "
    "share the pricing & other details."
)
HOME_TABLE_PRICING_CANONICAL = (
    "This is a meal that unfolds slowly, one course and one story at a time. It is a "
    "leisurely Bengali experience where each dish has its own place in the sequence—"
    "allowing you to experience Bengal through its flavours, histories, rituals and "
    "memories. The Vegetarian Experience includes 8 individually served dishes. The "
    "Non-Vegetarian Experience includes 9 individually served dishes. Both conclude with "
    "a seasonal pre-dessert, followed by three distinct desserts made entirely in our "
    "kitchen—including our signature norom-pak Jolbhora Sondesh. Vegetarian Experience — "
    "₹1,500 per person Non-Vegetarian Experience — ₹2,500 per person Prices include GST. "
    "Please let me know if you decide to book. I will share the payment details."
)
HOME_TABLE_PAYMENT_CANONICAL = (
    "A seat is booked on full payment of the total amount. You can transfer by UPI to "
    "9625083171 Or bank transfer to Yummraj Culinary Expeditions Pvt Ltd Account number- "
    "50200077136337 IFSC - HDFC0000572 Account type - Current OR credit card at "
    "https://razorpay.me/@camelliaartisanal We will send a GST invoice as proof of "
    "payment & confirmation of your seat"
)
HANDOFF_CANONICAL = (
    "Our Manager of Kitchens, Mr. Guru Dayal Singh (+918796551990), will connect with "
    "you shortly and share your invoice. He will be your single point of contact for this "
    "order and will be happy to assist you with any queries."
)


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
    assert _normalize(text) == _normalize(PAYMENT_CANONICAL)
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
    assert _normalize(text) == _normalize(PAYMENT_CANONICAL)


@pytest.mark.parametrize("message", [
    "What is the price of SUBSCRIPTION - HOME FOOD AWAY FROM HOME",
    "Tell me more about SUBSCRIPTION - HOME FOOD AWAY FROM HOME",
])
def test_subscription_canonical_questions_attach_the_supplied_rate_chart(message):
    text, matched, template_id, images = _ask(message)
    assert matched is True
    assert template_id == "subscription_details"
    assert _normalize(text) == _normalize(SUBSCRIPTION_CANONICAL)
    assert "ATTACH RATE CHART" not in text
    assert isinstance(images, list)
    assert images == [CHART_IMAGE]


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
    assert _normalize(text) == _normalize(SUBSCRIPTION_CANONICAL)
    assert images == [CHART_IMAGE]


@pytest.mark.parametrize("message", [
    "subscrption pric pls",
    "Tell me more about the meal plan",
])
def test_subscription_typo_and_short_forms_use_exact_canonical_copy(message):
    text, matched, template_id, images = _ask(message)
    assert matched is True
    assert template_id == "subscription_details"
    assert _normalize(text) == _normalize(SUBSCRIPTION_CANONICAL)
    assert images == [CHART_IMAGE]


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
    assert _normalize(text) == _normalize(HOME_TABLE_DETAILS_CANONICAL)
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
    assert _normalize(text) == _normalize(HOME_TABLE_PRICING_CANONICAL)
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
    assert _normalize(text) == _normalize(HOME_TABLE_PAYMENT_CANONICAL)
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


def test_unregistered_jolbhora_price_overrides_stale_context_but_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = _user("instagram_home_table_switch")
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts.update({
        "home_table_context": True,
        "last_topic": "bengali_home_table",
        "source_route": {
            "active": True,
            "routing_topic": "Bengali Home Table",
            "source_type": "story",
        },
    })

    text, matched, template_id, _images = responder.match_and_respond(
        "What is the price of Jolbhora?",
        user_id=user_id,
        channel="instagram",
    )

    assert matched is False
    assert template_id is None
    assert "Manager of Kitchens will connect" in text
    assert facts["last_route_source"] == "HUMAN_HANDOFF"
    assert facts["source_context_used"] is False


def test_unsupported_new_request_overrides_stale_home_table_context_on_instagram(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = _user("instagram_home_table_unsupported")
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts.update({
        "home_table_context": True,
        "last_topic": "bengali_home_table",
        "source_route": {
            "active": True,
            "routing_topic": "Bengali Home Table",
            "source_type": "story",
        },
    })

    _text, _matched, template_id, _images = responder.match_and_respond(
        "Can you cater a wedding in Mumbai?",
        user_id=user_id,
        channel="instagram",
    )

    assert template_id != "bengali_home_table_details"
    assert template_id != "bengali_home_table_pricing"


def test_explicit_bento_switch_preserves_bento_context_for_bare_price():
    from datetime import date
    user_id = _user("bento_switch")
    session_memory.clear_session(user_id, "whatsapp")
    with patch("camellia.menu_dates.today_ist", return_value=date(2026, 6, 21)):
        _text, matched, template_id, _images = responder.match_and_respond(
        "Actually I want Bengali Bento", user_id=user_id, channel="whatsapp"
        )
    assert matched is True
    assert template_id == "bento_details"

    with patch("camellia.menu_dates.today_ist", return_value=date(2026, 6, 21)):
        text, matched, template_id, images = responder.match_and_respond(
            "price?", user_id=user_id, channel="whatsapp"
        )
    assert matched is True
    assert template_id == "bento_details"
    assert "₹350" in text
    assert "₹400" in text
    assert "₹600" in text
    assert images is not None


@pytest.mark.parametrize("message", [
    "bengal home tabel kolkata",
])
def test_home_table_typo_and_short_forms_use_exact_introduction(message):
    text, matched, template_id, images = _ask(message)
    assert matched is True
    assert template_id == "bengali_home_table_details"
    assert _normalize(text) == _normalize(HOME_TABLE_DETAILS_CANONICAL)
    assert images is None


@pytest.mark.parametrize("message", [
    "How much per person for the Bengali home lunch?",
    "home tabel prise kolkatta",
    "home table price",
    "cost per head for home table",
])
def test_home_table_pricing_natural_language_and_typo_variants_use_exact_pricing(message):
    """A clear price/cost/per-person/per-head question inside the confirmed
    Home Table domain must win over the general introduction — these used to
    fall back to the intro template even though they are unambiguously
    asking for the numbers."""
    text, matched, template_id, images = _ask(message)
    assert matched is True
    assert template_id == "bengali_home_table_pricing"
    assert _normalize(text) == _normalize(HOME_TABLE_PRICING_CANONICAL)
    assert images is None


def test_kolkata_move_question_keeps_expansion_route():
    text, matched, template_id, images = _ask(
        "who runs the kitchen now that you're moving to kolkata"
    )
    assert matched is True
    assert template_id == "kolkata_expansion_info"
    assert images is None


@pytest.mark.parametrize("message", [
    "I'm ready to place the order",
    "Let's confirm",
    "finalise it",
    "book this",
    "Can we lock this in?",
    "Go ahead and book it",
    "order now",
    "i wnt 2 ordr pls",
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
    assert _normalize(text) == _normalize(HANDOFF_CANONICAL)
    assert images is None


# The tests below use channel="instagram" deliberately. Every other test in
# this file uses "whatsapp" (via `_ask`), which never exercises the
# this/it source-reference clarify gate at all -- that gate only runs when
# channel == "instagram" (see camellia/responder.py). Instagram is also the
# only channel any of these golden responses are ever actually served on
# (WhatsApp is human-only). A prior remediation round added "book this" /
# "finalise it" to ORDER_READY_RE and still failed in production, because
# the clarify gate intercepted those exact phrases (their bare "this"/"it")
# before ORDER_READY_RE ever ran -- a whatsapp-channel test could not have
# caught that. These tests exercise the real channel so the same regression
# cannot resurface unnoticed.
def _ask_instagram(message, user_id=None):
    user_id = user_id or _user("instagram")
    session_memory.clear_session(user_id, "instagram")
    return responder.match_and_respond(message, user_id=user_id, channel="instagram")


@pytest.mark.parametrize("message", [
    "I'm ready to place the order",
    "Let's confirm",
    "finalise it",
    "book this",
    "Can we lock this in?",
    "Go ahead and book it",
    "order now",
    "i wnt 2 ordr pls",
    "I want two boxes",
    "yes place my order",
    "final selection",
    "confirm my order",
    "place the order",
])
def test_ready_to_order_variants_use_manager_handoff_on_instagram(message, monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    text, matched, template_id, images = _ask_instagram(message)
    assert matched is False
    assert template_id == "order_next_steps"
    assert _normalize(text) == _normalize(HANDOFF_CANONICAL)
    assert images is None


@pytest.mark.parametrize("message", [
    "what is this?",
    "tell me about this",
    "is this available?",
    "what does it contain?",
])
def test_bare_this_or_it_questions_do_not_trigger_order_handoff_on_instagram(message, monkeypatch):
    """A pronoun alone must not become a ready-to-order match merely because
    it contains "this"/"it" -- these are genuinely ambiguous references and
    must still get the disambiguation prompt, not the Manager handoff."""
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    _text, _matched, template_id, _images = _ask_instagram(message)
    assert template_id != "order_next_steps"


@pytest.mark.parametrize("message", [
    "What's the best time to book?",
    "WHAT ARE THE NEXT STEPS TO PRE BOOK?",
])
def test_booking_related_questions_are_not_hijacked_by_ready_to_order_on_instagram(message, monkeypatch):
    """Moving the ready-to-order check earlier must not make it swallow
    unrelated questions that happen to contain "book"."""
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    _text, _matched, template_id, _images = _ask_instagram(message)
    assert template_id != "order_next_steps"


@pytest.mark.parametrize("message", [
    "who runs the kitchen now that you're moving to kolkata",
    "Are you moving to Kolkata?",
    "What happens to the Gurgaon kitchen if you expand to Kolkata?",
    "Who runs the kitchen now that you're moving to Kolkata?",
])
def test_unregistered_kolkata_questions_handoff_instead_of_reusing_stale_source(message, monkeypatch):
    """"now THAT you're moving" contains the bare word "that", which the
    this/it source-reference clarify gate matched with no grammatical
    awareness -- diverting a legitimate Kolkata-expansion question into a
    false "which item?" prompt. The old template is not in the Instagram
    client-approved registry, so the current contract is controlled handoff
    rather than legacy copy. No stale source description may be replayed."""
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = _user("instagram_kolkata_unregistered")
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts.update({
        "last_topic": "bengali_home_table",
        "home_table_context": True,
        "source_route": {
            "active": True,
            "routing_topic": "Bengali Home Table",
            "source_type": "story",
            "source_description": "stale campaign description",
        },
    })

    text, matched, template_id, images = responder.match_and_respond(
        message, user_id=user_id, channel="instagram",
    )

    assert matched is False
    assert template_id is None
    assert "Manager of Kitchens will connect" in text
    assert "stale campaign description" not in text
    assert images is None
    assert facts["last_route_source"] == "HUMAN_HANDOFF"
    assert facts["last_grounding"] == "client_registry_miss"
    assert facts["source_context_used"] is False
