"""Durable, bounded staff-notification delivery tests."""

from unittest.mock import patch

import pytest

from camellia import escalator as escalator_module


@pytest.fixture
def notification_db(tmp_path, monkeypatch):
    from camellia.database import Database

    database = Database(str(tmp_path / "notifications.db"))
    monkeypatch.setattr(escalator_module, "db", database)
    return database


def _escalation(database, *, user_id="notification-user"):
    return database.record_escalation(
        user_id=user_id,
        channel="instagram",
        user_message="I need a person",
        reason="Unmatched Instagram Query",
        context_data={"handoff_ref": "CAM-12345"},
    )


def test_escalation_outbox_is_idempotent(notification_db):
    escalation_id = _escalation(notification_db)

    first = notification_db.enqueue_human_notification(escalation_id)
    second = notification_db.enqueue_human_notification(escalation_id)

    assert first["id"] == second["id"]
    assert len(notification_db.get_human_notification_events()) == 1


def test_successful_notification_is_sent_once(notification_db):
    escalation_id = _escalation(notification_db)
    event = notification_db.enqueue_human_notification(escalation_id)

    with patch.object(
        escalator_module.escalator, "send_human_alert",
        return_value={"status": "sent"},
    ) as send:
        assert escalator_module.escalator.process_pending_notifications() == 1
        assert escalator_module.escalator.process_pending_notifications() == 0

    assert send.call_count == 1
    assert notification_db.get_human_notification_event(event["id"])["status"] == "SENT"


def test_retryable_provider_failure_is_bounded_and_persisted(notification_db):
    escalation_id = _escalation(notification_db)
    event = notification_db.enqueue_human_notification(escalation_id)

    with patch.object(
        escalator_module.escalator, "send_human_alert",
        return_value={"status": "failed", "retryable": True, "error": "HTTP 503"},
    ) as send:
        assert escalator_module.escalator.process_pending_notifications() == 0
        # Backoff prevents an identical retry storm on the next customer turn.
        assert escalator_module.escalator.process_pending_notifications() == 0

    assert send.call_count == 1
    stored = notification_db.get_human_notification_event(event["id"])
    assert stored["status"] == "RETRY"
    assert stored["attempts"] == 1
    assert stored["last_error"] == "HTTP 503"


def test_non_retryable_provider_failure_is_terminal(notification_db):
    escalation_id = _escalation(notification_db)
    event = notification_db.enqueue_human_notification(escalation_id)

    with patch.object(
        escalator_module.escalator, "send_human_alert",
        return_value={"status": "failed", "retryable": False, "error": "HTTP 400"},
    ) as send:
        assert escalator_module.escalator.process_pending_notifications() == 0
        assert escalator_module.escalator.process_pending_notifications() == 0

    assert send.call_count == 1
    stored = notification_db.get_human_notification_event(event["id"])
    assert stored["status"] == "FAILED"
    assert stored["attempts"] == 1


def test_handle_escalation_records_before_alert_and_returns_delivery_state(notification_db):
    with patch.object(
        escalator_module.escalator, "send_human_alert",
        return_value={"status": "sent"},
    ):
        result = escalator_module.escalator.handle_escalation(
            user_id="notification-handle",
            channel="instagram",
            user_message="Please connect me to a person",
            reason="Explicit human request",
            context_data={},
        )

    assert result["alert_sent"] is True
    events = notification_db.get_human_notification_events()
    assert len(events) == 1
    assert events[0]["status"] == "SENT"
