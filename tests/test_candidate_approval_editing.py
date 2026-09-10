from camellia.candidate_safety import candidate_eligibility, validate_candidate_answer
from camellia.database import Database


def test_candidate_can_be_edited_reedited_and_stays_pending(tmp_path):
    database = Database(str(tmp_path / "candidates.db"))
    candidate_id = database.save_candidate_template("delivery", "Original approved draft", "Do you deliver on Sunday?", confidence=0.7)
    assert database.edit_candidate_template(candidate_id, "First edited answer", "editor-one")
    assert database.edit_candidate_template(candidate_id, "Final edited answer", "editor-two")
    row = database.get_candidate_template(candidate_id)
    assert row["status"] == "PENDING_APPROVAL"
    assert row["original_draft"] == "Original approved draft"
    assert row["response"] == "Final edited answer"
    assert row["edited"] is True and row["edited_by"] == "editor-two" and row["edited_at"]


def test_approval_audit_records_approver_without_losing_edit_audit(tmp_path):
    database = Database(str(tmp_path / "approval.db"))
    candidate_id = database.save_candidate_template("packaging", "Original packaging answer", "What packaging do you use?")
    database.edit_candidate_template(candidate_id, "Verified current packaging answer", "staff-editor")
    assert database.approve_candidate_template(candidate_id, "staff-approver")
    row = database.get_candidate_template(candidate_id)
    assert row["status"] == "APPROVED" and row["approved_by"] == "staff-approver" and row["approved_at"]
    assert row["response"] == "Verified current packaging answer" and row["original_draft"] == "Original packaging answer"


def test_pending_candidates_are_deduplicated_by_normalized_question(tmp_path):
    database = Database(str(tmp_path / "dedupe.db"))
    first = database.save_candidate_template("delivery", "Draft one", "Do you deliver to Gurgaon?")
    second = database.save_candidate_template("delivery", "Draft two", "do you deliver to gurgaon")
    assert first == second
    assert len(database.get_pending_candidates()) == 1


def test_action_session_and_outreach_content_never_enters_queue(tmp_path):
    database = Database(str(tmp_path / "excluded.db"))
    probes = [
        ("I need an agent", "A person will assist"),
        ("Cancel my order", "Your current order will be cancelled"),
        ("Collaboration proposal", "I created a reel for you"),
    ]
    assert all(database.save_candidate_template("general", answer, question) is None for question, answer in probes)
    assert database.get_pending_candidates() == []


def test_existing_unsafe_rows_are_hidden_and_not_eligible(tmp_path):
    database = Database(str(tmp_path / "legacy.db"))
    with database.get_connection() as conn:
        conn.execute("INSERT INTO candidate_templates(name,user_message,response) VALUES('legacy','I need an agent','We will call you')")
        conn.commit()
    assert database.get_pending_candidates() == []
    assert candidate_eligibility("I need an agent", "We will call you")[0] is False


def test_empty_short_long_secret_and_debug_answers_are_rejected():
    answers = ["", "x", "a" * 8001, "debug: traceback follows", "API key: abc123", "password is hunter2"]
    assert all(validate_candidate_answer(answer)[0] is False for answer in answers)
    assert validate_candidate_answer("A concise, reviewed answer.")[0] is True


def test_dashboard_exposes_edit_cancel_save_and_clear_confidence_label():
    from camellia import app
    html = app.get_admin_dashboard().body.decode()
    for text in ("AI Draft Confidence", "beginCandidateEdit", "saveCandidateEdit", "cancelCandidateEdit", "edited_by"):
        assert text in html
