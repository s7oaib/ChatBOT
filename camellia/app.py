import asyncio
import hmac
import hashlib
import json
import os
import re
import time
import httpx
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, Response, Query, Header, HTTPException, File, UploadFile, BackgroundTasks
from fastapi.responses import PlainTextResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from typing import Dict, Any, List, Optional, Tuple
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Imported through config, never directly: `camellia.config` loads .env before
# it triggers the bootstrap, and importing the bootstrap first would run it
# against a pre-.env environment and seed the wrong paths.
from camellia.config import config, STARTUP_ACTIONS
from camellia.database import db
from camellia.ad_gate import EntryPoint, ad_gate, describe_entry_point
from camellia.ice_breakers import (
    DEFAULT_ICE_BREAKERS,
    MAX_ICE_BREAKERS,
    MAX_ICE_BREAKER_CHARS,
)
from camellia.responder import responder
from camellia.ai_responder import ai_responder
from camellia.whatsapp_adapter import whatsapp_adapter
from camellia.escalator import escalator
from camellia.assisted_resolution import create_suggestion
from camellia.ad_routing import product_for_ad
from camellia.sheet_sync import sheet_sync
from camellia.menu_parser import menu_parser
from camellia import weekly_flyer
from camellia import menu_dates
from camellia import menu_images
from camellia.session_memory import session_memory
from camellia.monitoring import get_logger, delivery_monitor, latency_monitor

log = get_logger("camellia.app")

_INSTAGRAM_CONTACT_REQUEST = "Could you share your WhatsApp number so our team can reach you directly?"
_INSTAGRAM_CONTACT_CONFIRM = "I have your WhatsApp number as {number}. Is this correct? Reply yes or no."
_INDIAN_MOBILE_RE = re.compile(
    r"(?<!\d)(?:(?:\+|00)?91[\s-]*)?0?[\s-]*"
    r"([6-9](?:[\s-]*\d){9})(?!\d)"
)


def _extract_whatsapp_number(text: str) -> Optional[str]:
    """Return a normalized Indian WhatsApp number from an Instagram reply."""
    match = _INDIAN_MOBILE_RE.search(text or "")
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return f"+91{digits}" if len(digits) == 10 else None


def _extract_contact_number_from_event(messaging_event: dict) -> Optional[str]:
    """Read a phone number from an Instagram contact-card attachment, if present."""
    message = messaging_event.get("message") or {}
    attachments = message.get("attachments") or []
    if not attachments:
        return None
    # Meta has used more than one attachment shape for shared/contact cards.
    # Searching the serialized attachment is deliberately limited to the
    # contact-collection state by the caller, so image URLs from normal chats
    # cannot accidentally become phone numbers.
    return _extract_whatsapp_number(json.dumps(attachments, ensure_ascii=False))


def _prune_expired_weekly_menus() -> None:
    """
    Drops weekly menus whose printed dates have passed.

    Runs at startup rather than only on upload because the stale entries are
    already sitting in menu.json on the volume, where no code change can reach
    them — the file outlives every deploy by design. The newest entry is never
    removed even when expired: it is the slot the next upload writes into, and
    an expired newest week is handled at reply time by telling the customer the
    menu is being updated.

    Non-fatal by construction. A menu file that cannot be read or written must
    leave the bot answering from whatever it already has rather than refusing
    to start.
    """
    try:
        with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
            menu_data = json.load(f)
        weekly = menu_data.get("weekly_menus") or []
        kept, removed = menu_dates.prune_expired_weeks(weekly)
        if not removed:
            return
        menu_data["weekly_menus"] = kept
        with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(menu_data, f, indent=2, ensure_ascii=False)
        responder.reload_data()
        log.info("Removed %s expired weekly menu(s): %s", len(removed), ", ".join(removed))
    except Exception:
        log.exception("Could not prune expired weekly menus; leaving menu.json untouched.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Enforces database init and the retention window promised on /privacy on startup."""
    # Seeding ran during import, before any path was resolved, so this is the
    # first moment it can be reported.
    for action in STARTUP_ACTIONS:
        log.info("Storage bootstrap: %s", action)

    _prune_expired_weekly_menus()

    db.init_db()
    if not config.META_APP_SECRET and not config.REQUIRE_WEBHOOK_SIGNATURE:
        # CRITICAL rather than WARNING: an unsigned webhook endpoint on a public
        # domain lets anyone POST fake customer messages, which this bot turns
        # into real escalation alerts on the manager's phone.
        log.critical(
            "META_APP_SECRET is not set. Webhook signature verification is DISABLED — "
            "anyone who finds the webhook URL can POST fake customer messages and "
            "trigger real escalation alerts. Set META_APP_SECRET (Meta App Dashboard "
            "> Settings > Basic > App Secret), then set REQUIRE_WEBHOOK_SIGNATURE=true."
        )
    if not (config.ADMIN_API_KEY or config.ADMIN_PIN):
        # require_admin() fails closed, so this is the admin UI being unusable
        # rather than being exposed.
        log.warning(
            "Neither ADMIN_API_KEY nor ADMIN_PIN is set. /admin/* endpoints will "
            "refuse every request with 503 — the dashboard cannot be opened."
        )
    else:
        # A rejected login is otherwise unfalsifiable from the outside: the
        # portal says "Incorrect PIN" whether the value is wrong, quoted, or
        # carries a character that did not survive being pasted. Lengths are
        # enough to spot every one of those and are only visible in the
        # deployment's own logs — never on /health, which is public.
        log.info(
            "Admin credentials configured: ADMIN_PIN %s, ADMIN_API_KEY %s. "
            "If a correct-looking value is refused, compare these lengths with "
            "what you are typing.",
            f"{len(config.ADMIN_PIN)} characters" if config.ADMIN_PIN else "not set",
            f"{len(config.ADMIN_API_KEY)} characters" if config.ADMIN_API_KEY else "not set",
        )
    if not config.WHATSAPP_MOCK_MODE and not (config.WHATSAPP_ACCESS_TOKEN and config.WHATSAPP_PHONE_NUMBER_ID):
        log.critical(
            "WhatsApp is in live mode but WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID "
            "are incomplete. Replies will not be delivered."
        )
    try:
        removed = db.purge_expired_data()
        if any(removed.values()):
            log.info(
                "Retention purge removed %s conversations, %s escalations, %s sessions "
                "older than %s days.",
                removed["conversations"], removed["escalations"], removed["sessions"],
                config.DATA_RETENTION_DAYS,
            )
    except Exception:
        log.exception("Retention purge failed on startup.")

    # The container filesystem does not persist, so a menu edited through /admin
    # survives only until the next deploy, when menu.json reverts to the copy
    # baked into the image. Re-pulling at boot makes the Google Sheet the
    # durable source of truth: whatever the client last put in the sheet is what
    # customers are quoted, no matter how often the service restarts.
    #
    # Deliberately non-fatal. An unreachable or malformed sheet must leave the
    # bot answering from the seed menu rather than refusing to start.
    if config.GOOGLE_SHEET_CSV_URL:
        try:
            result = await asyncio.to_thread(sheet_sync.sync_from_csv_url)
            if result.get("status") == "success":
                log.info(
                    "Menu synced from Google Sheet on startup (%s rows).",
                    result.get("updated_items_count"),
                )
            else:
                log.warning(
                    "Startup menu sync did not apply, serving the menu baked into "
                    "the image instead: %s", result,
                )
        except Exception:
            log.exception(
                "Startup menu sync failed; serving the menu baked into the image."
            )

    yield

limiter = Limiter(key_func=get_remote_address, default_limits=["300/minute"])
app = FastAPI(
    title="FastAPI Webhook Application for Camellia Artisanal — Instagram & WhatsApp Auto-Response Bot",
    version="1.0.0",
    lifespan=lifespan
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Security Headers Middleware (Clickjacking, MIME-sniffing, XSS protection)
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response

# Serve menu photo JPEGs from the dedicated images/ subdirectory.
# Mounting BASE_DIR directly would expose .env, source files and the database.
# This module lives in camellia/, so images/ sits one level up at the repo root.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app.mount("/images", StaticFiles(directory=os.path.join(BASE_DIR, "images")), name="images")

# Serves the operator's uploaded weekly flyer photo. Unlike images/ above, this
# directory lives on the durable volume (see camellia/weekly_flyer.py), so it
# has to be created before StaticFiles can mount it — a fresh volume does not
# have it yet, and StaticFiles raises if the directory is missing at mount time.
os.makedirs(weekly_flyer.uploads_dir(), exist_ok=True)
app.mount("/uploads", StaticFiles(directory=weekly_flyer.uploads_dir()), name="uploads")

def verify_meta_signature(raw_body: bytes, signature_header: Optional[str]) -> None:
    """
    Rejects webhook POSTs that Meta did not sign with our app secret.

    Skipped entirely when META_APP_SECRET is unset so local development and the
    test suite can post unsigned payloads. Always set it in production.
    """
    secret = (config.META_APP_SECRET or "").strip()
    if not secret:
        if config.REQUIRE_WEBHOOK_SIGNATURE:
            log.error(
                "Rejected webhook: REQUIRE_WEBHOOK_SIGNATURE is on but META_APP_SECRET "
                "is not set, so no payload can be verified."
            )
            raise HTTPException(status_code=403, detail="Webhook signature verification is required but not configured")
        return

    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    if not signature_header or not hmac.compare_digest(expected, signature_header):
        log.warning(
            "Rejected webhook payload signature mismatch!\n"
            "  Body length: %d bytes\n"
            "  Secret length: %d (starts with '%s...')\n"
            "  Received Header: %s\n"
            "  Expected HMAC:   %s",
            len(raw_body),
            len(secret),
            secret[:4] if len(secret) >= 4 else "***",
            signature_header,
            expected,
        )
        raise HTTPException(status_code=403, detail="Invalid payload signature")

def require_admin(provided_key: Optional[str] = None, provided_pin: Optional[str] = None) -> None:
    """
    Guards /admin/* routes, which expose customer conversations and can
    overwrite the live menu. Accepts either ADMIN_API_KEY or ADMIN_PIN.
    Fails closed if no admin keys or pins are configured in the environment.
    """
    valid_keys = [k.strip() for k in (config.ADMIN_API_KEY, config.ADMIN_PIN) if k and k.strip()]
    if not valid_keys:
        log.error("Admin access blocked: neither ADMIN_API_KEY nor ADMIN_PIN is configured.")
        raise HTTPException(
            status_code=503,
            detail="Admin interface disabled: no ADMIN_API_KEY or ADMIN_PIN configured in environment"
        )
    token = (provided_key or provided_pin or "").strip()
    if not token or not any(hmac.compare_digest(k, token) for k in valid_keys):
        raise HTTPException(status_code=403, detail="Invalid or missing X-Admin-Key or PIN")

def require_web_tester() -> None:
    """
    Guards the browser chat tester and its APIs.

    These write real conversation rows and fire real escalation alerts to the
    owner's phone, so on a public domain they are abusable by anyone who finds
    the URL. Keep ENABLE_WEB_TESTER false in production; it exists for local
    development, where a browser cannot attach an admin header on navigation.
    """
    if not config.ENABLE_WEB_TESTER:
        raise HTTPException(status_code=404, detail="Web tester is disabled. Set ENABLE_WEB_TESTER=true in environment variables.")

# --------------------------------------------------------------------------
# Root / Health Check / Favicon
# --------------------------------------------------------------------------
@app.get("/")
def read_root(request: Request):
    accept = request.headers.get("accept", "")
    # If opened in a web browser, redirect straight to the chat tester or admin portal
    if "text/html" in accept or "application/xhtml+xml" in accept:
        if config.ENABLE_WEB_TESTER:
            return RedirectResponse(url="/chat")
        return RedirectResponse(url="/admin")
    return {
        "client": "camellia.artisanal",
        "service": "Ad-Gated Auto-Response Bot",
        "status": "running",
        "chat_url": "/chat",
        "admin_url": "/admin"
    }

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content="", media_type="image/x-icon", status_code=204)

# --------------------------------------------------------------------------
# Instagram Webhook Endpoints
# --------------------------------------------------------------------------
@app.get("/webhook/instagram")
def verify_instagram_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge")
):
    """Meta Webhook verification endpoint for Instagram."""
    if (hub_mode in ("subscribe", None)) and hub_verify_token == config.META_VERIFY_TOKEN:
        log.info("Instagram webhook verification successful.")
        return Response(content=str(hub_challenge or ""), media_type="text/plain", status_code=200)
    log.warning("Instagram webhook verification failed (mode=%s).", hub_mode)
    return Response(content="Forbidden", media_type="text/plain", status_code=403)

def _process_instagram_event(
    sender_id: str,
    message_text: str,
    ad_id: Optional[str],
    entry_point: Optional[EntryPoint] = None,
) -> None:
    """
    Matches, logs, escalates and replies for one Instagram DM.

    Runs off the webhook request path. Everything in here is blocking — the
    AI call, sqlite writes, and two outbound Meta calls — and it used to run
    inside the `async` handler, which meant it blocked the event loop and
    serialised every concurrent customer behind whoever arrived first. Meta
    also retries a webhook it believes timed out, so a slow handler earns
    itself duplicate deliveries of the same message.

    Starlette runs a sync background function in a worker thread, so this both
    frees the loop and lets several customers be handled at once.
    """
    try:
        facts = session_memory.get_facts(sender_id, "instagram")
        awaiting_contact = facts.get("awaiting_instagram_contact", False)
        awaiting_contact_confirmation = facts.get("awaiting_instagram_contact_confirmation", False)
        supplied_contact = _extract_whatsapp_number(message_text)

        # How this message was composed, and which ad it came from, recorded on
        # every row this function writes. Phase 1 of the router rollout: until
        # a tap and a typed sentence look different in the data, there is no
        # way to tell whether a question repeated by fifty people is a prompt
        # Instagram is showing them.
        entry = entry_point or EntryPoint()
        trace = {
            "message_source": entry.source,
            # The ad on this event, else the one this session arrived on.
            "ad_id": entry.ad_id or ad_id,
            "ad_title": entry.ad_title or facts.get("ad_title"),
        }
        if entry.ad_title and not facts.get("ad_title"):
            facts["ad_title"] = entry.ad_title
        ad_product = product_for_ad(trace["ad_id"], trace["ad_title"])
        if ad_product:
            facts["ad_product"] = ad_product
        log.info(
            "Instagram inbound from %s: source=%s ad_id=%s ad_title=%r payload=%r",
            sender_id, entry.source, trace["ad_id"], trace["ad_title"], entry.raw_payload,
        )

        if awaiting_contact_confirmation:
            candidate = facts.get("pending_instagram_contact")
            lowered = " ".join((message_text or "").lower().split())
            if supplied_contact:
                candidate = supplied_contact
                facts["pending_instagram_contact"] = candidate
                response_text = _INSTAGRAM_CONTACT_CONFIRM.format(number=candidate)
                db.log_conversation(sender_id, "instagram", message_text, response_text, True, "instagram_contact_confirmation", **trace)
                send_instagram_reply(sender_id, response_text)
                return
            if re.search(r"\b(?:yes|yeah|yep|correct|right|haan|confirm|ok|okay)\b", lowered) and candidate:
                pending = facts.get("pending_instagram_escalation") or {}
                facts["awaiting_instagram_contact_confirmation"] = False
                facts["pending_instagram_contact"] = None
                facts["awaiting_instagram_contact"] = False
                facts["pending_instagram_escalation"] = None
                response_text = pending.get("response_text") or _INSTAGRAM_CONTACT_REQUEST
                was_matched = pending.get("was_matched", False)
                template_id = pending.get("template_id")
                user_message_for_escalation = pending.get("user_message", message_text)
                reason = pending.get("reason", "Unmatched Instagram Query / Sensitive Request")
                session_memory.add_turn(sender_id, "instagram", message_text, response_text)
                escalator.handle_escalation(
                    user_id=sender_id,
                    channel="instagram",
                    user_message=user_message_for_escalation,
                    reason=reason,
                    context_data={
                        "ad_id": ad_id,
                        "sender_id": sender_id,
                        "template_id": template_id,
                        "customer_whatsapp_number": candidate,
                    },
                )
                db.log_conversation(sender_id, "instagram", message_text, response_text, was_matched, template_id, **trace)
                send_instagram_reply(sender_id, response_text)
                return
            if re.search(r"\b(?:no|nope|wrong|incorrect|गलत)\b", lowered):
                facts["awaiting_instagram_contact_confirmation"] = False
                facts["pending_instagram_contact"] = None
                facts["awaiting_instagram_contact"] = True
                response_text = _INSTAGRAM_CONTACT_REQUEST
            else:
                response_text = _INSTAGRAM_CONTACT_CONFIRM.format(number=candidate or "the number you sent")
            db.log_conversation(sender_id, "instagram", message_text, response_text, True, "instagram_contact_confirmation", **trace)
            send_instagram_reply(sender_id, response_text)
            return

        # A customer is allowed to decline a callback.  Clearing all pending
        # state prevents every later sentence from being treated as a missing
        # phone number, while making no claim that staff were notified.
        if awaiting_contact and re.search(
            r"\b(?:no thanks?|not interested|don't need|do not need|nahi chahiye|nahin chahiye)\b",
            " ".join((message_text or "").lower().split()),
        ):
            facts["awaiting_instagram_contact"] = False
            facts["awaiting_instagram_contact_confirmation"] = False
            facts["pending_instagram_contact"] = None
            facts["pending_instagram_escalation"] = None
            response_text = "No problem at all. If you need anything later, just message us here."
            db.log_conversation(sender_id, "instagram", message_text, response_text, True, "instagram_contact_declined", **trace)
            send_instagram_reply(sender_id, response_text)
            return

        if awaiting_contact and not supplied_contact:
            response_text = _INSTAGRAM_CONTACT_REQUEST
            db.log_conversation(sender_id, "instagram", message_text, response_text, True, "instagram_contact_required", **trace)
            send_instagram_reply(sender_id, response_text)
            return

        if awaiting_contact and supplied_contact:
            facts["awaiting_instagram_contact"] = False
            facts["awaiting_instagram_contact_confirmation"] = True
            facts["pending_instagram_contact"] = supplied_contact
            response_text = _INSTAGRAM_CONTACT_CONFIRM.format(number=supplied_contact)
            db.log_conversation(sender_id, "instagram", message_text, response_text, True, "instagram_contact_confirmation", **trace)
            send_instagram_reply(sender_id, response_text)
            return

        response_text, was_matched, template_id, image_url = responder.match_and_respond(
            message_text, user_id=sender_id, channel="instagram",
            referral_source="ADS", referral_ad_id=trace["ad_id"], referral_ad_title=trace["ad_title"],
        )

        # Assisted Resolution is an Instagram-only review draft. It runs only
        # after the deterministic responder declines to resolve the message,
        # never on WhatsApp, and never for an active order/contact flow.
        if (
            config.LLM_ASSISTED_RESOLUTION_ENABLED
            and not was_matched
            and not awaiting_contact
            and not supplied_contact
            and not facts.get("cart")
            and not facts.get("order_draft", {}).get("flavour")
        ):
            create_suggestion(
                user_id=sender_id,
                channel="instagram",
                customer_message=message_text,
                deterministic_resolved=False,
                active_protected_flow=False,
                source_routed=False,
            )

        needs_escalation = awaiting_contact or not was_matched or template_id == "order_next_steps"
        if needs_escalation and not supplied_contact and not awaiting_contact:
            facts["awaiting_instagram_contact"] = True
            facts["awaiting_instagram_contact_confirmation"] = False
            facts["pending_instagram_contact"] = None
            facts["pending_instagram_escalation"] = {
                "response_text": response_text,
                "was_matched": was_matched,
                "template_id": template_id,
                "user_message": message_text,
                "reason": "Order Confirmation & Intake Request" if was_matched else "Unmatched Instagram Query / Sensitive Request",
            }
            response_text = _INSTAGRAM_CONTACT_REQUEST
            session_memory.add_turn(sender_id, "instagram", message_text, response_text)
            db.log_conversation(sender_id, "instagram", message_text, response_text, True, "instagram_contact_required", **trace)
            send_instagram_reply(sender_id, response_text)
            return

        if needs_escalation:
            facts["awaiting_instagram_contact"] = False
            facts["pending_instagram_escalation"] = None
            escalator.handle_escalation(
                user_id=sender_id,
                channel="instagram",
                user_message=message_text,
                reason="Order Confirmation & Intake Request" if was_matched else "Unmatched Instagram Query / Sensitive Request",
                context_data={
                    "ad_id": ad_id,
                    "sender_id": sender_id,
                    "template_id": template_id,
                    **({"customer_whatsapp_number": supplied_contact} if supplied_contact else {}),
                },
            )

        db.log_conversation(sender_id, "instagram", message_text, response_text, was_matched, template_id, **trace)

        send_instagram_reply(sender_id, response_text, image_url=image_url)
    except Exception:
        # A background task that raises disappears silently, and the customer
        # is left with no reply and nothing in the logs explaining why.
        log.exception("Failed to process Instagram message from %s.", sender_id)


# Webhook events the bot received and did not answer. Kept in memory and
# small: this exists to make a silent drop visible within seconds of it
# happening, not to be a second conversation log. The event's shape is
# recorded rather than its content, so no customer message is retained here.
_DROPPED_WEBHOOK_EVENTS: List[Dict[str, Any]] = []
_DROPPED_WEBHOOK_LIMIT = 25


_UNREADABLE_ATTACHMENT_REPLY = (
    "Thanks for sending that! I can't open photos or voice notes here — "
    "could you type your question instead?\n\n"
    "I can help with our Sondesh flavours & prices, this week's Bengali meals, "
    "or delivery across Delhi NCR. If you'd rather speak to someone, our team "
    "is on +91 87965 51990."
)


def _reply_to_unreadable_attachment(sender_id: str) -> None:
    """
    Answers a photo, sticker or voice note we cannot read.

    Sent at most once per conversation. A customer who sends three photos
    should not get the same apology three times — after the first, silence is
    less annoying than a bot repeating itself, and the message already told
    them how to reach a human.
    """
    try:
        facts = session_memory.get_facts(sender_id, "instagram")
        if facts.get("attachment_notice_sent"):
            log.info("Attachment from %s; the type-instead notice was already sent.", sender_id)
            return
        facts["attachment_notice_sent"] = True

        session_memory.add_turn(sender_id, "instagram", "[attachment]", _UNREADABLE_ATTACHMENT_REPLY)
        db.log_conversation(
            sender_id, "instagram", "[attachment]", _UNREADABLE_ATTACHMENT_REPLY,
            True, "unreadable_attachment", message_source="attachment",
        )
        send_instagram_reply(sender_id, _UNREADABLE_ATTACHMENT_REPLY)
    except Exception:
        log.exception("Failed to answer an unreadable attachment from %s.", sender_id)


def _classify_unanswered_event(messaging_event: dict) -> Optional[str]:
    """
    Why an event with no usable text is worth someone's attention, or None.

    Measured against one minute of live traffic: 5 read receipts, 3 echoes of
    our own replies and 16 empty message_edits filled the whole 25-event buffer
    and pushed out the single event that represented a real customer — someone
    who sent a photo and got nothing back.
    """
    if "read" in messaging_event or "delivery" in messaging_event:
        return None  # a receipt, not a message

    message = messaging_event.get("message") or {}
    edit = messaging_event.get("message_edit") or {}
    if message.get("is_echo") or edit.get("is_echo"):
        return None  # our own outbound message coming back

    if messaging_event.get("postback"):
        # A tapped prompt we could not turn into text. This is the one that
        # left a customer staring at silence after tapping "Show me the menu".
        return "postback_without_text"
    if message.get("attachments"):
        # A photo, sticker or voice note. A real person said something we
        # cannot read, and answering nothing is the worst available response.
        return "attachment_only"
    if edit:
        return "message_edit_without_text"
    return "empty_text"


def _record_dropped_webhook_event(reason: str, messaging_event: dict, sender_id: Optional[str]) -> None:
    """Remembers that an event was discarded, and enough to tell why."""
    inner = messaging_event.get("postback") or messaging_event.get("message") or {}
    _DROPPED_WEBHOOK_EVENTS.append({
        "at": datetime.utcnow().isoformat(timespec="seconds"),
        "reason": reason,
        "had_sender": bool(sender_id),
        "event_keys": sorted(messaging_event.keys()),
        "inner_keys": sorted(inner.keys()) if isinstance(inner, dict) else [],
        # The payload identifies which prompt was tapped and is our own string,
        # not anything the customer wrote.
        "postback_payload": (messaging_event.get("postback") or {}).get("payload"),
    })
    del _DROPPED_WEBHOOK_EVENTS[:-_DROPPED_WEBHOOK_LIMIT]


@app.get("/admin/dropped-events")
def get_dropped_webhook_events(
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Recent webhook events the bot received and did not reply to."""
    require_admin(x_admin_key, x_admin_pin)
    return {"count": len(_DROPPED_WEBHOOK_EVENTS), "events": list(reversed(_DROPPED_WEBHOOK_EVENTS))}


def _dispatch_instagram_payload(payload: dict, background_tasks: BackgroundTasks) -> dict:
    log.info("Instagram Webhook Raw Payload:\n%s", json.dumps(payload, indent=2))

    results = []
    for entry in payload.get("entry", []):
        events = list(entry.get("messaging", []))
        if "changes" in entry:
            for change in entry.get("changes", []):
                val = change.get("value")
                if isinstance(val, dict):
                    events.append(val)

        for messaging_event in events:
            is_ad_qualified, sender_id, message_text, ad_id = ad_gate.inspect_and_gate_message(messaging_event)
            entry_point = describe_entry_point(messaging_event)
            message = messaging_event.get("message") or {}

            if not sender_id:
                log.info("Ignored Instagram event without sender ID (keys: %s).", list(messaging_event.keys()))
                results.append({"action": "ignored_no_sender"})
                continue

            if not message_text:
                # A customer may send the WhatsApp number using Instagram's
                # contact-card picker. Meta delivers that as an attachment
                # with no message.text, so treat it as the contact response
                # instead of replying that we cannot read photos or voice notes.
                contact_card_number = _extract_contact_number_from_event(messaging_event)
                if contact_card_number:
                    if session_memory.get_facts(sender_id, "instagram").get("awaiting_instagram_contact"):
                        message_text = contact_card_number
                        log.info("Extracted WhatsApp contact card for Instagram sender %s.", sender_id)
                    else:
                        # The customer may have sent the number as text first;
                        # Meta can then deliver the contact card as a second
                        # event. It is already handled, so do not send the
                        # misleading unreadable-attachment apology.
                        _record_dropped_webhook_event("duplicate_contact_card", messaging_event, sender_id)
                        results.append({"sender_id": sender_id, "action": "ignored_duplicate_contact_card"})
                        continue

            if not message_text:
                # Only record the events that represent a customer going
                # unanswered. Instagram also sends read receipts, delivery
                # receipts and echoes of our own outbound messages, none of
                # which is anyone waiting for a reply — and there are enough of
                # them to fill the buffer in about a minute, burying the one
                # event that actually mattered.
                reason = _classify_unanswered_event(messaging_event)

                # A photo, voice note or sticker is a real person saying
                # something we cannot read. Silence is the worst available
                # answer — they cannot tell whether anyone is there. Asking
                # them to type costs one message and keeps the conversation
                # alive; it was found by the drop log within a minute of that
                # log existing.
                if reason == "attachment_only" and is_ad_qualified:
                    background_tasks.add_task(_reply_to_unreadable_attachment, sender_id)
                    results.append({"sender_id": sender_id, "action": "queued_attachment_reply"})
                    continue

                if reason:
                    _record_dropped_webhook_event(reason, messaging_event, sender_id)
                log.warning(
                    "Ignored Instagram event without text payload (sender: %s, keys: %s, inner: %s).",
                    sender_id, list(messaging_event.keys()),
                    list((messaging_event.get("postback") or messaging_event.get("message") or {}).keys()),
                )
                results.append({"sender_id": sender_id, "action": "ignored_empty_text"})
                continue

            if not is_ad_qualified:
                # Hard Gate: Ignore organic messages per PRD Requirement #4
                log.info("Ad-gate: sender %s is a non-ad organic DM; left for manual reply.", sender_id)
                results.append({"sender_id": sender_id, "action": "ignored_organic_dm"})
                continue

            # Accepted. The reply itself is built and sent off the request path
            # so Meta gets its 200 immediately and does not retry this delivery.
            background_tasks.add_task(
                _process_instagram_event, sender_id, message_text, ad_id, entry_point
            )
            results.append({
                "sender_id": sender_id,
                "action": "queued",
                "ad_id": ad_id,
                "message_source": entry_point.source,
                "ad_title": entry_point.ad_title,
            })

    return {"status": "processed", "results": results}


@app.post("/webhook/instagram")
@limiter.limit("120/minute")
async def handle_instagram_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Handles Instagram DM Webhooks.
    Applies Ad-Gate: Responds ONLY to Ad-originated messages (referral.source == 'ADS').
    """
    raw_body = await request.body()
    verify_meta_signature(raw_body, request.headers.get("X-Hub-Signature-256"))
    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        log.warning("Received malformed JSON on Instagram webhook — ignoring.")
        return {"status": "ignored", "reason": "malformed_json"}
    
    if payload.get("object") not in ("instagram", "page"):
        return {"status": "ignored", "reason": f"Not an instagram or page object (got {payload.get('object')})"}

    return _dispatch_instagram_payload(payload, background_tasks)

def send_instagram_reply(recipient_id: str, text: str, image_url=None) -> Dict[str, Any]:
    """Sends text plus zero or more image cards via Meta Graph API to Instagram user."""
    # Instagram has no caption field on attachments, so a captioned card is sent
    # as the image followed by its own text message — same card, detail, card,
    # detail sequence the customer sees on WhatsApp.
    # Meta fetches a card by URL, so it has to be absolute. This path used to
    # hand it the raw template value — "/images/AAMAR%20MISHTI.jpeg" — which
    # Meta cannot resolve, so the attachment POST failed and took the whole
    # reply down with it: "show me the menu" (captioned cards, sent first)
    # returned nothing at all, and "sondesh price" (uncaptioned, text first)
    # arrived as text with no menu. The WhatsApp adapter always did this
    # conversion; only Instagram was missing it.
    pairs = whatsapp_adapter._normalise_images(image_url)
    captioned = []
    undelivered_captions = []
    for img, caption in pairs:
        public = whatsapp_adapter._public_url(img)
        if not public.startswith(("http://", "https://")):
            log.warning(
                "Instagram card '%s' is relative and PUBLIC_BASE_URL is unset, so Meta "
                "cannot fetch it. Sending the caption as text instead.", img,
            )
            # The caption carries the description, so keep it rather than
            # letting the detail vanish with the card.
            if caption:
                undelivered_captions.append(caption)
            continue
        captioned.append((public, caption))

    if undelivered_captions:
        text = "\n\n".join(undelivered_captions + ([text] if text else []))

    images = [url for url, _ in captioned]
    # The client's requested sequence is note, then the menu it describes —
    # "first the general text, then the aamar collection note, then the aamar
    # collection menu, then signature note, signature menu". Instagram has no
    # caption field, so a card is an image message followed by a text message;
    # this flips that pair so the note introduces its card instead of trailing
    # it. Read off the raw list, the same way text_first is.
    caption_first = isinstance(image_url, list) and any(
        isinstance(entry, dict) and entry.get("caption_first") for entry in image_url
    )
    text_first = isinstance(image_url, list) and any(
        isinstance(entry, dict) and entry.get("text_first") for entry in image_url
    )
    cards_first = any(caption for _, caption in captioned) and not text_first

    if not config.INSTAGRAM_ACCESS_TOKEN:
        safe_text = text[:60].encode("ascii", "backslashreplace").decode("ascii")
        msg_type = f"IMAGE x{len(images)} ({', '.join(images)})" if images else "TEXT"
        log.info("[MOCK IG OUTBOUND] [%s] To: %s | Message: %s...", msg_type, recipient_id, safe_text)
        return {"mock": True, "status": "sent", "recipient": recipient_id, "type": msg_type}

    token = config.INSTAGRAM_ACCESS_TOKEN.strip()
    if token.startswith("IGAA"):
        url = f"https://graph.instagram.com/{config.WHATSAPP_API_VERSION}/me/messages"
    else:
        page_id = config.INSTAGRAM_PAGE_ID or "me"
        url = f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/{page_id}/messages"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            last = None

            def send_text(body):
                nonlocal last
                last = client.post(url, headers=headers, json={"recipient": {"id": recipient_id}, "message": {"text": body}})
                last.raise_for_status()

            if text and not cards_first:
                send_text(text)

            for img, caption in captioned:
                if caption and caption_first:
                    send_text(caption)
                # A rejected card must not cost the customer the whole reply.
                # Meta refuses images for reasons outside our control — an
                # unreachable host, an unsupported format, a size limit — and
                # letting that raise meant the customer got silence instead of
                # the answer we already had.
                try:
                    resp = client.post(url, headers=headers, json={
                        "recipient": {"id": recipient_id},
                        "message": {"attachment": {"type": "image", "payload": {"url": img, "is_reusable": True}}}
                    })
                    resp.raise_for_status()
                    last = resp
                except Exception as img_error:
                    log.warning("Instagram rejected image card %s: %s", img, img_error)
                if caption and not caption_first:
                    send_text(caption)

            # The closing question follows the cards it refers to.
            if text and cards_first:
                send_text(text)

            delivery_monitor.record_success("instagram")
            return last.json() if last is not None else {"status": "no_content"}
    except Exception as e:
        delivery_monitor.record_failure("instagram", str(e))
        if hasattr(e, "response") and getattr(e, "response", None) is not None:
            log.error("Instagram Meta API Error Body: %s", e.response.text)
        return {"error": str(e), "status": "failed"}

# --------------------------------------------------------------------------
# WhatsApp Webhook Endpoints
# --------------------------------------------------------------------------
@app.get("/webhook/whatsapp", response_class=PlainTextResponse)
def verify_whatsapp_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge")
):
    """Meta Webhook verification endpoint for WhatsApp Business API."""
    if hub_mode == "subscribe" and hub_verify_token == config.META_VERIFY_TOKEN:
        log.info("WhatsApp webhook verification successful.")
        return hub_challenge or ""
    log.warning("WhatsApp webhook verification failed: invalid token.")
    raise HTTPException(status_code=403, detail="Verification token mismatch")

def _process_whatsapp_event(sender_phone: str, message_text: str) -> None:
    """
    Handles one WhatsApp message off the webhook request path.

    Same reasoning as the Instagram side: the work is blocking, it used to run
    inside the `async` handler and stall the event loop for every other
    customer, and a slow 200 makes Meta redeliver the message.
    """
    try:
        whatsapp_adapter.process_incoming_message(sender_phone, message_text)
    except Exception:
        log.exception("Failed to process WhatsApp message from %s.", sender_phone)


@app.post("/webhook/whatsapp")
@limiter.limit("120/minute")
async def handle_whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handles WhatsApp Business Webhook events."""
    raw_body = await request.body()
    verify_meta_signature(raw_body, request.headers.get("X-Hub-Signature-256"))
    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        log.warning("Received malformed JSON on WhatsApp webhook — ignoring.")
        return {"status": "ignored", "reason": "malformed_json"}
    if payload.get("object") in ("instagram", "page"):
        return _dispatch_instagram_payload(payload, background_tasks)

    parsed = whatsapp_adapter.parse_webhook_message(payload)
    if not parsed:
        return {"status": "no_message_event"}

    sender_phone, msg_id, message_text = parsed
    background_tasks.add_task(_process_whatsapp_event, sender_phone, message_text)
    return {"status": "queued", "sender_phone": sender_phone, "message_id": msg_id}

# --------------------------------------------------------------------------
# Legal Pages (required by Meta before an app can go Live, and by the
# DPDP Act 2023 because we store customer names and message history)
# --------------------------------------------------------------------------

LEGAL_PAGE_CSS = """
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
           max-width: 760px; margin: 0 auto; padding: 40px 24px 80px; line-height: 1.65;
           color: #2c2420; background: #fdfaf6; }
    h1 { font-size: 28px; margin-bottom: 4px; }
    h2 { font-size: 19px; margin-top: 34px; border-bottom: 1px solid #e8ded2; padding-bottom: 6px; }
    .updated { color: #8a7c6d; font-size: 14px; margin-top: 0; }
    a { color: #8c5a2b; }
    ul { padding-left: 22px; }
    li { margin-bottom: 6px; }
    .box { background: #f5ede2; border-left: 3px solid #8c5a2b; padding: 14px 18px; margin: 22px 0; }
    footer { margin-top: 48px; font-size: 13px; color: #8a7c6d; }
"""


@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy():
    """Privacy policy. Meta requires this URL before the app can go Live."""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy — {config.BUSINESS_LEGAL_NAME}</title>
<style>{LEGAL_PAGE_CSS}</style></head><body>

<h1>Privacy Policy</h1>
<p class="updated">{config.BUSINESS_LEGAL_NAME} &middot; Last updated 5 August 2026</p>

<p>This policy explains what happens to your information when you message
{config.BUSINESS_LEGAL_NAME} on Instagram or WhatsApp and our automated assistant replies.
We have tried to write it in plain language rather than legal boilerplate.</p>

<h2>Who is responsible for your data</h2>
<p>{config.BUSINESS_LEGAL_NAME} is the data fiduciary for the information described here.
For any privacy question or request, contact
<a href="mailto:{config.PRIVACY_CONTACT_EMAIL}">{config.PRIVACY_CONTACT_EMAIL}</a>
or call +91 87965 51990.</p>

<h2>What we collect</h2>
<ul>
  <li><strong>Your messages to us</strong> and the assistant's replies.</li>
  <li><strong>Your Instagram or WhatsApp account identifier</strong>, as supplied by Meta.
      For WhatsApp this is your phone number.</li>
  <li><strong>Your first name</strong>, only if you tell it to us in the chat.</li>
  <li><strong>Whether you arrived via one of our advertisements</strong>, because our assistant
      only replies automatically to enquiries that started from an ad.</li>
</ul>

<div class="box">
  <strong>We do not collect payment card details, addresses, or health information
  through this chat.</strong> Orders are completed with a human being over a separate
  conversation or call.
</div>

<h2>Why we collect it</h2>
<ul>
  <li>To answer your questions about our menu, prices, and delivery.</li>
  <li>To pass your enquiry to a human colleague when the assistant cannot help,
      so that the person picking it up can see what you already asked.</li>
  <li>To count how many questions the assistant answered correctly, so we can improve it.
      This is a simple tally and is not linked back to you.</li>
</ul>

<h2>How long we keep it</h2>
<p>Conversations, enquiry records, and session information are
<strong>automatically deleted after {config.DATA_RETENTION_DAYS} days</strong>.
This deletion is enforced by the software itself, not by someone remembering to do it.</p>

<h2>Who we share it with</h2>
<p>We do not sell your information, and we do not share it with advertisers or data brokers.
Your messages travel through <strong>Meta Platforms</strong> (Instagram and WhatsApp), which
handles them under its own privacy policy — that is unavoidable for any business reachable on
those apps. Beyond Meta, your conversation stays on our own server. No third-party analytics
or advertising service receives it.</p>

<h2>Where it is stored</h2>
<p>On a private server rented by {config.BUSINESS_LEGAL_NAME}, in a database that is not
reachable from the public internet. Access is limited to the business owner and the technical
administrator. Traffic to and from this service is encrypted with HTTPS.</p>

<h2>Your rights</h2>
<p>Under India's Digital Personal Data Protection Act, 2023, you may:</p>
<ul>
  <li>Ask what information we hold about you.</li>
  <li>Ask us to correct anything that is wrong.</li>
  <li><strong>Ask us to delete everything we hold about you</strong>, at any time and without
      giving a reason.</li>
  <li>Withdraw your consent — simply stop messaging us, and the record is deleted within
      {config.DATA_RETENTION_DAYS} days regardless.</li>
  <li>Raise a grievance with us first, and thereafter with the Data Protection Board of India.</li>
</ul>
<p>Email <a href="mailto:{config.PRIVACY_CONTACT_EMAIL}">{config.PRIVACY_CONTACT_EMAIL}</a>
with the phone number or handle you messaged us from. We will action deletion requests
within 30 days.</p>

<h2>Children</h2>
<p>This service is intended for adults placing food orders. We do not knowingly keep
information about anyone under 18.</p>

<h2>Changes to this policy</h2>
<p>If we change how we handle your information, we will update this page and change the date
at the top.</p>

<footer><a href="/terms">Terms of Service</a></footer>
</body></html>"""


@app.get("/terms", response_class=HTMLResponse)
def terms_of_service():
    """Terms of service for the automated chat assistant."""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terms of Service — {config.BUSINESS_LEGAL_NAME}</title>
<style>{LEGAL_PAGE_CSS}</style></head><body>

<h1>Terms of Service</h1>
<p class="updated">{config.BUSINESS_LEGAL_NAME} &middot; Last updated 5 August 2026</p>

<p>These terms cover the automated chat assistant that replies to messages sent to
{config.BUSINESS_LEGAL_NAME} on Instagram and WhatsApp.</p>

<h2>You are talking to software</h2>
<p>Replies are generated automatically. When the assistant is not confident about your
question, it stops and passes you to a human colleague rather than guessing.</p>

<h2>Prices and availability</h2>
<p>Prices quoted in chat are indicative and may change with ingredient costs and seasonal
availability. <strong>An order is confirmed only when a member of our team confirms it
directly with you.</strong> Nothing the assistant says forms a binding contract by itself.
Delivery charges are billed on actuals.</p>

<h2>Delivery</h2>
<p>We deliver within Delhi NCR only, dispatched from Sushant Lok, Gurgaon. Our kitchen
operates 10:00 AM to 7:30 PM daily. Because our sweets are made fresh without preservatives,
we cannot ship them outside our delivery area.</p>

<h2>Food information</h2>
<p>Our sweets are dairy-based and are prepared in a kitchen that handles milk, nuts, and
wheat. <strong>If you have a food allergy, speak to a human colleague before ordering</strong> —
please do not rely on the automated assistant for allergy guidance.</p>

<h2>Fair use</h2>
<p>Please do not use this service to send unlawful, abusive, or automated bulk messages.
We may stop responding to any account that does.</p>

<h2>Availability</h2>
<p>We aim to keep the assistant running at all times but cannot guarantee uninterrupted
service. If it is unavailable, please call +91 87965 51990.</p>

<h2>Governing law</h2>
<p>These terms are governed by the laws of India, with jurisdiction in the courts of Gurugram,
Haryana.</p>

<h2>Contact</h2>
<p>{config.BUSINESS_LEGAL_NAME} &middot;
<a href="mailto:{config.PRIVACY_CONTACT_EMAIL}">{config.PRIVACY_CONTACT_EMAIL}</a>
&middot; +91 87965 51990</p>

<footer><a href="/privacy">Privacy Policy</a></footer>
</body></html>"""


# --------------------------------------------------------------------------
# Admin & Self-Service Endpoints
# --------------------------------------------------------------------------
def _answering_capability() -> dict:
    """
    Reports whether the bot can actually answer anything.

    This exists because a deployment once came up reporting healthy while its
    configured data paths pointed at files that no longer existed: the
    responder held zero templates and every customer got the fallback, and
    nothing in the process considered that a problem. Loading the JSON is not
    the same as being able to answer, so this asserts there is real content
    behind the paths rather than merely that the process started.
    """
    templates = (responder.qa_data or {}).get("templates") or []
    collections = (responder.menu_data or {}).get("collections") or {}
    weekly = (responder.menu_data or {}).get("weekly_menus") or []

    problems = []
    if len(templates) < 10:
        problems.append(
            f"only {len(templates)} Q&A templates loaded from {config.QA_TEMPLATES_PATH}"
        )
    if not collections and not weekly:
        problems.append(f"no menu content loaded from {config.MENU_JSON_PATH}")

    return {
        "ready": not problems,
        "templates_loaded": len(templates),
        "menu_collections": len(collections),
        "weekly_menus": len(weekly),
        "qa_templates_path": config.QA_TEMPLATES_PATH,
        "menu_json_path": config.MENU_JSON_PATH,
        "template_seed": _template_seed_state(),
        "problems": problems,
    }


def _template_seed_state() -> dict:
    """
    Whether the repository's templates have actually reached the live file.

    Compares the seed shipped in this image against the fingerprint recorded
    when the live file was last seeded or reconciled. Those differ only when
    the repository's templates changed and no seed_version bump delivered
    them — which is invisible everywhere else: the deploy succeeds, the
    process is healthy, and customers read the old file.

    An operator editing a template through /admin also moves the live file,
    but not the recorded fingerprint, so that case stays silent as it should.
    """
    from camellia import bootstrap

    seed_path = bootstrap.DATA_DIR / "qa_templates.json"
    state = {
        "seed_version": None,
        "live_seed_version": None,
        "undelivered_seed_changes": False,
    }
    try:
        seed_hash = bootstrap.seed_fingerprint(seed_path)
        state["seed_version"] = bootstrap._seed_version(seed_path)

        live_path = Path(config.QA_TEMPLATES_PATH)
        if live_path.resolve() == seed_path.resolve():
            # No volume in play; the bot is reading the seed itself.
            return state

        with open(live_path, "r", encoding="utf-8") as f:
            live = json.load(f)
        state["live_seed_version"] = live.get("seed_version")
        recorded = live.get("seeded_from_hash")
        if seed_hash and recorded and seed_hash != recorded:
            state["undelivered_seed_changes"] = True
    except Exception as e:
        log.warning("Could not compare the template seed against the live file: %s", e)
    return state


def _storage_durability() -> dict:
    """
    Reports where the database and menu live, and whether that survives a deploy.

    This was invisible from outside, which made a destructive question
    unanswerable without dashboard access: a container filesystem is wiped on
    every deploy, so an unmounted database silently loses every conversation,
    escalation and approved template each time the service restarts. The bot
    looks perfectly healthy either way.

    A path is treated as durable only if it sits under a mount point kept
    outside the image (/app/var by convention here, see docker-compose.yml).
    That is a strong hint, not proof — the volume still has to be attached in
    the host's dashboard.
    """
    durable_prefixes = ("/app/var", "/var/lib/camellia", "/data", "/mnt")

    def _placement(path: str) -> dict:
        # Compared as configured rather than through os.path.abspath, which is
        # platform-dependent: on Windows it rewrites "/app/var/x" to
        # "C:\app\var\x" and no deployment path would ever match.
        normalised = (path or "").replace("\\", "/")
        return {
            "path": normalised,
            "expected_durable": normalised.startswith(durable_prefixes),
        }

    database = _placement(config.DB_PATH)
    menu = _placement(config.MENU_JSON_PATH)
    return {
        "database": database,
        "menu": menu,
        # The menu has a durable source that is not the filesystem, so an
        # ephemeral path for it is only a problem when no sheet is configured.
        "menu_reseeded_from_google_sheet_on_boot": bool(config.GOOGLE_SHEET_CSV_URL),
    }


def _delivery_capability() -> dict:
    """
    Reports whether a reply would actually reach a customer.

    Mock mode is the failure worth naming here: it never constructs an outbound
    request, so the bot looks entirely healthy while every customer sits in
    silence. That is indistinguishable from working unless something says so.
    """
    if config.WHATSAPP_MOCK_MODE:
        # Not counted as unhealthy: mock is the correct and default state for
        # local development and the test suite, and failing health there would
        # train everyone to ignore a red health check. It is surfaced as a
        # warning instead, so an operator looking at a deployed bot can see it.
        return {
            "delivering": False,
            "mode": "mock",
            "problem": "WHATSAPP_MOCK_MODE is on — replies are logged, never sent to customers.",
        }
    missing = [
        name for name, value in (
            ("WHATSAPP_ACCESS_TOKEN", config.WHATSAPP_ACCESS_TOKEN),
            ("WHATSAPP_PHONE_NUMBER_ID", config.WHATSAPP_PHONE_NUMBER_ID),
        ) if not value
    ]
    return {
        "delivering": not missing,
        "mode": "live",
        "problem": f"missing {', '.join(missing)}" if missing else None,
    }


def _webhook_signature_status() -> str:
    """How webhook payloads are being authenticated, in one word."""
    if config.META_APP_SECRET:
        return "enforced"
    if config.REQUIRE_WEBHOOK_SIGNATURE:
        return "required_but_unconfigured"
    return "DISABLED"


def _instagram_profile_url_and_identity() -> Tuple[str, str]:
    """The messenger_profile URL for whichever token flavour is configured."""
    token = (config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    if token.startswith("IGAA"):
        return (
            f"https://graph.instagram.com/{config.WHATSAPP_API_VERSION}/me/messenger_profile",
            "instagram_login_token (graph.instagram.com/me)",
        )
    page_id = config.INSTAGRAM_PAGE_ID or "me"
    return (
        f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/{page_id}/messenger_profile",
        f"page_token (graph.facebook.com/{page_id})",
    )


# An expired Instagram token is the worst failure this service has: webhooks
# keep arriving and every reply silently fails to send, while /health stays
# green. It happened — Meta invalidated the token after an account password
# change, and the only trace was a consecutive-failure counter that resets on
# every deploy, so nothing ever reported it.
#
# Checking costs a call to Meta, so the result is cached: an uptime monitor
# polling /health every thirty seconds must not turn into a Graph API call
# every thirty seconds.
_IG_TOKEN_TTL_SECONDS = 600.0
_IG_TOKEN_TIMEOUT_SECONDS = 6.0
_ig_token_state: Dict[str, Any] = {"checked_at": 0.0, "result": None}


def instagram_token_status(force: bool = False) -> Dict[str, Any]:
    """
    Whether Meta still accepts the Instagram token, cached for ten minutes.

    States: `not_configured` (no token — mock mode, normal in development),
    `valid`, `invalid` (Meta rejected the credential itself), and `unknown`
    (we could not reach Meta, which is not evidence of anything).

    `unknown` is deliberately not an alarm. A network blip must not be
    reported as a dead credential, or the warning stops meaning anything.
    """
    token = (config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    if not token:
        return {"state": "not_configured", "detail": "INSTAGRAM_ACCESS_TOKEN is not set."}

    now = time.time()
    cached = _ig_token_state.get("result")
    if not force and cached and (now - _ig_token_state["checked_at"]) < _IG_TOKEN_TTL_SECONDS:
        return {**cached, "checked_seconds_ago": round(now - _ig_token_state["checked_at"], 1)}

    url, identity = _instagram_profile_url_and_identity()
    try:
        with httpx.Client(timeout=_IG_TOKEN_TIMEOUT_SECONDS) as client:
            resp = client.get(url, params={"fields": "ice_breakers", "access_token": token})
        if resp.status_code == 200:
            result = {"state": "valid", "identity": identity, "detail": "Meta accepted the token."}
        else:
            error = (resp.json() or {}).get("error", {}) if resp.content else {}
            code = error.get("code")
            # 190 is the credential itself being rejected — expired, revoked,
            # or invalidated by a password change. Other statuses may be a
            # permission or a path problem, which is a different conversation.
            state = "invalid" if code == 190 or error.get("type") == "OAuthException" else "unknown"
            result = {
                "state": state,
                "identity": identity,
                "detail": error.get("message") or f"HTTP {resp.status_code}",
                "meta_code": code,
            }
    except Exception as e:
        result = {
            "state": "unknown",
            "identity": identity,
            "detail": f"Could not reach Meta: {type(e).__name__}",
        }

    _ig_token_state["result"] = result
    _ig_token_state["checked_at"] = now
    if result["state"] == "invalid":
        log.error(
            "Instagram access token rejected by Meta (%s). Replies cannot be "
            "delivered until INSTAGRAM_ACCESS_TOKEN is renewed: %s",
            result.get("meta_code"), result.get("detail"),
        )
    return {**result, "checked_seconds_ago": 0.0}


@app.get("/health")
def health_check():
    """Deep health check. Point an uptime monitor at this, not at /."""
    outbound = delivery_monitor.snapshot()
    answering = _answering_capability()
    delivery = _delivery_capability()
    signature = _webhook_signature_status()
    storage = _storage_durability()
    instagram_token = instagram_token_status()

    # Which phone actually receives escalation alerts. The environment
    # overrides the code default, and the environment is a dashboard nothing
    # here can read — so changing the number in code and assuming it took is
    # the same mistake the undelivered templates and the dead token both were.
    # Masked to the last four digits: enough to tell two numbers apart, not
    # enough to publish someone's phone on an unauthenticated endpoint.
    escalation_phone = (config.HUMAN_ESCALATION_PHONE or "").strip()
    alert_destination = {
        "configured": bool(escalation_phone),
        "ends_with": escalation_phone[-4:] if len(escalation_phone) >= 4 else None,
        "from_environment": bool(os.getenv("HUMAN_ESCALATION_PHONE")),
    }

    # Shadow mode is switched on in the host dashboard, which nothing in this
    # repo can read. Without this, turning it on and hoping is the only option.
    router_shadow: Dict[str, Any] = {
        "enabled": config.ROUTER_SHADOW_ENABLED,
        "model": config.GROQ_ROUTER_MODEL if config.ROUTER_SHADOW_ENABLED else None,
    }
    if config.ROUTER_SHADOW_ENABLED:
        try:
            router_shadow.update(db.router_shadow_pulse())
        except Exception as e:
            router_shadow["error"] = f"{type(e).__name__}"
            log.warning("Could not read the router shadow pulse: %s", e)

    # A bot that cannot answer is down, whatever else is working. Live mode
    # without credentials is the same kind of failure one step later — it
    # produces a reply that cannot be delivered.
    live_but_broken = delivery["mode"] == "live" and not delivery["delivering"]
    healthy = outbound["healthy"] and answering["ready"] and not live_but_broken
    if not answering["ready"]:
        log.error("Health check failed — bot cannot answer: %s", "; ".join(answering["problems"]))
    if live_but_broken:
        log.error("Health check failed — replies cannot be delivered: %s", delivery["problem"])

    # Things that are not outages but that an operator needs to see, because
    # each of them looks exactly like a working bot from the outside.
    warnings = []
    if delivery["mode"] == "mock":
        warnings.append(delivery["problem"])
    if signature == "DISABLED":
        warnings.append("META_APP_SECRET unset — webhook payloads are not verified.")
    if config.ENABLE_WEB_TESTER:
        warnings.append("ENABLE_WEB_TESTER is on — /chat is reachable by anyone with the URL.")
    if not storage["database"]["expected_durable"]:
        warnings.append(
            f"Database is at {storage['database']['path']}, which is inside the "
            "container — conversations, escalations and approved templates are "
            "lost on every deploy unless a volume is mounted there."
        )
    if not storage["menu"]["expected_durable"] and not storage["menu_reseeded_from_google_sheet_on_boot"]:
        warnings.append(
            f"Menu is at {storage['menu']['path']} with no GOOGLE_SHEET_CSV_URL set — "
            "menu edits are lost on every deploy."
        )
    if instagram_token.get("state") == "invalid":
        # A warning rather than a 503, on purpose. This genuinely is an outage
        # of the Instagram channel, but the health endpoint is what the host
        # restarts the service on — and returning 503 for a dead credential
        # would take WhatsApp down too, to fix nothing. Only a new token in the
        # environment can resolve this, so it is reported as loudly as possible
        # without knocking over the half that still works.
        warnings.append(
            "Instagram access token is rejected by Meta "
            f"({instagram_token.get('detail')}). DMs are still being received "
            "and every reply is failing to send. Renew INSTAGRAM_ACCESS_TOKEN."
        )
    seed_state = answering.get("template_seed") or {}
    if seed_state.get("undelivered_seed_changes"):
        warnings.append(
            "data/qa_templates.json has changes customers are not being served. "
            f"The live file was seeded from an older version of it (seed_version "
            f"{seed_state.get('live_seed_version')}, image ships "
            f"{seed_state.get('seed_version')}). Bump seed_version in "
            "data/qa_templates.json and redeploy, or push the template through "
            "POST /admin/templates."
        )

    return Response(
        content=json.dumps({
            "status": "healthy" if healthy else "degraded",
            "warnings": warnings,
            "answering": answering,
            "storage": storage,
            "reply_delivery": delivery,
            "webhook_signature": signature,
            "outbound_delivery": outbound,
            "instagram_token": instagram_token,
            "escalation_alerts_to": alert_destination,
            "router_shadow": router_shadow,
            "language_understanding": {
                "structured_nlu_enabled": config.STRUCTURED_NLU_ENABLED,
                "rollout_percent": config.STRUCTURED_NLU_ROLLOUT_PERCENT,
            },
            "session_state": {
                "persistent": config.SESSION_PERSISTENCE_ENABLED,
                "ttl_seconds": config.SESSION_TTL_SECONDS,
            },
            # Not just which provider is configured — whether it last worked.
            "ai": ai_responder.status(),
            "reply_latency": latency_monitor.snapshot(),
            "slow_reply_notice_active": config.is_slow_reply_notice_active,
            "retention_days": config.DATA_RETENTION_DAYS,
        }),
        media_type="application/json",
        status_code=200 if healthy else 503,
    )

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB limit for menu images


@app.get("/admin/menu-manager/menus")
def menu_manager_menus(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    return {"status": "success", "menus": menu_images.list_menus()}


@app.get("/admin/menu-manager/menus/{menu_key}")
def menu_manager_preview(menu_key: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        return {"status": "success", **menu_images.preview(menu_key)}
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown menu")


@app.get("/admin/menu-manager/menus/{menu_key}/preview")
def menu_manager_preview_alias(menu_key: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    return menu_manager_preview(menu_key, x_admin_key, x_admin_pin)


async def _save_menu_image_draft(menu_key: str, file: Optional[UploadFile], x_admin_key: Optional[str], x_admin_pin: Optional[str]):
    require_admin(x_admin_key, x_admin_pin)
    if file is None:
        raise HTTPException(status_code=400, detail="No image file provided.")
    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large (maximum 10MB allowed).")
    try:
        return {"status": "success", "draft": menu_images.save_draft(menu_key, data, file.content_type or "", "admin")}
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown menu")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/admin/menu-manager/menus/{menu_key}/draft")
async def menu_manager_save_draft(menu_key: str, file: Optional[UploadFile] = File(None), x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    return await _save_menu_image_draft(menu_key, file, x_admin_key, x_admin_pin)


@app.put("/admin/menu-manager/menus/{menu_key}/draft")
async def menu_manager_save_draft_put(menu_key: str, file: Optional[UploadFile] = File(None), x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None),):
    return await _save_menu_image_draft(menu_key, file, x_admin_key, x_admin_pin)


@app.post("/admin/menu-manager/menus/{menu_key}/replace")
async def menu_manager_replace(menu_key: str, file: Optional[UploadFile] = File(None), x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    return await _save_menu_image_draft(menu_key, file, x_admin_key, x_admin_pin)


@app.post("/admin/menu-manager/menus/{menu_key}/publish")
def menu_manager_publish(menu_key: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        return {"status": "success", "menu": menu_images.publish(menu_key)}
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown menu")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/admin/menu-manager/menus/{menu_key}/history")
def menu_manager_history(menu_key: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    if menu_key not in menu_images.MENU_IMAGE_DEFINITIONS:
        raise HTTPException(status_code=404, detail="Unknown menu")
    return {"status": "success", "versions": menu_images.history(menu_key)}


@app.post("/admin/menu-manager/menus/{menu_key}/archive")
@app.delete("/admin/menu-manager/menus/{menu_key}")
def menu_manager_archive(menu_key: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    if menu_key not in menu_images.MENU_IMAGE_DEFINITIONS:
        raise HTTPException(status_code=404, detail="Unknown menu")
    return {"status": "success", "menu": menu_images.archive(menu_key)}


@app.post("/admin/menu-manager/menus/{menu_key}/restore/{version_id}")
def menu_manager_restore(menu_key: str, version_id: int, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    if menu_key not in menu_images.MENU_IMAGE_DEFINITIONS:
        raise HTTPException(status_code=404, detail="Unknown menu")
    restored = menu_images.restore(menu_key, version_id)
    if not restored:
        raise HTTPException(status_code=404, detail="Version not found")
    return {"status": "success", "menu": restored}

@app.post("/admin/verify-pin")
@limiter.limit("10/minute")
async def verify_admin_pin(request: Request):
    """Validates the 5-digit PIN or API key for the Admin dashboard."""
    try:
        body = await request.json()
        pin = str(body.get("pin", "")).strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    valid_keys = [k.strip() for k in (config.ADMIN_PIN, config.ADMIN_API_KEY) if k and k.strip()]
    if not valid_keys:
        log.error("Admin PIN verification blocked: no credentials configured in environment.")
        raise HTTPException(
            status_code=503, 
            detail="Admin interface disabled: no ADMIN_PIN or ADMIN_API_KEY configured in environment"
        )

    if pin and any(hmac.compare_digest(k, pin) for k in valid_keys):
        return {"valid": True, "message": "PIN verified successfully."}

    raise HTTPException(status_code=401, detail="Incorrect Admin PIN or Key")

@app.get("/admin/menu/active")
def get_active_menu(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """Returns the currently active menu JSON data."""
    require_admin(x_admin_key, x_admin_pin)
    try:
        with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Surfaced so the dashboard can show staff which week is actually
        # live, since the flyer photo and the dish text are now two separate
        # things that both have to agree with what was last uploaded.
        return {
            "status": "success",
            "menu": data,
            "weekly_flyer": weekly_flyer.get(),
            # Superseded flyers, newest first, already pruned to the two-week
            # window. Shown in the dashboard so the kitchen can see what a
            # customer asking for "last week's menu" would be sent.
            "flyer_archive": weekly_flyer.list_archive(),
        }
    except Exception as e:
        log.error("Failed to read menu.json: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/ad-routes")
def get_ad_routes(
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Lists the Instagram ads that have a kitchen-managed product route."""
    require_admin(x_admin_key, x_admin_pin)
    return {"status": "success", "routes": db.list_ad_product_routes()}


@app.post("/admin/ad-routes")
async def save_ad_route(
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Maps one Meta Ad ID to the product its conversation should lead with."""
    require_admin(x_admin_key, x_admin_pin)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    ad_id = str(payload.get("ad_id") or "").strip()
    product = str(payload.get("product") or "").lower().strip()
    if not ad_id or len(ad_id) > 200 or not re.fullmatch(r"[A-Za-z0-9_-]+", ad_id):
        raise HTTPException(status_code=400, detail="Enter a valid Meta Ad ID.")
    if product not in {"sondesh", "savoury", "bento"}:
        raise HTTPException(status_code=400, detail="Choose Sondesh, Savoury, or Bento.")

    route = db.set_ad_product_route(ad_id, product)
    return {"status": "success", "route": route}


@app.delete("/admin/ad-routes/{ad_id}")
def remove_ad_route(
    ad_id: str,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Stops applying a saved product preference to one Instagram ad."""
    require_admin(x_admin_key, x_admin_pin)
    if not db.delete_ad_product_route(ad_id):
        raise HTTPException(status_code=404, detail="Ad route not found.")
    return {"status": "success", "ad_id": ad_id}

@app.post("/admin/menu/upload-image")
@limiter.limit("10/minute")
async def upload_menu_image(
    request: Request,
    file: Optional[UploadFile] = File(None),
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Multimodal Menu Ingestion:
    Uploads a photo of a printed menu flyer or Instagram graphic, extracts dishes and prices with a Groq vision model,
    and updates menu.json with hot reload.
    """
    require_admin(x_admin_key, x_admin_pin)

    image_bytes = None
    mime_type = "image/jpeg"

    if file:
        image_bytes = await file.read()
        if len(image_bytes) > MAX_UPLOAD_SIZE:
            raise HTTPException(status_code=413, detail="File too large (maximum 10MB allowed).")
        mime_type = file.content_type or "image/jpeg"
    else:
        try:
            body = await request.json()
            b64_str = body.get("image_base64")
            mime_type = body.get("mime_type", "image/jpeg")
            if b64_str:
                if len(b64_str) > (MAX_UPLOAD_SIZE * 4 // 3 + 1024):
                    raise HTTPException(status_code=413, detail="Image payload too large (maximum 10MB allowed).")
                import base64
                if "," in b64_str:
                    b64_str = b64_str.split(",", 1)[1]
                image_bytes = base64.b64decode(b64_str)
                if len(image_bytes) > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail="Image payload exceeds 10MB limit.")
        except HTTPException:
            raise
        except Exception:
            pass

    if not image_bytes:
        raise HTTPException(status_code=400, detail="No image file or base64 data provided.")

    result = menu_parser.parse_image_and_update_menu(image_bytes, mime_type)

    # The parser writes the extracted text into menu.json but discards the
    # photo itself, so the photo is saved here — "show me the menu" sends this
    # actual upload instead of one of the two fixed images in the repo.
    #
    # Saved whether or not extraction succeeded. The vision model fails on
    # perfectly good flyers (a malformed-JSON completion is enough), and
    # gating the photo on that meant one bad model run made the whole upload
    # do nothing at all: the operator posted this week's menu, got a "failed"
    # toast, and customers kept seeing last week's. The photo is the thing
    # customers actually read; the extracted text is what powers dish prices
    # and ordering. Losing the second is a degraded menu, losing the first is
    # no menu, so they are no longer tied together.
    #
    # week_of only comes along when extraction produced one. An unreadable
    # date means a generic caption rather than a wrong one, and an undated
    # flyer never expires — deliberately, since hiding a good menu is worse
    # than showing one a few days longer (see camellia/menu_dates.py).
    flyer_saved = False
    try:
        weekly_flyer.save(
            image_bytes, mime_type,
            week_of=result.get("week_of"),
            service_window=result.get("service_window"),
        )
        # A new flyer is an explicit instruction to make this week's menu
        # visible again, including when OCR could not read the dish rows.
        with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
            menu_data = json.load(f)
        if menu_data.get("weekly_menus"):
            menu_data["weekly_menus"][-1]["is_published"] = True
            with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(menu_data, f, indent=2, ensure_ascii=False)
            responder.reload_data()
        flyer_saved = True
    except Exception as e:
        log.warning("Could not save the uploaded flyer photo: %s", e)

    # The operator has to be able to tell the two halves apart, or a "failed"
    # result reads as "nothing happened" when the photo did in fact go live.
    result["flyer_saved"] = flyer_saved
    if flyer_saved and result.get("status") != "success":
        result["flyer_notice"] = (
            "The menu photo is live and customers will now be sent it. "
            "The dish text could not be read off this image, so item prices "
            "still come from the previous menu — re-upload a clearer photo, "
            "or edit the dishes by hand, to update those too."
        )

    return result


@app.post("/admin/menu/unpublish-current")
def unpublish_current_menu(
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Hide the active flyer and dishes until the kitchen publishes a replacement."""
    require_admin(x_admin_key, x_admin_pin)
    try:
        with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
            menu_data = json.load(f)
        if not menu_data.get("weekly_menus"):
            raise HTTPException(status_code=404, detail="No weekly menu is available to hide")
        menu_data["weekly_menus"][-1]["is_published"] = False
        with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(menu_data, f, indent=2, ensure_ascii=False)
        flyer_removed = weekly_flyer.unpublish_current()
        responder.reload_data()
        return {
            "status": "success",
            "flyer_removed": flyer_removed,
            "message": "This week's menu is hidden from customers until a new menu is published.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Could not unpublish current menu: %s", exc)
        raise HTTPException(status_code=500, detail="Could not hide this week's menu")

@app.post("/admin/menu/archive/{flyer_id}/remove")
async def remove_archived_flyer(flyer_id: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """
    Deletes one archived weekly menu, so customers stop being offered it.

    Retention drops these after two weeks on its own, but a menu can be wrong
    long before it is old, and until now waiting was the only option.
    """
    require_admin(x_admin_key, x_admin_pin)
    try:
        label = weekly_flyer.remove_from_archive(flyer_id)
    except ValueError as exc:
        # The live flyer is not history; refusing by name beats a 500.
        raise HTTPException(status_code=400, detail=str(exc))
    if label is None:
        raise HTTPException(status_code=404, detail="No archived menu with that id")
    return {"status": "success", "removed": label}


@app.post("/admin/menu/archive/{flyer_id}/replace")
async def replace_archived_flyer(flyer_id: str, file: UploadFile = File(...), x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """
    Swaps the photo on one archived menu, keeping its date and its place.

    Re-uploading through the main form would make it this week's menu, which
    is not what fixing a badly shot old flyer means.
    """
    require_admin(x_admin_key, x_admin_pin)
    image_bytes = await file.read()
    try:
        updated = weekly_flyer.replace_archived(flyer_id, image_bytes, file.content_type or "image/jpeg")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if updated is None:
        raise HTTPException(status_code=404, detail="No archived menu with that id")
    return {"status": "success", "week_of": updated.get("week_of"), "url": updated.get("url")}


@app.post("/admin/menu/reset-defaults")
def reset_menu_defaults(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """Resets weekly menu dishes to default Bengal-rooted culinary baseline and reloads."""
    require_admin(x_admin_key, x_admin_pin)
    # A photo uploaded earlier must not survive a reset that claims to restore
    # the baseline — otherwise the dish text resets while the card still shows
    # last week's flyer, and "Reset to Baseline" would not mean what it says.
    weekly_flyer.clear()
    default_veg = [
        {"item": "Chholar Daal", "description": "Authentic Bengali gram dal with coconut chips", "price_inr": 250, "quantity": "250g"},
        {"item": "Dhokar Dalna", "description": "Lentil cakes in rich ginger-cumin gravy", "price_inr": 300, "quantity": "250g"},
        {"item": "Shukto", "description": "Traditional bitter-sweet medley with bodi & milk gravy", "price_inr": 280, "quantity": "250g"},
        {"item": "Chanar Dalna", "description": "Fresh handmade cottage cheese koftas in spiced curry", "price_inr": 350, "quantity": "250g"},
        {"item": "Tomato Aamsottwo Chatni", "description": "Sweet tangy tomato chutney with sun-dried mango pulp", "price_inr": 180, "quantity": "150g"}
    ]
    default_nonveg = [
        {"item": "Mangsho (Mutton Kosha)", "description": "Slow-cooked artisanal Bengali goat meat curry", "price_inr": 650, "quantity": "350g"},
        {"item": "Rui Machher Kalia", "description": "Fresh river carp in robust onion-raisin gravy", "price_inr": 450, "quantity": "2 pcs"},
        {"item": "Chingri Malaikari", "description": "Gold tiger prawns in creamy coconut milk sauce", "price_inr": 550, "quantity": "4 pcs"},
        {"item": "Chicken Kosha", "description": "Tender chicken braised with mustard oil & spices", "price_inr": 420, "quantity": "350g"}
    ]

    menu_path = config.MENU_JSON_PATH
    try:
        with open(menu_path, "r", encoding="utf-8") as f:
            menu_data = json.load(f)
        if "weekly_menus" in menu_data and menu_data["weekly_menus"]:
            current_week = menu_data["weekly_menus"][-1]
            current_week["vegetarian"] = default_veg
            current_week["non_vegetarian"] = default_nonveg
            with open(menu_path, "w", encoding="utf-8") as f:
                json.dump(menu_data, f, indent=2, ensure_ascii=False)
            sheet_sync.sync_local_reload()
            return {"status": "success", "message": "Weekly menu successfully reset to authentic Bengali culinary defaults."}
        else:
            return {"status": "failed", "reason": "No weekly_menus block found in menu.json"}
    except Exception as e:
        log.error("Failed to reset menu defaults: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/admin/user-data/{user_id}")
def delete_user_data(user_id: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """Erases everything held about one customer, for DPDP deletion requests."""
    require_admin(x_admin_key, x_admin_pin)
    removed = db.delete_user_data(user_id)
    session_memory.clear_session(user_id, "instagram")
    session_memory.clear_session(user_id, "whatsapp")
    log.info("Erased all stored data for user %s: %s", user_id, removed)
    return {"status": "deleted", "user_id": user_id, "removed": removed}

@app.get("/admin/escalations")
def get_escalations_list(limit: int = 50, status: Optional[str] = None, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """Returns recorded human escalations for client dashboard/records."""
    require_admin(x_admin_key, x_admin_pin)
    escalations = db.get_escalations(limit, status)
    return {"count": len(escalations), "escalations": escalations}

@app.get("/admin/analytics")
def get_analytics(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """Returns match rate vs escalation analytics summary."""
    require_admin(x_admin_key, x_admin_pin)
    return db.get_analytics()

@app.get("/admin/entry-points")
def get_entry_points(
    channel: str = "instagram",
    days: int = 30,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Where inbound messages are coming from, and which questions repeat.

    Reads the evidence for two open questions: whether the repeated
    "do you deliver outside Delhi NCR" messages are taps on a prompt or people
    typing, and whether Instagram sends us an ad title we could use to tell
    which product a customer is asking about.
    """
    require_admin(x_admin_key, x_admin_pin)
    return db.entry_point_breakdown(channel=channel, days=days)

@app.get("/admin/router-shadow")
def get_router_shadow_report(
    days: int = 7,
    limit: int = 40,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    How the LLM router would have routed, compared with the keyword matcher.

    Phase 3 of the rollout: the router picks in silence and nothing it decides
    reaches a customer. This is the evidence for whether it is trusted with
    live traffic — and the disagreements matter more than the agreement rate,
    because on the phrasings the trigger list never anticipated the matcher is
    the side expected to be wrong.
    """
    require_admin(x_admin_key, x_admin_pin)
    report = db.router_shadow_report(days=days, limit=limit)
    report["shadow_enabled"] = config.ROUTER_SHADOW_ENABLED
    if not config.ROUTER_SHADOW_ENABLED and not report["compared"]:
        report["note"] = "ROUTER_SHADOW_ENABLED is off, so nothing is being compared."
    return report

# The four prompts Instagram shows before a customer types anything. Each one
# was run through the matcher first: an opener the bot answers badly is worse
# than no opener at all, because every customer sees it.
#
#   Show me the menu               -> know_more, sends all three cards
#   Sondesh flavours & prices      -> sondesh_price_detailed
#   Do you deliver to my area?     -> delivery_area, then the area flow
#   I would like to place an order -> how_to_order
#
# The payloads say ICE_BREAKER on purpose: that is what describe_entry_point
# matches on, so a tap is recorded as a tap rather than as someone typing.
# Defined in camellia/ice_breakers.py so the webhook path can understand a tap
# on one without importing app.py, which would be a cycle.


@app.post("/admin/instagram/ice-breakers")
async def set_instagram_ice_breakers(
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Writes the Ice Breakers shown to every customer who opens a DM.

    Separate from the GET on purpose. This one changes what a stranger sees
    before they have typed a word, so it is a deliberate action taken once,
    not something a diagnostic does on the way past.

    POST an empty body for the four defaults, or {"questions": [...]} to
    choose your own. DELETE removes them and restores the blank state.
    """
    require_admin(x_admin_key, x_admin_pin)

    token = (config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="INSTAGRAM_ACCESS_TOKEN is not set on this service.")

    try:
        body = await request.json()
    except Exception:
        body = {}

    requested = body.get("questions") or DEFAULT_ICE_BREAKERS
    if not isinstance(requested, list) or not requested:
        raise HTTPException(status_code=400, detail="questions must be a non-empty list.")
    if len(requested) > MAX_ICE_BREAKERS:
        raise HTTPException(
            status_code=400,
            detail=f"Instagram allows at most {MAX_ICE_BREAKERS} ice breakers; {len(requested)} given.",
        )

    call_to_actions = []
    for index, item in enumerate(requested):
        question = (item.get("question") or "").strip() if isinstance(item, dict) else str(item).strip()
        if not question:
            raise HTTPException(status_code=400, detail=f"Ice breaker {index + 1} has no question text.")
        if len(question) > MAX_ICE_BREAKER_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"Ice breaker {index + 1} is {len(question)} characters; the limit is {MAX_ICE_BREAKER_CHARS}.",
            )
        payload = (item.get("payload") if isinstance(item, dict) else None) or f"ICE_BREAKER_{index + 1}"
        call_to_actions.append({"question": question, "payload": payload})

    url, identity = _instagram_profile_url_and_identity()
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                url,
                params={"access_token": token},
                json={"ice_breakers": [{"call_to_actions": call_to_actions, "locale": "default"}]},
            )
        result = resp.json() if resp.content else {}
    except Exception as e:
        log.warning("Could not set Instagram ice breakers: %s", e)
        raise HTTPException(status_code=502, detail=f"Could not reach Meta: {type(e).__name__}")

    if resp.status_code != 200:
        error = result.get("error", {}) if isinstance(result, dict) else {}
        log.error("Meta rejected the ice breaker write: %s", error or resp.text[:200])
        raise HTTPException(
            status_code=502,
            detail=f"Meta rejected the change: {error.get('message') or resp.text[:200]}",
        )

    log.info("Instagram ice breakers set (%s): %s", identity, [c["question"] for c in call_to_actions])
    # The write having been accepted is not the same as it being live, so the
    # state is read back rather than reported from what we just sent.
    return {
        "status": "set",
        "identity": identity,
        "questions": [c["question"] for c in call_to_actions],
        "meta_response": result,
        "now_live": get_instagram_messaging_profile(x_admin_key=x_admin_key, x_admin_pin=x_admin_pin),
    }


@app.delete("/admin/instagram/ice-breakers")
def clear_instagram_ice_breakers(
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Removes the Ice Breakers, restoring the blank state they were in."""
    require_admin(x_admin_key, x_admin_pin)

    token = (config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="INSTAGRAM_ACCESS_TOKEN is not set on this service.")

    url, identity = _instagram_profile_url_and_identity()
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.request(
                "DELETE", url, params={"access_token": token}, json={"fields": ["ice_breakers"]},
            )
        result = resp.json() if resp.content else {}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Meta: {type(e).__name__}")

    if resp.status_code != 200:
        error = result.get("error", {}) if isinstance(result, dict) else {}
        raise HTTPException(
            status_code=502,
            detail=f"Meta rejected the removal: {error.get('message') or resp.text[:200]}",
        )

    log.info("Instagram ice breakers cleared (%s).", identity)
    return {"status": "cleared", "identity": identity, "meta_response": result}


@app.get("/admin/instagram/profile")
def get_instagram_messaging_profile(
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Reads the Ice Breakers and persistent menu configured on the Instagram
    account, using the credentials this service already holds.

    Read-only on purpose. Ice Breakers are the tappable questions Instagram
    shows a customer before they type anything, so changing them changes what
    every customer is invited to ask — that is a decision to take deliberately,
    with Phase 1's evidence in hand, not a side effect of a diagnostic.

    Exists because the token lives only in the Railway dashboard: this is the
    one place that can ask Meta the question without anyone copying a secret
    around.
    """
    require_admin(x_admin_key, x_admin_pin)

    token = (config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    if not token:
        return {"configured": False, "reason": "INSTAGRAM_ACCESS_TOKEN is not set on this service."}

    # Same split the outbound sender uses, so this reports on the identity the
    # bot actually talks to Meta as.
    url, identity = _instagram_profile_url_and_identity()

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                url,
                params={"fields": "ice_breakers,persistent_menu", "access_token": token},
            )
        body = resp.json()
    except Exception as e:
        log.warning("Could not read the Instagram messaging profile: %s", e)
        return {"configured": True, "identity": identity, "ok": False, "error": f"{type(e).__name__}: {e}"}

    if resp.status_code != 200:
        error = body.get("error", {}) if isinstance(body, dict) else {}
        return {
            "configured": True,
            "identity": identity,
            "ok": False,
            "http_status": resp.status_code,
            "meta_error": {
                "message": error.get("message"),
                "code": error.get("code"),
                "type": error.get("type"),
            },
        }

    entries = body.get("data") or []
    profile = entries[0] if entries else {}
    ice_breakers = profile.get("ice_breakers") or []
    questions = [
        q.get("question")
        for block in ice_breakers
        for q in (block.get("call_to_actions") or [])
        if isinstance(q, dict)
    ]

    return {
        "configured": True,
        "identity": identity,
        "ok": True,
        "ice_breakers_set": bool(questions),
        "ice_breaker_questions": questions,
        "persistent_menu_set": bool(profile.get("persistent_menu")),
        "raw": profile,
    }

@app.get("/admin/conversations")
def get_conversations_list(
    limit: int = 50,
    offset: int = 0,
    channel: Optional[str] = None,
    user_id: Optional[str] = None,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Returns paginated customer conversation logs."""
    require_admin(x_admin_key, x_admin_pin)
    limit = max(1, min(limit, 200))
    convs = db.get_conversations(limit=limit, offset=offset, channel=channel, user_id=user_id)
    total = db.get_conversations_count(channel=channel, user_id=user_id)
    return {"count": len(convs), "total": total, "conversations": convs}

@app.get("/admin/sessions")
def get_sessions_list(
    limit: int = 50,
    offset: int = 0,
    channel: Optional[str] = None,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Returns stored user sessions."""
    require_admin(x_admin_key, x_admin_pin)
    limit = max(1, min(limit, 200))
    sessions = db.get_sessions(limit=limit, offset=offset, channel=channel)
    total = db.get_sessions_count(channel=channel)
    return {"count": len(sessions), "total": total, "sessions": sessions}

@app.get("/admin/learned-knowledge")
def get_learned_knowledge_list(
    limit: int = 100,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Returns active learned knowledge items from escalation self-healing."""
    require_admin(x_admin_key, x_admin_pin)
    items = db.get_learned_knowledge(limit=limit)
    return {"count": len(items), "learned_knowledge": items}

@app.post("/admin/learned-knowledge")
async def add_learned_knowledge_entry(
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Adds a new verified knowledge item to the self-healing knowledge base."""
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    topic = body.get("topic", "").strip()
    pattern = body.get("question_pattern", "").strip()
    answer = body.get("verified_answer", "").strip()
    source = body.get("source", "staff_resolution")
    confidence = float(body.get("confidence", 1.0))

    if not topic or not pattern or not answer:
        raise HTTPException(status_code=400, detail="topic, question_pattern, and verified_answer are required")

    entry_id = db.add_learned_knowledge(topic, pattern, answer, source, confidence)
    return {"status": "success", "id": entry_id, "message": "Learned knowledge saved and active."}

@app.delete("/admin/learned-knowledge/{knowledge_id}")
def delete_learned_knowledge_entry(
    knowledge_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Deletes a learned knowledge entry."""
    require_admin(x_admin_key, x_admin_pin)
    deleted = db.delete_learned_knowledge(knowledge_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")
    return {"status": "deleted", "id": knowledge_id}

@app.post("/admin/escalations/{escalation_id}/resolve")
async def resolve_escalation(
    escalation_id: int,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Resolves an escalation and optionally stores a verified answer to prevent future escalations."""
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        body = {}

    status = body.get("status", "resolved")
    verified_answer = body.get("verified_answer", "").strip()
    topic = body.get("topic", "").strip()
    question_pattern = body.get("question_pattern", "").strip()

    knowledge_id = None
    if verified_answer:
        esc = db.get_escalation_by_id(escalation_id)
        msg = esc.get("user_message", "") if esc else ""
        if not topic:
            topic = "General Inquiry" if not msg else msg[:40]
        if not question_pattern:
            question_pattern = msg or topic
        knowledge_id = db.add_learned_knowledge(topic, question_pattern, verified_answer, source="staff_resolution")

    updated = db.update_escalation_status(escalation_id, status)
    if not updated:
        raise HTTPException(status_code=404, detail="Escalation not found")

    return {
        "status": "resolved",
        "escalation_id": escalation_id,
        "learned_knowledge_id": knowledge_id,
        "message": "Escalation resolved successfully." + (" Knowledge base updated." if knowledge_id else "")
    }

@app.get("/admin/templates")
def get_templates(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """Returns active Q&A templates from qa_templates.json."""
    require_admin(x_admin_key, x_admin_pin)
    try:
        with open(config.QA_TEMPLATES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"status": "success", "templates": data.get("templates", []), "matching_rules": data.get("matching_rules", {})}
    except Exception as e:
        log.error("Failed to read qa_templates.json: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/templates")
async def update_templates(request: Request, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """Updates Q&A templates in qa_templates.json and hot-reloads the responder."""
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    try:
        with open(config.QA_TEMPLATES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        if "templates" in body and isinstance(body["templates"], list):
            data["templates"] = body["templates"]
        elif "template" in body and isinstance(body["template"], dict):
            new_t = body["template"]
            t_id = new_t.get("id")
            existing = False
            for i, t in enumerate(data.get("templates", [])):
                if t.get("id") == t_id:
                    data["templates"][i] = new_t
                    existing = True
                    break
            if not existing:
                data.setdefault("templates", []).append(new_t)
        else:
            raise HTTPException(status_code=400, detail="Must provide 'templates' list or 'template' object")

        with open(config.QA_TEMPLATES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        responder.reload_data()
        return {"status": "success", "message": "Q&A templates updated and responder hot-reloaded successfully."}
    except HTTPException:
        raise
    except Exception as e:
        log.error("Failed to update qa_templates.json: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin/candidates")
def get_candidate_templates(
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Returns RAG/AI candidate answers awaiting human approval."""
    require_admin(x_admin_key, x_admin_pin)
    candidates = db.get_pending_candidates()
    return {"status": "success", "count": len(candidates), "candidates": candidates}


@app.get("/admin/assisted-resolutions")
def get_assisted_resolutions(
    status: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 100,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Returns the human-review queue for LLM Assisted Resolution."""
    require_admin(x_admin_key, x_admin_pin)
    if status:
        status = status.strip().upper()
    if category:
        category = category.strip().upper()
    return {
        "status": "success",
        "count": len(db.list_assisted_suggestions(status=status, category=category, limit=limit)),
        "suggestions": db.list_assisted_suggestions(status=status, category=category, limit=limit),
        "metrics": db.assisted_metrics(),
    }


@app.get("/admin/assisted-resolutions/metrics")
def get_assisted_resolution_metrics(
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    return {"status": "success", "metrics": db.assisted_metrics()}


def _assisted_reviewer(x_admin_key: Optional[str], x_admin_pin: Optional[str]) -> str:
    return (x_admin_key or x_admin_pin or "admin")[:120]


def _send_assisted_answer(suggestion: Dict[str, Any], answer: str) -> None:
    if suggestion.get("channel") != "instagram":
        raise HTTPException(status_code=400, detail="Assisted replies are Instagram-only.")
    if not answer.strip():
        raise HTTPException(status_code=400, detail="A final answer is required.")
    send_instagram_reply(str(suggestion["user_id"]), answer.strip())


@app.post("/admin/assisted-resolutions/{suggestion_id}/approve")
async def approve_assisted_resolution(
    suggestion_id: int,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    suggestion = db.get_assisted_suggestion(suggestion_id)
    if not suggestion or suggestion.get("status") != "PENDING_REVIEW":
        raise HTTPException(status_code=404, detail="Pending assisted suggestion not found.")
    if suggestion.get("triage_category") in {"HUMAN_REQUIRED", "CONTACT_DETAIL", "TEACH_REQUIRED"}:
        raise HTTPException(status_code=400, detail="This suggestion requires the matching human action before it can be resolved.")
    try:
        body = await request.json()
    except Exception:
        body = {}
    final_answer = str(body.get("final_answer") or suggestion.get("suggested_answer") or "").strip()
    _send_assisted_answer(suggestion, final_answer)
    edited = final_answer != str(suggestion.get("original_suggested_answer") or "").strip()
    status = "EDITED_AND_APPROVED" if edited else "APPROVED"
    if not db.review_assisted_suggestion(
        suggestion_id, status=status, reviewed_by=_assisted_reviewer(x_admin_key, x_admin_pin),
        final_answer=final_answer, resolution_action="EDITED_AND_APPROVED" if edited else "APPROVED",
    ):
        raise HTTPException(status_code=409, detail="Suggestion was already reviewed.")
    knowledge_id = str(suggestion.get("matched_knowledge_id") or "")
    if knowledge_id.startswith("learned_knowledge#"):
        try:
            db.increment_learned_knowledge_usage(int(knowledge_id.rsplit("#", 1)[1]))
        except (TypeError, ValueError):
            pass
    if suggestion.get("escalation_id"):
        db.update_escalation_status(int(suggestion["escalation_id"]), "resolved")
    return {"status": status, "suggestion_id": suggestion_id, "final_answer": final_answer}


@app.post("/admin/assisted-resolutions/{suggestion_id}/edit-resolve")
async def edit_assisted_resolution(
    suggestion_id: int,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not str(body.get("final_answer") or "").strip():
        raise HTTPException(status_code=400, detail="final_answer is required for edit-and-resolve.")
    # Edit-and-resolve is the same authenticated send gate as approve.
    return await approve_assisted_resolution(suggestion_id, request, x_admin_key, x_admin_pin)


@app.post("/admin/assisted-resolutions/{suggestion_id}/reject")
async def reject_assisted_resolution(
    suggestion_id: int,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = str(body.get("reason") or "Other").strip()[:500]
    if not db.review_assisted_suggestion(
        suggestion_id, status="REJECTED", reviewed_by=_assisted_reviewer(x_admin_key, x_admin_pin),
        resolution_action="REJECTED", rejection_reason=reason,
    ):
        raise HTTPException(status_code=404, detail="Pending assisted suggestion not found.")
    return {"status": "REJECTED", "suggestion_id": suggestion_id}


@app.post("/admin/assisted-resolutions/{suggestion_id}/send-to-human")
def send_assisted_to_human(
    suggestion_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    suggestion = db.get_assisted_suggestion(suggestion_id)
    if not suggestion or suggestion.get("status") != "PENDING_REVIEW":
        raise HTTPException(status_code=404, detail="Pending assisted suggestion not found.")
    escalator.handle_escalation(
        user_id=str(suggestion["user_id"]), channel="instagram",
        user_message=str(suggestion["customer_message"]),
        reason="Assisted Resolution sent to human",
        context_data={"suggestion_id": suggestion_id},
    )
    db.review_assisted_suggestion(
        suggestion_id, status="SENT_TO_HUMAN", reviewed_by=_assisted_reviewer(x_admin_key, x_admin_pin),
        resolution_action="SENT_TO_HUMAN",
    )
    return {"status": "SENT_TO_HUMAN", "suggestion_id": suggestion_id}


@app.post("/admin/assisted-resolutions/{suggestion_id}/teach-and-resolve")
async def teach_and_resolve_assisted(
    suggestion_id: int,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    suggestion = db.get_assisted_suggestion(suggestion_id)
    if not suggestion or suggestion.get("status") != "PENDING_REVIEW":
        raise HTTPException(status_code=404, detail="Pending assisted suggestion not found.")
    try:
        body = await request.json()
    except Exception:
        body = {}
    answer = str(body.get("verified_answer") or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="verified_answer is required.")
    topic = str(body.get("topic") or suggestion["customer_message"][:80]).strip()
    pattern = str(body.get("question_pattern") or suggestion["customer_message"]).strip()
    knowledge_id = db.add_learned_knowledge(topic, pattern, answer, source="assisted_resolution")
    _send_assisted_answer(suggestion, answer)
    db.review_assisted_suggestion(
        suggestion_id, status="RESOLVED", reviewed_by=_assisted_reviewer(x_admin_key, x_admin_pin),
        final_answer=answer, resolution_action="TEACH_AND_RESOLVE",
    )
    if suggestion.get("escalation_id"):
        db.update_escalation_status(int(suggestion["escalation_id"]), "resolved")
    return {"status": "RESOLVED", "suggestion_id": suggestion_id, "learned_knowledge_id": knowledge_id}


@app.post("/admin/assisted-resolutions/{suggestion_id}/dismiss")
def dismiss_assisted_resolution(
    suggestion_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    if not db.review_assisted_suggestion(
        suggestion_id, status="REJECTED", reviewed_by=_assisted_reviewer(x_admin_key, x_admin_pin),
        resolution_action="DISMISSED", rejection_reason="Non-actionable",
    ):
        raise HTTPException(status_code=404, detail="Pending assisted suggestion not found.")
    return {"status": "DISMISSED", "suggestion_id": suggestion_id}

@app.get("/admin/knowledge-gaps")
def get_knowledge_gaps(
    status: str = "OPEN",
    limit: int = 100,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Lists deduplicated missing facts/actions, separate from answer drafts."""
    require_admin(x_admin_key, x_admin_pin)
    normalized = status.strip().upper()
    if normalized not in {"OPEN", "RESOLVED", "DISMISSED"}:
        raise HTTPException(status_code=400, detail="status must be OPEN, RESOLVED, or DISMISSED")
    gaps = db.get_knowledge_gaps(status=normalized, limit=max(1, min(limit, 200)))
    return {"status": "success", "count": len(gaps), "knowledge_gaps": gaps}

@app.post("/admin/knowledge-gaps/{gap_id}/status")
async def set_knowledge_gap_status(
    gap_id: int,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Marks review state; publishing knowledge still uses the approval workflow."""
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        body = {}
    status = str(body.get("status", "")).strip().upper()
    if status not in {"OPEN", "RESOLVED", "DISMISSED"}:
        raise HTTPException(status_code=400, detail="status must be OPEN, RESOLVED, or DISMISSED")
    if not db.update_knowledge_gap_status(gap_id, status):
        raise HTTPException(status_code=404, detail="Knowledge gap not found")
    return {"status": status, "knowledge_gap_id": gap_id}

@app.post("/admin/candidates/{candidate_id}/approve")
async def approve_candidate_template(
    candidate_id: int,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Approves a candidate and activates it in the reviewed knowledge base."""
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        body = {}

    candidate = db.get_candidate_template(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate template not found")
    if candidate.get("status") != "PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail="Candidate template is no longer pending")

    approved_by = body.get("approved_by", "admin")
    if not db.approve_candidate_template(candidate_id, approved_by=approved_by):
        raise HTTPException(status_code=409, detail="Candidate template could not be approved")

    knowledge_id = db.add_learned_knowledge(
        topic=candidate.get("name") or "Approved candidate",
        question_pattern=candidate.get("user_message") or candidate.get("name") or "",
        verified_answer=candidate["response"],
        source=f"approved_candidate#{candidate_id}",
        confidence=max(float(candidate.get("confidence") or 0.0), 0.90),
    )
    return {
        "status": "approved",
        "candidate_id": candidate_id,
        "learned_knowledge_id": knowledge_id,
        "message": "Candidate approved and added to the reviewed knowledge base.",
    }

@app.post("/admin/candidates/{candidate_id}/reject")
def reject_candidate_template(
    candidate_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Rejects a proposed answer without activating it."""
    require_admin(x_admin_key, x_admin_pin)
    if not db.get_candidate_template(candidate_id):
        raise HTTPException(status_code=404, detail="Candidate template not found")
    if not db.reject_candidate_template(candidate_id):
        raise HTTPException(status_code=409, detail="Candidate template could not be rejected")
    return {"status": "rejected", "candidate_id": candidate_id}

@app.post("/admin/sync-menu")
@limiter.limit("20/minute")
def sync_menu(request: Request, csv_url: Optional[str] = None, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """Triggers live menu reload from Google Sheet CSV or local files."""
    require_admin(x_admin_key, x_admin_pin)
    if csv_url:
        result = sheet_sync.sync_from_csv_url(csv_url)
    elif config.GOOGLE_SHEET_CSV_URL:
        # Without this the configured sheet was ignored unless the caller
        # happened to pass the URL again by hand: pressing Sync in the dashboard
        # only re-read the same menu.json from disk and reported success, so a
        # price changed in the sheet never reached a customer.
        result = sheet_sync.sync_from_csv_url()
    else:
        result = sheet_sync.sync_local_reload()
    return result

@app.post("/admin/update-menu")
async def update_menu(request: Request, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """Dynamically updates the latest weekly menu rotation block in menu.json and hot-reloads the responder."""
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Validated out here on purpose: the try below turns every exception into a
    # 500, which would bury the 400 that names the row the client must fix.
    veg_items = (
        _reviewed_menu_items(body["vegetarian"], "vegetarian")
        if isinstance(body.get("vegetarian"), list) else None
    )
    nonveg_items = (
        _reviewed_menu_items(body["non_vegetarian"], "non_vegetarian")
        if isinstance(body.get("non_vegetarian"), list) else None
    )
    bento_items = (
        _reviewed_menu_items(body["bengali_bento"], "bengali_bento")
        if isinstance(body.get("bengali_bento"), list) else None
    )

    menu_path = config.MENU_JSON_PATH
    try:
        with open(menu_path, "r", encoding="utf-8") as f:
            menu_data = json.load(f)

        if "weekly_menus" in menu_data and menu_data["weekly_menus"]:
            current_week = menu_data["weekly_menus"][-1]
            # Publishing a reviewed menu makes it customer-visible again.
            current_week["is_published"] = True
            if "week" in body:
                current_week["week"] = body["week"]
            if veg_items is not None:
                current_week["vegetarian"] = veg_items
            if nonveg_items is not None:
                current_week["non_vegetarian"] = nonveg_items
            if bento_items is not None:
                current_week["bengali_bento"] = bento_items

            with open(menu_path, "w", encoding="utf-8") as f:
                json.dump(menu_data, f, indent=2, ensure_ascii=False)

            responder.reload_data()
            return {"status": "success", "message": "Weekly menu updated and responder hot-reloaded successfully."}
        else:
            return {"status": "failed", "reason": "No weekly_menus block found in menu.json"}
    except Exception as e:
        log.error("Failed to update menu: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/parse-menu-card")
async def parse_menu_card(request: Request, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """
    Auto-extracts menu items, descriptions, quantities, prices, and categories (veg/non-veg)
    from raw unstructured text pasted by the user (flyer, message, notes) and updates menu.json.
    """
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
        raw_text = body.get("raw_text", "")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not raw_text or not raw_text.strip():
        raise HTTPException(status_code=400, detail="raw_text cannot be empty")

    result = menu_parser.parse_and_update_menu(raw_text)
    return result


@app.post("/admin/menu/preview-text")
async def preview_menu_text(request: Request, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """
    Reads dishes out of pasted text and returns them WITHOUT touching menu.json.

    The dashboard used to parse and publish in one press, so a misread price
    was being quoted to customers before anyone had seen it. This is the read
    half on its own: the client checks the rows, fixes them, and publishes
    through /admin/update-menu when they are right.
    """
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
        raw_text = body.get("raw_text", "")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not raw_text or not raw_text.strip():
        raise HTTPException(status_code=400, detail="raw_text cannot be empty")

    parsed = menu_parser.parse_menu_text(raw_text)
    veg = parsed.get("vegetarian", []) or []
    nonveg = parsed.get("non_vegetarian", []) or []
    if not veg and not nonveg:
        return {
            "status": "failed",
            "reason": "Could not find any dish with a price in that text.",
            "parsed": parsed,
        }
    return {
        "status": "success",
        "parsed": {"vegetarian": veg, "non_vegetarian": nonveg},
        "vegetarian_count": len(veg),
        "non_vegetarian_count": len(nonveg),
    }


def _reviewed_menu_items(raw: Any, field: str) -> List[Dict[str, Any]]:
    """
    Validates dishes coming back from the dashboard's review table.

    The client can now retype every field before publishing, so this is the
    last thing standing between a typo and the prices customers are quoted. A
    bad row is refused by name rather than dropped: silently skipping it would
    report success while the dish never reached the menu.

    description and quantity are only carried through when they have content,
    so a hand-added dish is stored exactly as sparse as it was entered instead
    of gaining empty keys the menu renderer would print as blanks.
    """
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail=f"'{field}' must be a list of dishes")

    cleaned: List[Dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail=f"{field}[{index}] is not a dish")
        name = str(entry.get("item") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail=f"{field}[{index}] has no dish name")
        try:
            price = int(float(entry.get("price_inr")))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail=f"{field}[{index}] ({name}) has no readable price"
            )
        if price <= 0:
            raise HTTPException(
                status_code=400, detail=f"{field}[{index}] ({name}) is priced at {price}"
            )
        item: Dict[str, Any] = {"item": name, "price_inr": price}
        description = str(entry.get("description") or "").strip()
        quantity = str(entry.get("quantity") or "").strip()
        if description:
            item["description"] = description
        if quantity:
            item["quantity"] = quantity
        cleaned.append(item)
    return cleaned


@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/dashboard", response_class=HTMLResponse)
def get_admin_dashboard():
    """
    Dedicated PIN-Protected Kitchen & Menu Management Portal for Camellia Artisanal.
    Features:
    - PIN Authentication (Configured ADMIN_PIN or ADMIN_API_KEY from environment)
    - 📋 Paste Menu Card Text Parser
    - 📸 Drag-and-Drop Image Flyer Upload with Groq Vision OCR
    - 📊 Google Sheets Live Sync
    - 🍱 Active Menu Viewer & Baseline Reset
    - 📈 Bot Performance & Escalations
    """
    # Raw on purpose. This block is 1300 lines of HTML and JavaScript, and in a
    # normal Python string every backslash in it is eaten before the browser
    # sees it: a JS "\n" became a real newline and "\'" became a bare quote,
    # each of which terminated a JavaScript string literal early. One such line
    # was enough to make the whole <script> fail to parse, so every function in
    # the dashboard — verifyPin included — was never defined and the Unlock
    # button silently did nothing. Raw keeps escapes intact for JavaScript, and
    # nothing here relies on Python expanding them.
    html_content = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Camellia Artisanal — Kitchen & Menu Admin Portal</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --primary: #524036;
            --primary-dark: #1F1C1B;
            --primary-light: #B8B0A8;
            --gold: #B8B0A8;
            --gold-light: #F3F0EC;
            --bg: #E6E2DD;
            --surface: #FFFFFF;
            --text-dark: #1F1C1B;
            --text-muted: #6F6965;
            --border: #D9D3CE;
            --success: #10B981;
            --warning: #F59E0B;
            --danger: #EF4444;
            --shadow: 0 2px 8px rgba(31, 28, 27, 0.06);
            --shadow-lg: 0 8px 24px rgba(31, 28, 27, 0.10);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }
        body { background: var(--bg); color: var(--text-dark); min-height: 100vh; display: flex; flex-direction: column; }
        
        /* PIN Lock Overlay */
        .auth-overlay {
            position: fixed; inset: 0; background: rgba(31,28,27,0.96);
            backdrop-filter: blur(12px); display: flex; justify-content: center; align-items: center; z-index: 1000; padding: 20px;
        }
        .auth-card {
            background: var(--surface); width: 100%; max-width: 420px; border-radius: 24px; padding: 36px 28px;
            box-shadow: 0 25px 50px rgba(0,0,0,0.3); text-align: center; border: 1px solid rgba(212,175,55,0.3);
            animation: fadeIn 0.4s ease-out;
        }
        .auth-icon {
            width: 68px; height: 68px; background: #FDF2F4; color: var(--primary); border-radius: 50%;
            display: flex; align-items: center; justify-content: center; font-size: 28px; margin: 0 auto 16px;
            border: 2px solid var(--primary-light);
        }
        .auth-card h2 { font-family: 'Outfit', sans-serif; font-size: 1.45rem; color: var(--primary); margin-bottom: 6px; }
        .auth-card p { font-size: 0.88rem; color: var(--text-muted); margin-bottom: 24px; line-height: 1.4; }
        .pin-input-wrap { margin-bottom: 20px; }
        .pin-input {
            width: 100%; max-width: 260px; font-size: 2rem; letter-spacing: 12px; text-align: center;
            padding: 12px 16px; border: 2px solid #E2E8F0; border-radius: 16px; outline: none; font-weight: 700;
            color: var(--primary); background: #FAF9F8; transition: all 0.2s ease;
        }
        .pin-input:focus { border-color: var(--primary); background: #FFFFFF; box-shadow: 0 0 0 4px rgba(139,30,63,0.12); }
        .auth-btn {
            width: 100%; max-width: 260px; background: var(--primary); color: white; border: none; padding: 14px 20px;
            border-radius: 14px; font-size: 1rem; font-weight: 600; cursor: pointer; transition: all 0.2s;
            display: inline-flex; align-items: center; justify-content: center; gap: 8px;
        }
        .auth-btn:hover { background: var(--primary-dark); transform: translateY(-1px); box-shadow: 0 6px 16px rgba(139,30,63,0.25); }
        .auth-hint { font-size: 0.76rem; color: #888; margin-top: 16px; line-height: 1.4; }
        .shake { animation: shake 0.4s ease-in-out; }
        
        /* Top Navigation */
        header {
            background: var(--primary-dark); color: white;
            padding: 16px 28px; display: flex; justify-content: space-between; align-items: center;
            box-shadow: 0 4px 16px rgba(101,18,43,0.15);
        }
        .brand-box { display: flex; align-items: center; gap: 14px; }
        .brand-logo { font-size: 1.7rem; }
        .brand-text h1 { font-family: 'Outfit', sans-serif; font-size: 1.25rem; font-weight: 700; letter-spacing: 0.3px; }
        .brand-text p { font-size: 0.78rem; opacity: 0.88; font-weight: 400; }
        .header-actions { display: flex; align-items: center; gap: 10px; }
        .header-btn {
            background: rgba(255,255,255,0.15); border: 1px solid rgba(255,255,255,0.3); color: white;
            padding: 8px 14px; border-radius: 10px; font-size: 0.82rem; font-weight: 600; cursor: pointer;
            text-decoration: none; display: inline-flex; align-items: center; gap: 6px; transition: background 0.2s;
        }
        .header-btn:hover { background: rgba(255,255,255,0.28); }
        
        /* Status Bar */
        .status-bar {
            background: #FFFFFF; border-bottom: 1px solid var(--border); padding: 10px 28px;
            display: flex; justify-content: space-between; align-items: center; font-size: 0.82rem;
            flex-wrap: wrap; gap: 12px;
        }
        .status-items { display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }
        /* Wrap between pills, never inside one — without this the row stayed
           unbroken and "Active Menu Items:" split across three lines instead. */
        .status-pill { display: inline-flex; align-items: center; gap: 6px; font-weight: 500; white-space: nowrap; }
        .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
        .dot.green { background: var(--success); box-shadow: 0 0 8px rgba(16,185,129,0.5); }
        .dot.gold { background: var(--gold); }
        .dot.red { background: var(--danger); }
        /* Remove sits opposite the veg/non-veg badge rather than under the
           price, so a mis-tap lands on the badge and not on a deletion. */
        .menu-card-footer { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-top: 8px; }
        .btn-remove-dish { padding: 5px 12px; font-size: 0.76rem; }
        /* Sized to sit under an 84px thumbnail without wrapping the pair. */
        .btn-archive-action { padding: 3px 7px; font-size: 0.66rem; border-radius: 6px; }
        /* Editable cells in the pre-publish review table. Bordered on purpose:
           a borderless input reads as printed text, and the client has to be
           able to tell at a glance that these values can be corrected. */
        .review-input { width: 100%; min-width: 90px; padding: 7px 9px; border: 1px solid var(--border); border-radius: 8px; font-size: 0.84rem; font-family: inherit; }
        .review-input:focus { outline: none; border-color: var(--primary); }
        .ad-routing-form {
            display: grid; grid-template-columns: minmax(180px, 1fr) minmax(160px, 220px) auto;
            gap: 10px; align-items: end;
        }
        .field-label { display: block; font-size: 0.78rem; font-weight: 700; margin-bottom: 5px; }
        .field-control {
            width: 100%; padding: 11px 12px; border: 1px solid var(--border);
            border-radius: 8px; background: #fff; color: var(--text-dark); font-size: 0.9rem;
        }
        .field-control:focus { outline: 2px solid var(--primary-light); border-color: var(--primary); }
        .ad-route-list { display: grid; gap: 8px; margin-top: 16px; }
        .ad-route-row {
            display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: 12px;
            padding: 10px 12px; background: #F7F5F3; border: 1px solid var(--border); border-radius: 8px;
        }
        .ad-route-id { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }
        .product-chip { padding: 4px 9px; border-radius: 999px; background: var(--gold-light); font-size: 0.76rem; font-weight: 700; }
        @media (max-width: 660px) {
            .ad-routing-form { grid-template-columns: 1fr; }
            .ad-route-row { grid-template-columns: 1fr auto; }
            .ad-route-id { grid-column: 1 / -1; }
        }
        
        /* Main Layout */
        .main-container { max-width: 1100px; width: 100%; margin: 24px auto; padding: 0 20px 40px; flex: 1; }
        
        /* Navigation Tabs */
        .tabs-nav {
            display: flex; gap: 8px; background: #EDE8E3; padding: 6px; border-radius: 16px;
            margin-bottom: 24px; flex-wrap: wrap;
        }
        .tab-btn {
            flex: 1 1 auto; min-width: 0; padding: 12px 18px; border: none; background: transparent; border-radius: 12px;
            font-size: 0.88rem; font-weight: 600; color: #555; cursor: pointer; transition: all 0.2s;
            white-space: nowrap; display: flex; align-items: center; justify-content: center; gap: 8px;
        }
        .tab-btn.active {
            background: var(--surface); color: var(--primary); box-shadow: 0 4px 12px rgba(0,0,0,0.06);
        }
        .tab-btn:hover:not(.active) { background: rgba(255,255,255,0.5); color: var(--primary); }
        
        /* Card Panel */
        .panel-card {
            background: var(--surface); border-radius: 20px; padding: 28px; box-shadow: var(--shadow);
            border: 1px solid var(--border); margin-bottom: 24px; animation: fadeIn 0.3s ease;
        }
        .panel-header { margin-bottom: 20px; }
        .panel-header h2 { font-family: 'Outfit', sans-serif; font-size: 1.25rem; color: var(--primary); margin-bottom: 4px; }
        .panel-header p { font-size: 0.86rem; color: var(--text-muted); }
        
        /* Forms & Inputs */
        textarea.text-input {
            width: 100%; height: 160px; border: 1.5px solid var(--border); border-radius: 14px; padding: 14px;
            font-size: 0.92rem; outline: none; transition: border-color 0.2s; resize: vertical; line-height: 1.5;
            background: #FAFAF9; color: var(--text-dark);
        }
        textarea.text-input:focus { border-color: var(--primary); background: #FFFFFF; }
        
        .action-bar { display: flex; justify-content: space-between; align-items: center; margin-top: 16px; flex-wrap: wrap; gap: 10px; }
        .btn-primary {
            background: var(--primary); color: white; border: none; padding: 12px 24px; border-radius: 12px;
            font-size: 0.92rem; font-weight: 600; cursor: pointer; transition: all 0.2s;
            display: inline-flex; align-items: center; gap: 8px;
        }
        .btn-primary:hover { background: var(--primary-dark); transform: translateY(-1px); }
        .btn-secondary {
            background: #F1EBE6; color: #4A4A4A; border: 1px solid var(--border); padding: 12px 20px;
            border-radius: 12px; font-size: 0.9rem; font-weight: 600; cursor: pointer; transition: all 0.2s;
            display: inline-flex; align-items: center; gap: 6px;
        }
        .btn-secondary:hover { background: #E5DED7; }
        .btn-danger {
            background: #FEE2E2; color: #DC2626; border: 1px solid #FCA5A5; padding: 10px 18px;
            border-radius: 12px; font-size: 0.86rem; font-weight: 600; cursor: pointer; transition: all 0.2s;
        }
        .btn-danger:hover { background: #FCA5A5; color: #991B1B; }
        
        /* Drag & Drop Zone */
        .drop-zone {
            border: 2px dashed #D1C7BD; border-radius: 18px; padding: 36px 20px; text-align: center;
            background: #FAF8F5; cursor: pointer; transition: all 0.2s ease; position: relative;
        }
        .drop-zone:hover, .drop-zone.dragover { border-color: var(--primary); background: #F5EEF0; }
        .drop-zone-icon { font-size: 40px; margin-bottom: 12px; color: var(--primary); }
        .drop-zone h3 { font-size: 1.05rem; font-weight: 600; color: var(--text-dark); margin-bottom: 6px; }
        .drop-zone p { font-size: 0.84rem; color: var(--text-muted); }
        .file-input-hidden { display: none; }
        
        .preview-box {
            margin-top: 16px; display: none; align-items: center; gap: 14px; background: #F5F3EF;
            padding: 12px 16px; border-radius: 12px; border: 1px solid var(--border);
        }
        .preview-img { width: 56px; height: 56px; object-fit: cover; border-radius: 8px; border: 1px solid #CCC; }
        
        /* Result Alert Boxes */
        .result-box {
            margin-top: 20px; padding: 16px; border-radius: 14px; display: none; font-size: 0.88rem;
            line-height: 1.5; border: 1px solid transparent; animation: fadeIn 0.3s;
        }
        .result-box.success { background: #ECFDF5; border-color: #A7F3D0; color: #065F46; }
        .result-box.error { background: #FEF2F2; border-color: #FECACA; color: #991B1B; }
        .result-box.info { background: #EFF6FF; border-color: #BFDBFE; color: #1E40AF; }
        .result-box pre {
            background: rgba(0,0,0,0.04); padding: 10px; border-radius: 8px; margin-top: 8px;
            font-family: monospace; font-size: 0.82rem; overflow-x: auto;
        }
        
        /* Active Menu Cards Grid */
        .menu-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin-top: 16px; }
        .menu-card {
            background: #FCFBF9; border: 1px solid var(--border); border-radius: 14px; padding: 16px;
            transition: all 0.2s;
        }
        .menu-card:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(0,0,0,0.04); }
        .menu-card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 6px; }
        .menu-item-name { font-weight: 700; font-size: 0.96rem; color: var(--text-dark); }
        .menu-item-price { font-weight: 700; color: var(--primary); font-size: 0.98rem; }
        .menu-item-desc { font-size: 0.82rem; color: var(--text-muted); line-height: 1.4; }
        .badge {
            display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 0.72rem; font-weight: 600;
            margin-top: 8px;
        }
        .badge-veg { background: #D1FAE5; color: #065F46; }
        .badge-nonveg { background: #FEE2E2; color: #991B1B; }

        /* Collection browser and builder */
        .collection-layout { display: grid; grid-template-columns: minmax(280px, 0.8fr) minmax(360px, 1.2fr); gap: 20px; }
        .collection-box { background: #FCFBF9; border: 1px solid var(--border); border-radius: 14px; padding: 16px; }
        .collection-box h3 { color: var(--primary); font-size: 1.05rem; margin-bottom: 6px; }
        .collection-select { width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 9px; background: white; font: inherit; }
        .collection-slot { padding: 12px 0; border-bottom: 1px solid var(--border); }
        .collection-slot:last-child { border-bottom: 0; }
        .collection-slot label { display: block; font-size: 0.8rem; font-weight: 700; margin-bottom: 5px; }
        .collection-choice-detail { color: var(--text-muted); font-size: 0.78rem; line-height: 1.4; margin-top: 5px; }
        .catalogue-section { margin-top: 18px; }
        .catalogue-section h3 { color: var(--primary); font-size: 1.05rem; margin-bottom: 8px; }
        .catalogue-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px; }
        .catalogue-item { border: 1px solid var(--border); border-radius: 10px; padding: 11px; background: #fff; font-size: 0.82rem; line-height: 1.45; }
        .catalogue-item b { display: block; color: var(--text-dark); margin-bottom: 3px; }
        .menu-manager-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; }
        .menu-manager-card { border: 1px solid var(--border); border-radius: 14px; overflow: hidden; background: #fff; box-shadow: 0 2px 8px rgba(0,0,0,.04); }
        .menu-manager-card h3 { margin: 0; padding: 14px 14px 5px; color: var(--primary); font-size: 1rem; }
        .menu-manager-image { display: block; width: 100%; height: 220px; object-fit: contain; background: #f6f3ef; cursor: zoom-in; }
        .menu-manager-meta { padding: 0 14px 10px; color: var(--text-muted); font-size: .8rem; }
        .menu-manager-actions { display: flex; flex-wrap: wrap; gap: 7px; padding: 0 14px 14px; }
        .menu-manager-actions button { font-size: .78rem; padding: 7px 9px; }
        .menu-manager-history { padding: 10px 14px; border-top: 1px solid var(--border); background: #fcfbf9; }
        .menu-manager-history img { width: 76px; height: 58px; object-fit: cover; vertical-align: middle; margin-right: 8px; border-radius: 5px; }
        
        /* Analytics Table */
        table.data-table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 0.86rem; }
        table.data-table th { background: #F5F1ED; text-align: left; padding: 10px 14px; font-weight: 600; border-bottom: 1.5px solid var(--border); }
        table.data-table td { padding: 10px 14px; border-bottom: 1px solid var(--border); vertical-align: top; }
        table.data-table tr:hover { background: #FAF8F5; }
        
        /* Instruction Guide */
        .guide-box { background: var(--gold-light); border: 1px solid rgba(212,175,55,0.35); border-radius: 14px; padding: 16px; margin-top: 20px; font-size: 0.85rem; line-height: 1.5; color: #634C0A; }
        .guide-box h4 { font-size: 0.92rem; margin-bottom: 6px; color: #523E07; }
        
        @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
        @keyframes shake {
            0%, 100% { transform: translateX(0); }
            20%, 60% { transform: translateX(-8px); }
            40%, 80% { transform: translateX(8px); }
        }
        @media (max-width: 768px) {
            header { padding: 14px 16px; flex-direction: column; gap: 10px; align-items: stretch; }
            .status-bar { padding: 8px 16px; gap: 6px; font-size: 0.76rem; }
            .status-items { gap: 12px; }
            .main-container { padding: 0 14px 30px; }
            .tabs-nav { justify-content: flex-start; gap: 4px; padding: 4px; }
            .tab-btn { padding: 10px 8px; font-size: 0.8rem; gap: 4px; }
            .panel-card { padding: 20px 16px; }
            .collection-layout { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>

    <!-- Authentication PIN Overlay -->
    <div class="auth-overlay" id="authOverlay">
        <div class="auth-card" id="authCard">
            <div class="auth-icon">🔐</div>
            <h2>Kitchen Admin Portal</h2>
            <p>Protected access for Camellia Artisanal kitchen managers & staff.</p>
            
            <div class="pin-input-wrap">
                <!-- No maxlength: the browser truncates silently, so a PIN or
                     API key longer than the cap was cut down before it was ever
                     sent and came back "Incorrect PIN" while being correct.
                     This box also accepts ADMIN_API_KEY, which is far longer
                     than any PIN. -->
                <input type="password" id="pinInput" class="pin-input" placeholder="•••••" autofocus onkeydown="if(event.key==='Enter') verifyPin()">
            </div>
            
            <button class="auth-btn" onclick="verifyPin()">
                <span>Unlock Portal</span> ➔
            </button>
            
            <p class="auth-hint">
                💡 <b>Kitchen Staff PIN:</b> Enter configured staff PIN or API Key (from server environment) to manage dishes & live bot responses.
            </p>
        </div>
    </div>

    <!-- Main Navigation Header -->
    <header>
        <div class="brand-box">
            <span class="brand-logo">C</span>
            <div class="brand-text">
                <h1>Camellia Artisanal — Kitchen & Menu Admin</h1>
                <p>Bengal-Rooted Artisanal Sweets & Food &middot; Gurgaon, Delhi NCR</p>
            </div>
        </div>
        <div class="header-actions">
            <a href="/chat" target="_blank" class="header-btn">💬 Customer Chat Tester</a>
            <button class="header-btn" onclick="lockPortal()">🔒 Lock Portal</button>
        </div>
    </header>

    <!-- Live Status Bar -->
    <div class="status-bar">
        <div class="status-items">
            <span class="status-pill"><span class="dot gold" id="ocrDot"></span> Menu reader: <b id="ocrStatus">checking…</b></span>
            <span class="status-pill">Dishes live: <b id="activeCountBadge">Loading...</b></span>
        </div>
        <div>
            <span>Escalation Contact: <b>Camellia Kitchen Team</b> (+91 87965 51990)</span>
        </div>
    </div>

    <!-- Main Workspace -->
    <div class="main-container">
        <!-- Tab Navigation Bar -->
        <div class="tabs-nav">
            <button class="tab-btn active" onclick="switchTab('tab-image', this)">🖼️ Menu Manager</button>
            <button class="tab-btn" onclick="switchTab('tab-collections', this)">🎁 Collections</button>
            <button class="tab-btn" onclick="switchTab('tab-stats', this)">💬 Customers</button>
            <button class="tab-btn" onclick="switchTab('tab-candidates', this)">✅ Answers <span id="candidateBadge" style="display:none; background: var(--danger); color: #fff; border-radius: 10px; padding: 1px 7px; font-size: 0.72rem; margin-left: 4px;"></span></button>
        </div>

        <div id="tab-menu-manager" class="panel-card" data-group="menu">
            <div class="panel-header">
                <h2>🖼️ Menu Manager</h2>
                <p>Manage the actual menu images customers receive. Uploading an image never changes approved prices or business facts.</p>
            </div>
            <div id="menuManagerResult" class="result-box"></div>
            <div id="menuManagerGrid" class="menu-manager-grid"><p style="color: var(--text-muted);">Loading menu images…</p></div>
        </div>

        <!-- MENU tab, panel 2 of 4: same job, for when there is no photo -->
        <div id="tab-image" class="panel-card" data-group="menu">
            <div class="panel-header">
                <h2>📸 Upload this week's menu</h2>
                <p>Upload a photo of your printed menu card, packaging flyer, or Instagram story. The image is read and the dishes hot-reload into the live bot.</p>
                <p id="activeFlyerWeek" style="font-size: 0.85rem; color: #555; margin-top: 4px;"></p>
                <div id="flyerArchive" style="margin-top: 10px;"></div>
            </div>

            <div class="drop-zone" id="dropZone" onclick="document.getElementById('fileInput').click()">
                <div class="drop-zone-icon">📷</div>
                <h3>Drag & Drop, Paste, or Click to Add the Full Menu Photo</h3>
                <p>Copy a menu image and press Ctrl+V here, or use JPEG, PNG, WEBP (flyer, WhatsApp image, Instagram banner).</p>
                <input type="file" id="fileInput" class="file-input-hidden" accept="image/jpeg,image/png,image/webp" onchange="handleFileSelected(event)">
            </div>

            <div class="preview-box" id="previewBox">
                <img id="previewImg" class="preview-img" src="" alt="Flyer Preview">
                <div style="flex: 1;">
                    <b id="previewName" style="font-size: 0.9rem;">filename.jpg</b>
                    <p id="previewSize" style="font-size: 0.78rem; color: #666;">0 KB</p>
                </div>
                <button class="btn-secondary" style="padding: 6px 12px; font-size: 0.8rem;" onclick="clearFileSelection()">Remove</button>
            </div>

            <div class="action-bar">
                <button class="btn-primary" id="uploadImageBtn" onclick="submitMenuImage()">🔍 Scan Flyer &amp; Update Menu</button>
                <button class="btn-danger" type="button" onclick="unpublishCurrentMenu()">Hide this week's menu from customers</button>
            </div>

            <p style="font-size: 0.78rem; color: #6C757D; margin-top: 10px; line-height: 1.45;">Use this only when the week is over or the menu is wrong. It hides the flyer and dishes from chatbot customers; upload or publish the next menu to restore them.</p>

            <div id="imageResult" class="result-box"></div>
        </div>

        <div id="tab-ad-routing" class="panel-card" data-group="menu" style="display: none;">
            <div class="panel-header">
                <h2>📣 Ad Routing</h2>
                <p>Choose what the chatbot shows first when somebody arrives from an Instagram ad. You only need the Ad ID from Meta.</p>
            </div>

            <div class="ad-routing-form">
                <div>
                    <label class="field-label" for="adRouteId">Instagram Ad ID</label>
                    <input class="field-control" id="adRouteId" inputmode="text" autocomplete="off" placeholder="Paste the Ad ID">
                </div>
                <div>
                    <label class="field-label" for="adRouteProduct">Show this product first</label>
                    <select class="field-control" id="adRouteProduct">
                        <option value="sondesh">Sondesh</option>
                        <option value="savoury">Savoury</option>
                        <option value="bento">Bento</option>
                    </select>
                </div>
                <button class="btn-primary" type="button" onclick="saveAdRoute()">Add ad</button>
            </div>

            <div id="adRouteResult" class="result-box"></div>
            <div id="adRouteList" class="ad-route-list">
                <p style="color: var(--text-muted); font-size: 0.84rem;">Loading saved ads...</p>
            </div>
        </div>

        <!-- MENU tab, panel 3 of 4: same job again, from a shared sheet -->
        <div id="tab-paste" class="panel-card" data-group="menu" style="display: none;">
            <div class="panel-header">
                <h2>📋 No photo? Paste the menu as text</h2>
                <p>Paste raw unstructured flyer text, WhatsApp broadcast message, or dish lists. The AI extractor categorizes veg/non-veg, captures prices, and updates the bot instantly.</p>
            </div>
            
            <textarea id="rawTextPaste" class="text-input" placeholder="Example:
THIS WEEK'S BENGAL SPECIALS
- Rose Sondesh (250g) - ₹450
- Jolbhora Nolen Gur (500g) - ₹1400
- Kosha Mangsho (Mutton) (350g) - ₹650
- Chholar Daal with Coconut (250g) - ₹250
- Rui Machher Kalia 2 pcs - ₹450"></textarea>

            <div class="action-bar">
                <button class="btn-primary" onclick="submitMenuText()">✨ Read the dishes</button>
                <button class="btn-secondary" onclick="loadSampleText()">📝 Load Sample Text</button>
            </div>

            <div id="pasteResult" class="result-box"></div>

            <!-- Nothing in here has reached a customer. The client corrects what
                 the parser misread, drops rows they do not want, and only
                 Publish writes to the live menu. -->
            <div id="menuReview" style="display: none; margin-top: 18px;">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 10px;">
                    <div>
                        <h3 style="margin: 0; font-size: 1rem; color: var(--primary);">Check this before it goes live</h3>
                        <p style="margin: 2px 0 0; font-size: 0.82rem; color: #555;">Fix any box, delete a row you do not want, then publish. <b>Customers keep seeing the old menu until you press Publish.</b></p>
                    </div>
                    <button class="btn-secondary" style="font-size: 0.82rem;" onclick="addReviewRow()">+ Add a dish</button>
                </div>

                <div style="overflow-x: auto;">
                    <table class="data-table">
                        <thead>
                            <tr><th>Dish</th><th>Description</th><th>Size</th><th>Price ₹</th><th>Type</th><th></th></tr>
                        </thead>
                        <tbody id="menuReviewBody"></tbody>
                    </table>
                </div>

                <div class="action-bar">
                    <button class="btn-primary" onclick="publishMenuReview()">✅ Publish to the bot</button>
                    <button class="btn-danger" onclick="discardMenuReview()">🗑️ Discard, change nothing</button>
                </div>
            </div>
        </div>

        <!-- MENU tab, panel 1 of 4: the weekly job — upload the flyer -->
        <div id="tab-sheets" class="panel-card" data-group="menu" style="display: none;">
            <div class="panel-header">
                <h2>📊 Or pull it from the Google Sheet</h2>
                <p>Synchronize your weekly menu directly from a shared Google Sheet. Update prices or add weekend specials in Google Sheets and sync in 1 click.</p>
            </div>

            <div style="margin-bottom: 16px;">
                <label style="font-size: 0.88rem; font-weight: 600; display: block; margin-bottom: 6px;">Published Google Sheet CSV URL:</label>
                <input type="url" id="sheetUrlInput" style="width: 100%; padding: 12px 14px; border: 1.5px solid var(--border); border-radius: 12px; font-size: 0.9rem; outline: none;" placeholder="https://docs.google.com/spreadsheets/d/.../pub?output=csv">
            </div>

            <div class="action-bar">
                <button class="btn-primary" onclick="syncGoogleSheet()">🔄 Sync Live from Google Sheet</button>
            </div>

            <div id="sheetResult" class="result-box"></div>

            <div class="guide-box">
                <h4>💡 How to setup Google Sheets Sync:</h4>
                <ol style="padding-left: 20px; line-height: 1.6;">
                    <li>Create columns in your Sheet: <b>Category</b> (Veg/Non-Veg), <b>Item</b>, <b>Description</b>, <b>Quantity</b>, <b>Price_INR</b>.</li>
                    <li>In Google Sheets: Click <b>File ➔ Share ➔ Publish to web</b>.</li>
                    <li>Select <b>Comma-separated values (.csv)</b> and paste the link above.</li>
                </ol>
            </div>
        </div>

        <!-- MENU tab, panel 4 of 4: what the bot is serving right now -->
        <div id="tab-active" class="panel-card" data-group="menu" style="display: none;">
            <div class="panel-header">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <h2>🍱 What the bot is serving right now</h2>
                        <p>Real-time view of all dishes currently served by the bot to customers on Instagram & WhatsApp.</p>
                    </div>
                    <div style="display: flex; gap: 8px;">
                        <button class="btn-secondary" onclick="loadActiveMenu()">🔄 Refresh View</button>
                        <button class="btn-danger" onclick="confirmResetDefaults()">⚠️ Reset to Baseline Bengali Menu</button>
                    </div>
                </div>
            </div>

            <div id="activeMenuContent">
                <p style="color: #666; font-size: 0.9rem;">Loading current menu...</p>
            </div>
        </div>

        <!-- COLLECTIONS tab: browse every available option and tailor a collection -->
        <div id="tab-collections" class="panel-card" data-group="collections" style="display: none;">
            <div class="panel-header">
                <h2>🎁 Review and tailor a collection</h2>
                <p>Start with either Sondesh collection, then replace any selection with another available option of the same kind. The complete menu catalogue is shown below for reference.</p>
            </div>

            <div class="collection-layout">
                <div class="collection-box">
                    <h3>Build a new collection</h3>
                    <p style="font-size: 0.82rem; color: var(--text-muted); margin-bottom: 12px;">Choose a starting collection. Every row can then be changed independently.</p>
                    <label class="field-label" for="collectionBase">Start with</label>
                    <select id="collectionBase" class="collection-select" onchange="startCollectionBuild(this.value)"></select>
                    <div id="collectionBuilder"><p style="color: var(--text-muted); font-size: 0.84rem; margin-top: 12px;">Loading collection choices...</p></div>
                    <div class="action-bar">
                        <button class="btn-primary" type="button" onclick="copyCollectionSummary()">Copy collection summary</button>
                    </div>
                    <div id="collectionCopyResult" class="result-box"></div>
                </div>

                <div class="collection-box">
                    <h3>Your selected collection</h3>
                    <div id="collectionSummary"><p style="color: var(--text-muted); font-size: 0.84rem;">Choose a starting collection to review it here.</p></div>
                </div>
            </div>

            <div id="allMenuCatalogue" style="margin-top: 22px;">
                <p style="color: var(--text-muted); font-size: 0.84rem;">Loading all available menus...</p>
            </div>
        </div>

        <!-- CUSTOMERS tab, panel 1 of 2: who is waiting on a person -->
        <div id="tab-stats" class="panel-card" data-group="customers" style="display: none;">
            <div class="panel-header">
                <h2>📈 Customers waiting on a person</h2>
                <p>Monitor automated Q&A resolution rates and view queries routed to kitchen management with 1-click self-healing.</p>
            </div>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px;">
                <div style="background: #F8FAFC; border: 1px solid var(--border); padding: 18px; border-radius: 14px; text-align: center;">
                    <span style="font-size: 0.8rem; color: #64748B; font-weight: 600;">TOTAL INQUIRIES</span>
                    <h3 id="statTotal" style="font-size: 1.8rem; color: var(--text-dark); margin-top: 4px;">0</h3>
                </div>
                <div style="background: #ECFDF5; border: 1px solid #A7F3D0; padding: 18px; border-radius: 14px; text-align: center;">
                    <span style="font-size: 0.8rem; color: #065F46; font-weight: 600;">AUTOMATED MATCHES</span>
                    <h3 id="statMatched" style="font-size: 1.8rem; color: var(--success); margin-top: 4px;">0</h3>
                </div>
                <div style="background: #FEF2F2; border: 1px solid #FECACA; padding: 18px; border-radius: 14px; text-align: center;">
                    <span style="font-size: 0.8rem; color: #991B1B; font-weight: 600;">HUMAN ESCALATIONS</span>
                    <h3 id="statEscalated" style="font-size: 1.8rem; color: var(--danger); margin-top: 4px;">0</h3>
                </div>
            </div>

            <h3 style="font-size: 1.05rem; color: var(--primary); margin-bottom: 10px;">Recent Escalations (Needs Kitchen Follow-up)</h3>
            <div style="overflow-x: auto;">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Channel</th>
                            <th>Customer / Sender</th>
                            <th>Customer Message</th>
                            <th>Status</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody id="escalationsTableBody">
                        <tr><td colspan="6" style="text-align: center; color: #888;">Loading escalations...</td></tr>
                    </tbody>
                </table>
            </div>

            <div class="panel-card" id="assistedResolutionSection" data-group="answers" style="margin-top: 18px; border: 1px solid var(--border);">
                <div class="panel-header">
                    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;">
                        <div>
                            <h2>🤝 AI Suggested — human review required</h2>
                            <p>Instagram-only drafts grounded in approved knowledge. They are never sent automatically. WhatsApp is always HUMAN ONLY.</p>
                        </div>
                        <button class="btn-secondary" onclick="loadAssistedResolutions()">🔄 Refresh AI suggestions</button>
                    </div>
                    <div id="assistedResolutionMetrics" style="font-size:0.82rem;color:#666;margin-top:8px;">Loading review metrics...</div>
                </div>
                <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap;">
                    <label for="assistedCategoryFilter" style="font-weight:600;">Category:</label>
                    <select id="assistedCategoryFilter" onchange="loadAssistedResolutions()">
                        <option value="">All</option>
                        <option value="AUTO_RESOLVE">AUTO_RESOLVE (candidate only)</option>
                        <option value="AI_SUGGESTED">AI_SUGGESTED</option>
                        <option value="TEACH_REQUIRED">TEACH_REQUIRED</option>
                        <option value="HUMAN_REQUIRED">HUMAN_REQUIRED</option>
                        <option value="NON_ACTIONABLE">NON_ACTIONABLE</option>
                        <option value="CONTACT_DETAIL">CONTACT_DETAIL</option>
                        <option value="UNSUPPORTED">UNSUPPORTED</option>
                    </select>
                </div>
                <div style="overflow-x:auto;">
                    <table class="data-table">
                        <thead><tr><th>Customer message</th><th>Suggested answer</th><th>Grounding</th><th>Matched Knowledge</th><th>Risk Flags</th><th>Action</th></tr></thead>
                        <tbody id="assistedResolutionsTableBody"><tr><td colspan="6" style="text-align:center;color:#888;">Loading AI suggestions...</td></tr></tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- CUSTOMERS tab, panel 2 of 2: everything being asked -->
        <div id="tab-conversations" class="panel-card" data-group="customers" style="display: none;">
            <div class="panel-header">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <h2>💬 What customers are asking</h2>
                        <p>Audit recent customer interactions across Instagram Ads and WhatsApp.</p>
                    </div>
                    <div style="display: flex; gap: 8px; align-items: center;">
                        <select id="convChannelFilter" style="padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border);" onchange="loadConversations()">
                            <option value="">All Channels</option>
                            <option value="instagram">Instagram</option>
                            <option value="whatsapp">WhatsApp</option>
                        </select>
                        <button class="btn-secondary" onclick="loadConversations()">🔄 Refresh</button>
                    </div>
                </div>
            </div>
            <div style="overflow-x: auto;">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Channel</th>
                            <th>User ID</th>
                            <th>Customer Message</th>
                            <th>Bot Response</th>
                            <th>Match Type</th>
                        </tr>
                    </thead>
                    <tbody id="conversationsTableBody">
                        <tr><td colspan="6" style="text-align: center; color: #888;">Loading conversations...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- ANSWERS tab, panel 2 of 2: write one by hand -->
        <div class="panel-card" id="tab-candidates" data-group="answers" style="display: none;">
            <div class="panel-header">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <h2>✅ Waiting for your approval</h2>
                        <p>When a customer asks something the bot has no approved answer for, it drafts one and parks it here. <b>Nothing on this page has been shown to a customer.</b> Approving an answer adds it to the bot's verified knowledge, so the next customer who asks gets it instantly instead of waiting for a human.</p>
                    </div>
                    <button class="btn-secondary" onclick="loadCandidates()">🔄 Refresh</button>
                </div>
            </div>

            <div style="overflow-x: auto;">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Customer Asked</th>
                            <th>Draft Answer — read it before approving</th>
                            <th>Confidence</th>
                            <th>Drafted By</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody id="candidatesTableBody">
                        <tr><td colspan="5" style="text-align: center; color: #888;">Loading suggested answers...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Writing an answer by hand used to be a collapsed form inside the
             panel below, opened by a button the client never pressed: the list
             of existing answers filled the screen and the button went with it.
             Given its own card, above that list and already open, it is the
             first thing the tab offers instead of the last. -->
        <div id="tab-write" class="panel-card" data-group="answers" style="display: none;">
            <div class="panel-header">
                <h2>✍️ Write an answer for the bot</h2>
                <p>Type a question customers ask and the exact reply the bot should send. It goes live at once, so nobody waits for a person to answer it again.</p>
            </div>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px;">
                <div>
                    <label style="font-size: 0.82rem; font-weight: 600; display: block; margin-bottom: 4px;">Topic / Category:</label>
                    <input type="text" id="newTopic" placeholder="e.g. Corporate Gifting, Custom Packaging" style="width: 100%; padding: 10px; border-radius: 8px; border: 1px solid var(--border);">
                </div>
                <div>
                    <label style="font-size: 0.82rem; font-weight: 600; display: block; margin-bottom: 4px;">Words a customer would use:</label>
                    <input type="text" id="newPattern" placeholder="e.g. corporate orders, wedding packaging" style="width: 100%; padding: 10px; border-radius: 8px; border: 1px solid var(--border);">
                </div>
            </div>
            <div style="margin-bottom: 12px;">
                <label style="font-size: 0.82rem; font-weight: 600; display: block; margin-bottom: 4px;">What the bot should reply:</label>
                <textarea id="newAnswer" placeholder="Type official response..." style="width: 100%; height: 80px; padding: 10px; border-radius: 8px; border: 1px solid var(--border);"></textarea>
            </div>
            <div class="action-bar">
                <button class="btn-primary" onclick="submitNewKnowledge()">💾 Save & Activate</button>
                <button class="btn-secondary" onclick="clearKnowledgeForm()">Clear</button>
            </div>
        </div>

        <div id="tab-knowledge" class="panel-card" data-group="answers" style="display: none;">
            <div class="panel-header">
                <h2>🧠 Answers you have written</h2>
                <p>Verified Q&A items taught by kitchen staff or auto-learned from resolved customer queries.</p>
            </div>

            <div style="overflow-x: auto;">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Topic</th>
                            <th>Trigger Keyword / Pattern</th>
                            <th>Verified Bot Answer</th>
                            <th>Source</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody id="knowledgeTableBody">
                        <tr><td colspan="5" style="text-align: center; color: #888;">Loading learned knowledge...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- ANSWERS tab, panel 1 of 2: the review queue -->


    </div>

    <!-- Teach & Resolve Modal -->
    <div id="resolveModal" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 9999; justify-content: center; align-items: center; padding: 20px;">
        <div style="background: white; border-radius: 16px; width: 100%; max-width: 520px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.2);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                <h3 style="margin: 0; color: var(--primary);">✨ Teach Bot & Resolve Escalation</h3>
                <button onclick="closeResolveModal()" style="background: none; border: none; font-size: 1.4rem; cursor: pointer;">&times;</button>
            </div>
            <p style="font-size: 0.85rem; color: #555; margin-bottom: 12px;">Customer Query: <b id="modalUserMsg" style="color: #222;"></b></p>
            <input type="hidden" id="modalEscId">
            <div style="margin-bottom: 12px;">
                <label style="font-size: 0.82rem; font-weight: 600; display: block; margin-bottom: 4px;">Verified Answer (Taught to Bot for Future Inquiries):</label>
                <textarea id="modalAnswer" style="width: 100%; height: 100px; padding: 10px; border-radius: 8px; border: 1.5px solid var(--border); font-size: 0.9rem;" placeholder="Type the verified answer..."></textarea>
            </div>
            <div style="display: flex; gap: 8px; justify-content: flex-end;">
                <button class="btn-secondary" onclick="resolveEscalationOnly()">Mark Resolved Only</button>
                <button class="btn-primary" onclick="submitResolveAndLearn()">Teach & Resolve</button>
            </div>
        </div>
    </div>

    <script>
        // Remembering the PIN between page loads is a convenience, never part
        // of authentication — so no storage failure may stand between a valid
        // PIN and the portal. Every one of these throws when site data is
        // blocked, in private browsing, and inside in-app browsers such as
        // Instagram's. The read below used to be bare and at top-level scope,
        // where a single SecurityError aborted the whole script: verifyPin was
        // then never defined and the Unlock button did nothing at all, with no
        // message to explain it.
        function safeStorageGet(key) {
            try { return sessionStorage.getItem(key) || ''; } catch (e) { return ''; }
        }
        function safeStorageSet(key, value) {
            try { sessionStorage.setItem(key, value); } catch (e) { /* not fatal */ }
        }
        function safeStorageRemove(key) {
            try { sessionStorage.removeItem(key); } catch (e) { /* not fatal */ }
        }

        let currentAdminKey = safeStorageGet('camellia_admin_pin');
        let selectedFile = null;

        // On Load: check if authenticated
        // The reader light used to be hardcoded to Ready. /health knows whether
        // the vision model is actually configured and answering, and it needs no
        // credentials, so the light can just tell the truth. If the check itself
        // fails we say so rather than falling back to a reassuring green.
        async function refreshReaderStatus() {
            const dot = document.getElementById('ocrDot');
            const label = document.getElementById('ocrStatus');
            if (!dot || !label) return;
            try {
                const resp = await fetch('/health');
                const data = await resp.json();
                const ai = data.ai || {};
                if (!ai.enabled) {
                    dot.className = 'dot red';
                    label.innerText = 'not set up';
                    label.title = 'No AI key configured, so an uploaded photo cannot be read into dishes.';
                } else if (ai.working === false) {
                    dot.className = 'dot red';
                    label.innerText = 'having trouble';
                    label.title = 'The last call to the vision model failed. Uploads may not extract dishes.';
                } else {
                    dot.className = 'dot green';
                    label.innerText = 'ready';
                    label.title = '';
                }
            } catch (e) {
                dot.className = 'dot gold';
                label.innerText = 'unknown';
                label.title = 'Could not reach /health to check.';
            }
        }

        window.addEventListener('DOMContentLoaded', () => {
            // The default tab is already marked active in the markup, so its
            // whole group has to be on screen to match — otherwise only the
            // leading panel shows and the rest look like they do not exist.
            showGroup('menu');
            refreshReaderStatus();
            if (currentAdminKey) {
                document.getElementById('authOverlay').style.display = 'none';
                loadMenuManager();
                loadActiveMenu();
                loadAdRoutes();
                loadAnalytics();
                refreshCandidateBadge();
                loadAssistedResolutions();
            } else {
                document.getElementById('authOverlay').style.display = 'flex';
            }
            setupDragAndDrop();
        });

        async function verifyPin() {
            const pin = document.getElementById('pinInput').value.trim();
            if (!pin) return;

            try {
                const resp = await fetch('/admin/verify-pin', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ pin: pin })
                });

                if (resp.ok) {
                    // Unlock first. Persisting the PIN is optional, and doing
                    // it first meant a storage failure threw before the overlay
                    // was hidden — the outer catch then reported a correct PIN
                    // as "Connection error" and the portal stayed locked.
                    currentAdminKey = pin;
                    document.getElementById('authOverlay').style.display = 'none';
                    safeStorageSet('camellia_admin_pin', pin);
                    loadMenuManager();
                    loadActiveMenu();
                    loadAdRoutes();
                    loadAnalytics();
                    refreshCandidateBadge();
                    loadAssistedResolutions();
                } else {
                    const card = document.getElementById('authCard');
                    card.classList.add('shake');
                    setTimeout(() => card.classList.remove('shake'), 400);
                    // Every failure used to read "Incorrect PIN", which sent
                    // people hunting for a wrong password when the real cause
                    // was the rate limit or an unconfigured server.
                    if (resp.status === 429) {
                        alert('Too many attempts. The server allows 10 per minute — wait a minute and try again.');
                    } else if (resp.status === 503) {
                        alert('No ADMIN_PIN or ADMIN_API_KEY is configured on the server, so the portal cannot be opened. Set one in the deployment environment and redeploy.');
                    } else {
                        alert('Incorrect PIN. Please enter your configured admin PIN or admin key.');
                    }
                }
            } catch (err) {
                // Naming the error matters: this branch also catches faults
                // that have nothing to do with the network, and calling all of
                // them "Connection error" hid a browser-side failure behind a
                // server-side explanation.
                alert('Could not verify the PIN: ' + (err && err.message ? err.message : err));
            }
        }

        function lockPortal() {
            safeStorageRemove('camellia_admin_pin');
            currentAdminKey = '';
            document.getElementById('pinInput').value = '';
            document.getElementById('authOverlay').style.display = 'flex';
        }

        function getAuthHeaders() {
            return {
                'X-Admin-Key': currentAdminKey,
                'X-Admin-Pin': currentAdminKey
            };
        }

        function escapeHtml(str) {
            if (!str) return '';
            return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }

        // The old panel-per-tab navigation overflowed a kitchen handset. Panels
        // are grouped by task and a tab shows its whole group stacked. Each tab
        // is named after the panel it leads with, so the panels themselves did
        // not have to move.
        const TAB_GROUPS = {
            'tab-image': 'menu',
            'tab-collections': 'collections',
            'tab-stats': 'customers',
            'tab-candidates': 'answers'
        };

        function showGroup(group) {
            document.querySelectorAll('.panel-card').forEach(panel => {
                panel.style.display = (panel.dataset.group === group) ? 'block' : 'none';
            });
        }

        function switchTab(tabId, el) {
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            if (el) el.classList.add('active');

            const group = TAB_GROUPS[tabId];
            showGroup(group);

            // Everything in the group is on screen at once, so everything in it
            // needs its data.
            if (group === 'menu') { loadActiveMenu(); loadAdRoutes(); }
            if (group === 'menu') { loadMenuManager(); }
            if (group === 'collections') { loadCollectionMenus(); }
            if (group === 'customers') { loadAnalytics(); loadConversations(); }
            if (group === 'answers') { loadCandidates(); loadAssistedResolutions(); loadKnowledge(); }
        }

        function managerRequest(url, options) {
            options = options || {};
            options.headers = Object.assign({}, getAuthHeaders(), options.headers || {});
            return fetch(url, options);
        }

        async function loadMenuManager() {
            const grid = document.getElementById('menuManagerGrid');
            if (!grid) return;
            try {
                const response = await managerRequest('/admin/menu-manager/menus');
                if (!response.ok) throw new Error('Could not load menu images');
                const data = await response.json();
                grid.innerHTML = (data.menus || []).map(menu => {
                    const draft = menu.draft ? '<span style="color:#8a5a00;">Draft ready</span>' : '';
                    const image = menu.image_url ? `<img class="menu-manager-image" src="${escapeHtml(menu.image_url)}" alt="${escapeHtml(menu.title)}" onclick="window.open('${escapeHtml(menu.image_url)}','_blank')">` : '<div class="menu-manager-image" style="display:grid;place-items:center;color:#888;">No published image</div>';
                    return `<article class="menu-manager-card"><h3>${escapeHtml(menu.title)}</h3>${image}<div class="menu-manager-meta"><b>${escapeHtml(menu.status)}</b>${menu.published_at ? ` · Published ${escapeHtml(menu.published_at)}` : ''}${draft ? `<br>${draft}` : ''}</div><div class="menu-manager-actions"><button class="btn-primary" onclick="replaceMenuImage('${escapeHtml(menu.menu_key)}')">Replace Menu</button><button class="btn-secondary" onclick="showMenuHistory('${escapeHtml(menu.menu_key)}')">History</button><button class="btn-danger" onclick="archiveMenuImage('${escapeHtml(menu.menu_key)}')">Archive</button></div><div id="menu-history-${escapeHtml(menu.menu_key)}" class="menu-manager-history" style="display:none"></div></article>`;
                }).join('');
            } catch (error) { grid.innerHTML = `<p style="color:#a33">${escapeHtml(error.message)}</p>`; }
        }

        function replaceMenuImage(menuKey) {
            const input = document.createElement('input'); input.type = 'file'; input.accept = 'image/jpeg,image/png,image/webp';
            input.onchange = async () => {
                const file = input.files && input.files[0]; if (!file) return;
                const form = new FormData(); form.append('file', file);
                const result = document.getElementById('menuManagerResult'); result.innerHTML = 'Uploading draft…';
                const response = await managerRequest(`/admin/menu-manager/menus/${encodeURIComponent(menuKey)}/draft`, {method:'POST', body:form});
                const data = await response.json();
                if (!response.ok) { result.innerHTML = escapeHtml(data.detail || 'Upload failed'); return; }
                const current = await managerRequest(`/admin/menu-manager/menus/${encodeURIComponent(menuKey)}`).then(r => r.json());
                const draftUrl = data.draft.image_path;
                result.innerHTML = `<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start"><div><b>Current Menu</b><br><img src="${escapeHtml(current.current || '')}" style="max-width:220px;max-height:260px"></div><div><b>New Menu</b><br><img src="${escapeHtml(draftUrl)}" style="max-width:220px;max-height:260px"></div></div><div class="action-bar"><button class="btn-secondary" onclick="loadMenuManager();document.getElementById('menuManagerResult').innerHTML=''">Cancel</button><button class="btn-secondary" onclick="previewMenuImage('${escapeHtml(menuKey)}')">Save Draft</button><button class="btn-primary" onclick="publishMenuImage('${escapeHtml(menuKey)}')">Publish New Menu</button></div>`;
            }; input.click();
        }

        async function previewMenuImage(menuKey) { const data = await managerRequest(`/admin/menu-manager/menus/${encodeURIComponent(menuKey)}`).then(r=>r.json()); document.getElementById('menuManagerResult').innerHTML = data.draft ? `Draft saved for ${escapeHtml(menuKey)}. Publish when ready.` : 'No draft image.'; }
        async function publishMenuImage(menuKey) { const response = await managerRequest(`/admin/menu-manager/menus/${encodeURIComponent(menuKey)}/publish`, {method:'POST'}); const data = await response.json(); document.getElementById('menuManagerResult').innerHTML = response.ok ? 'Published. Customers will receive the new image immediately.' : escapeHtml(data.detail || 'Publish failed'); loadMenuManager(); }
        async function archiveMenuImage(menuKey) { if (!confirm('Are you sure you want to remove this menu from customer view?')) return; const response = await managerRequest(`/admin/menu-manager/menus/${encodeURIComponent(menuKey)}/archive`, {method:'POST'}); const data = await response.json(); document.getElementById('menuManagerResult').innerHTML = response.ok ? 'Menu archived. It remains available in History.' : escapeHtml(data.detail || 'Archive failed'); loadMenuManager(); }
        async function showMenuHistory(menuKey) { const box = document.getElementById(`menu-history-${menuKey}`); if (!box) return; const data = await managerRequest(`/admin/menu-manager/menus/${encodeURIComponent(menuKey)}/history`).then(r=>r.json()); box.style.display = 'block'; box.innerHTML = (data.versions || []).length ? data.versions.map(v => `<div style="margin:7px 0"><img src="${escapeHtml(v.image_path)}"><span>Version ${v.version} — ${escapeHtml(v.status)}</span>${v.status !== 'current' ? ` <button class="btn-secondary" onclick="restoreMenuImage('${escapeHtml(menuKey)}',${v.id})">Restore</button>` : ''}</div>`).join('') : '<span>No image history yet.</span>'; }
        async function restoreMenuImage(menuKey, versionId) { const response = await managerRequest(`/admin/menu-manager/menus/${encodeURIComponent(menuKey)}/restore/${versionId}`, {method:'POST'}); const data = await response.json(); document.getElementById('menuManagerResult').innerHTML = response.ok ? 'Previous menu restored.' : escapeHtml(data.detail || 'Restore failed'); loadMenuManager(); showMenuHistory(menuKey); }

        let collectionCatalogue = null;
        let collectionVariants = [];
        let collectionSelections = [];

        function priceLines(prices) {
            return (prices || []).map(price => {
                const size = price.pack_size || '';
                const pieces = price.pieces ? `${price.pieces} pcs` : '';
                const amount = price.price_inr != null ? `₹${price.price_inr}` : '';
                return [size, pieces, amount].filter(Boolean).join(' · ');
            }).join(', ');
        }

        function collectionOptionDetail(product, flavour) {
            const prices = priceLines((flavour && flavour.prices) || product.prices);
            return [prices, (flavour && flavour.note) || product.description || product.pricing_note || '']
                .filter(Boolean).join(' — ');
        }

        function buildCollectionVariants(menu) {
            const variants = [];
            Object.entries(menu.collections || {}).forEach(([collectionKey, collection]) => {
                (collection.products || []).forEach(product => {
                    const type = String(product.item || 'Selection').toLowerCase();
                    const flavours = product.flavours || [];
                    if (flavours.length) {
                        flavours.forEach(flavour => {
                            const flavourName = typeof flavour === 'string' ? flavour : flavour.flavour;
                            variants.push({
                                collectionKey,
                                collectionName: collection.collection_name || collectionKey,
                                type,
                                item: product.item || 'Selection',
                                label: `${product.item}: ${flavourName}`,
                                detail: collectionOptionDetail(product, typeof flavour === 'string' ? null : flavour)
                            });
                        });
                    } else {
                        variants.push({
                            collectionKey,
                            collectionName: collection.collection_name || collectionKey,
                            type,
                            item: product.item || 'Selection',
                            label: product.item || 'Selection',
                            detail: collectionOptionDetail(product, null)
                        });
                    }
                });
            });

            const weeks = menu.weekly_menus || [];
            const current = weeks.length ? weeks[weeks.length - 1] : {};
            (current.norom_pak_sondesh || []).forEach(item => {
                variants.push({
                    collectionKey: 'weekly_norom_pak',
                    collectionName: 'Weekly Norom Pak Sondesh',
                    type: 'sondesh',
                    item: 'Sondesh',
                    label: `Sondesh: ${item.flavour || 'Weekly option'}`,
                    detail: priceLines([item]) || item.pricing_note || item.status || ''
                });
            });
            return variants;
        }

        function startCollectionBuild(collectionKey) {
            if (!collectionCatalogue) return;
            const collection = (collectionCatalogue.collections || {})[collectionKey];
            if (!collection) return;

            collectionSelections = (collection.products || []).map(product => {
                const type = String(product.item || 'Selection').toLowerCase();
                let selected = collectionVariants.findIndex(option =>
                    option.collectionKey === collectionKey && option.type === type
                );
                if (selected < 0) selected = collectionVariants.findIndex(option => option.type === type);
                return { type, item: product.item || 'Selection', selected };
            }).filter(slot => slot.selected >= 0);
            renderCollectionBuilder();
        }

        function renderCollectionBuilder() {
            const builder = document.getElementById('collectionBuilder');
            if (!builder) return;
            if (!collectionSelections.length) {
                builder.innerHTML = '<p style="color: var(--text-muted); font-size: 0.84rem; margin-top: 12px;">No selectable items are available in this collection.</p>';
                renderCollectionSummary();
                return;
            }

            builder.innerHTML = collectionSelections.map((slot, slotIndex) => {
                const options = collectionVariants.map((option, optionIndex) => ({ option, optionIndex }))
                    .filter(entry => entry.option.type === slot.type);
                return `<div class="collection-slot">
                    <label for="collectionSlot${slotIndex}">${escapeHtml(slot.item)}</label>
                    <select id="collectionSlot${slotIndex}" class="collection-select" onchange="changeCollectionSelection(${slotIndex}, this.value)">
                        ${options.map(entry => `<option value="${entry.optionIndex}" ${entry.optionIndex === slot.selected ? 'selected' : ''}>${escapeHtml(entry.option.collectionName)} — ${escapeHtml(entry.option.label)}</option>`).join('')}
                    </select>
                    <div class="collection-choice-detail">${escapeHtml((collectionVariants[slot.selected] || {}).detail || 'No price details listed')}</div>
                </div>`;
            }).join('');
            renderCollectionSummary();
        }

        function changeCollectionSelection(slotIndex, variantIndex) {
            const index = Number(variantIndex);
            if (!collectionSelections[slotIndex] || !collectionVariants[index]) return;
            collectionSelections[slotIndex].selected = index;
            renderCollectionBuilder();
        }

        function collectionSummaryText() {
            const base = document.getElementById('collectionBase');
            const baseName = base && base.options[base.selectedIndex] ? base.options[base.selectedIndex].text : 'Custom collection';
            const rows = collectionSelections.map((slot, index) => {
                const choice = collectionVariants[slot.selected];
                return `${index + 1}. ${choice.label} (${choice.collectionName})${choice.detail ? ` — ${choice.detail}` : ''}`;
            });
            return [`New collection — based on ${baseName}`, ...rows].join('\n');
        }

        function renderCollectionSummary() {
            const summary = document.getElementById('collectionSummary');
            if (!summary) return;
            if (!collectionSelections.length) {
                summary.innerHTML = '<p style="color: var(--text-muted); font-size: 0.84rem;">No selections yet.</p>';
                return;
            }
            summary.innerHTML = collectionSelections.map((slot, index) => {
                const choice = collectionVariants[slot.selected];
                return `<div class="collection-slot"><b>${index + 1}. ${escapeHtml(choice.label)}</b><div class="collection-choice-detail">From ${escapeHtml(choice.collectionName)}${choice.detail ? ` · ${escapeHtml(choice.detail)}` : ''}</div></div>`;
            }).join('');
        }

        async function copyCollectionSummary() {
            const result = document.getElementById('collectionCopyResult');
            if (!collectionSelections.length) return;
            try {
                await navigator.clipboard.writeText(collectionSummaryText());
                result.className = 'result-box success';
                result.innerText = 'Collection summary copied.';
            } catch (e) {
                result.className = 'result-box info';
                result.innerText = collectionSummaryText();
            }
            result.style.display = 'block';
        }

        function catalogueCard(title, detail) {
            return `<div class="catalogue-item"><b>${escapeHtml(title)}</b>${escapeHtml(detail || 'No additional details listed')}</div>`;
        }

        function renderAllMenuCatalogue(menu) {
            const container = document.getElementById('allMenuCatalogue');
            if (!container) return;
            let html = '<h2 style="font-size: 1.2rem; color: var(--primary);">All available menus and items</h2>';

            Object.values(menu.collections || {}).forEach(collection => {
                let cards = '';
                (collection.products || []).forEach(product => {
                    const flavours = product.flavours || [];
                    if (flavours.length) {
                        flavours.forEach(flavour => {
                            const name = typeof flavour === 'string' ? flavour : flavour.flavour;
                            cards += catalogueCard(`${product.item}: ${name}`, collectionOptionDetail(product, typeof flavour === 'string' ? null : flavour));
                        });
                    } else {
                        cards += catalogueCard(product.item, collectionOptionDetail(product, null));
                    }
                });
                html += `<section class="catalogue-section"><h3>${escapeHtml(collection.collection_name || 'Sondesh collection')}</h3><div class="catalogue-list">${cards}</div></section>`;
            });

            const weeks = menu.weekly_menus || [];
            if (weeks.length) {
                const week = weeks[weeks.length - 1];
                const weeklyGroups = [
                    ['Vegetarian weekly menu', week.vegetarian || [], item => [item.item, [item.quantity, item.price_inr != null ? `₹${item.price_inr}` : '', item.description].filter(Boolean).join(' · ')]],
                    ['Non-vegetarian weekly menu', week.non_vegetarian || [], item => [item.item, [item.quantity, item.price_inr != null ? `₹${item.price_inr}` : '', item.description].filter(Boolean).join(' · ')]],
                    ['Bengali bento menu', week.bengali_bento || [], item => [item.name, [item.price_inr != null ? `₹${item.price_inr}` : '', (item.includes || []).join(', ')].filter(Boolean).join(' · ')]],
                    ['Norom Pak Sondesh menu', week.norom_pak_sondesh || [], item => [item.flavour, priceLines([item]) || item.pricing_note || item.status || '']]
                ];
                weeklyGroups.forEach(([heading, items, describe]) => {
                    if (!items.length) return;
                    const cards = items.map(item => {
                        const [title, detail] = describe(item);
                        return catalogueCard(title, detail);
                    }).join('');
                    html += `<section class="catalogue-section"><h3>${escapeHtml(heading)}</h3><div class="catalogue-list">${cards}</div></section>`;
                });
            }
            container.innerHTML = html;
        }

        async function loadCollectionMenus() {
            try {
                const resp = await fetch('/admin/menu/active', { headers: getAuthHeaders() });
                if (!resp.ok) throw new Error('Could not load the menu catalogue.');
                const data = await resp.json();
                collectionCatalogue = data.menu || {};
                collectionVariants = buildCollectionVariants(collectionCatalogue);

                const base = document.getElementById('collectionBase');
                const collections = Object.entries(collectionCatalogue.collections || {});
                base.innerHTML = collections.map(([key, collection]) => `<option value="${escapeHtml(key)}">${escapeHtml(collection.collection_name || key)}</option>`).join('');
                renderAllMenuCatalogue(collectionCatalogue);
                if (collections.length) startCollectionBuild(base.value || collections[0][0]);
            } catch (err) {
                document.getElementById('collectionBuilder').innerHTML = `<p style="color: var(--danger); font-size: 0.84rem;">${escapeHtml(err.message || err)}</p>`;
                document.getElementById('allMenuCatalogue').innerHTML = '<p style="color: var(--danger); font-size: 0.84rem;">The menu catalogue could not be loaded.</p>';
            }
        }

        async function loadAdRoutes() {
            const list = document.getElementById('adRouteList');
            if (!list || !currentAdminKey) return;
            try {
                const resp = await fetch('/admin/ad-routes', { headers: getAuthHeaders() });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not load saved ads.');
                const routes = data.routes || [];
                if (!routes.length) {
                    list.innerHTML = '<p style="color: var(--text-muted); font-size: 0.84rem;">No ads added yet.</p>';
                    return;
                }
                const labels = { sondesh: 'Sondesh', savoury: 'Savoury', bento: 'Bento' };
                list.innerHTML = routes.map(route => `
                    <div class="ad-route-row">
                        <span class="ad-route-id">${escapeHtml(route.ad_id)}</span>
                        <span class="product-chip">${labels[route.product] || escapeHtml(route.product)}</span>
                        <button class="btn-danger" style="padding: 6px 10px; font-size: 0.76rem;" type="button"
                            onclick="removeAdRoute('${escapeHtml(route.ad_id)}')">Remove</button>
                    </div>`).join('');
            } catch (err) {
                list.innerHTML = `<p style="color: var(--danger); font-size: 0.84rem;">${escapeHtml(err.message || err)}</p>`;
            }
        }

        async function saveAdRoute() {
            const adIdInput = document.getElementById('adRouteId');
            const productInput = document.getElementById('adRouteProduct');
            const result = document.getElementById('adRouteResult');
            const adId = adIdInput.value.trim();
            if (!adId) return alert('Paste the Instagram Ad ID first.');
            result.style.display = 'block';
            result.className = 'result-box info';
            result.innerText = 'Saving...';
            try {
                const resp = await fetch('/admin/ad-routes', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
                    body: JSON.stringify({ ad_id: adId, product: productInput.value })
                });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not save this ad.');
                adIdInput.value = '';
                result.className = 'result-box success';
                result.innerText = 'Saved. New conversations from this ad will start with the selected product.';
                loadAdRoutes();
            } catch (err) {
                result.className = 'result-box error';
                result.innerText = err.message || String(err);
            }
        }

        async function removeAdRoute(adId) {
            if (!confirm(`Remove routing for ad ${adId}?`)) return;
            const result = document.getElementById('adRouteResult');
            try {
                const resp = await fetch(`/admin/ad-routes/${encodeURIComponent(adId)}`, {
                    method: 'DELETE', headers: getAuthHeaders()
                });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not remove this ad.');
                result.style.display = 'block';
                result.className = 'result-box success';
                result.innerText = 'Ad routing removed.';
                loadAdRoutes();
            } catch (err) {
                result.style.display = 'block';
                result.className = 'result-box error';
                result.innerText = err.message || String(err);
            }
        }

        // MENU tab: paste-text upload
        // Dishes read out of the pasted text, held in this browser only. Nothing
        // here exists on the server until publishMenuReview() sends it, so a bad
        // read costs the client a Discard rather than a wrong price quoted to a
        // customer.
        let menuReview = [];

        async function submitMenuText() {
            const text = document.getElementById('rawTextPaste').value.trim();
            const resBox = document.getElementById('pasteResult');
            if (!text) return alert('Please enter or paste menu card text.');

            resBox.style.display = 'block';
            resBox.className = 'result-box info';
            resBox.innerHTML = '⏳ <b>Reading the dishes...</b>';

            try {
                const resp = await fetch('/admin/menu/preview-text', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
                    body: JSON.stringify({ raw_text: text })
                });
                const data = await resp.json();

                if (resp.ok && data.status === 'success') {
                    const parsed = data.parsed || {};
                    menuReview = []
                        .concat((parsed.vegetarian || []).map(d => rowFrom(d, 'vegetarian')))
                        .concat((parsed.non_vegetarian || []).map(d => rowFrom(d, 'non_vegetarian')))
                        .concat((parsed.bengali_bento || []).map(d => rowFrom(d, 'bengali_bento')));
                    resBox.className = 'result-box info';
                    resBox.innerHTML = `📖 Read <b>${menuReview.length} dishes</b>. Nothing has changed for customers yet — check the rows below and press Publish.`;
                    renderMenuReview();
                } else {
                    menuReview = [];
                    renderMenuReview();
                    resBox.className = 'result-box error';
                    resBox.innerHTML = `⚠️ <b>Could not read that:</b> ${data.detail || data.reason || 'No dish with a price was found.'}`;
                }
            } catch (err) {
                resBox.className = 'result-box error';
                resBox.innerHTML = `❌ Network error communicating with server.`;
            }
        }

        function rowFrom(dish, category) {
            return {
                item: dish.item || '',
                description: dish.description || '',
                quantity: dish.quantity || '',
                price_inr: dish.price_inr != null ? dish.price_inr : '',
                category: category
            };
        }

        function renderMenuReview() {
            const wrap = document.getElementById('menuReview');
            const body = document.getElementById('menuReviewBody');
            if (!menuReview.length) {
                wrap.style.display = 'none';
                body.innerHTML = '';
                return;
            }
            wrap.style.display = 'block';
            // Values go through escapeHtml because they came off a flyer the
            // kitchen typed or photographed, and land straight in an attribute.
            body.innerHTML = menuReview.map((row, i) => `
                <tr>
                    <td><input class="review-input" value="${escapeHtml(row.item)}" oninput="editReviewRow(${i}, 'item', this.value)"></td>
                    <td><input class="review-input" value="${escapeHtml(row.description)}" oninput="editReviewRow(${i}, 'description', this.value)"></td>
                    <td><input class="review-input" style="min-width: 70px;" value="${escapeHtml(row.quantity)}" oninput="editReviewRow(${i}, 'quantity', this.value)"></td>
                    <td><input class="review-input" style="min-width: 70px;" type="number" min="1" value="${escapeHtml(row.price_inr)}" oninput="editReviewRow(${i}, 'price_inr', this.value)"></td>
                    <td>
                        <select class="review-input" onchange="editReviewRow(${i}, 'category', this.value)">
                            <option value="vegetarian" ${row.category === 'vegetarian' ? 'selected' : ''}>Veg</option>
                            <option value="non_vegetarian" ${row.category === 'non_vegetarian' ? 'selected' : ''}>Non-Veg</option>
                            <option value="bengali_bento" ${row.category === 'bengali_bento' ? 'selected' : ''}>Bento Meal Box</option>
                        </select>
                    </td>
                    <td><button class="btn-danger" style="padding: 5px 10px; font-size: 0.78rem;" onclick="deleteReviewRow(${i})">Delete</button></td>
                </tr>`).join('');
        }

        function editReviewRow(index, field, value) {
            if (menuReview[index]) menuReview[index][field] = value;
        }

        function deleteReviewRow(index) {
            menuReview.splice(index, 1);
            // Re-render rather than removing the <tr>: every remaining row's
            // handlers are bound to its old index and would edit the wrong dish.
            renderMenuReview();
        }

        function addReviewRow() {
            menuReview.push({ item: '', description: '', quantity: '', price_inr: '', category: 'vegetarian' });
            renderMenuReview();
        }

        function discardMenuReview() {
            if (menuReview.length && !confirm('Throw away these dishes? The menu customers see is unchanged either way.')) return;
            menuReview = [];
            renderMenuReview();
            const resBox = document.getElementById('pasteResult');
            resBox.className = 'result-box info';
            resBox.style.display = 'block';
            resBox.innerHTML = '🗑️ Discarded. The live menu was never touched.';
        }

        async function publishMenuReview() {
            const resBox = document.getElementById('pasteResult');
            if (!menuReview.length) return alert('There are no dishes to publish.');

            const blank = menuReview.findIndex(r => !String(r.item).trim() || !String(r.price_inr).trim());
            if (blank !== -1) {
                return alert(`Row ${blank + 1} needs both a dish name and a price before it can be published.`);
            }

            const payload = { vegetarian: [], non_vegetarian: [], bengali_bento: [] };
            menuReview.forEach(row => {
                payload[row.category].push({
                    item: String(row.item).trim(),
                    description: String(row.description).trim(),
                    quantity: String(row.quantity).trim(),
                    price_inr: Number(row.price_inr)
                });
            });

            resBox.style.display = 'block';
            resBox.className = 'result-box info';
            resBox.innerHTML = '⏳ <b>Publishing to the bot...</b>';

            try {
                const resp = await fetch('/admin/update-menu', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
                    body: JSON.stringify(payload)
                });
                const data = await resp.json();
                if (resp.ok && data.status === 'success') {
                    resBox.className = 'result-box success';
                    resBox.innerHTML = `✅ <b>Published.</b> ${payload.vegetarian.length} veg, ${payload.non_vegetarian.length} non-veg, and ${payload.bengali_bento.length} bento items are now what the bot quotes.`;
                    menuReview = [];
                    renderMenuReview();
                    loadActiveMenu();
                } else {
                    resBox.className = 'result-box error';
                    resBox.innerHTML = `⚠️ <b>Not published:</b> ${data.detail || data.reason || 'The server refused the change.'}`;
                }
            } catch (err) {
                resBox.className = 'result-box error';
                resBox.innerHTML = `❌ Network error — nothing was published.`;
            }
        }

        function loadSampleText() {
            document.getElementById('rawTextPaste').value = `Camellia Artisanal Weekly Specials
Veg:
• Chholar Daal (250g) - ₹250
• Dhokar Dalna (250g) - ₹300
• Shukto (250g) - ₹280
• Rose Sondesh (9 pcs/250g) - ₹450

Non-Veg:
• Mangsho (Mutton Kosha) (350g) - ₹650
• Rui Machher Kalia (2 pcs) - ₹450
• Chingri Malaikari (4 pcs) - ₹550
• Chicken Kosha (350g) - ₹420`;
        }

        // MENU tab: flyer photo upload
        function setupDragAndDrop() {
            const dropZone = document.getElementById('dropZone');
            if (!dropZone) return;

            ['dragenter', 'dragover'].forEach(eventName => {
                dropZone.addEventListener(eventName, (e) => { e.preventDefault(); dropZone.classList.add('dragover'); }, false);
            });
            ['dragleave', 'drop'].forEach(eventName => {
                dropZone.addEventListener(eventName, (e) => { e.preventDefault(); dropZone.classList.remove('dragover'); }, false);
            });
            dropZone.addEventListener('drop', (e) => {
                const dt = e.dataTransfer;
                const files = dt.files;
                if (files && files[0]) {
                    processFile(files[0]);
                }
            });

            // Clipboard images do not arrive through the file input. Listen at
            // document level so Ctrl+V works after the operator copies a full
            // menu screenshot, then reuse the same preview/upload path.
            document.addEventListener('paste', (e) => {
                const menuPanel = document.getElementById('tab-image');
                if (!menuPanel || menuPanel.style.display === 'none') return;
                const items = e.clipboardData && e.clipboardData.items;
                if (!items) return;
                for (const item of items) {
                    if (!item.type || !item.type.startsWith('image/')) continue;
                    const pasted = item.getAsFile();
                    if (!pasted) continue;
                    e.preventDefault();
                    const extension = (pasted.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
                    processFile(new File([pasted], `pasted-menu.${extension}`, { type: pasted.type }));
                    const resBox = document.getElementById('imageResult');
                    resBox.style.display = 'block';
                    resBox.className = 'result-box info';
                    resBox.innerHTML = '📋 <b>Menu photo pasted.</b> Review the preview, then click “Scan Flyer & Update Menu” to save it and update the bot.';
                    break;
                }
            });
        }

        function handleFileSelected(e) {
            if (e.target.files && e.target.files[0]) {
                processFile(e.target.files[0]);
            }
        }

        function processFile(file) {
            selectedFile = file;
            document.getElementById('previewName').innerText = file.name;
            document.getElementById('previewSize').innerText = `${Math.round(file.size / 1024)} KB`;
            
            const reader = new FileReader();
            reader.onload = (e) => {
                document.getElementById('previewImg').src = e.target.result;
                document.getElementById('previewBox').style.display = 'flex';
            };
            reader.readAsDataURL(file);
        }

        function clearFileSelection() {
            selectedFile = null;
            document.getElementById('fileInput').value = '';
            document.getElementById('previewBox').style.display = 'none';
        }

        async function submitMenuImage() {
            const resBox = document.getElementById('imageResult');
            if (!selectedFile) return alert('Please drop or select a menu card image file first.');

            resBox.style.display = 'block';
            resBox.className = 'result-box info';
            resBox.innerHTML = '🔍 <b>Scanning flyer image...</b> Please wait.';

            const formData = new FormData();
            formData.append('file', selectedFile);

            try {
                const resp = await fetch('/admin/menu/upload-image', {
                    method: 'POST',
                    headers: getAuthHeaders(),
                    body: formData
                });
                const data = await resp.json();

                if (resp.ok && data.status === 'success') {
                    resBox.className = 'result-box success';
                    resBox.innerHTML = `✅ <b>Vision OCR Extraction Complete!</b><br>${data.message}<pre>${JSON.stringify(data.parsed, null, 2)}</pre>`;
                    loadActiveMenu();
                } else if (data.status === 'warning') {
                    resBox.className = 'result-box info';
                    resBox.innerHTML = `ℹ️ <b>Test Upload Received:</b> ${data.message}`;
                } else if (data.flyer_saved) {
                    // The photo went live even though the text could not be
                    // read. Saying only "OCR failed" here would send the
                    // operator off to re-upload a menu that is already
                    // circulating, so lead with what did work.
                    resBox.className = 'result-box info';
                    resBox.innerHTML = `📸 <b>Menu photo is live.</b><br>${data.flyer_notice || ''}`
                        + `<br><small>Reader detail: ${data.detail || data.error || data.reason || 'could not extract dishes'}</small>`;
                    loadActiveMenu();
                } else {
                    resBox.className = 'result-box error';
                    resBox.innerHTML = `⚠️ <b>Image OCR Failed:</b> ${data.detail || data.error || data.reason || 'Could not extract dishes.'}`;
                }
            } catch (err) {
                resBox.className = 'result-box error';
                resBox.innerHTML = `❌ Error uploading image to server.`;
            }
        }

        // Test Purpose: 1-Click Sample Flyer Test
        // MENU tab: Google Sheet sync
        async function syncGoogleSheet() {
            const url = document.getElementById('sheetUrlInput').value.trim();
            const resBox = document.getElementById('sheetResult');
            resBox.style.display = 'block';
            resBox.className = 'result-box info';
            resBox.innerHTML = '🔄 <b>Fetching Google Sheet CSV and reloading bot memory...</b>';

            try {
                const queryParam = url ? `?csv_url=${encodeURIComponent(url)}` : '';
                const resp = await fetch(`/admin/sync-menu${queryParam}`, {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                const data = await resp.json();

                if (resp.ok && data.status === 'success') {
                    resBox.className = 'result-box success';
                    resBox.innerHTML = `✅ <b>Google Sheet Synced!</b> Updated ${data.updated_items_count} items in menu.json. Bot responder hot-reloaded.`;
                    loadActiveMenu();
                } else {
                    resBox.className = 'result-box error';
                    resBox.innerHTML = `⚠️ <b>Sync Failed:</b> ${data.detail || data.reason || data.error || 'Failed to fetch sheet.'}`;
                }
            } catch (err) {
                resBox.className = 'result-box error';
                resBox.innerHTML = `❌ Error connecting to server.`;
            }
        }

        // MENU tab: what is live now
        async function loadActiveMenu() {
            const container = document.getElementById('activeMenuContent');
            try {
                const resp = await fetch('/admin/menu/active', { headers: getAuthHeaders() });
                if (!resp.ok) throw new Error('Auth error or failed to load');
                const data = await resp.json();
                const menu = data.menu;

                // The flyer photo and the dish text are now two separate
                // things that both have to agree with the last upload, so
                // show staff which week is actually live rather than leaving
                // them to guess from the photo alone.
                const flyerWeekOf = data.weekly_flyer && data.weekly_flyer.week_of;
                const menuWeekOf = (menu.weekly_menus && menu.weekly_menus.length > 0)
                    ? menu.weekly_menus[menu.weekly_menus.length - 1].week_of
                    : null;
                // Superseded flyers, still reachable by a customer asking for
                // "previous menus" until they age out of the two-week window.
                const archiveEl = document.getElementById('flyerArchive');
                if (archiveEl) {
                    const archive = data.flyer_archive || [];
                    if (archive.length === 0) {
                        archiveEl.innerHTML = '';
                    } else {
                        let a = '<p style="font-size: 0.8rem; color: #777; margin-bottom: 6px;">'
                              + '🗂️ Previous menus (kept 2 weeks, customers can ask for these):</p>'
                              + '<div style="display: flex; gap: 10px; flex-wrap: wrap;">';
                        archive.forEach(function (entry) {
                            const label = entry.week_of || 'undated';
                            const safeLabel = escapeHtml(label);
                            const id = escapeHtml(entry.id || '');
                            a += '<div style="text-align: center; font-size: 0.7rem; color: #555;">'
                               + '<a href="' + entry.url + '" target="_blank" rel="noopener">'
                               + '<img src="' + entry.url + '" alt="Menu ' + safeLabel + '" '
                               + 'style="width: 84px; height: 84px; object-fit: cover; '
                               + 'border-radius: 6px; border: 1px solid #ddd;"></a>'
                               + '<div style="margin-top: 3px; max-width: 84px;">' + safeLabel + '</div>'
                               + '<div style="display: flex; gap: 4px; margin-top: 4px; justify-content: center;">'
                               + '<button class="btn-secondary btn-archive-action" '
                               + 'onclick="replaceArchivedFlyer(\'' + id + '\')">Replace</button>'
                               + '<button class="btn-danger btn-archive-action" '
                               + 'onclick="removeArchivedFlyer(\'' + id + '\', \'' + safeLabel + '\')">Remove</button>'
                               + '</div>'
                               + '</div>';
                        });
                        a += '</div>';
                        archiveEl.innerHTML = a;
                    }
                }

                const activeFlyerWeekEl = document.getElementById('activeFlyerWeek');
                if (activeFlyerWeekEl) {
                    const weekLabel = flyerWeekOf || menuWeekOf;
                    // The flyer photo and the dish list are two separate things,
                    // and removing every dish leaves the photo up. The label used
                    // to keep announcing that week as active regardless, which
                    // reads as "the menu is live" when nothing is actually live.
                    const labelWeek = menu.weekly_menus && menu.weekly_menus.length
                        ? menu.weekly_menus[menu.weekly_menus.length - 1]
                        : null;
                    const liveDishCount = labelWeek
                        ? ((labelWeek.vegetarian || []).length + (labelWeek.non_vegetarian || []).length)
                        : 0;
                    if (!weekLabel) {
                        activeFlyerWeekEl.innerText = '';
                    } else if (labelWeek && labelWeek.is_published === false) {
                        activeFlyerWeekEl.innerText = '🚫 Hidden from chatbot customers — upload or publish a replacement to make it live.';
                    } else if (liveDishCount > 0) {
                        activeFlyerWeekEl.innerText = `📅 Active menu: ${weekLabel}`;
                    } else {
                        activeFlyerWeekEl.innerText = `📅 Menu photo still up: ${weekLabel} — but no dishes are live. Upload this week's photo, or use Reset to Baseline to take the photo down too.`;
                    }
                }

                let html = '';
                let totalCount = 0;

                if (menu.weekly_menus && menu.weekly_menus.length > 0) {
                    const currentWeek = menu.weekly_menus[menu.weekly_menus.length - 1];
                    const veg = currentWeek.vegetarian || [];
                    const nonveg = currentWeek.non_vegetarian || [];
                    totalCount = veg.length + nonveg.length;
                    // Kept so Remove can send back the dishes that survive.
                    // update-menu replaces a whole list, so the button needs
                    // the other dishes as much as the one being dropped.
                    liveMenu = { vegetarian: veg, non_vegetarian: nonveg };

                    html += `<h3 style="color: var(--primary); margin-top: 10px; font-size: 1.1rem;">🌱 Vegetarian Weekly Specials (${veg.length})</h3>`;
                    html += '<div class="menu-grid">';
                    veg.forEach((item, idx) => {
                        html += `
                        <div class="menu-card">
                            <div class="menu-card-header">
                                <span class="menu-item-name">${escapeHtml(item.item)}</span>
                                <span class="menu-item-price">₹${item.price_inr}</span>
                            </div>
                            <p class="menu-item-desc">${escapeHtml(item.description || '')} (${escapeHtml(item.quantity || '1 portion')})</p>
                            <div class="menu-card-footer">
                                <span class="badge badge-veg">Vegetarian</span>
                                <button class="btn-danger btn-remove-dish" onclick="removeLiveDish('vegetarian', ${idx})">Remove</button>
                            </div>
                        </div>`;
                    });
                    html += '</div>';

                    html += `<h3 style="color: var(--primary); margin-top: 24px; font-size: 1.1rem;">🍗 Non-Vegetarian Weekly Specials (${nonveg.length})</h3>`;
                    html += '<div class="menu-grid">';
                    nonveg.forEach((item, idx) => {
                        html += `
                        <div class="menu-card">
                            <div class="menu-card-header">
                                <span class="menu-item-name">${escapeHtml(item.item)}</span>
                                <span class="menu-item-price">₹${item.price_inr}</span>
                            </div>
                            <p class="menu-item-desc">${escapeHtml(item.description || '')} (${escapeHtml(item.quantity || '1 portion')})</p>
                            <div class="menu-card-footer">
                                <span class="badge badge-nonveg">Non-Veg</span>
                                <button class="btn-danger btn-remove-dish" onclick="removeLiveDish('non_vegetarian', ${idx})">Remove</button>
                            </div>
                        </div>`;
                    });
                    html += '</div>';
                }

                container.innerHTML = html;
                document.getElementById('activeCountBadge').innerText = `${totalCount} dishes`;
            } catch (err) {
                container.innerHTML = '<p style="color: red;">Failed to load active menu. Please check your PIN.</p>';
            }
        }

        // The dishes the bot is serving right now, as last loaded. Removing one
        // sends the rest back, so this has to be what the server actually holds
        // rather than anything typed in the review table above.
        let liveMenu = { vegetarian: [], non_vegetarian: [] };

        async function removeLiveDish(category, index) {
            const list = liveMenu[category] || [];
            const dish = list[index];
            if (!dish) return;

            const remaining = (liveMenu.vegetarian.length + liveMenu.non_vegetarian.length) - 1;
            let question = `Remove "${dish.item}" from the live menu? Customers will stop being offered it straight away.`;
            if (remaining === 0) {
                // Worth spelling out: an empty menu is not an obvious outcome
                // of pressing Remove on one dish, and the bot has nothing to
                // quote once it happens.
                question = `"${dish.item}" is the last dish on the menu. Remove it and the bot has no weekly dishes left to offer anyone. Continue?`;
            }
            if (!confirm(question)) return;

            const payload = {
                vegetarian: liveMenu.vegetarian.slice(),
                non_vegetarian: liveMenu.non_vegetarian.slice()
            };
            payload[category].splice(index, 1);

            try {
                const resp = await fetch('/admin/update-menu', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
                    body: JSON.stringify(payload)
                });
                const data = await resp.json();
                if (resp.ok && data.status === 'success') {
                    loadActiveMenu();
                } else {
                    alert('Not removed: ' + (data.detail || data.reason || 'the server refused the change.'));
                }
            } catch (e) {
                alert('Connection error - nothing was removed.');
            }
        }

        async function removeArchivedFlyer(flyerId, label) {
            if (!confirm(`Delete the menu for ${label}? Customers who ask for previous menus will stop being shown it. This cannot be undone.`)) return;
            try {
                const resp = await fetch(`/admin/menu/archive/${encodeURIComponent(flyerId)}/remove`, {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                const data = await resp.json();
                if (resp.ok && data.status === 'success') {
                    loadActiveMenu();
                } else {
                    alert('Not removed: ' + (data.detail || 'the server refused the change.'));
                }
            } catch (e) {
                alert('Connection error - nothing was removed.');
            }
        }

        async function unpublishCurrentMenu() {
            const warning = 'Hide this week\'s menu from chatbot customers? The flyer and dishes will stop being shown until you upload or publish a replacement. Your saved kitchen data is kept.';
            if (!confirm(warning)) return;
            try {
                const resp = await fetch('/admin/menu/unpublish-current', {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                const data = await resp.json();
                if (resp.ok && data.status === 'success') {
                    alert(data.message);
                    clearFileSelection();
                    loadActiveMenu();
                } else {
                    alert('Could not hide the menu: ' + (data.detail || 'please try again.'));
                }
            } catch (e) {
                alert('Connection error - the menu was not hidden.');
            }
        }

        // Replace keeps the entry's date and its two-week clock. Uploading
        // through the main form instead would make this old flyer the menu
        // customers are served this week.
        function replaceArchivedFlyer(flyerId) {
            const picker = document.createElement('input');
            picker.type = 'file';
            picker.accept = 'image/jpeg,image/png,image/webp';
            picker.onchange = async () => {
                const file = picker.files && picker.files[0];
                if (!file) return;
                const body = new FormData();
                body.append('file', file);
                try {
                    const resp = await fetch(`/admin/menu/archive/${encodeURIComponent(flyerId)}/replace`, {
                        method: 'POST',
                        headers: getAuthHeaders(),
                        body: body
                    });
                    const data = await resp.json();
                    if (resp.ok && data.status === 'success') {
                        loadActiveMenu();
                    } else {
                        alert('Not replaced: ' + (data.detail || 'the server refused the change.'));
                    }
                } catch (e) {
                    alert('Connection error - nothing was replaced.');
                }
            };
            picker.click();
        }

        async function confirmResetDefaults() {
            if (!confirm("Are you sure you want to reset the weekly rotation dishes to authentic Bengali defaults?")) return;
            
            try {
                const resp = await fetch('/admin/menu/reset-defaults', {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                const data = await resp.json();
                if (resp.ok) {
                    alert(data.message);
                    loadActiveMenu();
                } else {
                    alert('Error: ' + (data.detail || 'Reset failed'));
                }
            } catch (e) {
                alert('Connection error resetting menu.');
            }
        }

        // CUSTOMERS tab: escalations
        async function loadAnalytics() {
            try {
                const statsResp = await fetch('/admin/analytics', { headers: getAuthHeaders() });
                if (statsResp.ok) {
                    const stats = await statsResp.json();
                    document.getElementById('statTotal').innerText = stats.total_conversations || 0;
                    document.getElementById('statMatched').innerText = stats.matched_conversations || 0;
                    document.getElementById('statEscalated').innerText = stats.escalated_conversations || 0;
                }

                const escResp = await fetch('/admin/escalations?limit=50', { headers: getAuthHeaders() });
                if (escResp.ok) {
                    const escData = await escResp.json();
                    const tbody = document.getElementById('escalationsTableBody');
                    if (escData.escalations && escData.escalations.length > 0) {
                        let rows = '';
                        escData.escalations.forEach(e => {
                            const isResolved = (e.status || '').toLowerCase() === 'resolved';
                            const actionBtn = isResolved
                                ? '<span style="color: var(--success); font-size: 0.8rem; font-weight: 600;">✓ Resolved</span>'
                                : `<button class="btn-secondary" style="padding: 4px 10px; font-size: 0.78rem;" onclick="openResolveModal(${e.id}, '${escapeHtml(e.user_message)}')">✨ Teach & Resolve</button>`;

                            rows += `
                            <tr>
                                <td style="white-space: nowrap;">${escapeHtml(e.timestamp || 'Recent')}</td>
                                <td><b>${escapeHtml(e.channel)}</b></td>
                                <td><code>${escapeHtml(e.user_id)}</code></td>
                                <td>${escapeHtml(e.user_message)}</td>
                                <td><span class="badge ${isResolved ? 'badge-veg' : 'badge-nonveg'}">${escapeHtml(e.status.toUpperCase())}</span></td>
                                <td>${actionBtn}</td>
                            </tr>`;
                        });
                        tbody.innerHTML = rows;
                    } else {
                        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #888;">No escalations recorded yet.</td></tr>';
                    }
                }
            } catch (err) {
                console.error("Failed to load analytics:", err);
            }
        }

        // CUSTOMERS tab: conversations
        async function loadConversations() {
            const tbody = document.getElementById('conversationsTableBody');
            tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #888;">Loading conversations...</td></tr>';
            const ch = document.getElementById('convChannelFilter').value;
            const url = ch ? `/admin/conversations?limit=50&channel=${encodeURIComponent(ch)}` : '/admin/conversations?limit=50';

            try {
                const resp = await fetch(url, { headers: getAuthHeaders() });
                if (resp.ok) {
                    const data = await resp.json();
                    if (data.conversations && data.conversations.length > 0) {
                        let rows = '';
                        data.conversations.forEach(c => {
                            rows += `
                            <tr>
                                <td style="white-space: nowrap;">${escapeHtml(c.timestamp || 'Recent')}</td>
                                <td><b>${escapeHtml(c.channel)}</b></td>
                                <td><code>${escapeHtml(c.user_id)}</code></td>
                                <td>${escapeHtml(c.user_message)}</td>
                                <td style="max-width: 260px; white-space: pre-wrap; font-size: 0.8rem;">${escapeHtml(c.bot_response || '')}</td>
                                <td><span class="badge ${c.was_matched ? 'badge-veg' : 'badge-nonveg'}">${c.was_matched ? 'MATCHED' : 'ESCALATED'}</span></td>
                            </tr>`;
                        });
                        tbody.innerHTML = rows;
                    } else {
                        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #888;">No conversations logged yet.</td></tr>';
                    }
                }
            } catch (err) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: red;">Failed to load conversations.</td></tr>';
            }
        }

        // ANSWERS tab: hand-written answers
        async function loadKnowledge() {
            const tbody = document.getElementById('knowledgeTableBody');
            tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #888;">Loading learned knowledge...</td></tr>';

            try {
                const resp = await fetch('/admin/learned-knowledge?limit=100', { headers: getAuthHeaders() });
                if (resp.ok) {
                    const data = await resp.json();
                    if (data.learned_knowledge && data.learned_knowledge.length > 0) {
                        let rows = '';
                        data.learned_knowledge.forEach(k => {
                            rows += `
                            <tr>
                                <td><b>${escapeHtml(k.topic)}</b></td>
                                <td><code>${escapeHtml(k.question_pattern)}</code></td>
                                <td style="max-width: 280px; white-space: pre-wrap; font-size: 0.82rem;">${escapeHtml(k.verified_answer)}</td>
                                <td><span class="badge badge-veg">${escapeHtml(k.source || 'staff')}</span></td>
                                <td><button class="btn-secondary" style="padding: 4px 8px; font-size: 0.75rem; color: var(--danger);" onclick="deleteKnowledge(${k.id})">🗑️ Delete</button></td>
                            </tr>`;
                        });
                        tbody.innerHTML = rows;
                    } else {
                        tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #888;">No learned knowledge items yet. Taught answers appear here.</td></tr>';
                    }
                }
            } catch (err) {
                tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: red;">Failed to load knowledge base.</td></tr>';
            }
        }

        // ANSWERS tab: the review queue.
        //
        // The bot has always drafted these and saved them as PENDING_APPROVAL,
        // and the approve/reject API has always existed, but nothing in this
        // dashboard ever showed them — so the queue only ever grew. Approving
        // good drafts is what raises the bot's answer rate over time, and it
        // costs no latency at all: an approved answer is served from the
        // verified knowledge base, not from a model.
        async function loadCandidates() {
            const tbody = document.getElementById('candidatesTableBody');
            tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #888;">Loading suggested answers...</td></tr>';

            try {
                const resp = await fetch('/admin/candidates', { headers: getAuthHeaders() });
                if (!resp.ok) {
                    tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: red;">Could not load suggested answers.</td></tr>';
                    return;
                }
                const data = await resp.json();
                const items = data.candidates || [];
                updateCandidateBadge(items.length);

                if (!items.length) {
                    tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #888;">Nothing waiting. Drafts appear here when a customer asks something the bot cannot yet answer.</td></tr>';
                    return;
                }

                let rows = '';
                items.forEach(c => {
                    const confidence = Math.round((c.confidence || 0) * 100);
                    const confColour = confidence >= 85 ? 'var(--success, #2e7d32)' : (confidence >= 60 ? '#b26a00' : 'var(--danger)');
                    rows += `
                    <tr>
                        <td style="max-width: 200px;"><b>${escapeHtml(c.user_message || '—')}</b></td>
                        <td style="max-width: 340px; white-space: pre-wrap; font-size: 0.82rem;">${escapeHtml(c.response)}</td>
                        <td style="color: ${confColour}; font-weight: 600;">${confidence}%</td>
                        <td><span class="badge badge-veg">${escapeHtml(c.created_by || 'bot')}</span></td>
                        <td style="white-space: nowrap;">
                            <button class="btn-primary" style="padding: 4px 10px; font-size: 0.75rem;" onclick="approveCandidate(${c.id})">✅ Approve</button>
                            <button class="btn-secondary" style="padding: 4px 10px; font-size: 0.75rem; color: var(--danger);" onclick="rejectCandidate(${c.id})">✕ Reject</button>
                        </td>
                    </tr>`;
                });
                tbody.innerHTML = rows;
            } catch (err) {
                tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: red;">Failed to load suggested answers.</td></tr>';
            }
        }

        async function loadAssistedResolutions() {
            const tbody = document.getElementById('assistedResolutionsTableBody');
            if (!tbody) return;
            tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#888;">Loading AI suggestions...</td></tr>';
            const category = (document.getElementById('assistedCategoryFilter') || {}).value || '';
            try {
                const query = category ? `&category=${encodeURIComponent(category)}` : '';
                const resp = await fetch(`/admin/assisted-resolutions?status=PENDING_REVIEW${query}`, { headers: getAuthHeaders() });
                if (!resp.ok) throw new Error('Could not load AI suggestions');
                const data = await resp.json();
                const metrics = data.metrics || {};
                const metricsEl = document.getElementById('assistedResolutionMetrics');
                if (metricsEl) metricsEl.textContent = `Reviewed: ${metrics.suggestions_reviewed || 0} · Approved: ${metrics.approved_as_is || 0} · Edited: ${metrics.edited_before_send || 0} · Rejected: ${metrics.rejected || 0} · Sent to human: ${metrics.sent_to_human || 0}`;
                const items = data.suggestions || [];
                if (!items.length) {
                    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#888;">No pending AI suggestions.</td></tr>';
                    return;
                }
                tbody.innerHTML = items.map(item => {
                    const categoryLabel = escapeHtml(item.triage_category || 'UNSUPPORTED');
                    const grounding = escapeHtml(item.grounding_status || 'Not Grounded');
                    const source = escapeHtml(item.matched_knowledge_source || '—');
                    const risks = escapeHtml((item.risk_flags || []).join(', ') || 'None');
                    let actions = '';
                    if (item.triage_category === 'HUMAN_REQUIRED' || item.triage_category === 'CONTACT_DETAIL') {
                        actions += `<button class="btn-secondary" style="padding:4px 8px;font-size:.72rem" onclick="sendAssistedToHuman(${item.id})">Send to Human</button>`;
                    } else if (item.triage_category === 'NON_ACTIONABLE') {
                        actions += `<button class="btn-secondary" style="padding:4px 8px;font-size:.72rem" onclick="dismissAssisted(${item.id})">Dismiss</button>`;
                    } else {
                        actions += `<button class="btn-primary" style="padding:4px 8px;font-size:.72rem" onclick="approveAssisted(${item.id})">Approve &amp; Resolve</button> `;
                        actions += `<button class="btn-secondary" style="padding:4px 8px;font-size:.72rem" onclick="editAssisted(${item.id})">Edit &amp; Resolve</button> `;
                        actions += `<button class="btn-secondary" style="padding:4px 8px;font-size:.72rem" onclick="teachAssisted(${item.id})">Teach &amp; Resolve</button>`;
                    }
                    actions += ` <button class="btn-secondary" style="padding:4px 8px;font-size:.72rem;color:var(--danger)" onclick="rejectAssisted(${item.id})">Reject Suggestion</button>`;
                    return `<tr><td style="max-width:190px;white-space:pre-wrap"><b>${escapeHtml(item.customer_message || '—')}</b><br><span class="badge badge-veg">${categoryLabel}</span></td><td style="max-width:260px;white-space:pre-wrap;font-size:.82rem">${escapeHtml(item.suggested_answer || '—')}<br><small>${escapeHtml(item.grounding_details || '')}</small></td><td>${grounding}</td><td>${source}</td><td>${risks}</td><td style="white-space:nowrap">${actions}</td></tr>`;
                }).join('');
            } catch (err) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:red;">Failed to load AI suggestions.</td></tr>';
            }
        }

        async function assistedAction(id, path, body) {
            const options = { method: 'POST', headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' } };
            if (body) options.body = JSON.stringify(body);
            const resp = await fetch(`/admin/assisted-resolutions/${id}/${path}`, options);
            if (!resp.ok) { const err = await resp.json().catch(() => ({})); throw new Error(err.detail || resp.status); }
            await loadAssistedResolutions();
        }
        async function approveAssisted(id) { if (confirm('Approve and send this Instagram answer now?')) { try { await assistedAction(id, 'approve', {}); } catch (e) { alert(e); } } }
        async function editAssisted(id) { const answer = prompt('Edit the final answer before sending:'); if (answer && answer.trim()) { try { await assistedAction(id, 'edit-resolve', { final_answer: answer.trim() }); } catch (e) { alert(e); } } }
        async function rejectAssisted(id) { const reason = prompt('Why reject this suggestion?', 'Not approved'); if (reason !== null) { try { await assistedAction(id, 'reject', { reason }); } catch (e) { alert(e); } } }
        async function sendAssistedToHuman(id) { if (confirm('Send this case to a human? No Instagram answer will be sent by AI.')) { try { await assistedAction(id, 'send-to-human', {}); } catch (e) { alert(e); } } }
        async function teachAssisted(id) { const answer = prompt('Enter the verified answer to teach and send:'); if (answer && answer.trim()) { try { await assistedAction(id, 'teach-and-resolve', { verified_answer: answer.trim() }); } catch (e) { alert(e); } } }
        async function dismissAssisted(id) { if (confirm('Dismiss this non-actionable message?')) { try { await assistedAction(id, 'dismiss', {}); } catch (e) { alert(e); } } }

        function updateCandidateBadge(count) {
            const badge = document.getElementById('candidateBadge');
            if (!badge) return;
            if (count > 0) {
                badge.textContent = count;
                badge.style.display = 'inline-block';
            } else {
                badge.style.display = 'none';
            }
        }

        // Counts the queue without opening the tab, so a pending draft is
        // visible the moment the dashboard is unlocked rather than only to
        // someone who already knows to go looking for it.
        async function refreshCandidateBadge() {
            try {
                const resp = await fetch('/admin/candidates', { headers: getAuthHeaders() });
                if (!resp.ok) return;
                const data = await resp.json();
                updateCandidateBadge((data.candidates || []).length);
            } catch (err) { /* badge is a nicety; never block the dashboard on it */ }
        }

        async function approveCandidate(id) {
            if (!confirm('Approve this answer?\n\nIt will be added to the bot\'s verified knowledge and sent to customers who ask this from now on.')) return;
            try {
                const resp = await fetch(`/admin/candidates/${id}/approve`, {
                    method: 'POST',
                    headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                    body: JSON.stringify({ approved_by: 'admin_dashboard' })
                });
                if (resp.ok) {
                    loadCandidates();
                } else {
                    const err = await resp.json().catch(() => ({}));
                    alert('Could not approve: ' + (err.detail || resp.status));
                }
            } catch (err) {
                alert('Could not approve: ' + err);
            }
        }

        async function rejectCandidate(id) {
            if (!confirm('Reject this draft? It will not be shown to customers and will be removed from this list.')) return;
            try {
                const resp = await fetch(`/admin/candidates/${id}/reject`, {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                if (resp.ok) {
                    loadCandidates();
                } else {
                    const err = await resp.json().catch(() => ({}));
                    alert('Could not reject: ' + (err.detail || resp.status));
                }
            } catch (err) {
                alert('Could not reject: ' + err);
            }
        }

        function clearKnowledgeForm() {
            document.getElementById('newTopic').value = '';
            document.getElementById('newPattern').value = '';
            document.getElementById('newAnswer').value = '';
        }

        async function submitNewKnowledge() {
            const topic = document.getElementById('newTopic').value.trim();
            const pattern = document.getElementById('newPattern').value.trim();
            const answer = document.getElementById('newAnswer').value.trim();

            if (!topic || !pattern || !answer) {
                return alert('Please fill in Topic, Question Pattern, and Verified Answer.');
            }

            try {
                const resp = await fetch('/admin/learned-knowledge', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
                    body: JSON.stringify({ topic, question_pattern: pattern, verified_answer: answer, source: 'staff_dashboard' })
                });

                if (resp.ok) {
                    alert('Verified Q&A saved! The bot will now answer this question immediately without escalation.');
                    clearKnowledgeForm();
                    loadKnowledge();
                } else {
                    const err = await resp.json();
                    alert('Error saving knowledge: ' + (err.detail || 'Unknown error'));
                }
            } catch (e) {
                alert('Connection error saving knowledge.');
            }
        }

        async function deleteKnowledge(kId) {
            if (!confirm('Are you sure you want to delete this learned knowledge item?')) return;
            try {
                const resp = await fetch(`/admin/learned-knowledge/${kId}`, {
                    method: 'DELETE',
                    headers: getAuthHeaders()
                });
                if (resp.ok) {
                    loadKnowledge();
                } else {
                    alert('Failed to delete knowledge item.');
                }
            } catch (e) {
                alert('Error deleting knowledge item.');
            }
        }

        // Escalation Resolution Modal Handlers
        function openResolveModal(escId, userMsg) {
            document.getElementById('modalEscId').value = escId;
            document.getElementById('modalUserMsg').innerText = userMsg;
            document.getElementById('modalAnswer').value = '';
            document.getElementById('resolveModal').style.display = 'flex';
        }

        function closeResolveModal() {
            document.getElementById('resolveModal').style.display = 'none';
        }

        async function submitResolveAndLearn() {
            const escId = document.getElementById('modalEscId').value;
            const answer = document.getElementById('modalAnswer').value.trim();
            if (!answer) return alert('Please enter a verified answer to teach the bot.');

            try {
                const resp = await fetch(`/admin/escalations/${escId}/resolve`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
                    body: JSON.stringify({ verified_answer: answer, status: 'resolved' })
                });
                const data = await resp.json();
                if (resp.ok) {
                    alert('Escalation resolved and taught to bot successfully!');
                    closeResolveModal();
                    loadAnalytics();
                } else {
                    alert('Failed: ' + (data.detail || 'Could not resolve escalation.'));
                }
            } catch (e) {
                alert('Connection error resolving escalation.');
            }
        }

        async function resolveEscalationOnly() {
            const escId = document.getElementById('modalEscId').value;
            try {
                const resp = await fetch(`/admin/escalations/${escId}/resolve`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
                    body: JSON.stringify({ status: 'resolved' })
                });
                if (resp.ok) {
                    alert('Escalation marked as resolved.');
                    closeResolveModal();
                    loadAnalytics();
                }
            } catch (e) {
                alert('Error resolving escalation.');
            }
        }
    </script>
</body>
</html>
"""
    # The dashboard is one HTML document with its JavaScript inline, and it
    # shipped with no cache headers at all - no Cache-Control, no ETag, no
    # Last-Modified. A browser is then free to keep serving the copy it already
    # has, which is exactly what makes a deployed dashboard change look like a
    # failed deploy: the server has the new page, the kitchen's phone does not.
    # It is rendered per request anyway, so there is nothing to gain by caching
    # it and a support call to lose.
    return HTMLResponse(
        content=html_content,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

# --------------------------------------------------------------------------
# Interactive Web Chat Tester Endpoint (/chat)
# --------------------------------------------------------------------------

@app.post("/api/test-chat")
async def api_test_chat(request: Request):
    """Endpoint for interactive chat testing with Session Memory support."""
    require_web_tester()
    payload = await request.json()
    user_message = payload.get("message", "")
    channel = payload.get("channel", "instagram_ad")
    user_id = payload.get("user_id", "web_tester")

    if channel == "instagram_organic":
        # Simulate organic DM blocked by Ad-Gate
        return {
            "status": "blocked",
            "is_ad_qualified": False,
            "response": "🛑 [AD-GATE BLOCKED]: This is an organic (non-ad) DM. The bot does not auto-reply to organic DMs. Message sits in Inbox for human reply.",
            "was_matched": False,
            "image_url": None,
            "active_memory": session_memory.get_session(user_id, "instagram")["facts"]
        }

    # Simulate Instagram Ad or WhatsApp
    if channel == "instagram_ad":
        db.set_ad_qualified(user_id, "instagram", is_ad_qualified=True)
        ch_name = "instagram"
        ref_source = "ADS"
    else:
        ch_name = "whatsapp"
        ref_source = None

    response_text, was_matched, template_id, image_url = responder.match_and_respond(
        user_message, user_id=user_id, channel=ch_name, referral_source=ref_source
    )
    db.log_conversation(user_id, ch_name, user_message, response_text, was_matched, template_id)

    if not was_matched:
        escalator.handle_escalation(user_id, ch_name, user_message, "Web Tester Unmatched", {})

    active_facts = session_memory.get_session(user_id, ch_name)["facts"]

    return {
        "status": "processed",
        "is_ad_qualified": True,
        "response": response_text,
        "was_matched": was_matched,
        "template_id": template_id,
        "image_url": image_url,
        "active_memory": active_facts
    }

@app.post("/api/reset-session")
async def api_reset_session(request: Request):
    """Resets session memory for specified user and channel."""
    require_web_tester()
    payload = await request.json()
    user_id = payload.get("user_id", "web_tester")
    channel = payload.get("channel", "instagram_ad")
    ch_name = "whatsapp" if channel == "whatsapp" else "instagram"
    session_memory.clear_session(user_id, ch_name)
    return {"status": "cleared", "user_id": user_id, "channel": ch_name}

@app.get("/chat", response_class=HTMLResponse)
@app.get("/demo", response_class=HTMLResponse)
@app.get("/test", response_class=HTMLResponse)
@app.get("/web", response_class=HTMLResponse)
@app.get("/tester", response_class=HTMLResponse)
@app.get("/bot", response_class=HTMLResponse)
def get_web_chat():
    """Renders a sleek browser-based chat window to ask the bot questions directly with Memory controls."""
    require_web_tester()
    html_content = r"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Camellia Artisanal — Bot & Memory Tester</title>
        <style>
            * { box-sizing: border-box; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
            body { background: #f0f2f5; margin: 0; padding: 20px; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
            .chat-container { width: 100%; max-width: 520px; background: white; border-radius: 16px; box-shadow: 0 8px 24px rgba(0,0,0,0.12); display: flex; flex-direction: column; height: 92vh; overflow: hidden; }
            .header { background: #8B1E3F; color: white; padding: 16px 20px; display: flex; flex-direction: column; gap: 6px; }
            .header-top { display: flex; justify-content: space-between; align-items: center; }
            .header h2 { margin: 0; font-size: 1.2rem; }
            .reset-btn { background: rgba(255,255,255,0.2); border: 1px solid rgba(255,255,255,0.4); color: white; font-size: 0.75rem; padding: 4px 10px; border-radius: 12px; cursor: pointer; }
            .reset-btn:hover { background: rgba(255,255,255,0.35); }
            .header p { margin: 0; font-size: 0.85rem; opacity: 0.9; }
            .channel-select { margin-top: 4px; padding: 6px 10px; border-radius: 6px; border: none; font-size: 0.85rem; background: #fff; color: #333; }
            .memory-badge { background: #6b1630; color: #f8f9fa; padding: 6px 12px; font-size: 0.78rem; font-weight: 500; border-bottom: 1px solid #541124; display: flex; align-items: center; justify-content: space-between; }
            .messages { flex: 1; padding: 16px; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; background: #faf9f6; }
            .message { max-width: 90%; padding: 12px 16px; border-radius: 12px; font-size: 0.92rem; line-height: 1.45; word-wrap: break-word; }
            .user-msg { align-self: flex-end; background: #8B1E3F; color: white; border-bottom-right-radius: 2px; }
            .bot-msg { align-self: flex-start; background: #ffffff; color: #212529; border-bottom-left-radius: 2px; border: 1px solid #e2e8f0; box-shadow: 0 2px 6px rgba(0,0,0,0.04); }
            .chat-table-wrapper { overflow-x: auto; margin: 10px 0; border-radius: 8px; border: 1px solid #cbd5e1; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
            .chat-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; background: #ffffff; text-align: left; }
            .chat-table th { background: #8B1E3F; color: #ffffff; font-weight: 600; padding: 8px 10px; border-bottom: 2px solid #541124; white-space: nowrap; }
            .chat-table td { padding: 7px 10px; border-bottom: 1px solid #f1f5f9; color: #334155; }
            .chat-table tr:nth-child(even) td { background: #fdfbf7; }
            .chat-table tr:last-child td { border-bottom: none; }
            .chat-table th, .chat-table td { border-right: 1px solid #e2e8f0; }
            .chat-table th:last-child, .chat-table td:last-child { border-right: none; }
            .chat-code-table { background: #1e1e2e; color: #f1f5f9; border-radius: 8px; padding: 10px 12px; margin: 8px 0; font-family: ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, Consolas, 'Courier New', monospace; font-size: 0.78rem; line-height: 1.38; white-space: pre; overflow-x: auto; border: 1px solid #334155; box-shadow: inset 0 1px 3px rgba(0,0,0,0.3); }
            .chat-code-table code { font-family: inherit; color: inherit; background: transparent; padding: 0; }
            code:not(.chat-code-table code) { background: #f1f5f9; color: #8B1E3F; padding: 2px 5px; border-radius: 4px; font-size: 0.85em; font-family: monospace; }
            .menu-card-img { width: 100%; border-radius: 8px; margin-bottom: 8px; border: 1px solid #ddd; display: block; object-fit: contain; }
            .menu-card-wrap { margin-bottom: 10px; }
            .menu-card-caption { margin: 0 0 8px; font-size: 0.92em; }
            .meta-tag { font-size: 0.72rem; color: #6c757d; margin-top: 4px; display: block; }
            .input-area { padding: 12px 16px; background: white; border-top: 1px solid #e9ecef; display: flex; gap: 8px; }
            input[type="text"] { flex: 1; padding: 10px 14px; border: 1px solid #ced4da; border-radius: 24px; outline: none; font-size: 0.95rem; }
            input[type="text"]:focus { border-color: #8B1E3F; }
            button.send-btn { background: #8B1E3F; color: white; border: none; border-radius: 24px; padding: 10px 20px; font-weight: 600; cursor: pointer; }
            button.send-btn:hover { background: #6b1630; }
            .suggestions { padding: 8px 16px; background: #f8f9fa; border-top: 1px solid #eee; display: flex; gap: 6px; overflow-x: auto; white-space: nowrap; }
            .chip { background: #e9ecef; font-size: 0.78rem; padding: 4px 10px; border-radius: 12px; cursor: pointer; color: #495057; }
            .chip:hover { background: #dee2e6; }
            .chip.memory-chip { background: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd; }
            .chip.memory-chip:hover { background: #bae6fd; }
            .chip.menu-parse-chip { background: #fef3c7; color: #92400e; border: 1px solid #fde68a; font-weight: 600; }
            .chip.menu-parse-chip:hover { background: #fde68a; }
            .modal-overlay { display: none; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 100; justify-content: center; align-items: center; padding: 16px; }
            .modal-box { background: white; border-radius: 12px; padding: 16px; width: 100%; max-width: 440px; display: flex; flex-direction: column; gap: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.25); }
            .modal-box h3 { margin: 0; font-size: 1.05rem; color: #8B1E3F; }
            .modal-box textarea { width: 100%; height: 140px; border: 1px solid #ccc; border-radius: 8px; padding: 10px; font-size: 0.88rem; outline: none; resize: vertical; }
            .modal-box .modal-actions { display: flex; justify-content: flex-end; gap: 8px; }
            .modal-box button { padding: 8px 16px; border-radius: 18px; border: none; font-weight: 600; cursor: pointer; font-size: 0.85rem; }
            .btn-cancel { background: #e9ecef; color: #495057; }
            .btn-parse { background: #8B1E3F; color: white; }
        </style>
    </head>
    <body>
        <div class="chat-container">
            <div class="header">
                <div class="header-top">
                    <h2>Camellia Artisanal Bot Tester</h2>
                    <div style="display: flex; gap: 6px; align-items: center;">
                        <a href="/admin" target="_blank" class="reset-btn" style="text-decoration: none; display: inline-flex; align-items: center; background: #D4AF37; color: #2B2D42; font-weight: 700; border-color: #D4AF37;">🔐 Kitchen Portal</a>
                        <button class="reset-btn" onclick="openMenuModal()">📋 Paste Menu</button>
                        <button class="reset-btn" onclick="resetSession()">🧹 Clear Memory</button>
                    </div>
                </div>
                <p>Bengal-Rooted Artisanal Sweets & Food</p>
                <select id="channelSelect" class="channel-select" onchange="resetSession()">
                    <option value="instagram_ad">Instagram DM (Ad-Originated) ✅ Auto-Reply Enabled</option>
                    <option value="instagram_organic">Instagram DM (Organic) 🛑 Auto-Reply Blocked</option>
                    <option value="whatsapp">WhatsApp Business ✅ Auto-Reply Enabled</option>
                </select>
            </div>
            
            <div class="memory-badge" id="memoryBadge">
                <span>👤 Active Session Memory: None recorded</span>
            </div>

            <div class="messages" id="messages">
                <div class="message bot-msg">
                    Hi! 👋 Ask me anything about our Sondesh flavours, prices, weekly menu card, or delivery options! Try telling me your name (e.g. "My name is Shoaib").
                </div>
            </div>

            <div class="suggestions">
                <span class="chip menu-parse-chip" onclick="openMenuModal()">📋 Paste Menu Card Text</span>
                <span class="chip memory-chip" onclick="quickSend('My name is Shoaib')">1. Set Name</span>
                <span class="chip memory-chip" onclick="quickSend('What is my name?')">2. Recall Name</span>
                <span class="chip" onclick="quickSend('Show me the veg menu this week')">Veg Menu Card</span>
                <span class="chip" onclick="quickSend('How much is Sondesh?')">Sondesh Price</span>
                <span class="chip" onclick="quickSend('Who is the President of US?')">Out-of-Context Refusal</span>
            </div>

            <div class="input-area">
                <input type="text" id="userInput" placeholder="Ask a question..." onkeydown="if(event.key==='Enter') sendMessage()" autofocus>
                <button class="send-btn" id="sendBtn" onclick="sendMessage()">Send</button>
            </div>
        </div>

        <div class="modal-overlay" id="menuModal">
            <div class="modal-box">
                <h3>📋 Paste Raw Menu Card Text</h3>
                <p style="margin: 0; font-size: 0.8rem; color: #555;">Paste raw flyer text, WhatsApp broadcast, or dish items with prices below. The AI/parser will structure & update the bot's menu automatically!</p>
                <textarea id="rawMenuInput" placeholder="Example:&#10;Rose Sondesh 250g - Rs 450&#10;Jolbhora Nolen Gur 500g ₹1400&#10;Kosha Mangsho Bento ₹650 (non veg)"></textarea>
                <div class="modal-actions">
                    <button class="btn-cancel" onclick="closeMenuModal()">Cancel</button>
                    <button class="btn-parse" onclick="parseMenuCardText()">✨ Auto-Extract & Update Menu</button>
                </div>
            </div>
        </div>

        <script>
            function openMenuModal() { document.getElementById('menuModal').style.display = 'flex'; }
            function closeMenuModal() { document.getElementById('menuModal').style.display = 'none'; }

            async function parseMenuCardText() {
                const text = document.getElementById('rawMenuInput').value.trim();
                if (!text) return alert('Please paste some menu text first!');

                closeMenuModal();
                appendMessage('📋 [Auto-Extracting Menu Card Text...]', 'user-msg');

                try {
                    const resp = await fetch('/admin/parse-menu-card', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ raw_text: text })
                    });
                    const data = await resp.json();

                    if (data.status === 'success') {
                        appendMessage(`✅ ${data.message}\n\nParsed Items:\n${JSON.stringify(data.parsed, null, 2)}`, 'bot-msg', 'Menu Hot-Reloaded');
                    } else {
                        appendMessage(`⚠️ Menu Parse Failed: ${data.reason || data.error}`, 'bot-msg');
                    }
                } catch (err) {
                    appendMessage('Error parsing menu card.', 'bot-msg');
                }
            }

            async function sendMessage() {
                const input = document.getElementById('userInput');
                const sendBtn = document.getElementById('sendBtn');
                const text = input.value.trim();
                if (!text) {
                    input.focus();
                    return;
                }

                const channel = document.getElementById('channelSelect').value;
                appendMessage(text, 'user-msg');
                input.value = '';

                // Show typing indicator & disable button
                if (sendBtn) { sendBtn.disabled = true; sendBtn.innerText = '...'; }
                if (input) input.disabled = true;

                const typingDiv = document.createElement('div');
                typingDiv.id = 'typingIndicator';
                typingDiv.className = 'message bot-msg';
                typingDiv.style.color = '#888';
                typingDiv.style.fontStyle = 'italic';
                typingDiv.innerText = 'Camellia is typing...';
                document.getElementById('messages').appendChild(typingDiv);
                document.getElementById('messages').scrollTop = document.getElementById('messages').scrollHeight;

                try {
                    const resp = await fetch('/api/test-chat', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ message: text, channel: channel, user_id: 'web_tester' })
                    });
                    
                    // Remove typing indicator
                    const indicator = document.getElementById('typingIndicator');
                    if (indicator) indicator.remove();

                    if (!resp.ok) {
                        const errData = await resp.json().catch(() => ({}));
                        appendMessage(`⚠️ Server Error (${resp.status}): ${errData.detail || resp.statusText || 'Unable to process request'}`, 'bot-msg');
                        return;
                    }

                    const data = await resp.json();
                    
                    let metaText = data.was_matched ? `Matched: ${data.template_id}` : `Escalated to Human`;
                    if (channel === 'instagram_organic') metaText = 'Ad-Gate Filtered';
                    
                    const botReply = data.response || data.detail || 'No response returned.';
                    appendMessage(botReply, 'bot-msg', metaText, data.image_url);
                    updateMemoryBadge(data.active_memory);
                } catch (err) {
                    const indicator = document.getElementById('typingIndicator');
                    if (indicator) indicator.remove();
                    appendMessage(`⚠️ Connection error: Could not reach backend server (${err.message}). Is the server running?`, 'bot-msg');
                } finally {
                    if (sendBtn) { sendBtn.disabled = false; sendBtn.innerText = 'Send'; }
                    if (input) {
                        input.disabled = false;
                        input.focus();
                    }
                }
            }

            async function resetSession() {
                const channel = document.getElementById('channelSelect').value;
                try {
                    await fetch('/api/reset-session', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ channel: channel, user_id: 'web_tester' })
                    });
                    updateMemoryBadge({ name: null });
                    appendMessage("🧹 Session memory reset for active channel.", "bot-msg", "System Event");
                } catch (err) {
                    console.error("Failed to reset session:", err);
                }
            }

            function updateMemoryBadge(memory) {
                const badge = document.getElementById('memoryBadge');
                if (!memory) return;
                const nameStr = memory.name ? `Name: <b>${memory.name}</b>` : 'Name: None';
                badge.innerHTML = `<span>👤 Session Memory: ${nameStr}</span>`;
            }

            function quickSend(text) {
                document.getElementById('userInput').value = text;
                sendMessage();
            }

            function formatMarkdown(text) {
                if (!text) return '';
                const escapeHtml = (str) => str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
                
                const lines = text.split(/\r?\n/);
                let out = [];
                let inCode = false;
                let codeLines = [];
                let inTable = false;
                let tableRows = [];

                function renderTable(rows) {
                    if (rows.length === 0) return '';
                    let html = '<div class="chat-table-wrapper"><table class="chat-table">';
                    let isHeader = true;
                    for (let r = 0; r < rows.length; r++) {
                        const rowStr = rows[r].trim();
                        if (/^\|?(\s*:?-+:?\s*\|?)+$/.test(rowStr)) {
                            isHeader = false;
                            continue;
                        }
                        const rawCols = rowStr.split('|');
                        const cols = rawCols.map(c => c.trim()).filter((c, idx, arr) => {
                            if ((idx === 0 || idx === arr.length - 1) && c === '') return false;
                            return true;
                        });
                        if (cols.length === 0) continue;

                        html += '<tr>';
                        const tag = isHeader ? 'th' : 'td';
                        cols.forEach(cell => {
                            let formattedCell = escapeHtml(cell)
                                .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
                                .replace(/(^|[^*])\*([^*]+)\*([^*]|$)/g, '$1<strong>$2</strong>$3')
                                .replace(/`([^`]+)`/g, '<code>$1</code>');
                            html += `<${tag}>${formattedCell}</${tag}>`;
                        });
                        html += '</tr>';
                        if (isHeader) isHeader = false;
                    }
                    html += '</table></div>';
                    return html;
                }

                function renderCodeBlock(cLines) {
                    if (cLines.length === 0) return '';
                    return `<pre class="chat-code-table"><code>${escapeHtml(cLines.join('\n'))}</code></pre>`;
                }

                for (let i = 0; i < lines.length; i++) {
                    const line = lines[i];
                    const trimmed = line.trim();

                    // Check for code block fence (```)
                    if (trimmed.startsWith('```')) {
                        if (inCode) {
                            out.push(renderCodeBlock(codeLines));
                            codeLines = [];
                            inCode = false;
                        } else {
                            if (inTable) {
                                out.push(renderTable(tableRows));
                                tableRows = [];
                                inTable = false;
                            }
                            inCode = true;
                            codeLines = [];
                        }
                        continue;
                    }

                    if (inCode) {
                        codeLines.push(line);
                        continue;
                    }

                    if (trimmed.startsWith('|') && trimmed.endsWith('|') && !trimmed.startsWith('+')) {
                        inTable = true;
                        tableRows.push(line);
                    } else {
                        if (inTable) {
                            out.push(renderTable(tableRows));
                            tableRows = [];
                            inTable = false;
                        }
                        let formattedLine = escapeHtml(line)
                            .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
                            .replace(/(^|[^*])\*([^*]+)\*([^*]|$)/g, '$1<strong>$2</strong>$3')
                            .replace(/`([^`]+)`/g, '<code>$1</code>');
                        out.push(formattedLine);
                    }
                }
                if (inCode) {
                    out.push(renderCodeBlock(codeLines));
                }
                if (inTable) {
                    out.push(renderTable(tableRows));
                }

                return out.join('<br>');
            }

            function appendMessage(text, className, meta = '', imageUrl = null) {
                const box = document.getElementById('messages');
                const div = document.createElement('div');
                div.className = 'message ' + className;

                // Real channels send the reply's own text first when the
                // first card is marked text_first (the price/menu flows: text,
                // then each collection's note-and-card pair). This function
                // used to always draw every image before the outer text
                // regardless of that flag, so the tester showed the general
                // text arriving dead last — the opposite of what Instagram and
                // WhatsApp actually send, and of the order the client asked for.
                const entries0 = Array.isArray(imageUrl) ? imageUrl : (imageUrl ? [imageUrl] : []);
                const leadsWithText = entries0.some(e => e && typeof e === 'object' && e.text_first);

                if (leadsWithText && text) {
                    const leadText = document.createElement('div');
                    leadText.className = 'msg-content';
                    leadText.innerHTML = formatMarkdown(text);
                    div.appendChild(leadText);
                }

                if (imageUrl) {
                    const entries = Array.isArray(imageUrl) ? imageUrl : [imageUrl];
                    // Each entry is either a plain URL string, or {url, caption}
                    // when the template pairs a card with its own description.
                    entries.forEach(entry => {
                        const isObj = entry && typeof entry === 'object';
                        const u = isObj ? entry.url : entry;
                        const caption = isObj ? entry.caption : null;
                        if (!u) return;

                        const wrap = document.createElement('div');
                        wrap.className = 'menu-card-wrap';

                        // The real channels send the note before the card it
                        // describes when caption_first is set. The tester used
                        // to always draw image-then-caption, so it showed the
                        // old order and looked like the change had not shipped.
                        if (caption && isObj && entry.caption_first) {
                            const leadIn = document.createElement('div');
                            leadIn.className = 'msg-content menu-card-caption';
                            leadIn.innerHTML = formatMarkdown(caption);
                            wrap.appendChild(leadIn);
                        }

                        const img = document.createElement('img');
                        img.src = u;
                        img.className = 'menu-card-img';
                        if (u.includes('AAMAR')) {
                            img.alt = 'Collection 1 — Aamar Mishti Menu Card';
                        } else if (u.includes('Signature')) {
                            img.alt = 'Collection 2 — Signature Sondesh Menu Card';
                        } else if (u.includes('Friday')) {
                            img.alt = 'From Friday Weekend Menu Card';
                        } else if (u.includes('Thursday')) {
                            img.alt = 'Till Thursday Weekday Menu Card';
                        } else {
                            img.alt = 'Camellia Artisanal Menu Card';
                        }
                        wrap.appendChild(img);

                        if (caption && !(isObj && entry.caption_first)) {
                            const capDiv = document.createElement('div');
                            capDiv.className = 'msg-content menu-card-caption';
                            capDiv.innerHTML = formatMarkdown(caption);
                            wrap.appendChild(capDiv);
                        }

                        div.appendChild(wrap);
                    });
                }

                if (!leadsWithText) {
                    const contentDiv = document.createElement('div');
                    contentDiv.className = 'msg-content';
                    contentDiv.innerHTML = formatMarkdown(text);
                    div.appendChild(contentDiv);
                }

                if (meta) {
                    const span = document.createElement('span');
                    span.className = 'meta-tag';
                    span.innerText = meta;
                    div.appendChild(span);
                }
                
                box.appendChild(div);
                box.scrollTop = box.scrollHeight;
            }

            // Keyboard shortcut for Enter
            document.addEventListener('DOMContentLoaded', () => {
                const input = document.getElementById('userInput');
                if (input) {
                    input.addEventListener('keydown', (e) => {
                        if (e.key === 'Enter') {
                            e.preventDefault();
                            sendMessage();
                        }
                    });
                }
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)

