"""Small provider adapter for administrator password-reset email.

The reset token is supplied only to the provider request and is never logged
or returned to the application caller. Providers are deliberately hidden
behind this function so a future mail service can be swapped without changing
the authentication flow.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from camellia.config import config


def send_password_reset_email(recipient: str, reset_url: str, expires_minutes: int) -> bool:
    """Send a reset email through the configured transactional provider."""
    provider = (config.ADMIN_EMAIL_PROVIDER or "").strip().lower()
    if provider != "resend":
        return False
    if not (recipient and config.ADMIN_EMAIL_FROM and config.ADMIN_EMAIL_API_KEY):
        return False

    payload = {
        "from": config.ADMIN_EMAIL_FROM,
        "to": [recipient],
        "subject": "Camellia Artisanal admin password reset",
        "text": (
            "A password reset was requested for the Camellia Artisanal Admin Portal.\n\n"
            f"Open this one-time link within {expires_minutes} minutes:\n{reset_url}\n\n"
            "If you did not request this, ignore this email. Contact the portal owner if you suspect misuse."
        ),
    }
    request = Request(
        config.ADMIN_EMAIL_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.ADMIN_EMAIL_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            return 200 <= int(response.status) < 300
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return False
