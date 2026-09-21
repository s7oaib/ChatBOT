"""Instagram Login authorization-code flow.

This module deliberately keeps the access token in process memory only. The
application verifies the account, but Railway remains the system of record for
the production token and account binding.
"""

import base64
import hashlib
import hmac
import html
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx


AUTHORIZATION_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
PROFILE_URL = "https://graph.instagram.com/me"
STATE_COOKIE = "camellia_instagram_oauth_state"
STATE_TTL_SECONDS = 600
OAUTH_SCOPES = (
    "instagram_business_basic",
    "instagram_business_manage_messages",
)


class InstagramOAuthError(Exception):
    """Safe, user-facing OAuth failure without provider response details."""


@dataclass(frozen=True)
class InstagramOAuthIdentity:
    account_id: str
    username: str
    access_token: str


def _signing_key(app_secret: str) -> bytes:
    secret = (app_secret or "").strip()
    if not secret:
        raise InstagramOAuthError("Instagram Login is not configured.")
    return secret.encode("utf-8")


def issue_state(app_secret: str, now: Optional[int] = None) -> str:
    """Create a short-lived, tamper-evident state value."""
    issued_at = int(time.time() if now is None else now)
    nonce = secrets.token_urlsafe(32)
    payload = f"{issued_at}.{nonce}"
    signature = hmac.new(
        _signing_key(app_secret), payload.encode("utf-8"), hashlib.sha256
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{payload}.{encoded_signature}"


def validate_state(
    state: Optional[str],
    cookie_state: Optional[str],
    app_secret: str,
    now: Optional[int] = None,
) -> bool:
    """Require the callback state to match the browser-bound cookie."""
    if not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
        return False
    parts = state.split(".")
    if len(parts) != 3:
        return False
    issued_at, nonce, encoded_signature = parts
    if not nonce or not encoded_signature:
        return False
    try:
        issued = int(issued_at)
    except ValueError:
        return False
    current = int(time.time() if now is None else now)
    if issued > current + 30 or current - issued > STATE_TTL_SECONDS:
        return False
    payload = f"{issued_at}.{nonce}"
    expected = hmac.new(
        _signing_key(app_secret), payload.encode("utf-8"), hashlib.sha256
    ).digest()
    supplied = encoded_signature + "=" * (-len(encoded_signature) % 4)
    try:
        decoded = base64.urlsafe_b64decode(supplied.encode("ascii"))
    except (ValueError, UnicodeError):
        return False
    return hmac.compare_digest(expected, decoded)


def build_authorization_url(
    app_id: str, redirect_uri: str, state: str
) -> str:
    if not app_id.strip() or not redirect_uri.strip():
        raise InstagramOAuthError("Instagram Login is not configured.")
    return AUTHORIZATION_URL + "?" + urlencode(
        {
            "force_reauth": "true",
            "client_id": app_id.strip(),
            "redirect_uri": redirect_uri.strip(),
            "response_type": "code",
            "scope": ",".join(OAUTH_SCOPES),
            "state": state,
        }
    )


def _json_object(response: httpx.Response) -> Dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


async def exchange_code(
    client: Any,
    *,
    app_id: str,
    app_secret: str,
    redirect_uri: str,
    code: str,
) -> str:
    """Exchange a fresh authorization code without exposing provider details."""
    try:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": app_id,
                "client_secret": app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
    except httpx.HTTPError as exc:
        raise InstagramOAuthError("Instagram authorization could not be completed.") from exc
    payload = _json_object(response)
    if response.status_code != 200:
        raise InstagramOAuthError("Instagram authorization could not be completed.")
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise InstagramOAuthError("Instagram authorization could not be completed.")
    return token


async def exchange_and_verify(
    *,
    app_id: str,
    app_secret: str,
    redirect_uri: str,
    code: str,
    expected_username: str,
) -> InstagramOAuthIdentity:
    if not code or len(code) > 4096 or any(char.isspace() for char in code):
        raise InstagramOAuthError("Instagram authorization code is missing or invalid.")
    if not app_id.strip() or not app_secret.strip() or not redirect_uri.strip():
        raise InstagramOAuthError("Instagram Login is not configured.")

    async with httpx.AsyncClient(timeout=10.0) as client:
        token = await exchange_code(
            client,
            app_id=app_id.strip(),
            app_secret=app_secret.strip(),
            redirect_uri=redirect_uri.strip(),
            code=code,
        )
        try:
            response = await client.get(
                PROFILE_URL,
                params={
                    "fields": "id,username",
                    "access_token": token,
                },
            )
        except httpx.HTTPError as exc:
            raise InstagramOAuthError("Instagram account verification could not be completed.") from exc
    profile = _json_object(response)
    if response.status_code != 200:
        raise InstagramOAuthError("Instagram account verification could not be completed.")
    account_id = str(profile.get("id") or "").strip()
    username = str(profile.get("username") or "").strip()
    if not account_id or not username:
        raise InstagramOAuthError("Instagram account verification could not be completed.")
    if username.casefold() != expected_username.strip().casefold():
        raise InstagramOAuthError("The authorized Instagram account is not the Camellia account.")
    return InstagramOAuthIdentity(account_id, username, token)


def success_page(identity: InstagramOAuthIdentity) -> str:
    """Render only the verified non-secret identity."""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>Instagram connection verified</title></head><body>"
        "<h1>Instagram connection verified</h1>"
        f"<p>Account: @{html.escape(identity.username)}</p>"
        f"<p>Instagram account ID: {html.escape(identity.account_id)}</p>"
        "<p>The token was not displayed or logged. Complete the Railway credential update before enabling the connection.</p>"
        "</body></html>"
    )


def error_page(message: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>Instagram connection unavailable</title></head><body>"
        "<h1>Instagram connection unavailable</h1>"
        f"<p>{html.escape(message)}</p></body></html>"
    )
