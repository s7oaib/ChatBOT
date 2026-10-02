"""
Somdeb Ji's confirmed answers, 2026-09-08.

These are facts a customer acts on — what is in the food, whether it is safe
for an allergy, what it costs, where to collect it. The client supplied the
wording and requires it sent as written, so these tests pin both the routing
and the facts. A failure here means a customer is being told something the
client did not say.
"""

import json
import uuid

import pytest

from camellia.responder import responder
from camellia.session_memory import session_memory

TEMPLATES = {t["id"]: t for t in responder.qa_data["templates"]}


def reply_to(message):
    user_id = "confirmed_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "whatsapp")
    return responder.match_and_respond(message, user_id=user_id, channel="whatsapp")


@pytest.mark.parametrize("message, template_id", [
    ("are you halal certified", "GUARD_halal_ingredients_oil"),
    ("what oil do you cook in", "GUARD_halal_ingredients_oil"),
    ("is it nut free", "GUARD_allergen_nutfree_glutenfree"),
    ("does it contain nuts", "GUARD_allergen_nutfree_glutenfree"),
    ("my child has a severe nut allergy is it safe", "GUARD_allergen_nutfree_glutenfree"),
    ("how should i store the sondesh", "GUARD_shelf_life_storage"),
    ("can i pay cash on delivery", "GUARD_payment_method"),
    ("where is your kitchen", "GUARD_pickup_address_hours"),
    ("who can i speak to directly", "GUARD_staff_names_manager_contact"),
    ("is the meal subscription available in delhi", "subscription_details"),
    ("what number should i call for pickup directions", "GUARD_pickup_address_hours"),
    ("how do i pre-order a meal", "alacarte_prebooking_steps"),
    # "can i talk to someone" is deliberately excluded here: it now resolves
    # to the direct human-escalation intent (semantic_fallback's
    # _ESCALATION_INTENT_RE) rather than this template, so the customer gets
    # a WhatsApp handoff instead of a text answer — see test_channel_policy.py.
    ("difference between aamar mishti and signature", "GUARD_collection_differentiation"),
    ("tell me about the subscription", "subscription_details"),
    ("how do i book the subscription", "subscription_booking_steps"),
    ("mutton bento", "bento_details"),
    ("whats the delivery time for meals", "savoury_delivery_time"),
    ("how long to deliver sondesh", "sondesh_dispatch_time"),
    ("do you deliver to mumbai", "delivery_out_of_area"),
])
def test_a_confirmed_question_reaches_its_confirmed_answer(message, template_id):
    text, was_matched, matched_id, _ = reply_to(message)

    from camellia.approved_response_alignment import HANDOFF_ONLY_TEMPLATE_IDS, effective_binding
    if template_id in HANDOFF_ONLY_TEMPLATE_IDS and not effective_binding(template_id)[0]:
        assert was_matched is False
        assert "Manager of Kitchens" in text
        assert "₹" not in text
    else:
        assert was_matched is True, f"{message!r} fell through to a human"
    assert matched_id == template_id
    assert text.strip()


class TestTheFactsThemselves:
    """What the client actually said, as opposed to which template answered."""

    @pytest.mark.parametrize("template_id, must_contain", [
        ("GUARD_halal_ingredients_oil", "mutton is halal"),
        ("GUARD_halal_ingredients_oil", "farmer who grows mustard"),
        ("GUARD_allergen_nutfree_glutenfree", "no nuts"),
        ("GUARD_shelf_life_storage", "12 seconds"),
        ("GUARD_payment_method", "UPI at 9625083171"),
        ("GUARD_payment_method", "Yummraj Culinary Expeditions Pvt Ltd"),
        ("GUARD_payment_method", "HDFC0000572"),
        ("GUARD_payment_method", "https://razorpay.me/@camelliaartisanal"),
        ("GUARD_pickup_address_hours", "E144"),
        ("GUARD_pickup_address_hours", "+91 87965 51990"),
        ("GUARD_staff_names_manager_contact", "Guru Dayal Singh"),
        ("GUARD_staff_names_manager_contact", "+91 87965 51990"),
        ("GUARD_staff_names_manager_contact", "camellia.artisanal.gurgaon@gmail.com"),
        ("bento_details", "₹350"),
        ("bento_details", "₹400"),
        ("bento_details", "₹600"),
        ("subscription_details", "Price of 5 / 10 / 30 MEALS"),
        ("subscription_details", "Sushant Lok Gurgaon kitchen"),
        ("alacarte_prebooking_steps", "+91 87965 51990"),
        ("savoury_food_intro", "No minimum order"),
        ("sondesh_dispatch_time", "within 15 minutes"),
        ("savoury_delivery_time", "within 45 minutes"),
        ("kolkata_expansion_info", "camellia.artisanal.gurgaon@gmail.com"),
    ])
    def test_the_confirmed_fact_is_present(self, template_id, must_contain):
        assert must_contain in TEMPLATES[template_id]["response"]

    def test_the_manager_email_is_no_longer_described_as_conflicted(self):
        # It was pending client confirmation; the client supplied it.
        answer = TEMPLATES["GUARD_staff_names_manager_contact"]["response"]
        assert "CONFLICTED" not in answer
        assert "pending client confirmation" not in answer


class TestNothingIsClaimedThatTheClientDidNotSay:
    def test_the_mustard_farmer_is_not_placed_in_bengal(self):
        # The client said "a farmer who grows mustard" and nothing about where.
        assert "farmer in Bengal" not in json.dumps(responder.qa_data, ensure_ascii=False)

    @pytest.mark.parametrize("absent_flavour", ["Thandai", "Medjool Dates"])
    def test_flavours_absent_from_the_menu_card_are_not_offered(self, absent_flavour):
        # The client's April 2026 Signature card prices the ₹700 tier as
        # "Nolen Gur / Chocolate". Offering a flavour the kitchen may no longer
        # make is worse than omitting one it does.
        assert absent_flavour not in json.dumps(responder.qa_data, ensure_ascii=False)

    def test_the_signature_card_prices_are_exactly_as_printed(self):
        answer = TEMPLATES["signature_collection"]["response"]
        for price in ["₹600", "₹1100", "₹2000", "₹650", "₹1200", "₹2200",
                      "₹700", "₹1300", "₹2500", "₹900", "₹1600", "₹2900"]:
            assert price in answer

    def test_flavour_mixing_still_goes_to_a_human(self):
        # The one question in the client's list they did not answer. It must
        # keep escalating rather than being guessed at.
        assert TEMPLATES["GUARD_customization_flavour_mixing"].get("escalate") is True
        text, matched, template_id, _ = reply_to("can i mix flavours in one box")
        assert matched is False
        assert template_id is None
        assert "Manager of Kitchens" in text


def test_the_seed_version_was_bumped_so_the_edits_actually_ship():
    # Repo template edits reach customers only when seed_version increases;
    # /health flags the mismatch when it does not.
    assert responder.qa_data.get("seed_version", 0) >= 8
