import base64
import hashlib
import hmac
import struct
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import camellia.app as app_module
from camellia.admin_security import hash_secret, verify_password
from camellia.app import app
from camellia.config import config
from camellia.database import Database


PASSWORD = "a long production-style admin passphrase"
NEW_PASSWORD = "an entirely different admin passphrase"
USERNAME = "owner"
RECOVERY_EMAIL = "owner@example.test"
MFA_SECRET = "JBSWY3DPEHPK3PXP"
API_KEY = "machine-api-key-never-in-browser"


def _totp(secret: str, now: float | None = None) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    counter = int((time.time() if now is None else now) // 30)
    raw = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = raw[-1] & 0x0F
    return str((struct.unpack(">I", raw[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000).zfill(6)


@pytest.fixture
def reset_client(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "admin-reset.db"))
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(config, "ADMIN_USERNAME", USERNAME)
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", hash_secret(PASSWORD))
    monkeypatch.setattr(config, "ADMIN_RECOVERY_EMAIL", RECOVERY_EMAIL)
    monkeypatch.setattr(config, "ADMIN_EMAIL_PROVIDER", "resend")
    monkeypatch.setattr(config, "ADMIN_EMAIL_FROM", "Camellia Admin <admin@example.test>")
    monkeypatch.setattr(config, "ADMIN_EMAIL_API_KEY", "provider-key-not-real")
    monkeypatch.setattr(config, "ADMIN_PUBLIC_BASE_URL", "https://admin.example.test")
    monkeypatch.setattr(config, "ADMIN_MFA_TOTP_SECRET", MFA_SECRET)
    monkeypatch.setattr(config, "ADMIN_MFA_RECOVERY_CODE_HASHES", "")
    monkeypatch.setattr(config, "ADMIN_MFA_REQUIRED", True)
    monkeypatch.setattr(config, "ADMIN_REQUIRE_HTTPS", True)
    monkeypatch.setattr(config, "ADMIN_API_KEY", API_KEY)
    sent = []

    def fake_send(recipient, reset_url, expires_minutes):
        sent.append((recipient, reset_url, expires_minutes))
        return True

    monkeypatch.setattr(app_module.admin_mailer, "send_password_reset_email", fake_send)
    limiter_enabled = app.state.limiter.enabled
    app.state.limiter.enabled = False
    with TestClient(app, base_url="https://testserver") as client:
        yield client, database, sent
    app.state.limiter.enabled = limiter_enabled


def _request_reset(client, email=RECOVERY_EMAIL):
    headers = {}
    csrf = client.cookies.get("camellia_admin_csrf")
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return client.post("/admin/forgot-password", json={"email": email}, headers=headers)


def _token(sent):
    url = urlsplit(sent[-1][1])
    values = parse_qs(url.fragment).get("token") or parse_qs(url.query).get("token")
    return values[0]


def _login(client, password=PASSWORD, totp=None):
    if totp is None:
        first = client.post("/admin/login", json={"username": USERNAME, "password": password})
        if first.status_code != 200 or first.json().get("mfa_required"):
            return client.post(
                "/admin/login",
                json={"username": USERNAME, "password": password, "totp": _totp(MFA_SECRET)},
            )
        return first
    return client.post(
        "/admin/login",
        json={"username": USERNAME, "password": password, "totp": totp},
    )


def test_forgot_password_ui_and_generic_email_response(reset_client):
    client, _database, sent = reset_client
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Forgot Password?" in page.text
    assert "ADMIN_API_KEY" not in page.text
    assert client.get("/admin/forgot-password").status_code == 200
    assert "Reset Admin Password" in client.get("/admin/forgot-password").text
    assert client.get("/admin/reset-password").status_code == 200

    valid = _request_reset(client)
    unknown = _request_reset(client, "not-the-owner@example.test")
    assert valid.status_code == unknown.status_code == 200
    assert valid.json() == unknown.json() == {
        "message": "If an admin account exists for that email, a password reset link has been sent."
    }
    assert RECOVERY_EMAIL not in unknown.text
    assert len(sent) == 1


def test_reset_token_is_random_hashed_and_previous_token_is_invalidated(reset_client):
    client, database, sent = reset_client
    assert _request_reset(client).status_code == 200
    first = _token(sent)
    assert _request_reset(client).status_code == 200
    second = _token(sent)
    assert first != second
    assert first in sent[0][1]
    rows = database.get_connection().execute(
        "SELECT token_hash, status FROM admin_password_reset_tokens ORDER BY id"
    ).fetchall()
    assert first not in {row["token_hash"] for row in rows}
    assert all(row["token_hash"] != second for row in rows)
    assert rows[0]["status"] == "CANCELLED"
    assert rows[1]["status"] == "ACTIVE"


def test_wrong_mfa_does_not_change_password_and_valid_mfa_is_single_use(reset_client):
    client, database, sent = reset_client
    assert _request_reset(client).status_code == 200
    reset_token = _token(sent)
    before = database.get_admin_credential()["password_hash"]
    wrong = client.post(
        "/admin/reset-password",
        json={"token": reset_token, "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD, "totp": "000000"},
    )
    assert wrong.status_code == 401
    assert database.get_admin_credential()["password_hash"] == before
    assert database.get_active_admin_password_reset_token(hashlib.sha256(reset_token.encode()).hexdigest(), time.time())

    completed = client.post(
        "/admin/reset-password",
        json={
            "token": reset_token,
            "password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
            "totp": _totp(MFA_SECRET),
        },
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "password_reset"
    assert verify_password(NEW_PASSWORD, database.get_admin_credential()["password_hash"])
    assert not verify_password(PASSWORD, database.get_admin_credential()["password_hash"])
    assert client.post(
        "/admin/reset-password",
        json={"token": reset_token, "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD, "totp": _totp(MFA_SECRET)},
    ).status_code == 400

    fresh = TestClient(app, base_url="https://testserver")
    assert _login(fresh, PASSWORD).status_code == 401
    assert _login(fresh, NEW_PASSWORD).status_code == 200


def test_existing_sessions_are_revoked_after_reset_and_api_key_is_unchanged(reset_client):
    client, _database, sent = reset_client
    assert _login(client).status_code == 200
    assert client.get("/admin/session").status_code == 200
    assert _request_reset(client).status_code == 200
    resetter = TestClient(app, base_url="https://testserver")
    response = resetter.post(
        "/admin/reset-password",
        json={
            "token": _token(sent),
            "password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
            "totp": _totp(MFA_SECRET),
        },
    )
    assert response.status_code == 200
    assert client.get("/admin/session").status_code == 403
    assert API_KEY == config.ADMIN_API_KEY
    assert resetter.get("/admin/menu/active", headers={"X-Admin-Key": API_KEY}).status_code == 200


def test_expired_and_cancelled_tokens_are_rejected(reset_client):
    client, database, sent = reset_client
    assert _request_reset(client).status_code == 200
    expired = _token(sent)
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE admin_password_reset_tokens SET expires_at=0 WHERE token_hash=?",
            (hashlib.sha256(expired.encode()).hexdigest(),),
        )
    assert client.post(
        "/admin/reset-password",
        json={"token": expired, "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD, "totp": _totp(MFA_SECRET)},
    ).status_code == 400

    assert _request_reset(client).status_code == 200
    cancelled = _token(sent)
    assert client.post("/admin/reset-password/cancel", json={"token": cancelled}).status_code == 200
    assert client.post(
        "/admin/reset-password",
        json={"token": cancelled, "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD, "totp": _totp(MFA_SECRET)},
    ).status_code == 400


def test_forgot_and_reset_attempts_are_rate_limited(reset_client):
    client, _database, _sent = reset_client
    for index in range(3):
        response = _request_reset(client, f"unknown-{index}@example.test")
        assert response.status_code == 200
    limited = _request_reset(client, "unknown-final@example.test")
    assert limited.status_code == 429
    assert limited.headers.get("retry-after")


def test_reset_does_not_log_token_or_password(reset_client, caplog):
    client, _database, sent = reset_client
    assert _request_reset(client).status_code == 200
    reset_token = _token(sent)
    caplog.clear()
    assert client.post(
        "/admin/reset-password",
        json={"token": reset_token, "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD, "totp": "000000"},
    ).status_code == 401
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert reset_token not in logs
    assert NEW_PASSWORD not in logs
    assert "password_hash" not in logs
