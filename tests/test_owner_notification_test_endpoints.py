import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia import owner_notifications
from camellia.config import config
from camellia.database import Database


ADMIN_KEY = "owner_notification_test_admin_key_98765"


@pytest.fixture
def admin_test_client(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "owner-notification-endpoints.db"))
    database.init_db()
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(config, "ADMIN_API_KEY", ADMIN_KEY)
    monkeypatch.setattr(config, "ADMIN_EMAIL_PROVIDER", "gmail_api")
    with TestClient(app_module.app) as client:
        yield client, database


def _counts(database):
    with database.get_connection() as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("cross_channel_handoffs", "human_notification_events", "escalations")
        }


def test_test_email_requires_admin_auth(admin_test_client):
    client, _ = admin_test_client
    assert client.post("/admin/notifications/test-email").status_code == 403


def test_test_email_uses_production_transport_and_has_no_cam_side_effects(admin_test_client):
    client, database = admin_test_client
    before = _counts(database)
    captured = {}

    def fake_send(payload):
        captured.update(payload)
        return {"status": "sent", "retryable": False}

    with patch.object(app_module.owner_notifications, "send_email", side_effect=fake_send) as send:
        response = client.post(
            "/admin/notifications/test-email", headers={"x-admin-key": ADMIN_KEY}
        )

    assert response.status_code == 200
    assert response.json() == {"status": "SENT", "provider": "gmail_api", "retryable": False}
    send.assert_called_once()
    assert captured["title"] == "Camellia owner notification test"
    assert captured["handoff_ref"] == "TEST-EMAIL"
    assert captured["source"] == "Admin"
    assert captured["status"] == "TEST"
    assert _counts(database) == before


def test_test_email_failure_is_safe_and_does_not_leak_provider_data(admin_test_client):
    client, _ = admin_test_client
    with patch.object(
        app_module.owner_notifications,
        "send_email",
        return_value={"status": "failed", "retryable": False, "error": "OAuth invalid_grant"},
    ):
        response = client.post(
            "/admin/notifications/test-email", headers={"x-admin-key": ADMIN_KEY}
        )

    assert response.json() == {
        "status": "FAILED",
        "provider": "gmail_api",
        "retryable": False,
        "error_class": "OAuth invalid_grant",
    }
    assert "access_token" not in response.text
    assert "refresh_token" not in response.text
    assert "client_secret" not in response.text


def test_test_email_does_not_fall_back_to_a_different_provider(admin_test_client, monkeypatch):
    client, _ = admin_test_client
    monkeypatch.setattr(config, "ADMIN_EMAIL_PROVIDER", "smtp")
    with patch.object(app_module.owner_notifications, "send_email") as send:
        response = client.post(
            "/admin/notifications/test-email", headers={"x-admin-key": ADMIN_KEY}
        )

    assert response.json() == {
        "status": "SKIPPED",
        "provider": "gmail_api",
        "retryable": False,
        "error_class": "provider_not_gmail_api",
    }
    send.assert_not_called()


def test_vapid_readiness_requires_all_values_and_a_subscription(monkeypatch):
    monkeypatch.setattr(config, "WEB_PUSH_ENABLED", True)
    monkeypatch.setattr(config, "WEB_PUSH_VAPID_PUBLIC_KEY", "public")
    monkeypatch.setattr(config, "WEB_PUSH_VAPID_PRIVATE_KEY", "private")
    monkeypatch.setattr(config, "WEB_PUSH_VAPID_SUBJECT", "mailto:owner@example.test")

    assert owner_notifications.web_push_readiness(0) == {
        "enabled": True,
        "configured": True,
        "public_key_available": True,
        "subscriber_count": 0,
        "ready": False,
        "reason": None,
    }
    assert owner_notifications.web_push_readiness(1)["ready"] is True

    monkeypatch.setattr(config, "WEB_PUSH_VAPID_PRIVATE_KEY", "")
    incomplete = owner_notifications.web_push_readiness(1)
    assert incomplete["configured"] is False
    assert incomplete["ready"] is False


def test_channel_health_keeps_stale_skipped_delivery_separate_from_current_readiness():
    readiness = {
        "enabled": True,
        "configured": True,
        "public_key_available": True,
        "subscriber_count": 1,
        "ready": True,
        "reason": None,
    }
    result = owner_notifications.channel_health(
        readiness,
        {
            "last_status": "SKIPPED",
            "last_error": "VAPID configuration is incomplete",
            "last_updated_at": "2026-09-19 22:22:24",
            "failures_last_24h": 0,
        },
        require_ready=False,
    )

    assert result["configured"] is True
    assert result["ready"] is True
    assert result["configuration_healthy"] is True
    assert result["healthy"] is True
    assert result["configuration"]["reason"] is None
    assert result["last_delivery"] == {
        "status": "SKIPPED",
        "error": "VAPID configuration is incomplete",
        "updated_at": "2026-09-19 22:22:24",
    }


def test_test_push_requires_auth_and_uses_only_synthetic_payload(admin_test_client):
    client, database = admin_test_client
    before = _counts(database)
    captured = {}

    def fake_push(payload, db):
        captured.update(payload)
        assert db is database
        return {"status": "sent", "retryable": False}

    with patch.object(app_module.owner_notifications, "send_test_web_push", side_effect=fake_push) as send:
        assert client.post("/admin/notifications/test-push").status_code == 403
        response = client.post(
            "/admin/notifications/test-push", headers={"x-admin-key": ADMIN_KEY}
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "SENT",
        "provider": "web_push",
        "retryable": False,
        "subscriber_count": 0,
    }
    send.assert_called_once()
    assert set(captured) == {
        "title", "handoff_ref", "reason", "source", "timestamp", "status", "admin_url"
    }
    assert captured["title"] == "Camellia owner notification test"
    assert captured["handoff_ref"] == "TEST-PUSH"
    assert captured["source"] == "Admin"
    assert captured["status"] == "TEST"
    assert _counts(database) == before


def test_test_push_transport_body_has_no_cam_or_customer_fields(admin_test_client, monkeypatch):
    _, database = admin_test_client
    database.upsert_web_push_subscription(
        endpoint="https://push.example/test", p256dh="public-key", auth="auth-key",
    )
    monkeypatch.setattr(config, "WEB_PUSH_ENABLED", True)
    monkeypatch.setattr(config, "WEB_PUSH_VAPID_PUBLIC_KEY", "public")
    monkeypatch.setattr(config, "WEB_PUSH_VAPID_PRIVATE_KEY", "private")
    monkeypatch.setattr(config, "WEB_PUSH_VAPID_SUBJECT", "mailto:owner@example.test")
    captured = {}

    def fake_webpush(**kwargs):
        captured.update(kwargs)

    with patch.dict("sys.modules", {"pywebpush": SimpleNamespace(webpush=fake_webpush)}):
        result = owner_notifications.send_test_web_push(
            {"timestamp": "2026-09-20T12:00:00Z", "admin_url": "/admin"}, database,
        )

    assert result["status"] == "sent"
    body = json.loads(captured["data"])
    assert set(body) == {"title", "body", "url", "timestamp"}
    assert body["title"] == "Camellia owner notification test"
    assert body["body"] == "Camellia owner notification test · 2026-09-20T12:00:00Z"
    assert body["url"] == "/admin"
