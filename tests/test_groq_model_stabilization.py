"""Groq model assignments and bounded structured-NLU fallback contracts."""

import json
from unittest.mock import MagicMock, patch

from camellia.ai_responder import ai_responder
from camellia.config import _env_value_or_default, config
from camellia.responder import responder
from camellia.session_memory import session_memory
from camellia.understanding import (
    STRUCTURED_NLU_RESPONSE_FORMAT,
    understanding_engine,
)


SUPPORTED_PRODUCTION_MODELS = {
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
}


def test_production_model_defaults_are_supported_and_separated():
    assert config.GROQ_MODEL == "openai/gpt-oss-20b"
    assert config.GROQ_ROUTER_MODEL in SUPPORTED_PRODUCTION_MODELS
    assert config.LLM_TRIAGE_MODEL in SUPPORTED_PRODUCTION_MODELS
    assert config.STRUCTURED_NLU_FALLBACK_MODEL in SUPPORTED_PRODUCTION_MODELS
    assert config.GROQ_ROUTER_MODEL != config.GROQ_MODEL
    assert config.STRUCTURED_NLU_FALLBACK_MODEL != config.GROQ_ROUTER_MODEL
    assert "compound" not in config.GROQ_ROUTER_MODEL
    assert "compound" not in config.LLM_TRIAGE_MODEL
    assert "compound" not in config.STRUCTURED_NLU_FALLBACK_MODEL


def test_fallback_default_is_used_only_when_the_environment_variable_is_absent(monkeypatch):
    monkeypatch.delenv("STRUCTURED_NLU_FALLBACK_MODEL", raising=False)
    assert _env_value_or_default(
        "STRUCTURED_NLU_FALLBACK_MODEL", "openai/gpt-oss-20b",
    ) == "openai/gpt-oss-20b"


def test_explicit_empty_fallback_environment_value_disables_fallback(monkeypatch):
    monkeypatch.setenv("STRUCTURED_NLU_FALLBACK_MODEL", "")
    assert _env_value_or_default(
        "STRUCTURED_NLU_FALLBACK_MODEL", "openai/gpt-oss-20b",
    ) == ""


def test_populated_fallback_environment_value_is_used_exactly(monkeypatch):
    monkeypatch.setenv("STRUCTURED_NLU_FALLBACK_MODEL", " custom/model ")
    assert _env_value_or_default(
        "STRUCTURED_NLU_FALLBACK_MODEL", "openai/gpt-oss-20b",
    ) == "custom/model"


def test_structured_nlu_uses_primary_then_one_configured_fallback():
    body = json.dumps({
        "intents": ["menu_request"],
        "entities": {},
        "confidence": 0.93,
        "requires_human": False,
        "knowledge_gap": False,
    })
    with patch.object(
        ai_responder, "get_active_provider", return_value="groq",
    ), patch.object(
        ai_responder, "_query_groq",
        side_effect=[("", False, "groq_http_429"), (body, True, "groq_rag")],
    ) as query:
        result = understanding_engine.analyze("show me the menu")

    assert result.available is True
    assert result.intents == ["menu_request"]
    assert isinstance(result.confidence, (int, float))
    assert result.requires_human is False
    assert result.knowledge_gap is False
    assert query.call_count == 2
    assert [call.kwargs["model"] for call in query.call_args_list] == [
        config.GROQ_ROUTER_MODEL,
        config.STRUCTURED_NLU_FALLBACK_MODEL,
    ]
    assert config.GROQ_ROUTER_MODEL != config.STRUCTURED_NLU_FALLBACK_MODEL
    assert all(
        call.kwargs["response_format"] == STRUCTURED_NLU_RESPONSE_FORMAT
        for call in query.call_args_list
    )
    assert all(
        call.kwargs["reasoning_effort"] == "low"
        for call in query.call_args_list
    )
    assert STRUCTURED_NLU_RESPONSE_FORMAT["type"] == "json_schema"
    assert STRUCTURED_NLU_RESPONSE_FORMAT["json_schema"]["strict"] is True


def test_primary_500_uses_exactly_one_strict_schema_fallback_attempt():
    body = json.dumps({
        "intents": ["menu_request"],
        "entities": {
            "product": None, "location": None, "quantity": None,
            "event": None, "menu_type": None, "pack_size": None,
            "topic": None, "subtopic": None,
        },
        "confidence": 0.93,
        "requires_human": False,
        "knowledge_gap": False,
    })
    with patch.object(
        ai_responder, "get_active_provider", return_value="groq",
    ), patch.object(
        ai_responder, "_query_groq",
        side_effect=[("", False, "groq_http_500"), (body, True, "groq_rag")],
    ) as query:
        result = understanding_engine.analyze("show me the menu")

    assert result.available is True
    assert query.call_count == 2
    assert query.call_args_list[0].kwargs["response_format"] == (
        query.call_args_list[1].kwargs["response_format"]
    )
    assert query.call_args_list[0].kwargs["reasoning_effort"] == "low"


def test_structured_nlu_strict_failure_is_parked_without_repeated_requests(monkeypatch):
    monkeypatch.setattr(understanding_engine, "_strict_format_disabled_until", 0.0)
    with patch.object(
        ai_responder, "get_active_provider", return_value="groq",
    ), patch.object(
        ai_responder, "_query_groq",
        return_value=("", False, "groq_json_validate_failed"),
    ) as query:
        first = understanding_engine.analyze("show me the menu")
        second = understanding_engine.analyze("what are your opening hours?")

    assert first.reason == "groq_json_validate_failed"
    assert second.reason == "structured_nlu_format_cooldown"
    assert query.call_count == 1


def test_reasoning_effort_is_caller_specific_and_serialized_for_groq():
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
    }
    with patch("camellia.ai_responder.httpx.Client") as client_cls, patch.object(
        config, "GROQ_API_KEY", "test-key",
    ):
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = response
        ai_responder._query_groq(
            "probe",
            is_refinement=True,
            model="openai/gpt-oss-120b",
            response_format=STRUCTURED_NLU_RESPONSE_FORMAT,
            max_tokens=300,
            reasoning_effort="low",
        )

    payload = client.post.call_args.kwargs["json"]
    assert payload["reasoning_effort"] == "low"
    assert payload["max_tokens"] == 300
    assert payload["response_format"] == STRUCTURED_NLU_RESPONSE_FORMAT


def test_json_validate_failed_strict_response_fails_closed_without_side_model_cooldown(monkeypatch):
    monkeypatch.setattr(ai_responder, "_groq_cooldown_until", 0.0)
    response = MagicMock()
    response.status_code = 400
    response.text = '{"error":{"code":"json_validate_failed"}}'
    response.json.return_value = {
        "error": {"code": "json_validate_failed"},
    }
    with patch("camellia.ai_responder.httpx.Client") as client_cls, patch.object(
        config, "GROQ_API_KEY", "test-key",
    ):
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = response
        raw, ok, reason = ai_responder._query_groq(
            "probe",
            is_refinement=True,
            model="openai/gpt-oss-120b",
            response_format=STRUCTURED_NLU_RESPONSE_FORMAT,
            max_tokens=300,
            reasoning_effort="low",
        )

    assert (raw, ok, reason) == ("", False, "groq_json_validate_failed")
    assert ai_responder._groq_cooldown_until == 0.0


def test_schema_invalid_output_is_not_retried_on_the_fallback_model():
    malformed_schema = json.dumps({
        "intents": ["menu_request"],
        "entities": {},
        "confidence": 0.93,
        "requires_human": False,
        "knowledge_gap": "false",
    })
    with patch.object(
        ai_responder, "get_active_provider", return_value="groq",
    ), patch.object(
        ai_responder, "_query_groq",
        return_value=(malformed_schema, True, "groq_rag"),
    ) as query:
        result = understanding_engine.analyze("show me the menu")

    assert result.available is False
    assert result.reason == "invalid_schema"
    assert query.call_count == 1


def test_unsupported_intent_cannot_become_executable_authority():
    body = json.dumps({
        "intents": ["invented_customer_authority"],
        "entities": {},
        "confidence": 0.99,
        "requires_human": False,
        "knowledge_gap": False,
    })
    with patch.object(
        ai_responder, "get_active_provider", return_value="groq",
    ), patch.object(
        ai_responder, "_query_groq", return_value=(body, True, "groq_rag"),
    ):
        result = understanding_engine.analyze("show me the menu")

    assert result.available is True
    assert result.intents == ["unknown"]
    assert "invented_customer_authority" not in (
        STRUCTURED_NLU_RESPONSE_FORMAT["json_schema"]["schema"]["properties"]
        ["intents"]["items"]["enum"]
    )


def test_both_model_failures_keep_the_deterministic_compound_route():
    user_id = "groq-both-models-fail"
    session_memory.clear_session(user_id, "whatsapp")
    with patch.object(
        ai_responder, "get_active_provider", return_value="groq",
    ), patch.object(
        ai_responder, "_query_groq",
        side_effect=[
            ("", False, "groq_http_429"),
            ("", False, "groq_http_503"),
        ],
    ) as query:
        text, matched, template_id, _images = responder.match_and_respond(
            "Jolbhora price and do you deliver to Delhi?",
            user_id=user_id,
            channel="whatsapp",
        )

    assert query.call_count == 2
    assert matched is False
    assert template_id == "jolbhora"
    assert "Manager of Kitchens" in text
    assert "₹" not in text
    assert "Delhi NCR" not in text
