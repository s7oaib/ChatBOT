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
    ADMIN_API_KEY: str = os.getenv("ADMIN_API_KEY", "")
    ADMIN_PIN: str = os.getenv("ADMIN_PIN", "")  # Must be explicitly configured via ADMIN_PIN env; unconfigured blocks admin access

    # The browser chat tester writes real rows and fires real escalation alerts,
    # so it can be explicitly disabled in production with ENABLE_WEB_TESTER=false.
    ENABLE_WEB_TESTER: bool = os.getenv("ENABLE_WEB_TESTER", "true").lower() in ("true", "1", "t", "yes")

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

    # AI Engine Config (Google Gemini Flash)
    AI_PROVIDER: str = os.getenv("AI_PROVIDER", "gemini").lower()  # "gemini" (primary), "openai", or "auto"

    # OpenAI API Config (optional fallback)
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "o3-mini")
    OPENAI_REASONING_EFFORT: str = os.getenv("OPENAI_REASONING_EFFORT", "low")  # "low", "medium", "high"
    OPENAI_TIMEOUT: float = float(os.getenv("OPENAI_TIMEOUT", "15.0"))

    # Gemini Cloud API Config (Google Gemini Flash)
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
    # Verified against the live models endpoint on 2026-08-26. Avoid the
    # `-latest` aliases here: they were returning 503 under load, which the
    # caller waits out before failing, and a pinned model fails fast instead.
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    GEMINI_TIMEOUT: float = float(os.getenv("GEMINI_TIMEOUT", "20.0"))
    # Refinement only rewords an answer the bot already has, so the customer
    # must never wait long for it. Kept below SLOW_REPLY_THRESHOLD_MS on
    # purpose: a slow or overloaded provider should be abandoned in favour of
    # the template rather than delay the reply far enough to tell the customer
    # the business is running slow, which is what a 7-second 503 was doing.
    GEMINI_REFINE_TIMEOUT: float = float(os.getenv("GEMINI_REFINE_TIMEOUT", "2.5"))
    GEMINI_ENABLED: bool = os.getenv("GEMINI_ENABLED", "true").lower() in ("true", "1", "t", "yes")
    AI_FIRST_MODE: bool = os.getenv("AI_FIRST_MODE", "true").lower() in ("true", "1", "t", "yes")

    @property
    def is_ai_enabled(self) -> bool:
        if not self.GEMINI_ENABLED:
            return False
        if self.AI_PROVIDER == "openai":
            return bool(self.OPENAI_API_KEY)
        elif self.AI_PROVIDER == "gemini":
            return bool(self.GEMINI_API_KEY)
        return bool(self.GEMINI_API_KEY or self.OPENAI_API_KEY)

config = Config()

