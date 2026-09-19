import json
from typing import Dict, Any, Optional
from camellia.database import db
from camellia.monitoring import get_logger
from camellia.config import config
from camellia import owner_notifications

from camellia.session_memory import session_memory

log = get_logger("camellia.escalator")

class Escalator:
    """
    Escalation & Human Handoff System.
    
    Per PRD Requirement #4b & Phase 3:
    - Answer only 'in syllabus' questions; else forward to human.
    - Escalation path is mandatory, not optional.
    - Logs escalation details in database with Customer Handoff Briefs for full client visibility.
    - Sends automated alert message to owner/human contact.
    """

    def handle_escalation(
        self,
        user_id: str,
        channel: str,
        user_message: str,
        reason: str,
        context_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Records escalation in database and sends notification with Customer Handoff Brief.
        """
        context = context_data or {}
        context["ad_qualified"] = db.is_ad_qualified(user_id, channel)
        
        # Attach Session Memory Customer Handoff Brief
        handoff_brief = session_memory.get_handoff_brief(user_id, channel, escalation_reason=reason)
        context["handoff_brief"] = handoff_brief
        
        # 1. Record escalation record in database
        escalation_id = db.record_escalation(
            user_id=user_id,
            channel=channel,
            user_message=user_message,
            reason=reason,
            context_data=context
        )

        # 2. Enqueue before attempting delivery. The escalation row and its
        # outbox event survive a process restart or provider outage.
        event = db.enqueue_human_notification(
            escalation_id, handoff_ref=str(context.get("handoff_ref") or "") or None,
        )
        self.process_pending_notifications(event_id=int(event["id"]))
        notification = db.get_human_notification_event(int(event["id"]))
        alert_sent = bool(notification and notification.get("status") == "SENT")

        return {
            "escalation_id": escalation_id,
            "status": "escalated",
            "handoff_brief": handoff_brief,
            "alert_sent": alert_sent,
            "notification_status": notification.get("status") if notification else None,
        }

    def process_pending_notifications(
        self, *, limit: int = 10, event_id: Optional[int] = None,
    ) -> int:
        """Deliver due durable staff events once, with bounded retry state."""
        delivered = 0
        for event in db.claim_human_notification_events(limit=limit, event_id=event_id):
            escalation = db.get_escalation_by_id(int(event["escalation_id"]))
            if not escalation:
                db.complete_human_notification(
                    int(event["id"]), success=False, retryable=False,
                    error="escalation record is missing",
                )
                continue
            try:
                context = json.loads(escalation.get("context_data") or "{}")
                if not isinstance(context, dict):
                    context = {}
            except (TypeError, ValueError):
                context = {}
            try:
                result = self.send_human_alert(
                    int(escalation["id"]), escalation["user_id"], escalation["channel"],
                    escalation["user_message"], escalation["reason"], context,
                    event_id=int(event["id"]),
                )
            except Exception as exc:
                log.exception("Durable human notification dispatch failed unexpectedly.")
                result = {
                    "status": "failed", "retryable": False,
                    "error": type(exc).__name__,
                }
            success = result.get("status") == "sent" or result.get("mock") is True
            retryable = bool(result.get("retryable"))
            attempts = max(1, int(event.get("attempts") or 1))
            retry_delay = min(3600, 60 * (2 ** min(attempts - 1, 6)))
            db.complete_human_notification(
                int(event["id"]), success=success and not retryable,
                skipped=str(result.get("status") or "").lower() == "skipped" and not retryable,
                retryable=retryable,
                error=result.get("error"), retry_after_seconds=retry_delay,
            )
            if success:
                delivered += 1
        return delivered

    def _evaluate_order_verification(self, user_message: str, chat_history: str) -> str:
        """Inspects message & history for phone number & address to flag potential fake/unverified orders."""
        import re
        full_text = f"{user_message}\n{chat_history}".lower()
        has_phone = bool(re.search(r'\b[6-9]\d{9}\b|\+?91\s*[6-9]\d{9}\b', full_text))
        addr_keywords = ["delhi", "gurgaon", "gurugram", "noida", "ghaziabad", "faridabad", "sector", "block", "flat", "house", "road", "pincode", "pin", "address", "phase", "street"]
        has_address = any(kw in full_text for kw in addr_keywords)

        if has_phone and has_address:
            return "✅ READY FOR VERIFICATION & INVOICING (Phone & Address Present)"
        elif has_phone:
            return "⚠️ PENDING VERIFICATION (Phone Present, Address Missing — Confirm address first)"
        elif has_address:
            return "⚠️ PENDING VERIFICATION (Address Present, Phone Missing — Confirm contact number first)"
        else:
            return "⚠️ PENDING VERIFICATION (Missing Address & Phone — Staff MUST verify customer before prepping)"

    def send_human_alert(
        self,
        escalation_id: int,
        user_id: str,
        channel: str,
        user_message: str,
        reason: str,
        context: Dict[str, Any],
        event_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Fan out one safe owner event to email, Web Push, and Admin."""
        if event_id is None:
            events = db.get_human_notification_events(limit=200)
            matching = [e for e in events if int(e.get("escalation_id") or 0) == int(escalation_id)]
            event_id = int(matching[0]["id"]) if matching else None
        if event_id is None:
            # Preserve the legacy direct-call contract used by diagnostics and
            # older integrations. Normal production dispatch always supplies a
            # durable event id from process_pending_notifications().
            return {"status": "skipped", "retryable": False, "reason": "manual_handoff_only"}

        handoff_ref = str(context.get("handoff_ref") or "").strip()
        handoff = db.get_handoff_by_ref(handoff_ref) if handoff_ref else None
        if not handoff:
            # Legacy escalation rows can still be processed safely, but have no
            # CAM payload for owner channels; retain the old manual-only state.
            log.info("Skipping owner channels for escalation %s without a CAM handoff.", escalation_id)
            for delivery in db.get_notification_deliveries(event_id):
                db.complete_notification_delivery(
                    int(delivery["id"]), status="SKIPPED", error="no CAM handoff reference",
                )
            return {"status": "skipped", "retryable": False, "reason": "no_handoff"}

        payload = owner_notifications.safe_handoff_payload(
            handoff, public_base_url=config.PUBLIC_BASE_URL,
        )
        outcomes = []
        retry_after = 60
        for delivery in db.get_notification_deliveries(event_id):
            delivery_id = int(delivery["id"])
            channel_name = delivery["channel"]
            if delivery["status"] in {"SENT", "FAILED", "SKIPPED"}:
                outcomes.append({"status": delivery["status"].lower(), "retryable": False})
                continue
            if channel_name == "ADMIN":
                result = {"status": "sent"}
            elif channel_name == "EMAIL":
                result = owner_notifications.send_email(payload)
            elif channel_name == "WEB_PUSH":
                result = owner_notifications.send_web_push(payload, db)
            else:
                result = {"status": "failed", "retryable": False, "error": "unknown channel"}
            normalized = str(result.get("status") or "failed").lower()
            if normalized == "sent":
                stored_status = "SENT"
            elif normalized == "skipped":
                stored_status = "SKIPPED"
            elif result.get("retryable"):
                stored_status = "RETRYING"
            else:
                stored_status = "FAILED"
            attempts = max(1, int(delivery.get("attempts") or 1))
            retry_after = min(3600, 60 * (2 ** min(attempts - 1, 6)))
            db.complete_notification_delivery(
                delivery_id, status=stored_status, error=result.get("error"),
                retry_after_seconds=retry_after,
            )
            outcomes.append(result)

        if any(str(item.get("status")).lower() == "sent" for item in outcomes):
            log.info("Delivered owner notification for %s via configured channel(s).", handoff_ref)
            return {"status": "sent", "retryable": any(item.get("retryable") for item in outcomes)}
        if any(item.get("retryable") for item in outcomes):
            return {"status": "failed", "retryable": True, "error": "temporary owner notification failure"}
        if any(str(item.get("status")).lower() == "failed" for item in outcomes):
            return {"status": "failed", "retryable": False, "error": "owner notification channel failed"}
        return {"status": "skipped", "retryable": False, "reason": "no owner channel configured"}

escalator = Escalator()
