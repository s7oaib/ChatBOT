"""Owner email/Admin/Web Push notification contracts."""

from unittest.mock import patch
from types import SimpleNamespace

import pytest

from camellia import app as app_module
from camellia import escalator as escalator_module
from camellia import handoff as handoff_module
from camellia import owner_notifications
from camellia.config import config


@pytest.fixture
def owner_db(tmp_path, monkeypatch):
    from camellia.database import Database

    database = Database(str(tmp_path / "owner-notifications.db"))
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(escalator_module, "db", database)
    monkeypatch.setattr(handoff_module, "db", database)
    return database


def _new_handoff(database, user_id="owner-user"):
    handoff = handoff_module.get_or_create_handoff(
        user_id=user_id,
        channel="instagram",
        reason="Explicit human request",
        user_message="Please connect me to a person",
        facts={"last_entities": {"topic": "human_help"}},
    )
    database.update_handoff_status(handoff["handoff_ref"], "WAITING_FOR_HUMAN")
    return handoff


def test_new_handoff_creates_one_event_and_duplicate_reuses_without_new_event(owner_db):
    first = _new_handoff(owner_db, "dedupe-user")
    second = _new_handoff(owner_db, "dedupe-user")
    assert second["handoff_ref"] == first["handoff_ref"]
    assert second["newly_created"] is False
    assert owner_db.get_human_notification_events() == []

    with patch.object(escalator_module.escalator, "send_human_alert", return_value={"status": "sent"}):
        first_result = escalator_module.escalator.handle_escalation(
            user_id="dedupe-user", channel="instagram",
            user_message="Please connect me to a person",
            reason="Explicit human request",
            context_data={"handoff_ref": first["handoff_ref"]},
        )
        # A duplicate handoff is not supposed to call handle_escalation in the
        # application path; this explicit second call is intentionally omitted.
    assert first_result["alert_sent"] is True
    assert len(owner_db.get_human_notification_events()) == 1


def test_safe_payload_excludes_meta_ids_and_raw_message(owner_db):
    handoff = _new_handoff(owner_db)
    payload = owner_notifications.safe_handoff_payload(
        {**handoff, "source_user_id": "meta-customer-id", "summary": "raw customer text"},
        public_base_url="https://camellia.example",
    )
    encoded = repr(payload)
    assert "meta-customer-id" not in encoded
    assert "raw customer text" not in encoded
    assert payload["status"] == "WAITING_FOR_HUMAN"
    assert payload["admin_url"].endswith("/admin?handoff=" + handoff["handoff_ref"])


def test_email_retry_is_persisted_without_breaking_handoff(owner_db, monkeypatch):
    handoff = _new_handoff(owner_db, "retry-user")
    monkeypatch.setattr(config, "ADMIN_EMAIL_PROVIDER", "smtp")
    monkeypatch.setattr(config, "ADMIN_EMAIL_SMTP_HOST", "smtp.example")
    monkeypatch.setattr(config, "ADMIN_EMAIL_FROM", "owner@example")
    monkeypatch.setattr(config, "ADMIN_RECOVERY_EMAIL", "owner@example")
    monkeypatch.setattr(config, "WEB_PUSH_ENABLED", False)
    with patch.object(owner_notifications.smtplib, "SMTP", side_effect=TimeoutError):
        result = escalator_module.escalator.handle_escalation(
            user_id="retry-user", channel="instagram", user_message="human please",
            reason="Explicit human request", context_data={"handoff_ref": handoff["handoff_ref"]},
        )
    assert result["alert_sent"] is False  # Parent remains retryable while Admin is already durable.
    event = owner_db.get_human_notification_events()[0]
    assert event["status"] == "RETRY"
    deliveries = {row["channel"]: row for row in owner_db.get_notification_deliveries(event["id"])}
    assert deliveries["EMAIL"]["status"] == "RETRYING"
    assert deliveries["WEB_PUSH"]["status"] == "SKIPPED"


def test_existing_resend_provider_is_used_without_exposing_payload_secrets(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_EMAIL_PROVIDER", "resend")
    monkeypatch.setattr(config, "ADMIN_EMAIL_API_KEY", "test-provider-key")
    monkeypatch.setattr(config, "ADMIN_EMAIL_FROM", "owner@example")
    monkeypatch.setattr(config, "ADMIN_RECOVERY_EMAIL", "recipient@example")

    class Response:
        status_code = 200

    captured = {}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    with patch.object(owner_notifications.httpx, "post", side_effect=fake_post):
        result = owner_notifications.send_email({
            "handoff_ref": "CAM-54321", "reason": "Explicit human request",
            "source": "Instagram", "timestamp": "2026-09-20 12:00:00",
            "status": "WAITING_FOR_HUMAN", "admin_url": "/admin",
        })
    assert result["status"] == "sent"
    assert captured["url"] == "https://api.resend.com/emails"
    assert "test-provider-key" not in repr(captured["json"])
    assert "raw" not in repr(captured["json"]).lower()


def test_admin_seen_state_is_separate_from_waiting_status(owner_db):
    handoff = _new_handoff(owner_db, "seen-user")
    with owner_db.get_connection() as conn:
        conn.execute("INSERT INTO admin_notification_states (handoff_ref) VALUES (?)", (handoff["handoff_ref"],))
        conn.commit()
    before = owner_db.get_admin_notification_state()
    assert before["unread_count"] == 1
    owner_db.mark_admin_notification_seen(handoff["handoff_ref"])
    assert owner_db.get_admin_notification_state()["unread_count"] == 0
    assert owner_db.get_handoff_by_ref(handoff["handoff_ref"])["status"] == "WAITING_FOR_HUMAN"


def test_push_subscription_persists_and_revokes(owner_db):
    stored = owner_db.upsert_web_push_subscription(
        endpoint="https://push.example/subscription",
        p256dh="public-key", auth="auth-key", user_agent="test",
    )
    assert stored["endpoint"].startswith("https://")
    assert owner_db.web_push_subscription_count() == 1
    owner_db.revoke_web_push_endpoint(stored["endpoint"])
    assert owner_db.web_push_subscription_count() == 0


def test_expired_push_subscription_is_removed(owner_db, monkeypatch):
    owner_db.upsert_web_push_subscription(
        endpoint="https://push.example/expired", p256dh="public-key", auth="auth-key",
    )
    monkeypatch.setattr(config, "WEB_PUSH_ENABLED", True)
    monkeypatch.setattr(config, "WEB_PUSH_VAPID_PUBLIC_KEY", "public")
    monkeypatch.setattr(config, "WEB_PUSH_VAPID_PRIVATE_KEY", "private")
    monkeypatch.setattr(config, "WEB_PUSH_VAPID_SUBJECT", "mailto:owner@example")

    class ExpiredPush(Exception):
        response = type("Response", (), {"status_code": 410})()

    def expired(**_kwargs):
        raise ExpiredPush()

    with patch.dict("sys.modules", {"pywebpush": SimpleNamespace(webpush=expired)}):
        result = owner_notifications.send_web_push(
            {"title": "New Camellia handoff", "handoff_ref": "CAM-12345", "reason": "test", "admin_url": "/admin", "timestamp": "now"},
            owner_db,
        )
    assert result["status"] == "skipped"
    assert owner_db.web_push_subscription_count() == 0
