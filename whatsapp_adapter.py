import httpx
from typing import Dict, Any, Tuple, Optional
from config import config
from responder import responder
from database import db
from escalator import escalator
from monitoring import get_logger, delivery_monitor

log = get_logger("camellia.whatsapp")

class WhatsAppAdapter:
    """
    Adapter for Meta WhatsApp Business Cloud API.
    
    Per PRD Requirement #2:
    - Same bot logic/session reachable via WhatsApp Business.
    - Deterministic responses and escalation on WhatsApp.
    """

    @property
    def access_token(self) -> str:
        return config.WHATSAPP_ACCESS_TOKEN

    @property
    def phone_number_id(self) -> str:
        return config.WHATSAPP_PHONE_NUMBER_ID

    @property
    def api_version(self) -> str:
        return config.WHATSAPP_API_VERSION

    @property
    def api_url(self) -> str:
        return f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"

    def configuration_status(self) -> Dict[str, Any]:
        """Reports safe WhatsApp configuration state without exposing credentials."""
        if config.WHATSAPP_MOCK_MODE:
            return {"mode": "mock", "ready": True, "reason": "WHATSAPP_MOCK_MODE is enabled"}
        missing = []
        if not self.access_token:
            missing.append("WHATSAPP_ACCESS_TOKEN")
        if not self.phone_number_id:
            missing.append("WHATSAPP_PHONE_NUMBER_ID")
        return {
            "mode": "live",
            "ready": not missing,
            "reason": "Configured" if not missing else "Missing required environment variables",
            "missing": missing,
        }

    def parse_webhook_message(self, payload: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
        """
        Parses Meta WhatsApp Business API webhook payload.
        
        Returns:
            (sender_phone: str, message_id: str, message_text: str) or None
        """
        try:
            entry = payload.get("entry", [])[0]
            changes = entry.get("changes", [])[0]
            value = changes.get("value", {})
            messages = value.get("messages", [])
            if not messages:
                return None
            
            msg = messages[0]
            sender_phone = msg.get("from")
            msg_id = msg.get("id")
            
            msg_text = ""
            if msg.get("type") == "text":
                msg_text = msg.get("text", {}).get("body", "")
            elif msg.get("type") == "interactive":
                interactive = msg.get("interactive", {})
                msg_text = (
                    interactive.get("button_reply", {}).get("title") or
                    interactive.get("list_reply", {}).get("title") or ""
                )
                
            return sender_phone, msg_id, msg_text
        except Exception as e:
            log.error("Error parsing WhatsApp webhook: %s", e)
            return None

    def process_incoming_message(self, sender_phone: str, message_text: str) -> Dict[str, Any]:
        """Processes an incoming WhatsApp message through responder & escalator."""
        # Match response with session memory
        response_text, was_matched, template_id, image_url = responder.match_and_respond(message_text, user_id=sender_phone, channel="whatsapp")
        
        # Log to DB
        db.log_conversation(sender_phone, "whatsapp", message_text, response_text, was_matched, template_id)
        
        # Handle escalation if unmatched OR order confirmation
        if not was_matched or template_id == "order_next_steps":
            escalator.handle_escalation(
                user_id=sender_phone,
                channel="whatsapp",
                user_message=message_text,
                reason="Order Confirmation & Intake Request" if was_matched else "Unmatched WhatsApp Query / Sensitive Request",
                context_data={"phone": sender_phone, "template_id": template_id}
            )

        # Quick reply buttons logic for WhatsApp
        buttons = self._get_contextual_buttons(template_id, was_matched)

        # Send response via WhatsApp API (or mock if no token configured)
        send_result = self.send_message(sender_phone, response_text, image_url=image_url, buttons=buttons)
        
        return {
            "status": "processed",
            "sender_phone": sender_phone,
            "was_matched": was_matched,
            "template_id": template_id,
            "response_text": response_text,
            "image_url": image_url,
            "buttons": buttons,
            "send_result": send_result
        }

    def _get_contextual_buttons(self, template_id: Optional[str], was_matched: bool) -> Optional[list]:
        """Generates dynamic, context-aware WhatsApp quick reply buttons (max 3 buttons)."""
        # The menu reply already shows the card and names the two ranges, so its
        # buttons continue that choice instead of offering the menu again.
        if template_id in ["know_more", "product_overview", "menu_general"]:
            return ["🌸 Sondesh", "🍱 Meals", "🛍️ Place Order"]
        # Asked "Veg or Non-Veg?" — the buttons must be the two answers.
        if template_id in ["context_meal_price_followup", "savoury_food_intro", "context_veg_followup"]:
            return ["🌿 Veg Menu", "🍗 Non-Veg Menu", "🛍️ Place Order"]
        if not was_matched or template_id in ["greeting", "bot_capabilities", "bot_identity"]:
            return ["🌸 See Menu", "🍫 Sondesh Prices", "🛍️ Place Order"]
        elif template_id in ["pricing_general", "pricing_aamar", "pricing_signature", "nolen_gur_aamar", "nolen_gur_signature", "order_calculation"]:
            return ["🛍️ Place Order", "📦 Delivery Times", "👨‍🍳 Talk to Chef"]
        elif template_id in ["delivery_time", "delivery_faq", "delivery_area"]:
            return ["🌸 See Menu", "🛍️ Place Order", "📍 Kitchen Location"]
        elif template_id in ["order_next_steps", "how_to_order"]:
            return ["📋 Confirm Details", "💳 Payment Info", "📞 Call Kitchen"]
        elif template_id in ["festive_gifting", "custom_order"]:
            return ["🎁 Gift Hampers", "🛍️ Custom Box", "📞 Speak to Chef"]
        return ["🌸 See Menu", "🍫 Sondesh Prices", "🛍️ Place Order"]

    def _public_url(self, path: str) -> str:
        """Converts a relative image path to a full public URL Meta can fetch.
        Paths like /images/foo.jpeg become https://your-domain.com/images/foo.jpeg.
        Already-absolute URLs are returned unchanged."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        base = config.PUBLIC_BASE_URL.rstrip("/")
        return base + path if base else path

    def send_message(self, recipient_phone: str, text_body: str, image_url=None, buttons=None) -> Dict[str, Any]:
        """Sends a text or interactive button message followed by zero or more image cards via Meta WhatsApp Cloud API."""
        raw_images = [image_url] if isinstance(image_url, str) else list(image_url or [])
        images = [self._public_url(img) for img in raw_images]

        if config.WHATSAPP_MOCK_MODE:
            # Development / Mock Mode: never construct an outbound HTTP request.
            safe_body = text_body[:60].encode("ascii", "backslashreplace").decode("ascii")
            btn_info = f" [Buttons: {', '.join(buttons)}]" if buttons else ""
            msg_type = f"IMAGE x{len(images)} ({', '.join(images)}){btn_info}" if images else f"TEXT{btn_info}"
            log.info("[MOCK WHATSAPP OUTBOUND] [%s] To: %s | Body: %s...", msg_type, recipient_phone, safe_body)
            return {"mock": True, "status": "sent", "mode": "mock", "recipient": recipient_phone, "type": msg_type, "buttons": buttons}

        if not self.access_token or not self.phone_number_id:
            log.error("WhatsApp live mode is enabled but required credentials are missing")
            return {
                "mock": False,
                "status": "not_configured",
                "mode": "live",
                "error": "Set WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID before enabling live mode",
            }

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

        payloads = []
        if text_body:
            if buttons and len(buttons) > 0:
                # Meta Interactive Button Message (Max 3 buttons)
                formatted_buttons = [
                    {
                        "type": "reply",
                        "reply": {
                            "id": f"quick_btn_{i+1}",
                            "title": title[:20]  # Meta button titles max 20 chars
                        }
                    }
                    for i, title in enumerate(buttons[:3])
                ]
                payloads.append({
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": recipient_phone,
                    "type": "interactive",
                    "interactive": {
                        "type": "button",
                        "body": {"text": text_body},
                        "action": {"buttons": formatted_buttons}
                    }
                })
            else:
                payloads.append({
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": recipient_phone,
                    "type": "text",
                    "text": {"preview_url": False, "body": text_body}
                })

        for img in images:
            img_url = img
            if img.startswith("/"):
                base_url = getattr(config, "PUBLIC_BASE_URL", "").rstrip("/")
                if base_url:
                    img_url = f"{base_url}{img}"
                else:
                    log.warning("Image URL '%s' is relative but PUBLIC_BASE_URL is not set. Meta API requires an absolute http(s) URL.", img)
                    continue
            payloads.append({
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient_phone,
                "type": "image",
                "image": {"link": img_url}
            })

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = None
                for payload in payloads:
                    resp = client.post(self.api_url, headers=headers, json=payload)
                    resp.raise_for_status()
                delivery_monitor.record_success("whatsapp")
                return resp.json() if resp is not None else {"status": "no_content"}
        except Exception as e:
            body = getattr(getattr(e, "response", None), "text", "")
            delivery_monitor.record_failure("whatsapp", f"to {recipient_phone}: {e}")
            if body:
                log.error("Meta API error body: %s", body)
            return {"error": str(e), "status": "failed"}

whatsapp_adapter = WhatsAppAdapter()
