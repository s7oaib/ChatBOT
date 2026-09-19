"""Stateful suppression and explicit resume for Instagram handoffs."""

from unittest.mock import patch

import pytest

from camellia import app as app_module
from camellia import handoff as handoff_module
from camellia.handoff import get_or_create_handoff, mark_referred, mark_whatsapp_connected
from camellia.session_memory import session_memory


@pytest.fixture
def waiting_db(tmp_path, monkeypatch):
    from camellia.database import Database

    database = Database(str(tmp_path / "waiting.db"))
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(handoff_module, "db", database)
    return database


def _waiting_handoff(database, user_id="waiting-customer"):
    session_memory.clear_session(user_id, "instagram")
    handoff = get_or_create_handoff(
        user_id=user_id, channel="instagram", reason="Explicit human request",
        user_message="I need a person", facts=session_memory.get_facts(user_id, "instagram"),
    )
    mark_referred(handoff["handoff_ref"])
    return handoff


def test_waiting_customer_message_is_stored_without_ai_reply_or_new_escalation(waiting_db):
    user_id = "waiting-suppressed"
    _waiting_handoff(waiting_db, user_id)

    with patch.object(app_module.responder, "match_and_respond") as respond, patch.object(
        app_module, "send_instagram_reply"
    ) as send, patch.object(app_module.escalator, "handle_escalation") as escalate:
        app_module._process_instagram_event(user_id, "Are you there?", "ad-1")

    respond.assert_not_called()
    send.assert_not_called()
    escalate.assert_not_called()
    rows = waiting_db.get_conversations(user_id=user_id, channel="instagram")
    assert rows[0]["user_message"] == "Are you there?"
    assert rows[0]["bot_response"] == ""
    assert rows[0]["template_id"] == "waiting_for_human"
    assert len(waiting_db.get_handoffs(limit=20)) == 1


def test_whatsapp_connected_handoff_remains_suppressed(waiting_db):
    user_id = "waiting-whatsapp-connected"
    handoff = _waiting_handoff(waiting_db, user_id)
    mark_whatsapp_connected(handoff["handoff_ref"])

    with patch.object(app_module.responder, "match_and_respond") as respond, patch.object(
        app_module, "send_instagram_reply"
    ) as send:
        app_module._process_instagram_event(user_id, "I sent the reference", "ad-1")

    respond.assert_not_called()
    send.assert_not_called()
    assert waiting_db.get_handoff_by_ref(handoff["handoff_ref"])["status"] == "WHATSAPP_CONNECTED"


def test_only_explicit_operator_resume_restores_ai_and_is_idempotent(waiting_db, monkeypatch):
    user_id = "waiting-resume"
    handoff = _waiting_handoff(waiting_db, user_id)

    monkeypatch.setattr(app_module, "require_admin", lambda *_args, **_kwargs: None)
    first = app_module.resume_handoff(handoff["handoff_ref"], None, None)
    second = app_module.resume_handoff(handoff["handoff_ref"], None, None)

    assert first["handoff_status"] == "RESUMED"
    assert second["handoff_status"] == "RESUMED"
    assert waiting_db.get_active_handoff("instagram", user_id) is None

    with patch.object(
        app_module.responder, "match_and_respond",
        return_value=("hello", True, "greeting", None),
    ) as respond, patch.object(app_module, "send_instagram_reply") as send:
        app_module._process_instagram_event(user_id, "Hello again", "ad-1")

    respond.assert_called_once()
    send.assert_called_once()
    assert send.call_args.args[1] == "hello"


def test_resume_missing_handoff_is_not_silent(waiting_db, monkeypatch):
    monkeypatch.setattr(app_module, "require_admin", lambda *_args, **_kwargs: None)

    with pytest.raises(app_module.HTTPException) as exc:
        app_module.resume_handoff("CAM-99999", None, None)

    assert exc.value.status_code == 404
