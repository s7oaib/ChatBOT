"""
Delivery-time and Kolkata questions were matching on raw keyword overlap
against templates that share words with a completely different answer:

  "when will my lunch arrive"        -> was answered with Sondesh's 15-minute
                                         dispatch time, because delivery_time
                                         and sondesh_dispatch_time carry the
                                         broadest "how long / how soon" triggers
  "is gurgaon kitchen closing"       -> was answered with delivery_area's
                                         "which area are you in?", because
                                         "gurgaon" is a delivery_area trigger
  "do you deliver pan india"         -> same: no named city, so
                                         delivery_out_of_area's own triggers
                                         never fired and delivery_area's
                                         generic reply won instead

Each is disambiguated by context before trigger scoring runs, not by adding
more overlapping trigger phrases to the templates themselves.
"""

import uuid

import pytest

from camellia.responder import responder
from camellia.session_memory import session_memory


def reply_to(message):
    user_id = "disambig_" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "whatsapp")
    return responder.match_and_respond(message, user_id=user_id, channel="whatsapp")


class TestSavouryVsSondeshDeliveryTime:
    @pytest.mark.parametrize("message", [
        "whats the delivery time for savoury food",
        "when will my lunch arrive",
        "when will my dinner arrive",
        "how much time for meals",
        "how long does bento take",
        "how soon can i get my meal",
        "delivery time for bento",
        "how long does the bengali meal take",
        "eta for my meal",
        "how fast is food delivery",
    ])
    def test_a_food_context_time_question_gets_the_45_minute_answer(self, message):
        text, matched, template_id, _ = reply_to(message)
        assert matched is True
        assert template_id == "savoury_delivery_time"
        assert "45 minutes" in text
        assert "15 minutes" not in text

    @pytest.mark.parametrize("message", [
        "how long to deliver sondesh",
        "how fast can you dispatch sondesh",
        "when will my sondesh arrive",
        "how soon can i get my mishti",
    ])
    def test_a_sondesh_context_time_question_keeps_its_own_answer(self, message):
        text, matched, template_id, _ = reply_to(message)
        assert matched is True
        assert template_id == "sondesh_dispatch_time"
        assert "15 minutes" in text

    def test_a_context_free_time_question_is_unaffected(self):
        # The Phase 3B client-confirmed rule treats a context-free delivery
        # question as savoury delivery timing rather than the broader
        # Sondesh/general delivery template.
        text, matched, template_id, _ = reply_to("how long does delivery take")
        assert matched is True
        assert template_id == "savoury_delivery_time"
        assert "45 minutes" in text


class TestKolkataDisambiguation:
    @pytest.mark.parametrize("message", [
        "what happens after you leave for kolkata",
        "are you moving to kolkata",
        "is somdeb moving to kolkata",
        "will quality change after kolkata move",
        "who will run the kitchen after you move",
        "are you shutting down gurgaon",
        "is gurgaon kitchen closing",
        "what about after founders leave",
        "are you relocating",
        "is the kitchen moving",
        "will the recipes change after you move",
    ])
    def test_a_relocation_question_reaches_the_reassurance_answer(self, message):
        text, matched, template_id, _ = reply_to(message)
        assert matched is True
        assert template_id == "kolkata_expansion_info"
        assert "Guru Dayal Singh" in text

    @pytest.mark.parametrize("message, expected_template", [
        ("who is the founder", "GUARD_staff_names_manager_contact"),
        ("where is your kitchen", "GUARD_pickup_address_hours"),
        ("do you deliver to gurgaon", "delivery_area"),
    ])
    def test_the_same_words_without_a_relocation_verb_still_answer_their_own_question(
        self, message, expected_template
    ):
        # "kitchen", "gurgaon" and "founder" are legitimate words in their own
        # right — the disambiguator only fires alongside a move/leave/close verb.
        _text, matched, template_id, _ = reply_to(message)
        assert matched is True
        assert template_id == expected_template


class TestOutOfAreaWithoutANamedCity:
    @pytest.mark.parametrize("message", [
        "do you deliver pan india",
        "do you ship internationally",
        "deliver outside india",
        "ship to another state",
        "do you deliver all over india",
        "is this available worldwide",
    ])
    def test_a_generic_beyond_ncr_question_gets_the_approved_refusal(self, message):
        text, matched, template_id, _ = reply_to(message)
        assert matched is True
        assert template_id == "delivery_out_of_area"
        assert "unable to ship our sondesh outside Delhi NCR" in text

    @pytest.mark.parametrize("message", [
        "do you deliver outside delhi ncr",
        "where do you deliver",
        "do you deliver to noida",
    ])
    def test_ncr_zone_questions_are_unaffected(self, message):
        _text, matched, template_id, _ = reply_to(message)
        assert matched is True
        assert template_id == "delivery_area"


class TestTheSavourySondeshCrosssellUsesTheClientsOwnText:
    def test_the_nudge_is_the_crosssell_templates_own_wording(self):
        text, matched, template_id, images = reply_to("do you have lunch or dinner")
        assert matched is True
        assert template_id == "savoury_food_intro"
        assert "We also make & send Bengali mishti - norom pak Sondesh" in text

    def test_a_sondesh_menu_card_is_attached_alongside_the_weekly_menu(self):
        _text, _matched, _template_id, images = reply_to("do you have lunch or dinner")
        assert isinstance(images, list)
        urls = [img.get("url") if isinstance(img, dict) else img for img in images]
        assert sum("/images/Sondesh%20" in url for url in urls) == 3
        assert isinstance(images[0], dict) and images[0].get("text_first") is True

    def test_the_nudge_is_sent_once_per_session_not_on_every_savoury_reply(self):
        user_id = "crosssell_once_" + uuid.uuid4().hex[:8]
        session_memory.clear_session(user_id, "whatsapp")

        first = responder.match_and_respond("do you have lunch or dinner", user_id=user_id, channel="whatsapp")
        second = responder.match_and_respond("tell me about the subscription", user_id=user_id, channel="whatsapp")

        assert "We also make & send Bengali mishti" in first[0]
        assert "We also make & send Bengali mishti" not in second[0]

    @pytest.mark.parametrize("message, expected_template", [
        ("what are the bento meal box prices", "bento_details"),
        ("when should i book savoury food", "savoury_best_time_to_book"),
        ("how long does savoury food take", "savoury_delivery_time"),
    ])
    def test_every_direct_savoury_information_path_gets_the_one_time_offer(
        self, message, expected_template
    ):
        text, matched, template_id, images = reply_to(message)
        assert matched is True
        assert template_id == expected_template
        assert "WE ALSO OFFER SONDESH" in text
        urls = [img.get("url") if isinstance(img, dict) else img for img in images]
        assert sum("/images/Sondesh%20" in url for url in urls) == 3
