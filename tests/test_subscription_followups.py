"""
The subscription message is followed by a chart and manager contact, so the
customer's next line is usually a bare "yes", "sign me up", or "I'll take option 2". None
of those carry a subscription word, so trigger scoring answered them with:

  "what is included in option 2"     -> the Sondesh Signature Collection,
                                        because "option 2" reads as Collection 2
  "yes i want to book"               -> the generic pre-ordering blurb
  "how do i book"                    -> savoury_best_time_to_book, same blurb
  "can i change the date later"      -> a Sondesh price table, or delivery_area's
                                        "which area are you in?"

The subscription context resolves all four before trigger scoring runs, and the
answers come from the two client-confirmed subscription templates only.
"""

import uuid

import pytest

from camellia.responder import responder
from camellia.session_memory import session_memory


def start_a_subscription_conversation():
    user_id = "sub_followup_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "whatsapp")
    _text, _matched, template_id, _images = responder.match_and_respond(
        "tell me about your everyday meal subscription", user_id=user_id, channel="whatsapp"
    )
    assert template_id == "subscription_details"
    return user_id


def follow_up(user_id, message):
    return responder.match_and_respond(message, user_id=user_id, channel="whatsapp")


class TestTheReplyToWouldYouLikeToBook:
    @pytest.mark.parametrize("message", [
        "yes",
        "yes i want to book",
        "sign me up",
        "how do i book",
        "ok i'll take option 2",
        "what's next",
    ])
    def test_an_intent_to_book_gets_the_four_steps(self, message):
        user_id = start_a_subscription_conversation()
        text, matched, template_id, _images = follow_up(user_id, message)

        assert matched
        assert template_id == "subscription_booking_steps"
        assert "Choose Option 1, 2 or 3" in text

    def test_asking_what_an_option_contains_stays_on_the_meal_plans(self):
        user_id = start_a_subscription_conversation()
        text, _matched, template_id, images = follow_up(user_id, "what is included in option 2")

        assert template_id == "subscription_details"
        assert "Price of 5 / 10 / 30 MEALS" in text
        assert "/images/Everyday%20Meal%20Subscription%20Rate%20Chart.jpeg" in images
        assert "₹1,750" not in text
        assert "luchi" not in text.lower()
        assert "11th" not in text

    def test_how_do_i_pay_keeps_its_own_approved_answer(self):
        """Step 2 only says "Pay" — the payment question has a real answer elsewhere."""
        user_id = start_a_subscription_conversation()
        _text, _matched, template_id, _images = follow_up(user_id, "how do i pay?")

        assert template_id == "GUARD_payment_method"


class TestChangingADeliveryDate:
    @pytest.mark.parametrize("message", [
        "can i change the date later?",
        "what if i need to change a delivery date",
        "i need to reschedule my delivery",
    ])
    def test_the_five_hour_notice_answers_it(self, message):
        user_id = start_a_subscription_conversation()
        follow_up(user_id, "yes book me")  # steps sent, awaiting flag cleared

        text, _matched, template_id, _images = follow_up(user_id, message)

        assert template_id == "subscription_booking_steps"
        assert "at least 5 hours in advance" in text

    def test_a_sondesh_question_is_not_swallowed_by_the_subscription_context(self):
        user_id = start_a_subscription_conversation()
        _text, _matched, template_id, _images = follow_up(
            user_id, "what is the price of nolen gur sondesh"
        )

        assert template_id == "sondesh_nolen_gur"


class TestASondeshConversationIsUnaffected:
    def test_option_2_still_means_collection_2_without_a_subscription_context(self):
        user_id = "sondesh_opt_" + uuid.uuid4().hex[:8]
        session_memory.clear_session(user_id, "whatsapp")
        responder.match_and_respond("what sondesh do you have", user_id=user_id, channel="whatsapp")

        _text, _matched, template_id, _images = follow_up(user_id, "option 2")

        assert template_id == "signature_collection"
