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
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

class Config:
    # Meta & Webhook Config
    META_VERIFY_TOKEN: str = os.getenv("META_VERIFY_TOKEN", "camellia_verify_secret_2026")
    META_APP_SECRET: str = os.getenv("META_APP_SECRET", "")

    # Public Domain / ngrok URL for serving images & webhooks
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "")

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
    DB_PATH: str = os.getenv("DB_PATH", str(BASE_DIR / "camellia.db"))
    MENU_JSON_PATH: str = os.getenv("MENU_JSON_PATH", str(BASE_DIR / "menu.json"))
    QA_TEMPLATES_PATH: str = os.getenv("QA_TEMPLATES_PATH", str(BASE_DIR / "qa_templates.json"))
    
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
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    GEMINI_TIMEOUT: float = float(os.getenv("GEMINI_TIMEOUT", "20.0"))
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

