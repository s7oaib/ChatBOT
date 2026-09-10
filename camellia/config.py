import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Ensure stdout handles Unicode/emojis safely on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

# Load .env if present
# This module lives in camellia/, so the repo root is one level up. Everything
# below is resolved from the root, not from the package directory.
BASE_DIR = Path(__file__).resolve().parent.parent
# Seed configuration that ships with the image. Kept out of the runtime data
# directory because Docker mounts a volume there, which would hide these files.
DATA_DIR = BASE_DIR / "data"
load_dotenv(BASE_DIR / ".env", override=True)

# Must come after the environment is loaded and before the paths below are
# resolved: a volume starts empty, and `_resolve_path(must_exist=True)` would
# quietly fall back to the image's own copy if the seeded file were not already
# in place. `camellia.app` logs what this did once logging exists.
from camellia.bootstrap import STARTUP_ACTIONS  # noqa: E402


def _env_secret(env_var: str) -> str:
    """
    Reads a credential, tolerating the way hosting dashboards store values.

    A `.env` file has quoting rules and python-dotenv applies them, so
    ADMIN_PIN="1234" yields 1234. A dashboard variable has no such rules: the
    value is taken literally, so the same entry yields `"1234"` including the
    quotes. The credential then never matches what anyone types, and the
    failure is indistinguishable from a wrong PIN — the portal simply refuses a
    password that is correct.

    Surrounding matching quotes are stripped, and the substitution is logged so
    it is visible rather than magic. A credential that genuinely begins and ends
    with the same quote character cannot be expressed this way; use the API key
    instead.
    """
    raw = (os.getenv(env_var) or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        print(
            f"[config] {env_var} is wrapped in {raw[0]} quotes and they have been "
            f"removed. Dashboard variables are stored literally — remove the quotes "
            f"there so the value is unambiguous."
        )
        return raw[1:-1].strip()
    return raw


def _resolve_path(env_var: str, default: Path, must_exist: bool = False) -> str:
    """
    Resolves a configured path against the repo root rather than the working
    directory. A relative value like `data/menu.json` then means the same thing
    whether the process was started from the root, from a container, or from a
    test run — an absolute value is left untouched.

    `must_exist` guards the files the bot cannot work without. A deployment
    that still carries an older setting — `MENU_JSON_PATH=menu.json` from
    before these files moved into data/ — would otherwise start cleanly, load
    no templates at all, and answer every customer with the fallback. Rather
    than serve a bot with nothing to say, fall back to the file that shipped
    with the build and say so loudly in the logs.
    """
    raw = (os.getenv(env_var) or "").strip()
    if not raw:
        return str(default)

    path = Path(raw)
    resolved = path if path.is_absolute() else BASE_DIR / path

    if must_exist and not resolved.exists() and default.exists():
        print(
            f"[config] {env_var}={raw!r} does not exist ({resolved}). "
            f"Falling back to {default}. Update or remove {env_var} in this "
            f"environment — the file moved into data/."
        )
        return str(default)
    return str(resolved)


class Config:
    CONVERSATION_IMPROVEMENTS_ENABLED: bool = os.getenv("CONVERSATION_IMPROVEMENTS_ENABLED", "true").lower() in ("true", "1", "yes")
    # Meta & Webhook Config
    META_VERIFY_TOKEN: str = os.getenv("META_VERIFY_TOKEN", "camellia_verify_secret_2026")
    META_APP_SECRET: str = os.getenv("META_APP_SECRET", "")

    # Public Domain / ngrok URL for serving images & webhooks
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "")

    # When true, a webhook POST with a missing or wrong signature is rejected
    # even if META_APP_SECRET is unset — meaning an unconfigured deployment
    # stops answering rather than accepting forged customer messages.
    #
    # Left opt-in rather than defaulted on: turning it on before the secret is
    # actually set in the live environment would take a running bot off the air.
    # The intended order is set META_APP_SECRET, confirm /health reports
    # webhook_signature "enforced", then set this to true.
    REQUIRE_WEBHOOK_SIGNATURE: bool = os.getenv("REQUIRE_WEBHOOK_SIGNATURE", "false").lower() in ("true", "1", "t", "yes")

    # Protects /admin/* endpoints (customer logs & menu overwrite). Unset = open.
    ADMIN_API_KEY: str = _env_secret("ADMIN_API_KEY")
    ADMIN_PIN: str = _env_secret("ADMIN_PIN")  # Must be explicitly configured via ADMIN_PIN env; unconfigured blocks admin access

    # The browser chat tester writes real conversation rows and fires real
    # escalation alerts at the manager's phone, so it defaults OFF and has to be
    # switched on deliberately for local work.
    #
    # It used to default ON, which meant a deployment got a publicly reachable
    # /chat unless someone remembered to disable it. render.yaml set it false,
    # but the live service runs on Railway, which never reads that file — so
    # production ran with the tester open and only /health's warning said so.
    # A control that protects real customers must fail closed.
    ENABLE_WEB_TESTER: bool = os.getenv("ENABLE_WEB_TESTER", "false").lower() in ("true", "1", "t", "yes")

    # When true, auto-replies to all DMs regardless of ad referral (bypasses PRD #4 Ad-Gate)
    BYPASS_AD_GATE: bool = os.getenv("BYPASS_AD_GATE", "false").lower() in ("true", "1", "t", "yes")
    # Phase 3 of the router rollout. With this on, every message is also shown
    # to an LLM router that picks a template id, and its pick is logged beside
    # the keyword matcher's. It changes no reply and sends nothing — the point
    # is to measure where the two disagree before anything depends on it.
    # Off by default: it costs a Groq call per message.
    ROUTER_SHADOW_ENABLED: bool = os.getenv("ROUTER_SHADOW_ENABLED", "false").lower() in ("true", "1", "t", "yes")
    # The router runs on every message, so it is the call most likely to hit a
    # per-minute token ceiling — and it is a different job from writing a
    # reply, so it gets its own model rather than sharing GROQ_MODEL.
    #
    # Measured on this account's key (x-ratelimit-limit-tokens):
    #   openai/gpt-oss-20b    8,000 TPM     qwen/qwen3.8-27b     8,000 TPM
    #   openai/gpt-oss-120b   8,000 TPM     groq/compound-mini  70,000 TPM
    # At ~1,900 tokens a call that is ~4 router calls a minute against ~36,
    # and the 8,000 is already shared with refinement and grounded answers.
    # compound-mini picked slightly less well on a seven-case trial, which is
    # far too small to decide on — the shadow table records the model behind
    # every pick so the choice can be settled on real traffic instead.
    GROQ_ROUTER_MODEL: str = os.getenv("GROQ_ROUTER_MODEL", "") or "groq/compound-mini"
    # Understanding runs on every substantive message, so it is the call that
    # meets the per-day request ceiling first — and when it does, every
    # customer drops to the deterministic path at once. One explicitly
    # configured second model is tried, once, only for failures that a
    # different model could actually survive (quota, rate limit, provider
    # error). Never a retry of the same model, never a model the operator did
    # not name, and the schema contract is identical for both.
    STRUCTURED_NLU_FALLBACK_MODEL: str = os.getenv("STRUCTURED_NLU_FALLBACK_MODEL", "").strip()
    # A bounded, live semantic fallback for messages the deterministic trigger
    # matcher cannot confidently place (typos, Hinglish, and unusual wording).
    # The router selects only an existing approved template; it never writes
    # customer-facing facts. Set false to retain deterministic-only routing.
    SEMANTIC_ROUTER_ENABLED: bool = os.getenv("SEMANTIC_ROUTER_ENABLED", "true").lower() in ("true", "1", "t", "yes")
    # Structured intent/entity analysis for compound requests and wording the
    # deterministic matcher cannot place. The model returns no customer-facing
    # prose; code validates its JSON and executes only approved actions.
    STRUCTURED_NLU_ENABLED: bool = os.getenv("STRUCTURED_NLU_ENABLED", "true").lower() in ("true", "1", "t", "yes")
    try:
        STRUCTURED_NLU_ROLLOUT_PERCENT: int = max(
            0, min(100, int(os.getenv("STRUCTURED_NLU_ROLLOUT_PERCENT", "100")))
        )
    except ValueError:
        STRUCTURED_NLU_ROLLOUT_PERCENT: int = 100

    # Active conversation state survives restarts and is shared by workers via
    # the existing SQLite volume. The short TTL limits stale conversational state.
    SESSION_PERSISTENCE_ENABLED: bool = os.getenv("SESSION_PERSISTENCE_ENABLED", "true").lower() in ("true", "1", "t", "yes")
    try:
        SESSION_TTL_SECONDS: int = max(300, int(os.getenv("SESSION_TTL_SECONDS", "86400")))
    except ValueError:
        SESSION_TTL_SECONDS: int = 86400

    # Conversations older than this are purged; stated in the privacy policy.
    DATA_RETENTION_DAYS: int = int(os.getenv("DATA_RETENTION_DAYS", "90"))

    # Shown on /privacy and /terms.
    BUSINESS_LEGAL_NAME: str = os.getenv("BUSINESS_LEGAL_NAME", "Camellia Artisanal")
    PRIVACY_CONTACT_EMAIL: str = os.getenv("PRIVACY_CONTACT_EMAIL", "")
    
    # WhatsApp Cloud API Config
    # Mock mode is deliberately enabled by default so local/test runs never call Meta.
    WHATSAPP_MOCK_MODE: bool = os.getenv("WHATSAPP_MOCK_MODE", "true").lower() in ("true", "1", "t", "yes")
    WHATSAPP_ACCESS_TOKEN: str = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    WHATSAPP_PHONE_NUMBER_ID: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    WHATSAPP_API_VERSION: str = os.getenv("WHATSAPP_API_VERSION", "v19.0")

    # Instagram Config — uses its own Page token (may differ from WhatsApp token)
    INSTAGRAM_PAGE_ID: str = os.getenv("INSTAGRAM_PAGE_ID", "")
    INSTAGRAM_ACCESS_TOKEN: str = os.getenv("INSTAGRAM_ACCESS_TOKEN", "") or os.getenv("WHATSAPP_ACCESS_TOKEN", "")

    # --- Channel policy: Instagram is the only AI chatbot channel -----------
    # The client does not want customers talking to an AI on WhatsApp at all.
    # WHATSAPP_CHATBOT_ENABLED defaults false and is the one flag that would
    # restore the old auto-reply behaviour; camellia/whatsapp_adapter.py only
    # consults it as an explicit, logged escape hatch for a future policy
    # change — the code path that calls the understanding/RAG/template stack
    # for WhatsApp does not run by default, and nothing else in this file
    # turns it on implicitly.
    INSTAGRAM_CHATBOT_ENABLED: bool = os.getenv("INSTAGRAM_CHATBOT_ENABLED", "true").lower() in ("true", "1", "t", "yes")
    WHATSAPP_CHATBOT_ENABLED: bool = os.getenv("WHATSAPP_CHATBOT_ENABLED", "false").lower() in ("true", "1", "t", "yes")
    WHATSAPP_HUMAN_HANDOFF_ENABLED: bool = os.getenv("WHATSAPP_HUMAN_HANDOFF_ENABLED", "true").lower() in ("true", "1", "t", "yes")
    # The client's own WhatsApp Business number that customers are sent to,
    # digits only with country code (e.g. "9198XXXXXXXX") — NOT the Meta Cloud
    # API sender number above, which never faces the customer directly here.
    # Left blank until the client provides it; see the manual-configuration
    # report for what happens while it is unset.
    WHATSAPP_BUSINESS_NUMBER: str = os.getenv("WHATSAPP_BUSINESS_NUMBER", "")
    # How long an Instagram -> WhatsApp handoff reference stays valid/active
    # for deduplication before a repeated "human please" creates a new one.
    CROSS_CHANNEL_HANDOFF_EXPIRY_HOURS: int = int(os.getenv("CROSS_CHANNEL_HANDOFF_EXPIRY_HOURS", "72"))
    # Placeholder copy — the manual-configuration report flags this as
    # pending client sign-off; do not treat it as approved client wording.
    INSTAGRAM_HANDOFF_MESSAGE: str = os.getenv(
        "INSTAGRAM_HANDOFF_MESSAGE",
        "Our team can assist you personally with this on WhatsApp. "
        "Tap below to continue your enquiry — please send the pre-filled "
        "message so our team can identify your Instagram enquiry.",
    )

    # Human Escalation Contact
    # Where the bot sends its escalation alerts. Distinct from the number
    # customers are told to call, which stays +91 87965 51990 across the
    # templates, the menu and the order summaries — this one is internal.
    HUMAN_ESCALATION_PHONE: str = os.getenv("HUMAN_ESCALATION_PHONE", "+919902181650")
    HUMAN_ESCALATION_EMAIL: str = os.getenv("HUMAN_ESCALATION_EMAIL", "")

    # Slow-reply notice.
    # When the bot itself becomes slow to answer, the customer is sitting there
    # watching nothing happen. Rather than let them guess, we say so once per
    # conversation and hand them the manager's direct line — they can switch to
    # a human or carry on with the bot, whichever they prefer.
    SLOW_REPLY_NOTICE_ENABLED: bool = os.getenv("SLOW_REPLY_NOTICE_ENABLED", "true").lower() in ("true", "1", "t", "yes")
    # A reply slower than this (milliseconds) counts as slow. Parsed defensively:
    # a blank or malformed value must not take the whole bot down at import time.
    try:
        SLOW_REPLY_THRESHOLD_MS: float = float(os.getenv("SLOW_REPLY_THRESHOLD_MS", "").strip() or 3000)
    except ValueError:
        SLOW_REPLY_THRESHOLD_MS: float = 3000.0
    # The notice promises a limited period, so it must be able to expire —
    # otherwise the bot keeps claiming "for a week" indefinitely. ISO date
    # (YYYY-MM-DD); once past, the notice stops being sent. Empty = no expiry.
    SLOW_REPLY_NOTICE_UNTIL: str = os.getenv("SLOW_REPLY_NOTICE_UNTIL", "")
    # `or` rather than a getenv default: an empty SLOW_REPLY_NOTICE_TEXT= line in
    # .env would otherwise blank the notice instead of falling back to the copy.
    SLOW_REPLY_NOTICE_TEXT: str = os.getenv("SLOW_REPLY_NOTICE_TEXT", "").strip() or (
        "Hi thanks so much for connecting. I will slow in replying to the Instagram "
        "messages for a week, due to unavoidable circumstances. Request you to connect "
        "directly with Our Manager of Kitchens, Mr. Guru Dayal Singh (8796551990), on a "
        "call or WhatsApp . He will help you in the selection & purchase process & also "
        "and share your invoice. He will be your single point of contact for this order "
        "and will be happy to assist you with any queries."
    )

    @property
    def is_slow_reply_notice_active(self) -> bool:
        """True while the slow-reply notice is switched on and not past its end date."""
        if not self.SLOW_REPLY_NOTICE_ENABLED:
            return False
        raw_until = (self.SLOW_REPLY_NOTICE_UNTIL or "").strip()
        if not raw_until:
            return True
        try:
            from datetime import date
            return date.today() <= date.fromisoformat(raw_until)
        except ValueError:
            # A malformed date must not silently disable a notice the client
            # switched on, so fall back to "active" and complain in the logs.
            print(
                f"[config] SLOW_REPLY_NOTICE_UNTIL={raw_until!r} is not a valid "
                f"YYYY-MM-DD date; ignoring the expiry and keeping the notice active."
            )
            return True
    
    # Data Storage
    DB_PATH: str = _resolve_path("DB_PATH", BASE_DIR / "camellia.db")
    MENU_JSON_PATH: str = _resolve_path("MENU_JSON_PATH", DATA_DIR / "menu.json", must_exist=True)
    QA_TEMPLATES_PATH: str = _resolve_path("QA_TEMPLATES_PATH", DATA_DIR / "qa_templates.json", must_exist=True)
    
    # Google Sheet CSV URL for Menu Updates (Option A in PRD)
    GOOGLE_SHEET_CSV_URL: str = os.getenv("GOOGLE_SHEET_CSV_URL", "")

    # AI Engine Config (Groq — fast inference, OpenAI-compatible chat API)
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    # Instagram ads are mapped to a product deliberately by immutable Meta ad
    # ID.  Example Railway value:
    # {"12021000123456789":"savoury","12021000987654321":"sondesh"}
    # Titles are only a conservative fallback because an ad name can change.
    INSTAGRAM_AD_PRODUCT_MAP: str = os.getenv("INSTAGRAM_AD_PRODUCT_MAP", "{}")
    GROQ_TIMEOUT: float = float(os.getenv("GROQ_TIMEOUT", "20.0"))
    # Refinement only rewords an answer the bot already has, so the customer
    # must never wait long for it. Kept below SLOW_REPLY_THRESHOLD_MS on
    # purpose: a slow or overloaded provider should be abandoned in favour of
    # the template rather than delay the reply far enough to tell the customer
    # the business is running slow.
    GROQ_REFINE_TIMEOUT: float = float(os.getenv("GROQ_REFINE_TIMEOUT", "2.5"))
    GROQ_ENABLED: bool = os.getenv("GROQ_ENABLED", "true").lower() in ("true", "1", "t", "yes")
    # Menu flyer OCR. The chat model (gpt-oss-20b) has no vision, but Groq
    # serves multimodal models on the same key and endpoint, so photo parsing
    # needs no second provider account. Verified against console.groq.com/docs/vision
    # on 2026-09-02; an unserved name here answers 404 and parks the provider,
    # so change it only against that list.
    GROQ_VISION_MODEL: str = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")
    AI_FIRST_MODE: bool = os.getenv("AI_FIRST_MODE", "true").lower() in ("true", "1", "t", "yes")

    @property
    def is_ai_enabled(self) -> bool:
        if not self.GROQ_ENABLED:
            return False
        return bool(self.GROQ_API_KEY)

config = Config()

