"""Business facts are addressed by meaning, and every factual answer is traceable.

The old shape of this bot kept its knowledge in customer phrasing: to answer a
new wording you added a trigger. These tests hold the replacement in place —
a catalogue keyed by intent and entities, a sufficiency decision behind every
factual reply, and a relevance rule that does not need to know in advance which
topics the business has no answer for.
"""

import json
import uuid
from unittest.mock import patch

import pytest

from camellia.ai_responder import ai_responder
from camellia.config import config
from camellia.knowledge import CATALOGUE, SOURCE_MAP, approved_knowledge
from camellia.rag_fallback import RAGResult, rag_fallback_engine
from camellia.responder import responder
from camellia.session_memory import session_memory
from camellia.understanding import Intent, UnderstandingResult, understanding_engine


def _fresh(prefix):
    user_id = f"{prefix}_{uuid.uuid4().hex[:8]}"
    session_memory.clear_session(user_id, "whatsapp")
    return user_id


def _model_understanding(message, history_turns=None, state=None):
    result = understanding_engine.deterministic_fallback(message)
    result.reason = "model_nlu"
    result.available = True
    result.confidence = 0.92
    return result


def _ask_with_model(message, user_id):
    with patch.object(understanding_engine, "analyze", side_effect=_model_understanding), patch(
        "camellia.responder.ai_responder.compose_verified_response",
        return_value=("", False, "provider_unavailable"),
    ):
        return responder.match_and_respond(message, user_id=user_id, channel="whatsapp")


class TestTheCatalogueCoversTheBusiness:
    @pytest.mark.parametrize("domain", [
        "weekly_menu", "products", "flavours", "pack_sizes", "bento", "prices",
        "delivery", "delivery_time", "storage", "packaging", "ordering",
        "subscriptions", "contact", "allergens", "payment",
    ])
    def test_each_business_domain_has_canonical_knowledge(self, domain):
        assert domain in approved_knowledge.domains()

    def test_every_record_points_at_an_approved_answer_that_exists(self):
        templates = {
            template["id"]: template
            for template in responder.qa_data.get("templates", [])
        }
        for record in CATALOGUE:
            template = templates.get(record.template_id)
            assert template is not None, f"{record.knowledge_id} names a missing template"
            assert not template.get("escalate"), f"{record.knowledge_id} points at an escalation"
            assert template.get("response") or template.get("responses")

    def test_the_catalogue_stores_no_customer_phrasing(self):
        """Records carry meaning and a pointer, never a question a customer typed."""
        for record in CATALOGUE:
            text = f"{record.knowledge_id} {record.domain} {record.template_id}"
            assert "?" not in text
            for phrase in ("what", "how much", "do you", "can i", "show me"):
                assert phrase not in text.lower()

    def test_the_source_map_names_one_owner_per_domain(self):
        for domain, entry in SOURCE_MAP.items():
            assert entry.get("source"), f"{domain} has no source of truth"

    def test_every_intent_the_model_may_return_is_either_answered_or_human_only(self):
        from camellia.knowledge import HUMAN_ONLY_INTENTS

        answered = {intent for record in CATALOGUE for intent in record.intents}
        # These are resolved in code rather than by a record: they need the
        # customer's location, or they ask a question instead of stating a fact.
        resolved_in_code = {
            Intent.GREETING.value, Intent.DELIVERY_QUERY.value, Intent.PRODUCT_PRICE.value,
        }
        for intent in Intent:
            assert (
                intent.value in answered
                or intent.value in HUMAN_ONLY_INTENTS
                or intent.value in resolved_in_code
            ), f"{intent.value} has neither knowledge nor a human-only rule"


class TestKnowledgeIsAddressedByMeaning:
    @pytest.mark.parametrize("intents, entities, expected_template", [
        (["product_price"], {"product": "Jolbhora"}, "jolbhora"),
        (["product_price"], {"product": "aamar mishti"}, "aamar_mishti_collection"),
        (["product_information"], {"product": "bengali bento"}, "bento_details"),
        (["pack_size_question"], {"pack_size": "250g"}, "pack_size_250g"),
        (["subscription_query"], {}, "subscription_details"),
        (["subscription_booking"], {}, "subscription_booking_steps"),
        (["subscription_change"], {}, "subscription_booking_steps"),
        (["packaging_question"], {}, "packaging"),
        (["storage_question"], {}, "GUARD_shelf_life_storage"),
        (["order_process"], {}, "how_to_order"),
        (["menu_request"], {"menu_type": "non_vegetarian"}, "weekly_menu_nonveg"),
    ])
    def test_intent_and_entities_select_the_approved_answer(
        self, intents, entities, expected_template,
    ):
        understood = UnderstandingResult(
            intents=intents, entities=entities, confidence=0.9, available=True, reason="test",
        )
        resolution = approved_knowledge.resolve(
            understood, responder.qa_data.get("templates", []),
        )

        assert [item.template_id for item in resolution.items] == [expected_template]
        assert resolution.fully_sufficient is True

    @pytest.mark.parametrize("topic", [
        "washrooms", "wheelchair access", "pet policy", "outdoor seating",
        "air conditioning", "charging point", "smoking area", "valet parking",
    ])
    def test_a_topic_with_no_record_is_a_gap_whatever_it_is(self, topic):
        """No list of unsupported topics is consulted; a record either exists or not."""
        understood = UnderstandingResult(
            intents=["unknown"], entities={"topic": topic}, confidence=0.9,
            available=True, reason="test",
        )
        resolution = approved_knowledge.resolve(
            understood, responder.qa_data.get("templates", []),
        )

        assert resolution.items == []
        assert resolution.sufficient_by_intent == {"unknown": False}


class TestUnknownTopicsCannotBorrowAnUnrelatedAnswer:
    @pytest.mark.parametrize("message", [
        "Do you have washrooms?",
        "Is there wheelchair access?",
        "Can I bring my dog?",
        "Do you have air conditioning?",
        "Is there a charging point?",
        "Can we sit inside?",
        "Do you allow smoking?",
        "Do you take table reservations?",
        "Can I take photos in the kitchen?",
        "Do you have valet parking?",
    ])
    def test_the_deterministic_path_refuses_an_off_subject_answer(self, message):
        user_id = _fresh("relevance")
        with patch.object(config, "STRUCTURED_NLU_ENABLED", False), patch.object(
            config, "SEMANTIC_ROUTER_ENABLED", False,
        ):
            text, matched, template_id, _images = responder.match_and_respond(
                message, user_id=user_id, channel="whatsapp",
            )

        assert matched is False
        assert template_id is None
        assert "cornstarch" not in text.lower()

    @pytest.mark.parametrize("message, expected_template", [
        ("show me the menu", "know_more"),
        ("what is the price of jolbhora", "jolbhora"),
        ("do you deliver to noida", "delivery_area"),
        ("how do i store the sondesh", "GUARD_shelf_life_storage"),
        ("how do you pack the sweets", "packaging"),
        ("supscription details please", "subscription_details"),
    ])
    def test_a_real_question_is_still_answered(self, message, expected_template):
        user_id = _fresh("relevance_ok")
        with patch.object(config, "STRUCTURED_NLU_ENABLED", False), patch.object(
            config, "SEMANTIC_ROUTER_ENABLED", False,
        ):
            _text, matched, template_id, _images = responder.match_and_respond(
                message, user_id=user_id, channel="whatsapp",
            )

        assert matched is True
        assert template_id == expected_template


class TestEveryFactualAnswerIsTraceable:
    @pytest.mark.parametrize("message", [
        "how do you pack the sweets",
        "how do i store the sondesh",
        "what is the price of jolbhora",
        "do you deliver to noida",
    ])
    def test_a_compatibility_answer_records_its_source_and_sufficiency(self, message):
        user_id = _fresh("sufficiency")
        with patch.object(config, "STRUCTURED_NLU_ENABLED", False), patch.object(
            config, "SEMANTIC_ROUTER_ENABLED", False,
        ):
            responder.match_and_respond(message, user_id=user_id, channel="whatsapp")
        facts = session_memory.get_facts(user_id, "whatsapp")

        assert facts["last_knowledge_ids"], "a business fact was sent with no source"
        assert facts["last_knowledge_sufficiency"]
        assert all(facts["last_knowledge_sufficiency"].values())
        assert facts["knowledge_sufficient"] is True
        assert facts["last_source_ref"]

    def test_an_unanswered_question_records_an_insufficiency(self):
        user_id = _fresh("sufficiency_gap")
        with patch.object(config, "STRUCTURED_NLU_ENABLED", False), patch.object(
            config, "SEMANTIC_ROUTER_ENABLED", False,
        ):
            responder.match_and_respond(
                "Do you have wheelchair access?", user_id=user_id, channel="whatsapp",
            )
        facts = session_memory.get_facts(user_id, "whatsapp")

        assert facts["knowledge_sufficient"] is False
        assert facts["last_knowledge_sufficiency"]
        assert not any(facts["last_knowledge_sufficiency"].values())

    def test_a_clarifying_question_is_not_dressed_up_as_a_fact(self):
        """"Sondesh or Meals?" asserts nothing, so it needs no knowledge source."""
        user_id = _fresh("sufficiency_clarify")
        with patch.object(config, "STRUCTURED_NLU_ENABLED", False), patch.object(
            config, "SEMANTIC_ROUTER_ENABLED", False,
        ):
            _text, _matched, template_id, _images = responder.match_and_respond(
                "how much", user_id=user_id, channel="whatsapp",
            )
        facts = session_memory.get_facts(user_id, "whatsapp")

        assert template_id == "price_clarify_product"
        assert facts.get("last_knowledge_ids") == []


class TestPartialMultiIntent:
    def test_the_verified_price_is_answered_and_the_discount_is_not_invented(self):
        user_id = _fresh("partial_discount")
        text, matched, template_id, _images = _ask_with_model(
            "What is the price of Jolbhora and can I get a 30% discount?", user_id,
        )
        facts = session_memory.get_facts(user_id, "whatsapp")

        assert facts["last_knowledge_sufficiency"] == {
            "product_price": True, "discount_request": False,
        }
        assert facts["last_knowledge_ids"] == ["approved:jolbhora"]
        assert "₹750" in text            # the approved Jolbhora price still ships
        assert "30%" not in text
        assert "discount" not in text.lower()
        assert "team" in text.lower()    # the unsupported half goes to a person
        assert matched is False
        assert template_id == "compound_product_price_discount_request"

    def test_availability_is_escalated_while_the_product_answer_stands(self):
        user_id = _fresh("partial_availability")
        text, _matched, _template_id, _images = _ask_with_model(
            "Is Jolbhora available today and what does it cost?", user_id,
        )
        facts = session_memory.get_facts(user_id, "whatsapp")

        assert facts["last_knowledge_sufficiency"] == {
            "product_price": True, "availability_query": False,
        }
        assert "available today" not in text.lower()
        assert "team" in text.lower()


class TestGeneratedProseCannotIntroduceFacts:
    @pytest.mark.parametrize("answer, context", [
        ("We prepare Jolbhora fresh every morning.",
         ["Jolbhora is a Nolen Gur Sondesh with a liquid filling."]),
        ("Our sweets are handmade by an all-women team.",
         ["An all-women team handcrafts every sondesh."]),
        ("We are open on Sundays.",
         ["Our kitchen is open 10:00 AM - 7:30 PM every day."]),
        ("Orders are packed in reusable steel tins.",
         ["Food-safe cornstarch boxes are used for every order."]),
    ])
    def test_an_unsupported_statement_is_rejected_without_a_keyword_list(
        self, answer, context,
    ):
        grounded, reason = rag_fallback_engine.grounding_report(answer, context)

        assert grounded is False
        assert reason.startswith("unsupported_")

    def test_a_faithful_paraphrase_still_passes(self):
        grounded, reason = rag_fallback_engine.grounding_report(
            "We deliver across Delhi NCR in food-safe packaging.",
            ["We deliver across Delhi NCR. We use food-safe packaging."],
        )

        assert (grounded, reason) == (True, "grounded")


class TestRetrievalMustBeAboutTheQuestion:
    def test_off_subject_excerpts_never_reach_generation(self):
        """Packaging copy retrieved for a seating question is not an answer."""
        retrieved = RAGResult(
            retrieved_sources=["packaging"],
            source_chunks=["All our food is packed in eco-friendly cornstarch boxes."],
            confidence_score=0.9,
            proposed_intent="packaging",
        )
        user_id = _fresh("rag_relevance")
        with patch.object(config, "STRUCTURED_NLU_ENABLED", False), patch.object(
            config, "SEMANTIC_ROUTER_ENABLED", False,
        ), patch("camellia.responder.rag_fallback_engine.retrieve", return_value=retrieved), patch.object(
            ai_responder, "answer_from_retrieved_context",
        ) as generate:
            text, matched, template_id, _images = responder.match_and_respond(
                "Do you have outdoor seating?", user_id=user_id, channel="whatsapp",
            )

        assert not generate.called
        assert matched is False
        assert template_id is None
        assert "cornstarch" not in text.lower()


class TestTheConfiguredSecondUnderstandingModel:
    @pytest.mark.parametrize("reason, worth_retrying", [
        ("groq_http_429", True),
        ("groq_http_503", True),
        ("groq_exception_ConnectError", True),
        ("groq_no_choices", True),
        ("groq_http_400", False),
        ("groq_http_401", False),
        ("invalid_schema", False),
        ("invalid_json", False),
    ])
    def test_only_capacity_failures_are_worth_a_second_model(self, reason, worth_retrying):
        assert understanding_engine._another_model_could_survive(reason) is worth_retrying

    def test_no_second_call_happens_when_no_fallback_model_is_configured(self):
        with patch.object(config, "STRUCTURED_NLU_FALLBACK_MODEL", ""), patch.object(
            ai_responder, "get_active_provider", return_value="groq",
        ), patch.object(
            ai_responder, "_query_groq", return_value=("", False, "groq_http_429"),
        ) as query:
            result = understanding_engine.analyze("do you deliver to delhi")

        assert query.call_count == 1
        assert result.available is False
        assert result.reason == "groq_http_429"

    def test_the_configured_model_is_tried_once_and_only_once(self):
        with patch.object(config, "STRUCTURED_NLU_FALLBACK_MODEL", "openai/gpt-oss-20b"), patch.object(
            ai_responder, "get_active_provider", return_value="groq",
        ), patch.object(
            ai_responder, "_query_groq", return_value=("", False, "groq_http_429"),
        ) as query:
            result = understanding_engine.analyze("do you deliver to delhi")

        assert query.call_count == 2
        models = [call.kwargs["model"] for call in query.call_args_list]
        assert models == [config.GROQ_ROUTER_MODEL, "openai/gpt-oss-20b"]
        assert result.available is False
        assert result.reason == "groq_http_429>groq_http_429"

    def test_a_schema_violation_is_never_retried_on_another_model(self):
        body = json.dumps({
            "intents": ["menu_request"], "entities": {}, "confidence": 0.9,
            "requires_human": False, "knowledge_gap": "false",
        })
        with patch.object(config, "STRUCTURED_NLU_FALLBACK_MODEL", "openai/gpt-oss-20b"), patch.object(
            ai_responder, "get_active_provider", return_value="groq",
        ), patch.object(ai_responder, "_query_groq", return_value=(body, True, "groq_rag")) as query:
            result = understanding_engine.analyze("show me the menu")

        assert query.call_count == 1
        assert result.reason == "invalid_schema"
