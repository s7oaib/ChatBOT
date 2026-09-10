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


class TestModelSuppliedTopicCannotEstablishTruth:
    """A model's `topic` guess is evidence for a human, never a lookup key.

    No catalogue record is keyed on `topic` — records are keyed on intent and
    on `product`/`location`/`menu_type`/`pack_size`/`subtopic`. So even a
    wrong or adversarial topic value (the model says "packaging" for a
    washroom question) cannot select an answer, because `unknown` and
    `unknown_business_query` have no catalogue entry to select in the first
    place. This test pins that structural guarantee down directly, rather
    than relying on it staying true by accident.
    """

    @pytest.mark.parametrize("fake_topic", [
        "packaging", "delivery", "storage", "menu", "subscriptions", "payment",
    ])
    def test_a_topic_naming_a_real_domain_still_resolves_to_nothing(self, fake_topic):
        for intent in ("unknown", "unknown_business_query"):
            understood = UnderstandingResult(
                intents=[intent], entities={"topic": fake_topic}, confidence=0.95,
                available=True, reason="test",
            )
            resolution = approved_knowledge.resolve(
                understood, responder.qa_data.get("templates", []),
            )

            assert resolution.items == [], (
                f"topic={fake_topic!r} let {intent} resolve to an answer"
            )
            assert resolution.sufficient_by_intent == {intent: False}

    def test_no_catalogue_record_is_keyed_on_the_unknown_intents(self):
        from camellia.knowledge import CATALOGUE
        from camellia.understanding import Intent

        keyed_intents = {value for record in CATALOGUE for value in record.intents}
        assert Intent.UNKNOWN.value not in keyed_intents
        assert Intent.BUSINESS_POLICY_UNKNOWN.value not in keyed_intents


class TestModelEntitiesAreNormalizedAtTheBoundary:
    """A real live-Groq run surfaced this: the model returned menu_type="veg",
    not the "vegetarian" the catalogue matcher expects, and every record for
    menu_request silently failed to match — the customer got the generic
    "Sondesh or Meals?" prompt instead of the vegetarian menu they asked for.

    The fix normalizes at the boundary so the vocabulary a raw model value
    uses can never silently defeat every matcher — this is the model-output
    cannot establish untrusted structure through raw strings.
    """

    @pytest.mark.parametrize("raw_value, expected", [
        ("veg", "vegetarian"), ("Veg", "vegetarian"), ("vegetarian", "vegetarian"),
        ("VEGETARIAN", "vegetarian"), ("niramish", "vegetarian"),
        ("non veg", "non_vegetarian"), ("non-veg", "non_vegetarian"),
        ("nonveg", "non_vegetarian"), ("Non Vegetarian", "non_vegetarian"),
        ("chicken", "non_vegetarian"), ("mutton", "non_vegetarian"),
    ])
    def test_menu_type_normalizes_to_the_catalogue_vocabulary(self, raw_value, expected):
        from camellia.semantic_fallback import normalize_menu_type

        assert normalize_menu_type(raw_value) == expected

    @pytest.mark.parametrize("raw_value, expected", [
        ("250g", "250g"), ("250 grams", "250g"), ("quarter kilo", "250g"),
        ("500gm", "500g"), ("half kilo", "500g"),
        ("1 kg", "1kg"), ("1000g", "1kg"), ("one kg", "1kg"),
    ])
    def test_pack_size_normalizes_to_the_catalogue_vocabulary(self, raw_value, expected):
        from camellia.semantic_fallback import normalize_pack_size

        assert normalize_pack_size(raw_value) == expected

    def test_a_raw_model_menu_type_still_resolves_the_right_menu(self):
        """The exact failure reproduced: model says "veg", not "vegetarian"."""
        understood = UnderstandingResult(
            intents=["menu_request"], entities={"menu_type": "veg"},
            confidence=0.95, available=True, reason="test",
        )
        raw_understood = UnderstandingResult(
            intents=["menu_request"], entities={"menu_type": "veg"},
            confidence=0.95, available=True, reason="ok",
        )
        # Simulates what analyze() does at the API boundary before resolve()
        # ever sees the entity.
        from camellia.semantic_fallback import normalize_menu_type

        normalized = normalize_menu_type(raw_understood.entities["menu_type"])
        raw_understood.entities["menu_type"] = normalized

        resolution = approved_knowledge.resolve(
            raw_understood, responder.qa_data.get("templates", []),
        )

        assert [item.template_id for item in resolution.items] == ["weekly_menu_veg"]
        assert resolution.fully_sufficient is True


class TestRefinementAuditAgainstNamedApprovedSources:
    """The exact cases requested for a refinement-safety audit: given a
    specific approved source sentence, a specific model rewrite that adds a
    fact must be rejected, using that source's own vocabulary as the only
    permitted context — not the delivery template above.
    """

    def _refine_against(self, template_id, approved_text, model_text, message="tell me more"):
        with patch.object(ai_responder, "get_active_provider", return_value="groq"), patch.object(
            config, "GROQ_ENABLED", True,
        ), patch.object(ai_responder, "_query_groq", return_value=(model_text, True, "groq")):
            ai_responder._refinement_cache.clear()
            return ai_responder.refine_template_response(
                template_id, approved_text, message, user_context="Customer Name: Unknown",
            )

    def test_packaging_rewrite_cannot_upgrade_to_gift_wrapping(self):
        text, refined = self._refine_against(
            "packaging",
            "We use food-safe packaging.",
            "We use premium biodegradable gift wrapping.",
        )
        assert refined is False
        assert text == "We use food-safe packaging."

    def test_delivery_fee_rewrite_cannot_invent_a_flat_rate(self):
        text, refined = self._refine_against(
            "delivery_area",
            "Courier charges are on actuals.",
            "We usually charge around \u20b9150.",
        )
        assert refined is False
        assert text == "Courier charges are on actuals."

    def test_product_rewrite_cannot_invent_a_freshness_claim(self):
        text, refined = self._refine_against(
            "jolbhora",
            "Jolbhora has liquid filling.",
            "Jolbhora is freshly prepared every morning.",
        )
        assert refined is False
        assert text == "Jolbhora has liquid filling."

    def test_storage_rewrite_cannot_invent_a_shelf_life_number(self):
        text, refined = self._refine_against(
            "GUARD_shelf_life_storage",
            "Please consume the sondesh within the day for the best taste.",
            "Refrigerate for up to 7 days.",
        )
        assert refined is False
        assert text == "Please consume the sondesh within the day for the best taste."
