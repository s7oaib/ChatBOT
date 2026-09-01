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
    
    # Human Escalation Contact
    HUMAN_ESCALATION_PHONE: str = os.getenv("HUMAN_ESCALATION_PHONE", "+918796551990")
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

    # Vision OCR only (menu flyer photo parsing) — unrelated to the Groq chat
    # RAG above. gpt-oss-20b has no vision support, so image parsing keeps
    # using Gemini/OpenAI vision models if a key is configured for them.
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    GEMINI_ENABLED: bool = os.getenv("GEMINI_ENABLED", "true").lower() in ("true", "1", "t", "yes")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    @property
    def is_ai_enabled(self) -> bool:
        if not self.GROQ_ENABLED:
            return False
        return bool(self.GROQ_API_KEY)

config = Config()

