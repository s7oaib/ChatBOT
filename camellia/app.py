import asyncio
import hmac
import hashlib
import json
import os
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, Query, Header, HTTPException, File, UploadFile, BackgroundTasks
from fastapi.responses import PlainTextResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from typing import Dict, Any, Optional
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Imported through config, never directly: `camellia.config` loads .env before
# it triggers the bootstrap, and importing the bootstrap first would run it
# against a pre-.env environment and seed the wrong paths.
from camellia.config import config, STARTUP_ACTIONS
from camellia.database import db
from camellia.ad_gate import ad_gate
from camellia.responder import responder
from camellia.gemini_responder import gemini_responder
from camellia.whatsapp_adapter import whatsapp_adapter
from camellia.escalator import escalator
from camellia.sheet_sync import sheet_sync
from camellia.menu_parser import menu_parser
from camellia.session_memory import session_memory
from camellia.monitoring import get_logger, delivery_monitor, latency_monitor

log = get_logger("camellia.app")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Enforces database init and the retention window promised on /privacy on startup."""
    # Seeding ran during import, before any path was resolved, so this is the
    # first moment it can be reported.
    for action in STARTUP_ACTIONS:
        log.info("Storage bootstrap: %s", action)

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

def _process_instagram_event(sender_id: str, message_text: str, ad_id: Optional[str]) -> None:
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
        response_text, was_matched, template_id, image_url = responder.match_and_respond(
            message_text, user_id=sender_id, channel="instagram",
            referral_source="ADS", referral_ad_id=ad_id,
        )

        db.log_conversation(sender_id, "instagram", message_text, response_text, was_matched, template_id)

        if not was_matched or template_id == "order_next_steps":
            escalator.handle_escalation(
                user_id=sender_id,
                channel="instagram",
                user_message=message_text,
                reason="Order Confirmation & Intake Request" if was_matched else "Unmatched Instagram Query / Sensitive Request",
                context_data={"ad_id": ad_id, "sender_id": sender_id, "template_id": template_id},
            )

        send_instagram_reply(sender_id, response_text, image_url=image_url)
    except Exception:
        # A background task that raises disappears silently, and the customer
        # is left with no reply and nothing in the logs explaining why.
        log.exception("Failed to process Instagram message from %s.", sender_id)


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
            
            if not sender_id:
                log.info("Ignored Instagram event without sender ID (keys: %s).", list(messaging_event.keys()))
                results.append({"action": "ignored_no_sender"})
                continue

            if not message_text:
                log.info("Ignored Instagram event without text payload (sender: %s, keys: %s).", sender_id, list(messaging_event.keys()))
                results.append({"sender_id": sender_id, "action": "ignored_empty_text"})
                continue

            if not is_ad_qualified:
                # Hard Gate: Ignore organic messages per PRD Requirement #4
                log.info("Ad-gate: sender %s is a non-ad organic DM; left for manual reply.", sender_id)
                results.append({"sender_id": sender_id, "action": "ignored_organic_dm"})
                continue

            # Accepted. The reply itself is built and sent off the request path
            # so Meta gets its 200 immediately and does not retry this delivery.
            background_tasks.add_task(_process_instagram_event, sender_id, message_text, ad_id)
            results.append({
                "sender_id": sender_id,
                "action": "queued",
                "ad_id": ad_id,
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
    cards_first = any(caption for _, caption in captioned)

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
                if caption:
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
        "problems": problems,
    }


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


@app.get("/health")
def health_check():
    """Deep health check. Point an uptime monitor at this, not at /."""
    outbound = delivery_monitor.snapshot()
    answering = _answering_capability()
    delivery = _delivery_capability()
    signature = _webhook_signature_status()
    storage = _storage_durability()

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

    return Response(
        content=json.dumps({
            "status": "healthy" if healthy else "degraded",
            "warnings": warnings,
            "answering": answering,
            "storage": storage,
            "reply_delivery": delivery,
            "webhook_signature": signature,
            "outbound_delivery": outbound,
            # Not just which provider is configured — whether it last worked.
            "ai": gemini_responder.status(),
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
        return {"status": "success", "menu": data}
    except Exception as e:
        log.error("Failed to read menu.json: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

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
    Uploads a photo of a printed menu flyer or Instagram graphic, extracts dishes and prices with Gemini Vision,
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
    return result

@app.post("/admin/menu/reset-defaults")
def reset_menu_defaults(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    """Resets weekly menu dishes to default Bengal-rooted culinary baseline and reloads."""
    require_admin(x_admin_key, x_admin_pin)
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

    menu_path = config.MENU_JSON_PATH
    try:
        with open(menu_path, "r", encoding="utf-8") as f:
            menu_data = json.load(f)

        if "weekly_menus" in menu_data and menu_data["weekly_menus"]:
            current_week = menu_data["weekly_menus"][-1]
            if "week" in body:
                current_week["week"] = body["week"]
            if "vegetarian" in body and isinstance(body["vegetarian"], list):
                current_week["vegetarian"] = body["vegetarian"]
            if "non_vegetarian" in body and isinstance(body["non_vegetarian"], list):
                current_week["non_vegetarian"] = body["non_vegetarian"]

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

@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/dashboard", response_class=HTMLResponse)
def get_admin_dashboard():
    """
    Dedicated PIN-Protected Kitchen & Menu Management Portal for Camellia Artisanal.
    Features:
    - PIN Authentication (Configured ADMIN_PIN or ADMIN_API_KEY from environment)
    - 📋 Paste Menu Card Text Parser
    - 📸 Drag-and-Drop Image Flyer Upload with Gemini Vision OCR
    - 📊 Google Sheets Live Sync
    - 🍱 Active Menu Viewer & Baseline Reset
    - 📈 Bot Performance & Escalations
    """
    html_content = """<!DOCTYPE html>
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
            --primary: #8B1E3F;
            --primary-dark: #65122B;
            --primary-light: #A8284F;
            --gold: #D4AF37;
            --gold-light: #FBF4E4;
            --bg: #F8F9FA;
            --surface: #FFFFFF;
            --text-dark: #2B2D42;
            --text-muted: #6C757D;
            --border: #E8DFD8;
            --success: #10B981;
            --warning: #F59E0B;
            --danger: #EF4444;
            --shadow: 0 10px 25px rgba(139, 30, 63, 0.08);
            --shadow-lg: 0 20px 40px rgba(139, 30, 63, 0.12);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }
        body { background: var(--bg); color: var(--text-dark); min-height: 100vh; display: flex; flex-direction: column; }
        
        /* PIN Lock Overlay */
        .auth-overlay {
            position: fixed; inset: 0; background: linear-gradient(135deg, rgba(101,18,43,0.96), rgba(139,30,63,0.98));
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
            background: linear-gradient(90deg, var(--primary-dark), var(--primary)); color: white;
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
        .status-items { display: flex; gap: 20px; align-items: center; }
        .status-pill { display: inline-flex; align-items: center; gap: 6px; font-weight: 500; }
        .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
        .dot.green { background: var(--success); box-shadow: 0 0 8px rgba(16,185,129,0.5); }
        .dot.gold { background: var(--gold); }
        
        /* Main Layout */
        .main-container { max-width: 1100px; width: 100%; margin: 24px auto; padding: 0 20px 40px; flex: 1; }
        
        /* Navigation Tabs */
        .tabs-nav {
            display: flex; gap: 8px; background: #EDE8E3; padding: 6px; border-radius: 16px;
            margin-bottom: 24px; overflow-x: auto; scrollbar-width: none;
        }
        .tab-btn {
            flex: 1; padding: 12px 18px; border: none; background: transparent; border-radius: 12px;
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
            .status-bar { padding: 10px 16px; }
            .main-container { padding: 0 14px 30px; }
            .tabs-nav { justify-content: flex-start; }
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
            <span class="brand-logo">🌸</span>
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
            <span class="status-pill"><span class="dot green"></span> Server: <b>Online</b></span>
            <span class="status-pill"><span class="dot gold"></span> Gemini Vision OCR: <b>Ready</b></span>
            <span class="status-pill">Active Menu Items: <b id="activeCountBadge">Loading...</b></span>
        </div>
        <div>
            <span>Escalation Contact: <b>Camellia Kitchen Team</b> (+91 87965 51990)</span>
        </div>
    </div>

    <!-- Main Workspace -->
    <div class="main-container">
        <!-- Tab Navigation Bar -->
        <div class="tabs-nav">
            <button class="tab-btn active" onclick="switchTab('tab-paste', this)">📋 Paste Menu Text</button>
            <button class="tab-btn" onclick="switchTab('tab-image', this)">📸 Flyer Photo (Vision OCR)</button>
            <button class="tab-btn" onclick="switchTab('tab-sheets', this)">📊 Google Sheets Sync</button>
            <button class="tab-btn" onclick="switchTab('tab-active', this)">🍱 Active Menu Viewer</button>
            <button class="tab-btn" onclick="switchTab('tab-stats', this)">📈 Escalations & Self-Healing</button>
            <button class="tab-btn" onclick="switchTab('tab-conversations', this)">💬 Live Conversations</button>
            <button class="tab-btn" onclick="switchTab('tab-knowledge', this)">🧠 Learned Knowledge</button>
            <button class="tab-btn" onclick="switchTab('tab-candidates', this)">✅ Suggested Answers <span id="candidateBadge" style="display:none; background: var(--danger); color: #fff; border-radius: 10px; padding: 1px 7px; font-size: 0.72rem; margin-left: 4px;"></span></button>
        </div>

        <!-- TAB 1: Paste Menu Text -->
        <div id="tab-paste" class="panel-card">
            <div class="panel-header">
                <h2>📋 Quick Paste Menu Text (WhatsApp / Notes)</h2>
                <p>Paste raw unstructured flyer text, WhatsApp broadcast message, or dish lists. The AI extractor categorizes veg/non-veg, captures prices, and updates the bot instantly.</p>
            </div>
            
            <textarea id="rawTextPaste" class="text-input" placeholder="Example:
🌸 THIS WEEK'S BENGAL SPECIALS 🌸
- Rose Sondesh (250g) - ₹450
- Jolbhora Nolen Gur (500g) - ₹1400
- Kosha Mangsho (Mutton) (350g) - ₹650
- Chholar Daal with Coconut (250g) - ₹250
- Rui Machher Kalia 2 pcs - ₹450"></textarea>

            <div class="action-bar">
                <button class="btn-primary" onclick="submitMenuText()">✨ Auto-Extract Dishes & Update Bot</button>
                <button class="btn-secondary" onclick="loadSampleText()">📝 Load Sample Text</button>
            </div>

            <div id="pasteResult" class="result-box"></div>
        </div>

        <!-- TAB 2: Image Flyer Upload (Vision OCR) -->
        <div id="tab-image" class="panel-card" style="display: none;">
            <div class="panel-header">
                <h2>📸 Drag-and-Drop Menu Image Flyer (Gemini Vision OCR)</h2>
                <p>Upload a photo of your printed menu card, packaging flyer, or Instagram story. Gemini Vision reads the image and hot-reloads dishes into the live bot.</p>
            </div>

            <div class="drop-zone" id="dropZone" onclick="document.getElementById('fileInput').click()">
                <div class="drop-zone-icon">📷</div>
                <h3>Drag & Drop Menu Card Image or Click to Browse</h3>
                <p>Supports JPEG, PNG, WEBP (Flyer photo, WhatsApp image, Instagram banner)</p>
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
                <button class="btn-primary" id="uploadImageBtn" onclick="submitMenuImage()">🔍 Scan Flyer with Gemini Vision OCR</button>
                <button class="btn-secondary" onclick="testSampleFlyer()">🧪 Test Sample Menu Flyer (1-Click Test)</button>
            </div>

            <div id="imageResult" class="result-box"></div>
        </div>

        <!-- TAB 3: Google Sheets Sync -->
        <div id="tab-sheets" class="panel-card" style="display: none;">
            <div class="panel-header">
                <h2>📊 Google Sheets Live Menu Sync</h2>
                <p>Synchronize your weekly menu directly from a shared Google Sheet. Update prices or add weekend specials in Google Sheets and sync in 1 click.</p>
            </div>

            <div style="margin-bottom: 16px;">
                <label style="font-size: 0.88rem; font-weight: 600; display: block; margin-bottom: 6px;">Published Google Sheet CSV URL:</label>
                <input type="url" id="sheetUrlInput" style="width: 100%; padding: 12px 14px; border: 1.5px solid var(--border); border-radius: 12px; font-size: 0.9rem; outline: none;" placeholder="https://docs.google.com/spreadsheets/d/.../pub?output=csv">
            </div>

            <div class="action-bar">
                <button class="btn-primary" onclick="syncGoogleSheet()">🔄 Sync Live from Google Sheet</button>
                <button class="btn-secondary" onclick="syncLocalDisk()">📂 Reload from Local Disk Files</button>
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

        <!-- TAB 4: Active Menu Viewer & Reset -->
        <div id="tab-active" class="panel-card" style="display: none;">
            <div class="panel-header">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <h2>🍱 Active Weekly Menu & Standing Collections</h2>
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

        <!-- TAB 5: Escalations & Analytics -->
        <div id="tab-stats" class="panel-card" style="display: none;">
            <div class="panel-header">
                <h2>📈 Bot Intelligence & Escalations Log</h2>
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

        <!-- TAB 6: Live Customer Conversations -->
        <div id="tab-conversations" class="panel-card" style="display: none;">
            <div class="panel-header">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <h2>💬 Live Customer Conversations</h2>
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

        <!-- TAB 7: Learned Knowledge Base -->
        <div id="tab-knowledge" class="panel-card" style="display: none;">
            <div class="panel-header">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <h2>🧠 Self-Healing Learned Knowledge</h2>
                        <p>Verified Q&A items taught by kitchen staff or auto-learned from resolved customer queries.</p>
                    </div>
                    <button class="btn-primary" style="font-size: 0.85rem;" onclick="toggleAddKnowledgeForm()">+ Add Verified Q&A</button>
                </div>
            </div>

            <!-- Add Knowledge Inline Form -->
            <div id="addKnowledgeForm" style="display: none; background: #F8FAFC; border: 1.5px solid var(--border); border-radius: 12px; padding: 18px; margin-bottom: 20px;">
                <h3 style="margin-top: 0; font-size: 1rem; color: var(--primary);">➕ Teach Bot New Verified Answer</h3>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px;">
                    <div>
                        <label style="font-size: 0.82rem; font-weight: 600; display: block; margin-bottom: 4px;">Topic / Category:</label>
                        <input type="text" id="newTopic" placeholder="e.g. Corporate Gifting, Custom Packaging" style="width: 100%; padding: 10px; border-radius: 8px; border: 1px solid var(--border);">
                    </div>
                    <div>
                        <label style="font-size: 0.82rem; font-weight: 600; display: block; margin-bottom: 4px;">Trigger / Question Keyword:</label>
                        <input type="text" id="newPattern" placeholder="e.g. corporate orders, wedding packaging" style="width: 100%; padding: 10px; border-radius: 8px; border: 1px solid var(--border);">
                    </div>
                </div>
                <div style="margin-bottom: 12px;">
                    <label style="font-size: 0.82rem; font-weight: 600; display: block; margin-bottom: 4px;">Verified Answer for Customer:</label>
                    <textarea id="newAnswer" placeholder="Type official response..." style="width: 100%; height: 80px; padding: 10px; border-radius: 8px; border: 1px solid var(--border);"></textarea>
                </div>
                <div style="display: flex; gap: 8px;">
                    <button class="btn-primary" onclick="submitNewKnowledge()">💾 Save & Activate</button>
                    <button class="btn-secondary" onclick="toggleAddKnowledgeForm()">Cancel</button>
                </div>
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

        <!-- TAB 8: Candidate Answers Awaiting Approval -->
        <div class="panel-card" id="tab-candidates" style="display: none;">
            <div class="panel-header">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <div>
                        <h2>✅ Suggested Answers Awaiting Your Approval</h2>
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
        let currentAdminKey = sessionStorage.getItem('camellia_admin_pin') || '';
        let selectedFile = null;

        // On Load: check if authenticated
        window.addEventListener('DOMContentLoaded', () => {
            if (currentAdminKey) {
                document.getElementById('authOverlay').style.display = 'none';
                loadActiveMenu();
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
                    currentAdminKey = pin;
                    sessionStorage.setItem('camellia_admin_pin', pin);
                    document.getElementById('authOverlay').style.display = 'none';
                    loadActiveMenu();
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
                alert('Connection error verifying PIN.');
            }
        }

        function lockPortal() {
            sessionStorage.removeItem('camellia_admin_pin');
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

        function switchTab(tabId, el) {
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            if (el) el.classList.add('active');
            
            document.querySelectorAll('.panel-card').forEach(panel => panel.style.display = 'none');
            const target = document.getElementById(tabId);
            if (target) target.style.display = 'block';

            if (tabId === 'tab-active') loadActiveMenu();
            if (tabId === 'tab-stats') loadAnalytics();
            if (tabId === 'tab-conversations') loadConversations();
            if (tabId === 'tab-knowledge') loadKnowledge();
            if (tabId === 'tab-candidates') loadCandidates();
        }

        // TAB 1: Submit Text
        async function submitMenuText() {
            const text = document.getElementById('rawTextPaste').value.trim();
            const resBox = document.getElementById('pasteResult');
            if (!text) return alert('Please enter or paste menu card text.');

            resBox.style.display = 'block';
            resBox.className = 'result-box info';
            resBox.innerHTML = '⏳ <b>Extracting dishes and updating menu.json...</b>';

            try {
                const resp = await fetch('/admin/parse-menu-card', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
                    body: JSON.stringify({ raw_text: text })
                });
                const data = await resp.json();

                if (resp.ok && data.status === 'success') {
                    resBox.className = 'result-box success';
                    resBox.innerHTML = `✅ <b>Menu Successfully Updated!</b><br>${data.message || ''}<br>Updated: <b>${data.vegetarian_count || 0} Veg</b>, <b>${data.non_vegetarian_count || 0} Non-Veg</b> items.<pre>${JSON.stringify(data.parsed, null, 2)}</pre>`;
                    loadActiveMenu();
                } else {
                    resBox.className = 'result-box error';
                    resBox.innerHTML = `⚠️ <b>Extraction Failed:</b> ${data.detail || data.reason || 'Could not parse text.'}`;
                }
            } catch (err) {
                resBox.className = 'result-box error';
                resBox.innerHTML = `❌ Network error communicating with server.`;
            }
        }

        function loadSampleText() {
            document.getElementById('rawTextPaste').value = `🌸 Camellia Artisanal Weekly Specials 🌸
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

        // TAB 2: Image Drag & Drop & Upload
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
            resBox.innerHTML = '🔍 <b>Scanning flyer image with Gemini Vision OCR...</b> Please wait.';

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
        async function testSampleFlyer() {
            const resBox = document.getElementById('imageResult');
            resBox.style.display = 'block';
            resBox.className = 'result-box info';
            resBox.innerHTML = '🧪 <b>Generating test flyer canvas & running Vision OCR pipeline...</b>';

            // Create a small sample image in memory on canvas
            const canvas = document.createElement('canvas');
            canvas.width = 400;
            canvas.height = 300;
            const ctx = canvas.getContext('2d');
            ctx.fillStyle = '#8B1E3F';
            ctx.fillRect(0, 0, 400, 300);
            ctx.fillStyle = '#FFFFFF';
            ctx.font = 'bold 18px sans-serif';
            ctx.fillText('CAMELLIA ARTISANAL MENU', 30, 40);
            ctx.font = '14px sans-serif';
            ctx.fillText('• Classic Rose Sondesh (250g) - Rs 450', 30, 90);
            ctx.fillText('• Mutton Kosha Bento (350g) - Rs 650', 30, 130);
            ctx.fillText('• Chholar Daal (250g) - Rs 250', 30, 170);
            ctx.fillText('• Rui Machher Kalia (2 pcs) - Rs 450', 30, 210);

            canvas.toBlob(async (blob) => {
                const formData = new FormData();
                formData.append('file', blob, 'sample_test_flyer.jpg');

                try {
                    const resp = await fetch('/admin/menu/upload-image', {
                        method: 'POST',
                        headers: getAuthHeaders(),
                        body: formData
                    });
                    const data = await resp.json();
                    if (data.status === 'success') {
                        resBox.className = 'result-box success';
                        resBox.innerHTML = `✅ <b>1-Click Test Succeeded!</b><br>${data.message}<pre>${JSON.stringify(data.parsed, null, 2)}</pre>`;
                        loadActiveMenu();
                    } else {
                        resBox.className = 'result-box info';
                        resBox.innerHTML = `🧪 <b>Test Pipeline Result:</b> ${data.message || data.reason || JSON.stringify(data)}`;
                    }
                } catch (e) {
                    resBox.className = 'result-box error';
                    resBox.innerHTML = 'Error running test flyer.';
                }
            }, 'image/jpeg');
        }

        // TAB 3: Google Sheets Sync
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

        async function syncLocalDisk() {
            const resBox = document.getElementById('sheetResult');
            resBox.style.display = 'block';
            resBox.className = 'result-box info';
            resBox.innerHTML = '📂 Reloading menu from disk files...';

            try {
                const resp = await fetch('/admin/sync-menu', {
                    method: 'POST',
                    headers: getAuthHeaders()
                });
                const data = await resp.json();
                resBox.className = 'result-box success';
                resBox.innerHTML = `✅ ${data.message || 'Reloaded from disk.'}`;
                loadActiveMenu();
            } catch (e) {
                resBox.className = 'result-box error';
                resBox.innerHTML = 'Failed to reload from disk.';
            }
        }

        // TAB 4: Active Menu Viewer
        async function loadActiveMenu() {
            const container = document.getElementById('activeMenuContent');
            try {
                const resp = await fetch('/admin/menu/active', { headers: getAuthHeaders() });
                if (!resp.ok) throw new Error('Auth error or failed to load');
                const data = await resp.json();
                const menu = data.menu;

                let html = '';
                let totalCount = 0;

                if (menu.weekly_menus && menu.weekly_menus.length > 0) {
                    const currentWeek = menu.weekly_menus[menu.weekly_menus.length - 1];
                    const veg = currentWeek.vegetarian || [];
                    const nonveg = currentWeek.non_vegetarian || [];
                    totalCount = veg.length + nonveg.length;

                    html += `<h3 style="color: var(--primary); margin-top: 10px; font-size: 1.1rem;">🌱 Vegetarian Weekly Specials (${veg.length})</h3>`;
                    html += '<div class="menu-grid">';
                    veg.forEach(item => {
                        html += `
                        <div class="menu-card">
                            <div class="menu-card-header">
                                <span class="menu-item-name">${escapeHtml(item.item)}</span>
                                <span class="menu-item-price">₹${item.price_inr}</span>
                            </div>
                            <p class="menu-item-desc">${escapeHtml(item.description || '')} (${escapeHtml(item.quantity || '1 portion')})</p>
                            <span class="badge badge-veg">Vegetarian</span>
                        </div>`;
                    });
                    html += '</div>';

                    html += `<h3 style="color: var(--primary); margin-top: 24px; font-size: 1.1rem;">🍗 Non-Vegetarian Weekly Specials (${nonveg.length})</h3>`;
                    html += '<div class="menu-grid">';
                    nonveg.forEach(item => {
                        html += `
                        <div class="menu-card">
                            <div class="menu-card-header">
                                <span class="menu-item-name">${escapeHtml(item.item)}</span>
                                <span class="menu-item-price">₹${item.price_inr}</span>
                            </div>
                            <p class="menu-item-desc">${escapeHtml(item.description || '')} (${escapeHtml(item.quantity || '1 portion')})</p>
                            <span class="badge badge-nonveg">Non-Veg</span>
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

        // TAB 5: Analytics & Escalations
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

        // TAB 6: Live Customer Conversations
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

        // TAB 7: Learned Knowledge Base
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

        // TAB 8: Candidate answers awaiting approval.
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

        function toggleAddKnowledgeForm() {
            const form = document.getElementById('addKnowledgeForm');
            form.style.display = form.style.display === 'none' ? 'block' : 'none';
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
                    document.getElementById('newTopic').value = '';
                    document.getElementById('newPattern').value = '';
                    document.getElementById('newAnswer').value = '';
                    document.getElementById('addKnowledgeForm').style.display = 'none';
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
    return HTMLResponse(content=html_content)

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
                    <h2>🌸 Camellia Artisanal Bot Tester</h2>
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
                typingDiv.innerText = '🌸 Camellia is typing...';
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

                        if (caption) {
                            const capDiv = document.createElement('div');
                            capDiv.className = 'msg-content menu-card-caption';
                            capDiv.innerHTML = formatMarkdown(caption);
                            wrap.appendChild(capDiv);
                        }

                        div.appendChild(wrap);
                    });
                }

                const contentDiv = document.createElement('div');
                contentDiv.className = 'msg-content';
                contentDiv.innerHTML = formatMarkdown(text);
                div.appendChild(contentDiv);
                
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

