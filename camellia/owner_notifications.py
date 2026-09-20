"""Safe owner-notification adapters for durable CAM handoff events.

This module deliberately contains no customer-channel logic.  It builds a
small allow-listed payload from the durable handoff row, then dispatches the
email and Web Push channels independently.  A provider failure is returned to
the outbox caller; it is never raised into Instagram handoff creation.
"""

import base64
import json
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote
import httpx

from camellia.config import config
from camellia.handoff import normalize_instagram_username
from camellia.monitoring import get_logger

log = get_logger("camellia.owner_notifications")

_GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users"


def _recipient() -> str:
    return (config.ADMIN_RECOVERY_EMAIL or config.HUMAN_ESCALATION_EMAIL or "").strip()


def _email_provider_readiness(provider: str, recipient: str, requirements: tuple) -> Dict[str, Any]:
    missing = [name for name, value in requirements if not value]
    return {
        "configured": not missing,
        "provider": provider,
        "recipient_configured": bool(recipient),
        "ready": not missing,
        "reason": ", ".join(missing) if missing else None,
    }


def email_readiness() -> Dict[str, Any]:
    provider = (config.ADMIN_EMAIL_PROVIDER or "").strip().lower()
    recipient = _recipient()
    if not provider and not config.ADMIN_EMAIL_SMTP_HOST:
        return {
            "configured": False,
            "provider": None,
            "recipient_configured": bool(recipient),
            "ready": False,
            "reason": "ADMIN_EMAIL_PROVIDER/ADMIN_EMAIL_SMTP_HOST is not configured",
        }
    if provider not in {"smtp", "resend", "gmail_api", ""}:
        return {
            "configured": bool(recipient),
            "provider": provider,
            "recipient_configured": bool(recipient),
            "ready": False,
            "reason": "email provider adapter is not supported by this build",
        }
    if provider == "resend":
        return _email_provider_readiness("resend", recipient, (
            ("ADMIN_EMAIL_API_KEY", config.ADMIN_EMAIL_API_KEY),
            ("ADMIN_EMAIL_FROM", config.ADMIN_EMAIL_FROM),
            ("ADMIN_RECOVERY_EMAIL or HUMAN_ESCALATION_EMAIL", recipient),
        ))
    if provider == "gmail_api":
        return _email_provider_readiness("gmail_api", recipient, (
            ("GMAIL_API_CLIENT_ID", config.GMAIL_API_CLIENT_ID),
            ("GMAIL_API_CLIENT_SECRET", config.GMAIL_API_CLIENT_SECRET),
            ("GMAIL_API_REFRESH_TOKEN", config.GMAIL_API_REFRESH_TOKEN),
            ("ADMIN_EMAIL_FROM", config.ADMIN_EMAIL_FROM),
            ("ADMIN_RECOVERY_EMAIL or HUMAN_ESCALATION_EMAIL", recipient),
        ))
    return _email_provider_readiness("smtp", recipient, (
        ("ADMIN_EMAIL_SMTP_HOST", config.ADMIN_EMAIL_SMTP_HOST),
        ("ADMIN_EMAIL_FROM", config.ADMIN_EMAIL_FROM),
        ("ADMIN_RECOVERY_EMAIL or HUMAN_ESCALATION_EMAIL", recipient),
    ))


def web_push_readiness(subscription_count: int) -> Dict[str, Any]:
    configured = bool(
        config.WEB_PUSH_ENABLED
        and config.WEB_PUSH_VAPID_PUBLIC_KEY
        and config.WEB_PUSH_VAPID_PRIVATE_KEY
        and config.WEB_PUSH_VAPID_SUBJECT
    )
    return {
        "enabled": bool(config.WEB_PUSH_ENABLED),
        "configured": configured,
        "public_key_available": bool(config.WEB_PUSH_VAPID_PUBLIC_KEY),
        "subscriber_count": int(subscription_count or 0),
        "ready": configured and int(subscription_count or 0) > 0,
        "reason": None if configured else "VAPID configuration is incomplete",
    }


def channel_health(
    readiness: Dict[str, Any],
    pulse: Optional[Dict[str, Any]],
    *,
    require_ready: bool,
) -> Dict[str, Any]:
    """Keep current configuration readiness separate from CAM history.

    ``pulse`` is durable delivery history, so it may describe a skipped or
    failed attempt made before the channel was configured.  That history stays
    visible, but it must not overwrite the current configuration assessment.
    The flat history fields remain for existing Admin/API consumers; the
    explicit nested blocks make the distinction unambiguous.
    """
    current = dict(readiness)
    history = dict(pulse or {})
    current.update({
        key: history.get(key)
        for key in ("last_status", "last_error", "last_updated_at", "failures_last_24h")
        if key in history
    })
    last_status = str(history.get("last_status") or "").upper() or None
    delivery_healthy = last_status not in {"FAILED", "RETRYING"}
    configuration_healthy = bool(readiness.get("configured"))
    current["configuration_healthy"] = configuration_healthy
    current["delivery_healthy"] = delivery_healthy
    current["healthy"] = bool(
        (readiness.get("ready") if require_ready else configuration_healthy)
        and delivery_healthy
    )
    current["configuration"] = {
        key: readiness.get(key)
        for key in ("enabled", "configured", "public_key_available", "subscriber_count", "ready", "reason")
        if key in readiness
    }
    current["last_delivery"] = {
        "status": history.get("last_status"),
        "error": history.get("last_error"),
        "updated_at": history.get("last_updated_at"),
    }
    return current


def safe_handoff_payload(handoff: Dict[str, Any], *, public_base_url: str = "") -> Dict[str, Any]:
    """Return only metadata approved for owner notification channels."""
    ref = str(handoff.get("handoff_ref") or "")
    reason = str(handoff.get("reason") or "Human handoff")[:160]
    created_at = str(handoff.get("created_at") or "")[:40]
    username = normalize_instagram_username(handoff.get("instagram_username")) or "Instagram customer"
    query_summary = str(handoff.get("query_summary") or "Customer needs human assistance.")[:220]
    contact_mode = str(handoff.get("contact_mode") or "support_callback")[:40]
    base = str(public_base_url or "").rstrip("/")
    admin_url = f"{base}/admin?handoff={ref}" if base and ref else "/admin"
    return {
        "title": "New Camellia handoff",
        "handoff_ref": ref,
        "reason": reason,
        "instagram_username": username,
        "query_summary": query_summary,
        "contact_mode": contact_mode,
        "source": "Instagram",
        "timestamp": created_at,
        "status": "WAITING_FOR_HUMAN",
        "admin_url": admin_url,
    }


def _email_message(payload: Dict[str, Any]) -> EmailMessage:
    message = EmailMessage()
    title = str(payload.get("title") or f"New Camellia handoff {payload['handoff_ref']}")
    username = str(payload.get("instagram_username") or "").strip()
    username_label = f"@{username}" if username and username != "Instagram customer" else "Instagram customer"
    message["Subject"] = title
    message["From"] = config.ADMIN_EMAIL_FROM
    message["To"] = _recipient()
    message.set_content(
        f"{title}\n\n"
        f"CAM reference: {payload['handoff_ref']}\n"
        f"Instagram username: {username_label}\n"
        f"Customer wants: {payload.get('query_summary') or 'Customer needs human assistance.'}\n"
        f"Reason: {payload['reason']}\n"
        f"Contact path: {payload.get('contact_mode') or 'support_callback'}\n"
        f"Source: {payload['source']}\n"
        f"Time: {payload['timestamp']}\n"
        f"Status: {payload['status']}\n\n"
        f"Open Admin: {payload['admin_url']}\n"
    )
    return message


def _http_failure(status_code: int) -> Dict[str, Any]:
    """Classify an HTTP failure without retaining a provider response body."""
    if status_code in (408, 429) or status_code >= 500:
        return {"status": "failed", "retryable": True, "error": f"HTTP {status_code}"}
    return {"status": "failed", "retryable": False, "error": f"HTTP {status_code}"}


def _gmail_access_token() -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Exchange the configured refresh token without exposing OAuth material."""
    try:
        response = httpx.post(
            _GMAIL_TOKEN_URL,
            data={
                "client_id": config.GMAIL_API_CLIENT_ID,
                "client_secret": config.GMAIL_API_CLIENT_SECRET,
                "refresh_token": config.GMAIL_API_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        return None, {"status": "failed", "retryable": True, "error": type(exc).__name__}

    if not 200 <= response.status_code < 300:
        failure = _http_failure(response.status_code)
        if not failure["retryable"] and response.status_code in (400, 401, 403):
            # These are configuration/authorization failures. The known OAuth
            # error code is safe to retain; the rest of Google's response is not.
            try:
                error_code = str(response.json().get("error") or "")
            except (TypeError, ValueError, AttributeError):
                error_code = ""
            if error_code in {"invalid_grant", "invalid_client", "unauthorized_client", "invalid_scope"}:
                failure["error"] = f"OAuth {error_code}"
        return None, failure

    try:
        access_token = response.json().get("access_token")
    except (TypeError, ValueError, AttributeError):
        access_token = None
    if not isinstance(access_token, str) or not access_token.strip():
        return None, {"status": "failed", "retryable": False, "error": "invalid OAuth token response"}
    return access_token, None


def _send_via_gmail_api(payload: Dict[str, Any]) -> Dict[str, Any]:
    access_token, failure = _gmail_access_token()
    if failure:
        return failure

    message = _email_message(payload)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
    user_id = quote(config.GMAIL_API_USER_ID or "me", safe="")
    try:
        response = httpx.post(
            f"{_GMAIL_SEND_URL}/{user_id}/messages/send",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"raw": raw},
            timeout=15,
        )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        return {"status": "failed", "retryable": True, "error": type(exc).__name__}

    if not 200 <= response.status_code < 300:
        return _http_failure(response.status_code)
    return {"status": "sent"}


def send_email(payload: Dict[str, Any]) -> Dict[str, Any]:
    readiness = email_readiness()
    if not readiness["ready"]:
        return {"status": "skipped", "retryable": False, "error": readiness["reason"]}
    try:
        if readiness["provider"] == "gmail_api":
            return _send_via_gmail_api(payload)
        if readiness["provider"] == "resend":
            username = str(payload.get("instagram_username") or "").strip()
            username_label = f"@{username}" if username and username != "Instagram customer" else "Instagram customer"
            response = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {config.ADMIN_EMAIL_API_KEY}"},
                json={
                    "from": config.ADMIN_EMAIL_FROM,
                    "to": [_recipient()],
                    "subject": str(payload.get("title") or f"New Camellia handoff {payload['handoff_ref']}"),
                    "text": (
                        f"{str(payload.get('title') or 'A new Camellia human handoff is waiting.')}\n\n"
                        f"CAM reference: {payload['handoff_ref']}\n"
                        f"Instagram username: {username_label}\n"
                        f"Customer wants: {payload.get('query_summary') or 'Customer needs human assistance.'}\n"
                        f"Reason: {payload['reason']}\n"
                        f"Contact path: {payload.get('contact_mode') or 'support_callback'}\n"
                        f"Source: {payload['source']}\n"
                        f"Time: {payload['timestamp']}\n"
                        f"Status: {payload['status']}\n\n"
                        f"Open Admin: {payload['admin_url']}\n"
                    ),
                },
                timeout=15,
            )
            if response.status_code in (408, 429) or response.status_code >= 500:
                return {"status": "failed", "retryable": True, "error": f"HTTP {response.status_code}"}
            if response.status_code >= 400:
                return {"status": "failed", "retryable": False, "error": f"HTTP {response.status_code}"}
            return {"status": "sent"}
        if config.ADMIN_EMAIL_SMTP_USE_TLS:
            with smtplib.SMTP(config.ADMIN_EMAIL_SMTP_HOST, config.ADMIN_EMAIL_SMTP_PORT, timeout=15) as client:
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
                if config.ADMIN_EMAIL_SMTP_USERNAME:
                    client.login(config.ADMIN_EMAIL_SMTP_USERNAME, config.ADMIN_EMAIL_SMTP_PASSWORD)
                client.send_message(_email_message(payload))
        else:
            with smtplib.SMTP(config.ADMIN_EMAIL_SMTP_HOST, config.ADMIN_EMAIL_SMTP_PORT, timeout=15) as client:
                if config.ADMIN_EMAIL_SMTP_USERNAME:
                    client.login(config.ADMIN_EMAIL_SMTP_USERNAME, config.ADMIN_EMAIL_SMTP_PASSWORD)
                client.send_message(_email_message(payload))
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, TimeoutError, OSError, httpx.TimeoutException, httpx.NetworkError) as exc:
        return {"status": "failed", "retryable": True, "error": type(exc).__name__}
    except smtplib.SMTPException as exc:
        code = getattr(exc, "smtp_code", None)
        return {"status": "failed", "retryable": bool(code and int(code) >= 500), "error": type(exc).__name__}
    except Exception as exc:
        log.exception("Owner email dispatch failed unexpectedly.")
        return {"status": "failed", "retryable": False, "error": type(exc).__name__}
    return {"status": "sent"}


def _remove_subscription(database: Any, subscription_id: int) -> None:
    try:
        database.revoke_web_push_subscription(subscription_id)
    except Exception:
        log.exception("Could not revoke an expired Web Push subscription.")


def send_web_push(payload: Dict[str, Any], database: Any, *, test: bool = False) -> Dict[str, Any]:
    subscriptions = database.get_active_web_push_subscriptions()
    readiness = web_push_readiness(len(subscriptions))
    if not readiness["configured"] or not subscriptions:
        return {"status": "skipped", "retryable": False, "error": readiness["reason"] or "no active subscriptions"}
    try:
        from pywebpush import webpush
    except ImportError:
        return {"status": "failed", "retryable": False, "error": "pywebpush is not installed"}

    if test:
        body_payload = {
            "title": "Camellia owner notification test",
            "body": f"Camellia owner notification test · {payload['timestamp']}",
            "url": payload["admin_url"],
            "timestamp": payload["timestamp"],
        }
    else:
        body_payload = {
            "title": payload["title"],
            "body": f"{payload['handoff_ref']} · {payload['reason']}",
            "url": payload["admin_url"],
            "handoff_ref": payload["handoff_ref"],
            "timestamp": payload["timestamp"],
        }
    body = json.dumps(body_payload, separators=(",", ":"))
    sent = 0
    retryable_failure = False
    permanent_failure = False
    for subscription in subscriptions:
        info = {
            "endpoint": subscription["endpoint"],
            "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
        }
        try:
            webpush(
                subscription_info=info,
                data=body,
                vapid_private_key=config.WEB_PUSH_VAPID_PRIVATE_KEY,
                vapid_claims={"sub": config.WEB_PUSH_VAPID_SUBJECT},
            )
            database.mark_web_push_subscription_used(subscription["id"])
            sent += 1
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in (404, 410):
                _remove_subscription(database, int(subscription["id"]))
            elif status_code in (408, 429) or (status_code and status_code >= 500):
                retryable_failure = True
            else:
                permanent_failure = True
            log.warning("Web Push delivery failed for subscription %s (%s).", subscription["id"], type(exc).__name__)
    if sent:
        return {"status": "sent", "retryable": retryable_failure, "error": None if not retryable_failure else "one or more subscriptions temporarily failed"}
    if retryable_failure:
        return {"status": "failed", "retryable": True, "error": "all Web Push subscriptions temporarily failed"}
    return {"status": "failed" if permanent_failure else "skipped", "retryable": False, "error": "all Web Push subscriptions rejected delivery" if permanent_failure else "no active subscriptions"}


def send_test_web_push(payload: Dict[str, Any], database: Any) -> Dict[str, Any]:
    """Send the isolated Admin transport test without creating a CAM event."""
    return send_web_push(payload, database, test=True)
