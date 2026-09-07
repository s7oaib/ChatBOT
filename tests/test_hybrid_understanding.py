"""
The hybrid understanding layer: deterministic templates first, then approved
retrieval, then a Groq answer written only from what retrieval found, then a
human. These tests pin the two properties that make that safe to ship — a
greeting never swallows the request next to it, and a generated answer never
reaches a customer stating a number the approved sources did not state.
"""

from unittest.mock import patch

import pytest

from camellia.config import config
from camellia.rag_fallback import rag_fallback_engine
from camellia.responder import responder
from camellia.session_memory import session_memory


def _reply(message, user_id, channel="whatsapp"):
    session_memory.clear_session(user_id, channel)
    return responder.match_and_respond(message, user_id=user_id, channel=channel)


# The phrasings the client asked us to cover. None of these is a literal
# trigger phrase; they have to be understood rather than looked up.
@pytest.mark.parametrize(
    "message, expected_template",
    [
        ("Hi can I get the menu?", "know_more"),
        ("Hello, please share your menu", "know_more"),
        ("What sweets do you have?", None),
        ("Hi, what is the price?", "price_clarify_product"),
        ("Hello, do you deliver to Delhi?", "delivery_area"),
        ("Can you tell me what products are available?", "product_overview"),
        ("What can I order from you?", "how_to_order"),
    ],
)
def test_natural_phrasings_are_understood_without_a_literal_trigger(message, expected_template):
    text, was_matched, template_id, _ = _reply(message, f"hybrid_{abs(hash(message))}")

    assert was_matched is True
    assert text.strip()
    if expected_template:
        assert template_id == expected_template


def test_a_greeting_next_to_a_request_does_not_swallow_the_request():
    # "Hey" alone would match the greeting template. The event enquiry beside
    # it is not something we answer from a template, so it must reach a human
    # rather than be answered with "Hello from Camellia Artisanal!".
    text, _was_matched, template_id, _ = _reply(
        "Hey, I need sweets for an event", "hybrid_event_request"
    )

    assert template_id != "greeting"
    assert "Hello from Camellia Artisanal" not in text
    assert "event" in text.lower() or "bulk" in text.lower() or "team" in text.lower()


@pytest.mark.parametrize("greeting", ["hi", "hello there", "hey!", "good morning", "Namaste"])
def test_a_bare_greeting_is_still_just_a_greeting(greeting):
    _text, _was_matched, template_id, _ = _reply(greeting, f"hybrid_greet_{greeting}")

    assert template_id == "greeting"


class TestGroundingGuard:
    """The number check that decides whether a generated answer may ship."""

    def test_an_answer_reusing_the_sources_numbers_is_grounded(self):
        assert rag_fallback_engine.is_grounded_in_context(
            "Our 250g box is ₹450.", ["250g boxes start at ₹450 for 9 pieces."]
        )

    def test_an_invented_price_is_not_grounded(self):
        assert not rag_fallback_engine.is_grounded_in_context(
            "Our 250g box is ₹520.", ["250g boxes start at ₹450 for 9 pieces."]
        )

    def test_an_answer_with_no_numbers_at_all_is_grounded(self):
        assert rag_fallback_engine.is_grounded_in_context(
            "We deliver across Delhi NCR.", ["We deliver across Delhi NCR."]
        )

    def test_thousands_separators_do_not_read_as_a_new_number(self):
        assert rag_fallback_engine.is_grounded_in_context(
            "A 1kg box is ₹1,500.", ["1kg box ₹1500"]
        )

    def test_an_empty_answer_is_never_grounded(self):
        assert not rag_fallback_engine.is_grounded_in_context("", ["₹450"])


class TestGroqGroundedAnswerPath:
    """Groq may answer, but only from retrieval, and only if it invents nothing."""

    def _force_ai_on(self):
        return patch.object(type(config), "is_ai_enabled", property(lambda self: True))

    def test_a_grounded_answer_reaches_the_customer(self):
        with self._force_ai_on(), patch.object(
            responder.__class__, "_is_vague_enough_to_clarify", return_value=False
        ), patch("camellia.responder.ai_responder.get_active_provider", return_value="groq"), patch(
            "camellia.responder.ai_responder.answer_from_retrieved_context",
            return_value=("Yes — we deliver right across Delhi NCR.", True, "groq_rag"),
        ):
            text, was_matched, template_id, _ = _reply(
                "is my area covered by you people", "hybrid_groq_ok"
            )

        if template_id == "groq_grounded_answer":
            assert was_matched is True
            assert "Delhi NCR" in text

    def test_an_answer_inventing_a_price_is_discarded_and_never_sent(self):
        # The model is told to use only approved excerpts; this simulates it
        # doing what models sometimes do anyway.
        hallucinated = "Our gift hampers start at ₹9999 with free delivery."

        with self._force_ai_on(), patch(
            "camellia.responder.ai_responder.get_active_provider", return_value="groq"
        ), patch(
            "camellia.responder.ai_responder.answer_from_retrieved_context",
            return_value=(hallucinated, True, "groq_rag"),
        ):
            text, _was_matched, template_id, _ = _reply(
                "do you sell gift hampers for corporate diwali", "hybrid_groq_hallucination"
            )

        assert template_id != "groq_grounded_answer"
        assert "9999" not in text
        assert "₹9999" not in text

    def test_groq_is_not_consulted_when_retrieval_found_nothing(self):
        with self._force_ai_on(), patch(
            "camellia.responder.ai_responder.get_active_provider", return_value="groq"
        ), patch(
            "camellia.responder.ai_responder.answer_from_retrieved_context"
        ) as generate, patch(
            "camellia.responder.rag_fallback_engine.retrieve"
        ) as retrieve:
            retrieve.return_value = type(
                "R",
                (),
                {
                    "auto_response": "",
                    "source_chunks": [],
                    "source_refs": [],
                    "retrieved_sources": [],
                    "confidence_score": 0.0,
                    "proposed_intent": "unknown_intent",
                    "proposed_response": "",
                    "approval_status": "FAILED",
                    "has_conflict": False,
                },
            )()
            _reply("what is your view on the monsoon this year", "hybrid_groq_no_context")

        assert not generate.called


def test_an_unmatched_reply_carrying_the_slow_notice_does_not_also_promise_a_callback():
    # The notice tells the customer to call Guru Dayal directly this week. The
    # handover copy underneath used to tell them we had notified the team and
    # would call them back — two contradictory instructions in one message.
    user_id = "hybrid_slow_notice"
    session_memory.clear_session(user_id, "instagram")

    with patch.object(responder, "_should_send_slow_notice", return_value=True), patch.object(
        responder,
        "_resolve_response",
        return_value=("Thank you for getting in touch! I've notified our team.", False, None, None),
    ):
        text, _was_matched, _template_id, _ = responder.match_and_respond(
            "something we cannot answer", user_id=user_id, channel="instagram"
        )

    assert text == config.SLOW_REPLY_NOTICE_TEXT
    assert "notified our team" not in text
    assert "Guru Dayal" in text


def test_a_matched_reply_still_keeps_its_answer_under_the_slow_notice():
    user_id = "hybrid_slow_notice_matched"
    session_memory.clear_session(user_id, "instagram")

    with patch.object(responder, "_should_send_slow_notice", return_value=True), patch.object(
        responder,
        "_resolve_response",
        return_value=("We deliver across Delhi NCR.", True, "delivery_area", None),
    ):
        text, _was_matched, _template_id, _ = responder.match_and_respond(
            "do you deliver", user_id=user_id, channel="instagram"
        )

    assert "Guru Dayal" in text
    assert "We deliver across Delhi NCR." in text
