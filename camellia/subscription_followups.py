"""Durable, Instagram-only Subscription follow-up scheduling.

The client-approved follow-up copy is intentionally not bundled in this
phase. A resolver is called at send time, so a later approved template can
return text and the same media payload shape used by normal Instagram sends.
"""

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

from camellia.database import db

log = logging.getLogger("camellia.subscription_followups")

FOLLOWUP_TYPE = "subscription_24h_followup"
INSTAGRAM_CHANNEL = "instagram"
FOLLOWUP_DELAY = timedelta(hours=24)
RETRY_DELAY = timedelta(minutes=5)


def utc_now() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def iso_time(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def followup_idempotency_key(
    customer_id: str,
    channel: str = INSTAGRAM_CHANNEL,
    followup_type: str = FOLLOWUP_TYPE,
) -> str:
    """Return a stable opaque key without putting a customer ID in logs."""
    material = "\x00".join((str(customer_id), str(channel), str(followup_type)))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{followup_type}:{digest}"


def schedule_subscription_followup(
    *,
    customer_id: str,
    successful_send_at: Optional[datetime] = None,
    source_conversation_id: Optional[int] = None,
    source_event_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist one 24-hour task; replay returns the original row."""
    sent_at = successful_send_at or utc_now()
    key = followup_idempotency_key(customer_id)
    row = db.schedule_subscription_followup(
        customer_id=str(customer_id),
        channel=INSTAGRAM_CHANNEL,
        followup_type=FOLLOWUP_TYPE,
        scheduled_for=iso_time(sent_at + FOLLOWUP_DELAY),
        idempotency_key=key,
        source_conversation_id=source_conversation_id,
        source_event_id=source_event_id,
    )
    log.info(
        "Subscription follow-up scheduled: type=%s status=%s due=%s key=%s",
        FOLLOWUP_TYPE, row.get("status"), row.get("scheduled_for"), key[-12:],
    )
    return row


def _safe_error(error: Any) -> str:
    message = str(error or "send failed")
    return message.replace("Bearer ", "Bearer [redacted] ")[:500]


def _send_succeeded(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and not result.get("error")
        and str(result.get("status") or "").lower() not in {"failed", "error"}
    )


def run_due_subscription_followups(
    *,
    sender: Callable[..., Dict[str, Any]],
    template_resolver: Callable[[str], Optional[Dict[str, Any]]],
    now: Optional[datetime] = None,
    limit: int = 20,
    is_eligible: Optional[Callable[[Dict[str, Any]], bool]] = None,
    should_suppress: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Dict[str, int]:
    """Claim and process due tasks without ever using WhatsApp.

    ``sender`` and ``template_resolver`` are injected to keep this service
    testable. Missing final copy is deferred, never replaced with a customer-
    visible placeholder.
    """
    current = now or utc_now()
    now_text = iso_time(current)
    stale_before = iso_time(current - RETRY_DELAY)
    result = {"claimed": 0, "sent": 0, "deferred": 0, "failed": 0, "cancelled": 0}

    for _ in range(max(0, int(limit))):
        claim_token = hashlib.sha256(
            f"{now_text}:{_}".encode("utf-8")
        ).hexdigest()
        task = db.claim_due_subscription_followup(
            now=now_text, claim_token=claim_token, stale_before=stale_before,
        )
        if not task:
            break
        result["claimed"] += 1
        log.info(
            "Subscription follow-up claimed: id=%s type=%s status=%s",
            task.get("id"), task.get("followup_type"), task.get("status"),
        )

        if task.get("channel") != INSTAGRAM_CHANNEL:
            db.cancel_subscription_followup(int(task["id"]), "unsupported_channel")
            result["cancelled"] += 1
            log.warning("Subscription follow-up %s cancelled: unsupported channel", task.get("id"))
            continue
        if is_eligible is not None and not is_eligible(task):
            db.cancel_subscription_followup(int(task["id"]), "customer_or_channel_ineligible")
            result["cancelled"] += 1
            log.info("Subscription follow-up %s cancelled: no longer eligible", task.get("id"))
            continue
        if should_suppress is not None and should_suppress(task):
            db.cancel_subscription_followup(int(task["id"]), "business_rule_suppressed")
            result["cancelled"] += 1
            log.info("Subscription follow-up %s cancelled: suppressed", task.get("id"))
            continue

        payload = template_resolver(FOLLOWUP_TYPE)
        text = str((payload or {}).get("text") or "").strip()
        if not text:
            db.defer_subscription_followup(
                int(task["id"]), claim_token, "approved_template_unavailable",
                iso_time(current + RETRY_DELAY),
            )
            result["deferred"] += 1
            log.info("Subscription follow-up %s deferred: approved copy unavailable", task.get("id"))
            continue

        db.mark_subscription_followup_send_started(int(task["id"]), claim_token, now_text)
        try:
            send_result = sender(
                task["customer_id"], text,
                image_url=(payload or {}).get("media"),
                inbound_message_id=task.get("source_conversation_id"),
                response_type="scheduled_followup",
            )
            if not _send_succeeded(send_result):
                raise RuntimeError((send_result or {}).get("error") or "Instagram send failed")
        except Exception as error:
            db.mark_subscription_followup_failed(int(task["id"]), claim_token, _safe_error(error))
            result["failed"] += 1
            log.warning("Subscription follow-up %s failed; preserved for retry", task.get("id"))
            continue

        if db.mark_subscription_followup_sent(int(task["id"]), claim_token, iso_time(current)):
            result["sent"] += 1
            log.info("Subscription follow-up sent: id=%s type=%s", task.get("id"), FOLLOWUP_TYPE)

    return result
