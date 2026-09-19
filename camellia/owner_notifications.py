"""Safe owner-notification adapters for durable CAM handoff events.

This module deliberately contains no customer-channel logic.  It builds a
small allow-listed payload from the durable handoff row, then dispatches the
email and Web Push channels independently.  A provider failure is returned to
the outbox caller; it is never raised into Instagram handoff creation.
"""

import json
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Dict, Iterable

from camellia.config import config
from camellia.monitoring import get_logger

log = get_logger("camellia.owner_notifications")


def _recipient() -> str:
    return (config.ADMIN_RECOVERY_EMAIL or config.HUMAN_ESCALATION_EMAIL or "").strip()


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
    if provider not in {"smtp", ""}:
        return {
            "configured": bool(recipient),
            "provider": provider,
            "recipient_configured": bool(recipient),
            "ready": False,
            "reason": "email provider adapter is not supported by this build",
        }
    missing = []
    if not config.ADMIN_EMAIL_SMTP_HOST:
        missing.append("ADMIN_EMAIL_SMTP_HOST")
    if not config.ADMIN_EMAIL_FROM:
        missing.append("ADMIN_EMAIL_FROM")
    if not recipient:
        missing.append("ADMIN_RECOVERY_EMAIL or HUMAN_ESCALATION_EMAIL")
    return {
        "configured": not missing,
        "provider": "smtp",
        "recipient_configured": bool(recipient),
        "ready": not missing,
        "reason": ", ".join(missing) if missing else None,
    }


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


def safe_handoff_payload(handoff: Dict[str, Any], *, public_base_url: str = "") -> Dict[str, Any]:
    """Return only metadata approved for owner notification channels."""
    ref = str(handoff.get("handoff_ref") or "")
    reason = str(handoff.get("reason") or "Human handoff")[:160]
    created_at = str(handoff.get("created_at") or "")[:40]
    base = str(public_base_url or "").rstrip("/")
    admin_url = f"{base}/admin?handoff={ref}" if base and ref else "/admin"
    return {
        "title": "New Camellia handoff",
        "handoff_ref": ref,
        "reason": reason,
        "source": "Instagram",
        "timestamp": created_at,
        "status": "WAITING_FOR_HUMAN",
        "admin_url": admin_url,
    }


def _email_message(payload: Dict[str, Any]) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"New Camellia handoff {payload['handoff_ref']}"
    message["From"] = config.ADMIN_EMAIL_FROM
    message["To"] = _recipient()
    message.set_content(
        "A new Camellia human handoff is waiting.\n\n"
        f"CAM reference: {payload['handoff_ref']}\n"
        f"Reason: {payload['reason']}\n"
        f"Source: {payload['source']}\n"
        f"Time: {payload['timestamp']}\n"
        f"Status: {payload['status']}\n\n"
        f"Open Admin: {payload['admin_url']}\n"
    )
    return message


def send_email(payload: Dict[str, Any]) -> Dict[str, Any]:
    readiness = email_readiness()
    if not readiness["ready"]:
        return {"status": "skipped", "retryable": False, "error": readiness["reason"]}
    try:
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
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
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


def send_web_push(payload: Dict[str, Any], database: Any) -> Dict[str, Any]:
    subscriptions = database.get_active_web_push_subscriptions()
    readiness = web_push_readiness(len(subscriptions))
    if not readiness["configured"] or not subscriptions:
        return {"status": "skipped", "retryable": False, "error": readiness["reason"] or "no active subscriptions"}
    try:
        from pywebpush import webpush
    except ImportError:
        return {"status": "failed", "retryable": False, "error": "pywebpush is not installed"}

    body = json.dumps({
        "title": payload["title"],
        "body": f"{payload['handoff_ref']} · {payload['reason']}",
        "url": payload["admin_url"],
        "handoff_ref": payload["handoff_ref"],
        "timestamp": payload["timestamp"],
    }, separators=(",", ":"))
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

