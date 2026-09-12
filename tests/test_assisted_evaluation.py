import tempfile

from camellia.database import Database


def _row(db, **overrides):
    fields = {
        "user_id": "u", "channel": "instagram", "customer_message": "question",
        "triage_category": "AI_SUGGESTED", "confidence": 0.9,
        "grounding_status": "Grounded", "suggested_answer": "approved draft",
        "original_suggested_answer": "approved draft", "risk_flags": [],
    }
    fields.update(overrides)
    return db.create_assisted_suggestion(**fields)


def test_assisted_evaluation_rollups_and_buckets():
    db = Database(tempfile.mktemp(suffix=".db"))
    approved = _row(db, user_id="approved")
    edited = _row(db, user_id="edited", customer_message="edited q", confidence=0.75, risk_flags=["price"])
    rejected = _row(db, user_id="rejected", customer_message="rejected q", confidence=0.95, grounding_status="Not Grounded")
    human = _row(db, user_id="human", customer_message="human q", triage_category="HUMAN_REQUIRED", confidence=0.4)
    assert db.review_assisted_suggestion(approved, status="APPROVED", reviewed_by="a", final_answer="approved draft", resolution_action="APPROVED")
    assert db.review_assisted_suggestion(edited, status="EDITED_AND_APPROVED", reviewed_by="a", final_answer="a materially different factual answer", resolution_action="EDITED_AND_APPROVED")
    db.record_assisted_edit_metrics(edited, "approved draft", "a materially different factual answer")
    assert db.review_assisted_suggestion(rejected, status="REJECTED", reviewed_by="a", rejection_reason="Wrong Fact", resolution_action="REJECTED")
    assert db.review_assisted_suggestion(human, status="SENT_TO_HUMAN", reviewed_by="a", resolution_action="SENT_TO_HUMAN")

    evaluation = db.assisted_evaluation()
    performance = evaluation["performance"]
    assert performance["suggestions_generated"] == 4
    assert performance["reviewed"] == 4
    assert performance["approved_as_is"] == 1
    assert performance["edited_and_approved"] == 1
    assert performance["rejected"] == 1
    assert performance["sent_to_human"] == 1
    assert evaluation["confidence_buckets"]["85-94%"]["approved_as_is"] == 1
    assert evaluation["confidence_buckets"]["95-100%"]["rejected"] == 1
    assert evaluation["category_breakdown"]["HUMAN_REQUIRED"]["human_handoff"] == 1
    assert evaluation["grounding_breakdown"]["Not Grounded"]["rejected"] == 1
    assert evaluation["risk_breakdown"]["Price"]["edited"] == 1
    assert len(evaluation["high_confidence_problems"]) == 1
    assert evaluation["high_confidence_problems"][0]["rejection_reason"] == "Wrong Fact"
    assert evaluation["safety"]["source_routing_overrides"] == 0
    assert evaluation["safety"]["whatsapp_assisted_activity"] == 0


def test_edit_severity_is_deterministic():
    db = Database(tempfile.mktemp(suffix=".db"))
    sid = _row(db, original_suggested_answer="A verified answer")
    assert db.review_assisted_suggestion(sid, status="EDITED_AND_APPROVED", reviewed_by="a", final_answer="A verified answer with a factual correction", resolution_action="EDITED_AND_APPROVED")
    metrics = db.record_assisted_edit_metrics(sid, "A verified answer", "A verified answer with a factual correction")
    assert metrics["edit_severity"] in {"Minor Edit", "Moderate Edit", "Major Edit"}
