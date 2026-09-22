"""Best-effort Instagram customer identity for authenticated operations."""

from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import httpx

from camellia.config import config
from camellia.monitoring import get_logger

log = get_logger("camellia.instagram_identity")


@dataclass(frozen=True)
class InstagramIdentity:
    external_id: str
    username: Optional[str] = None
    display_name: Optional[str] = None
    profile_pic: Optional[str] = None

    def as_record(self) -> dict:
        return {
            "customer_external_id": self.external_id,
            "customer_username": self.username,
            "customer_display_name": self.display_name,
            "customer_profile_pic": self.profile_pic,
        }


def resolve_instagram_identity(sender_id: str) -> InstagramIdentity:
    """Return the sender ID immediately and enrich it with username if allowed."""
    external_id = str(sender_id or "").strip()
    if not external_id:
        return InstagramIdentity("")

    token = str(config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    username = None
    display_name = None
    profile_pic = None
    if token:
        auth_mode = "unavailable"
        http_status = None
        category = "other"
        try:
            auth_mode = config.instagram_auth_mode()
            host = "graph.instagram.com" if auth_mode == "instagram_login" else "graph.facebook.com"
            url = f"https://{host}/{config.INSTAGRAM_API_VERSION}/{quote(external_id, safe='')}"
            with httpx.Client(timeout=5.0) as client:
                response = client.get(
                    url,
                    params={"fields": "name,username,profile_pic", "access_token": token},
                )
            http_status = response.status_code
            try:
                payload = response.json()
            except Exception:
                payload = None
            if response.status_code != 200:
                category = _failure_category(http_status, payload)
                _log_lookup_failure(http_status, category, auth_mode)
                payload = None
            elif not isinstance(payload, dict):
                category = "malformed_response"
                _log_lookup_failure(http_status, category, auth_mode)
            candidate = payload.get("username") if isinstance(payload, dict) else None
            if isinstance(candidate, str):
                candidate = candidate.strip().lstrip("@")
                username = candidate or None
            if isinstance(payload, dict):
                display_name = str(payload.get("name") or "").strip() or None
                picture = str(payload.get("profile_pic") or "").strip()
                profile_pic = picture if picture.startswith("https://") and "access_token" not in picture.lower() else None
        except Exception:
            # Never include the exception: HTTP client errors can contain URL
            # query parameters, including the access token.
            _log_lookup_failure(http_status, "provider_unavailable", auth_mode)

    return InstagramIdentity(external_id=external_id, username=username, display_name=display_name, profile_pic=profile_pic)


def _failure_category(http_status, payload) -> str:
    if http_status == 429:
        return "rate_limited"
    if isinstance(http_status, int) and http_status >= 500:
        return "provider_unavailable"
    if http_status == 404:
        return "not_found"
    if http_status == 401:
        return "invalid_or_expired_token"
    if http_status == 403:
        return "permission_denied"
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return "malformed_response" if payload is not None else "other"
    code = error.get("code")
    message = str(error.get("message") or "").casefold()
    error_type = str(error.get("type") or "").casefold()
    if code == 190 or "expired" in message or "invalid oauth" in message or "invalid access token" in message:
        return "invalid_or_expired_token"
    if code in {10, 200} or "permission" in message or "permission" in error_type:
        return "permission_denied"
    if "unsupported get request" in message or "endpoint" in message:
        return "endpoint_mismatch"
    if "field" in message and (code == 100 or "unsupported" in message or "cannot be queried" in message):
        return "unsupported_field"
    if "not found" in message or "does not exist" in message:
        return "not_found"
    return "other"


def _log_lookup_failure(http_status, category: str, auth_mode: str) -> None:
    log.info(
        "INSTAGRAM_IDENTITY_LOOKUP status=failed http_status=%s category=%s auth_mode=%s",
        http_status if isinstance(http_status, int) else "unavailable",
        category,
        auth_mode,
    )
