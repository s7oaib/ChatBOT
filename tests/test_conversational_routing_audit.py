"""General conversational precedence and state-resolution coverage."""

from unittest.mock import patch

import pytest

from camellia.config import config
from camellia.responder import responder
from camellia.session_memory import session_memory


@pytest.fixture(autouse=True)
def deterministic_runtime():
    with patch.object(session_memory, "_sessions", type(session_memory._sessions)()), \
         patch.object(config, "STRUCTURED_NLU_ENABLED", False), \
         patch.object(config, "SEMANTIC_ROUTER_ENABLED", False), \
         patch.object(config, "CONVERSATION_IMPROVEMENTS_ENABLED", True), \
         patch.object(config, "BYPASS_AD_GATE", True):
        yield


def ask(user_id, text, channel="whatsapp"):
    return responder.match_and_respond(text, user_id=user_id, channel=channel)


@pytest.mark.parametrize("selection", ["Bengali meal", "second one", "option 2", "the Bengali one", "meals please"])
def test_price_clarification_selection_continues_original_intent(selection, eligible_weekly_asset):
    user_id = "routing-price-selection"
    first = ask(user_id, "What's the pricing?")
    assert first[2] == "price_clarify_product"

    second = ask(user_id, selection)
    assert second[1] is True
    assert second[2] == "savoury_food_intro"
    approved = next(
        item for item in responder.qa_data["templates"]
        if item.get("id") == "savoury_food_intro"
    )["response"]
    assert approved in second[0]
    facts = session_memory.get_facts(user_id, "whatsapp")
    assert facts["pending_clarification"] is None
    assert facts["knowledge_sufficient"] is True


def test_correction_replaces_pending_product_selection():
    user_id = "routing-correction"
    ask(user_id, "What's the pricing?")
    result = ask(user_id, "actually sondesh")
    assert result[1] is True
    assert result[2] == "sondesh_price_detailed"
    assert "₹" in result[0]
    assert session_memory.get_facts(user_id, "whatsapp")["pending_clarification"] == "sondesh_collection"


def test_subscription_chart_request_sends_chart_only():
    user_id = "routing-chart"
    first = ask(user_id, "subscription pricing")
    assert first[2] == "subscription_details"
    assert first[3] and any("Subscription%20Rate%20Chart" in str(item) for item in first[3])

    second = ask(user_id, "Can you share the price chart?")
    assert second[1] is True
    assert second[2] == "asset_request_subscription_chart"
    assert second[3] and len(second[3]) == 1
    assert "Everyday Meal Subscription" not in second[0]


def test_explicit_product_switch_overrides_stale_subscription_context():
    user_id = "routing-topic-switch"
    ask(user_id, "subscription pricing")
    result = ask(user_id, "How much is Sondesh?")
    assert result[1] is True
    assert result[2] == "sondesh_price_detailed"
    assert "Sondesh" in result[0]


def test_ambiguous_price_without_context_clarifies():
    result = ask("routing-ambiguous", "price?")
    assert result[1] is True
    assert result[2] == "price_clarify_product"
    assert "Sondesh" in result[0] and "Bengali Meals" in result[0]


def test_option_selection_is_scoped_to_active_clarification():
    result = ask("routing-standalone", "option 2")
    assert result[1] is False
    assert result[2] is None


def test_state_expires_without_leaking_topic_or_pending_choice():
    user_id = "routing-expiry"
    ask(user_id, "price?")
    session_memory.get_session(user_id, "whatsapp")["last_updated"] = 0
    with patch("camellia.session_memory.time.time", return_value=10**10):
        fresh = session_memory.get_facts(user_id, "whatsapp")
    assert fresh["pending_clarification"] is None
    assert fresh["active_topic"] is None
