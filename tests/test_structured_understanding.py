"""Structured NLU may interpret requests; approved code still owns every action."""

from unittest.mock import patch

from camellia.ai_responder import AIResponder
from camellia.config import config
from camellia.database import Database
from camellia.rag_fallback import rag_fallback_engine
from camellia.responder import _structured_nlu_enabled_for, responder
from camellia.session_memory import session_memory
from camellia.understanding import (
    STRUCTURED_NLU_RESPONSE_FORMAT,
    UnderstandingResult,
    understanding_engine,
)


def _result(intents, entities=None, requires_human=False):
    return UnderstandingResult(
        intents=intents,
        entities=entities or {},
        confidence=0.95,
        requires_human=requires_human,
        available=True,
        reason="test",
    )


def _ask(message, result, user_id):
    session_memory.clear_session(user_id, "whatsapp")
    with patch.object(config, "STRUCTURED_NLU_ROLLOUT_PERCENT", 100), patch.object(
        understanding_engine, "analyze", return_value=result
    ):
        return responder.match_and_respond(message, user_id=user_id, channel="whatsapp")


def test_llm_contract_is_structured_json_and_validated():
    raw = (
        '{"intents":["greeting","product_price","delivery_query"],'
        '"entities":{"product":"Jolbhora","location":"Delhi","quantity":null,'
        '"event":null,"menu_type":null},"confidence":0.91,'
        '"requires_human":false,"knowledge_gap":false}'
    )
    with patch.object(config, "STRUCTURED_NLU_ENABLED", True), patch(
        "camellia.ai_responder.ai_responder.get_active_provider", return_value="groq"
    ), patch(
        "camellia.ai_responder.ai_responder._query_groq",
        return_value=(raw, True, "groq_rag"),
    ) as query:
        result = understanding_engine.analyze("Hi, Jolbhora price and delivery to Delhi?")

    assert result.intents == ["product_price", "delivery_query"]
    assert result.entities == {"product": "Jolbhora", "location": "Delhi"}
    assert query.call_args.kwargs["response_format"] == STRUCTURED_NLU_RESPONSE_FORMAT
    assert query.call_args.kwargs["response_format"]["type"] == "json_schema"
    assert query.call_args.kwargs["response_format"]["json_schema"]["strict"] is True
    assert query.call_args.kwargs["max_tokens"] == 300
    assert query.call_args.kwargs["reasoning_effort"] == "low"


def test_llm_contract_rejects_non_boolean_gap_fields():
    raw = (
        '{"intents":["menu_request"],"entities":{},"confidence":0.9,'
        '"requires_human":false,"knowledge_gap":"false"}'
    )
    with patch.object(config, "STRUCTURED_NLU_ENABLED", True), patch(
        "camellia.ai_responder.ai_responder.get_active_provider", return_value="groq"
    ), patch(
        "camellia.ai_responder.ai_responder._query_groq",
        return_value=(raw, True, "groq_rag"),
    ):
        result = understanding_engine.analyze("Show me the menu")

    assert result.available is False
    assert result.reason == "invalid_schema"


def test_price_and_delivery_execute_two_approved_actions():
    result = _result(
        ["product_price", "delivery_query"],
        {"product": "Jolbhora", "location": "Delhi"},
    )
    text, matched, template_id, _ = _ask(
        "What is the price of Jolbhora and do you deliver to Delhi?",
        result,
        "structured-price-delivery",
    )

    facts = session_memory.get_facts("structured-price-delivery", "whatsapp")
    assert matched is True
    assert template_id == "compound_product_price_delivery_query"
    assert "Jolbhora" in text and "Delhi NCR" in text
    assert "₹750" in text and "₹900" in text
    assert facts["last_intents"] == ["product_price", "delivery_query"]
    assert "SEND_PRICE_SEQUENCE" in facts["last_actions"]
    assert "CONFIRM_DELIVERY_AREA" in facts["last_actions"]
    assert facts["knowledge_sufficient"] is True


def test_structured_nlu_rollout_cohort_is_stable():
    with patch.object(config, "STRUCTURED_NLU_ENABLED", True), patch.object(
        config, "STRUCTURED_NLU_ROLLOUT_PERCENT", 0
    ):
        assert _structured_nlu_enabled_for("customer", "whatsapp") is False
    with patch.object(config, "STRUCTURED_NLU_ROLLOUT_PERCENT", 100):
        assert _structured_nlu_enabled_for("customer", "whatsapp") is True
    with patch.object(config, "STRUCTURED_NLU_ROLLOUT_PERCENT", 50):
        first = _structured_nlu_enabled_for("customer", "whatsapp")
        assert _structured_nlu_enabled_for("customer", "whatsapp") is first


def test_jolbhora_price_uses_specific_approved_table_without_nlu():
    with patch.object(config, "STRUCTURED_NLU_ROLLOUT_PERCENT", 0):
        text, matched, template_id, _ = responder.match_and_respond(
            "What is the price of Jolbhora?",
            user_id="deterministic-jolbhora-price",
            channel="whatsapp",
        )

    assert matched is True
    assert template_id == "jolbhora"
    assert "₹750" in text and "₹900" in text


def test_event_request_uses_deterministic_human_handoff():
    result = _result(
        ["product_information", "event_order"],
        {"product": "Bengali sweets", "event": "event"},
        requires_human=True,
    )
    text, matched, template_id, _ = _ask(
        "I'm looking for Bengali sweets for an event",
        result,
        "structured-event",
    )

    facts = session_memory.get_facts("structured-event", "whatsapp")
    assert matched is False
    assert template_id == "groq_structured_event_escalation"
    assert "assist you personally on whatsapp" in text.lower()
    assert facts["last_actions"] == ["ESCALATE_TO_TEAM", "SEND_RESPONSE"]


def test_provider_failure_keeps_multi_intent_behavior_deterministically():
    unavailable = UnderstandingResult(reason="groq_http_429")
    text, matched, template_id, _ = _ask(
        "Jolbhora price and do you deliver to Delhi?",
        unavailable,
        "structured-provider-down",
    )

    assert matched is True
    assert template_id == "compound_product_price_delivery_query"
    assert "Jolbhora" in text and "Delhi NCR" in text


def test_qualitative_business_claim_must_exist_in_approved_context():
    assert not rag_fallback_engine.is_grounded_in_context(
        "We offer free delivery across Delhi NCR.",
        ["Delivery is available across Delhi NCR; courier charges are on actuals."],
    )


def test_ungrounded_refinement_falls_back_to_approved_template():
    ai = AIResponder()
    approved = "Courier charges are on actuals across Delhi NCR."
    with patch.object(ai, "get_active_provider", return_value="groq"), patch.object(
        ai, "_query_groq", return_value=("We offer free delivery across Delhi NCR.", True, "groq_rag")
    ):
        response, refined = ai.refine_template_response(
            "delivery_area", approved, "Do you deliver?"
        )

    assert refined is False
    assert response == approved


def test_knowledge_gaps_deduplicate_by_intent_and_entities(tmp_path):
    database = Database(str(tmp_path / "gaps.db"))
    first = database.record_knowledge_gap(
        "Can you rent plates for my wedding?",
        ["unknown"],
        {"event": "wedding"},
        "unsupported",
    )
    second = database.record_knowledge_gap(
        "Do you provide tableware for weddings?",
        ["unknown"],
        {"event": "wedding"},
        "unsupported",
    )

    gaps = database.get_knowledge_gaps()
    assert first == second
    assert len(gaps) == 1
    assert gaps[0]["occurrences"] == 2
