from camellia.auto_resolve import evaluate_eligibility
from camellia.database import Database


def _candidate(**overrides):
    value = {
        "channel": "instagram",
        "user_id": "u1",
        "customer_message": "What is your menu?",
        "triage_category": "AUTO_RESOLVE",
        "confidence": 0.99,
        "grounding_status": "Grounded",
        "matched_knowledge_id": "qa_templates.json#menu",
        "risk_flags": [],
        "suggested_answer": "Here is our approved menu.",
    }
    value.update(overrides)
    return value


def test_simulation_eligibility_is_deterministic_and_safe():
    result = evaluate_eligibility(_candidate())
    assert result["would_auto_resolve"] is True
    assert result["eligibility_reasons"] == ["all_gates_passed"]

    for message, flag in (("What is the price?", "price"), ("Can you deliver tomorrow?", "delivery_commitment")):
        blocked = evaluate_eligibility(_candidate(customer_message=message, risk_flags=[flag]))
        assert blocked["would_auto_resolve"] is False
        assert "risk_flags_present" in blocked["eligibility_reasons"]


def test_channel_source_and_order_guards_fail_closed():
    assert "instagram_only" in evaluate_eligibility(_candidate(channel="whatsapp"))["eligibility_reasons"]
    assert "source_route_precedence" in evaluate_eligibility(_candidate(source_route_present=True))["eligibility_reasons"]
    assert "protected_order_flow" in evaluate_eligibility(_candidate(protected_order_flow=True))["eligibility_reasons"]


def test_decision_record_is_idempotent_and_review_linked(tmp_path):
    database = Database(str(tmp_path / "auto.db"))
    first = database.record_auto_resolve_decision(
        assisted_suggestion_id=7, user_id="u1", channel="instagram", customer_message="menu",
        category="AUTO_RESOLVE", confidence=.99, grounding_status="Grounded",
        matched_knowledge_id="qa_templates.json#menu", risk_flags=[], eligibility="ELIGIBLE",
        eligibility_reasons=["all_gates_passed"], simulated=True, would_auto_resolve=True,
        idempotency_key="assisted:7", response_text="menu",
    )
    second = database.record_auto_resolve_decision(
        assisted_suggestion_id=7, user_id="u1", channel="instagram", customer_message="menu",
        category="AUTO_RESOLVE", confidence=.99, grounding_status="Grounded",
        matched_knowledge_id="qa_templates.json#menu", risk_flags=[], eligibility="ELIGIBLE",
        eligibility_reasons=["all_gates_passed"], simulated=True, would_auto_resolve=True,
        idempotency_key="assisted:7", response_text="menu",
    )
    assert first["id"] == second["id"]
    assert database.auto_resolve_evaluation()["simulated_eligible_cases"] == 1
