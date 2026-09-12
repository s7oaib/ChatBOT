import httpx
from typing import Dict, Any, Tuple, Optional
from camellia.config import config
from camellia.responder import responder, _strip_flower_emoji
from camellia.database import db
from camellia.escalator import escalator
from camellia.handoff import extract_reference, mark_whatsapp_connected
from camellia.monitoring import get_logger, delivery_monitor

log = get_logger("camellia.whatsapp")

class WhatsAppAdapter:
    """
    Adapter for Meta WhatsApp Business Cloud API.

    Channel policy: WhatsApp is human-only. The client does not want
    customers talking to an AI bot here at all — Instagram is the only AI
    chatbot channel, and a customer who needs a person is handed a WhatsApp
    click-to-chat link from there (see camellia/handoff.py). This class still
    owns outbound *sending* (used by the human-alert path in escalator.py and
    by anything staff-triggered), but `process_incoming_message` — the one
    path that would compose and send an automated reply to a customer message
    — does not call the understanding/RAG/template stack by default. Flipping
    `WHATSAPP_CHATBOT_ENABLED` back on is the only way to restore the old
    behaviour, and it is off by default; see camellia/config.py.
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
            entries = payload.get("entry", [])
            if not entries:
                return None
            changes = entries[0].get("changes", [])
            if not changes:
                return None
            value = changes[0].get("value", {})
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
        """Records an incoming WhatsApp message. Sends NO automated reply.

        WhatsApp is the human destination after an Instagram handoff, not a
        second chatbot surface. This never calls understanding_engine.analyze,
        Groq, RAG, semantic fallback, or a QA-template response — none of
        that machinery is reachable from here at all when the chatbot is
        disabled (the default). If the message names a handoff reference
        ("Ref: CAM-48371"), that reference is marked connected purely for
        staff-side tracking; the customer still gets no bot reply for it.
        """
        if config.WHATSAPP_CHATBOT_ENABLED and not config.WHATSAPP_CHATBOT_ENABLE_CONFIRM:
            # A lone WHATSAPP_CHATBOT_ENABLED=true is treated as an accidental
            # flip, not a decision — restoring the old behaviour requires the
            # separate WHATSAPP_CHATBOT_ENABLE_CONFIRM flag too. Fail safe to
            # human-only rather than silently answering as a bot.
            log.error(
                "WHATSAPP_CHATBOT_ENABLED is true but WHATSAPP_CHATBOT_ENABLE_CONFIRM "
                "is not set; refusing to run WhatsApp as a chatbot for %s. This is the "
                "client's explicit channel policy: WhatsApp is human-only. Set both "
                "flags to true if this is a deliberate policy change.",
                sender_phone,
            )
        elif config.WHATSAPP_CHATBOT_ENABLED:
            # Explicit, logged escape hatch for a future policy change only —
            # off by default, and not exercised by any default configuration.
            log.warning(
                "WHATSAPP_CHATBOT_ENABLED is on: WhatsApp is answering %s as a "
                "chatbot, which the client's current channel policy forbids.",
                sender_phone,
            )
            return self._process_as_chatbot(sender_phone, message_text)

        reference = extract_reference(message_text)
        if reference and config.WHATSAPP_HUMAN_HANDOFF_ENABLED:
            mark_whatsapp_connected(reference)

        db.log_conversation(sender_phone, "whatsapp", message_text, "", False, "whatsapp_human_only")
        log.info(
            "WhatsApp inbound from %s recorded for human reply (handoff_ref=%s). No bot reply sent.",
            sender_phone, reference,
        )
        return {
            "status": "human_only",
            "sender_phone": sender_phone,
            "was_matched": False,
            "template_id": "whatsapp_human_only",
            "response_text": None,
            "handoff_ref": reference,
            "bot_replied": False,
        }

    def _process_as_chatbot(self, sender_phone: str, message_text: str) -> Dict[str, Any]:
        """The old auto-reply behaviour, kept only behind WHATSAPP_CHATBOT_ENABLED."""
        response_text, was_matched, template_id, image_url = responder.match_and_respond(message_text, user_id=sender_phone, channel="whatsapp")

        db.log_conversation(sender_phone, "whatsapp", message_text, response_text, was_matched, template_id)

        if not was_matched or template_id == "order_next_steps":
            escalator.handle_escalation(
                user_id=sender_phone,
                channel="whatsapp",
                user_message=message_text,
                reason="Order Confirmation & Intake Request" if was_matched else "Unmatched WhatsApp Query / Sensitive Request",
                context_data={"phone": sender_phone, "template_id": template_id}
            )

        buttons = self._get_contextual_buttons(template_id, was_matched)
        send_result = self.send_message(sender_phone, response_text, image_url=image_url, buttons=buttons)

        return {
            "status": "processed",
            "sender_phone": sender_phone,
            "was_matched": was_matched,
            "template_id": template_id,
            "response_text": response_text,
            "image_url": image_url,
            "buttons": buttons,
            "send_result": send_result,
            "bot_replied": True,
        }

    def _get_contextual_buttons(self, template_id: Optional[str], was_matched: bool) -> Optional[list]:
        """Generates dynamic, context-aware WhatsApp quick reply buttons (max 3 buttons)."""
        if config.CONVERSATION_IMPROVEMENTS_ENABLED:
            if template_id in {"acknowledgement", "conversation_closing", "explicit_escalation"}:
                return None
            if template_id in {"price_clarify_product", "compound_menu_delivery"}:
                return ["Sondesh", "🍱 Meals"]
        # Asked the customer what they meant — the buttons are the options offered.
        if template_id == "clarify_request":
            return ["Sondesh", "🍱 Meals", "🚚 Delivery"]
        # The menu reply already shows the card and names the two ranges, so its
        # buttons continue that choice instead of offering the menu again.
        if template_id in ["know_more", "product_overview", "menu_general"]:
            return ["Sondesh", "🍱 Meals", "🛍️ Place Order"]
        # Asked "Veg or Non-Veg?" — the buttons must be the two answers.
        if template_id in ["context_meal_price_followup", "savoury_food_intro", "context_veg_followup"]:
            return ["🌿 Veg Menu", "🍗 Non-Veg Menu", "🛍️ Place Order"]
        if not was_matched or template_id in ["greeting", "bot_capabilities", "bot_identity"]:
            return ["See Menu", "🍫 Sondesh Prices", "🛍️ Place Order"]
        elif template_id in ["pricing_general", "pricing_aamar", "pricing_signature", "nolen_gur_aamar", "nolen_gur_signature", "order_calculation"]:
            return ["🛍️ Place Order", "📦 Delivery Times", "👨‍🍳 Talk to Chef"]
        elif template_id in ["delivery_time", "delivery_faq", "delivery_area"]:
            return ["See Menu", "🛍️ Place Order", "📍 Kitchen Location"]
        elif template_id in ["order_next_steps", "how_to_order"]:
            return ["📋 Confirm Details", "💳 Payment Info", "📞 Call Kitchen"]
        elif template_id in ["festive_gifting", "custom_order"]:
            return ["🎁 Gift Hampers", "🛍️ Custom Box", "📞 Speak to Chef"]
        return ["See Menu", "🍫 Sondesh Prices", "🛍️ Place Order"]

    def _public_url(self, path: str) -> str:
        """Converts a relative image path to a full public URL Meta can fetch.
        Paths like /images/foo.jpeg become https://your-domain.com/images/foo.jpeg.
        Already-absolute URLs are returned unchanged."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        base = config.PUBLIC_BASE_URL.rstrip("/")
        return base + path if base else path

    @staticmethod
    def _normalise_images(image_url) -> list:
        """
        Accepts a URL, a list of URLs, or a list of {"url", "caption"} entries and
        returns a uniform list of (url, caption) pairs. Plain strings stay
        supported so templates without captions are unaffected.
        """
        raw = [image_url] if isinstance(image_url, (str, dict)) else list(image_url or [])
        pairs = []
        for entry in raw:
            if isinstance(entry, dict):
                url = entry.get("url")
                if url:
                    pairs.append((url, entry.get("caption")))
            elif entry:
                pairs.append((entry, None))
        return pairs

    def send_message(self, recipient_phone: str, text_body: str, image_url=None, buttons=None) -> Dict[str, Any]:
        """Sends a text or interactive button message plus zero or more image cards via Meta WhatsApp Cloud API."""
        # Last stop before Meta. The source text carries no flowers any more,
        # but a refined reply or a staff-written learned answer can still
        # arrive with one, and this is the only place every outbound WhatsApp
        # message passes through.
        text_body = _strip_flower_emoji(text_body or "")
        buttons = [_strip_flower_emoji(str(button)) for button in (buttons or [])]
        pairs = self._normalise_images(image_url)
        captioned = [(self._public_url(u), _strip_flower_emoji(c or "")) for u, c in pairs]
        images = [u for u, _ in captioned]
        # When cards carry their own descriptions, they lead and the closing
        # question follows, so each card arrives next to the detail it belongs to
        # instead of after a wall of text.
        # Captioned browse cards traditionally lead the text, but the price
        # flow is explicitly text -> Aamar note/card -> Signature note/card.
        # The responder marks that flow on its first card without changing the
        # public image-pair shape used by the rest of the adapter.
        text_first = isinstance(image_url, list) and any(
            isinstance(entry, dict) and entry.get("text_first") for entry in image_url
        )
        # The client's requested sequence puts each note before the menu it
        # describes. A WhatsApp caption renders under its image, so honouring
        # that means sending the note as its own message first rather than
        # attaching it — the same order Instagram produces.
        caption_first = isinstance(image_url, list) and any(
            isinstance(entry, dict) and entry.get("caption_first") for entry in image_url
        )
        cards_first = any(c for _, c in captioned) and not text_first

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

        image_payloads = []
        undelivered_captions = []
        for img, caption in captioned:
            img_url = img
            if img.startswith("/"):
                base_url = getattr(config, "PUBLIC_BASE_URL", "").rstrip("/")
                if base_url:
                    img_url = f"{base_url}{img}"
                else:
                    log.warning("Image URL '%s' is relative but PUBLIC_BASE_URL is not set. Meta API requires an absolute http(s) URL.", img)
                    # The caption carries the description, so keep it rather than
                    # letting the detail vanish with the card.
                    if caption:
                        undelivered_captions.append(caption)
                    continue
            if caption and caption_first:
                image_payloads.append({
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": recipient_phone,
                    "type": "text",
                    "text": {"preview_url": False, "body": caption},
                })
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient_phone,
                "type": "image",
                "image": {"link": img_url},
            }
            if caption and not caption_first:
                payload["image"]["caption"] = caption
            image_payloads.append(payload)

        if undelivered_captions:
            text_body = "\n\n".join(undelivered_captions + ([text_body] if text_body else []))

        text_payloads = []
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
                text_payloads.append({
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
                text_payloads.append({
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": recipient_phone,
                    "type": "text",
                    "text": {"preview_url": False, "body": text_body}
                })

        # Captioned cards lead so each one arrives with its own description;
        # otherwise the original order (text, then cards) is preserved.
        payloads = (image_payloads + text_payloads) if cards_first else (text_payloads + image_payloads)

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
