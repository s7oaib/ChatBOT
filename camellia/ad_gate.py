import logging
import httpx
from typing import Dict, Any, Tuple, Optional
from camellia.database import db
from camellia.config import config

log = logging.getLogger("camellia.ad_gate")

def fetch_message_details_fallback(mid: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Fallback Graph API lookup for message_edit events missing sender/from fields.
    GET https://graph.facebook.com/{version}/{mid}?fields=from,to,message&access_token={token}
    """
    token = config.INSTAGRAM_ACCESS_TOKEN
    masked_token = f"{token[:6]}...{token[-4:]}" if token and len(token) > 10 else ("SET" if token else "EMPTY")
    log.info("Graph API fallback for mid '%s' using INSTAGRAM_ACCESS_TOKEN (%s)", mid, masked_token)

    if not mid or not token:
        log.warning("Graph API fallback skipped for mid '%s': missing mid or access token (fallback failed, unknown).", mid)
        return None, None

    url = f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/{mid}"
    params = {
        "fields": "from,to,message",
        "access_token": token
    }
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                sender_id = data.get("from", {}).get("id") or data.get("sender", {}).get("id")
                raw_msg = data.get("message")
                message_text = raw_msg.get("text") if isinstance(raw_msg, dict) else raw_msg

                if sender_id and message_text:
                    log.info("Graph API fallback succeeded for mid %s: sender_id=%s, text=%s", mid, sender_id, str(message_text)[:30])
                    return sender_id, str(message_text)
                else:
                    log.info("Graph API fallback for mid %s returned HTTP 200 but no usable sender/message: %s (confirmed noise).", mid, data)
                    return None, None
            else:
                log.warning("Graph API fallback failed for mid %s with HTTP %s: %s (fallback failed, unknown).", mid, resp.status_code, resp.text)
    except Exception as e:
        log.error("Graph API fallback failed for mid %s with exception: %s (fallback failed, unknown).", mid, str(e))

    return None, None

class AdGate:
    """Instagram Ad-Referral Hard Gate Processor.
    
    Per PRD Requirement #4:
    - Auto-replies MUST only engage for ad-originated inquiries (referral.source == "ADS").
    - Non-ad organic DMs are strictly IGNORED by the bot (left for manual human handling).

    BYPASS_AD_GATE=true answers every DM instead. This is no longer only a
    development switch — production runs with it on, because the client wants
    the bot answering organic DMs too. With it on, nothing here filters a
    complaint or a personal message away from the bot, so the escalation
    triggers in qa_templates.json are what route those to a human.
    """

    @staticmethod
    def inspect_and_gate_message(messaging_event: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
        """
        Inspects an incoming Instagram messaging event.
        
        Returns:
            (is_ad_qualified: bool, sender_id: str, message_text: str, ad_id: Optional[str])
        """
        edit_obj = messaging_event.get("message_edit", {})
        message_obj = messaging_event.get("message") or edit_obj or {}
        postback_obj = messaging_event.get("postback", {})

        sender_obj = (
            messaging_event.get("sender")
            or messaging_event.get("from")
            or message_obj.get("sender")
            or message_obj.get("from")
        )
        sender_id = None
        if isinstance(sender_obj, dict):
            sender_id = sender_obj.get("id")
        elif sender_obj:
            sender_id = str(sender_obj)

        message_text = message_obj.get("text") or postback_obj.get("title") or edit_obj.get("text") or ""

        # Fallback for message_edit events lacking a sender field
        mid = edit_obj.get("mid") or messaging_event.get("mid") or message_obj.get("mid")
        if not sender_id and edit_obj and mid:
            fetched_sender, fetched_text = fetch_message_details_fallback(mid)
            if fetched_sender and fetched_text:
                sender_id = fetched_sender
                message_text = fetched_text

        # Ignore echoes (messages sent by our own business account)
        if message_obj.get("is_echo") or edit_obj.get("is_echo"):
            return False, sender_id, None, None

        if not sender_id:
            return False, None, None, None

        # Extract referral from message level or postback level
        referral = message_obj.get("referral") or postback_obj.get("referral") or messaging_event.get("referral")
        
        ad_id = None
        is_direct_ad_referral = False

        if referral and isinstance(referral, dict):
            source = str(referral.get("source", "")).upper()
            if source == "ADS":
                is_direct_ad_referral = True
                ad_id = referral.get("ad_id")

        if config.BYPASS_AD_GATE:
            return True, sender_id, message_text, ad_id

        # Check existing session status in database
        already_qualified = db.is_ad_qualified(sender_id, "instagram")

        if is_direct_ad_referral:
            # Tag session as ad_qualified in DB
            db.set_ad_qualified(sender_id, "instagram", is_ad_qualified=True, ad_id=ad_id)
            return True, sender_id, message_text, ad_id
        elif already_qualified:
            # User was previously qualified via Ad in this session
            return True, sender_id, message_text, None
        else:
            # Organic DM (non-ad): Ignore per PRD Requirement #4
            return False, sender_id, message_text, None

ad_gate = AdGate()
