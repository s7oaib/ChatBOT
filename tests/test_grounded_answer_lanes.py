"""Focused Instagram customer-answer boundary tests (no live Meta traffic)."""

import json
from unittest.mock import patch

import pytest

from camellia import client_qa
from camellia import app as app_module
from camellia.config import config
from camellia.grounded_answers import validate_model_draft
from camellia.responder import responder
from camellia.session_memory import session_memory


@pytest.fixture(autouse=True)
def no_groq():
    with patch("camellia.grounded_answers.ai_responder.get_active_provider", return_value="none"):
        yield


def route(message, user_id="grounded-lane"):
    session_memory.clear_session(user_id, "instagram")
    response = responder.match_and_respond(message, user_id=user_id, channel="instagram")
    return response, session_memory.get_facts(user_id, "instagram")


@pytest.mark.parametrize("message", ["Hi", "Thanks", "Okay", "😊"])
def test_system_conversation_never_handoffs(message):
    (answer, matched, template, images), facts = route(message)
    assert answer and matched and template == "system_conversation" and images is None
    assert facts["decision_lane"] == "SYSTEM_CONVERSATION"
    assert facts["last_knowledge_ids"] == []


@pytest.mark.parametrize("message,source_id", [
    ("What is the Sondesh price?", "sondesh_price_client"),
    ("How much is Sondesh?", "sondesh_price_client"),
    ("Tell me about Bengali Bento", "bento_details_client"),
])
def test_paraphrase_returns_unchanged_client_answer(message, source_id, eligible_weekly_asset):
    (answer, matched, _template, _images), facts = route(message)
    assert matched and answer == client_qa.REGISTRY_BY_ID[source_id].answer
    assert facts["decision_lane"] == "EXACT_APPROVED"


@pytest.mark.parametrize("message,answer", [
    ("What's the cheapest Bento?", "The Vegetarian Bento is the lowest-priced option at ₹350."),
    ("How much more is Chicken Bento than Vegetarian?", "The Chicken Bento is ₹50 more than the Vegetarian Bento."),
    ("Which costs more, Chicken or Mangsho?", "The Mangsho Bento is more expensive at ₹600, compared with ₹400 for the Chicken Bento."),
    ("Is Mangsho Bento ₹200 more than Chicken?", "Yes. The Mangsho Bento is ₹200 more than the Chicken Bento."),
])
def test_grounded_bento_arithmetic(message, answer):
    (text, matched, template, _images), facts = route(message)
    assert (text, matched, template) == (answer, True, "grounded_derived")
    assert facts["decision_lane"] == "GROUNDED_DERIVED"
    assert facts["last_knowledge_ids"] == ["client_qa:bento_details_client"]


@pytest.mark.parametrize("message", [
    "Can you give me a discount for 100 Bento?",
    "Can you cater for 300 wedding guests?",
    "No onion option available?",
    "Can someone with a severe gluten allergy safely eat your Sondesh?",
    "What's the cheapest Bento delivered to Mumbai?",
    "Is Chicken Bento available today?",
])
def test_unsupported_high_risk_question_handoffs(message):
    (answer, matched, template, images), facts = route(message)
    assert answer == responder._generic_fallback()
    assert (matched, template, images) == (False, None, None)
    assert facts["decision_lane"] == "HUMAN_HANDOFF"


@pytest.mark.parametrize("draft", [
    {"decision": "GROUNDED_DERIVED", "answer": "The Vegetarian Bento is ₹250.", "source_ids": ["bento_details_client"], "claims": [{"claim": "₹250", "supported_by": ["bento_details_client"]}], "confidence": 0.99},
    {"decision": "GROUNDED_DERIVED", "answer": "safe", "source_ids": ["unknown"], "claims": [{"claim": "safe", "supported_by": ["unknown"]}], "confidence": 0.99},
    {"decision": "GROUNDED_DERIVED", "answer": "safe", "source_ids": [], "claims": [{"claim": "safe", "supported_by": []}], "confidence": 0.99},
])
def test_grounding_gate_rejects_unsupported_model_drafts(draft):
    assert not validate_model_draft(draft, "safe")


@pytest.mark.parametrize("answer,sources", [
    ("The Vegetarian Bento is ₹250.", ["bento_details_client"]),
    ("The Vegetarian Bento is the lowest-priced option at ₹350.", ["unknown"]),
    ("The Vegetarian Bento is the lowest-priced option at ₹350.", []),
])
def test_rejected_groq_draft_handoffs(answer, sources):
    bad = {"decision": "GROUNDED_DERIVED", "answer": answer,
           "source_ids": sources,
           "claims": [{"claim": answer, "supported_by": sources}],
           "confidence": 0.99}
    with patch("camellia.grounded_answers.ai_responder.get_active_provider", return_value="groq"), patch(
        "camellia.grounded_answers.ai_responder._query_groq", return_value=(json.dumps(bad), True, "ok")
    ):
        (answer, matched, template, _images), facts = route("What's the cheapest Bento?", "bad-draft")
    assert (answer, matched, template) == (responder._generic_fallback(), False, None)
    assert facts["decision_lane"] == "HUMAN_HANDOFF"


def test_greeting_then_exact_answer(eligible_weekly_asset):
    user_id = "greeting-then-price"
    session_memory.clear_session(user_id, "instagram")
    responder.match_and_respond("Hi", user_id=user_id, channel="instagram")
    answer, matched, _template, _images = responder.match_and_respond(
        "What's the price of Sondesh?", user_id=user_id, channel="instagram"
    )
    assert matched and answer == client_qa.REGISTRY_BY_ID["sondesh_price_client"].answer


def test_sondesh_gluten_statement_is_quoted_only_from_approved_source():
    (answer, matched, template, _images), facts = route("Is your Sondesh gluten free?")
    assert (answer, matched, template) == (
        "The client-approved Sondesh description says ‘no gluten.’", True, "grounded_derived"
    )
    assert facts["last_knowledge_ids"] == ["client_qa:general_details"]


def test_bento_context_supports_single_option_selection(eligible_weekly_asset):
    user_id = "bento-option-selection"
    session_memory.clear_session(user_id, "instagram")
    responder.match_and_respond("Tell me about Bengali Bento", user_id=user_id, channel="instagram")
    answer, matched, template, _images = responder.match_and_respond(
        "Chicken", user_id=user_id, channel="instagram"
    )
    assert (answer, matched, template) == (
        "The Chicken Bento is ₹400.", True, "grounded_derived"
    )


def test_groq_outage_does_not_block_deterministic_approved_facts():
    with patch("camellia.grounded_answers.ai_responder.get_active_provider", return_value="groq"), patch(
        "camellia.grounded_answers.ai_responder._query_groq", return_value=("", False, "groq_unavailable")
    ):
        (answer, matched, template, _images), _facts = route("What's the cheapest Bento?", "groq-outage")
    assert (answer, matched, template) == (
        "The Vegetarian Bento is the lowest-priced option at ₹350.", True, "grounded_derived"
    )


def test_instagram_greeting_sends_reply_without_handoff():
    user_id = "grounded-greeting-app"
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app_module, "send_instagram_reply") as send, patch.object(
        app_module.escalator, "handle_escalation"
    ) as escalation, patch.object(config, "LLM_ASSISTED_RESOLUTION_ENABLED", False), patch.object(
        config, "LLM_AUTO_RESOLVE_SIMULATION_ENABLED", False
    ):
        app_module._process_instagram_event(user_id, "Hi", "grounded-ad")
    send.assert_called_once()
    assert "How can I help" in send.call_args.args[1]
    escalation.assert_not_called()
