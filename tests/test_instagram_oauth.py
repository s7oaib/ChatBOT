import asyncio
import time
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia.config import config
from camellia.instagram_oauth import (
    STATE_COOKIE,
    InstagramOAuthError,
    InstagramOAuthIdentity,
    exchange_and_verify,
    issue_state,
)


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.post_calls = []
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, data):
        self.post_calls.append((url, data))
        return next(self.responses)

    async def get(self, url, params):
        self.get_calls.append((url, params))
        return next(self.responses)


def _set_oauth_config(monkeypatch):
    monkeypatch.setattr(config, "INSTAGRAM_APP_ID", "28666204229670328")
    monkeypatch.setattr(config, "INSTAGRAM_APP_SECRET", "instagram-secret-for-test")
    monkeypatch.setattr(config, "INSTAGRAM_REDIRECT_URI", "https://example.test/auth/instagram/callback")
    monkeypatch.setattr(config, "INSTAGRAM_EXPECTED_USERNAME", "camellia.artisanal")


def test_token_exchange_sends_exact_redirect_uri_and_verifies_identity(monkeypatch):
    fake = _FakeClient([
        _Response(200, {"access_token": "token-never-rendered", "user_id": "123"}),
        _Response(200, {"id": "987654", "username": "camellia.artisanal"}),
    ])
    monkeypatch.setattr("camellia.instagram_oauth.httpx.AsyncClient", lambda **kwargs: fake)

    identity = asyncio.run(exchange_and_verify(
        app_id="28666204229670328",
        app_secret="instagram-secret-for-test",
        redirect_uri="https://example.test/auth/instagram/callback",
        code="fresh-code",
        expected_username="camellia.artisanal",
    ))

    assert identity.account_id == "987654"
    assert identity.username == "camellia.artisanal"
    assert fake.post_calls[0][1]["redirect_uri"] == "https://example.test/auth/instagram/callback"


def test_exchange_failure_is_sanitized(monkeypatch):
    fake = _FakeClient([
        _Response(400, {"error_message": "provider body contains secret-code-and-token"}),
    ])
    monkeypatch.setattr("camellia.instagram_oauth.httpx.AsyncClient", lambda **kwargs: fake)

    try:
        asyncio.run(exchange_and_verify(
            app_id="28666204229670328",
            app_secret="instagram-secret-for-test",
            redirect_uri="https://example.test/auth/instagram/callback",
            code="fresh-code",
            expected_username="camellia.artisanal",
        ))
    except InstagramOAuthError as exc:
        message = str(exc)
    else:
        raise AssertionError("exchange should fail")
    assert message == "Instagram authorization could not be completed."
    assert "secret-code-and-token" not in message


def test_callback_rejects_missing_or_invalid_state(monkeypatch):
    _set_oauth_config(monkeypatch)
    with TestClient(app_module.app) as client:
        missing = client.get("/auth/instagram/callback?code=fresh-code")
        assert missing.status_code == 400
        assert "fresh-code" not in missing.text

        invalid = client.get("/auth/instagram/callback?state=bad&code=fresh-code")
        assert invalid.status_code == 400
        assert "bad" not in invalid.text


def test_callback_rejects_missing_or_malformed_code(monkeypatch):
    _set_oauth_config(monkeypatch)
    state = issue_state(config.INSTAGRAM_APP_SECRET, now=int(time.time()))
    with TestClient(app_module.app) as client:
        client.cookies.set(STATE_COOKIE, state)
        missing = client.get(f"/auth/instagram/callback?state={state}")
        assert missing.status_code == 400
        client.cookies.set(STATE_COOKIE, state)
        malformed = client.get(f"/auth/instagram/callback?state={state}&code=%20")
        assert malformed.status_code == 400


def test_callback_accepts_camellia_identity_without_rendering_token(monkeypatch):
    _set_oauth_config(monkeypatch)
    token = "token-never-rendered"
    monkeypatch.setattr(
        app_module,
        "exchange_and_verify",
        AsyncMock(return_value=InstagramOAuthIdentity("987654", "camellia.artisanal", token)),
    )
    state = issue_state(config.INSTAGRAM_APP_SECRET, now=int(time.time()))
    with TestClient(app_module.app) as client:
        client.cookies.set(STATE_COOKIE, state)
        response = client.get(f"/auth/instagram/callback?state={state}&code=fresh-code")
    assert response.status_code == 200
    assert "@camellia.artisanal" in response.text
    assert "987654" in response.text
    assert token not in response.text
    assert "fresh-code" not in response.text
    assert "instagram-secret-for-test" not in response.text


def test_callback_rejects_wrong_username_and_does_not_adopt(monkeypatch):
    _set_oauth_config(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "exchange_and_verify",
        AsyncMock(side_effect=InstagramOAuthError("The authorized Instagram account is not the Camellia account.")),
    )
    state = issue_state(config.INSTAGRAM_APP_SECRET, now=int(time.time()))
    with TestClient(app_module.app) as client:
        client.cookies.set(STATE_COOKIE, state)
        response = client.get(f"/auth/instagram/callback?state={state}&code=fresh-code")
    assert response.status_code == 502
    assert "not the Camellia account" in response.text
    assert "fresh-code" not in response.text


def test_start_route_requires_admin_and_uses_minimum_scopes(monkeypatch):
    _set_oauth_config(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_API_KEY", "machine-key")
    with TestClient(app_module.app) as client:
        denied = client.get("/auth/instagram/start")
        assert denied.status_code == 403
        response = client.get("/auth/instagram/start", headers={"x-admin-key": "machine-key"}, follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert "client_id=28666204229670328" in location
    assert "instagram_business_basic%2Cinstagram_business_manage_messages" in location
    assert "force_reauth=true" in location
