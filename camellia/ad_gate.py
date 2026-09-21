import logging
import hashlib
import re
import httpx
from dataclasses import dataclass
from typing import Dict, Any, Tuple, Optional
from camellia.database import db
from camellia.config import config
from camellia.ice_breakers import question_for_payload

log = logging.getLogger("camellia.ad_gate")

# Meta's own generated ice-breaker postbacks carry a payload naming themselves.
# A business-configured one carries whatever payload we set, so this pattern is
# a positive identification only — anything else stays the vaguer "postback".
_ICE_BREAKER_PAYLOAD_RE = re.compile(r'ice[_\s-]?breaker', re.IGNORECASE)


@dataclass(frozen=True)
class EntryPoint:
    """
    How one inbound message was composed, and which ad it came in from.

    These are two separate questions and conflating them loses the answer to
    both. `source` is what the person did — typed a sentence, or tapped
    something we (or Meta) put in front of them. `ad_id` / `ad_title` are where
    they arrived from, which is true of a tap and a typed message alike.

    Worth the columns because of a specific complaint: around fifty people
    asked whether we deliver outside Delhi NCR and then named Gurgaon, Noida
    and Faridabad — all of which are inside it. Fifty people do not
    independently phrase a question that way. Either Instagram is offering it
    as a tappable prompt or they are typing it, and until the two are recorded
    separately there is no way to tell which, or to prove a fix worked.
    """

    source: str = "unknown"        # typed | ice_breaker | postback | story_reply | unknown
    ad_id: Optional[str] = None
    ad_title: Optional[str] = None
    media_id: Optional[str] = None
    from_ad_click: bool = False    # this event carried the ad referral itself
    raw_payload: Optional[str] = None


def _referral_of(messaging_event: Dict[str, Any]) -> Dict[str, Any]:
    """The referral block, wherever Meta hung it on this event."""
    message_obj = messaging_event.get("message") or {}
    postback_obj = messaging_event.get("postback") or {}
    for candidate in (
        message_obj.get("referral"),
        postback_obj.get("referral"),
        messaging_event.get("referral"),
    ):
        if isinstance(candidate, dict):
            return candidate
    return {}


def describe_entry_point(messaging_event: Dict[str, Any]) -> EntryPoint:
    """
    Classifies one raw messaging event without gating or replying to it.

    Deliberately separate from `inspect_and_gate_message`, which answers a
    different question (may we reply at all) and whose tuple shape several
    callers and tests already depend on.
    """
    message_obj = messaging_event.get("message") or {}
    postback_obj = messaging_event.get("postback") or {}
    referral = _referral_of(messaging_event)

    ad_id = referral.get("ad_id")
    ads_context = referral.get("ads_context_data")
    ad_title = ads_context.get("ad_title") if isinstance(ads_context, dict) else None
    from_ad_click = str(referral.get("source", "")).upper() == "ADS"
    # The only media identifier on a *typed* message is the story id attached
    # to a reply; nothing here infers one from anywhere else. A reel shared as
    # an attachment does carry its own permalink, but that arrives on a
    # different event shape and is read by shared_reel_identity() rather than
    # folded in here.
    reply_to = message_obj.get("reply_to") or {}
    story = reply_to.get("story") if isinstance(reply_to, dict) else None
    media_id = str(story.get("id")) if isinstance(story, dict) and story.get("id") else None
    story_mention = message_obj.get("story_mention") or messaging_event.get("story_mention")

    payload = postback_obj.get("payload")
    if postback_obj:
        # A tap, not typing. Which kind of tap is the whole question, so the
        # raw payload is kept even when it does not match a known shape.
        source = "ice_breaker" if payload and _ICE_BREAKER_PAYLOAD_RE.search(str(payload)) else "postback"
    elif isinstance(message_obj.get("reply_to"), dict) and message_obj["reply_to"].get("story"):
        source = "story_reply"
    elif story_mention:
        # A customer mentioning the business in their own Story is a
        # different event from replying to our Story. It carries no product
        # context and is intentionally left out of customer routing.
        source = "story_mention"
    elif message_obj.get("text") or (messaging_event.get("message_edit") or {}).get("text"):
        source = "typed"
    else:
        source = "unknown"

    return EntryPoint(
        source=source,
        ad_id=ad_id,
        ad_title=ad_title,
        media_id=media_id,
        from_ad_click=from_ad_click,
        raw_payload=str(payload) if payload else None,
    )

def fetch_message_details_fallback(mid: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Fallback Graph API lookup for message_edit events missing sender/from fields.
    GET https://graph.facebook.com/{version}/{mid}?fields=from,to,message&access_token={token}
    """
    token = config.INSTAGRAM_ACCESS_TOKEN
    mid_hash = hashlib.sha256(str(mid or "").encode("utf-8")).hexdigest()[:12] if mid else None
    log.info("Graph API fallback for message_hash=%s token_configured=%s", mid_hash, bool(token))

    if not mid or not token:
        log.warning("Graph API fallback skipped for message_hash=%s: missing mid or access token.", mid_hash)
        return None, None

    url = f"https://graph.facebook.com/{config.INSTAGRAM_API_VERSION}/{mid}"
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
                    log.info(
                        "Graph API fallback succeeded for message_hash=%s: "
                        "sender_hash=%s text_present=true",
                        mid_hash,
                        hashlib.sha256(str(sender_id).encode("utf-8")).hexdigest()[:12],
                    )
                    return sender_id, str(message_text)
                else:
                    log.info(
                        "Graph API fallback for message_hash=%s returned HTTP 200 "
                        "without usable sender/message.", mid_hash,
                    )
                    return None, None
            else:
                log.warning(
                    "Graph API fallback failed for message_hash=%s with HTTP %s.",
                    mid_hash, resp.status_code,
                )
    except Exception as e:
        log.error(
            "Graph API fallback failed for message_hash=%s with exception=%s.",
            mid_hash, type(e).__name__,
        )

    return None, None


def fetch_instagram_username(sender_id: str) -> Optional[str]:
    """Resolve the username for an exact Instagram-scoped sender ID.

    Instagram webhook events normally identify the customer by scoped ID, not
    by handle.  The User Profile API is the supported fallback when the event
    itself does not carry ``sender.username``.  Only the username is returned;
    identifiers and Graph response details stay out of logs.
    """
    sender = str(sender_id or "").strip()
    token = (config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    sender_hash = hashlib.sha256(sender.encode("utf-8")).hexdigest()[:12] if sender else None
    if not sender or not token:
        return None

    host = "graph.instagram.com" if token.startswith("IGAA") else "graph.facebook.com"
    url = f"https://{host}/{config.INSTAGRAM_API_VERSION}/{sender}"
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(
                url,
                params={"fields": "username", "access_token": token},
            )
        if response.status_code != 200:
            log.info(
                "Instagram profile lookup unavailable for sender_hash=%s status=%s.",
                sender_hash, response.status_code,
            )
            return None
        payload = response.json()
        username = payload.get("username") if isinstance(payload, dict) else None
        return str(username).strip().lstrip("@") if username else None
    except Exception as exc:
        log.info(
            "Instagram profile lookup failed for sender_hash=%s error=%s.",
            sender_hash, type(exc).__name__,
        )
        return None

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

        # A tapped prompt is the customer asking a question, so it has to end
        # up as text the matcher can read. Messenger sends `title` on a
        # postback; Instagram does not reliably, and without the payload
        # fallback below a tap arrives with no text at all and is discarded as
        # an empty event — the customer taps "Show me the menu" and gets
        # silence, which is exactly what happened when these went live.
        message_text = (
            message_obj.get("text")
            or postback_obj.get("title")
            or edit_obj.get("text")
            or question_for_payload(postback_obj.get("payload") or "")
            or ""
        )

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
