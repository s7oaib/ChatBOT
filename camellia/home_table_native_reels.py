"""Exact owned-media mapping verified through Camellia's Instagram Login Graph API.

Verified 2 October 2026: @camellia.artisanal owned /me/media collection,
six pages, unique exact permalinks; Reel #1 MEDIA_SHARE accepted and its
native card confirmed by the allowed tester. No caption or image inference.
"""
from camellia.config import config


VERIFIED_ACCOUNT_ID = "17841455615744812"
VERIFIED_MEDIA = {
    "https://www.instagram.com/reel/DdJhXQMT4VZ/": "18073790945717461",
    "https://www.instagram.com/reel/Dcd5bbEzbCo/": "18191102770349496",
    "https://www.instagram.com/reel/DcLZpuQzCKE/": "18158439172491979",
    "https://www.instagram.com/reel/DcQYp72zf8D/": "17908289592288134",
}


def verified_media_id(url):
    """Never reuse this account's verified capability for another account."""
    if (config.INSTAGRAM_ACCOUNT_ID == VERIFIED_ACCOUNT_ID
            and config.INSTAGRAM_EXPECTED_USERNAME == "camellia.artisanal"
            and config.instagram_auth_mode() == "instagram_login"):
        return VERIFIED_MEDIA.get(url)
    return None


def explicitly_unavailable(response):
    """Fallback only on an explicit native rejection, never network uncertainty."""
    if response is None or response.status_code not in {400, 403}:
        return False
    try:
        error = response.json().get("error", {})
    except (ValueError, AttributeError):
        return False
    message = str(error.get("message", "")).lower()
    return error.get("code") in {10, 100, 200} and any(
        term in message for term in ("media_share", "media share", "media sharing", "attachment type")
    ) and any(term in message for term in (
        "not supported", "unsupported", "not available", "permission", "not owned", "own media",
    ))
