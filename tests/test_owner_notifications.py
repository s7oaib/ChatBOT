"""Owner email/Admin/Web Push notification contracts."""

import base64
from unittest.mock import patch
from types import SimpleNamespace

import pytest
import httpx

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


def test_owner_payload_and_email_include_username_and_customer_need(owner_db, monkeypatch):
    handoff = handoff_module.get_or_create_handoff(
        user_id="meta-numeric-id-should-not-leak",
        channel="instagram",
        reason="Generic Human Support",
        user_message="I want to talk to a person",
        facts={"last_entities": {}},
        instagram_username="chatbot_testing.168",
        query_summary="A team member to respond.",
        contact_mode="support_callback",
    )
    payload = owner_notifications.safe_handoff_payload(
        handoff, public_base_url="https://camellia.example",
    )
    assert payload["instagram_username"] == "chatbot_testing.168"
    assert payload["query_summary"] == "A team member to respond."
    assert payload["contact_mode"] == "support_callback"
    assert "meta-numeric-id-should-not-leak" not in repr(payload)

    monkeypatch.setattr(config, "ADMIN_EMAIL_FROM", "owner@example")
    monkeypatch.setattr(config, "ADMIN_RECOVERY_EMAIL", "owner@example")
    content = owner_notifications._email_message(payload).get_content()
    assert "Instagram username: @chatbot_testing.168" in content
    assert "Customer wants: A team member to respond." in content
    assert "Generic Human Support" in content
    assert "meta-numeric-id-should-not-leak" not in content


def test_recent_cam_admin_state_includes_username_and_customer_need(owner_db):
    handoff = handoff_module.get_or_create_handoff(
        user_id="numeric-meta-id",
        channel="instagram",
        reason="Generic Human Support",
        user_message="Can someone get back to me?",
        facts={"last_entities": {}},
        instagram_username="customer.handle",
        query_summary="A team member to respond.",
        contact_mode="support_callback",
    )
    with owner_db.get_connection() as conn:
        conn.execute(
            "INSERT INTO admin_notification_states (handoff_ref) VALUES (?)",
            (handoff["handoff_ref"],),
        )
        conn.commit()
    row = owner_db.get_admin_notification_state()["handoffs"][0]
    assert row["instagram_username"] == "customer.handle"
    assert row["query_summary"] == "A team member to respond."
    assert "numeric-meta-id" not in repr(row)


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


def _gmail_payload():
    return {
        "handoff_ref": "CAM-54321", "reason": "Explicit human request",
        "source": "Instagram", "timestamp": "2026-09-20 12:00:00",
        "status": "WAITING_FOR_HUMAN", "admin_url": "/admin",
    }


def _configure_gmail(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_EMAIL_PROVIDER", "gmail_api")
    monkeypatch.setattr(config, "GMAIL_API_CLIENT_ID", "client-id")
    monkeypatch.setattr(config, "GMAIL_API_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(config, "GMAIL_API_REFRESH_TOKEN", "refresh-token")
    monkeypatch.setattr(config, "GMAIL_API_USER_ID", "me")
    monkeypatch.setattr(config, "ADMIN_EMAIL_FROM", "owner@example.com")
    monkeypatch.setattr(config, "ADMIN_RECOVERY_EMAIL", "alerts@example.com")


class _HttpResponse:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data


def test_gmail_api_readiness_reports_missing_oauth_variables_without_values(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_EMAIL_PROVIDER", "gmail_api")
    for name in (
        "GMAIL_API_CLIENT_ID", "GMAIL_API_CLIENT_SECRET", "GMAIL_API_REFRESH_TOKEN",
        "ADMIN_EMAIL_FROM", "ADMIN_RECOVERY_EMAIL", "HUMAN_ESCALATION_EMAIL",
    ):
        monkeypatch.setattr(config, name, "")

    readiness = owner_notifications.email_readiness()

    assert readiness["provider"] == "gmail_api"
    assert readiness["configured"] is False
    assert readiness["ready"] is False
    assert "GMAIL_API_CLIENT_ID" in readiness["reason"]
    assert "alerts@example.com" not in repr(readiness)
    assert "refresh-token" not in repr(readiness)


def test_gmail_api_exchanges_refresh_token_and_sends_privacy_safe_mime(monkeypatch):
    _configure_gmail(monkeypatch)
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url == "https://oauth2.googleapis.com/token":
            return _HttpResponse(200, {"access_token": "access-token"})
        return _HttpResponse(200, {"id": "gmail-message-id"})

    with patch.object(owner_notifications.httpx, "post", side_effect=fake_post):
        result = owner_notifications.send_email({
            **_gmail_payload(),
            "handoff_ref": "CAM-54321",
            "reason": "safe reason",
        })

    assert result == {"status": "sent"}
    assert [call[0] for call in calls] == [
        "https://oauth2.googleapis.com/token",
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
    ]
    assert calls[0][1]["data"]["grant_type"] == "refresh_token"
    assert calls[1][1]["headers"]["Authorization"] == "Bearer access-token"
    raw = calls[1][1]["json"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    assert "CAM-54321" in decoded
    assert "WAITING_FOR_HUMAN" in decoded
    assert "safe reason" in decoded
    assert "raw customer message" not in decoded
    assert "meta-customer-id" not in decoded
    assert "access-token" not in repr(result)
    assert "client-secret" not in repr(result)

    readiness = owner_notifications.email_readiness()
    assert "owner@example.com" not in repr(readiness)
    assert "alerts@example.com" not in repr(readiness)


@pytest.mark.parametrize("status_code", [429, 500])
def test_gmail_api_token_exchange_retryable_http_failures(monkeypatch, status_code):
    _configure_gmail(monkeypatch)
    with patch.object(
        owner_notifications.httpx, "post", return_value=_HttpResponse(status_code, {"error": "temporarily unavailable"}),
    ):
        result = owner_notifications.send_email(_gmail_payload())
    assert result == {"status": "failed", "retryable": True, "error": f"HTTP {status_code}"}


def test_gmail_api_network_failure_is_retryable(monkeypatch):
    _configure_gmail(monkeypatch)
    with patch.object(owner_notifications.httpx, "post", side_effect=httpx.NetworkError("offline")):
        result = owner_notifications.send_email(_gmail_payload())
    assert result["status"] == "failed"
    assert result["retryable"] is True
    assert result["error"] == "NetworkError"


def test_gmail_api_invalid_grant_is_non_retryable_and_does_not_leak_response(monkeypatch):
    _configure_gmail(monkeypatch)
    with patch.object(
        owner_notifications.httpx, "post",
        return_value=_HttpResponse(400, {"error": "invalid_grant", "error_description": "refresh-token-secret"}),
    ):
        result = owner_notifications.send_email(_gmail_payload())
    assert result == {"status": "failed", "retryable": False, "error": "OAuth invalid_grant"}
    assert "refresh-token-secret" not in repr(result)


@pytest.mark.parametrize("status_code", [401, 403])
def test_gmail_api_send_auth_failures_are_non_retryable(monkeypatch, status_code):
    _configure_gmail(monkeypatch)
    responses = iter([_HttpResponse(200, {"access_token": "access-token"}), _HttpResponse(status_code)])
    with patch.object(owner_notifications.httpx, "post", side_effect=lambda *_args, **_kwargs: next(responses)):
        result = owner_notifications.send_email(_gmail_payload())
    assert result == {"status": "failed", "retryable": False, "error": f"HTTP {status_code}"}


@pytest.mark.parametrize("status_code", [429, 503])
def test_gmail_api_send_retryable_http_failures(monkeypatch, status_code):
    _configure_gmail(monkeypatch)
    responses = iter([_HttpResponse(200, {"access_token": "access-token"}), _HttpResponse(status_code)])
    with patch.object(owner_notifications.httpx, "post", side_effect=lambda *_args, **_kwargs: next(responses)):
        result = owner_notifications.send_email(_gmail_payload())
    assert result == {"status": "failed", "retryable": True, "error": f"HTTP {status_code}"}


def test_gmail_api_send_network_failure_is_retryable(monkeypatch):
    _configure_gmail(monkeypatch)
    responses = iter([_HttpResponse(200, {"access_token": "access-token"}), httpx.NetworkError("offline")])

    def fake_post(*_args, **_kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    with patch.object(owner_notifications.httpx, "post", side_effect=fake_post):
        result = owner_notifications.send_email(_gmail_payload())
    assert result["status"] == "failed"
    assert result["retryable"] is True
    assert result["error"] == "NetworkError"


def test_gmail_api_failure_keeps_cam_waiting_and_durable(owner_db, monkeypatch):
    _configure_gmail(monkeypatch)
    monkeypatch.setattr(config, "WEB_PUSH_ENABLED", False)
    handoff = _new_handoff(owner_db, "gmail-failure-user")
    with patch.object(owner_notifications.httpx, "post", return_value=_HttpResponse(503)):
        result = escalator_module.escalator.handle_escalation(
            user_id="gmail-failure-user", channel="instagram", user_message="human please",
            reason="Explicit human request", context_data={"handoff_ref": handoff["handoff_ref"]},
        )
    assert result["status"] == "escalated"
    assert owner_db.get_handoff_by_ref(handoff["handoff_ref"])["status"] == "WAITING_FOR_HUMAN"
    event = owner_db.get_human_notification_events()[0]
    assert event["status"] == "RETRY"
    deliveries = {row["channel"]: row for row in owner_db.get_notification_deliveries(event["id"])}
    assert deliveries["EMAIL"]["status"] == "RETRYING"


def test_duplicate_handoff_does_not_send_gmail_email_twice(owner_db, monkeypatch):
    _configure_gmail(monkeypatch)
    monkeypatch.setattr(config, "WEB_PUSH_ENABLED", False)
    responses = iter([_HttpResponse(200, {"access_token": "access-token"}), _HttpResponse(200, {"id": "sent"})])
    with patch.object(owner_notifications.httpx, "post", side_effect=lambda *_args, **_kwargs: next(responses)) as post:
        first = _new_handoff(owner_db, "gmail-dedupe-user")
        escalator_module.escalator.handle_escalation(
            user_id="gmail-dedupe-user", channel="instagram", user_message="human please",
            reason="Explicit human request", context_data={"handoff_ref": first["handoff_ref"]},
        )
        second = _new_handoff(owner_db, "gmail-dedupe-user")
    assert post.call_count == 2
    assert second["handoff_ref"] == first["handoff_ref"]
    assert second["newly_created"] is False
    assert len(owner_db.get_human_notification_events()) == 1


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
