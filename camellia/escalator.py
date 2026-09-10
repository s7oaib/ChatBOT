import httpx
from typing import Dict, Any, Optional
from camellia.config import config
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

    # Read from config on every use rather than captured once at import. This
    # is a module-level singleton, so a captured copy would keep alerting the
    # previous number for the life of the process after the setting changed —
    # the failure would look exactly like the change not having been made.
    @property
    def human_phone(self) -> str:
        return config.HUMAN_ESCALATION_PHONE

    @property
    def human_email(self) -> str:
        return config.HUMAN_ESCALATION_EMAIL

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

        # 2. Dispatch human alert notification
        alert_result = self.send_human_alert(escalation_id, user_id, channel, user_message, reason, context)

        return {
            "escalation_id": escalation_id,
            "status": "escalated",
            "handoff_brief": handoff_brief,
            "alert_sent": alert_result.get("status") == "sent" or alert_result.get("mock") is True
        }

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
        """Sends WhatsApp/API alert notification to human team contact."""
        handoff = context.get("handoff_brief", {})
        cust_name = handoff.get("customer_name", "Unknown")
        chat_history = "\n".join(handoff.get("recent_chat_history", []))

        verif_status = self._evaluate_order_verification(user_message, chat_history)

        # A cross-channel handoff (camellia/handoff.py) attaches its own
        # CAM-XXXXX reference and known-entities summary here so staff see, in
        # the same alert, the reference the customer will quote on WhatsApp —
        # not a second, disconnected notification.
        handoff_ref = context.get("handoff_ref")
        handoff_block = f"\n🔗 *Handoff Reference:* {handoff_ref}\n" if handoff_ref else ""
        handoff_summary = context.get("handoff_summary")
        summary_block = f"\n{handoff_summary}\n" if handoff_summary else ""

        alert_body = (
            f"🚨 *NEW CAMELLIA BOT ORDER / ESCALATION #{escalation_id}*\n"
            f"{handoff_block}"
            f"• *Verification Status:* {verif_status}\n"
            f"• *Channel:* {channel.upper()}\n"
            f"• *Customer ID/Phone:* {user_id}\n"
            f"• *Customer Name:* {cust_name}\n"
            f"• *Reason:* {reason}\n"
            f"• *Last Message:* \"{user_message}\"\n\n"
            f"📋 *Recent Conversation History:*\n{chat_history or 'None'}\n"
            f"{summary_block}\n"
            f"Please verify order details & send invoice via WhatsApp/Call to customer before cooking/dispatching."
        )

        # In production, uses Meta WhatsApp API to alert the human phone number
        if config.WHATSAPP_ACCESS_TOKEN and config.WHATSAPP_PHONE_NUMBER_ID and self.human_phone:
            url = f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/{config.WHATSAPP_PHONE_NUMBER_ID}/messages"
            headers = {
                "Authorization": f"Bearer {config.WHATSAPP_ACCESS_TOKEN}",
                "Content-Type": "application/json"
            }
            payload = {
                "messaging_product": "whatsapp",
                "to": self.human_phone.lstrip("+"),
                "type": "text",
                "text": {"body": alert_body}
            }
            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    return {"status": "sent", "api_response": resp.json()}
            except Exception as e:
                log.error("Failed to send escalation alert to owner: %s", e)
                return {"status": "failed", "error": str(e)}
        else:
            # Development / Mock notification
            safe_alert = alert_body.encode("ascii", "backslashreplace").decode("ascii")
            log.warning("HUMAN ESCALATION ALERT (WhatsApp credentials not configured)\n%s", safe_alert)
            return {"mock": True, "status": "sent", "recipient": self.human_phone}

escalator = Escalator()
