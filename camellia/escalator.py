import json
from typing import Dict, Any, Optional
from camellia.database import db
from camellia.monitoring import get_logger

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
        event = db.enqueue_human_notification(escalation_id)
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
                )
            except Exception as exc:
                log.exception("Durable human notification dispatch failed unexpectedly.")
                result = {
                    "status": "failed", "retryable": False,
                    "error": type(exc).__name__,
                }
            success = result.get("status") == "sent" or result.get("mock") is True
            attempts = max(1, int(event.get("attempts") or 1))
            retry_delay = min(3600, 60 * (2 ** min(attempts - 1, 6)))
            db.complete_human_notification(
                int(event["id"]), success=success,
                skipped=str(result.get("status") or "").lower() == "skipped",
                retryable=bool(result.get("retryable")),
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
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Record the escalation; customer and staff continuation are manual."""
        log.info(
            "Human escalation notification skipped: manual handoff only "
            "(escalation_id=%s, channel=%s).",
            escalation_id,
            channel,
        )
        return {
            "status": "skipped",
            "retryable": False,
            "reason": "manual_handoff_only",
        }

escalator = Escalator()
