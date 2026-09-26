"""v1.0.4 deterministic asset-request precedence and state coverage."""

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from camellia.config import config
from camellia.responder import responder
from camellia.session_memory import session_memory


@pytest.fixture(autouse=True)
def deterministic_runtime():
    from datetime import date
    with patch.object(session_memory, "_sessions", type(session_memory._sessions)()), \
         patch("camellia.menu_dates.today_ist", return_value=date(2026, 6, 21)), \
         patch.object(config, "STRUCTURED_NLU_ENABLED", False), \
         patch.object(config, "SEMANTIC_ROUTER_ENABLED", False), \
         patch.object(config, "CONVERSATION_IMPROVEMENTS_ENABLED", True), \
         patch.object(config, "BYPASS_AD_GATE", True):
        yield


def ask(user_id, text, channel="whatsapp"):
    return responder.match_and_respond(text, user_id=user_id, channel=channel)


def asset_urls(result):
    return [item.get("url") if isinstance(item, dict) else item for item in (result[3] or [])]


def test_story_subscription_then_price_chart_is_chart_only():
    user_id = "asset-story-subscription"
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {
        "routing_id": 41,
        "routing_topic": "Everyday Meal Subscription",
        "source_type": "story",
        "media_id": "17841400000000001",
        "active": True,
    }

    first = ask(user_id, "Hiii, possible to share the menu?", "instagram")
    assert first[2] == "subscription_details"
    assert any("Subscription%20Rate%20Chart" in str(url) for url in asset_urls(first))

    second = ask(user_id, "share price chart", "instagram")
    assert second[:3] == (second[0], True, "asset_request_subscription_chart")
    assert len(asset_urls(second)) == 1
    assert "Subscription%20Rate%20Chart" in asset_urls(second)[0]
    assert "Everyday Meal Subscription" not in second[0]
    state = session_memory.get_facts(user_id, "instagram")
    assert state["active_asset"] == "SUBSCRIPTION_RATE_CHART"
    assert state["active_intent"] == "ASSET_REQUEST"
    assert state["source_context_used"] is False


@pytest.mark.parametrize("message", [
    "share price chart", "send the chart", "share the rates", "where is the chart?",
])
def test_subscription_chart_paraphrases_share_the_same_asset(message):
    user_id = "asset-chart-" + hashlib.sha1(message.encode()).hexdigest()[:8]
    ask(user_id, "subscription pricing")
    result = ask(user_id, message)
    assert result[2] == "asset_request_subscription_chart"
    assert asset_urls(result) == ["/images/Everyday%20Meal%20Subscription%20Rate%20Chart.jpeg"]
    assert result[0] == "Here is the subscription price chart again."


@pytest.mark.parametrize(
    "opening, followup, expected_route, expected_asset",
    [
        ("Aamar Mishti menu", "send the menu again", "asset_request_aamar_mishti_menu", "AAMAR_MISHTI_MENU"),
        ("Signature menu", "show option 2 menu", "asset_request_signature_sondesh_menu", "SIGNATURE_SONDESH_MENU"),
        ("savoury food", "share current weekly menu", "asset_request_weekly_menu", "WEEKLY_MENU"),
        ("Bengali Bento images", "send it again", "asset_request_bento_images", "BENTO_IMAGES"),
    ],
)
def test_asset_requests_work_across_supported_asset_types(opening, followup, expected_route, expected_asset):
    user_id = "asset-generic-" + expected_asset.lower()
    first = ask(user_id, opening)
    assert first[1] is True
    second = ask(user_id, followup)
    assert second[1] is True
    assert second[2] == expected_route
    assert second[3]
    assert session_memory.get_facts(user_id, "whatsapp")["active_asset"] == expected_asset


def test_explicit_current_asset_overrides_stale_story_context():
    user_id = "asset-explicit-overrides-source"
    facts = session_memory.get_facts(user_id, "instagram")
    facts.update({
        "source_route": {"routing_topic": "Signature Story", "source_type": "story", "active": True},
        "active_topic": "subscription",
        "last_topic": "subscription",
        "active_asset": "SUBSCRIPTION_RATE_CHART",
    })
    result = ask(user_id, "share Aamar Mishti menu", "instagram")
    assert result[2] == "asset_request_aamar_mishti_menu"
    assert "AAMAR%20MISHTI.jpeg" in asset_urls(result)[0]
    assert session_memory.get_facts(user_id, "instagram")["source_context_used"] is False


def test_stale_clarification_cannot_replay_canonical_answer_for_asset_request():
    user_id = "asset-stale-clarification"
    facts = session_memory.get_facts(user_id, "whatsapp")
    facts.update({
        "pending_clarification": "product_price",
        "clarification_options": ["SONDESH", "BENGALI_MEAL"],
        "active_topic": "subscription",
        "last_topic": "subscription",
        "last_answer_id": "price_clarify_product",
    })
    result = ask(user_id, "send the chart")
    assert result[2] == "asset_request_subscription_chart"
    assert "Would you like prices" not in result[0]
    assert "Everyday Meal Subscription" not in result[0]


def test_ambiguous_resend_clarifies_without_guessing_or_cam():
    result = ask("asset-ambiguous", "send it again", "instagram")
    assert result[1] is True
    assert result[2] == "asset_request_clarify"
    assert result[3] is None
    assert "which approved asset" in result[0].lower()
    assert "whatsapp" not in result[0].lower()


def test_supported_asset_routes_never_call_nlu_or_llm():
    user_id = "asset-no-llm"
    ask(user_id, "subscription pricing")
    with patch("camellia.responder.understanding_engine.analyze", side_effect=AssertionError("asset route called NLU")), \
         patch("camellia.responder.ai_responder.refine_template_response", side_effect=AssertionError("asset route called LLM")):
        result = ask(user_id, "can you send it?")
    assert result[2] == "asset_request_subscription_chart"
    assert session_memory.get_facts(user_id, "whatsapp")["last_llm_called"] is False


def test_approved_qa_sources_are_byte_for_byte_unchanged():
    root = Path(__file__).resolve().parents[1]
    expected = {
        "data/qa_templates.json": "736525135DC452EDD4270F7D65422D2EAF7822E2006EAEBBD09C73703564E2F0",
        "data/client_approved_qa_source.txt": "8B976BB68EC91B3B978FAA4C0BDBF0CDA770ED4972A5E8BC77D6581AE7F967B8",
    }
    for relative, digest in expected.items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest().upper() == digest
