"""A domain match must never imply a fact the approved copy never carried.

Packaging exists, so "do you gift wrap?" used to reach the packaging answer —
food-safe cornstarch boxes — which says nothing about wrapping. Delivery
exists, so "can you guarantee 30 minutes?" reached the coverage answer.
Subscriptions exist, so "can I cancel anytime?" reached the plans.

Sufficiency is therefore checked at the level of the thing being asked about,
not only the domain, and the evidence is the approved copy itself: a subtopic
counts as covered when the copy mentions it, and not because it belongs to a
domain we happen to answer.
"""

import uuid
from unittest.mock import patch

import pytest

from camellia.ai_responder import ai_responder
from camellia.config import config
from camellia.knowledge import approved_knowledge
from camellia.rag_fallback import rag_fallback_engine
from camellia.responder import responder
from camellia.semantic_fallback import read
from camellia.session_memory import session_memory
from camellia.understanding import UnderstandingResult, understanding_engine


def _fresh(prefix):
    user_id = f"{prefix}_{uuid.uuid4().hex[:8]}"
    session_memory.clear_session(user_id, "whatsapp")
    return user_id


def _outage_reply(message, user_id):
    def unavailable(*args, **kwargs):
        return UnderstandingResult(reason="provider_unavailable")

    with patch.object(understanding_engine, "analyze", side_effect=unavailable), patch(
        "camellia.responder.ai_responder.compose_verified_response",
        return_value=("", False, "provider_unavailable"),
    ):
        return responder.match_and_respond(message, user_id=user_id, channel="whatsapp")


class TestASubtopicIsNotImpliedByItsDomain:
    @pytest.mark.parametrize("message", [
        "Do you gift wrap?",
        "Do you provide birthday wrapping?",
        "Can you decorate the box?",
        "Do you do custom boxes?",
        "Can you guarantee delivery in 30 minutes?",
        "Can I cancel my subscription anytime?",
    ])
    def test_an_unmentioned_subtopic_is_a_gap_not_the_domain_answer(self, message):
        user_id = _fresh("subtopic")
        text, matched, _template_id, _images = _outage_reply(message, user_id)
        facts = session_memory.get_facts(user_id, "whatsapp")

        assert matched is False, "an unsupported subtopic must not be answered"
        assert facts["last_knowledge_sufficiency"]
        assert not any(facts["last_knowledge_sufficiency"].values())
        assert "cornstarch" not in text.lower()
        assert "team" in text.lower()

    @pytest.mark.parametrize("message, expected_template", [
        # These subtopics ARE in the approved copy, so they stay answerable.
        ("What are the delivery charges?", "delivery_area"),
        ("How do you pack the sweets?", "packaging"),
        ("What if I need to change a delivery date?", "subscription_booking_steps"),
    ])
    def test_a_subtopic_the_copy_does_mention_is_still_answered(
        self, message, expected_template,
    ):
        user_id = _fresh("subtopic_ok")
        if expected_template == "subscription_booking_steps":
            _outage_reply("tell me about the subscription", user_id)
        _text, matched, template_id, _images = _outage_reply(message, user_id)

        assert matched is True
        assert template_id == expected_template

    def test_the_evidence_is_the_approved_copy_itself(self):
        """Change the copy and the answer changes with it — no second list."""
        assert approved_knowledge._copy_covers_subtopic(
            "gift_wrap", "Every box can be gift wrapped with a ribbon.",
        ) is True
        assert approved_knowledge._copy_covers_subtopic(
            "gift_wrap", "All our food is packed in food-safe cornstarch boxes.",
        ) is False


class TestPartialFulfilmentSurvivesSubtopicGaps:
    def test_a_verified_price_ships_while_gift_wrap_is_escalated(self):
        user_id = _fresh("partial_wrap")
        text, matched, _template_id, _images = _outage_reply(
            "What's Jolbhora price and do you gift wrap?", user_id,
        )
        facts = session_memory.get_facts(user_id, "whatsapp")

        assert facts["last_knowledge_sufficiency"] == {
            "product_price": True, "packaging_question": False,
        }
        assert "₹750" in text          # the approved Jolbhora price still ships
        assert "wrap" not in text.lower().split("team")[0] or "team" in text.lower()
        assert matched is False        # the unsupported half reaches a person


@pytest.mark.parametrize("message", [
    "Do you have toilets?",
    "Can I charge my phone?",
    "Can pets come inside?",
    "Is there AC?",
    "Can I reserve a table?",
    "Do you have wheelchair access?",
    "Is parking free?",
])
def test_an_unseen_restaurant_topic_is_typed_as_a_business_gap(message):
    """No list of unsupported topics decides this — the absence of a record does."""
    user_id = _fresh("unseen_topic")
    text, matched, _template_id, _images = _outage_reply(message, user_id)
    facts = session_memory.get_facts(user_id, "whatsapp")
    reading = read(message)

    assert "unknown_business_query" in reading["intents"]
    assert matched is False or facts.get("last_route_source") == "state_handler"
    assert "cornstarch" not in text.lower()
    assert "refrigerat" not in text.lower()


@pytest.mark.parametrize("message, forbidden", [
    ("Can I get 40% discount?", ("40%", "discount code")),
    ("Is Jolbhora available right now?", ("available right now", "in stock")),
    ("Can you guarantee delivery in 30 minutes?", ("guarantee", "30 minutes")),
])
def test_an_unsupported_promise_is_never_made(message, forbidden):
    user_id = _fresh("promise")
    text, _matched, _template_id, _images = _outage_reply(message, user_id)

    for phrase in forbidden:
        assert phrase.lower() not in text.lower()


class TestRefinementCannotIntroduceFacts:
    """Refinement rewrites approved copy; it may not add anything to it."""

    TEMPLATE = (
        "We deliver right across Delhi NCR — Delhi, Gurgaon, Noida, Ghaziabad "
        "and Faridabad are all covered. Courier charges are on actuals."
    )

    def _refine(self, model_text):
        with patch.object(ai_responder, "get_active_provider", return_value="groq"), patch.object(
            config, "GROQ_ENABLED", True,
        ), patch.object(ai_responder, "_query_groq", return_value=(model_text, True, "groq")):
            ai_responder._refinement_cache.clear()
            return ai_responder.refine_template_response(
                "delivery_area", self.TEMPLATE, "where do you deliver",
                user_context="Customer Name: Unknown",
            )

    @pytest.mark.parametrize("model_text", [
        "We deliver across Delhi NCR and everything is prepared fresh every morning.",
        "We deliver across Delhi NCR, usually within 30 minutes.",
        "We deliver across Delhi NCR for a flat ₹99 courier fee.",
        "We deliver across Delhi NCR and to Mumbai on request.",
        "Hello Shoaib! We deliver across Delhi NCR.",
        "We deliver across Delhi NCR and your sweets are nut free.",
        "We deliver across Delhi NCR — refrigerate on arrival.",
    ])
    def test_a_refinement_that_adds_a_claim_is_discarded(self, model_text):
        text, refined = self._refine(model_text)

        assert refined is False
        assert text == self.TEMPLATE

    def test_a_faithful_rephrasing_is_accepted(self):
        text, refined = self._refine(
            "Yes — we deliver right across Delhi NCR: Delhi, Gurgaon, Noida, "
            "Ghaziabad and Faridabad are all covered."
        )

        assert refined is True
        assert "Delhi NCR" in text

    def test_the_same_guard_rejects_a_generated_answer(self):
        grounded, reason = rag_fallback_engine.grounding_report(
            "We prepare Jolbhora fresh every morning.",
            ["Jolbhora is a Nolen Gur Sondesh with a liquid filling."],
        )

        assert grounded is False
        assert reason.startswith("unsupported_")
