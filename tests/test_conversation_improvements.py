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
def test_both_requests_use_live_menu_and_approved_delivery(message):
    result = responder.match_and_respond(message, "holdout", "whatsapp")
    assert result and result[2] == "delivery_area"
    assert "Delhi NCR" in result[0]
    assert "menu" in result[0].lower()
    assert result[3]
    assert len(session_memory.get_session("holdout", "whatsapp")["turns"]) == 2


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
    assert "finalising" in reply
    assert "₹" not in reply


def test_complaint_wins_over_menu_and_delivery():
    reply, matched, tid, _ = responder._conversation_request("wrong order delivered and show veg menu", "holdout", "whatsapp")
    assert not matched and tid == "explicit_escalation"
    assert "sorry" in reply.lower()


def test_ad_gate_still_precedes_conversation_improvements():
    with patch.object(config, "BYPASS_AD_GATE", False):
        reply = responder.match_and_respond("menu and do you deliver to noida", "holdout", "instagram")
    assert reply[2] == "ad_gate_blocked"


def test_whatsapp_does_not_reopen_sales_menu_after_closing_or_handoff():
    from camellia.whatsapp_adapter import whatsapp_adapter
    for tid in ("acknowledgement", "conversation_closing", "explicit_escalation"):
        assert whatsapp_adapter._get_contextual_buttons(tid, tid != "explicit_escalation") is None
    assert whatsapp_adapter._get_contextual_buttons("price_clarify_product", True) == ["Sondesh", "🍱 Meals"]
