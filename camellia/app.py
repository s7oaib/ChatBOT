import asyncio
import csv
import difflib
import hmac
import hashlib
import io
import json
import os
import re
import time
import uuid
import httpx
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Request, Response, Query, Header, HTTPException, File, Form, UploadFile, BackgroundTasks
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
import camellia.responder as responder_module
from camellia.responder import compose_topic_response, responder
from camellia.ai_responder import ai_responder
from camellia.whatsapp_adapter import whatsapp_adapter
from camellia.escalator import escalator
from camellia.handoff import (
    customer_facing_link_message,
    customer_facing_notice,
    get_or_create_handoff,
    mark_referred,
    retire_order_state,
    staff_summary,
)
from camellia.ad_routing import product_for_ad
from camellia.instagram_routing import (
    InstagramTopicError,
    create_topic,
    decode_route,
    decode_topic,
    lookup_instagram_media,
    migrate_legacy_routing_topics,
    normalize_instagram_url,
    product_catalogue,
    route_for_inbound,
    route_template_id,
    RESPONSE_ORDERS,
    shared_post_media_id,
    shared_reel_identity,
    update_topic,
    validate_route_payload,
    verify_media_by_id,
)
from camellia.sheet_sync import sheet_sync
from camellia.menu_parser import menu_parser
from camellia import weekly_flyer
from camellia import menu_dates
from camellia import menu_manager
from camellia import menu_images
from camellia import menu_catalogue
from camellia.menu_catalogue import MenuCatalogueError
from camellia.session_memory import session_memory
from camellia.monitoring import get_logger, delivery_monitor, latency_monitor
from camellia.knowledge import SOURCE_MAP
from camellia.candidate_safety import candidate_eligibility, detect_business_commitment_warnings, validate_candidate_answer
from camellia.assisted_resolution import create_suggestion
from camellia.auto_resolve import simulate_auto_resolve
from camellia.content_integrity import apply_confirmed_menu_facts, apply_safe_content_remediations

log = get_logger("camellia.app")


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
    # Apply only the audited, canonical copy corrections through the existing
    # versioned Response Library store.  This is idempotent and deliberately
    # preserves any distinct client-authored override.
    # Production uses the durable /app/var database.  Keep local/unit-test
    # databases opt-in so a test that deliberately exercises legacy seed copy
    # is not mutated merely by constructing a TestClient.
    db_path = str(getattr(config, "DB_PATH", "")).replace("\\", "/")
    if db_path.startswith("/app/var/") or os.getenv("CONTENT_INTEGRITY_REMEDIATION") == "true":
        try:
            if apply_confirmed_menu_facts(config.MENU_JSON_PATH, log):
                responder.reload_data()
            apply_safe_content_remediations(
                db, responder.menu_data, responder.qa_data.get("templates", []), log
            )
        except Exception:
            # Copy remediation must never make the service fail to start; the
            # protected seed/runtime remains available if the durable volume
            # is temporarily unavailable.
            log.exception("Could not apply content-integrity copy remediations.")
    # Built-in menu catalogue rows are seeded inside init_db() itself (every
    # Database instance gets them, test fixtures included); only the
    # Topic-backfill migration needs a separate call here.
    try:
        migrate_legacy_routing_topics()
    except Exception:
        log.exception("Could not migrate legacy Instagram routing topics.")
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
            "media_id": entry.media_id,
        }
        if entry.ad_title and not facts.get("ad_title"):
            facts["ad_title"] = entry.ad_title
        ad_product = product_for_ad(trace["ad_id"], trace["ad_title"])
        if ad_product:
            facts["ad_product"] = ad_product
        source_route, source_match_method = route_for_inbound(
            ad_id=trace["ad_id"], media_id=trace["media_id"],
        )
        if source_route:
            facts["source_route"] = source_route
        elif isinstance(facts.get("source_route"), dict):
            source_route = facts["source_route"]
            # A reel shared a moment ago is why this route is in the session
            # at all, so the follow-up message is attributed to the share
            # rather than to the vaguer "it was already here".
            source_match_method = facts.get("source_match_method") or "session"
        facts["source_context_used"] = False
        trace.update({
            "source_routing_id": (source_route or {}).get("routing_id"),
            "source_match_method": source_match_method,
            "source_type": (source_route or {}).get("source_type"),
            "shortcode": (source_route or {}).get("shortcode"),
            "routing_topic": (source_route or {}).get("routing_topic"),
        })
        log.info(
            "Instagram inbound from %s: source=%s ad_id=%s ad_title=%r payload=%r",
            sender_id, entry.source, trace["ad_id"], trace["ad_title"], entry.raw_payload,
        )

        response_text, was_matched, template_id, image_url = responder.match_and_respond(
            message_text, user_id=sender_id, channel="instagram",
            referral_source="ADS", referral_ad_id=trace["ad_id"], referral_ad_title=trace["ad_title"],
        )

        # Human-gated assisted resolution: unresolved Instagram messages only.
        # The draft is durable and reviewable; it is never sent from this path.
        protected_flow = bool(facts.get("cart") or facts.get("order_draft")) or template_id in ("order_draft_receipt", "order_next_steps")
        if (config.LLM_ASSISTED_RESOLUTION_ENABLED or config.LLM_AUTO_RESOLVE_SIMULATION_ENABLED) and not was_matched and not protected_flow and not source_route:
            assisted_suggestion = create_suggestion(
                user_id=sender_id, channel="instagram", customer_message=message_text,
                deterministic_resolved=False, active_protected_flow=protected_flow,
                source_routed=bool(source_route), source_routing_id=trace.get("source_routing_id"),
            )
            # Full deterministic eligibility runs in simulation, but this call
            # only records an audit row.  It never sends an automatic reply;
            # the normal human-gated assisted workflow below remains intact.
            if assisted_suggestion and config.LLM_AUTO_RESOLVE_SIMULATION_ENABLED:
                simulate_auto_resolve(assisted_suggestion, protected_order_flow=protected_flow, source_routed=bool(source_route))

        # Channel policy: Instagram is the only AI chatbot channel. When it
        # cannot answer — an unsupported question, or an order/event ask that
        # genuinely needs a person — the customer continues on WhatsApp with
        # a human, never with more bot. No phone number is collected here any
        # more: the customer opens WhatsApp themselves via the link below, so
        # nothing is typed, mistyped, or stored on Instagram along the way.
        # An explicit human-contact request (matched by its own approved
        # template) is also a handoff, not just an unmatched question: the
        # client wants a clickable WhatsApp continuation here, not a phone
        # number quoted as plain Instagram text.
        needs_escalation = (
            not was_matched
            or (source_route or {}).get("escalation_policy") == "always_human"
            or template_id in (
                "explicit_escalation",
                "order_draft_receipt",
                "order_next_steps",
                "GUARD_staff_names_manager_contact",
            )
        )
        if needs_escalation:
            reason = (
                "Order Confirmation & Intake Request" if was_matched
                else "Unmatched Instagram Query / Sensitive Request"
            )
            # Created before the staff alert (not after) so the alert can name
            # the CAM reference and the known-entities summary the customer
            # never has to repeat, instead of a second, disconnected message.
            # Only a message that is actually placing an order may carry the
            # cart into the handoff. "Talk to human" is not one of those, and
            # treating it as one is what attached a days-old Jolbhora order to
            # a plain request to speak to somebody.
            is_order_handoff = template_id in ("order_draft_receipt", "order_next_steps")
            handoff = get_or_create_handoff(
                user_id=sender_id, channel="instagram", reason=reason,
                user_message=message_text, facts=facts,
                include_order=is_order_handoff,
            )
            # A repeated "human"/"can someone help" against an already-active
            # handoff reuses the reference (above) and must not re-alert
            # staff a second time for the same open enquiry.
            if handoff.get("newly_created", True):
                escalator.handle_escalation(
                    user_id=sender_id,
                    channel="instagram",
                    user_message=message_text,
                    reason=reason,
                    context_data={
                        "ad_id": ad_id, "sender_id": sender_id, "template_id": template_id,
                        "handoff_ref": handoff["handoff_ref"],
                        "handoff_summary": staff_summary(handoff),
                    },
                )
            order_summary = handoff.get("verified_order")
            if template_id == "order_draft_receipt":
                response_text = response_text.rstrip()
            else:
                response_text = customer_facing_notice()
            link_message = customer_facing_link_message(handoff["handoff_ref"], order_summary)
            mark_referred(handoff["handoff_ref"])
            if is_order_handoff and order_summary:
                # The order is now on its way to a human, prefilled in the
                # link above. Instagram's copy has done its job; keeping it
                # would make it luggage for every later enquiry.
                retire_order_state(sender_id, "instagram")
            template_id = "instagram_whatsapp_handoff"
            image_url = None

        trace["source_context_used"] = bool(facts.get("source_context_used"))
        logged_response = response_text
        if needs_escalation:
            logged_response = f"{response_text}\n\n{link_message}"
        conversation_id = db.log_conversation(
            sender_id, "instagram", message_text, logged_response, was_matched,
            template_id, **trace,
        )

        send_instagram_reply(
            sender_id, response_text, image_url=image_url,
            inbound_message_id=conversation_id, response_type="reply",
        )
        if needs_escalation:
            send_instagram_reply(
                sender_id, link_message,
                inbound_message_id=conversation_id, response_type="handoff",
            )
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
    "or delivery across Delhi NCR. If you'd rather speak to someone, reply “human”."
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
        conversation_id = db.log_conversation(
            sender_id, "instagram", "[attachment]", _UNREADABLE_ATTACHMENT_REPLY,
            True, "unreadable_attachment", message_source="attachment",
        )
        send_instagram_reply(
            sender_id, _UNREADABLE_ATTACHMENT_REPLY,
            inbound_message_id=conversation_id, response_type="attachment_notice",
        )
    except Exception:
        log.exception("Failed to answer an unreadable attachment from %s.", sender_id)


def _apply_source_context(sender_id: str, route: Dict[str, Any], match_method: str) -> None:
    """Writes a resolved route into the session for the next message to use.

    Shared by the reel and the paired-post paths — both establish exactly
    the same kind of context, just from different evidence.
    """
    facts = session_memory.get_facts(sender_id, "instagram")
    facts["source_route"] = route
    facts["source_match_method"] = match_method
    facts["source_context_superseded"] = False
    # Sharing a post is itself a change of subject: the customer has just put
    # a specific thing in front of us, newer and more specific than whatever
    # was last discussed. Without clearing that stale topic, source routing
    # is skipped for the vague follow-up it exists to answer —
    # `can_use_source` in responder.py treats any `last_topic` as the
    # conversation already having a subject. In production this is what sent
    # a "tell me more" straight after a shared reel into general matching
    # instead of the reel's route.
    #
    # An open question the bot itself asked (awaiting_*) is deliberately left
    # alone: that is a reply the customer still owes us, and the pack size or
    # area they type next has to keep resolving. Cart, order_draft and any
    # CAM handoff are untouched — a share changes conversational subject, not
    # order state.
    facts["last_topic"] = None
    facts["source_priority_cursor"] = 0


def _establish_shared_reel_context(sender_id: str, identity: Dict[str, str]) -> bool:
    """
    Remembers which configured reel a customer just shared, for their next
    message to use. Returns whether a configured route was found.

    A shared reel carries no question, so nothing is answered from it here —
    it only establishes context. The customer sends "tell me more" a few
    seconds later, and that message is the one that needs to know which post
    it is about.

    An unconfigured reel (a customer forwarding someone else's video, which
    production traffic does contain) resolves to nothing and leaves the
    session exactly as it was. No topic is guessed from the caption.
    """
    try:
        route, match_method = route_for_inbound(
            ad_id=None, media_id=None, shortcode=identity.get("shortcode"),
        )
        if not route:
            log.info(
                "Shared reel %s from %s is not configured for routing; no source context set.",
                identity.get("shortcode"), sender_id,
            )
            return False
        _apply_source_context(sender_id, route, match_method)
        log.info(
            "Shared reel %s from %s matched routing_id %s (%s) via %s.",
            identity.get("shortcode"), sender_id, route.get("routing_id"),
            route.get("routing_topic"), match_method,
        )
        return True
    except Exception:
        # Context is a nicety; the attachment reply below is not allowed to
        # fail because of it.
        log.exception("Could not establish shared-reel context for %s.", sender_id)
        return False


def _establish_shared_post_context(sender_id: str, post_media_id: str) -> bool:
    """
    Resolves a shared post's messaging-attachment id against a configured
    route, either one already paired or — while an admin has deliberately
    opened a pairing window — this exact share. Returns whether a route was
    established.

    A post's id has no deterministic permalink to fall back on (see
    ``shared_post_media_id``'s docstring), so unlike a reel, an unpaired post
    resolves to nothing: no shortcode, no caption match, no guess from
    timing. Only an id that is either already bound to a route, or captured
    during that route's own open pairing window, is trusted.
    """
    try:
        route, match_method = route_for_inbound(
            ad_id=None, media_id=None, post_media_id=post_media_id,
        )
        if route:
            _apply_source_context(sender_id, route, match_method)
            log.info(
                "Shared post %s from %s matched routing_id %s (%s) via %s.",
                post_media_id, sender_id, route.get("routing_id"),
                route.get("routing_topic"), match_method,
            )
            return True

        pairing = db.get_active_post_pairing()
        if not pairing:
            log.info(
                "Shared post %s from %s is not configured and no pairing is open; no source context set.",
                post_media_id, sender_id,
            )
            return False

        bound = db.bind_post_pairing(pairing["id"], post_media_id)
        if not bound:
            # The window closed between the check above and this write —
            # a race, not an error; the share is simply unpaired.
            return False
        route_row = db.get_instagram_source_route(pairing["routing_id"])
        if not route_row:
            return False
        route = decode_route(route_row)
        _apply_source_context(sender_id, route, "shared_post_media_id")
        log.info(
            "Paired shared post %s from %s to routing_id %s (%s) via an open admin pairing window.",
            post_media_id, sender_id, route.get("routing_id"), route.get("routing_topic"),
        )
        return True
    except Exception:
        log.exception("Could not establish shared-post context for %s.", sender_id)
        return False


def _handle_shared_attachment(
    sender_id: str,
    reel_identity: Optional[Dict[str, str]],
    post_media_id: Optional[str] = None,
) -> None:
    """
    Establishes source context where there is any, then answers the
    attachment.

    A recognized post — already paired, or bound just now against an open
    pairing window — skips the "I can't open photos" reply: the system did
    understand what was shared, and sending that message would misstate what
    just happened. A reel keeps its existing reply exactly as before; that
    path is proven in production and this change does not touch it.
    """
    if reel_identity:
        _establish_shared_reel_context(sender_id, reel_identity)
        _reply_to_unreadable_attachment(sender_id)
        return
    if post_media_id:
        recognized = _establish_shared_post_context(sender_id, post_media_id)
        if recognized:
            return
    _reply_to_unreadable_attachment(sender_id)


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


# Sharing a reel into a DM arrives as an attachment with no message text, so
# it never reaches the source-routing path. Whether Meta puts anything usable
# (a permalink, a media id) inside that attachment is not documented by any
# payload this repo has ever captured, and the code has only ever checked
# `message.attachments` for truthiness — so nothing here assumes a field name.
# This records the structure Meta actually sends, so the question can be
# answered from evidence before any matcher is written.
#
# Structure, not content: values are kept only where they identify a shared
# object (ids, types, URLs with the query string removed), everything else is
# reported as its type. Deliberately in memory and bounded — a diagnostic
# answering one question, not a second copy of customer data.
_ATTACHMENT_DEBUG_EVENTS: List[Dict[str, Any]] = []
_ATTACHMENT_DEBUG_LIMIT = 10
_ATTACHMENT_DEBUG_MAX_DEPTH = 6
_ATTACHMENT_DEBUG_MAX_STR = 300

# Meta signs its CDN URLs with short-lived credentials in the query string
# (and a shared link can carry click-tracking tokens), so a URL is recorded
# as scheme://host/path only. A key that looks like a credential is never
# recorded at all, whatever Meta decides to call it.
_SECRET_KEY_HINTS = (
    "token", "secret", "signature", "auth", "cookie", "password",
    "credential", "session", "bearer", "apikey", "api_key",
)


def _sanitize_debug_url(value: str) -> str:
    """scheme://host/path — the shape that identifies a shared object, with
    signed query parameters dropped."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<unparseable-url>"
    if not parsed.scheme or not parsed.hostname:
        return "<not-a-url>"
    cleaned = f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
    if parsed.query:
        cleaned += "?<query-removed>"
    return cleaned[:_ATTACHMENT_DEBUG_MAX_STR]


def _sanitize_debug_value(key: str, value: Any, depth: int = 0) -> Any:
    """
    Reports what Meta sent without copying anything sensitive out of it.

    Field names are never assumed — whatever keys arrive are the keys
    reported. Only the values that could identify a shared post/reel survive
    in readable form; everything else degrades to its type.
    """
    if any(hint in key.lower() for hint in _SECRET_KEY_HINTS):
        return "<redacted-by-key-name>"
    if depth >= _ATTACHMENT_DEBUG_MAX_DEPTH:
        return "<max-depth>"

    if isinstance(value, dict):
        return {
            str(k): _sanitize_debug_value(str(k), v, depth + 1)
            for k, v in list(value.items())[:40]
        }
    if isinstance(value, list):
        return [_sanitize_debug_value(key, item, depth + 1) for item in value[:10]]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return _sanitize_debug_url(value)
        if len(value) > _ATTACHMENT_DEBUG_MAX_STR:
            return f"<str len={len(value)}>"
        return value
    return f"<{type(value).__name__}>"


def _record_attachment_debug_event(messaging_event: dict, sender_id: Optional[str]) -> None:
    """
    Captures the sanitized shape of one attachment-only Instagram message.

    Changes nothing about how the message is answered — this only observes.
    Never allowed to raise: a diagnostic that can break the webhook is worse
    than the missing evidence it was added to collect.
    """
    try:
        message = messaging_event.get("message") or {}
        attachments = message.get("attachments")
        attachment_list = attachments if isinstance(attachments, list) else []

        _ATTACHMENT_DEBUG_EVENTS.append({
            "at": datetime.utcnow().isoformat(timespec="seconds"),
            "had_sender": bool(sender_id),
            "event_keys": sorted(str(k) for k in messaging_event.keys()),
            "message_keys": sorted(str(k) for k in message.keys()),
            "attachment_count": len(attachment_list),
            # Every key each attachment actually carries, so a field this
            # code has never heard of still shows up.
            "attachment_keys": [
                sorted(str(k) for k in item.keys()) if isinstance(item, dict) else f"<{type(item).__name__}>"
                for item in attachment_list
            ],
            "attachments": _sanitize_debug_value("attachments", attachment_list),
            # A reply/share wrapper, if Meta sends one, lives beside the
            # attachments rather than inside them.
            "reply_to": _sanitize_debug_value("reply_to", message.get("reply_to")),
            "referral": _sanitize_debug_value("referral", messaging_event.get("referral")),
        })
        del _ATTACHMENT_DEBUG_EVENTS[:-_ATTACHMENT_DEBUG_LIMIT]
    except Exception:
        log.exception("Attachment diagnostic capture failed; the message itself is unaffected.")


@app.get("/admin/debug/instagram-attachments")
def get_instagram_attachment_debug(
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Sanitized structure of recent attachment-only Instagram messages."""
    require_admin(x_admin_key, x_admin_pin)
    return {
        "count": len(_ATTACHMENT_DEBUG_EVENTS),
        "limit": _ATTACHMENT_DEBUG_LIMIT,
        "events": list(reversed(_ATTACHMENT_DEBUG_EVENTS)),
    }


@app.get("/admin/debug/instagram-media/{media_id}")
def get_instagram_media_verification(
    media_id: str,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Fetches one media id directly by Graph API and reports its real identity.

    Diagnostic only: this is how an id captured off a real webhook event
    (an attachment's ig_post_media_id, for instance) is checked against what
    it actually is before any routing logic trusts it. It never writes
    anything and is not part of any inbound matcher.
    """
    require_admin(x_admin_key, x_admin_pin)
    return verify_media_by_id(media_id)


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

            # A contact-card attachment used to be read as a WhatsApp number
            # the customer was asked to share. Channel policy retired that
            # flow — Instagram no longer asks for or collects a phone number,
            # the customer opens WhatsApp themselves via the handoff link —
            # so a contact card with no message text now falls through to the
            # same "cannot read this attachment" handling as a photo or a
            # voice note, just below.

            if not message_text:
                # Only record the events that represent a customer going
                # unanswered. Instagram also sends read receipts, delivery
                # receipts and echoes of our own outbound messages, none of
                # which is anyone waiting for a reply — and there are enough of
                # them to fill the buffer in about a minute, burying the one
                # event that actually mattered.
                reason = _classify_unanswered_event(messaging_event)

                # Observation only, before either branch below: a reel shared
                # into a DM lands here, and what Meta puts inside that
                # attachment is the open question blocking shared-reel
                # routing. Recording it changes nothing about the reply.
                if reason == "attachment_only":
                    _record_attachment_debug_event(messaging_event, sender_id)

                # Parsed here because it is pure string work, but resolved and
                # stored in the background task below — the webhook must keep
                # returning 200 before Meta decides it timed out and redelivers.
                shared_reel = (
                    shared_reel_identity(messaging_event.get("message") or {})
                    if reason == "attachment_only" else None
                )
                shared_post_id = (
                    shared_post_media_id(messaging_event.get("message") or {})
                    if reason == "attachment_only" and not shared_reel else None
                )

                # A photo, voice note or sticker is a real person saying
                # something we cannot read. Silence is the worst available
                # answer — they cannot tell whether anyone is there. Asking
                # them to type costs one message and keeps the conversation
                # alive; it was found by the drop log within a minute of that
                # log existing.
                if reason == "attachment_only" and is_ad_qualified:
                    background_tasks.add_task(
                        _handle_shared_attachment, sender_id, shared_reel, shared_post_id,
                    )
                    results.append({
                        "sender_id": sender_id,
                        "action": "queued_attachment_reply",
                        "shared_reel_shortcode": (shared_reel or {}).get("shortcode"),
                        "shared_post_media_id": shared_post_id,
                    })
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

def _audit_recipient_id(recipient_id: str) -> str:
    """Return a stable, non-reversible identifier for outbound audit rows."""
    digest = hashlib.sha256(str(recipient_id).encode("utf-8")).hexdigest()[:20]
    return f"sha256:{digest}"


def _safe_outbound_exception(exc: Exception) -> str:
    """Keep exception summaries useful without persisting credentials."""
    return _safe_outbound_text(str(exc or ""))


def _safe_outbound_text(value: str) -> str:
    """Redact credential-shaped values from persisted diagnostic text."""
    summary = str(value or "")
    summary = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[REDACTED]", summary)
    summary = re.sub(r"(?i)(access_token|token|secret|password)=?[^\s,;]+", r"\1=[REDACTED]", summary)
    return summary[:500]


def _meta_error_fields(payload: Any) -> Tuple[Any, Any, Optional[str]]:
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None, None, None
    return (
        error.get("code"),
        error.get("error_subcode"),
        _safe_outbound_text(str(error.get("message") or "")) or None,
    )


_INSTAGRAM_PREFERRED_CHUNK_SIZE = 950
_INSTAGRAM_MAX_TEXT_CHARS = 1000


def _split_instagram_text(
    text: str,
    preferred_chunk_size: int = _INSTAGRAM_PREFERRED_CHUNK_SIZE,
) -> List[str]:
    """Split text at natural boundaries while preserving it byte-for-byte."""
    value = str(text or "")
    preferred = max(1, min(int(preferred_chunk_size or _INSTAGRAM_PREFERRED_CHUNK_SIZE), _INSTAGRAM_MAX_TEXT_CHARS))
    if len(value) <= preferred:
        return [value] if value else []

    chunks: List[str] = []
    while value:
        if len(value) <= preferred:
            chunks.append(value)
            break
        # Keep the chunk close to the preferred size. A boundary in the
        # latter half is preferable to a very short first message; if there
        # is no such boundary, the final fallback still avoids breaking a
        # word whenever possible.
        cut = _instagram_natural_split(value, preferred)
        chunks.append(value[:cut])
        value = value[cut:]
    return chunks


def _instagram_natural_split(value: str, limit: int) -> int:
    """Return a content-preserving split point at or before ``limit``."""
    minimum = max(1, limit // 2)

    def choose(candidate: int) -> Optional[int]:
        if candidate <= 0 or candidate > limit:
            return None
        return candidate if candidate >= minimum else None

    # Paragraph and line boundaries include their line break in the first
    # chunk, so joining chunks reproduces the original text exactly.
    for marker in ("\n\n", "\n"):
        position = value.rfind(marker, 0, limit + 1)
        candidate = choose(position + len(marker)) if position >= 0 else None
        if candidate:
            return candidate

    # A sentence boundary is punctuation followed by whitespace (or the end
    # of the complete value), never punctuation embedded in a number/table.
    sentence_candidate = None
    for index, character in enumerate(value[:limit]):
        if character not in ".?!":
            continue
        if index + 1 >= len(value) or value[index + 1].isspace():
            sentence_candidate = index + 1
    candidate = choose(sentence_candidate or 0)
    if candidate:
        return candidate

    whitespace_candidate = None
    for index, character in enumerate(value[:limit]):
        if character.isspace():
            whitespace_candidate = index + 1
    candidate = choose(whitespace_candidate or 0)
    if candidate:
        return candidate

    # Prefer a smaller natural boundary to splitting through a word when the
    # message has an unusually long opening block.
    for marker in ("\n\n", "\n"):
        position = value.rfind(marker, 0, limit + 1)
        if position >= 0 and 0 < position + len(marker) <= limit:
            return position + len(marker)
    if sentence_candidate:
        return sentence_candidate
    if whitespace_candidate:
        return whitespace_candidate
    return min(limit, _INSTAGRAM_MAX_TEXT_CHARS)


def send_instagram_reply(
    recipient_id: str,
    text: str,
    image_url=None,
    *,
    inbound_message_id: Optional[int] = None,
    response_type: str = "text",
    retry_count: int = 0,
) -> Dict[str, Any]:
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
    parent_send_id = uuid.uuid4().hex
    failed_chunk_number = None

    try:
        with httpx.Client(timeout=10.0) as client:
            last = None

            def send_request(
                body,
                *,
                kind: str,
                chunk_number: Optional[int] = None,
                total_chunks: Optional[int] = None,
                character_count: Optional[int] = None,
            ):
                nonlocal last
                audit_id = None
                try:
                    audit_id = db.create_instagram_outbound_audit(
                        inbound_message_id=inbound_message_id,
                        recipient_id=_audit_recipient_id(recipient_id),
                        response_type=kind or response_type,
                        retry_count=retry_count,
                        parent_send_id=parent_send_id,
                        chunk_number=chunk_number,
                        total_chunks=total_chunks,
                        character_count=character_count,
                    )
                except Exception as audit_error:
                    # Audit is observability only. A replaced/locked SQLite
                    # file must never prevent the real Meta send.
                    log.warning(
                        "Could not create Instagram outbound audit row; "
                        "continuing with Meta send: %s",
                        _safe_outbound_exception(audit_error),
                    )
                started = time.perf_counter()
                completed = False
                resp = None
                try:
                    resp = client.post(url, headers=headers, json=body)
                    last = resp
                    payload = resp.json()
                    code, subcode, message = _meta_error_fields(payload)
                    status_code = int(getattr(resp, "status_code", 200))
                    ok = 200 <= status_code < 300 and not (
                        isinstance(payload, dict) and payload.get("error")
                    )
                    message_id = payload.get("message_id") or payload.get("id") if isinstance(payload, dict) else None
                    payload_error = payload.get("error") if isinstance(payload, dict) else None
                    trace_id = (
                        getattr(resp, "headers", {}).get("x-fb-trace-id")
                        or (payload_error or {}).get("fbtrace_id")
                        or (payload.get("fbtrace_id") if isinstance(payload, dict) else None)
                    )
                    if audit_id is not None:
                        try:
                            db.complete_instagram_outbound_audit(
                                audit_id,
                                http_status=status_code, success=bool(ok),
                                meta_message_id=message_id, meta_error_code=code,
                                meta_error_subcode=subcode, meta_error_message=message,
                                meta_trace_id=trace_id,
                                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                            )
                        except Exception as audit_error:
                            log.warning(
                                "Could not complete Instagram outbound audit row %s: %s",
                                audit_id, _safe_outbound_exception(audit_error),
                            )
                    completed = True
                    if not ok:
                        log.error(
                            "Instagram Meta API send failed: HTTP %s code=%s subcode=%s "
                            "trace=%s message=%s",
                            status_code, code, subcode, trace_id, message,
                        )
                    if hasattr(resp, "raise_for_status"):
                        resp.raise_for_status()
                    if isinstance(payload, dict) and payload.get("error"):
                        raise RuntimeError(message or "Meta returned an error payload")
                except Exception as exc:
                    # If the HTTP call itself failed, the pending row still
                    # records the exception and elapsed time for diagnosis.
                    if not completed and audit_id is not None:
                        try:
                            db.complete_instagram_outbound_audit(
                                audit_id,
                                http_status=getattr(resp, "status_code", None) if resp is not None else None,
                                success=False,
                                meta_trace_id=getattr(resp, "headers", {}).get("x-fb-trace-id") if resp is not None else None,
                                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                                exception_class=type(exc).__name__,
                                exception_message=_safe_outbound_exception(exc),
                            )
                        except Exception as audit_error:
                            log.warning(
                                "Could not record Instagram outbound exception for audit row %s: %s",
                                audit_id, _safe_outbound_exception(audit_error),
                            )
                    raise

            text_groups = []
            if text and not cards_first:
                text_groups.append(_split_instagram_text(text))
            for _img, caption in captioned:
                if caption:
                    text_groups.append(_split_instagram_text(caption))
            if text and cards_first:
                text_groups.append(_split_instagram_text(text))
            total_text_chunks = sum(len(group) for group in text_groups)
            text_group_index = 0
            next_chunk_number = 0

            def send_text_body(body: str):
                nonlocal text_group_index, next_chunk_number, failed_chunk_number
                # The groups are precomputed in the same order as the sends
                # below, so every text chunk shares one parent sequence even
                # when images are interleaved between caption groups.
                chunks = text_groups[text_group_index]
                text_group_index += 1
                for chunk in chunks:
                    next_chunk_number += 1
                    try:
                        send_request(
                            {"recipient": {"id": recipient_id}, "message": {"text": chunk}},
                            kind="text",
                            chunk_number=next_chunk_number,
                            total_chunks=total_text_chunks,
                            character_count=len(chunk),
                        )
                    except Exception:
                        failed_chunk_number = next_chunk_number
                        raise

            if text and not cards_first:
                send_text_body(text)

            for img, caption in captioned:
                if caption and caption_first:
                    send_text_body(caption)
                # A rejected card must not cost the customer the whole reply.
                # Meta refuses images for reasons outside our control — an
                # unreachable host, an unsupported format, a size limit — and
                # letting that raise meant the customer got silence instead of
                # the answer we already had.
                try:
                    send_request({
                        "recipient": {"id": recipient_id},
                        "message": {"attachment": {"type": "image", "payload": {"url": img, "is_reusable": True}}}
                    }, kind="image")
                except Exception as img_error:
                    log.warning("Instagram rejected image card %s: %s", img, img_error)
                if caption and not caption_first:
                    send_text_body(caption)

            # The closing question follows the cards it refers to.
            if text and cards_first:
                send_text_body(text)

            try:
                db.finalize_instagram_outbound_audit(
                    parent_send_id, overall_success=True,
                )
            except Exception as audit_error:
                log.warning(
                    "Could not finalize Instagram outbound audit %s: %s",
                    parent_send_id, _safe_outbound_exception(audit_error),
                )
            delivery_monitor.record_success("instagram")
            return last.json() if last is not None else {"status": "no_content"}
    except Exception as e:
        try:
            db.finalize_instagram_outbound_audit(
                parent_send_id,
                overall_success=False,
                overall_failure_chunk=failed_chunk_number,
            )
        except Exception as audit_error:
            log.warning(
                "Could not finalize failed Instagram outbound audit %s: %s",
                parent_send_id, _safe_outbound_exception(audit_error),
            )
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


def _check_instagram_token(token: str, url: str, identity: str) -> Dict[str, Any]:
    """Perform one real Meta token check; isolated for health-test mocking."""
    try:
        with httpx.Client(timeout=_IG_TOKEN_TIMEOUT_SECONDS) as client:
            resp = client.get(url, params={"fields": "ice_breakers", "access_token": token})
        if resp.status_code == 200:
            return {"state": "valid", "identity": identity, "detail": "Meta accepted the token."}
        error = (resp.json() or {}).get("error", {}) if resp.content else {}
        code = error.get("code")
        state = "invalid" if code == 190 or error.get("type") == "OAuthException" else "unknown"
        return {
            "state": state,
            "identity": identity,
            "detail": error.get("message") or f"HTTP {resp.status_code}",
            "meta_code": code,
        }
    except Exception as e:
        return {
            "state": "unknown",
            "identity": identity,
            "detail": f"Could not reach Meta: {type(e).__name__}",
        }


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
    result = _check_instagram_token(token, url, identity)
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
    instagram_outbound = db.instagram_outbound_health()

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

    # AI Resolution & Answers, Phase 3 slice 2. Same reasoning as
    # router_shadow above: LLM_TRIAGE_ENABLED lives in the host dashboard,
    # invisible to anything in this repo, so this is the only way to confirm
    # a flip actually took without opening the admin dashboard.
    llm_triage: Dict[str, Any] = {
        "enabled": config.LLM_TRIAGE_ENABLED,
        "model": config.LLM_TRIAGE_MODEL if config.LLM_TRIAGE_ENABLED else None,
    }
    if config.LLM_TRIAGE_ENABLED:
        try:
            llm_triage.update(db.triage_shadow_pulse())
        except Exception as e:
            llm_triage["error"] = f"{type(e).__name__}"
            log.warning("Could not read the triage shadow pulse: %s", e)

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
            # Diagnostic only: no traffic is not an outage, so this block never
            # participates in the overall healthy/degraded calculation.
            "instagram_outbound": instagram_outbound,
            "escalation_alerts_to": alert_destination,
            "router_shadow": router_shadow,
            "llm_triage": llm_triage,
            "auto_resolve": {
                "simulation_enabled": config.LLM_AUTO_RESOLVE_SIMULATION_ENABLED,
                "actual_enabled": bool(config.LLM_AUTO_RESOLVE_ENABLED and db.get_auto_resolve_settings().get("kill_switch", True)),
                "min_confidence": config.LLM_AUTO_RESOLVE_MIN_CONFIDENCE,
                "readiness": db.auto_resolve_evaluation().get("readiness_status", "NOT READY"),
            },
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


@app.post("/admin/instagram-routes/identify")
async def identify_instagram_route(
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Normalize an operator-friendly post/reel URL without inventing metadata."""
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
        identity = normalize_instagram_url(body.get("url"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    existing = next((
        decode_route(row) for row in db.list_instagram_source_routes()
        if row.get("canonical_url") == identity["canonical_url"]
    ), None)
    lookup = lookup_instagram_media(identity)
    verified_media_id = lookup.get("verified_media_identifier") or (existing or {}).get("media_id")
    return {
        "status": "success",
        **identity,
        "verified_media_identifier": verified_media_id,
        "thumbnail_url": lookup.get("thumbnail_url"),
        "caption": lookup.get("caption"),
        "metadata_status": (
            lookup.get("metadata_status") if lookup.get("metadata_status") == "verified_graph_api"
            else "saved_mapping" if existing and existing.get("media_id")
            else lookup.get("metadata_status") or "not_retrievable_from_webhook"
        ),
    }


@app.get("/admin/instagram-routes")
def list_instagram_routes(
    include_archived: bool = False,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    if include_archived:
        # The Archived tab: only archived rows, never mixed with active ones.
        routes = [decode_route(row) for row in db.list_instagram_source_routes(include_archived=True) if row.get("archived_at")]
    else:
        routes = [decode_route(row) for row in db.list_instagram_source_routes()]
    return {
        "status": "success",
        "routes": routes,
        "legacy_ad_routes": db.list_ad_product_routes(),
        "product_catalogue": product_catalogue(),
        "knowledge_scopes": sorted(SOURCE_MAP),
        "inbound_identifiers": ["ad_id", "story_media_id"],
    }


@app.post("/admin/instagram-routes")
async def save_instagram_route(
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
        route = validate_route_payload(body)
        routing_id = body.get("routing_id")
        if routing_id is not None:
            routing_id = int(routing_id)
        saved = decode_route(db.save_instagram_source_route(route, routing_id))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "success", "route": saved}


@app.patch("/admin/instagram-routes/{routing_id}/active")
async def set_instagram_route_active(
    routing_id: int,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    body = await request.json()
    if not isinstance(body.get("active"), bool):
        raise HTTPException(status_code=400, detail="active must be true or false.")
    if not db.set_instagram_source_route_active(routing_id, body["active"]):
        raise HTTPException(status_code=404, detail="Routing record not found.")
    return {"status": "success", "routing_id": routing_id, "active": body["active"]}


@app.delete("/admin/instagram-routes/{routing_id}")
def archive_instagram_route(
    routing_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    if not db.archive_instagram_source_route(routing_id):
        raise HTTPException(status_code=404, detail="Routing record not found.")
    return {"status": "success", "routing_id": routing_id, "archived": True}


@app.post("/admin/instagram-routes/{routing_id}/restore")
def restore_instagram_route(
    routing_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Un-archives a route in place. Same row, same id — nothing duplicated."""
    require_admin(x_admin_key, x_admin_pin)
    if not db.restore_instagram_source_route(routing_id):
        raise HTTPException(status_code=404, detail="Archived routing record not found.")
    return {"status": "success", "routing_id": routing_id, "restored": True}


@app.delete("/admin/instagram-routes/{routing_id}/permanent")
def delete_instagram_route(
    routing_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Permanently deletes one routing configuration.

    Deliberately a separate, explicit path from the archive DELETE above —
    "Remove" and "Archive" are different actions with different consequences,
    and giving them different endpoints instead of a body flag makes that
    distinction obvious to anything calling this, not only to the admin UI.
    Scope is narrow: this route and the pairing attempts that exist only to
    serve it. Topics, menus, catalogue entries, knowledge, conversations and
    handoffs are never touched by removing a route.
    """
    require_admin(x_admin_key, x_admin_pin)
    if not db.delete_instagram_source_route(routing_id, removed_by=x_admin_key or "admin"):
        raise HTTPException(status_code=404, detail="Routing record not found.")
    return {"status": "success", "routing_id": routing_id, "removed": True}


# --------------------------------------------------------------------------
# Shared-post pairing. A shared post carries no shortcode/URL Meta will put
# in a webhook, and its messaging-attachment id has not been shown to
# correspond to anything Graph will verify — so a post route only gets a
# trustworthy media_id through an admin deliberately pairing one: start a
# short window, share the exact post into the business DM, and whatever id
# arrives during that window binds to the route. Binding is a one-shot
# write into instagram_source_routes.media_id; runtime matching then reuses
# the existing exact media_id lookup, no separate code path required.
# --------------------------------------------------------------------------

@app.post("/admin/instagram-routes/{routing_id}/pairing/start")
def start_instagram_post_pairing(
    routing_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    if not db.get_instagram_source_route(routing_id):
        raise HTTPException(status_code=404, detail="Routing record not found.")
    pairing = db.start_post_pairing(
        routing_id, config.INSTAGRAM_POST_PAIRING_TTL_SECONDS, started_by=x_admin_key or "admin",
    )
    return {"status": "success", "pairing": pairing, "ttl_seconds": config.INSTAGRAM_POST_PAIRING_TTL_SECONDS}


@app.get("/admin/instagram-routes/{routing_id}/pairing")
def get_instagram_post_pairing_status(
    routing_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    pairing = db.get_post_pairing_for_route(routing_id)
    return {"status": "success", "pairing": pairing}


@app.post("/admin/instagram-routes/{routing_id}/pairing/cancel")
def cancel_instagram_post_pairing(
    routing_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    cancelled = db.cancel_post_pairing(routing_id)
    return {"status": "success", "cancelled": cancelled}


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

@app.get("/admin/triage-shadow")
def get_triage_shadow_report(
    days: int = 7,
    limit: int = 40,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    AI Resolution & Answers, Phase 3 slice 2: how a triage model would have
    categorized every message the deterministic/canonical path could not
    answer, and what it would have needed to be grounded and confident
    enough to auto-resolve. Nothing here has ever been sent to a customer or
    changed a candidate/escalation/knowledge-gap row — it is a parallel
    measurement, read for review only.
    """
    require_admin(x_admin_key, x_admin_pin)
    report = db.triage_shadow_report(days=days, limit=limit)
    report["shadow_enabled"] = config.LLM_TRIAGE_ENABLED
    report["confidence_threshold"] = config.LLM_TRIAGE_CONFIDENCE_THRESHOLD
    if not config.LLM_TRIAGE_ENABLED and not report["classified"]:
        report["note"] = "LLM_TRIAGE_ENABLED is off, so nothing is being classified."
    return report


@app.get("/admin/assisted-resolutions")
def get_assisted_resolutions(status: Optional[str] = None, category: Optional[str] = None, limit: int = 100, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    status = status.strip().upper() if status else None; category = category.strip().upper() if category else None
    items = db.list_assisted_suggestions(status=status, category=category, limit=limit)
    return {"status": "success", "count": len(items), "suggestions": items, "metrics": db.assisted_metrics(), "evaluation": db.assisted_evaluation()}


@app.get("/admin/assisted-resolutions/metrics")
def get_assisted_resolution_metrics(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    return {"status": "success", "metrics": db.assisted_metrics(), "evaluation": db.assisted_evaluation()}


@app.get("/admin/assisted-resolutions/evaluation")
@app.get("/admin/assisted-resolutions/analytics")
def get_assisted_resolution_evaluation(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    return {"status": "success", "evaluation": db.assisted_evaluation(), "auto_resolve_enabled": bool(config.LLM_AUTO_RESOLVE_ENABLED), "auto_resolve_settings": db.get_auto_resolve_settings()}


@app.get("/admin/auto-resolve/evaluation")
def get_auto_resolve_evaluation(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """Authenticated, read-only simulation and readiness evidence."""
    require_admin(x_admin_key, x_admin_pin)
    return {"status": "success", "evaluation": db.auto_resolve_evaluation(), "settings": db.get_auto_resolve_settings(), "decisions": db.list_auto_resolve_decisions()}


@app.get("/admin/auto-resolve/settings")
def get_auto_resolve_settings(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    settings = db.get_auto_resolve_settings()
    settings["state_label"] = "AUTO RESOLVE: ON" if settings["actual_enabled"] else "AUTO RESOLVE: OFF"
    return {"status": "success", "settings": settings}


@app.post("/admin/auto-resolve/kill-switch")
async def set_auto_resolve_kill_switch(request: Request, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        body = {}
    enabled = bool(body.get("enabled", False))
    db.set_auto_resolve_setting("kill_switch", enabled)
    settings = db.get_auto_resolve_settings()
    return {"status": "success", "kill_switch": enabled, "settings": settings}


@app.post("/admin/auto-resolve/category-controls")
async def set_auto_resolve_category_controls(request: Request, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        body = {}
    controls = body.get("category_controls") if isinstance(body, dict) else None
    category_settings = body.get("category_settings") if isinstance(body, dict) else None
    if controls is not None:
        if not isinstance(controls, dict):
            raise HTTPException(status_code=400, detail="category_controls must be an object")
        # Real category activation is intentionally blocked while the global
        # env switch is false. Operators may still save a pilot plan for later.
        normalized = {str(k).strip().upper(): bool(v) for k, v in controls.items() if str(k).strip()}
        db.set_auto_resolve_setting("category_controls", normalized)
    if category_settings is not None:
        if not isinstance(category_settings, dict):
            raise HTTPException(status_code=400, detail="category_settings must be an object")
        db.set_auto_resolve_setting("category_settings", category_settings)
    return {"status": "success", "settings": db.get_auto_resolve_settings(), "actual_send_enabled": bool(config.LLM_AUTO_RESOLVE_ENABLED)}


def _assisted_reviewer(x_admin_key: Optional[str], x_admin_pin: Optional[str]) -> str:
    return (x_admin_key or x_admin_pin or "admin")[:120]


def _send_assisted_answer(suggestion: Dict[str, Any], answer: str) -> None:
    if str(suggestion.get("channel")).lower() != "instagram":
        raise HTTPException(status_code=400, detail="Assisted replies are Instagram-only.")
    if not answer.strip():
        raise HTTPException(status_code=400, detail="A final answer is required.")
    send_instagram_reply(
        str(suggestion["user_id"]), answer.strip(),
        inbound_message_id=suggestion.get("conversation_id"),
        response_type="assisted_answer",
    )


@app.post("/admin/assisted-resolutions/{suggestion_id}/approve")
async def approve_assisted_resolution(suggestion_id: int, request: Request, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    suggestion = db.get_assisted_suggestion(suggestion_id)
    if not suggestion or suggestion.get("status") != "PENDING_REVIEW": raise HTTPException(status_code=404, detail="Pending assisted suggestion not found.")
    if suggestion.get("triage_category") in {"HUMAN_REQUIRED", "CONTACT_DETAIL", "TEACH_REQUIRED"}: raise HTTPException(status_code=400, detail="This suggestion requires human handling before resolution.")
    try: body = await request.json()
    except Exception: body = {}
    final_answer = str(body.get("final_answer") or suggestion.get("suggested_answer") or "").strip()
    _send_assisted_answer(suggestion, final_answer)
    edited = final_answer != str(suggestion.get("original_suggested_answer") or "").strip(); status = "EDITED_AND_APPROVED" if edited else "APPROVED"
    if not db.review_assisted_suggestion(suggestion_id, status=status, reviewed_by=_assisted_reviewer(x_admin_key, x_admin_pin), final_answer=final_answer, resolution_action=status): raise HTTPException(status_code=409, detail="Suggestion was already reviewed.")
    if edited:
        db.record_assisted_edit_metrics(suggestion_id, str(suggestion.get("original_suggested_answer") or ""), final_answer)
    kid = str(suggestion.get("matched_knowledge_id") or "")
    if kid.startswith("learned_knowledge#"):
        try: db.increment_learned_knowledge_usage(int(kid.rsplit("#", 1)[1]))
        except (TypeError, ValueError): pass
    return {"status": status, "suggestion_id": suggestion_id, "final_answer": final_answer}


@app.post("/admin/assisted-resolutions/{suggestion_id}/edit-resolve")
async def edit_assisted_resolution(suggestion_id: int, request: Request, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    try: body = await request.json()
    except Exception: body = {}
    if not str(body.get("final_answer") or "").strip(): raise HTTPException(status_code=400, detail="final_answer is required for edit-and-resolve.")
    return await approve_assisted_resolution(suggestion_id, request, x_admin_key, x_admin_pin)


@app.post("/admin/assisted-resolutions/{suggestion_id}/reject")
async def reject_assisted_resolution(suggestion_id: int, request: Request, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try: body = await request.json()
    except Exception: body = {}
    reason_code = str(body.get("reason_code") or body.get("reason") or "Other").strip()[:80]
    notes = str(body.get("notes") or "").strip()[:400]
    rejection_reason = f"{reason_code}: {notes}" if notes else reason_code
    if not db.review_assisted_suggestion(suggestion_id, status="REJECTED", reviewed_by=_assisted_reviewer(x_admin_key, x_admin_pin), resolution_action="REJECTED", rejection_reason=rejection_reason): raise HTTPException(status_code=404, detail="Pending assisted suggestion not found.")
    return {"status": "REJECTED", "suggestion_id": suggestion_id}


@app.post("/admin/assisted-resolutions/{suggestion_id}/send-to-human")
def send_assisted_to_human(suggestion_id: int, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin); suggestion = db.get_assisted_suggestion(suggestion_id)
    if not suggestion or suggestion.get("status") != "PENDING_REVIEW": raise HTTPException(status_code=404, detail="Pending assisted suggestion not found.")
    escalator.handle_escalation(user_id=str(suggestion["user_id"]), channel="instagram", user_message=str(suggestion["customer_message"]), reason="Assisted Resolution sent to human", context_data={"suggestion_id": suggestion_id})
    db.review_assisted_suggestion(suggestion_id, status="SENT_TO_HUMAN", reviewed_by=_assisted_reviewer(x_admin_key, x_admin_pin), resolution_action="SENT_TO_HUMAN")
    return {"status": "SENT_TO_HUMAN", "suggestion_id": suggestion_id}


@app.post("/admin/assisted-resolutions/{suggestion_id}/teach-and-resolve")
async def teach_and_resolve_assisted(suggestion_id: int, request: Request, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin); suggestion = db.get_assisted_suggestion(suggestion_id)
    if not suggestion or suggestion.get("status") != "PENDING_REVIEW": raise HTTPException(status_code=404, detail="Pending assisted suggestion not found.")
    try: body = await request.json()
    except Exception: body = {}
    answer = str(body.get("verified_answer") or "").strip()
    if not answer: raise HTTPException(status_code=400, detail="verified_answer is required.")
    kid = db.add_learned_knowledge(str(body.get("topic") or suggestion["customer_message"][:80]), str(body.get("question_pattern") or suggestion["customer_message"]), answer, source="assisted_resolution")
    _send_assisted_answer(suggestion, answer); db.review_assisted_suggestion(suggestion_id, status="RESOLVED", reviewed_by=_assisted_reviewer(x_admin_key, x_admin_pin), final_answer=answer, resolution_action="TEACH_AND_RESOLVE")
    return {"status": "RESOLVED", "suggestion_id": suggestion_id, "learned_knowledge_id": kid}


@app.post("/admin/assisted-resolutions/{suggestion_id}/dismiss")
def dismiss_assisted_resolution(suggestion_id: int, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    if not db.review_assisted_suggestion(suggestion_id, status="REJECTED", reviewed_by=_assisted_reviewer(x_admin_key, x_admin_pin), resolution_action="DISMISSED", rejection_reason="Non-actionable"): raise HTTPException(status_code=404, detail="Pending assisted suggestion not found.")
    return {"status": "DISMISSED", "suggestion_id": suggestion_id}

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


@app.get("/admin/instagram-outbound-audit")
def get_instagram_outbound_audit(
    limit: int = 50,
    success: Optional[bool] = None,
    inbound_message_id: Optional[int] = None,
    since: Optional[str] = None,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Authenticated, read-only Meta send diagnostics."""
    require_admin(x_admin_key, x_admin_pin)
    audits = db.get_instagram_outbound_audit(
        limit=limit,
        success=success,
        inbound_message_id=inbound_message_id,
        since=since,
    )
    return {"count": len(audits), "audits": audits}

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

SOURCE_LABELS = {
    "system_template": "System Template",
    "client_admin": "Created by Client",
    "staff_resolution": "Teach & Resolve",
    "staff_dashboard": "Staff Resolution",
    "csv_import": "Imported",
}


def _source_label(source: Optional[str]) -> str:
    """
    Cryptic DB source strings never reach a client as the primary label.
    Mapped by what actually writes each one, not by string similarity —
    `staff_resolution` is the Teach & Resolve producer's literal source
    value, `staff_dashboard` is the older manual-entry form's.
    """
    source = source or ""
    if source in SOURCE_LABELS:
        return SOURCE_LABELS[source]
    if source.startswith("approved_candidate"):
        return "AI Approved"
    return source.replace("_", " ").title() if source else "Unknown"


def _custom_qa_public(item: Dict[str, Any]) -> Dict[str, Any]:
    """Shapes a learned_knowledge row for the Custom Q&A tab — no bare DB id up front."""
    result = {
        "id": item.get("id"),
        "topic": item.get("topic"),
        "primary_question": item.get("question_pattern"),
        "aliases": item.get("aliases", []),
        "approved_answer": item.get("verified_answer"),
        "priority": item.get("priority") or "NORMAL",
        "status": item.get("status") or "ACTIVE",
        "source": item.get("source"),
        "source_label": _source_label(item.get("source")),
        "usage_count": item.get("usage_count") or 0,
        "last_used_at": item.get("last_used_at"),
        "created_at": item.get("created_at"),
        "created_by": item.get("created_by"),
        "updated_at": item.get("updated_at"),
        "updated_by": item.get("updated_by"),
    }
    try:
        result["auto_resolve_health"] = db.knowledge_health(int(item["id"]))
    except (TypeError, ValueError):
        result["auto_resolve_health"] = {"health": "Review Recommended"}
    return result


def _system_template_public(template: Dict[str, Any], override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Shape protected JSON template logic as a Response Library card.

    The template's triggers and escalation behavior stay read-only.  Only the
    response copy may be overridden through the managed response store.
    """
    template_id = str(template.get("id") or "")
    responses = template.get("responses") or []
    answer = (override or {}).get("response") or template.get("response") or (responses[0] if responses else "")
    # Keep every shipped greeting variant searchable while presenting one
    # canonical answer.  This lets an operator find legacy copy by its exact
    # phrase without creating a duplicate Response Library row.
    response_variants = list(responses) if responses else [answer]
    if override and override.get("response"):
        response_variants.insert(0, str(override["response"]))
    trigger_list = [str(t).strip() for t in (template.get("triggers") or []) if str(t).strip()]
    title = str(template.get("name") or template_id.replace("_", " ").title())
    category = str(template.get("category") or "System")
    status = str((override or {}).get("status") or "ACTIVE").upper()
    return {
        "library_id": f"system:{template_id}",
        "kind": "system_template",
        "template_id": template_id,
        "title": title,
        "topic": category,
        "primary_question": trigger_list[0] if trigger_list else title,
        "aliases": trigger_list[1:],
        "triggers": trigger_list,
        "approved_answer": answer,
        "response_variants": response_variants,
        "source": "system_template",
        "source_label": "System Template",
        "status": status,
        "priority": "PROTECTED",
        "editable": bool(answer) and not bool(template.get("escalate")),
        "protected_runtime": True,
        "created_at": None,
        "updated_at": (override or {}).get("updated_at"),
        "updated_by": (override or {}).get("updated_by"),
        "usage_count": None,
        "last_used_at": None,
    }


def _response_library_search_score(query: Optional[str], item: Dict[str, Any]) -> int:
    """Rank library discovery fields without matching character substrings."""
    needle = " ".join(str(query or "").casefold().split())
    if not needle:
        return 1
    query_tokens = re.findall(r"[\w]+", needle, flags=re.UNICODE)
    if not query_tokens:
        return 0
    # One- and two-character searches are intentionally strict: they may
    # identify a trigger/title token, but never scan answer paragraphs or use
    # fuzzy matching. This prevents `hi` from matching `this`/`which`.
    short = len(needle.replace(" ", "")) <= 2
    search_synonyms = {
        "sweets": ["sondesh", "kanchagolla", "jolbhora"],
        "shipping": ["delivery"],
        "cost": ["price"],
        "rate": ["price"],
        "menu": ["weekly menu", "meal menu"],
    }
    expanded_tokens = query_tokens + [
        token for synonym in search_synonyms.get(needle, [])
        for token in re.findall(r"[\w]+", synonym.casefold())
    ]
    fields = {
        "primary": [item.get("primary_question")],
        "alias": list(item.get("aliases") or []),
        "title": [item.get("title")],
        "id_category": [item.get("template_id"), item.get("category"), item.get("topic")],
        "answer": [item.get("approved_answer"), *(item.get("response_variants") or [])],
    }
    def normalized(value: Any) -> str:
        return " ".join(str(value or "").casefold().split())
    def token_set(values: List[Any]) -> set:
        return {token for value in values for token in re.findall(r"[\w]+", normalized(value), flags=re.UNICODE)}
    def prefix_match(values: List[Any]) -> bool:
        words = token_set(values)
        if short and len(needle.replace(" ", "")) > 1:
            return any(query_token and query_token in words for query_token in expanded_tokens)
        return any(query_token and any(word.startswith(query_token) for word in words) for query_token in expanded_tokens)
    def short_social_prefix(values: List[Any]) -> bool:
        return short and any(word in {"hi", "hello", "hey"} for word in token_set(values))
    def fuzzy_match(values: List[Any]) -> bool:
        if short:
            return False
        words = token_set(values)
        return all(
            any(difflib.SequenceMatcher(None, query_token, word).ratio() >= 0.75 for word in words)
            for query_token in query_tokens
        )

    # Primary question is the first trigger; additional triggers are aliases.
    if any(normalized(value) == needle for value in fields["primary"]):
        return 100
    if any(normalized(value) == needle for value in fields["alias"]):
        return 95
    if any(normalized(value) == needle for value in fields["title"]):
        return 90
    if any(normalized(value) == needle for value in fields["id_category"]):
        return 85
    # An exact customer-facing phrase is a strong diagnostic match (including
    # a legacy variant retained on the same protected system-template card),
    # but remains below exact trigger/title matches.
    if any(normalized(value) == needle for value in fields["answer"]):
        return 84
    if len(query_tokens) > 1 and any(
        re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", normalized(value))
        for value in fields["answer"]
    ):
        return 82
    if short_social_prefix(fields["primary"]):
        return 92
    if short_social_prefix(fields["alias"]):
        return 91
    if prefix_match(fields["primary"]):
        return 80
    if prefix_match(fields["alias"]):
        return 76
    if prefix_match(fields["title"]):
        return 72
    if prefix_match(fields["id_category"]):
        return 68
    if set(query_tokens) & token_set(fields["primary"]):
        return 80
    if set(query_tokens) & token_set(fields["alias"]):
        return 75
    if set(query_tokens) & token_set(fields["title"]):
        return 70
    if set(query_tokens) & token_set(fields["id_category"]):
        return 65
    if fuzzy_match(fields["primary"]) or fuzzy_match(fields["alias"]):
        return 60
    if fuzzy_match(fields["title"]):
        return 55
    if not short and set(query_tokens) & token_set(fields["answer"]):
        return 40
    return 0


def _response_library_system_items(status: str = "ALL", search: Optional[str] = None) -> List[Dict[str, Any]]:
    overrides = db.list_response_template_overrides()
    items = []
    for template in (responder.qa_data or {}).get("templates", []):
        item = _system_template_public(template, overrides.get(str(template.get("id"))))
        if status and status.upper() != "ALL" and item["status"] != status.upper():
            continue
        score = _response_library_search_score(search, item)
        if search and not score:
            continue
        item["relevance_score"] = score
        items.append(item)
    return sorted(items, key=lambda item: (-int(item.get("relevance_score") or 0), item.get("title") or ""))


def _system_trigger_conflicts(primary_question: str, aliases: List[str]) -> List[Dict[str, Any]]:
    """Find exact trigger collisions without copying system templates into DB."""
    candidates = {str(value or "").strip().lower() for value in [primary_question, *(aliases or [])] if str(value or "").strip()}
    conflicts = []
    for template in (responder.qa_data or {}).get("templates", []):
        override = db.get_response_template_override(str(template.get("id")))
        if override and str(override.get("status") or "ACTIVE").upper() != "ACTIVE":
            continue
        triggers = {str(value or "").strip().lower() for value in (template.get("triggers") or [])}
        if candidates & triggers:
            conflicts.append(_system_template_public(template))
    return conflicts


@app.get("/admin/response-library")
def list_response_library(
    status: str = "ACTIVE",
    source: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 500,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Unified read-only view of protected templates plus managed Q&A rows."""
    require_admin(x_admin_key, x_admin_pin)
    limit = max(1, min(limit, 1000))
    items: List[Dict[str, Any]] = []
    if not source or source in {"system_template", "system"}:
        items.extend(_response_library_system_items(status=status, search=search))
    if not source or source not in {"system_template", "system"}:
        learned_status = status if status else "ALL"
        learned = db.list_custom_qa(status=learned_status, source=None if source in {"system_template", "system"} else source, topic=category, search=None, limit=limit)
        for item in learned:
            shaped = _custom_qa_public(item)
            shaped.update({"library_id": f"custom:{item.get('id')}", "kind": "custom_qa", "title": item.get("topic") or item.get("question_pattern"), "category": item.get("topic")})
            score = _response_library_search_score(search, shaped)
            if not search or score:
                shaped["relevance_score"] = score
                items.append(shaped)
    if category and (not source or source in {"system_template", "system"}):
        items = [item for item in items if str(item.get("topic") or item.get("category") or "").lower() == category.lower()]
    items.sort(key=lambda item: (-int(item.get("relevance_score") or 0), item.get("title") or item.get("topic") or ""))
    # A one-character type-ahead query is a suggestion surface, not a full
    # result dump. Keep the API response small so the client remains instant.
    if search and len("".join(str(search).split())) <= 1:
        items = [item for item in items if int(item.get("relevance_score") or 0) >= 90][:8]
    return {"status": "success", "count": len(items[:limit]), "items": items[:limit]}


@app.patch("/admin/response-library/system/{template_id}")
async def edit_response_library_system_template(
    template_id: str,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Edit customer-facing copy only; protected trigger/runtime fields cannot be changed."""
    require_admin(x_admin_key, x_admin_pin)
    template = next((t for t in (responder.qa_data or {}).get("templates", []) if t.get("id") == template_id), None)
    if not template:
        raise HTTPException(status_code=404, detail="System template not found")
    if template.get("escalate") or not (template.get("response") or template.get("responses")):
        raise HTTPException(status_code=400, detail="This system template has no editable customer-facing response")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    response = str(body.get("approved_answer") or body.get("response") or "").strip()
    actor = body.get("updated_by") or "client_admin_dashboard"
    if not response:
        raise HTTPException(status_code=400, detail="response is required")
    _validate_custom_qa_answer(response)
    status = str(body.get("status") or "ACTIVE").upper()
    default_response = template.get("response") or ((template.get("responses") or [""])[0])
    if not db.save_response_template_override(template_id, response, actor, status=status, previous_response=default_response):
        raise HTTPException(status_code=400, detail="Could not save response override")
    return {"status": "success", "item": _system_template_public(template, db.get_response_template_override(template_id))}


@app.get("/admin/response-library/system/{template_id}/versions")
def get_response_library_system_versions(
    template_id: str,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    return {"status": "success", "versions": db.get_response_template_override_versions(template_id)}


@app.post("/admin/response-library/system/{template_id}/preview")
def preview_response_library_system_template(
    template_id: str,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    template = next((t for t in (responder.qa_data or {}).get("templates", []) if t.get("id") == template_id), None)
    if not template:
        raise HTTPException(status_code=404, detail="System template not found")
    preview_text, _images = responder.render_approved_template(template_id, channel="instagram")
    return {"preview_text": preview_text, "template_id": template_id, "note": "Preview only — nothing has been sent."}


@app.post("/admin/response-library/system/{template_id}/restore/{version_id:int}")
async def restore_response_library_system_version(
    template_id: str,
    version_id: int,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        body = {}
    actor = body.get("actor") or "client_admin_dashboard"
    if not db.restore_response_template_override_version(template_id, version_id, actor):
        raise HTTPException(status_code=404, detail="Version or system template not found")
    return {"status": "restored", "template_id": template_id, "version_id": version_id}


@app.post("/admin/response-library/test")
async def test_response_library_match(
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Dry-run a real deterministic response without session/send/usage side effects."""
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    message = str(body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    winner, matched_via, score = db._rank_learned_knowledge(message)
    if winner:
        item = _custom_qa_public(winner)
        return {
            "matched": True, "template_id": None, "source": item["source_label"],
            "score": round(score, 3), "matched_via": matched_via,
            "response": item["approved_answer"], "source_context_used": False,
        }
    normalized = " ".join(message.lower().split())
    candidates = []
    for template in (responder.qa_data or {}).get("templates", []):
        triggers = [str(trigger).lower().strip() for trigger in (template.get("triggers") or [])]
        matched_trigger = next((trigger for trigger in sorted(triggers, key=len, reverse=True) if trigger and trigger in normalized), None)
        if matched_trigger:
            item = _system_template_public(template, db.get_response_template_override(str(template.get("id"))))
            candidates.append((len(matched_trigger), item, matched_trigger))
    if not candidates:
        return {"matched": False, "template_id": None, "source": None, "score": 0.0, "response": "", "source_context_used": False}
    _length, item, matched_trigger = max(candidates, key=lambda candidate: candidate[0])
    preview_response, _images = responder.render_approved_template(
        item["template_id"], channel="instagram"
    )
    return {
        "matched": True, "template_id": item["template_id"], "source": item["source_label"],
        "score": 1.0, "matched_via": matched_trigger,
        "response": preview_response, "source_context_used": False,
    }


@app.get("/admin/custom-qa")
def list_custom_qa(
    status: str = "ACTIVE",
    source: Optional[str] = None,
    priority: Optional[str] = None,
    topic: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 500,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """The Custom Q&A tab's list — the same learned_knowledge rows AI Resolution & Answers' Approved Answers filter reads, with aliases attached."""
    require_admin(x_admin_key, x_admin_pin)
    items = db.list_custom_qa(status=status, source=source, priority=priority, topic=topic, search=None, limit=limit)
    shaped_items = []
    for item in items:
        shaped = _custom_qa_public(item)
        score = _response_library_search_score(search, {**shaped, "title": item.get("topic"), "category": item.get("topic")})
        if not search or score:
            shaped["relevance_score"] = score
            shaped_items.append(shaped)
    return {
        "status": "success", "count": len(shaped_items), "items": shaped_items,
        # Kept as a separate field for backwards compatibility: existing
        # Custom Q&A consumers still receive only learned rows in `items`,
        # while the tab can show the same protected system templates through
        # the unified Response Library view without duplicating database rows.
        "system_templates": (
            _response_library_system_items(status=status, search=search)
            if not source or source in {"system_template", "system"} else []
        ),
    }


@app.get("/admin/custom-qa/{knowledge_id:int}")
def get_custom_qa_detail(
    knowledge_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    item = db.get_custom_qa(knowledge_id)
    if not item:
        raise HTTPException(status_code=404, detail="Custom Q&A not found")
    return _custom_qa_public(item)


def _validate_custom_qa_answer(answer: str) -> None:
    valid, error = validate_candidate_answer(answer)
    if not valid:
        raise HTTPException(status_code=400, detail=error)


@app.post("/admin/custom-qa")
async def create_custom_qa(
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Creates a new Custom Q&A — a plain learned_knowledge row (source
    defaults to client_admin) plus its aliases and a first version snapshot.
    Exact duplicates always block; near-duplicates and business-commitment
    content only warn, and only until force_save resubmits the same body.
    """
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    topic = (body.get("topic") or "").strip()
    primary_question = (body.get("primary_question") or "").strip()
    answer = (body.get("approved_answer") or "").strip()
    aliases = [a for a in (body.get("aliases") or []) if (a or "").strip()]
    priority = (body.get("priority") or "NORMAL").upper()
    status = (body.get("status") or "ACTIVE").upper()
    created_by = body.get("created_by") or "client_admin_dashboard"
    force_save = bool(body.get("force_save"))

    if not topic or not primary_question or not answer:
        raise HTTPException(status_code=400, detail="topic, primary_question, and approved_answer are required")
    if priority not in {"NORMAL", "HIGH"}:
        raise HTTPException(status_code=400, detail="priority must be NORMAL or HIGH")
    if status not in {"ACTIVE", "DISABLED", "ARCHIVED"}:
        raise HTTPException(status_code=400, detail="status must be ACTIVE, DISABLED, or ARCHIVED")

    _validate_custom_qa_answer(answer)

    dupes = db.find_learned_knowledge_duplicates(primary_question, aliases)
    if dupes["exact"]:
        raise HTTPException(status_code=409, detail={
            "message": "An identical question or alias already exists. Edit the existing Q&A instead of creating a duplicate.",
            "existing": [_custom_qa_public(i) for i in dupes["exact"]],
        })
    system_conflicts = _system_trigger_conflicts(primary_question, aliases)
    if system_conflicts:
        raise HTTPException(status_code=409, detail={
            "message": "Another active response already matches this trigger. Edit the existing response instead of creating an ambiguous match.",
            "existing": system_conflicts,
        })

    warnings = detect_business_commitment_warnings(answer)
    if (dupes["similar"] or warnings) and not force_save:
        return {
            "status": "warning",
            "similar": [_custom_qa_public(i) for i in dupes["similar"]],
            "commitment_warnings": warnings,
        }

    knowledge_id = db.add_learned_knowledge(
        topic, primary_question, answer, source="client_admin", confidence=1.0,
        priority=priority, status=status, created_by=created_by, aliases=aliases,
    )
    return {"status": "success", "id": knowledge_id}


@app.patch("/admin/custom-qa/{knowledge_id:int}")
async def edit_custom_qa(
    knowledge_id: int,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Edits the same learned_knowledge record in place; the prior state is versioned first."""
    require_admin(x_admin_key, x_admin_pin)
    existing = db.get_custom_qa(knowledge_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Custom Q&A not found")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    topic = (body.get("topic") or existing.get("topic") or "").strip()
    primary_question = (body.get("primary_question") or existing.get("question_pattern") or "").strip()
    answer = (body.get("approved_answer") or existing.get("verified_answer") or "").strip()
    aliases = body.get("aliases")
    if aliases is None:
        aliases = [a["alias_text"] for a in existing.get("aliases", [])]
    aliases = [a for a in aliases if (a or "").strip()]
    priority = (body.get("priority") or existing.get("priority") or "NORMAL").upper()
    status = (body.get("status") or existing.get("status") or "ACTIVE").upper()
    updated_by = body.get("updated_by") or "client_admin_dashboard"
    force_save = bool(body.get("force_save"))

    if priority not in {"NORMAL", "HIGH"}:
        raise HTTPException(status_code=400, detail="priority must be NORMAL or HIGH")
    if status not in {"ACTIVE", "DISABLED", "ARCHIVED"}:
        raise HTTPException(status_code=400, detail="status must be ACTIVE, DISABLED, or ARCHIVED")

    _validate_custom_qa_answer(answer)

    dupes = db.find_learned_knowledge_duplicates(primary_question, aliases, exclude_id=knowledge_id)
    if dupes["exact"]:
        raise HTTPException(status_code=409, detail={
            "message": "An identical question or alias already exists on a different Q&A.",
            "existing": [_custom_qa_public(i) for i in dupes["exact"]],
        })
    system_conflicts = _system_trigger_conflicts(primary_question, aliases)
    if system_conflicts:
        raise HTTPException(status_code=409, detail={
            "message": "Another active response already matches this trigger. Edit the existing response instead of creating an ambiguous match.",
            "existing": system_conflicts,
        })

    warnings = detect_business_commitment_warnings(answer)
    if (dupes["similar"] or warnings) and not force_save:
        return {
            "status": "warning",
            "similar": [_custom_qa_public(i) for i in dupes["similar"]],
            "commitment_warnings": warnings,
        }

    db.update_custom_qa(knowledge_id, topic, primary_question, answer, aliases, priority, status, updated_by)
    return {"status": "success", "id": knowledge_id}


@app.post("/admin/custom-qa/{knowledge_id:int}/status")
async def set_custom_qa_status(
    knowledge_id: int,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Disable / enable / archive / reactivate — status only, versioned like any other edit."""
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        body = {}
    status = str(body.get("status", "")).strip().upper()
    actor = body.get("actor") or "client_admin_dashboard"
    if not db.set_custom_qa_status(knowledge_id, status, actor):
        raise HTTPException(status_code=400, detail="Could not update status (bad status value or Q&A not found)")
    return {"status": status, "id": knowledge_id}


@app.delete("/admin/custom-qa/{knowledge_id:int}")
def delete_custom_qa(
    knowledge_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Permanent delete — only this Q&A and its own aliases/version history."""
    require_admin(x_admin_key, x_admin_pin)
    if not db.delete_learned_knowledge(knowledge_id):
        raise HTTPException(status_code=404, detail="Custom Q&A not found")
    return {"status": "deleted", "id": knowledge_id}


@app.get("/admin/custom-qa/{knowledge_id:int}/versions")
def get_custom_qa_versions(
    knowledge_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    return {"status": "success", "versions": db.get_learned_knowledge_versions(knowledge_id)}


@app.post("/admin/custom-qa/{knowledge_id:int}/restore/{version_id:int}")
async def restore_custom_qa_version(
    knowledge_id: int,
    version_id: int,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        body = {}
    actor = body.get("actor") or "client_admin_dashboard"
    if not db.restore_learned_knowledge_version(knowledge_id, version_id, actor):
        raise HTTPException(status_code=404, detail="Version or Q&A not found")
    return {"status": "restored", "id": knowledge_id, "version_id": version_id}


@app.delete("/admin/custom-qa/aliases/{alias_id}")
def delete_custom_qa_alias(
    alias_id: int,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Removes one alias only — the owning Q&A is untouched."""
    require_admin(x_admin_key, x_admin_pin)
    if not db.remove_learned_knowledge_alias(alias_id):
        raise HTTPException(status_code=404, detail="Alias not found")
    return {"status": "deleted", "alias_id": alias_id}


@app.post("/admin/custom-qa/test")
async def test_custom_qa_match(
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Dry run only: matches a sample message against live Custom Q&A the same
    way a real customer message would, without sending anything, writing
    conversation history, creating an escalation/candidate, incrementing
    usage, or touching session state.
    """
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    winner, matched_via, score = db._rank_learned_knowledge(message)
    focus_id = body.get("focus_id")

    if not winner:
        return {
            "would_match": False, "matched": None, "matched_via": None, "score": 0.0,
            "would_win": focus_id is None,
        }

    result = {
        "would_match": True,
        "matched": _custom_qa_public(winner),
        "matched_via": matched_via,
        "score": round(score, 3),
        "ambiguous": 0.35 <= score < 0.55,
    }
    if focus_id is not None:
        would_win = int(winner.get("id")) == int(focus_id)
        result["would_win"] = would_win
        if not would_win:
            result["winning_qa"] = _custom_qa_public(winner)
            result["reason"] = (
                f"Another Q&A already matches this question with a {'higher or equal' if score >= 0.4 else 'comparable'} "
                f"score via {matched_via.lower() if matched_via else 'a broader match'} — "
                "edit that one, or make this question/alias more specific."
            )
    else:
        result["would_win"] = True
    return result


@app.post("/admin/custom-qa/preview")
async def preview_custom_qa_reply(
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Shows the exact text a customer would receive — learned_knowledge
    answers are sent verbatim on the live path (camellia/responder.py's
    `_conversation_request`), so the preview is that same text with no
    reformatting, and nothing is ever sent.
    """
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    knowledge_id = body.get("id")
    answer = body.get("approved_answer")
    if answer is None and knowledge_id is not None:
        item = db.get_custom_qa(int(knowledge_id))
        if not item:
            raise HTTPException(status_code=404, detail="Custom Q&A not found")
        answer = item.get("verified_answer")
    if answer is None:
        raise HTTPException(status_code=400, detail="Provide either id or approved_answer")

    return {"preview_text": answer, "note": "Preview only — nothing has been sent."}


_CSV_COLUMNS = [
    "id", "topic", "primary_question", "aliases", "approved_answer",
    "priority", "status", "source", "usage_count", "last_used_at",
    "created_at", "updated_at",
]


def _custom_qa_to_csv_row(item: Dict[str, Any]) -> Dict[str, Any]:
    public = _custom_qa_public(item)
    return {
        "id": public["id"],
        "topic": public["topic"],
        "primary_question": public["primary_question"],
        "aliases": json.dumps(public["aliases"]),
        "approved_answer": public["approved_answer"],
        "priority": public["priority"],
        "status": public["status"],
        "source": public["source"],
        "usage_count": public["usage_count"],
        "last_used_at": public["last_used_at"] or "",
        "created_at": public["created_at"] or "",
        "updated_at": public["updated_at"] or "",
    }


@app.get("/admin/custom-qa/export.csv")
def export_custom_qa_csv(
    status: str = "ALL",
    source: Optional[str] = None,
    priority: Optional[str] = None,
    topic: Optional[str] = None,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Admin-only CSV export. Columns are stable and never include secrets —
    they are drawn from the same public shaping (_custom_qa_public) the
    JSON list endpoint uses, which never surfaces a raw credential field.
    """
    require_admin(x_admin_key, x_admin_pin)
    items = db.list_custom_qa(status=status, source=source, priority=priority, topic=topic, limit=5000)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_COLUMNS)
    writer.writeheader()
    for item in items:
        writer.writerow(_custom_qa_to_csv_row(item))
    return PlainTextResponse(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=custom_qa_export.csv"},
    )


@app.get("/admin/custom-qa/csv-template")
def custom_qa_csv_template(
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["topic", "primary_question", "aliases", "approved_answer", "priority", "status"])
    writer.writeheader()
    writer.writerow({
        "topic": "Delivery",
        "primary_question": "Do you deliver outside Delhi NCR?",
        "aliases": json.dumps(["deliver outside ncr", "ship outside delhi"]),
        "approved_answer": "We currently deliver only within Delhi NCR.",
        "priority": "NORMAL",
        "status": "ACTIVE",
    })
    return PlainTextResponse(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=custom_qa_template.csv"},
    )


def _parse_custom_qa_csv(raw_text: str) -> List[Dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(raw_text))
    rows = []
    for i, row in enumerate(reader, start=1):
        rows.append({"row_number": i, **{k: (v or "").strip() for k, v in row.items()}})
    return rows


def _validate_import_row(row: Dict[str, Any]) -> Dict[str, Any]:
    topic = row.get("topic", "")
    primary_question = row.get("primary_question", "")
    answer = row.get("approved_answer", "")
    priority = (row.get("priority") or "NORMAL").upper()
    status = (row.get("status") or "ACTIVE").upper()
    try:
        aliases = json.loads(row.get("aliases") or "[]")
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.split("|") if a.strip()]
    except Exception:
        aliases = [a.strip() for a in (row.get("aliases") or "").split("|") if a.strip()]

    errors = []
    if not topic:
        errors.append("topic is required")
    if not primary_question:
        errors.append("primary_question is required")
    if priority not in {"NORMAL", "HIGH"}:
        errors.append("priority must be NORMAL or HIGH")
    if status not in {"ACTIVE", "DISABLED", "ARCHIVED"}:
        errors.append("status must be ACTIVE, DISABLED, or ARCHIVED")

    valid, answer_error = validate_candidate_answer(answer)
    if not valid:
        errors.append(answer_error)

    if errors:
        return {"row_number": row["row_number"], "row_status": "Invalid", "errors": errors, "data": row}

    warnings = detect_business_commitment_warnings(answer)
    dupes = db.find_learned_knowledge_duplicates(primary_question, aliases)
    row_status = "Ready"
    if dupes["exact"]:
        row_status = "Duplicate"
    elif dupes["similar"]:
        row_status = "Possible Duplicate"
    elif warnings:
        row_status = "Validation Warning"

    return {
        "row_number": row["row_number"],
        "row_status": row_status,
        "errors": [],
        "warnings": warnings,
        "duplicates": [i.get("id") for i in dupes["exact"]] + [i.get("id") for i in dupes["similar"]],
        "data": {"topic": topic, "primary_question": primary_question, "approved_answer": answer, "aliases": aliases, "priority": priority, "status": status},
    }


@app.post("/admin/custom-qa/import/dry-run")
async def import_custom_qa_dry_run(
    file: UploadFile = File(...),
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Step 2 of the import flow: validates every row, writes nothing."""
    require_admin(x_admin_key, x_admin_pin)
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    rows = _parse_custom_qa_csv(raw)
    results = [_validate_import_row(row) for row in rows]
    summary = {
        "rows": len(results),
        "valid": sum(1 for r in results if r["row_status"] == "Ready"),
        "warnings": sum(1 for r in results if r["row_status"] == "Validation Warning"),
        "duplicates": sum(1 for r in results if r["row_status"] == "Duplicate"),
        "possible_duplicates": sum(1 for r in results if r["row_status"] == "Possible Duplicate"),
        "errors": sum(1 for r in results if r["row_status"] == "Invalid"),
    }
    return {"status": "success", "summary": summary, "rows": results}


@app.post("/admin/custom-qa/import/confirm")
async def import_custom_qa_confirm(
    file: UploadFile = File(...),
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Step 3: re-validates the same file (nothing was staged from the dry
    run) and imports only rows that are Ready. Duplicate/Invalid rows are
    skipped, never silently overwritten or partially published.
    """
    require_admin(x_admin_key, x_admin_pin)
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    rows = _parse_custom_qa_csv(raw)
    results = [_validate_import_row(row) for row in rows]

    imported_ids = []
    skipped = []
    for r in results:
        if r["row_status"] != "Ready":
            skipped.append({"row_number": r["row_number"], "reason": r["row_status"]})
            continue
        data = r["data"]
        new_id = db.add_learned_knowledge(
            data["topic"], data["primary_question"], data["approved_answer"],
            source="csv_import", confidence=1.0, priority=data["priority"], status=data["status"],
            created_by="csv_import", aliases=data["aliases"],
        )
        imported_ids.append(new_id)

    return {"status": "success", "imported": len(imported_ids), "imported_ids": imported_ids, "skipped": skipped}


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
    status: str = "PENDING_APPROVAL",
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Returns RAG/AI candidate answers by status. Default is the live approval
    queue (PENDING_APPROVAL); APPROVED/REJECTED return the historical record
    for the AI Resolution & Answers "Rejected / Archived" view — a decision
    already made, not a new place decisions get made.
    """
    require_admin(x_admin_key, x_admin_pin)
    normalized = status.strip().upper()
    if normalized == "PENDING_APPROVAL":
        candidates = db.get_pending_candidates()
    elif normalized in {"APPROVED", "REJECTED"}:
        candidates = db.get_candidates_by_status(normalized)
    else:
        raise HTTPException(status_code=400, detail="status must be PENDING_APPROVAL, APPROVED, or REJECTED")
    return {"status": "success", "count": len(candidates), "candidates": candidates}

@app.post("/admin/db-backup")
def create_db_backup(
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Snapshots the live SQLite file next to itself before a schema migration
    runs — every prior schema change in this project (routing tables, the
    Topic/Menu catalogue, router/triage shadow) landed via additive
    `_ensure_column` calls with no separate backup step, but a client asked
    for one explicitly ahead of the Custom Q&A migration, so this exists to
    make that step actually possible from here (nothing in this repo can
    reach the Railway volume directly). Checkpoints WAL first so the copy is
    consistent, not just a copy of the base file with pending writes still
    sitting in a -wal sidecar.
    """
    require_admin(x_admin_key, x_admin_pin)
    import shutil
    import sqlite3

    src_path = db.db_path
    if not os.path.exists(src_path):
        raise HTTPException(status_code=404, detail=f"No database file at {src_path}")

    try:
        with sqlite3.connect(src_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(FULL);")
    except Exception as e:
        log.warning("WAL checkpoint before backup failed (continuing anyway): %s", e)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{src_path}.backup-{stamp}"
    shutil.copy2(src_path, backup_path)
    size_bytes = os.path.getsize(backup_path)
    integrity_check = "unknown"
    try:
        with sqlite3.connect(backup_path) as verify_conn:
            integrity_check = str(verify_conn.execute("PRAGMA integrity_check;").fetchone()[0])
    except Exception as e:
        log.warning("Database backup integrity check failed: %s", e)
        integrity_check = f"error:{type(e).__name__}"
    log.info("Database backed up to %s (%d bytes)", backup_path, size_bytes)
    return {"status": "backed_up", "backup_path": backup_path, "size_bytes": size_bytes, "source_path": src_path, "integrity_check": integrity_check, "verified": integrity_check.lower() == "ok" and size_bytes > 0}

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

    eligible, eligibility_error = candidate_eligibility(
        candidate.get("user_message") or "", candidate.get("response") or ""
    )
    valid, validation_error = validate_candidate_answer(candidate.get("response") or "")
    if not eligible or not valid:
        raise HTTPException(status_code=422, detail=eligibility_error or validation_error)

    approved_by = body.get("approved_by", "admin")
    knowledge_id = db.add_learned_knowledge(
        topic=candidate.get("name") or "Approved candidate",
        question_pattern=candidate.get("user_message") or candidate.get("name") or "",
        verified_answer=candidate["response"],
        source=f"approved_candidate#{candidate_id}",
        confidence=max(float(candidate.get("confidence") or 0.0), 0.90),
    )
    if not db.approve_candidate_template(candidate_id, approved_by=approved_by):
        raise HTTPException(status_code=409, detail="Candidate template could not be approved")
    return {
        "status": "approved",
        "candidate_id": candidate_id,
        "learned_knowledge_id": knowledge_id,
        "message": "Candidate approved and added to the reviewed knowledge base.",
    }

@app.patch("/admin/candidates/{candidate_id}")
async def edit_candidate_template(
    candidate_id: int,
    request: Request,
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """Edits the current answer while leaving it in the approval queue."""
    require_admin(x_admin_key, x_admin_pin)
    candidate = db.get_candidate_template(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate template not found")
    if candidate.get("status") != "PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail="Candidate template is no longer pending")
    try:
        body = await request.json()
    except Exception:
        body = {}
    response = str(body.get("response") or "")
    valid, error = validate_candidate_answer(response)
    if not valid:
        raise HTTPException(status_code=422, detail=error)
    eligible, error = candidate_eligibility(candidate.get("user_message") or "", response)
    if not eligible:
        raise HTTPException(status_code=422, detail=error)
    if not db.edit_candidate_template(
        candidate_id, response, edited_by=str(body.get("edited_by") or "admin")
    ):
        raise HTTPException(status_code=409, detail="Candidate template could not be edited")
    return {"status": "pending", "candidate_id": candidate_id}

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


# --------------------------------------------------------------------------
# Dynamic menu catalogue — the registry every menu (built-in or client-
# created) is listed under. camellia/menu_catalogue.py owns validation and
# id generation; the durable image/version machinery in menu_images.py
# already works against any menu_id here, so creating a menu needs nothing
# beyond a catalogue row plus one published image to become real everywhere
# it is referenced (Menu Manager gallery, Topic default menu, Instagram
# routing) — no second upload system, no restart.
# --------------------------------------------------------------------------

@app.get("/admin/menu-catalogue")
def list_menu_catalogue_route(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    return {"status": "success", "menus": menu_catalogue.list_menus(), "categories": menu_catalogue.CATEGORIES}


@app.get("/admin/menu-manager/images")
def list_menu_images_gallery(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """Every menu's current image card — built-in and client-created alike."""
    require_admin(x_admin_key, x_admin_pin)
    return {"status": "success", "menus": menu_images.list_menus()}


@app.post("/admin/menu-catalogue")
async def create_menu_catalogue_entry(
    name: str = Form(...),
    category: str = Form("Other"),
    description: str = Form(...),
    active: bool = Form(True),
    file: UploadFile = File(...),
    x_admin_key: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None),
):
    """
    Creates a new menu and immediately publishes its image — the single
    "Create Menu" action the client sees corresponds to one catalogue row
    plus one published image version, not a separate draft-then-publish
    step, since a brand-new menu has nothing else to preview against yet.

    No OCR, no fact extraction: the uploaded image is stored exactly as
    uploaded. Only the name/category/description typed here are recorded as
    customer-facing text; nothing about price, availability, ingredients or
    allergens is inferred from the image or created by this endpoint.
    """
    require_admin(x_admin_key, x_admin_pin)
    image_bytes = await file.read()
    if len(image_bytes) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large (maximum 10MB allowed).")
    try:
        entry = menu_catalogue.create_menu(name, category, description, active=active)
    except MenuCatalogueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        menu_images.save_draft(entry["menu_id"], image_bytes, file.content_type or "", x_admin_key or "admin")
        published = menu_images.publish(entry["menu_id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "success", "menu": entry, "image": published}


@app.patch("/admin/menu-catalogue/{menu_id}")
async def update_menu_catalogue_entry_route(
    menu_id: str, request: Request,
    x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        updated = menu_catalogue.update_menu(
            menu_id, name=body.get("name"), category=body.get("category"),
            description=body.get("description"), active=body.get("active"),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No menu '{menu_id}'")
    except MenuCatalogueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "success", "menu": updated}


# --------------------------------------------------------------------------
# Instagram Content Routing topics — a reusable Topic an operator picks once
# instead of retyping the same routing_topic text on every route. Selecting
# one on a route pre-fills that route's topic text, priority products and
# knowledge scope; from then on the route itself, not the Topic, is what
# matching and replies act on (see validate_route_payload's docstring).
# --------------------------------------------------------------------------

@app.get("/admin/instagram-topics")
def list_instagram_topics_route(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    return {"status": "success", "topics": [decode_topic(t) for t in db.list_instagram_topics()]}


@app.post("/admin/instagram-topics")
async def create_instagram_topic_route(
    request: Request, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    try:
        topic = create_topic(
            body.get("name", ""), body.get("description", ""),
            default_menu_id=body.get("default_menu_id") or None,
            priority_products=body.get("priority_products") or [],
            knowledge_scope=body.get("knowledge_scope") or [],
            active=bool(body.get("active", True)),
            response_order=body.get("response_order") or "description_first",
        )
    except InstagramTopicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "success", "topic": decode_topic(topic)}


@app.patch("/admin/instagram-topics/{topic_id}")
async def update_instagram_topic_route(
    topic_id: str, request: Request,
    x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None),
):
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        body = {}
    kwargs: Dict[str, Any] = {}
    for field in ("name", "description", "active"):
        if field in body:
            kwargs[field] = body[field]
    if "priority_products" in body:
        kwargs["priority_products"] = body["priority_products"]
    if "knowledge_scope" in body:
        kwargs["knowledge_scope"] = body["knowledge_scope"]
    if "response_order" in body:
        kwargs["response_order"] = body["response_order"]
    if "default_menu_id" in body:
        if body["default_menu_id"]:
            kwargs["default_menu_id"] = body["default_menu_id"]
        else:
            kwargs["clear_default_menu"] = True
    try:
        topic = update_topic(topic_id, **kwargs)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No topic '{topic_id}'")
    except InstagramTopicError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "success", "topic": decode_topic(topic)}


@app.get("/admin/instagram-routes/preview")
def preview_instagram_route_response(
    topic_id: str = "", product: str = "", menu_id: str = "", response_order: str = "", menu_mode: str = "INHERIT",
    x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None),
):
    """
    Approximately what a customer would receive from a vague "Tell me more"
    through this route — read-only, no session created, no conversation
    logged. Composed from exactly the pieces a real reply would use: the
    Topic's own admin-authored description, the linked menu's current image,
    and the first priority product's approved template — never invented.

    `product` and `menu_id` are the in-progress form's own current choices,
    not yet saved — passing them previews the route being edited rather than
    only the Topic's last-saved defaults, so unsaved changes show up too.
    """
    require_admin(x_admin_key, x_admin_pin)
    topic = db.get_instagram_topic(topic_id) if topic_id else None
    if topic_id and not topic:
        raise HTTPException(status_code=404, detail=f"No topic '{topic_id}'")

    topic_view = decode_topic(topic) if topic else {}
    menu_mode = str(menu_mode or "INHERIT").strip().upper()
    if menu_mode not in {"NONE", "SELECTED", "INHERIT"}:
        raise HTTPException(status_code=400, detail="menu_mode must be NONE, SELECTED, or INHERIT.")
    priority = topic_view.get("priority_products", []) if topic else []
    chosen_product = product or (priority[0] if priority and menu_mode != "NONE" else None)
    template_id = route_template_id(chosen_product) if chosen_product else None
    template = responder._template_by_id(template_id) if template_id else None

    resolved_menu_id = menu_id if menu_mode == "SELECTED" else ((topic or {}).get("default_menu_id") if menu_mode == "INHERIT" else None)
    menu_entry = menu_catalogue.get_menu(resolved_menu_id) if resolved_menu_id else None
    image_url = menu_images.current_url(resolved_menu_id) if resolved_menu_id else None
    menu_response = ((template or {}).get("response") or "") if menu_mode != "NONE" else ""
    chosen_order = response_order if response_order in RESPONSE_ORDERS else (topic_view.get("response_order") or "description_first")
    ordered_response = compose_topic_response(
        topic_view.get("name"), topic_view.get("description"), menu_response, chosen_order, menu_mode,
    )

    return {
        "status": "success",
        "topic_name": topic_view.get("name"),
        "topic_description": topic_view.get("description"),
        "linked_menu": menu_entry,
        "linked_menu_image": image_url,
        "matched_product": chosen_product,
        "approved_response": ordered_response,
        "menu_response": menu_response,
        "response_order": chosen_order,
        "menu_mode": menu_mode,
    }


# --------------------------------------------------------------------------
# Menu Manager — every menu the chatbot actively serves, edited by the client
# as a draft, previewed, and only made customer-facing by Save & Publish
# (camellia/menu_manager.py owns validation/rendering/persistence; this is
# just the admin-gated HTTP surface over it, same auth as every other
# /admin/menu/* route).
# --------------------------------------------------------------------------

@app.get("/admin/menu-manager/menus")
def list_manageable_menus(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    menus = menu_manager.list_menus()
    for menu in menus:
        menu.update(menu_images.card(menu["menu_key"]))
    return {"status": "success", "menus": menus}


@app.get("/admin/menu-manager/menus/{menu_key}")
def get_manageable_menu(menu_key: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        return {"status": "success", **menu_manager.get_menu(menu_key), "image": menu_images.card(menu_key)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No manageable menu '{menu_key}'")


@app.put("/admin/menu-manager/menus/{menu_key}/draft")
async def save_menu_draft(menu_key: str, request: Request, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body.get("items"), list):
        raise HTTPException(status_code=400, detail="'items' must be a list")
    admin_identity = x_admin_key or "admin"
    try:
        result = menu_manager.save_draft(menu_key, body["items"], updated_by=admin_identity)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No manageable menu '{menu_key}'")
    except menu_manager.MenuValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "success", **result}


@app.delete("/admin/menu-manager/menus/{menu_key}/draft")
def discard_menu_draft(menu_key: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        menu_manager.discard_draft(menu_key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No manageable menu '{menu_key}'")
    return {"status": "success"}


@app.get("/admin/menu-manager/menus/{menu_key}/preview")
def preview_menu(menu_key: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        return {"status": "success", **menu_manager.preview(menu_key)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No manageable menu '{menu_key}'")


@app.post("/admin/menu-manager/menus/{menu_key}/publish")
async def publish_menu(menu_key: str, request: Request, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    admin_identity = x_admin_key or "admin"
    raw_items = None
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("items"), list):
            raw_items = body["items"]
    except Exception:
        raw_items = None  # No body / not JSON: publish the existing draft.
    try:
        result = menu_manager.publish(menu_key, raw_items=raw_items, changed_by=admin_identity)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No manageable menu '{menu_key}'")
    except menu_manager.MenuValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "success", **result}


@app.get("/admin/menu-manager/menus/{menu_key}/versions")
def list_menu_versions(menu_key: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        return {"status": "success", "versions": menu_manager.list_versions(menu_key)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No manageable menu '{menu_key}'")


@app.post("/admin/menu-manager/menus/{menu_key}/versions/{version_id}/restore")
def restore_menu_version(menu_key: str, version_id: int, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    admin_identity = x_admin_key or "admin"
    try:
        result = menu_manager.restore_version(menu_key, version_id, changed_by=admin_identity)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No manageable menu '{menu_key}' or version '{version_id}'")
    except menu_manager.MenuValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "success", **result}


# Visual image workflow.  Structured item editing above remains available as
# an advanced capability, while these routes keep image bytes in the durable
# upload directory and SQLite holds metadata only.
@app.post("/admin/menu-manager/menus/{menu_key}/image/draft")
async def save_menu_image_draft(menu_key: str, file: Optional[UploadFile] = File(None), x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    if file is None:
        raise HTTPException(status_code=400, detail="No image file provided.")
    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large (maximum 10MB allowed).")
    try:
        return {"status": "success", "draft": menu_images.save_draft(menu_key, data, file.content_type or "", x_admin_key or "admin")}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No manageable menu '{menu_key}'")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/admin/menu-manager/menus/{menu_key}/image/preview")
def preview_menu_image(menu_key: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        return {"status": "success", **menu_images.preview(menu_key)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No manageable menu '{menu_key}'")


@app.post("/admin/menu-manager/menus/{menu_key}/image/publish")
def publish_menu_image(menu_key: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        return {"status": "success", "image": menu_images.publish(menu_key)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No manageable menu '{menu_key}'")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/admin/menu-manager/menus/{menu_key}/image/history")
def menu_image_history(menu_key: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        return {"status": "success", "versions": menu_images.history(menu_key)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No manageable menu '{menu_key}'")


@app.post("/admin/menu-manager/menus/{menu_key}/image/archive")
def archive_menu_image(menu_key: str, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        return {"status": "success", "image": menu_images.archive(menu_key)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No manageable menu '{menu_key}'")


@app.post("/admin/menu-manager/menus/{menu_key}/image/restore/{version_id}")
def restore_menu_image(menu_key: str, version_id: int, x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    require_admin(x_admin_key, x_admin_pin)
    try:
        restored = menu_images.restore(menu_key, version_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No manageable menu '{menu_key}'")
    if not restored:
        raise HTTPException(status_code=404, detail="Image version not found")
    return {"status": "success", "image": restored}


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

        /* Menu Manager */
        .collection-select { width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px; background: #fff; color: var(--text-dark); }
        .catalogue-item { border: 1px solid var(--border); background: #FCFBF9; color: var(--text-dark); padding: 8px 10px; border-radius: 9px; cursor: pointer; text-align: left; }
        .catalogue-item:hover { border-color: var(--primary); background: #F5EEF0; }
        .catalogue-item small { display: block; color: var(--text-muted); margin-top: 2px; }
        .visual-menu-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; margin-bottom: 20px; }
        .visual-menu-card { border: 1px solid var(--border); border-radius: 14px; overflow: hidden; background: #fff; }
        .visual-menu-card h3 { margin: 0; padding: 14px 14px 6px; color: var(--primary); font-size: 1rem; }
        .visual-menu-card img { width: 100%; height: 210px; object-fit: contain; background: #f6f3ef; display: block; }
        .visual-menu-meta { padding: 8px 14px; color: var(--text-muted); font-size: .8rem; }
        .visual-menu-actions { display: flex; flex-wrap: wrap; gap: 7px; padding: 0 14px 14px; }
        .visual-menu-actions button { font-size: .78rem; padding: 7px 9px; }
        .collection-layout { display: grid; grid-template-columns: minmax(220px, 1fr) 2fr; gap: 18px; align-items: start; }
        .collection-box { border: 1px solid var(--border); border-radius: 14px; padding: 14px; background: #FCFBF9; }
        .collection-box h3 { margin: 0 0 10px; color: var(--primary); font-size: 0.98rem; }
        .collection-slot { border: 1px solid var(--border); border-radius: 10px; padding: 12px; margin-bottom: 10px; background: #fff; }
        .collection-slot label { display: block; font-size: 0.78rem; font-weight: 600; margin-top: 8px; }
        .collection-slot label:first-child { margin-top: 0; }
        @media (max-width: 780px) {
            .collection-layout { grid-template-columns: 1fr; }
        }

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
            <button class="tab-btn active" onclick="switchTab('tab-image', this)">🍱 Menu</button>
            <button class="tab-btn" onclick="switchTab('tab-menu-manager', this)">📋 Menu Manager</button>
            <button class="tab-btn" onclick="switchTab('tab-ai-resolution', this)">🤖 AI Resolution &amp; Answers <span id="aiResolutionBadge" style="display:none; background: var(--danger); color: #fff; border-radius: 10px; padding: 1px 7px; font-size: 0.72rem; margin-left: 4px;"></span></button>
            <button class="tab-btn" onclick="switchTab('tab-custom-qa', this)">🗂️ Custom Q&amp;A</button>
        </div>

        <!-- Customers and Answers used to be their own top-level tabs. Every
             action either one offered — view an escalation, Teach & Resolve,
             view/edit/approve/reject a candidate draft, see approved
             knowledge — is now inside AI Resolution & Answers below, reading
             and writing the exact same candidate_templates/escalations/
             learned_knowledge/knowledge_gaps tables through the exact same
             endpoints. Nothing about those tables or endpoints changed; only
             which screen a client reaches them from did. The two panels
             stay in the DOM, unstyled into the normal flow, reachable only
             from here — for regression comparison during this transition,
             not for daily use. -->
        <details style="margin: 10px 0 0; font-size: 0.82rem; color: var(--text-muted);">
            <summary style="cursor:pointer; user-select:none;">⚙️ Advanced / Legacy Views (debugging only)</summary>
            <div style="display:flex; gap:8px; margin-top:8px; flex-wrap:wrap;">
                <button class="btn-secondary" type="button" onclick="switchTab('tab-stats', null)">Open Legacy Customers View</button>
                <button class="btn-secondary" type="button" onclick="switchTab('tab-candidates', null)">Open Legacy Answers View</button>
            </div>
            <p style="margin-top:6px;">These are the old per-workflow screens, kept only so a real discrepancy against AI Resolution &amp; Answers can be checked during this transition. Use the 🤖 AI Resolution &amp; Answers tab above for normal work.</p>
        </details>

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
                <h2>📣 Instagram Content Routing</h2>
                <p>Connect an Instagram post or reel to a Topic.</p>
            </div>
            <div class="ad-routing-form">
                <div>
                    <label class="field-label" for="igRouteUrl">Instagram Link</label>
                    <input class="field-control" id="igRouteUrl" autocomplete="off" placeholder="https://www.instagram.com/reel/DdAnO6MTok1/" onblur="identifyInstagramRoute()" onchange="identifyInstagramRoute()">
                </div>
            </div>
            <div id="igRouteIdentity" aria-live="polite" style="font-size:0.82rem; margin:10px 0; color:var(--text-muted);"></div>
            <div id="igPostConnectionStatus" style="display:none; font-size:0.82rem; margin:10px 0; padding:8px 10px; border-radius:8px;"></div>

            <div style="margin-top:14px;">
                <label class="field-label" for="igTopicPicker">Topic</label>
                <div style="display:flex; gap:8px; flex-wrap:wrap;">
                    <select class="field-control" id="igTopicPicker" onchange="onIgTopicChange()" style="flex:1; min-width:220px;"><option value="">Select Topic</option></select>
                    <button class="btn-secondary" type="button" onclick="openTopicModal()">+ Add Topic</button>
                    <button class="btn-secondary" id="igEditTopicButton" type="button" onclick="openTopicEditor()" disabled>Edit Topic</button>
                </div>
            </div>

            <div id="igTopicPreview" style="display:none; margin-top:14px; background:#FCFBF9; border:1px solid var(--border); border-radius:10px; padding:14px; font-size:0.85rem;">
                <div style="font-size:0.76rem; color:var(--text-muted); margin-bottom:6px;">Topic preview</div>
                <h3 id="igTopicPreviewName" style="margin:0; font-size:1rem; color:var(--primary);"></h3>
                <div style="margin-top:8px;"><b>Menu:</b> <span id="igTopicPreviewMenu">—</span></div>
                <div style="margin-top:4px;"><b>Priority:</b> <span id="igTopicPreviewPriority">—</span></div>
                <div style="margin-top:4px; white-space:pre-wrap;"><b>Description:</b> <span id="igTopicPreviewDescription">—</span></div>
                <div style="margin-top:10px;"><b>Reply Order:</b> <span id="igTopicPreviewOrder">Description First</span></div>
                <button class="btn-secondary" type="button" onclick="swapIgResponseOrder()" style="margin-top:8px;">⇅ Swap Order</button>
                <button class="btn-secondary" type="button" onclick="previewInstagramRoute()" style="margin-top:10px;">Preview Reply</button>
                <div id="igPreviewBox" style="display:none; margin-top:10px; background:#fff; border:1px solid var(--border); border-radius:8px; padding:10px; font-size:0.84rem;"></div>
            </div>

            <details id="igCustomize" style="margin-top:12px;"><summary style="cursor:pointer;"><b>Customize</b></summary>
                <div style="margin-top:10px;">
                    <label class="field-label" for="igMenuPicker">Menu to Show</label>
                    <div style="display:flex; gap:8px;">
                        <select class="field-control" id="igMenuPicker"><option value="">Select Menu</option></select>
                        <button class="btn-secondary" type="button" onclick="openMenuModal()">+ Add Menu</button>
                    </div>
                    <label class="field-label" for="igTopicDescription" style="margin-top:10px;">Topic Description</label>
                    <textarea class="field-control" id="igTopicDescription" rows="2" placeholder="Select a Topic to see its description"></textarea>
                    <label class="field-label" style="margin-top:10px;">Priority Products</label>
                    <div style="display:flex; gap:8px;"><select class="field-control" id="igProductPicker"></select><button class="btn-secondary" type="button" onclick="addIgPriorityProduct()">+ Add Product</button></div>
                    <div id="igPriorityProducts" class="ad-route-list" style="margin-top:8px;"></div>
                </div>
            </details>

            <details id="igAdvancedSettings" style="margin-top:12px;"><summary style="cursor:pointer;"><b>Advanced Settings</b></summary>
                <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin-top:10px;">
                    <input class="field-control" id="igMediaId" placeholder="Verified Media / Story ID">
                    <input class="field-control" id="igAdId" placeholder="Meta Ad ID">
                    <input class="field-control" id="igCampaignId" placeholder="Campaign ID (reference only)">
                    <select class="field-control" id="igEscalationPolicy"><option value="human_if_unsupported">Human if unsupported</option><option value="canonical">Normal canonical rules</option><option value="always_human">Always offer human help</option></select>
                    <select class="field-control" id="igKnowledgeScope" multiple title="Optional canonical knowledge scope"></select>
                </div>
                <p style="font-size:0.76rem;color:var(--text-muted);">Advanced settings are normally filled automatically. Only change these when instructed.</p>
            </details>

            <div class="action-bar">
                <button class="btn-primary" id="igSaveButton" type="button" onclick="saveInstagramRoute()">Save Routing</button>
                <button class="btn-secondary" type="button" onclick="resetInstagramRouteForm()">Cancel</button>
            </div>
            <div id="adRouteResult" class="result-box"></div>
            <div style="display:flex; gap:8px; margin-top:16px; border-bottom:1px solid var(--border);">
                <button class="tab-btn active" type="button" id="igRouteTabActiveBtn" onclick="switchIgRouteTab('active')" style="border-radius:8px 8px 0 0;">Active</button>
                <button class="tab-btn" type="button" id="igRouteTabArchivedBtn" onclick="switchIgRouteTab('archived')" style="border-radius:8px 8px 0 0;">Archived</button>
            </div>
            <div id="adRouteList" class="ad-route-list"><p style="color:var(--text-muted);font-size:0.84rem;">Loading saved routes...</p></div>
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

        <!-- MENU MANAGER tab: every menu the chatbot actively serves, edited
             as a draft, previewed exactly as customers will see it, and only
             made live by Save & Publish. camellia/menu_manager.py owns
             validation/persistence/reload; this is its admin UI. -->
        <div id="tab-menu-manager" class="panel-card" data-group="menu_manager" style="display: none;">
            <div class="panel-header">
                <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap;">
                    <div>
                        <h2>📋 Menu Manager</h2>
                        <p>Edit any menu the chatbot uses. Changes are a draft until you press Save &amp; Publish — the bot keeps answering with the current live menu until then.</p>
                    </div>
                    <button class="btn-primary" type="button" onclick="openMenuModal()" style="white-space:nowrap;">+ Add New Menu</button>
                </div>
            </div>

            <div id="mmImageGrid" class="visual-menu-grid"><p style="color: var(--text-muted);">Loading menu images...</p></div>

            <div class="collection-layout">
                <div class="collection-box">
                    <h3>Menus</h3>
                    <div id="mmMenuList"><p style="color: var(--text-muted); font-size: 0.84rem;">Loading menus...</p></div>
                </div>

                <div class="collection-box">
                    <h3 id="mmEditorTitle">Select a menu to edit</h3>
                    <div id="mmEditorItems"></div>
                    <div class="action-bar" style="margin-top: 14px; flex-wrap: wrap; gap: 8px;">
                        <button class="btn-secondary" type="button" onclick="mmAddItem()">+ Add item</button>
                        <button class="btn-secondary" type="button" onclick="mmSaveDraft()">💾 Save draft</button>
                        <button class="btn-secondary" type="button" onclick="mmPreview()">👁 Preview</button>
                        <button class="btn-primary" type="button" onclick="mmPublish()">✅ Save &amp; Publish</button>
                        <button class="btn-secondary" type="button" onclick="mmCancelDraft()">✕ Cancel draft</button>
                    </div>
                    <div id="mmMessage" class="result-box"></div>
                    <div id="mmPreviewBox" style="display:none; margin-top: 14px;">
                        <h3 style="color: var(--primary); font-size: 0.95rem;">Preview — exactly what customers/chatbot will see</h3>
                        <pre id="mmPreviewText" style="white-space: pre-wrap; background: #FCFBF9; border: 1px solid var(--border); border-radius: 10px; padding: 12px; font-size: 0.82rem;"></pre>
                    </div>
                    <div id="mmVersions" style="margin-top: 18px;"></div>
                </div>
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
                            <th>AI Draft Confidence</th>
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

        <!-- AI RESOLUTION & ANSWERS: one queue combining what used to be two
             screens (Customers waiting on a person, and Answers) plus the
             knowledge-gaps queue that already existed as an API but had no
             screen at all. It reads the same three tables through the same
             endpoints those older screens already use — approving, rejecting,
             editing, and resolving here call the exact same backend actions,
             so nothing about how an answer reaches a customer changes. This
             is a merged view, not a new workflow. -->
        <div id="tab-ai-resolution" class="panel-card" data-group="ai_resolution" style="display: none;">
            <div class="panel-header">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <h2>🤖 AI Resolution &amp; Answers</h2>
                        <p>The one place for everything the bot could not answer on its own: drafts waiting on your approval, questions it has no knowledge of yet, and customers waiting on a person — plus the answers you've already approved. <b>Nothing here has been shown to a customer</b> except items already marked resolved/approved.</p>
                    </div>
                    <button class="btn-secondary" onclick="loadAiResolution()">🔄 Refresh</button>
                </div>
            </div>

            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 18px;">
                <div style="background: #F8FAFC; border: 1px solid var(--border); padding: 16px; border-radius: 14px; text-align: center;">
                    <span style="font-size: 0.75rem; color: #64748B; font-weight: 600;">TOTAL INQUIRIES</span>
                    <h3 id="aiStatTotal" style="font-size: 1.7rem; margin-top: 4px;">0</h3>
                </div>
                <div style="background: #ECFDF5; border: 1px solid #A7F3D0; padding: 16px; border-radius: 14px; text-align: center;">
                    <span style="font-size: 0.75rem; color: #065F46; font-weight: 600;">AUTO RESOLVED</span>
                    <h3 id="aiStatAutoResolved" style="font-size: 1.7rem; color: var(--success); margin-top: 4px;">0</h3>
                </div>
                <div style="background: #FFF7ED; border: 1px solid #FED7AA; padding: 16px; border-radius: 14px; text-align: center;">
                    <span style="font-size: 0.75rem; color: #9A3412; font-weight: 600;">NEEDS ATTENTION</span>
                    <h3 id="aiStatNeedsAttention" style="font-size: 1.7rem; color: #9A3412; margin-top: 4px;">0</h3>
                </div>
                <div style="background: #FEF2F2; border: 1px solid #FECACA; padding: 16px; border-radius: 14px; text-align: center;">
                    <span style="font-size: 0.75rem; color: #991B1B; font-weight: 600;">HUMAN REQUIRED</span>
                    <h3 id="aiStatHuman" style="font-size: 1.7rem; color: var(--danger); margin-top: 4px;">0</h3>
                </div>
                <div id="aiStatAnalyzedCard" style="display:none; background: #EEF2FF; border: 1px solid #C7D2FE; padding: 16px; border-radius: 14px; text-align: center;">
                    <span style="font-size: 0.75rem; color: #3730A3; font-weight: 600;">AI ANALYZED (shadow, not resolved)</span>
                    <h3 id="aiStatAnalyzed" style="font-size: 1.7rem; color: #3730A3; margin-top: 4px;">0</h3>
                </div>
            </div>

            <div style="margin-bottom:12px;">
                <input type="text" id="aiResolutionSearch" class="field-control" placeholder="🔎 Search customer questions or answers…" style="max-width:420px;" oninput="renderAiResolution()">
            </div>

            <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px;">
                <button class="tab-btn active" type="button" id="aiFilterBtn-needs_attention" onclick="setAiResolutionFilter('needs_attention')">Needs Attention</button>
                <button class="tab-btn" type="button" id="aiFilterBtn-ai_suggested" onclick="setAiResolutionFilter('ai_suggested')">AI Suggested</button>
                <button class="tab-btn" type="button" id="aiFilterBtn-human_required" onclick="setAiResolutionFilter('human_required')">Human Required</button>
                <button class="tab-btn" type="button" id="aiFilterBtn-teach_required" onclick="setAiResolutionFilter('teach_required')">Knowledge Gaps</button>
                <button class="tab-btn" type="button" id="aiFilterBtn-approved" onclick="setAiResolutionFilter('approved')">Approved Answers</button>
                <button class="tab-btn" type="button" id="aiFilterBtn-rejected" onclick="setAiResolutionFilter('rejected')">Rejected / Archived</button>
                <button class="tab-btn" type="button" onclick="document.getElementById('triage-shadow-section').scrollIntoView({behavior:'smooth'})">Triage Shadow ↓</button>
            </div>

            <div style="overflow-x: auto;">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Status</th>
                            <th>Customer Asked / Situation</th>
                            <th>Draft Answer / Detail</th>
                            <th>Confidence / Seen</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody id="aiResolutionTableBody">
                        <tr><td colspan="5" style="text-align: center; color: #888;">Loading...</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- Triage shadow mode: a model's opinion on every message above,
                 run purely for measurement. It has never sent a reply, never
                 auto-approved anything, and never touched the queue above —
                 this is read-only so the quality of its calls can be judged
                 against real traffic before any auto-resolve path is built. -->
            <div id="triage-shadow-section" style="margin-top:28px; padding-top:20px; border-top:2px solid var(--border);">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 10px;">
                    <div>
                        <h3 style="margin:0;">🔬 Triage Shadow Mode <span style="font-weight:400; font-size:0.8rem; color:var(--text-muted);">(measurement only — nothing below has ever reached a customer)</span></h3>
                        <p id="triageShadowStatusLine" style="font-size:0.82rem; color:var(--text-muted); margin-top:4px;">Checking status…</p>
                    </div>
                    <button class="btn-secondary" onclick="loadTriageShadow()">🔄 Refresh</button>
                </div>

                <div id="triageShadowSummary" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px;"></div>

                <div style="overflow-x: auto;">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>Message</th>
                                <th>Triage Category</th>
                                <th>Confidence</th>
                                <th>Grounded</th>
                                <th>Would Auto-Resolve?</th>
                                <th>What Actually Happened</th>
                                <th>Model's Reasoning</th>
                            </tr>
                        </thead>
                        <tbody id="triageShadowTableBody">
                            <tr><td colspan="7" style="text-align: center; color: #888;">Loading...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <div id="assisted-evaluation-section" style="margin-top:28px;padding-top:20px;border-top:2px solid var(--border);">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;">
                    <div><h3 style="margin:0;">📊 Assisted Resolution Performance</h3><p style="font-size:.82rem;color:var(--text-muted);margin-top:4px;">Read-only evaluation. Approval rate is not model accuracy; Auto Resolve remains informational only.</p></div>
                    <button class="btn-secondary" onclick="loadAssistedEvaluation()">🔄 Refresh evaluation</button>
                </div>
                <div id="assistedPerformanceSummary" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0;"></div>
                <div id="assistedReadiness" style="padding:12px;border:1px solid var(--border);border-radius:10px;margin-bottom:14px;"><b>Auto Resolve Readiness</b><span id="assistedReadinessBody">Loading…</span></div>
                <div id="autoResolveControlState" style="padding:12px;border:1px solid #FDE68A;background:#FFFBEB;border-radius:10px;margin-bottom:14px;"><b>AUTO RESOLVE: OFF</b><span id="autoResolveControlDetail" style="margin-left:8px;color:var(--text-muted);">Simulation status loading…</span></div>
                <div id="autoResolveMetrics" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px;"></div>
                <div style="overflow-x:auto;margin-bottom:14px;"><table class="data-table"><thead><tr><th>Confidence Bucket</th><th>Reviewed</th><th>Approved As-Is</th><th>Edited</th><th>Rejected</th><th>Human Handoff</th></tr></thead><tbody id="assistedConfidenceTable"></tbody></table></div>
                <div style="overflow-x:auto;margin-bottom:14px;"><table class="data-table"><thead><tr><th>Category</th><th>Total</th><th>Approved</th><th>Edited</th><th>Rejected</th><th>Human Final Action</th></tr></thead><tbody id="assistedCategoryTable"></tbody></table></div>
                <div style="overflow-x:auto;margin-bottom:14px;"><table class="data-table"><thead><tr><th>Grounding</th><th>Reviewed</th><th>Approved</th><th>Edited</th><th>Rejected</th><th>Sent to Human</th></tr></thead><tbody id="assistedGroundingTable"></tbody></table></div>
                <div style="overflow-x:auto;margin-bottom:14px;"><table class="data-table"><thead><tr><th>Risk Flag</th><th>Total</th><th>Approved</th><th>Edited</th><th>Rejected</th><th>Sent to Human</th></tr></thead><tbody id="assistedRiskTable"></tbody></table></div>
                <h4>High Confidence Problems</h4><div id="assistedHighConfidenceProblems" style="font-size:.82rem;color:var(--text-muted);">None recorded.</div>
                <h4>Shadow vs Assisted Comparison</h4><div id="assistedShadowComparison" style="font-size:.82rem;color:var(--text-muted);">No correlated records.</div>
                <h4>Safety Metrics</h4><div id="assistedSafetyMetrics" style="font-size:.82rem;color:var(--text-muted);">Loading…</div>
            </div>
        </div>

        <!-- CUSTOM Q&A: the client-managed reusable knowledge library. This
             is not a second knowledge system -- every row here is a plain
             learned_knowledge record, the exact same table Teach & Resolve
             and candidate approval already write to, read through the exact
             same matcher (camellia.database.search_learned_knowledge). AI
             Resolution & Answers stays the operational/AI/human-review
             workflow; this tab is where the client intentionally authors
             and maintains reusable answers. -->
        <div id="tab-custom-qa" class="panel-card" data-group="custom_qa" style="display: none;">
            <div class="panel-header">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <h2>🗂️ Custom Q&amp;A / Response Library</h2>
                        <p>Create and manage approved answers the chatbot can reuse when customers ask similar questions. Search, preview, test and safely edit customer-facing copy; protected runtime rules remain read-only.</p>
                    </div>
                    <div style="display:flex; gap:8px; flex-wrap:wrap;">
                        <button class="btn-primary" onclick="openCustomQaModal()">+ Add Q&amp;A</button>
                        <button class="btn-secondary" onclick="openCustomQaImportModal()">Import CSV</button>
                        <button class="btn-secondary" onclick="exportCustomQaCsv()">Export CSV</button>
                    </div>
                </div>
            </div>

            <div style="margin-bottom:12px;">
                <div id="responseLibrarySearchWrap" style="position:relative; max-width:560px;">
                    <input type="text" id="customQaSearch" class="field-control" placeholder="🔎 Search questions, triggers, answers, category or template ID…" autocomplete="off" style="max-width:560px;" oninput="scheduleResponseLibrarySearch()" onkeydown="responseLibrarySearchKeydown(event)">
                    <div id="responseLibrarySuggestions" role="listbox" aria-label="Response Library suggestions" style="display:none; position:absolute; z-index:20; left:0; right:0; top:calc(100% + 3px); max-height:320px; overflow:auto; background:#fff; border:1px solid var(--border); border-radius:10px; box-shadow:0 10px 25px rgba(15,23,42,.14);"></div>
                </div>
                <div id="responseLibraryResultCount" style="font-size:.78rem;color:var(--text-muted);margin-top:5px;"></div>
            </div>
            <div id="customQaAiSignals" style="padding:10px;border:1px solid var(--border);border-radius:10px;margin-bottom:12px;font-size:.82rem;">AI Review Signal: loading…</div>

            <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px;">
                <button class="tab-btn active" type="button" id="cqaStatusBtn-ACTIVE" onclick="setCustomQaStatusFilter('ACTIVE')">Active</button>
                <button class="tab-btn" type="button" id="cqaStatusBtn-DISABLED" onclick="setCustomQaStatusFilter('DISABLED')">Disabled</button>
                <button class="tab-btn" type="button" id="cqaStatusBtn-ARCHIVED" onclick="setCustomQaStatusFilter('ARCHIVED')">Archived</button>
                <button class="tab-btn" type="button" id="cqaStatusBtn-ALL" onclick="setCustomQaStatusFilter('ALL')">All</button>
            </div>
            <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px;">
                <select class="field-control" id="cqaSourceFilter" style="max-width:200px;" onchange="renderCustomQaList(); renderSystemResponseLibrary()">
                    <option value="">All Sources</option>
                    <option value="client_admin">Created by Client</option>
                    <option value="staff_resolution">Teach &amp; Resolve</option>
                    <option value="staff_dashboard">Staff Resolution</option>
                    <option value="csv_import">Imported</option>
                    <option value="system_template">System Template</option>
                    <option value="approved_candidate">AI Approved</option>
                </select>
                <select class="field-control" id="cqaTopicFilter" style="max-width:200px;" onchange="renderCustomQaList(); renderSystemResponseLibrary()">
                    <option value="">All Topics</option>
                </select>
                <select class="field-control" id="cqaPriorityFilter" style="max-width:160px;" onchange="renderCustomQaList()">
                    <option value="">All Priorities</option>
                    <option value="NORMAL">Normal</option>
                    <option value="HIGH">High</option>
                    <option value="PROTECTED">Protected System</option>
                </select>
                <button class="btn-secondary" onclick="loadCustomQa()">🔄 Refresh</button>
            </div>

            <div id="systemResponseLibrary" style="margin:0 0 18px; padding:14px; border:1px solid var(--border); border-radius:12px; background:#FCFBFA;">
                <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;">
                    <h3 style="margin:0; color:var(--primary);">System Templates</h3>
                    <span style="font-size:.78rem; color:var(--text-muted);">Triggers and protected runtime logic are read-only; response copy can be safely overridden.</span>
                </div>
                <div id="systemResponseLibraryCards" style="display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:10px; margin-top:10px;"></div>
            </div>

            <div style="overflow-x: auto;">
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Topic</th>
                            <th>Question / Aliases</th>
                            <th>Approved Answer</th>
                            <th>Status</th>
                            <th>Priority</th>
                            <th>Source</th>
                            <th>Usage</th>
                            <th>Last Edited</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="customQaTableBody">
                        <tr><td colspan="9" style="text-align: center; color: #888;">Loading...</td></tr>
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

    <div id="igRemoveModal" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 9999; justify-content: center; align-items: center; padding: 20px;">
        <div style="background: white; border-radius: 16px; width: 100%; max-width: 480px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.2);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                <h3 style="margin: 0; color: var(--danger);">Delete this Instagram routing connection?</h3>
                <button onclick="closeIgRemoveModal()" style="background: none; border: none; font-size: 1.4rem; cursor: pointer;">&times;</button>
            </div>
            <div id="igRemoveModalDetails" style="background:#F7F5F3; border:1px solid var(--border); border-radius:10px; padding:12px; font-size:0.82rem; line-height:1.6; margin-bottom:12px;"></div>
            <div style="background:#FEF2F2; border:1px solid #FCA5A5; border-radius:10px; padding:10px 12px; font-size:0.8rem; color:#991B1B; margin-bottom:16px;">
                This only removes this Instagram routing connection. It will not delete the Instagram content, Topic, Menu, products, Custom Q&amp;A, or customer conversations.
            </div>
            <div style="display: flex; gap: 8px; justify-content: flex-end;">
                <button class="btn-secondary" onclick="closeIgRemoveModal()">Cancel</button>
                <button class="btn-danger" onclick="confirmIgRemove()">Delete Routing</button>
            </div>
        </div>
    </div>

    <!-- Custom Q&A: Add / Edit modal. One learned_knowledge record per
         modal session -- editing always PATCHes the same id, never
         creates a new one. -->
    <div id="customQaModal" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 9999; justify-content: center; align-items: center; padding: 20px; overflow-y: auto;">
        <div style="background: white; border-radius: 16px; width: 100%; max-width: 640px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); max-height: 90vh; overflow-y: auto;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                <h3 style="margin: 0; color: var(--primary);" id="customQaModalTitle">+ Add Q&amp;A</h3>
                <button onclick="closeCustomQaModal()" style="background: none; border: none; font-size: 1.4rem; cursor: pointer;">&times;</button>
            </div>
            <input type="hidden" id="cqaEditingId">

            <div style="margin-bottom:12px;">
                <label class="field-label">Topic / Category</label>
                <select class="field-control" id="cqaTopicSelect" onchange="onCqaTopicSelectChange()">
                    <option>Pricing</option><option>Delivery</option><option>Corporate Gifting</option>
                    <option>Meal Subscription</option><option>Sondesh</option><option>Events</option>
                    <option>Careers</option><option>Other</option>
                </select>
                <input type="text" class="field-control" id="cqaTopicCustom" placeholder="Type a topic…" style="display:none; margin-top:6px;">
            </div>

            <div style="margin-bottom:12px;">
                <label class="field-label">Primary Customer Question / Trigger *</label>
                <input type="text" class="field-control" id="cqaPrimaryQuestion" placeholder="What is the price per meal?">
            </div>

            <div style="margin-bottom:12px;">
                <label class="field-label">Alternative Questions / Aliases</label>
                <div id="cqaAliasList"></div>
                <div style="display:flex; gap:8px; margin-top:6px;">
                    <input type="text" class="field-control" id="cqaAliasInput" placeholder="meal price">
                    <button class="btn-secondary" type="button" onclick="addCqaAlias()">+ Add Alternative Question</button>
                </div>
            </div>

            <div style="margin-bottom:12px;">
                <label class="field-label">Approved Answer *</label>
                <textarea class="field-control" id="cqaAnswer" rows="4" placeholder="The exact reply the bot should send."></textarea>
            </div>

            <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:12px;">
                <div>
                    <label class="field-label">Priority</label>
                    <select class="field-control" id="cqaPriority"><option value="NORMAL">Normal</option><option value="HIGH">High</option></select>
                </div>
                <div>
                    <label class="field-label">Status</label>
                    <select class="field-control" id="cqaStatus"><option value="ACTIVE">Active</option><option value="DISABLED">Disabled</option><option value="ARCHIVED">Archived</option></select>
                </div>
            </div>

            <p style="font-size:0.78rem; color:var(--text-muted); margin-bottom:16px;">Source: <span id="cqaSourceLabel">Created by Client</span></p>

            <div id="cqaWarningsBox" style="display:none; background:#FFF7ED; border:1px solid #FED7AA; border-radius:10px; padding:12px; margin-bottom:14px; font-size:0.84rem;"></div>

            <div style="border-top:1px solid var(--border); padding-top:14px; margin-bottom:14px;">
                <label class="field-label">Test Question <span style="font-weight:400; color:var(--text-muted);">(optional, before saving)</span></label>
                <div style="display:flex; gap:8px;">
                    <input type="text" class="field-control" id="cqaTestQuestion" placeholder="How much is one meal?">
                    <button class="btn-secondary" type="button" onclick="runCqaTest()">Test</button>
                    <button class="btn-secondary" type="button" onclick="runCqaPreview()">Preview</button>
                </div>
                <div id="cqaTestResult" style="margin-top:10px; font-size:0.84rem;"></div>
            </div>

            <div style="display: flex; gap: 8px; justify-content: flex-end;">
                <button class="btn-secondary" onclick="closeCustomQaModal()">Cancel</button>
                <button class="btn-primary" onclick="saveCustomQa()">Save Q&amp;A</button>
            </div>
        </div>
    </div>

    <!-- Custom Q&A: delete confirmation. Permanent -- distinct from Disable/Archive. -->
    <div id="customQaDeleteModal" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 9999; justify-content: center; align-items: center; padding: 20px;">
        <div style="background: white; border-radius: 16px; width: 100%; max-width: 480px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.2);">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                <h3 style="margin: 0; color: var(--danger);">Delete this Custom Q&amp;A permanently?</h3>
                <button onclick="closeCustomQaDeleteModal()" style="background: none; border: none; font-size: 1.4rem; cursor: pointer;">&times;</button>
            </div>
            <div id="cqaDeleteDetails" style="background:#F7F5F3; border:1px solid var(--border); border-radius:10px; padding:12px; font-size:0.82rem; line-height:1.6; margin-bottom:12px;"></div>
            <div style="background:#FEF2F2; border:1px solid #FCA5A5; border-radius:10px; padding:10px 12px; font-size:0.8rem; color:#991B1B; margin-bottom:16px;">
                This permanently removes this Q&amp;A and it will no longer be available to the chatbot.
            </div>
            <div style="display: flex; gap: 8px; justify-content: flex-end;">
                <button class="btn-secondary" onclick="closeCustomQaDeleteModal()">Cancel</button>
                <button class="btn-danger" onclick="confirmCustomQaDelete()">Delete Permanently</button>
            </div>
        </div>
    </div>

    <!-- Custom Q&A: version history. Newest first; Restore snapshots the
         current state before applying the historical one. -->
    <div id="customQaHistoryModal" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 9999; justify-content: center; align-items: center; padding: 20px;">
        <div style="background: white; border-radius: 16px; width: 100%; max-width: 620px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); max-height: 85vh; overflow-y: auto;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                <h3 style="margin: 0; color: var(--primary);">History</h3>
                <button onclick="closeCustomQaHistoryModal()" style="background: none; border: none; font-size: 1.4rem; cursor: pointer;">&times;</button>
            </div>
            <div id="cqaHistoryBody"></div>
        </div>
    </div>

    <!-- Custom Q&A: CSV import, three steps -- upload, dry-run report,
         confirm. Nothing is saved until Confirm Import is pressed, and
         confirm re-validates the same file rather than trusting client
         state from the dry run. -->
    <div id="customQaImportModal" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 9999; justify-content: center; align-items: center; padding: 20px;">
        <div style="background: white; border-radius: 16px; width: 100%; max-width: 680px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); max-height: 85vh; overflow-y: auto;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                <h3 style="margin: 0; color: var(--primary);">Import CSV</h3>
                <button onclick="closeCustomQaImportModal()" style="background: none; border: none; font-size: 1.4rem; cursor: pointer;">&times;</button>
            </div>
            <p style="font-size:0.82rem; color:var(--text-muted);">
                <a href="#" onclick="downloadCustomQaTemplate(); return false;">Download CSV Template</a>
            </p>
            <input type="file" id="cqaImportFile" accept=".csv" class="field-control" style="margin-bottom:12px;">
            <div style="display:flex; gap:8px; margin-bottom:14px;">
                <button class="btn-secondary" onclick="runCustomQaImportDryRun()">Validate (Dry Run)</button>
                <button class="btn-primary" id="cqaImportConfirmBtn" style="display:none;" onclick="confirmCustomQaImport()">Confirm Import</button>
            </div>
            <div id="cqaImportSummary" style="font-size:0.85rem; margin-bottom:10px;"></div>
            <div style="overflow-x:auto;">
                <table class="data-table">
                    <thead><tr><th>Row</th><th>Question</th><th>Status</th><th>Notes</th></tr></thead>
                    <tbody id="cqaImportRows"><tr><td colspan="4" style="text-align:center; color:#888;">Upload a CSV and click Validate.</td></tr></tbody>
                </table>
            </div>
        </div>
    </div>

    <div id="topicModal" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 9999; justify-content: center; align-items: center; padding: 20px;">
        <div style="background: white; border-radius: 16px; width: 100%; max-width: 520px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); max-height: 90vh; overflow-y: auto;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                <h3 style="margin: 0; color: var(--primary);" id="topicModalTitle">+ Add Topic</h3>
                <button onclick="closeTopicModal()" style="background: none; border: none; font-size: 1.4rem; cursor: pointer;">&times;</button>
            </div>
            <input type="hidden" id="topicModalEditingId">
            <label class="field-label">Topic Name *</label>
            <input class="field-control" id="topicName" placeholder="e.g. Wedding / Event Sondesh" style="margin-bottom:10px;">
            <label class="field-label">Description *</label>
            <textarea class="field-control" id="topicDescription" rows="3" placeholder="Shown to customers who ask a vague question after sharing this content." style="margin-bottom:10px;"></textarea>
            <label class="field-label">Default Menu *</label>
            <select class="field-control" id="topicDefaultMenu" style="margin-bottom:10px;"><option value="">Select Menu</option></select>
            <label class="field-label">Reply Order</label>
            <select class="field-control" id="topicResponseOrder" style="margin-bottom:10px;"><option value="description_first">Description First</option><option value="menu_first">Menu First</option></select>
            <label style="display:flex; align-items:center; gap:6px; font-size:0.85rem; margin-bottom:10px;"><input type="checkbox" id="topicActive" checked> Active</label>
            <details><summary style="cursor:pointer; font-size:0.85rem; margin-bottom:8px;"><b>Advanced</b> — Priority Products, Knowledge Scope</summary>
                <div style="margin-top:8px;">
                    <label class="field-label">Priority Products</label>
                    <div style="display:flex; gap:8px;"><select class="field-control" id="topicProductPicker"></select><button class="btn-secondary" type="button" onclick="addTopicPriorityProduct()">+ Add</button></div>
                    <div id="topicPriorityProducts" class="ad-route-list" style="margin-top:8px;"></div>
                    <label class="field-label" style="margin-top:10px;">Knowledge Scope</label>
                    <select class="field-control" id="topicKnowledgeScope" multiple></select>
                </div>
            </details>
            <div style="display: flex; gap: 8px; justify-content: flex-end; margin-top:16px;">
                <button class="btn-secondary" onclick="closeTopicModal()">Cancel</button>
                <button class="btn-primary" onclick="submitTopic()">Save Topic</button>
            </div>
            <div id="topicModalResult" class="result-box"></div>
        </div>
    </div>

    <div id="menuModal" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 9999; justify-content: center; align-items: center; padding: 20px;">
        <div style="background: white; border-radius: 16px; width: 100%; max-width: 480px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); max-height: 90vh; overflow-y: auto;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                <h3 style="margin: 0; color: var(--primary);">+ Add New Menu</h3>
                <button onclick="closeMenuModal()" style="background: none; border: none; font-size: 1.4rem; cursor: pointer;">&times;</button>
            </div>
            <label class="field-label">Menu Name *</label>
            <input class="field-control" id="menuName" placeholder="e.g. Meal Subscription" style="margin-bottom:10px;">
            <label class="field-label">Menu Type / Category</label>
            <select class="field-control" id="menuCategory" style="margin-bottom:10px;"></select>
            <label class="field-label">Customer Description *</label>
            <textarea class="field-control" id="menuDescription" rows="3" placeholder="e.g. Weekly Bengali home-style meal subscription with rotating menus." style="margin-bottom:10px;"></textarea>
            <label class="field-label">Menu Image *</label>
            <input type="file" id="menuImageFile" accept="image/jpeg,image/png,image/webp" style="margin-bottom:10px;">
            <label style="display:flex; align-items:center; gap:6px; font-size:0.85rem; margin-bottom:10px;"><input type="checkbox" id="menuActive" checked> Active</label>
            <div style="display: flex; gap: 8px; justify-content: flex-end;">
                <button class="btn-secondary" onclick="closeMenuModal()">Cancel</button>
                <button class="btn-primary" onclick="submitMenu()">Create Menu</button>
            </div>
            <div id="menuModalResult" class="result-box"></div>
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
                loadActiveMenu();
                loadAdRoutes();
                loadAnalytics();
                refreshCandidateBadge();
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
                    loadActiveMenu();
                    loadAdRoutes();
                    loadAnalytics();
                    refreshCandidateBadge();
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

        // Eight tabs needed 1655px of nav on a 347px phone, so six of them were
        // simply invisible to a kitchen working off a handset. Panels are now
        // grouped three ways and a tab shows its whole group stacked. Each tab
        // is named after the panel it leads with, so the panels themselves did
        // not have to move.
        const TAB_GROUPS = {
            'tab-image': 'menu',
            'tab-menu-manager': 'menu_manager',
            'tab-stats': 'customers',
            'tab-candidates': 'answers',
            'tab-ai-resolution': 'ai_resolution',
            'tab-custom-qa': 'custom_qa'
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
            if (group === 'menu_manager') { mmLoadMenuList(); }
            if (group === 'customers') { loadAnalytics(); loadConversations(); }
            if (group === 'answers') { loadCandidates(); loadKnowledge(); }
            if (group === 'ai_resolution') { loadAiResolution(); loadTriageShadow(); }
            if (group === 'custom_qa') { loadCustomQa(); loadAssistedEvaluation(); }
        }

        // MENU MANAGER tab: every menu is edited as a draft (server-side, in
        // menu_drafts) and only becomes the chatbot's live answer when Save &
        // Publish is pressed — camellia/menu_manager.py owns validation,
        // rendering and the reload, this is only its HTTP-backed editor.
        let mmMenus = [];
        let mmCurrentKey = null;
        let mmCurrentKind = null;
        let mmItems = [];

        async function mmLoadMenuList() {
            const list = document.getElementById('mmMenuList');
            if (!list || !currentAdminKey) return;
            try {
                const resp = await fetch('/admin/menu-manager/menus', { headers: getAuthHeaders() });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not load menus.');
                mmMenus = data.menus || [];
                mmRenderImageGallery();
                list.innerHTML = mmMenus.map(m => `
                    <button class="catalogue-item" type="button" style="display:block; width:100%; text-align:left; margin-bottom:8px;" onclick="mmOpenMenu('${m.menu_key}')">
                        <b>${escapeHtml(m.label)}</b>
                        <small>${m.active_item_count}/${m.item_count} active item(s)${m.has_draft ? ' · <span style="color:var(--primary); font-weight:700;">unsaved draft</span>' : ''}</small>
                    </button>`).join('');
                if (mmCurrentKey) mmOpenMenu(mmCurrentKey);
            } catch (err) {
                list.innerHTML = `<p style="color: var(--danger);">${escapeHtml(err.message || err)}</p>`;
            }
        }

        // The image gallery is deliberately its own fetch, independent of
        // mmMenus (the structured item/price editor list, which only ever
        // covers the five built-in menus that have a MANAGEABLE_MENUS entry).
        // A menu created purely through "+ Add New Menu" has no item list at
        // all — just a name, category, description and image — so it would
        // never appear here if this reused mmMenus. Fetching the dynamic
        // catalogue directly means every menu, old or brand new, shows up.
        async function mmRenderImageGallery() {
            const grid = document.getElementById('mmImageGrid');
            if (!grid || !currentAdminKey) return;
            try {
                const resp = await fetch('/admin/menu-manager/images', { headers: getAuthHeaders() });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not load menu images.');
                const menus = data.menus || [];
                grid.innerHTML = menus.map(m => {
                    const image = m.image_url ? `<img src="${escapeHtml(m.image_url)}" alt="${escapeHtml(m.title)}">` : '<div style="height:210px;display:grid;place-items:center;background:#f6f3ef;color:#888">No image published</div>';
                    const key = encodeURIComponent(m.menu_key);
                    return `<article class="visual-menu-card"><h3>${escapeHtml(m.title)}${m.category ? ` <span style="font-weight:400;font-size:0.75rem;color:var(--text-muted);">· ${escapeHtml(m.category)}</span>` : ''}</h3>${image}<div class="visual-menu-meta"><b>${escapeHtml(m.status || 'Active')}</b>${m.published_at ? ` · ${escapeHtml(m.published_at)}` : ''}</div><div class="visual-menu-actions"><button class="btn-primary" onclick="mmReplaceImage('${key}')">Replace</button><button class="btn-secondary" onclick="mmPreviewImage('${key}')">Preview</button><button class="btn-secondary" onclick="mmHistoryImage('${key}')">History</button><button class="btn-danger" onclick="mmArchiveImage('${key}')">Archive</button></div><div id="mm-image-history-${key}" style="padding:0 14px 12px"></div></article>`;
                }).join('');
            } catch (err) {
                grid.innerHTML = `<p style="color: var(--danger);">${escapeHtml(err.message || err)}</p>`;
            }
        }

        function mmReplaceImage(key) { const input=document.createElement('input'); input.type='file'; input.accept='image/jpeg,image/png,image/webp'; input.onchange=async()=>{const file=input.files&&input.files[0];if(!file)return;const form=new FormData();form.append('file',file);const response=await fetch(`/admin/menu-manager/menus/${key}/image/draft`,{method:'POST',headers:getAuthHeaders(),body:form});const data=await response.json();const msg=document.getElementById('mmMessage');msg.style.display='block';msg.className=response.ok?'result-box info':'result-box error';msg.innerText=response.ok?'Draft image saved. Preview it, then publish when ready.':(data.detail||'Upload failed.');if(response.ok)mmLoadMenuList();};input.click(); }
        async function mmPreviewImage(key){const response=await fetch(`/admin/menu-manager/menus/${key}/image/preview`,{headers:getAuthHeaders()});const data=await response.json();const msg=document.getElementById('mmMessage');msg.style.display='block';msg.className='result-box info';msg.innerHTML=response.ok?`<b>Current</b><br><img src="${escapeHtml(data.current||'')}" style="max-width:220px;max-height:260px"><br><b>New draft</b><br>${data.draft?`<img src="${escapeHtml(data.draft.image_path)}" style="max-width:220px;max-height:260px">`:'No draft saved.'}`:escapeHtml(data.detail||'Preview failed');}
        async function mmPublishImage(key){const response=await fetch(`/admin/menu-manager/menus/${key}/image/publish`,{method:'POST',headers:getAuthHeaders()});const data=await response.json();const msg=document.getElementById('mmMessage');msg.style.display='block';msg.className=response.ok?'result-box success':'result-box error';msg.innerText=response.ok?'Published. Customers receive the new image immediately.':(data.detail||'Publish failed.');if(response.ok)mmLoadMenuList();}
        async function mmArchiveImage(key){if(!confirm('Are you sure you want to remove this menu from customer view?'))return;const response=await fetch(`/admin/menu-manager/menus/${key}/image/archive`,{method:'POST',headers:getAuthHeaders()});const data=await response.json();const msg=document.getElementById('mmMessage');msg.style.display='block';msg.className='result-box info';msg.innerText=response.ok?'Menu archived; it remains in History.':(data.detail||'Archive failed.');if(response.ok)mmLoadMenuList();}
        async function mmHistoryImage(key){const response=await fetch(`/admin/menu-manager/menus/${key}/image/history`,{headers:getAuthHeaders()});const data=await response.json();const box=document.getElementById(`mm-image-history-${key}`);box.innerHTML=(data.versions||[]).map(v=>`<div style="margin:7px 0"><img src="${escapeHtml(v.image_path)}" style="width:70px;height:50px;object-fit:cover;vertical-align:middle;border-radius:4px"><span style="font-size:.78rem">Version ${v.version} — ${escapeHtml(v.status)}</span>${v.status!=='current'?` <button class="btn-secondary" onclick="mmRestoreImage('${key}',${v.id})">Restore</button>`:''}</div>`).join('')||'<small>No image history yet.</small>';}
        async function mmRestoreImage(key,versionId){const response=await fetch(`/admin/menu-manager/menus/${key}/image/restore/${versionId}`,{method:'POST',headers:getAuthHeaders()});const data=await response.json();const msg=document.getElementById('mmMessage');msg.style.display='block';msg.className=response.ok?'result-box success':'result-box error';msg.innerText=response.ok?'Previous menu restored.':(data.detail||'Restore failed.');if(response.ok)mmLoadMenuList();}

        async function mmOpenMenu(menuKey) {
            const msg = document.getElementById('mmMessage');
            msg.style.display = 'none';
            document.getElementById('mmPreviewBox').style.display = 'none';
            try {
                const resp = await fetch(`/admin/menu-manager/menus/${encodeURIComponent(menuKey)}`, { headers: getAuthHeaders() });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not load this menu.');
                mmCurrentKey = menuKey;
                mmCurrentKind = data.kind;
                mmItems = data.draft || data.published || [];
                document.getElementById('mmEditorTitle').innerText = data.label + (data.draft ? ' (editing draft)' : '');
                mmRenderItems();
                mmLoadVersions();
            } catch (err) {
                msg.style.display = 'block';
                msg.className = 'result-box error';
                msg.innerText = err.message || String(err);
            }
        }

        function mmRenderItems() {
            const container = document.getElementById('mmEditorItems');
            if (!mmCurrentKey) { container.innerHTML = ''; return; }
            if (mmCurrentKind === 'collection') {
                container.innerHTML = mmItems.map((item, i) => `
                    <div class="collection-slot">
                        <label>Flavour / item name</label>
                        <input type="text" class="collection-select" value="${escapeHtml(item.flavour || '')}" onchange="mmItems[${i}].flavour=this.value">
                        <div style="display:flex; gap:8px; margin-top:6px; flex-wrap:wrap;">
                            ${['250g','500g','1kg'].map(size => `
                                <label style="font-size:0.76rem;">${size} price
                                    <input type="number" style="width:90px;" value="${(item.prices && item.prices[size]) || ''}"
                                        onchange="mmSetPrice(${i}, '${size}', this.value)">
                                </label>`).join('')}
                        </div>
                        <label style="display:flex; align-items:center; gap:6px; margin-top:6px; font-size:0.8rem;">
                            <input type="checkbox" ${item.active !== false ? 'checked' : ''} onchange="mmItems[${i}].active=this.checked">
                            Active (unchecked items are kept here but hidden from customers)
                        </label>
                        <button class="btn-danger" type="button" style="margin-top:6px;" onclick="mmRemoveItem(${i})">Remove</button>
                    </div>`).join('') || '<p style="color: var(--text-muted);">No items yet — use + Add item.</p>';
            } else {
                container.innerHTML = mmItems.map((item, i) => `
                    <div class="collection-slot">
                        <label>Dish name</label>
                        <input type="text" class="collection-select" value="${escapeHtml(item.item || '')}" onchange="mmItems[${i}].item=this.value">
                        <label style="margin-top:6px;">Price (₹)</label>
                        <input type="number" class="collection-select" value="${item.price_inr || ''}" onchange="mmItems[${i}].price_inr=Number(this.value)">
                        <label style="margin-top:6px;">Description</label>
                        <input type="text" class="collection-select" value="${escapeHtml(item.description || '')}" onchange="mmItems[${i}].description=this.value">
                        <label style="margin-top:6px;">Quantity / pack</label>
                        <input type="text" class="collection-select" value="${escapeHtml(item.quantity || '')}" onchange="mmItems[${i}].quantity=this.value">
                        <button class="btn-danger" type="button" style="margin-top:6px;" onclick="mmRemoveItem(${i})">Remove</button>
                    </div>`).join('') || '<p style="color: var(--text-muted);">No items yet — use + Add item.</p>';
            }
        }

        function mmSetPrice(index, size, value) {
            const item = mmItems[index];
            item.prices = item.prices || {};
            const num = Number(value);
            if (value === '' || Number.isNaN(num)) { delete item.prices[size]; }
            else { item.prices[size] = num; }
        }

        function mmAddItem() {
            if (!mmCurrentKey) return alert('Choose a menu first.');
            mmItems.push(mmCurrentKind === 'collection'
                ? { flavour: '', active: true, prices: {} }
                : { item: '', price_inr: 0 });
            mmRenderItems();
        }

        function mmRemoveItem(index) {
            mmItems.splice(index, 1);
            mmRenderItems();
        }

        function mmShowMessage(kind, text) {
            const msg = document.getElementById('mmMessage');
            msg.style.display = 'block';
            msg.className = `result-box ${kind}`;
            msg.innerText = text;
        }

        async function mmSaveDraft() {
            if (!mmCurrentKey) return;
            try {
                const resp = await fetch(`/admin/menu-manager/menus/${encodeURIComponent(mmCurrentKey)}/draft`, {
                    method: 'PUT', headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
                    body: JSON.stringify({ items: mmItems })
                });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not save draft.');
                mmShowMessage('success', 'Draft saved. The live chatbot menu is unchanged until you Save & Publish.');
                mmLoadMenuList();
            } catch (err) {
                mmShowMessage('error', err.message || String(err));
            }
        }

        async function mmPreview() {
            if (!mmCurrentKey) return;
            try {
                await mmSaveDraft();
                const resp = await fetch(`/admin/menu-manager/menus/${encodeURIComponent(mmCurrentKey)}/preview`, { headers: getAuthHeaders() });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not build preview.');
                document.getElementById('mmPreviewBox').style.display = 'block';
                document.getElementById('mmPreviewText').innerText = data.customer_facing_text;
            } catch (err) {
                mmShowMessage('error', err.message || String(err));
            }
        }

        async function mmPublish() {
            if (!mmCurrentKey) return;
            if (!confirm('Publish this menu? Customers and the chatbot will see it immediately.')) return;
            try {
                const resp = await fetch(`/admin/menu-manager/menus/${encodeURIComponent(mmCurrentKey)}/publish`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
                    body: JSON.stringify({ items: mmItems })
                });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not publish.');
                mmShowMessage('success', `Published as version ${data.version_id}. The chatbot is using this menu now.`);
                mmLoadMenuList();
            } catch (err) {
                mmShowMessage('error', err.message || String(err));
            }
        }

        async function mmCancelDraft() {
            if (!mmCurrentKey) return;
            try {
                await fetch(`/admin/menu-manager/menus/${encodeURIComponent(mmCurrentKey)}/draft`, {
                    method: 'DELETE', headers: getAuthHeaders()
                });
                mmShowMessage('info', 'Draft discarded.');
                mmOpenMenu(mmCurrentKey);
                mmLoadMenuList();
            } catch (err) {
                mmShowMessage('error', err.message || String(err));
            }
        }

        async function mmLoadVersions() {
            const box = document.getElementById('mmVersions');
            if (!mmCurrentKey) { box.innerHTML = ''; return; }
            try {
                const resp = await fetch(`/admin/menu-manager/menus/${encodeURIComponent(mmCurrentKey)}/versions`, { headers: getAuthHeaders() });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not load version history.');
                const versions = data.versions || [];
                if (!versions.length) { box.innerHTML = '<p style="color: var(--text-muted); font-size: 0.8rem;">No published versions yet.</p>'; return; }
                box.innerHTML = '<h3 style="color: var(--primary); font-size: 0.95rem;">Version history</h3>' + versions.map(v => `
                    <div class="collection-slot" style="display:flex; justify-content:space-between; align-items:center;">
                        <span style="font-size:0.8rem;">#${v.id} · ${escapeHtml(v.action)} · ${escapeHtml(v.changed_at)} · ${escapeHtml(v.changed_by || 'admin')}</span>
                        <button class="btn-secondary" type="button" onclick="mmRestoreVersion(${v.id})">Restore</button>
                    </div>`).join('');
            } catch (err) {
                box.innerHTML = `<p style="color: var(--danger); font-size: 0.8rem;">${escapeHtml(err.message || err)}</p>`;
            }
        }

        async function mmRestoreVersion(versionId) {
            if (!mmCurrentKey) return;
            if (!confirm(`Restore version #${versionId}? This publishes it immediately.`)) return;
            try {
                const resp = await fetch(`/admin/menu-manager/menus/${encodeURIComponent(mmCurrentKey)}/versions/${versionId}/restore`, {
                    method: 'POST', headers: getAuthHeaders()
                });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not restore this version.');
                mmShowMessage('success', `Restored and published as version ${data.version_id}.`);
                mmOpenMenu(mmCurrentKey);
                mmLoadMenuList();
            } catch (err) {
                mmShowMessage('error', err.message || String(err));
            }
        }

        let igProducts = [];
        let igPriority = [];
        let igEditingId = null;
        let igTopics = [];
        let igMenus = [];
        let igResponseOrder = 'description_first';

        // Topic and Menu dropdowns are populated purely from the backend —
        // no hardcoded list — so a Topic or Menu created just now is
        // immediately selectable everywhere it belongs, no restart needed.
        async function loadTopicsAndMenus() {
            try {
                const [topicsResp, menusResp] = await Promise.all([
                    fetch('/admin/instagram-topics', { headers: getAuthHeaders() }),
                    fetch('/admin/menu-catalogue', { headers: getAuthHeaders() }),
                ]);
                const topicsData = await topicsResp.json();
                const menusData = await menusResp.json();
                igTopics = topicsData.topics || [];
                igMenus = (menusData.menus || []).filter(m => m.active);
                window.igMenuCategories = menusData.categories || [];

                const topicOptions = '<option value="">Select Topic</option>' + igTopics.map(t =>
                    `<option value="${escapeHtml(t.topic_id)}">${escapeHtml(t.name)}</option>`).join('');
                document.getElementById('igTopicPicker').innerHTML = topicOptions;

                const menuOptions = '<option value="">No menu — description only</option>' + igMenus.map(m =>
                    `<option value="${escapeHtml(m.menu_id)}">${escapeHtml(m.name)}</option>`).join('');
                document.getElementById('igMenuPicker').innerHTML = menuOptions;
                const topicDefaultMenuEl = document.getElementById('topicDefaultMenu');
                if (topicDefaultMenuEl) topicDefaultMenuEl.innerHTML = menuOptions;
            } catch (err) { /* dropdowns just stay empty; the rest of the tab still works */ }
        }

        function onIgTopicChange(preservePriority = false) {
            const topicId = document.getElementById('igTopicPicker').value;
            const topic = igTopics.find(t => t.topic_id === topicId);
            igResponseOrder = topic && topic.response_order === 'menu_first' ? 'menu_first' : 'description_first';
            const editTopicButton = document.getElementById('igEditTopicButton');
            if (editTopicButton) editTopicButton.disabled = !topic;
            document.getElementById('igTopicDescription').value = topic ? (topic.description || '') : '';
            // A new Topic owns the normal defaults. Edit mode passes true so
            // an existing route override is preserved until the client changes
            // it in Customize.
            if (topic && !preservePriority) {
                igPriority = [...topic.priority_products];
                renderIgPriorityProducts();
            }
            renderIgTopicPreview();
        }

        function renderIgTopicPreview() {
            const box = document.getElementById('igTopicPreview');
            const topic = igTopics.find(t => t.topic_id === document.getElementById('igTopicPicker').value);
            if (!box || !topic) { if (box) box.style.display = 'none'; return; }
            const menuId = document.getElementById('igMenuPicker').value;
            const menu = igMenus.find(item => item.menu_id === menuId);
            const labels = igPriority.map(id => {
                const product = igProducts.find(item => item.id === id);
                return product ? product.label : id;
            });
            document.getElementById('igTopicPreviewName').innerText = topic.name || '';
            document.getElementById('igTopicPreviewMenu').innerText = menu ? menu.name : 'No menu selected';
            document.getElementById('igTopicPreviewPriority').innerText = labels.length ? labels.join(' → ') : 'Normal conversation routing';
            document.getElementById('igTopicPreviewDescription').innerText = document.getElementById('igTopicDescription').value || '—';
            document.getElementById('igTopicPreviewOrder').innerText = igResponseOrder === 'menu_first' ? 'Menu First' : 'Description First';
            box.style.display = 'block';
        }

        function swapIgResponseOrder() {
            if (!document.getElementById('igTopicPicker').value) return;
            igResponseOrder = igResponseOrder === 'menu_first' ? 'description_first' : 'menu_first';
            renderIgTopicPreview();
            previewInstagramRoute();
        }

        function renderIgPriorityProducts() {
            const box = document.getElementById('igPriorityProducts');
            if (!box) return;
            if (!igPriority.length) { box.innerHTML = '<small>No priority products. Normal conversation routing will apply.</small>'; renderIgTopicPreview(); return; }
            box.innerHTML = igPriority.map((id, index) => {
                const product = igProducts.find(item => item.id === id);
                const label = product ? product.label : `${id} (currently unavailable)`;
                return `<div class="ad-route-row"><span><b>${index + 1}.</b> ${escapeHtml(label)}</span><span>
                    <button class="btn-secondary" type="button" onclick="moveIgPriority(${index},-1)" ${index === 0 ? 'disabled' : ''}>↑</button>
                    <button class="btn-secondary" type="button" onclick="moveIgPriority(${index},1)" ${index === igPriority.length - 1 ? 'disabled' : ''}>↓</button>
                    <button class="btn-danger" type="button" onclick="removeIgPriority(${index})">Remove</button></span></div>`;
            }).join('');
            renderIgTopicPreview();
        }

        function addIgPriorityProduct() {
            const id = document.getElementById('igProductPicker').value;
            if (!id || igPriority.includes(id)) return;
            igPriority.push(id); renderIgPriorityProducts();
        }
        function moveIgPriority(index, change) { const target=index+change; if(target<0||target>=igPriority.length)return; [igPriority[index],igPriority[target]]=[igPriority[target],igPriority[index]]; renderIgPriorityProducts(); }
        function removeIgPriority(index) { igPriority.splice(index,1); renderIgPriorityProducts(); }

        function resetInstagramRouteForm() {
            igEditingId = null; igPriority = [];
            ['igRouteUrl','igMediaId','igAdId','igCampaignId'].forEach(id => { const el=document.getElementById(id); if(el)el.value=''; });
            document.getElementById('igTopicPicker').value = '';
            igResponseOrder = 'description_first';
            document.getElementById('igMenuPicker').value = '';
            document.getElementById('igTopicDescription').value = '';
            document.getElementById('igEscalationPolicy').value='human_if_unsupported';
            Array.from(document.getElementById('igKnowledgeScope').options).forEach(option => option.selected=false);
            document.getElementById('igRouteIdentity').innerHTML='';
            document.getElementById('igPreviewBox').style.display = 'none';
            document.getElementById('igTopicPreview').style.display = 'none';
            document.getElementById('igCustomize').open = false;
            document.getElementById('igAdvancedSettings').open = false;
            const saveButton = document.getElementById('igSaveButton');
            if (saveButton) saveButton.innerText = 'Save Routing';
            const status = document.getElementById('igPostConnectionStatus');
            if (status) status.style.display = 'none';
            renderIgPriorityProducts();
        }

        async function identifyInstagramRoute() {
            const url=document.getElementById('igRouteUrl').value.trim();
            const box=document.getElementById('igRouteIdentity');
            if (!url) { box.innerHTML=''; return; }
            const resp=await fetch('/admin/instagram-routes/identify',{method:'POST',headers:{...getAuthHeaders(),'Content-Type':'application/json'},body:JSON.stringify({url})});
            const data=await resp.json().catch(()=>({}));
            if(!resp.ok){box.innerHTML=`<span style="color:var(--danger);">Could not identify this link. Check the Instagram URL.</span>`;return;}
            document.getElementById('igRouteUrl').value=data.canonical_url;
            if(data.verified_media_identifier) document.getElementById('igMediaId').value=data.verified_media_identifier;
            const detected = data.source_type === 'reel' ? 'Reel' : data.source_type === 'post' ? 'Instagram post' : 'Instagram content';
            box.innerHTML=`<span style="color:#166534;">✓ ${detected} detected</span>`;
        }

        function editInstagramRoute(id) {
            const route=(window.igRoutes||[]).find(item=>item.routing_id===id); if(!route)return;
            igEditingId=id; igPriority=[...(route.priority_products||[])];
            document.getElementById('igRouteUrl').value=route.canonical_url||'';
            document.getElementById('igTopicPicker').value=route.topic_id||'';
            document.getElementById('igMenuPicker').value=route.menu_id||'';
            const topic = igTopics.find(t => t.topic_id === route.topic_id);
            document.getElementById('igTopicDescription').value = topic ? (topic.description || '') : '';
            document.getElementById('igMediaId').value=route.media_id||'';
            document.getElementById('igAdId').value=route.ad_id||'';
            document.getElementById('igCampaignId').value=route.campaign_id||'';
            document.getElementById('igEscalationPolicy').value=route.escalation_policy||'human_if_unsupported';
            Array.from(document.getElementById('igKnowledgeScope').options).forEach(option=>option.selected=(route.knowledge_scope||[]).includes(option.value));
            renderIgPriorityProducts(); onIgTopicChange(true);
            const saveButton = document.getElementById('igSaveButton');
            if (saveButton) saveButton.innerText = 'Save Changes';
            document.getElementById('igRouteUrl').scrollIntoView({behavior:'smooth'});
        }

        async function saveInstagramRoute() {
            const result=document.getElementById('adRouteResult');
            const selectedMenuId=document.getElementById('igMenuPicker').value;
            const payload={routing_id:igEditingId,url:document.getElementById('igRouteUrl').value.trim(),topic_id:document.getElementById('igTopicPicker').value,menu_id:selectedMenuId,menu_mode:selectedMenuId?'SELECTED':'NONE',media_id:document.getElementById('igMediaId').value.trim(),ad_id:document.getElementById('igAdId').value.trim(),campaign_id:document.getElementById('igCampaignId').value.trim(),escalation_policy:document.getElementById('igEscalationPolicy').value,priority_products:igPriority,knowledge_scope:Array.from(document.getElementById('igKnowledgeScope').selectedOptions).map(o=>o.value),active:true};
            const resp=await fetch('/admin/instagram-routes',{method:'POST',headers:{...getAuthHeaders(),'Content-Type':'application/json'},body:JSON.stringify(payload)});
            const data=await resp.json().catch(()=>({})); result.style.display='block';
            if(!resp.ok){result.className='result-box error';result.innerText=data.detail||'Could not save route.';return;}
            const saved = data.route || {};
            const topic = igTopics.find(item => item.topic_id === saved.topic_id) || {};
            const menu = igMenus.find(item => item.menu_id === (saved.menu_id || payload.menu_id)) || {};
            const content = saved.source_type === 'reel' ? 'Reel' : saved.source_type === 'post' ? 'Post' : 'Instagram content';
            const label = saved.shortcode ? `${content} ${saved.shortcode}` : content;
            resetInstagramRouteForm();
            result.style.display='block'; result.className='result-box success';
            result.innerHTML=`<b>✓ Instagram content connected successfully</b><br><small>Instagram content: ${escapeHtml(label)}<br>Topic: ${escapeHtml(topic.name || saved.routing_topic || '—')}<br>Menu: ${escapeHtml(menu.name || saved.menu_id || '—')}</small>`;
            loadAdRoutes();
        }

        async function previewInstagramRoute() {
            const box = document.getElementById('igPreviewBox');
            const topicId = document.getElementById('igTopicPicker').value;
            if (!topicId) { alert('Select a Topic first to preview its response.'); return; }
            box.style.display = 'block';
            box.innerHTML = 'Loading preview…';
            const menuId = document.getElementById('igMenuPicker').value;
            const product = igPriority[0] || '';
            const params = new URLSearchParams({ topic_id: topicId, menu_id: menuId, menu_mode: menuId ? 'SELECTED' : 'NONE', product, response_order: igResponseOrder });
            try {
                const resp = await fetch(`/admin/instagram-routes/preview?${params}`, { headers: getAuthHeaders() });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not build preview.');
                const imageHtml = data.linked_menu_image ? `<img src="${escapeHtml(data.linked_menu_image)}" style="max-width:220px;max-height:220px;border-radius:8px;margin-top:8px;display:block;">` : '';
                box.innerHTML = `
                    <div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:6px;">Reply order: ${escapeHtml(data.response_order === 'menu_first' ? 'Menu First' : 'Description First')}</div>
                    <div style="white-space:pre-wrap; font-family:inherit; font-size:0.85rem; background:#fff; border:1px solid var(--border); border-radius:8px; padding:8px;">${escapeHtml(data.approved_response || '(no approved product template matched yet — add Priority Products)')}</div>
                    ${imageHtml}`;
            } catch (err) {
                box.innerHTML = `<span style="color:var(--danger);">${escapeHtml(err.message || err)}</span>`;
            }
        }

        async function setInstagramRouteActive(id, active) {
            await fetch(`/admin/instagram-routes/${id}/active`,{method:'PATCH',headers:{...getAuthHeaders(),'Content-Type':'application/json'},body:JSON.stringify({active})}); loadAdRoutes();
        }

        // + Add Topic modal. A Topic is a reusable label: create it once here,
        // and it is immediately selectable on this and every future route —
        // no hardcoded list, no restart.
        let topicPriority = [];
        function renderTopicPriorityProducts() {
            const box = document.getElementById('topicPriorityProducts');
            if (!box) return;
            if (!topicPriority.length) { box.innerHTML = '<small>No priority products.</small>'; return; }
            box.innerHTML = topicPriority.map((id, index) => {
                const product = igProducts.find(item => item.id === id);
                const label = product ? product.label : id;
                return `<div class="ad-route-row"><span>${index + 1}. ${escapeHtml(label)}</span><button class="btn-danger" type="button" onclick="topicPriority.splice(${index},1);renderTopicPriorityProducts();">Remove</button></div>`;
            }).join('');
        }
        function addTopicPriorityProduct() {
            const id = document.getElementById('topicProductPicker').value;
            if (!id || topicPriority.includes(id)) return;
            topicPriority.push(id); renderTopicPriorityProducts();
        }

        function openTopicModal(editingId = '') {
            const topic = editingId ? igTopics.find(item => item.topic_id === editingId) : null;
            document.getElementById('topicModalTitle').innerText = topic ? 'Edit Topic' : '+ Add Topic';
            document.getElementById('topicModalEditingId').value = topic ? topic.topic_id : '';
            document.getElementById('topicName').value = '';
            document.getElementById('topicDescription').value = '';
            document.getElementById('topicDefaultMenu').value = '';
            document.getElementById('topicResponseOrder').value = topic && topic.response_order === 'menu_first' ? 'menu_first' : 'description_first';
            document.getElementById('topicActive').checked = true;
            topicPriority = []; renderTopicPriorityProducts();
            Array.from(document.getElementById('topicKnowledgeScope').options).forEach(o => o.selected = false);
            document.getElementById('topicModalResult').style.display = 'none';
            document.getElementById('topicModal').style.display = 'flex';
            if (topic) {
                document.getElementById('topicName').value = topic.name || '';
                document.getElementById('topicDescription').value = topic.description || '';
                document.getElementById('topicDefaultMenu').value = topic.default_menu_id || '';
                document.getElementById('topicActive').checked = topic.active !== false;
                topicPriority = [...(topic.priority_products || [])];
                renderTopicPriorityProducts();
                Array.from(document.getElementById('topicKnowledgeScope').options).forEach(o => o.selected = (topic.knowledge_scope || []).includes(o.value));
            }
        }
        function openTopicEditor() {
            const id = document.getElementById('igTopicPicker').value;
            if (id) openTopicModal(id);
        }
        function closeTopicModal() { document.getElementById('topicModal').style.display = 'none'; }

        async function submitTopic() {
            const result = document.getElementById('topicModalResult');
            const payload = {
                name: document.getElementById('topicName').value.trim(),
                description: document.getElementById('topicDescription').value.trim(),
                default_menu_id: document.getElementById('topicDefaultMenu').value,
                active: document.getElementById('topicActive').checked,
                response_order: document.getElementById('topicResponseOrder').value || 'description_first',
                priority_products: topicPriority,
                knowledge_scope: Array.from(document.getElementById('topicKnowledgeScope').selectedOptions).map(o => o.value),
            };
            try {
                const editingId = document.getElementById('topicModalEditingId').value;
                const resp = await fetch(editingId ? `/admin/instagram-topics/${encodeURIComponent(editingId)}` : '/admin/instagram-topics', {
                    method: editingId ? 'PATCH' : 'POST', headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not save topic.');
                closeTopicModal();
                await loadTopicsAndMenus();
                // The saved topic is selected immediately, and its defaults
                // (including reply order) fill in like any normal selection.
                document.getElementById('igTopicPicker').value = data.topic.topic_id;
                onIgTopicChange();
            } catch (err) {
                result.style.display = 'block';
                result.className = 'result-box error';
                result.innerText = err.message || String(err);
            }
        }

        // + Add New Menu modal. Shared by Instagram Content Routing and the
        // Visual Menu Manager — a menu created from either place appears in
        // both, plus the Topic form's Default Menu dropdown, immediately.
        function openMenuModal() {
            document.getElementById('menuName').value = '';
            document.getElementById('menuDescription').value = '';
            document.getElementById('menuImageFile').value = '';
            document.getElementById('menuActive').checked = true;
            const categories = window.igMenuCategories || ['Sondesh','Weekly Vegetarian','Weekly Non-Vegetarian','Bento / Meal','Subscription','Event','Other'];
            document.getElementById('menuCategory').innerHTML = categories.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
            document.getElementById('menuModalResult').style.display = 'none';
            document.getElementById('menuModal').style.display = 'flex';
        }
        function closeMenuModal() { document.getElementById('menuModal').style.display = 'none'; }

        async function submitMenu() {
            const result = document.getElementById('menuModalResult');
            const name = document.getElementById('menuName').value.trim();
            const description = document.getElementById('menuDescription').value.trim();
            const file = document.getElementById('menuImageFile').files[0];
            if (!name) { result.style.display='block'; result.className='result-box error'; result.innerText='Menu name is required.'; return; }
            if (!description) { result.style.display='block'; result.className='result-box error'; result.innerText='Customer description is required.'; return; }
            if (!file) { result.style.display='block'; result.className='result-box error'; result.innerText='A menu image is required.'; return; }
            const form = new FormData();
            form.append('name', name);
            form.append('category', document.getElementById('menuCategory').value);
            form.append('description', description);
            form.append('active', document.getElementById('menuActive').checked);
            form.append('file', file);
            try {
                const resp = await fetch('/admin/menu-catalogue', { method: 'POST', headers: getAuthHeaders(), body: form });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not create menu.');
                closeMenuModal();
                await loadTopicsAndMenus();
                document.getElementById('igMenuPicker').value = data.menu.menu_id;
                if (typeof mmLoadMenuList === 'function') mmLoadMenuList();
            } catch (err) {
                result.style.display = 'block';
                result.className = 'result-box error';
                result.innerText = err.message || String(err);
            }
        }
        async function archiveInstagramRoute(id) { if(!confirm('Archive this Instagram route? It stops matching customer traffic but stays recoverable from the Archived tab.'))return; await fetch(`/admin/instagram-routes/${id}`,{method:'DELETE',headers:getAuthHeaders()}); loadAdRoutes(); }

        async function restoreInstagramRoute(id) {
            const resp = await fetch(`/admin/instagram-routes/${id}/restore`, { method: 'POST', headers: getAuthHeaders() });
            if (!resp.ok) { alert('Could not restore this route. Please try again.'); return; }
            loadAdRoutes();
        }

        // Instagram Content Routing: Active / Archived tabs. Archived rows get
        // Restore/Remove only — no Edit/Disable/pairing until restored, so an
        // archived route cannot be quietly edited without first bringing it back.
        let igRouteTab = 'active';
        function switchIgRouteTab(tab) {
            igRouteTab = tab;
            document.getElementById('igRouteTabActiveBtn').classList.toggle('active', tab === 'active');
            document.getElementById('igRouteTabArchivedBtn').classList.toggle('active', tab === 'archived');
            loadAdRoutes();
        }

        // Remove is permanent and destructive, so it always goes through this
        // confirmation modal — never a one-click action, and never triggered
        // from the same row position Edit/Disable/Archive already occupy.
        let igRemoveTargetId = null;
        function openIgRemoveModal(id) {
            const route = (window.igRoutes || []).find(r => r.routing_id === id);
            if (!route) return;
            igRemoveTargetId = id;
            const menu = igMenus.find(item => item.menu_id === route.menu_id);
            const sourceType = route.source_type === 'reel' ? 'Reel' : route.source_type === 'post' ? 'Post' : 'Ad';
            const status = route.archived_at ? 'Archived' : route.active ? 'Active' : 'Disabled';
            document.getElementById('igRemoveModalDetails').innerHTML = `
                <div><b>Topic:</b> ${escapeHtml(route.routing_topic || '—')}</div>
                <div><b>Instagram content type:</b> ${sourceType}</div>
                <div><b>Menu:</b> ${escapeHtml((menu || {}).name || route.menu_id || '—')}</div>
                <div><b>Status:</b> ${status}</div>`;
            document.getElementById('igRemoveModal').style.display = 'flex';
        }
        function closeIgRemoveModal() { document.getElementById('igRemoveModal').style.display = 'none'; igRemoveTargetId = null; }

        async function confirmIgRemove() {
            if (!igRemoveTargetId) return;
            const id = igRemoveTargetId;
            try {
                const resp = await fetch(`/admin/instagram-routes/${id}/permanent`, { method: 'DELETE', headers: getAuthHeaders() });
                if (!resp.ok) throw new Error();
                closeIgRemoveModal();
                const result = document.getElementById('adRouteResult');
                result.style.display = 'block';
                result.className = 'result-box success';
                result.innerText = '✓ Instagram routing deleted';
                loadAdRoutes();
            } catch (err) {
                const result = document.getElementById('adRouteResult');
                result.style.display = 'block';
                result.className = 'result-box error';
                result.innerText = 'Could not remove this routing configuration. Please try again.';
            }
        }

        async function loadAdRoutes() {
            const list = document.getElementById('adRouteList');
            if (!list || !currentAdminKey) return;
            try {
                const query = igRouteTab === 'archived' ? '?include_archived=true' : '';
                const resp = await fetch(`/admin/instagram-routes${query}`, { headers: getAuthHeaders() });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not load saved routes.');
                const routes = data.routes || [];
                window.igRoutes=routes; igProducts=data.product_catalogue||[];
                document.getElementById('igProductPicker').innerHTML='<option value="">Choose an approved product…</option>'+igProducts.map(item=>`<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)}</option>`).join('');
                document.getElementById('topicProductPicker') && (document.getElementById('topicProductPicker').innerHTML=document.getElementById('igProductPicker').innerHTML);
                document.getElementById('igKnowledgeScope').innerHTML=(data.knowledge_scopes||[]).map(id=>`<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join('');
                document.getElementById('topicKnowledgeScope') && (document.getElementById('topicKnowledgeScope').innerHTML=document.getElementById('igKnowledgeScope').innerHTML);
                renderIgPriorityProducts();
                if (!igTopics.length && !igMenus.length) await loadTopicsAndMenus();

                if (igRouteTab === 'archived') {
                    if (!routes.length) { list.innerHTML='<p style="color:var(--text-muted);font-size:0.84rem;">No archived routes.</p>'; return; }
                    list.innerHTML = routes.map(route => `
                        <div class="ad-route-row" style="display:block;">
                            <div style="display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; align-items:flex-start;">
                                <span><b>${escapeHtml(route.routing_topic || 'Instagram content')}</b><br><small>${route.source_type === 'reel' ? 'Instagram Reel' : route.source_type === 'post' ? 'Instagram Post' : 'Instagram Ad'} · Archived</small></span>
                                <span><button class="btn-secondary" type="button" onclick="restoreInstagramRoute(${route.routing_id})">Restore</button> <button class="btn-danger" type="button" onclick="openIgRemoveModal(${route.routing_id})">Delete</button></span>
                            </div>
                            <div style="margin-top:8px;"><b>Menu:</b> ${escapeHtml((igMenus.find(m => m.menu_id === route.menu_id) || {}).name || route.menu_id || '—')}</div>
                            <div style="margin-top:4px;"><b>Priority:</b> ${escapeHtml((route.priority_products||[]).map(id => ((igProducts.find(p => p.id === id) || {}).label || id)).join(' → ') || 'Normal conversation routing')}</div>
                        </div>`).join('');
                    return;
                }

                if (!routes.length && !(data.legacy_ad_routes||[]).length) { list.innerHTML='<p style="color:var(--text-muted);font-size:0.84rem;">No Instagram routes added yet.</p>'; return; }
                list.innerHTML = routes.map(route => {
                    const menuName = (igMenus.find(m => m.menu_id === route.menu_id) || {}).name;
                    const sourceLabel = route.source_type==='reel' ? 'Instagram Reel' : route.source_type==='post' ? 'Instagram Post' : 'Instagram Ad';
                    const priorityLabels = (route.priority_products||[]).map(id => ((igProducts.find(p => p.id === id) || {}).label || id));
                    // A post's connection is a client-friendly status, not a raw
                    // Meta id — the id itself stays inside Advanced Settings
                    // (the Media ID field, populated when you press Edit).
                    const postStatus = route.source_type==='post'
                        ? `<div id="igPairingStatus-${route.routing_id}" style="font-size:0.78rem;margin-top:4px;padding:4px 8px;border-radius:6px;display:inline-block;${route.media_id?'background:#ECFDF5;color:#065F46;':'background:#FEF3C7;color:#92400E;'}">${route.media_id?'✓ Post connected':'⚠ Post connection needs verification'}</div>`
                        : '';
                    const connectBtn = route.source_type==='post'
                        ? ` <button class="btn-secondary" type="button" onclick="pairInstagramPost(${route.routing_id})">${route.media_id?'Reconnect Post':'Connect Post'}</button>`
                        : '';
                    const technical = `<details style="margin-top:8px;"><summary style="cursor:pointer; font-size:0.8rem;">View Details</summary><div style="margin-top:6px; font-size:0.78rem; color:var(--text-muted); line-height:1.6;">Shortcode: ${escapeHtml(route.shortcode||'—')}<br>Media ID: ${escapeHtml(route.media_id||'—')}<br>Ad ID: ${escapeHtml(route.ad_id||'—')}<br>Campaign ID: ${escapeHtml(route.campaign_id||'—')}<br>Knowledge scope: ${escapeHtml((route.knowledge_scope||[]).join(', ')||'—')}</div></details>`;
                    return `<div class="ad-route-row" style="display:block;">
                        <div style="display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; align-items:flex-start;">
                            <span><b>${escapeHtml(route.routing_topic || 'Instagram content')}</b><br><small>${sourceLabel}</small></span>
                            <span><button class="btn-secondary" type="button" onclick="editInstagramRoute(${route.routing_id})">Edit</button> <button class="btn-secondary" type="button" onclick="setInstagramRouteActive(${route.routing_id},${!route.active})">${route.active?'Disable':'Enable'}</button> <button class="btn-danger" type="button" onclick="archiveInstagramRoute(${route.routing_id})">Archive</button> <button class="btn-danger" type="button" onclick="openIgRemoveModal(${route.routing_id})">Delete</button>${connectBtn}</span>
                        </div>
                        <div style="margin-top:8px;"><b>Menu:</b> ${escapeHtml(menuName || route.menu_id || '—')}</div>
                        <div style="margin-top:4px;"><b>Priority:</b> ${escapeHtml(priorityLabels.join(' → ') || 'Normal conversation routing')}</div>
                        <div style="margin-top:4px;"><b>Status:</b> ${route.active ? 'Active' : 'Disabled'}</div>
                        ${postStatus}${technical}
                    </div>`;
                }).join('') + (data.legacy_ad_routes||[]).map(route=>`<div class="ad-route-row" style="display:block;"><b>Instagram Ad</b><br><small>Priority: ${escapeHtml(route.product || 'Normal conversation routing')}</small><details style="margin-top:8px;"><summary style="cursor:pointer; font-size:0.8rem;">View Details</summary><div style="margin-top:6px; font-size:0.78rem; color:var(--text-muted);">Ad ID: ${escapeHtml(route.ad_id || '—')}</div></details></div>`).join('');
            } catch (err) {
                list.innerHTML = `<p style="color: var(--danger); font-size: 0.84rem;">${escapeHtml(err.message || err)}</p>`;
            }
        }

        // Shared-post pairing: a post carries no shortcode/URL in Meta's share
        // webhook, so a verified media ID can only be established by an admin
        // deliberately sharing the exact configured post while a short pairing
        // window is open. Starting a window supersedes any other open one —
        // only one route may be mid-pairing at a time.
        let igPairingPollTimer = null;

        async function pairInstagramPost(routingId) {
            const box = document.getElementById(`igPairingStatus-${routingId}`);
            if (!confirm('This starts a 10-minute pairing window and cancels any other route currently pairing.\n\nAfter starting, share the exact configured post into the business Instagram DM from an admin account. Continue?')) return;
            try {
                const resp = await fetch(`/admin/instagram-routes/${routingId}/pairing/start`, { method: 'POST', headers: getAuthHeaders() });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.detail || 'Could not start pairing.');
                if (box) box.innerHTML = `⏳ Pairing window open (expires ${escapeHtml(data.pairing.expires_at)}). Share the exact post into the business DM now.`;
                if (igPairingPollTimer) clearInterval(igPairingPollTimer);
                igPairingPollTimer = setInterval(() => pollInstagramPairing(routingId), 4000);
            } catch (err) {
                if (box) box.innerHTML = `<span style="color:var(--danger);">${escapeHtml(err.message || err)}</span>`;
            }
        }

        async function pollInstagramPairing(routingId) {
            const box = document.getElementById(`igPairingStatus-${routingId}`);
            if (!box) { clearInterval(igPairingPollTimer); return; }
            try {
                const resp = await fetch(`/admin/instagram-routes/${routingId}/pairing`, { headers: getAuthHeaders() });
                const data = await resp.json();
                const pairing = data.pairing;
                if (!pairing) return;
                if (pairing.status === 'bound') {
                    clearInterval(igPairingPollTimer);
                    box.innerHTML = `✅ Post connected`;
                    loadAdRoutes();
                } else if (pairing.status === 'pending') {
                    box.innerHTML = `⏳ Waiting for the shared post (expires ${escapeHtml(pairing.expires_at)}). <button class="btn-secondary" type="button" onclick="cancelInstagramPairing(${routingId})">Cancel</button>`;
                } else {
                    clearInterval(igPairingPollTimer);
                }
            } catch (err) { /* transient; next poll retries */ }
        }

        async function cancelInstagramPairing(routingId) {
            clearInterval(igPairingPollTimer);
            const box = document.getElementById(`igPairingStatus-${routingId}`);
            try {
                await fetch(`/admin/instagram-routes/${routingId}/pairing/cancel`, { method: 'POST', headers: getAuthHeaders() });
                if (box) box.innerText = 'Pairing cancelled.';
            } catch (err) { /* best-effort */ }
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
                        <td style="max-width: 340px; font-size: 0.82rem;">
                            <div id="candidateText-${c.id}" style="white-space:pre-wrap;">${escapeHtml(c.response)}</div>
                            <textarea id="candidateEdit-${c.id}" style="display:none;width:100%;min-height:110px;">${escapeHtml(c.response)}</textarea>
                            ${c.edited ? `<small>Edited by ${escapeHtml(c.edited_by || 'admin')} · ${escapeHtml(c.edited_at || '')}</small>` : ''}
                        </td>
                        <td style="color: ${confColour}; font-weight: 600;">${confidence}%</td>
                        <td><span class="badge badge-veg">${escapeHtml(c.created_by || 'bot')}</span></td>
                        <td style="white-space: nowrap;">
                            <button class="btn-primary" style="padding: 4px 10px; font-size: 0.75rem;" onclick="approveCandidate(${c.id})">✅ Approve</button>
                            <button class="btn-secondary" style="padding:4px 10px;font-size:0.75rem;" id="candidateEditButton-${c.id}" onclick="beginCandidateEdit(${c.id})">✏️ Edit</button>
                            <button class="btn-primary" style="display:none;padding:4px 10px;font-size:0.75rem;" id="candidateSaveButton-${c.id}" onclick="saveCandidateEdit(${c.id})">Save draft</button>
                            <button class="btn-secondary" style="display:none;padding:4px 10px;font-size:0.75rem;" id="candidateCancelButton-${c.id}" onclick="cancelCandidateEdit(${c.id})">Cancel</button>
                            <button class="btn-secondary" style="padding: 4px 10px; font-size: 0.75rem; color: var(--danger);" onclick="rejectCandidate(${c.id})">✕ Reject</button>
                        </td>
                    </tr>`;
                });
                tbody.innerHTML = rows;
            } catch (err) {
                tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: red;">Failed to load suggested answers.</td></tr>';
            }
        }

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

        function beginCandidateEdit(id) {
            document.getElementById(`candidateText-${id}`).style.display = 'none';
            document.getElementById(`candidateEdit-${id}`).style.display = 'block';
            document.getElementById(`candidateEditButton-${id}`).style.display = 'none';
            document.getElementById(`candidateSaveButton-${id}`).style.display = 'inline-block';
            document.getElementById(`candidateCancelButton-${id}`).style.display = 'inline-block';
        }

        function cancelCandidateEdit(id) { loadCandidates(); }

        async function saveCandidateEdit(id) {
            const response = document.getElementById(`candidateEdit-${id}`).value.trim();
            if (!response) return alert('Draft answer cannot be empty.');
            const resp = await fetch(`/admin/candidates/${id}`, {
                method: 'PATCH', headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                body: JSON.stringify({ response, edited_by: 'admin_dashboard' })
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) return alert('Could not save draft: ' + (data.detail || resp.status));
            loadCandidates();
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
                    refreshAiResolutionIfVisible();
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
                    refreshAiResolutionIfVisible();
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
                    refreshAiResolutionIfVisible();
                }
            } catch (e) {
                alert('Error resolving escalation.');
            }
        }

        // AI RESOLUTION & ANSWERS: a merged read of the existing queues
        // (candidate_templates pending/approved/rejected, escalations,
        // knowledge_gaps, learned_knowledge) through their existing
        // endpoints. It introduces no new table and no new customer-facing
        // behaviour — approve/reject/edit/resolve/delete here call the
        // identical endpoints the old Answers and Customers tabs used.
        let aiResolutionFilter = 'needs_attention';
        let aiResolutionItems = [];          // candidates(pending) + escalations(pending) + gaps(open)
        let aiResolutionApproved = [];       // learned_knowledge
        let aiResolutionRejected = [];       // candidates(status=REJECTED)

        function refreshAiResolutionIfVisible() {
            const panel = document.getElementById('tab-ai-resolution');
            if (panel && panel.style.display !== 'none') loadAiResolution();
        }

        function setAiResolutionFilter(filter) {
            aiResolutionFilter = filter;
            ['needs_attention', 'ai_suggested', 'teach_required', 'human_required', 'approved', 'rejected'].forEach(f => {
                const btn = document.getElementById(`aiFilterBtn-${f}`);
                if (btn) btn.classList.toggle('active', f === filter);
            });
            renderAiResolution();
        }

        async function loadAiResolution() {
            const tbody = document.getElementById('aiResolutionTableBody');
            tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #888;">Loading...</td></tr>';
            try {
                const [candResp, escResp, gapResp, approvedResp, rejectedResp, analyticsResp, assistedResp] = await Promise.all([
                    fetch('/admin/candidates', { headers: getAuthHeaders() }),
                    fetch('/admin/escalations?status=pending&limit=100', { headers: getAuthHeaders() }),
                    fetch('/admin/knowledge-gaps?status=OPEN&limit=100', { headers: getAuthHeaders() }),
                    fetch('/admin/learned-knowledge?limit=100', { headers: getAuthHeaders() }),
                    fetch('/admin/candidates?status=REJECTED', { headers: getAuthHeaders() }),
                    fetch('/admin/analytics', { headers: getAuthHeaders() }),
                    fetch('/admin/assisted-resolutions?status=PENDING_REVIEW', { headers: getAuthHeaders() }),
                ]);
                const candData = candResp.ok ? await candResp.json() : { candidates: [] };
                const escData = escResp.ok ? await escResp.json() : { escalations: [] };
                const gapData = gapResp.ok ? await gapResp.json() : { knowledge_gaps: [] };
                const approvedData = approvedResp.ok ? await approvedResp.json() : { learned_knowledge: [] };
                const rejectedData = rejectedResp.ok ? await rejectedResp.json() : { candidates: [] };
                const analytics = analyticsResp.ok ? await analyticsResp.json() : {};
                const assistedData = assistedResp.ok ? await assistedResp.json() : { suggestions: [] };

                const candidates = candData.candidates || [];
                const escalations = escData.escalations || [];
                const gaps = gapData.knowledge_gaps || [];
                aiResolutionApproved = approvedData.learned_knowledge || [];
                aiResolutionRejected = rejectedData.candidates || [];

                aiResolutionItems = [
                    ...(assistedData.suggestions || []).map(s => ({ kind: 'assisted', category: (s.triage_category || 'UNSUPPORTED').toLowerCase(), data: s })),
                    ...candidates.map(c => ({ kind: 'candidate', category: 'ai_suggested', data: c })),
                    ...escalations.map(e => ({ kind: 'escalation', category: 'human_required', data: e })),
                    ...gaps.map(g => ({ kind: 'gap', category: 'teach_required', data: g })),
                ];

                const needsAttention = candidates.length + escalations.length + gaps.length;
                document.getElementById('aiStatTotal').innerText = analytics.total_conversations || 0;
                document.getElementById('aiStatAutoResolved').innerText = analytics.matched_conversations || 0;
                document.getElementById('aiStatNeedsAttention').innerText = needsAttention;
                document.getElementById('aiStatHuman').innerText = escalations.length;
                updateAiResolutionBadge(needsAttention);

                renderAiResolution();
                loadAssistedEvaluation();
            } catch (err) {
                tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: red;">Failed to load AI Resolution &amp; Answers.</td></tr>';
            }
        }

        function updateAiResolutionBadge(count) {
            const badge = document.getElementById('aiResolutionBadge');
            if (!badge) return;
            if (count > 0) {
                badge.textContent = count;
                badge.style.display = 'inline-block';
            } else {
                badge.style.display = 'none';
            }
        }

        const AI_CATEGORY_LABEL = {
            ai_suggested: '✨ AI Suggested',
            teach_required: '📘 Knowledge Gap',
            human_required: '🙋 Pending Human',
            approved: '✅ Approved',
            rejected: '🗄️ Rejected',
        };

        function renderAiResolution() {
            const tbody = document.getElementById('aiResolutionTableBody');
            const query = (document.getElementById('aiResolutionSearch').value || '').trim().toLowerCase();

            let items;
            if (aiResolutionFilter === 'approved') {
                items = aiResolutionApproved.map(k => ({ kind: 'approved', category: 'approved', data: k }));
            } else if (aiResolutionFilter === 'rejected') {
                items = aiResolutionRejected.map(c => ({ kind: 'rejected', category: 'rejected', data: c }));
            } else if (aiResolutionFilter === 'needs_attention') {
                items = aiResolutionItems;
            } else {
                items = aiResolutionItems.filter(i => i.category === aiResolutionFilter);
            }

            if (query) {
                items = items.filter(i => aiResolutionSearchText(i).includes(query));
            }

            if (!items.length) {
                tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #888;">Nothing here right now.</td></tr>';
                return;
            }

            tbody.innerHTML = items.map(item => renderAiResolutionRow(item)).join('');
        }

        function aiResolutionSearchText(item) {
            const d = item.data;
            return [
                d.user_message, d.response, d.verified_answer, d.question_pattern,
                d.topic, d.example_message, d.reason,
            ].filter(Boolean).join(' ').toLowerCase();
        }

        function renderAiResolutionRow(item) {
            const label = AI_CATEGORY_LABEL[item.category];
            if (item.kind === 'assisted') {
                const s = item.data;
                const category = (s.triage_category || 'UNSUPPORTED').toLowerCase();
                const risk = (s.risk_flags || []).join(', ') || 'None';
                let actions = '';
                if (category === 'human_required' || category === 'contact_detail' || category === 'unsupported') actions += `<button class="btn-secondary" style="padding:4px 8px;font-size:.72rem" onclick="sendAssistedToHuman(${s.id})">Send to Human</button>`;
                else if (category === 'non_actionable') actions += `<button class="btn-secondary" style="padding:4px 8px;font-size:.72rem" onclick="dismissAssisted(${s.id})">Dismiss</button>`;
                else if (category === 'teach_required') actions += `<button class="btn-secondary" style="padding:4px 8px;font-size:.72rem" onclick="teachAssisted(${s.id})">Teach &amp; Resolve</button> <button class="btn-secondary" style="padding:4px 8px;font-size:.72rem" onclick="sendAssistedToHuman(${s.id})">Send to Human</button>`;
                else actions += `<button class="btn-primary" style="padding:4px 8px;font-size:.72rem" onclick="approveAssisted(${s.id})">Approve &amp; Resolve</button> <button class="btn-secondary" style="padding:4px 8px;font-size:.72rem" onclick="editAssisted(${s.id})">Edit &amp; Resolve</button> <button class="btn-secondary" style="padding:4px 8px;font-size:.72rem" onclick="teachAssisted(${s.id})">Teach &amp; Resolve</button>`;
                actions += ` <button class="btn-secondary" style="padding:4px 8px;font-size:.72rem;color:var(--danger)" onclick="rejectAssisted(${s.id})">Reject Suggestion</button>`;
                return `<tr><td><span class="badge badge-veg">${escapeHtml(label || category)}</span><br><small>Category: ${escapeHtml(s.triage_category || 'UNSUPPORTED')}<br>Detected Intent: ${escapeHtml(s.detected_intent || '—')}</small></td><td style="max-width:220px;white-space:pre-wrap">${escapeHtml(s.customer_message || '—')}<br><small>Created Time: ${escapeHtml(s.created_at || '—')}</small></td><td style="max-width:300px;white-space:pre-wrap"><b>Suggested Reply:</b> ${escapeHtml(s.suggested_answer || '—')}<br><small>Grounding Status: ${escapeHtml(s.grounding_status || 'Not Grounded')}<br>Matched Knowledge: ${escapeHtml(s.matched_knowledge_source || '—')}<br>Risk Flags: ${escapeHtml(risk)}<br>Source: ${escapeHtml(s.model || '—')}<br>${escapeHtml(s.grounding_details || '')}</small></td><td>Confidence: ${Math.round((s.confidence || 0) * 100)}%</td><td style="white-space:nowrap">${actions}</td></tr>`;
            }
            if (item.kind === 'candidate') {
                const c = item.data;
                const confidence = Math.round((c.confidence || 0) * 100);
                const confColour = confidence >= 85 ? 'var(--success, #2e7d32)' : (confidence >= 60 ? '#b26a00' : 'var(--danger)');
                return `
                <tr>
                    <td><span class="badge badge-veg">${label}</span></td>
                    <td style="max-width: 220px;">${escapeHtml(c.user_message || '—')}</td>
                    <td style="max-width: 340px; font-size: 0.82rem;">
                        <div id="ai-candidateText-${c.id}" style="white-space:pre-wrap;">${escapeHtml(c.response)}</div>
                        <textarea id="ai-candidateEdit-${c.id}" style="display:none;width:100%;min-height:110px;">${escapeHtml(c.response)}</textarea>
                        ${c.edited ? `<small style="color:var(--text-muted);">Edited by ${escapeHtml(c.edited_by || 'admin')} · ${escapeHtml(c.edited_at || '')}</small>` : ''}
                    </td>
                    <td style="color: ${confColour}; font-weight: 600;">${confidence}%</td>
                    <td style="white-space: nowrap;">
                        <button class="btn-primary" style="padding: 4px 10px; font-size: 0.75rem;" onclick="aiApproveCandidate(${c.id})">✅ Approve</button>
                        <button class="btn-secondary" style="padding:4px 10px;font-size:0.75rem;" id="ai-candidateEditButton-${c.id}" onclick="aiBeginCandidateEdit(${c.id})">✏️ Edit</button>
                        <button class="btn-primary" style="display:none;padding:4px 10px;font-size:0.75rem;" id="ai-candidateSaveButton-${c.id}" onclick="aiSaveCandidateEdit(${c.id})">Save draft</button>
                        <button class="btn-secondary" style="display:none;padding:4px 10px;font-size:0.75rem;" id="ai-candidateCancelButton-${c.id}" onclick="aiCancelCandidateEdit(${c.id})">Cancel</button>
                        <button class="btn-secondary" style="padding: 4px 10px; font-size: 0.75rem; color: var(--danger);" onclick="aiRejectCandidate(${c.id})">✕ Reject</button>
                    </td>
                </tr>`;
            }
            if (item.kind === 'escalation') {
                const e = item.data;
                return `
                <tr>
                    <td><span class="badge badge-nonveg">${label}</span></td>
                    <td style="max-width: 220px;"><b>${escapeHtml(e.channel)}</b> · <code>${escapeHtml(e.user_id)}</code><br>${escapeHtml(e.user_message)}</td>
                    <td style="font-size: 0.82rem;">${escapeHtml(e.reason || '')}</td>
                    <td>—</td>
                    <td style="white-space: nowrap;">
                        <button class="btn-secondary" style="padding: 4px 10px; font-size: 0.78rem;" onclick="openResolveModal(${e.id}, '${escapeHtml(e.user_message)}')">✨ Teach &amp; Resolve</button>
                    </td>
                </tr>`;
            }
            if (item.kind === 'gap') {
                const g = item.data;
                const intents = (g.intents || []).join(', ');
                return `
                <tr>
                    <td><span class="badge badge-nonveg">${label}</span></td>
                    <td style="max-width: 220px;">${escapeHtml(g.example_message || '—')}</td>
                    <td style="font-size: 0.82rem;">${escapeHtml(intents || g.reason || '')}</td>
                    <td>seen ${g.occurrences || 1}x</td>
                    <td style="white-space: nowrap;">
                        <button class="btn-secondary" style="padding: 4px 10px; font-size: 0.75rem;" onclick="aiSetGapStatus(${g.id}, 'DISMISSED')">Dismiss</button>
                    </td>
                </tr>`;
            }
            if (item.kind === 'approved') {
                const k = item.data;
                return `
                <tr>
                    <td><span class="badge badge-veg">${label}</span></td>
                    <td style="max-width: 220px;"><b>${escapeHtml(k.topic || '')}</b><br><code style="font-size:0.78rem;">${escapeHtml(k.question_pattern || '')}</code></td>
                    <td style="max-width: 340px; font-size: 0.82rem; white-space:pre-wrap;">${escapeHtml(k.verified_answer || '')}</td>
                    <td><span class="badge badge-veg">${escapeHtml(k.source || 'staff')}</span></td>
                    <td style="white-space: nowrap;">
                        <button class="btn-secondary" style="padding: 4px 8px; font-size: 0.75rem;" onclick="openInCustomQa(${k.id})">Open in Custom Q&amp;A</button>
                        <button class="btn-secondary" style="padding: 4px 8px; font-size: 0.75rem; color: var(--danger);" onclick="deleteKnowledge(${k.id})">🗑️ Delete</button>
                    </td>
                </tr>`;
            }
            // rejected candidate — a decision already made, kept for the record only
            const r = item.data;
            const rConfidence = Math.round((r.confidence || 0) * 100);
            return `
            <tr style="opacity:0.75;">
                <td><span class="badge badge-nonveg">${label}</span></td>
                <td style="max-width: 220px;">${escapeHtml(r.user_message || '—')}</td>
                <td style="max-width: 340px; font-size: 0.82rem; white-space:pre-wrap;">${escapeHtml(r.response || '')}</td>
                <td>${rConfidence}%</td>
                <td style="font-size:0.78rem; color:var(--text-muted);">No longer editable</td>
            </tr>`;
        }

        async function assistedAction(id, path, body) {
            const options = { method:'POST', headers:{...getAuthHeaders(), 'Content-Type':'application/json'} };
            if (body) options.body = JSON.stringify(body);
            const resp = await fetch(`/admin/assisted-resolutions/${id}/${path}`, options);
            if (!resp.ok) { const err = await resp.json().catch(() => ({})); throw new Error(err.detail || resp.status); }
            await loadAiResolution();
        }

        async function loadAssistedEvaluation() {
            try {
                const resp = await fetch('/admin/assisted-resolutions/evaluation', { headers: getAuthHeaders() });
                if (!resp.ok) throw new Error('evaluation unavailable');
                const data = await resp.json();
                const ev = data.evaluation || {}, p = ev.performance || {}, readiness = ev.readiness || {}, safety = ev.safety || {}, ar = ev.auto_resolve || {};
                const card = (label, value) => `<div style="padding:10px;border:1px solid var(--border);border-radius:10px;text-align:center"><small>${escapeHtml(label)}</small><h3 style="margin:4px 0">${escapeHtml(value)}</h3></div>`;
                document.getElementById('assistedPerformanceSummary').innerHTML = [
                    card('Suggestions Generated', p.suggestions_generated || 0), card('Reviewed', p.reviewed || 0), card('Approved As-Is', p.approved_as_is || 0), card('Edited & Approved', p.edited_and_approved || 0), card('Rejected', p.rejected || 0), card('Sent to Human', p.sent_to_human || 0), card('Teach & Resolve', p.teach_and_resolve || 0), card('Non-Actionable Dismissed', p.non_actionable_dismissed || 0),
                ].join('');
                const readinessText = readiness.reviewed_grounded_suggestions ? `${readiness.reviewed_grounded_suggestions.value} / ${readiness.reviewed_grounded_suggestions.target} grounded human-reviewed suggestions · unsafe facts ${readiness.unsafe_factual_suggestions || 0} · high-confidence problems ${readiness.high_confidence_rejected || 0} · price/discount/delivery errors ${readiness.price_discount_delivery_errors || 0} · Auto Resolve: ${data.auto_resolve_enabled ? 'ENABLED (unexpected)' : 'OFF'}` : 'No data yet.';
                document.getElementById('assistedReadinessBody').innerHTML = ` <span style="font-weight:400;margin-left:8px">${escapeHtml(readinessText)}</span>`;
                const settings = data.auto_resolve_settings || {};
                const state = document.getElementById('autoResolveControlState');
                if (state) {
                    state.innerHTML = `<b>${escapeHtml(settings.state_label || (settings.actual_enabled ? 'AUTO RESOLVE: ON' : 'AUTO RESOLVE: OFF'))}</b><span id="autoResolveControlDetail" style="margin-left:8px;color:var(--text-muted)">Simulation ${settings.simulation_enabled ? 'ON' : 'OFF'} · Readiness ${escapeHtml(readiness.readiness_status || ar.readiness_status || 'NOT READY')} · ${ar.simulated_eligible_cases || 0} simulated eligible · ${ar.simulation_false_positives || 0} false positives</span>`;
                }
                const metricTarget = document.getElementById('autoResolveMetrics');
                if (metricTarget) {
                    const metric = (label, value) => `<div style="padding:10px;border:1px solid var(--border);border-radius:10px;text-align:center"><small>${escapeHtml(label)}</small><h3 style="margin:4px 0">${escapeHtml(value)}</h3></div>`;
                    metricTarget.innerHTML = [metric('Simulated Eligible', ar.simulated_eligible_cases || 0), metric('False Positives', ar.simulation_false_positives || 0), metric('Grounded Reviewed', ar.reviewed_grounded_cases || 0), metric('Risk Violations', ar.risk_violations || 0), metric('Source Overrides', ar.source_routing_overrides || 0), metric('WhatsApp AI', ar.whatsapp_ai_activity || 0), metric('Order/CAM Interference', ar.order_cam_interference || 0)].join('');
                }
                const rows = (obj, labels) => labels.map(label => { const x = obj[label] || {}; return `<tr><td>${escapeHtml(label)}</td><td>${x.reviewed ?? x.total ?? 0}</td><td>${x.approved_as_is || x.approved || 0}</td><td>${x.edited || 0}</td><td>${x.rejected || 0}</td><td>${x.human_handoff || x.sent_to_human || 0}</td></tr>`; }).join('');
                document.getElementById('assistedConfidenceTable').innerHTML = rows(ev.confidence_buckets || {}, Object.keys(ev.confidence_buckets || {}));
                document.getElementById('assistedCategoryTable').innerHTML = rows(ev.category_breakdown || {}, Object.keys(ev.category_breakdown || {}));
                document.getElementById('assistedGroundingTable').innerHTML = rows(ev.grounding_breakdown || {}, Object.keys(ev.grounding_breakdown || {}));
                document.getElementById('assistedRiskTable').innerHTML = rows(ev.risk_breakdown || {}, Object.keys(ev.risk_breakdown || {}));
                const problems = ev.high_confidence_problems || [];
                document.getElementById('assistedHighConfidenceProblems').innerHTML = problems.length ? problems.map(x => `<div style="padding:6px;border-bottom:1px solid var(--border)"><b>${escapeHtml(x.confidence)}</b> · ${escapeHtml(x.triage_category)} · ${escapeHtml(x.status)} · ${escapeHtml(x.customer_message)}<br><small>Grounding: ${escapeHtml(x.grounding_status)} · Knowledge: ${escapeHtml(x.matched_knowledge_source || '—')} · Reason: ${escapeHtml(x.rejection_reason || x.edit_severity || '—')}</small></div>`).join('') : 'None recorded.';
                const comparisons = ev.shadow_vs_assisted || [];
                document.getElementById('assistedShadowComparison').innerHTML = comparisons.length ? comparisons.map(x => `<div>${escapeHtml(x.shadow_prediction)} → ${escapeHtml(x.assisted_prediction)} → ${escapeHtml(x.human_final_decision || 'Pending')}</div>`).join('') : 'No correlated records.';
                document.getElementById('assistedSafetyMetrics').innerHTML = `Source Routing Overrides by AI: <b>${safety.source_routing_overrides || 0}</b> · WhatsApp Assisted AI Activity: <b>${safety.whatsapp_assisted_activity || 0}</b> · Protected order flow: <b>${safety.protected_order_flow || 0}</b> · CAM interference: <b>${safety.cam_interference || 0}</b>`;
                const signal = document.getElementById('customQaAiSignals');
                if (signal) signal.innerHTML = 'AI Review Signal: ' + ((ev.knowledge_signals || []).length ? (ev.knowledge_signals || []).map(k => `${escapeHtml(k.topic || k.knowledge_id)} — Used in ${k.total || 0} assisted suggestions; Approved ${k.approved_as_is || 0}; Edited ${k.edited || 0}; Rejected ${k.rejected || 0}${k.knowledge_review_recommended ? ' · <b>Knowledge Review Recommended</b>' : ''}`).join('<br>') : 'No Custom Q&A usage yet.');
            } catch (err) {
                const target = document.getElementById('assistedSafetyMetrics'); if (target) target.textContent = 'Evaluation unavailable.';
            }
        }
        async function approveAssisted(id) { if (confirm('Approve and send this Instagram answer now?')) { try { await assistedAction(id,'approve',{}); } catch(e) { alert(e); } } }
        async function editAssisted(id) { const answer = prompt('Edit the final answer before sending:'); if (answer && answer.trim()) { try { await assistedAction(id,'edit-resolve',{final_answer:answer.trim()}); } catch(e) { alert(e); } } }
        async function rejectAssisted(id) { const reason = prompt('Why reject this suggestion?','Not approved'); if (reason !== null) { try { await assistedAction(id,'reject',{reason}); } catch(e) { alert(e); } } }
        async function sendAssistedToHuman(id) { if (confirm('Send this case to a human? No AI answer will be sent.')) { try { await assistedAction(id,'send-to-human',{}); } catch(e) { alert(e); } } }
        async function teachAssisted(id) { const answer = prompt('Enter the verified answer to teach and send:'); if (answer && answer.trim()) { try { await assistedAction(id,'teach-and-resolve',{verified_answer:answer.trim()}); } catch(e) { alert(e); } } }
        async function dismissAssisted(id) { if (confirm('Dismiss this non-actionable message?')) { try { await assistedAction(id,'dismiss',{}); } catch(e) { alert(e); } } }

        async function aiApproveCandidate(id) {
            if (!confirm('Approve this answer?\n\nIt will be added to the bot\'s verified knowledge and sent to customers who ask this from now on.')) return;
            try {
                const resp = await fetch(`/admin/candidates/${id}/approve`, {
                    method: 'POST',
                    headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                    body: JSON.stringify({ approved_by: 'admin_dashboard' })
                });
                if (resp.ok) { loadAiResolution(); refreshCandidateBadge(); }
                else { const err = await resp.json().catch(() => ({})); alert('Could not approve: ' + (err.detail || resp.status)); }
            } catch (err) { alert('Could not approve: ' + err); }
        }

        function aiBeginCandidateEdit(id) {
            document.getElementById(`ai-candidateText-${id}`).style.display = 'none';
            document.getElementById(`ai-candidateEdit-${id}`).style.display = 'block';
            document.getElementById(`ai-candidateEditButton-${id}`).style.display = 'none';
            document.getElementById(`ai-candidateSaveButton-${id}`).style.display = 'inline-block';
            document.getElementById(`ai-candidateCancelButton-${id}`).style.display = 'inline-block';
        }

        function aiCancelCandidateEdit(id) { renderAiResolution(); }

        async function aiSaveCandidateEdit(id) {
            const response = document.getElementById(`ai-candidateEdit-${id}`).value.trim();
            if (!response) return alert('Draft answer cannot be empty.');
            const resp = await fetch(`/admin/candidates/${id}`, {
                method: 'PATCH', headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                body: JSON.stringify({ response, edited_by: 'admin_dashboard' })
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) return alert('Could not save draft: ' + (data.detail || resp.status));
            loadAiResolution();
        }

        async function aiRejectCandidate(id) {
            if (!confirm('Reject this draft? It will not be shown to customers and will be removed from this list.')) return;
            try {
                const resp = await fetch(`/admin/candidates/${id}/reject`, { method: 'POST', headers: getAuthHeaders() });
                if (resp.ok) { loadAiResolution(); refreshCandidateBadge(); }
                else { const err = await resp.json().catch(() => ({})); alert('Could not reject: ' + (err.detail || resp.status)); }
            } catch (err) { alert('Could not reject: ' + err); }
        }

        async function aiSetGapStatus(gapId, status) {
            try {
                const resp = await fetch(`/admin/knowledge-gaps/${gapId}/status`, {
                    method: 'POST',
                    headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                    body: JSON.stringify({ status })
                });
                if (resp.ok) loadAiResolution();
                else { const err = await resp.json().catch(() => ({})); alert('Could not update: ' + (err.detail || resp.status)); }
            } catch (err) { alert('Could not update: ' + err); }
        }

        // Triage shadow mode: read-only. There is deliberately no approve,
        // reject, or resolve action anywhere below — a shadow row was never
        // acted on by anything, and this view must not become the first
        // thing that does.
        const TRIAGE_CATEGORY_LABEL = {
            AUTO_RESOLVE: '🟢 Auto-Resolve',
            AI_SUGGESTED: '✨ AI Suggested',
            TEACH_REQUIRED: '📘 Teach Required',
            HUMAN_REQUIRED: '🙋 Human Required',
            NON_ACTIONABLE: '💬 Non-Actionable',
            CONTACT_DETAIL: '📇 Contact Detail',
            UNSUPPORTED: '🚫 Unsupported',
        };

        async function loadTriageShadow() {
            const statusLine = document.getElementById('triageShadowStatusLine');
            const summary = document.getElementById('triageShadowSummary');
            const tbody = document.getElementById('triageShadowTableBody');
            try {
                const resp = await fetch('/admin/triage-shadow?days=7&limit=40', { headers: getAuthHeaders() });
                if (!resp.ok) {
                    statusLine.innerText = 'Could not load triage shadow report.';
                    tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: red;">Failed to load.</td></tr>';
                    return;
                }
                const data = await resp.json();

                statusLine.innerText = data.shadow_enabled
                    ? `On — ${data.classified} message(s) classified in the last ${data.days} day(s). Confidence threshold for a future auto-resolve: ${Math.round(data.confidence_threshold * 100)}%.`
                    : (data.note || 'LLM_TRIAGE_ENABLED is off.');

                // Shown in the top summary only while shadow mode is on, and
                // deliberately never folded into AUTO RESOLVED — a shadow
                // category is a prediction nobody has acted on, not a
                // resolution.
                const analyzedCard = document.getElementById('aiStatAnalyzedCard');
                if (analyzedCard) {
                    analyzedCard.style.display = data.shadow_enabled ? 'block' : 'none';
                    document.getElementById('aiStatAnalyzed').innerText = data.classified || 0;
                }

                summary.innerHTML = (data.by_category || []).map(c => `
                    <div style="background:#F8FAFC; border:1px solid var(--border); padding:12px; border-radius:12px; text-align:center;">
                        <span style="font-size:0.72rem; color:#64748B; font-weight:600;">${escapeHtml(TRIAGE_CATEGORY_LABEL[c.category] || c.category)}</span>
                        <h3 style="font-size:1.4rem; margin-top:4px;">${c.count}</h3>
                        ${c.avg_confidence !== null ? `<small style="color:var(--text-muted);">avg ${Math.round(c.avg_confidence * 100)}%</small>` : ''}
                    </div>`).join('') || '<p style="color: var(--text-muted); grid-column: 1/-1;">Nothing classified yet in this window.</p>';

                const rows = data.recent || [];
                if (!rows.length) {
                    tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: #888;">Nothing classified yet in this window.</td></tr>';
                    return;
                }
                tbody.innerHTML = rows.map(r => {
                    const label = r.ok ? (TRIAGE_CATEGORY_LABEL[r.category] || r.category) : `⚠️ ${escapeHtml(r.detail || 'failed')}`;
                    const wouldAuto = r.would_auto_resolve ? '✅ Yes' : '—';
                    const grounded = r.ok ? (r.grounded ? '✅ Grounded' : '—') : '—';
                    const actual = r.was_matched ? `Matched: ${escapeHtml(r.template_id || '')}` : `Fell through${r.template_id ? ` (${escapeHtml(r.template_id)})` : ''}`;
                    return `
                    <tr>
                        <td style="max-width: 220px;">${escapeHtml(r.user_message || '')}</td>
                        <td><span class="badge badge-veg">${label}</span></td>
                        <td>${r.ok && r.confidence !== null ? Math.round(r.confidence * 100) + '%' : '—'}</td>
                        <td>${grounded}</td>
                        <td>${wouldAuto}</td>
                        <td style="font-size:0.8rem;">${actual}</td>
                        <td style="max-width: 260px; font-size:0.78rem; color:var(--text-muted);">${escapeHtml(r.reasoning || '')}</td>
                    </tr>`;
                }).join('');
            } catch (err) {
                statusLine.innerText = 'Could not load triage shadow report.';
                tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: red;">Failed to load.</td></tr>';
            }
        }

        // CUSTOM Q&A: the client-managed reusable knowledge library. Every
        // read/write here goes through /admin/custom-qa, which is backed by
        // the exact same learned_knowledge table (and matcher) Teach &
        // Resolve and candidate approval already use -- there is no second
        // knowledge store or second editor.
        let customQaItems = [];
        let systemResponseItems = [];
        let customQaStatusFilter = 'ACTIVE';
        let cqaAliasesDraft = [];
        let responseLibrarySearchTimer = null;
        let responseLibrarySuggestionIndex = -1;
        let responseLibraryVisibleLimit = 20;

        const RESPONSE_LIBRARY_SYNONYMS = {
            sweets: ['sondesh', 'kanchagolla', 'jolbhora'],
            shipping: ['delivery'],
            cost: ['price'],
            rate: ['price'],
            menu: ['weekly menu', 'meal menu'],
        };

        // Linked from AI Resolution & Answers' Approved Answers filter --
        // one editor for learned_knowledge, not two. Switches tabs and opens
        // the same Edit modal Custom Q&A's own Edit button uses.
        async function openInCustomQa(id) {
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            showGroup('custom_qa');
            await loadCustomQa();
            const item = customQaItems.find(i => i.id === id);
            if (item) openCustomQaModal(item);
            else alert('Could not find this Q&A in Custom Q&A (it may be Disabled/Archived — check those filters).');
        }

        async function loadCustomQa() {
            const tbody = document.getElementById('customQaTableBody');
            tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; color: #888;">Loading...</td></tr>';
            try {
                const sourceFilter = document.getElementById('cqaSourceFilter').value;
                const customSource = sourceFilter === 'system_template' ? '' : sourceFilter;
                const [resp, systemResp] = await Promise.all([
                    fetch(`/admin/custom-qa?status=${encodeURIComponent(customQaStatusFilter)}${customSource ? `&source=${encodeURIComponent(customSource)}` : ''}&limit=500`, { headers: getAuthHeaders() }),
                    fetch(`/admin/response-library?status=${encodeURIComponent(customQaStatusFilter)}${sourceFilter ? `&source=${encodeURIComponent(sourceFilter)}` : ''}&limit=500`, { headers: getAuthHeaders() }),
                ]);
                if (!resp.ok || !systemResp.ok) throw new Error('failed');
                const data = await resp.json();
                const systemData = await systemResp.json();
                customQaItems = data.items || [];
                systemResponseItems = (systemData.items || []).filter(i => i.kind === 'system_template');

                const topicSelect = document.getElementById('cqaTopicFilter');
                const currentTopic = topicSelect.value;
                const topics = [...new Set(customQaItems.concat(systemResponseItems).map(i => i.topic || i.category).filter(Boolean))].sort();
                topicSelect.innerHTML = '<option value="">All Topics</option>' + topics.map(t => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join('');
                topicSelect.value = topics.includes(currentTopic) ? currentTopic : '';

                renderCustomQaList();
                renderSystemResponseLibrary();
                renderResponseLibrarySuggestions();
            } catch (err) {
                tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; color: red;">Failed to load Custom Q&amp;A.</td></tr>';
            }
        }

        function setCustomQaStatusFilter(status) {
            customQaStatusFilter = status;
            ['ACTIVE', 'DISABLED', 'ARCHIVED', 'ALL'].forEach(s => {
                const btn = document.getElementById(`cqaStatusBtn-${s}`);
                if (btn) btn.classList.toggle('active', s === status);
            });
            loadCustomQa();
        }

        function responseLibraryNormalise(value) {
            return String(value || '').toLowerCase().replaceAll("’", '').replaceAll("'", '').replace(/[^\p{L}\p{N}]+/gu, ' ').trim().replace(/\s+/g, ' ');
        }

        function responseLibraryEditDistance(a, b) {
            const prev = Array.from({ length: b.length + 1 }, (_, i) => i);
            for (let i = 1; i <= a.length; i++) {
                const row = [i];
                for (let j = 1; j <= b.length; j++) row[j] = a[i - 1] === b[j - 1] ? prev[j - 1] : 1 + Math.min(prev[j], row[j - 1], prev[j - 1]);
                for (let j = 0; j <= b.length; j++) prev[j] = row[j];
            }
            return prev[b.length];
        }

        function responseLibrarySearchScore(query, item) {
            const needle = responseLibraryNormalise(query);
            if (!needle) return 1;
            const queryTokens = needle.split(' ').filter(Boolean);
            const expanded = [...queryTokens, ...(RESPONSE_LIBRARY_SYNONYMS[needle] || []).flatMap(v => responseLibraryNormalise(v).split(' '))];
            const fields = {
                primary: [item.primary_question], aliases: item.aliases || [],
                title: [item.title], category: [item.category, item.topic, item.template_id],
                answer: [item.approved_answer, ...(item.response_variants || [])],
            };
            const words = values => values.flatMap(value => responseLibraryNormalise(value).split(' ').filter(Boolean));
            const exact = values => values.some(value => responseLibraryNormalise(value) === needle);
            const prefix = values => expanded.some(q => q && (needle.replace(/\s/g, '').length > 2 ? words(values).some(word => word.startsWith(q)) : words(values).includes(q)));
            const shortSocialPrefix = values => needle.length <= 2 && words(values).some(word => ['hi', 'hello', 'hey'].includes(word));
            const fuzzy = values => queryTokens.length > 0 && queryTokens.every(q => words(values).some(word => q.length > 2 && responseLibraryEditDistance(q, word) <= Math.max(1, Math.floor(q.length / 4))));
            if (exact(fields.primary)) return 100;
            if (exact(fields.aliases)) return 95;
            if (shortSocialPrefix(fields.primary)) return 92;
            if (shortSocialPrefix(fields.aliases)) return 91;
            if (prefix(fields.primary)) return 80;
            if (prefix(fields.aliases)) return 76;
            if (exact(fields.title)) return 86;
            if (prefix(fields.title)) return 82;
            if (exact(fields.category)) return 78;
            if (prefix(fields.category)) return 74;
            if (fuzzy(fields.primary) || fuzzy(fields.aliases)) return 68;
            if (fuzzy(fields.title)) return 62;
            if (needle.replace(/\s/g, '').length > 2 && queryTokens.some(q => words(fields.answer).includes(q))) return 40;
            return 0;
        }

        function responseLibraryHighlight(value, query) {
            const text = escapeHtml(value || '');
            const needle = responseLibraryNormalise(query);
            if (!needle) return text;
            const token = needle.split(' ')[0];
            if (!token || token.length < 1) return text;
            // `token` comes from responseLibraryNormalise and is therefore
            // composed only of letters/numbers; no regex escaping is needed.
            return text.replace(new RegExp(`(${token})`, 'ig'), '<mark>$1</mark>');
        }

        function scheduleResponseLibrarySearch() {
            clearTimeout(responseLibrarySearchTimer);
            responseLibrarySearchTimer = setTimeout(() => {
                responseLibraryVisibleLimit = 20;
                renderCustomQaList();
                renderSystemResponseLibrary();
                renderResponseLibrarySuggestions();
            }, 200);
        }

        function renderCustomQaList() {
            const tbody = document.getElementById('customQaTableBody');
            const query = (document.getElementById('customQaSearch').value || '').trim().toLowerCase();
            const source = document.getElementById('cqaSourceFilter').value;
            const topic = document.getElementById('cqaTopicFilter').value;
            const priority = document.getElementById('cqaPriorityFilter').value;

            let items = customQaItems;
            if (source === 'system_template') items = [];
            else if (source) items = items.filter(i => i.source === source);
            if (topic) items = items.filter(i => i.topic === topic);
            if (priority) items = items.filter(i => i.priority === priority);
            if (query) items = items.map(i => ({ item: i, score: responseLibrarySearchScore(query, { ...i, title: i.topic, category: i.topic }) }))
                .filter(entry => entry.score >= (query.replace(/\s/g, '').length <= 1 ? 90 : 1))
                .sort((a, b) => b.score - a.score)
                .map(entry => entry.item);

            if (!items.length) {
                tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; color: #888;">Nothing here yet.</td></tr>';
                return;
            }

            tbody.innerHTML = items.map(renderCustomQaRow).join('');
        }

        function renderSystemResponseLibrary() {
            const box = document.getElementById('systemResponseLibraryCards');
            if (!box) return;
            const query = (document.getElementById('customQaSearch').value || '').trim().toLowerCase();
            const source = document.getElementById('cqaSourceFilter').value;
            const topic = document.getElementById('cqaTopicFilter').value;
            const priority = document.getElementById('cqaPriorityFilter').value;
            if (source && source !== 'system_template') { box.innerHTML = ''; return; }
            const ranked = systemResponseItems.map(item => ({ item, score: responseLibrarySearchScore(query, item) })).filter(entry => {
                const item = entry.item;
                if (priority && priority !== 'PROTECTED') return false;
                if (topic && String(item.topic || item.category || '') !== topic) return false;
                return !query || entry.score >= (query.replace(/\s/g, '').length <= 1 ? 90 : 1);
            }).sort((a, b) => b.score - a.score || String(a.item.title || '').localeCompare(String(b.item.title || '')));
            const items = ranked.slice(0, responseLibraryVisibleLimit).map(entry => entry.item);
            const count = document.getElementById('responseLibraryResultCount');
            if (count) count.textContent = `${ranked.length} matching response${ranked.length === 1 ? '' : 's'}`;
            if (!items.length) { box.innerHTML = '<div style="font-size:.82rem;color:var(--text-muted);">No matching system templates.</div>'; return; }
            box.innerHTML = items.map(renderSystemResponseCard).join('') + (ranked.length > items.length
                ? `<button class="btn-secondary" style="grid-column:1/-1;justify-self:start;" onclick="responseLibraryVisibleLimit += 20; renderSystemResponseLibrary();">Load more</button>` : '');
        }

        function renderSystemResponseCard(item) {
            const status = item.status || 'ACTIVE';
            const aliases = (item.aliases || []).slice(0, 5).map(escapeHtml).join(', ') || '—';
            const editButton = item.editable ? `<button class="btn-secondary" style="padding:4px 8px;font-size:.72rem;" onclick="openSystemResponseEditor('${escapeHtml(item.template_id)}')">Edit</button>` : '';
            const query = (document.getElementById('customQaSearch') || {}).value || '';
            const full = escapeHtml(item.approved_answer || '');
            const preview = full.length > 260 ? `${full.slice(0, 260)}…` : full;
            const expandButton = full.length > 260
                ? '<button class="btn-secondary" style="padding:3px 7px;font-size:.7rem;" onclick="toggleSystemResponse(\'' + escapeHtml(item.template_id) + '\', this)" data-full-response="' + full.replace(/"/g, '&quot;') + '">View full response</button>'
                : '';
            return `<div id="response-card-${escapeHtml(item.template_id)}" style="border:1px solid var(--border);border-radius:10px;padding:12px;background:#fff;">
                <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;"><b>${responseLibraryHighlight(item.title || item.template_id, query)}</b><span class="badge badge-veg">${escapeHtml(status)}</span></div>
                <div style="font-size:.78rem;color:var(--text-muted);margin:5px 0;">${responseLibraryHighlight(item.primary_question || '', query)} · ${responseLibraryHighlight(item.template_id || '', query)}</div>
                <div style="font-size:.76rem;color:var(--text-muted);">Aliases: ${aliases}</div>
                <div id="response-copy-${escapeHtml(item.template_id)}" data-collapsed="true" style="white-space:pre-wrap;font-size:.82rem;margin:8px 0;">${preview}</div>
                ${expandButton}
                <div style="display:flex;gap:6px;flex-wrap:wrap;">
                    ${editButton}
                    <button class="btn-secondary" style="padding:4px 8px;font-size:.72rem;" onclick="previewSystemResponse('${escapeHtml(item.template_id)}')">Preview</button>
                    <button class="btn-secondary" style="padding:4px 8px;font-size:.72rem;" onclick="testSystemResponse('${escapeHtml(item.template_id)}')">Test</button>
                    <button class="btn-secondary" style="padding:4px 8px;font-size:.72rem;" onclick="historySystemResponse('${escapeHtml(item.template_id)}')">History</button>
                </div>
            </div>`;
        }

        function renderResponseLibrarySuggestions() {
            const box = document.getElementById('responseLibrarySuggestions');
            const input = document.getElementById('customQaSearch');
            if (!box || !input) return;
            const query = input.value.trim();
            if (!query) { box.style.display = 'none'; box.innerHTML = ''; return; }
            const source = document.getElementById('cqaSourceFilter').value;
            const topic = document.getElementById('cqaTopicFilter').value;
            const ranked = systemResponseItems.concat(customQaItems.map(item => ({ ...item, title: item.topic, category: item.topic, kind: 'custom_qa' })))
                .filter(item => !source || (source === 'system_template' ? item.kind === 'system_template' : item.source === source))
                .filter(item => !topic || String(item.topic || item.category || '') === topic)
                .map(item => ({ item, score: responseLibrarySearchScore(query, item) }))
                .filter(entry => entry.score >= (query.replace(/\s/g, '').length <= 1 ? 90 : 1))
                .sort((a, b) => b.score - a.score || String(a.item.title || '').localeCompare(String(b.item.title || '')))
                .slice(0, 8);
            responseLibrarySuggestionIndex = -1;
            if (!ranked.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
            box.innerHTML = ranked.map((entry, index) => {
                const item = entry.item;
                const id = item.template_id || item.id;
                const primary = item.primary_question || item.question_pattern || '—';
                const normalizedQuery = responseLibraryNormalise(query);
                const matchingAliases = (item.aliases || [])
                    .filter(alias => normalizedQuery.length <= 2 && responseLibraryNormalise(alias).startsWith(normalizedQuery))
                    .slice(0, 3);
                const triggerSummary = [primary, ...matchingAliases].filter(Boolean).join(' · ');
                const sourceLabel = item.source_label || (item.kind === 'system_template' ? 'System Template' : 'Client Q&A');
                return `<button type="button" class="response-library-suggestion" role="option" data-index="${index}" onclick="chooseResponseLibrarySuggestion('${escapeHtml(id)}','${escapeHtml(item.kind || 'custom_qa')}')" style="display:block;width:100%;text-align:left;border:0;border-bottom:1px solid #EEF2F7;background:#fff;padding:9px 11px;cursor:pointer;">
                    <b>${responseLibraryHighlight(item.title || id, query)}</b><br><small>${responseLibraryHighlight(triggerSummary, query)} · ${escapeHtml(sourceLabel)}</small>
                </button>`;
            }).join('');
            box.style.display = 'block';
        }

        function chooseResponseLibrarySuggestion(id, kind) {
            const input = document.getElementById('customQaSearch');
            const box = document.getElementById('responseLibrarySuggestions');
            if (box) box.style.display = 'none';
            if (kind === 'system_template') {
                const item = systemResponseItems.find(entry => entry.template_id === id);
                if (item) input.value = item.title || item.primary_question || id;
            } else {
                const item = customQaItems.find(entry => String(entry.id) === String(id));
                if (item) input.value = item.primary_question || item.topic || id;
            }
            responseLibraryVisibleLimit = 20;
            renderCustomQaList();
            renderSystemResponseLibrary();
            setTimeout(() => document.getElementById(`response-card-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 0);
        }

        function responseLibrarySearchKeydown(event) {
            const box = document.getElementById('responseLibrarySuggestions');
            if (!box || box.style.display === 'none') return;
            const options = [...box.querySelectorAll('.response-library-suggestion')];
            if (event.key === 'Escape') { box.style.display = 'none'; return; }
            if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp' && event.key !== 'Enter') return;
            event.preventDefault();
            if (event.key === 'ArrowDown') responseLibrarySuggestionIndex = Math.min(responseLibrarySuggestionIndex + 1, options.length - 1);
            if (event.key === 'ArrowUp') responseLibrarySuggestionIndex = Math.max(responseLibrarySuggestionIndex - 1, 0);
            options.forEach((option, index) => option.style.background = index === responseLibrarySuggestionIndex ? '#F1F5F9' : '#fff');
            if (event.key === 'Enter' && responseLibrarySuggestionIndex >= 0) options[responseLibrarySuggestionIndex].click();
        }

        function toggleSystemResponse(templateId, button) {
            const copy = document.getElementById(`response-copy-${templateId}`);
            if (!copy || !button) return;
            if (copy.dataset.collapsed === 'true') {
                copy.innerText = button.dataset.fullResponse || copy.innerText;
                copy.dataset.collapsed = 'false';
                button.innerText = 'Collapse response';
            } else {
                const full = button.dataset.fullResponse || '';
                copy.innerText = `${full.slice(0, 260)}${full.length > 260 ? '…' : ''}`;
                copy.dataset.collapsed = 'true';
                button.innerText = 'View full response';
            }
        }

        document.addEventListener('click', event => {
            const wrap = document.getElementById('responseLibrarySearchWrap');
            const box = document.getElementById('responseLibrarySuggestions');
            if (wrap && box && !wrap.contains(event.target)) box.style.display = 'none';
        });

        async function openSystemResponseEditor(templateId) {
            const item = systemResponseItems.find(i => i.template_id === templateId);
            if (!item || !item.editable) return;
            const response = window.prompt('Editable Response Text (protected triggers and runtime logic cannot be changed):', item.approved_answer || '');
            if (response === null || !response.trim() || response.trim() === (item.approved_answer || '').trim()) return;
            try {
                const resp = await fetch(`/admin/response-library/system/${encodeURIComponent(templateId)}`, {
                    method: 'PATCH', headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                    body: JSON.stringify({ response: response.trim(), updated_by: 'client_admin_dashboard' }),
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) { alert(data.detail || 'Could not save response.'); return; }
                await loadCustomQa();
                alert('Response saved and active for the next matching customer message.');
            } catch (e) { alert('Could not save response.'); }
        }

        async function previewSystemResponse(templateId) {
            try {
                const resp = await fetch(`/admin/response-library/system/${encodeURIComponent(templateId)}/preview`, { method: 'POST', headers: getAuthHeaders() });
                const data = await resp.json();
                alert(`${data.preview_text || ''}\n\n(${data.note || 'Preview only — nothing has been sent.'})`);
            } catch (e) { alert('Preview failed.'); }
        }

        async function testSystemResponse(templateId) {
            const message = window.prompt('Message to test (dry-run only):', 'hi');
            if (!message) return;
            try {
                const resp = await fetch('/admin/response-library/test', {
                    method: 'POST', headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message, focus_template_id: templateId }),
                });
                const data = await resp.json();
                alert(`Detected template: ${data.template_id || 'none'}\nSource: ${data.source || 'none'}\nScore: ${Math.round((data.score || 0) * 100)}%\n\n${data.response || 'No approved response matched.'}`);
            } catch (e) { alert('Test failed.'); }
        }

        async function historySystemResponse(templateId) {
            try {
                const resp = await fetch(`/admin/response-library/system/${encodeURIComponent(templateId)}/versions`, { headers: getAuthHeaders() });
                const data = await resp.json();
                const versions = data.versions || [];
                if (!versions.length) { alert('No edits yet — this system response is using its approved default copy.'); return; }
                const choice = window.prompt('Version IDs (newest first):\n' + versions.map(v => `${v.version_id}: ${v.changed_at} — ${v.response}`).join('\n\n') + '\n\nEnter a version ID to restore, or Cancel:');
                if (!choice) return;
                const restore = await fetch(`/admin/response-library/system/${encodeURIComponent(templateId)}/restore/${encodeURIComponent(choice)}`, {
                    method: 'POST', headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                    body: JSON.stringify({ actor: 'client_admin_dashboard' }),
                });
                if (!restore.ok) { alert('Could not restore that version.'); return; }
                await loadCustomQa();
                alert('Previous response restored.');
            } catch (e) { alert('History failed.'); }
        }

        function timeAgo(iso) {
            if (!iso) return 'Never';
            const then = new Date(iso.includes('T') || iso.includes(' ') ? iso + (iso.endsWith('Z') ? '' : 'Z') : iso);
            const diffMs = Date.now() - then.getTime();
            if (isNaN(diffMs)) return iso;
            const mins = Math.round(diffMs / 60000);
            if (mins < 1) return 'Just now';
            if (mins < 60) return `${mins} min ago`;
            const hrs = Math.round(mins / 60);
            if (hrs < 24) return `${hrs} hour${hrs === 1 ? '' : 's'} ago`;
            const days = Math.round(hrs / 24);
            return `${days} day${days === 1 ? '' : 's'} ago`;
        }

        function renderCustomQaRow(item) {
            const aliasCount = (item.aliases || []).length;
            const priorityBadge = item.priority === 'HIGH' ? '<span class="badge badge-nonveg">High</span>' : '<span class="badge badge-veg">Normal</span>';
            const statusBadge = { ACTIVE: '<span class="badge badge-veg">Active</span>', DISABLED: '<span class="badge badge-nonveg">Disabled</span>', ARCHIVED: '<span class="badge">Archived</span>' }[item.status] || item.status;
            const lifecycleAction = item.status === 'ARCHIVED'
                ? `<button class="btn-secondary" style="padding:4px 8px;font-size:0.72rem;" onclick="cqaSetStatus(${item.id},'ACTIVE')">Restore</button>`
                : `<button class="btn-secondary" style="padding:4px 8px;font-size:0.72rem;" onclick="cqaSetStatus(${item.id},'${item.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE'}')">${item.status === 'ACTIVE' ? 'Disable' : 'Enable'}</button>
                   <button class="btn-secondary" style="padding:4px 8px;font-size:0.72rem;" onclick="cqaSetStatus(${item.id},'ARCHIVED')">Archive</button>`;
            return `
            <tr>
                <td style="max-width:140px;">${escapeHtml(item.topic || '')}</td>
                <td style="max-width:220px;">
                    <b>${escapeHtml(item.primary_question || '')}</b>
                    ${aliasCount ? `<br><small style="color:var(--text-muted);">+${aliasCount} alternative question${aliasCount === 1 ? '' : 's'}</small>` : ''}
                </td>
                <td style="max-width:280px; font-size:0.82rem; white-space:pre-wrap;">${escapeHtml((item.approved_answer || '').slice(0, 220))}${(item.approved_answer || '').length > 220 ? '…' : ''}</td>
                <td>${statusBadge}</td>
                <td>${priorityBadge}</td>
                <td><span class="badge badge-veg">${escapeHtml(item.source_label || item.source || '')}</span></td>
                <td style="font-size:0.78rem;">Used ${item.usage_count || 0} time${item.usage_count === 1 ? '' : 's'}<br>Last used: ${item.last_used_at ? timeAgo(item.last_used_at) : 'Never'}</td>
                <td style="font-size:0.76rem; color:var(--text-muted);">${item.updated_at ? timeAgo(item.updated_at) : ''}${item.updated_by ? `<br>by ${escapeHtml(item.updated_by)}` : ''}</td>
                <td style="white-space:nowrap;">
                    <button class="btn-secondary" style="padding:4px 8px;font-size:0.72rem;" onclick="cqaQuickTest(${item.id})">Test</button>
                    <button class="btn-secondary" style="padding:4px 8px;font-size:0.72rem;" onclick="cqaQuickPreview(${item.id})">Preview</button>
                    <button class="btn-secondary" style="padding:4px 8px;font-size:0.72rem;" onclick="cqaOpenEdit(${item.id})">Edit</button>
                    ${lifecycleAction}
                    <button class="btn-secondary" style="padding:4px 8px;font-size:0.72rem;" onclick="cqaOpenHistory(${item.id})">History</button>
                    <button class="btn-secondary" style="padding:4px 8px;font-size:0.72rem;color:var(--danger);" onclick="cqaOpenDeleteConfirm(${item.id})">Delete</button>
                </td>
            </tr>`;
        }

        async function cqaSetStatus(id, status) {
            try {
                const resp = await fetch(`/admin/custom-qa/${id}/status`, {
                    method: 'POST', headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                    body: JSON.stringify({ status, actor: 'client_admin_dashboard' }),
                });
                if (resp.ok) { loadCustomQa(); refreshAiResolutionIfVisible(); }
                else alert('Could not update status.');
            } catch (e) { alert('Could not update status.'); }
        }

        async function cqaQuickTest(id) {
            const message = window.prompt('Sample customer question to test against this Q&A:');
            if (!message) return;
            try {
                const resp = await fetch('/admin/custom-qa/test', {
                    method: 'POST', headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message, focus_id: id }),
                });
                const data = await resp.json();
                if (!data.would_match) { alert('Would Match: NO — nothing in Custom Q&A matches that question.'); return; }
                let text = `Would Match: YES\nMatched Q&A: ${data.matched.topic} — ${data.matched.primary_question}\nMatched Via: ${data.matched_via}\nScore: ${Math.round(data.score * 100)}%\nWould This Q&A Win: ${data.would_win ? 'YES' : 'NO'}`;
                if (!data.would_win) text += `\nWinning Q&A: ${data.winning_qa.topic} — ${data.winning_qa.primary_question}\nReason: ${data.reason}`;
                if (data.ambiguous) text += '\n\n⚠ Two answers are close in score — consider making the question more specific.';
                alert(text);
            } catch (e) { alert('Test failed.'); }
        }

        async function cqaQuickPreview(id) {
            try {
                const resp = await fetch('/admin/custom-qa/preview', {
                    method: 'POST', headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id }),
                });
                const data = await resp.json();
                alert(`${data.preview_text}\n\n(${data.note})`);
            } catch (e) { alert('Preview failed.'); }
        }

        function cqaOpenDeleteConfirm(id) {
            const item = customQaItems.find(i => i.id === id);
            if (!item) return;
            document.getElementById('cqaDeleteDetails').innerHTML = `
                <div><b>Topic:</b> ${escapeHtml(item.topic || '')}</div>
                <div><b>Question:</b> ${escapeHtml(item.primary_question || '')}</div>
                <div><b>Source:</b> ${escapeHtml(item.source_label || '')}</div>
                <div><b>Usage Count:</b> ${item.usage_count || 0}</div>`;
            document.getElementById('customQaDeleteModal').dataset.id = id;
            document.getElementById('customQaDeleteModal').style.display = 'flex';
        }
        function closeCustomQaDeleteModal() { document.getElementById('customQaDeleteModal').style.display = 'none'; }
        async function confirmCustomQaDelete() {
            const id = document.getElementById('customQaDeleteModal').dataset.id;
            try {
                const resp = await fetch(`/admin/custom-qa/${id}`, { method: 'DELETE', headers: getAuthHeaders() });
                if (resp.ok) { closeCustomQaDeleteModal(); loadCustomQa(); refreshAiResolutionIfVisible(); }
                else alert('Could not delete.');
            } catch (e) { alert('Could not delete.'); }
        }

        async function cqaOpenHistory(id) {
            const body = document.getElementById('cqaHistoryBody');
            body.innerHTML = 'Loading…';
            document.getElementById('customQaHistoryModal').style.display = 'flex';
            document.getElementById('customQaHistoryModal').dataset.id = id;
            try {
                const resp = await fetch(`/admin/custom-qa/${id}/versions`, { headers: getAuthHeaders() });
                const data = await resp.json();
                const versions = data.versions || [];
                if (!versions.length) { body.innerHTML = '<p style="color:var(--text-muted);">No edits yet — this Q&A has not been changed since it was created.</p>'; return; }
                body.innerHTML = versions.map(v => `
                    <div style="border:1px solid var(--border); border-radius:10px; padding:12px; margin-bottom:10px;">
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <b>Version ${v.version_number}</b>
                            <button class="btn-secondary" style="padding:4px 10px;font-size:0.75rem;" onclick="cqaRestoreVersion(${id},${v.version_id})">Restore</button>
                        </div>
                        <div style="font-size:0.8rem; color:var(--text-muted); margin:4px 0;">${escapeHtml(v.changed_by || 'unknown')} · ${escapeHtml(v.changed_at || '')} · ${escapeHtml(v.change_reason || '')}</div>
                        <div style="font-size:0.82rem;"><b>Topic:</b> ${escapeHtml(v.topic || '')}</div>
                        <div style="font-size:0.82rem;"><b>Question:</b> ${escapeHtml(v.primary_question || '')}</div>
                        <div style="font-size:0.82rem; white-space:pre-wrap;"><b>Answer:</b> ${escapeHtml(v.answer || '')}</div>
                        <div style="font-size:0.78rem; color:var(--text-muted);"><b>Priority:</b> ${escapeHtml(v.priority || '')} · <b>Aliases:</b> ${(v.aliases_snapshot || []).map(escapeHtml).join(', ') || '—'}</div>
                    </div>`).join('');
            } catch (e) { body.innerHTML = '<p style="color:red;">Could not load history.</p>'; }
        }
        function closeCustomQaHistoryModal() { document.getElementById('customQaHistoryModal').style.display = 'none'; }
        async function cqaRestoreVersion(id, versionId) {
            if (!confirm('Restore this version?')) return;
            try {
                const resp = await fetch(`/admin/custom-qa/${id}/restore/${versionId}`, {
                    method: 'POST', headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                    body: JSON.stringify({ actor: 'client_admin_dashboard' }),
                });
                if (resp.ok) { closeCustomQaHistoryModal(); loadCustomQa(); }
                else alert('Could not restore this version.');
            } catch (e) { alert('Could not restore this version.'); }
        }

        // -- Add / Edit modal ---------------------------------------------

        const CQA_TOPIC_PRESETS = ['Pricing', 'Delivery', 'Corporate Gifting', 'Meal Subscription', 'Sondesh', 'Events', 'Careers'];

        function onCqaTopicSelectChange() {
            const select = document.getElementById('cqaTopicSelect');
            document.getElementById('cqaTopicCustom').style.display = select.value === 'Other' ? 'block' : 'none';
        }

        function renderCqaAliasList() {
            const list = document.getElementById('cqaAliasList');
            list.innerHTML = cqaAliasesDraft.map((a, idx) => `
                <div style="display:flex; gap:6px; align-items:center; margin-bottom:4px;">
                    <span style="flex:1; background:#F8FAFC; border:1px solid var(--border); border-radius:6px; padding:6px 10px; font-size:0.82rem;">${escapeHtml(a)}</span>
                    <button class="btn-secondary" type="button" style="padding:2px 8px; font-size:0.75rem;" onclick="removeCqaAlias(${idx})">✕</button>
                </div>`).join('');
        }
        function addCqaAlias() {
            const input = document.getElementById('cqaAliasInput');
            const value = input.value.trim();
            if (!value) return;
            cqaAliasesDraft.push(value);
            input.value = '';
            renderCqaAliasList();
        }
        function removeCqaAlias(idx) { cqaAliasesDraft.splice(idx, 1); renderCqaAliasList(); }

        function openCustomQaModal(item) {
            document.getElementById('cqaEditingId').value = item ? item.id : '';
            document.getElementById('customQaModalTitle').innerText = item ? 'Edit Q&A' : '+ Add Q&A';
            const topicSelect = document.getElementById('cqaTopicSelect');
            const topic = item ? item.topic : '';
            if (topic && CQA_TOPIC_PRESETS.includes(topic)) {
                topicSelect.value = topic;
                document.getElementById('cqaTopicCustom').style.display = 'none';
                document.getElementById('cqaTopicCustom').value = '';
            } else {
                topicSelect.value = 'Other';
                document.getElementById('cqaTopicCustom').style.display = 'block';
                document.getElementById('cqaTopicCustom').value = topic || '';
            }
            document.getElementById('cqaPrimaryQuestion').value = item ? item.primary_question : '';
            document.getElementById('cqaAnswer').value = item ? item.approved_answer : '';
            document.getElementById('cqaPriority').value = item ? item.priority : 'NORMAL';
            document.getElementById('cqaStatus').value = item ? item.status : 'ACTIVE';
            document.getElementById('cqaSourceLabel').innerText = item ? (item.source_label || item.source || '') : 'Created by Client';
            cqaAliasesDraft = item ? [...(item.aliases || [])] : [];
            renderCqaAliasList();
            document.getElementById('cqaTestQuestion').value = '';
            document.getElementById('cqaTestResult').innerHTML = '';
            document.getElementById('cqaWarningsBox').style.display = 'none';
            document.getElementById('cqaWarningsBox').innerHTML = '';
            document.getElementById('customQaModal').style.display = 'flex';
        }
        function closeCustomQaModal() { document.getElementById('customQaModal').style.display = 'none'; }

        function cqaOpenEdit(id) {
            const item = customQaItems.find(i => i.id === id);
            if (item) openCustomQaModal(item);
        }

        function currentCqaTopic() {
            const select = document.getElementById('cqaTopicSelect');
            return select.value === 'Other' ? document.getElementById('cqaTopicCustom').value.trim() : select.value;
        }

        async function runCqaTest() {
            const message = document.getElementById('cqaTestQuestion').value.trim();
            const resultBox = document.getElementById('cqaTestResult');
            if (!message) { resultBox.innerHTML = 'Enter a sample question first.'; return; }
            const editingId = document.getElementById('cqaEditingId').value;
            try {
                const resp = await fetch('/admin/custom-qa/test', {
                    method: 'POST', headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message, focus_id: editingId || null }),
                });
                const data = await resp.json();
                if (!data.would_match) { resultBox.innerHTML = '<b>Would Match:</b> NO'; return; }
                let html = `<b>Would Match:</b> YES<br><b>Matched Q&A:</b> ${escapeHtml(data.matched.topic)} — ${escapeHtml(data.matched.primary_question)}<br><b>Matched Via:</b> ${escapeHtml(data.matched_via)}<br><b>Score:</b> ${Math.round(data.score * 100)}%<br><b>Would This Q&A Win:</b> ${data.would_win ? 'YES' : 'NO'}`;
                if (!data.would_win) html += `<br><b>Winning Q&A:</b> ${escapeHtml(data.winning_qa.topic)} — ${escapeHtml(data.winning_qa.primary_question)}<br><b>Reason:</b> ${escapeHtml(data.reason)}`;
                if (data.ambiguous) html += '<br>⚠ Two answers are close in score.';
                resultBox.innerHTML = html;
            } catch (e) { resultBox.innerHTML = 'Test failed.'; }
        }

        function runCqaPreview() {
            const answer = document.getElementById('cqaAnswer').value;
            document.getElementById('cqaTestResult').innerHTML = `<div style="white-space:pre-wrap; background:#F8FAFC; border:1px solid var(--border); border-radius:8px; padding:10px;">${escapeHtml(answer)}</div><small style="color:var(--text-muted);">Preview only — nothing has been sent.</small>`;
        }

        async function saveCustomQa(forceSave) {
            const id = document.getElementById('cqaEditingId').value;
            const payload = {
                topic: currentCqaTopic(),
                primary_question: document.getElementById('cqaPrimaryQuestion').value.trim(),
                approved_answer: document.getElementById('cqaAnswer').value.trim(),
                aliases: cqaAliasesDraft,
                priority: document.getElementById('cqaPriority').value,
                status: document.getElementById('cqaStatus').value,
                force_save: !!forceSave,
            };
            if (!payload.topic || !payload.primary_question || !payload.approved_answer) {
                alert('Topic, Primary Question, and Approved Answer are required.');
                return;
            }
            try {
                const resp = await fetch(id ? `/admin/custom-qa/${id}` : '/admin/custom-qa', {
                    method: id ? 'PATCH' : 'POST',
                    headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const data = await resp.json().catch(() => ({}));
                if (resp.status === 409) {
                    const existing = (data.detail && data.detail.existing) || [];
                    alert('An identical question or alias already exists:\n' + existing.map(e => `${e.topic} — ${e.primary_question}`).join('\n') + '\n\nEdit the existing Q&A instead.');
                    return;
                }
                if (!resp.ok) { alert('Could not save: ' + (data.detail || resp.status)); return; }
                if (data.status === 'warning') {
                    let msg = '';
                    if (data.similar && data.similar.length) {
                        msg += 'Similar Q&A already exists:\n' + data.similar.map(s => `${s.topic} — ${s.primary_question}\n${(s.approved_answer || '').slice(0, 120)}`).join('\n\n') + '\n\n';
                    }
                    if (data.commitment_warnings && data.commitment_warnings.length) {
                        msg += '⚠ This answer appears to contain: ' + data.commitment_warnings.join(', ') + '. Please confirm this information is current and approved.\n\n';
                    }
                    msg += 'Save anyway?';
                    if (confirm(msg)) { saveCustomQa(true); }
                    return;
                }
                closeCustomQaModal();
                loadCustomQa();
                refreshAiResolutionIfVisible();
            } catch (e) { alert('Could not save Q&A.'); }
        }

        // -- CSV export / import -------------------------------------------

        async function downloadBlobFrom(url, filename) {
            const resp = await fetch(url, { headers: getAuthHeaders() });
            if (!resp.ok) { alert('Download failed.'); return; }
            const blob = await resp.blob();
            const objectUrl = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = objectUrl; a.download = filename;
            document.body.appendChild(a); a.click(); a.remove();
            URL.revokeObjectURL(objectUrl);
        }
        function exportCustomQaCsv() { downloadBlobFrom('/admin/custom-qa/export.csv?status=ALL', 'custom_qa_export.csv'); }
        function downloadCustomQaTemplate() { downloadBlobFrom('/admin/custom-qa/csv-template', 'custom_qa_template.csv'); }

        function openCustomQaImportModal() {
            document.getElementById('cqaImportFile').value = '';
            document.getElementById('cqaImportSummary').innerHTML = '';
            document.getElementById('cqaImportRows').innerHTML = '<tr><td colspan="4" style="text-align:center; color:#888;">Upload a CSV and click Validate.</td></tr>';
            document.getElementById('cqaImportConfirmBtn').style.display = 'none';
            document.getElementById('customQaImportModal').style.display = 'flex';
        }
        function closeCustomQaImportModal() { document.getElementById('customQaImportModal').style.display = 'none'; }

        async function runCustomQaImportDryRun() {
            const fileInput = document.getElementById('cqaImportFile');
            if (!fileInput.files.length) { alert('Choose a CSV file first.'); return; }
            const form = new FormData();
            form.append('file', fileInput.files[0]);
            try {
                const resp = await fetch('/admin/custom-qa/import/dry-run', { method: 'POST', headers: getAuthHeaders(), body: form });
                const data = await resp.json();
                if (!resp.ok) { alert('Validation failed.'); return; }
                const s = data.summary;
                document.getElementById('cqaImportSummary').innerHTML = `Rows: ${s.rows} · Valid: ${s.valid} · Warnings: ${s.warnings} · Duplicates: ${s.duplicates} · Possible Duplicates: ${s.possible_duplicates} · Errors: ${s.errors}`;
                document.getElementById('cqaImportRows').innerHTML = data.rows.map(r => `
                    <tr>
                        <td>${r.row_number}</td>
                        <td style="max-width:220px;">${escapeHtml((r.data && r.data.primary_question) || '')}</td>
                        <td><span class="badge ${r.row_status === 'Ready' ? 'badge-veg' : 'badge-nonveg'}">${r.row_status}</span></td>
                        <td style="font-size:0.78rem;">${escapeHtml((r.errors || []).concat(r.warnings || []).join('; '))}</td>
                    </tr>`).join('');
                document.getElementById('cqaImportConfirmBtn').style.display = s.valid > 0 ? 'inline-block' : 'none';
            } catch (e) { alert('Validation failed.'); }
        }

        async function confirmCustomQaImport() {
            const fileInput = document.getElementById('cqaImportFile');
            if (!fileInput.files.length) { alert('Choose a CSV file first.'); return; }
            const form = new FormData();
            form.append('file', fileInput.files[0]);
            try {
                const resp = await fetch('/admin/custom-qa/import/confirm', { method: 'POST', headers: getAuthHeaders(), body: form });
                const data = await resp.json();
                if (!resp.ok) { alert('Import failed.'); return; }
                alert(`Imported ${data.imported} row(s). Skipped ${data.skipped.length} row(s).`);
                closeCustomQaImportModal();
                loadCustomQa();
            } catch (e) { alert('Import failed.'); }
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
                    <option value="whatsapp">WhatsApp Business 🛑 Human-Only in Production (engine preview only)</option>
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

