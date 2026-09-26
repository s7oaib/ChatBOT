import json
from pathlib import Path

import pytest

from camellia.config import config
from camellia.responder import responder
from camellia.session_memory import session_memory


CASES = json.loads(
    (Path(__file__).with_name("golden_routing_cases.json")).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_client_approved_routing_golden(case, monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    if case["id"] == "weekly_veg_menu":
        from datetime import date
        monkeypatch.setattr("camellia.menu_dates.today_ist", lambda: date(2026, 6, 21))
    user_id = "golden_" + case["id"]
    session_memory.clear_session(user_id, "instagram")

    response, matched, template_id, _images = responder.match_and_respond(
        case["input"], user_id=user_id, channel="instagram",
    )
    facts = session_memory.get_facts(user_id, "instagram")

    assert facts["decision_lane"] == case["lane"]
    assert template_id == case["template"]
    assert bool(not matched) == bool(case["handoff"])
    for required in case["contains_any"]:
        assert required.lower() in response.lower()
    if case["handoff"]:
        assert facts["knowledge_sufficient"] is False
    if not case["whatsapp_allowed"]:
        assert "wa.me" not in response


@pytest.mark.parametrize(
    "first, second, expected_template",
    [
        ("What subscriptions do you offer?", "What are the next steps to pre book?", "alacarte_prebooking_steps"),
        ("Tell me about Sondesh", "What payment methods do you accept?", "GUARD_payment_method"),
        ("Tell me about Sondesh", "What meals do you offer à la carte?", None),
    ],
)
def test_dirty_state_explicit_intent_wins(first, second, expected_template, monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "dirty_" + (expected_template or "unsupported_alacarte")
    session_memory.clear_session(user_id, "instagram")
    responder.match_and_respond(first, user_id=user_id, channel="instagram")
    response, matched, template_id, _images = responder.match_and_respond(
        second, user_id=user_id, channel="instagram",
    )
    facts = session_memory.get_facts(user_id, "instagram")
    if expected_template is None:
        assert matched is False
        assert template_id is None
        assert facts["decision_lane"] == "HUMAN_HANDOFF"
        assert "Manager of Kitchens" in response
    else:
        assert matched is True
        assert template_id == expected_template
        assert facts["decision_lane"] == "EXACT_APPROVED"
    assert response


def test_story_context_keeps_bengali_home_table_price_followup(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "dirty_story_home_table_price"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {
        "routing_id": 1,
        "source_type": "story",
        "active": True,
        "routing_topic": "Bengali Home Table",
        "priority_products": [],
    }

    response, matched, template_id, _images = responder.match_and_respond(
        "What's the price?", user_id=user_id, channel="instagram",
    )
    facts = session_memory.get_facts(user_id, "instagram")
    assert matched is True
    assert template_id == "bengali_home_table_pricing"
    assert facts["decision_lane"] == "EXACT_APPROVED"
    assert facts["source_context_used"] is True
    assert "₹1,500 per person" in response


def test_booking_state_does_not_hijack_explicit_payment_question(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "dirty_booking_payment"
    session_memory.clear_session(user_id, "instagram")
    responder.match_and_respond("I want to book this", user_id=user_id, channel="instagram")
    response, matched, template_id, _images = responder.match_and_respond(
        "What payment methods do you accept?", user_id=user_id, channel="instagram",
    )
    assert matched is True
    assert template_id == "GUARD_payment_method"
    assert "UPI" in response
