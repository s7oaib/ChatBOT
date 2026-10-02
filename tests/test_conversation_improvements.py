"""Conversation holdouts beyond the benchmark's paraphrase matrix."""
from unittest.mock import patch
import pytest
from camellia.responder import responder
from camellia.config import config
from camellia.session_memory import session_memory


@pytest.fixture(autouse=True)
def isolated():
    with patch.object(session_memory, "_sessions", type(session_memory._sessions)()), \
         patch.object(config, "CONVERSATION_IMPROVEMENTS_ENABLED", True), \
         patch.object(config, "STRUCTURED_NLU_ROLLOUT_PERCENT", 100):
        yield


@pytest.mark.parametrize("message", [
    "do you deliver to noida and non-veg menu please",
    "veg menu dikhao aur gurgaon delivery hai",
    "Please share vegetarian menu? Can you deliver to Delhi?",
])
def test_both_requests_use_live_menu_and_approved_delivery(message, eligible_weekly_asset):
    from datetime import date
    with patch("camellia.menu_dates.today_ist", return_value=date(2026, 6, 21)):
        result = responder.match_and_respond(message, "holdout", "whatsapp")
    # A generic coverage question cannot invent a delivery promise.
    assert "Yes, we deliver" not in result[0]
    assert "₹" not in result[0]
    assert result[3] is None or eligible_weekly_asset in result[3]


@pytest.mark.parametrize("message", [
    "veg menu and do you deliver to not gurgaon",
    "veg menu and do you deliver to gurgaon tomorrow",  # no timing promise
    "mix rose and classic", "veg menu and payment options",
    "veg menu and do you deliver to atlantis",
])
def test_unsupported_combinations_remain_in_existing_pipeline(message):
    assert responder._conversation_request(message, "holdout", "whatsapp") is None


def test_expired_menu_does_not_gain_fresh_dishes_through_compound_route():
    with patch.object(responder, "_weekly_menu_expired", return_value=True):
        reply, _, _, _ = responder.match_and_respond("veg menu and do you deliver to noida", "holdout", "whatsapp")
    assert "Manager of Kitchens" in reply
    assert "₹" not in reply


def test_complaint_wins_over_menu_and_delivery():
    reply, matched, tid, _ = responder._conversation_request("wrong order delivered and show veg menu", "holdout", "whatsapp")
    assert not matched and tid == "explicit_escalation"
    assert "manager of kitchens" in reply.lower()
    assert "menu" not in reply.lower()


def test_ad_gate_still_precedes_conversation_improvements():
    with patch.object(config, "BYPASS_AD_GATE", False):
        reply = responder.match_and_respond("menu and do you deliver to noida", "holdout", "instagram")
    assert reply[2] == "ad_gate_blocked"
