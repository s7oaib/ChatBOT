import inspect
import time

import pytest
from fastapi.testclient import TestClient

import camellia.app as app_module
from camellia.app import app
from camellia.admin_security import digest, hash_password
from camellia.config import config
from camellia.database import Database


PASSWORD = "a long production-style admin passphrase"
USERNAME = "owner"
@pytest.fixture
def security_client(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "admin-security.db"))
    database.init_db()
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(config, "ADMIN_USERNAME", USERNAME)
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", hash_password(PASSWORD))
    monkeypatch.setattr(config, "ADMIN_REQUIRE_HTTPS", True)
    monkeypatch.setattr(config, "ADMIN_API_KEY", "machine-api-key-never-in-browser")
    limiter_enabled = app.state.limiter.enabled
    app.state.limiter.enabled = False
    with TestClient(app, base_url="https://testserver") as client:
        yield client, database
    app.state.limiter.enabled = limiter_enabled


def _login(client, username=USERNAME, password=PASSWORD):
    return client.post(
        "/admin/login",
        json={"username": username, "password": password},
    )


def test_valid_password_login_creates_secure_server_session(security_client):
    client, _database = security_client
    response = _login(client)
    assert response.status_code == 200
    assert response.json()["status"] == "authenticated"
    assert "machine-api-key-never-in-browser" not in response.text
    cookie_header = response.headers["set-cookie"]
    assert "camellia_admin_session=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "Secure" in cookie_header
    assert "SameSite=strict" in cookie_header
    assert client.get("/admin/session").status_code == 200
    assert client.get("/admin/menu/active").status_code == 200


def test_authenticated_session_survives_dashboard_refresh_and_protected_navigation(security_client):
    """Browser refresh/navigation must reuse the persisted server session."""
    client, _database = security_client
    assert _login(client).status_code == 200

    for path in (
        "/admin",
        "/admin/dashboard",
        "/admin/session",
        "/admin/menu/active",
        "/admin/analytics",
        "/admin/conversations?limit=1",
    ):
        assert client.get(path).status_code == 200, path

    # A separate browser without the session cookie remains unauthenticated.
    unauthenticated = TestClient(app, base_url="https://testserver")
    assert unauthenticated.get("/admin/session").status_code == 403


def test_wrong_and_unknown_credentials_are_generic(security_client):
    client, _database = security_client
    wrong_password = _login(client, password="wrong password")
    unknown_user = _login(client, username="unknown-user", password="wrong password")
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json() == {"detail": "Invalid credentials."}


def test_repeated_failures_trigger_persistent_cooldown_and_correct_password_cannot_bypass(security_client):
    client, database = security_client
    responses = [_login(client, password="wrong password") for _ in range(5)]
    assert [response.status_code for response in responses] == [401, 401, 401, 401, 429]
    assert responses[-1].headers["retry-after"]
    assert _login(client).status_code == 429

    restarted = Database(database.db_path)
    scopes = [f"account:{digest(USERNAME)}", f"ip:{digest('testclient')}", "endpoint:admin-login"]
    assert restarted.admin_login_rate_status(scopes, time.time()) > 0


def test_ip_and_account_scopes_are_independent_and_cooldown_expires(tmp_path):
    database = Database(str(tmp_path / "throttle.db"))
    database.init_db()
    now = 1000.0
    account = "account:one"
    ip = "ip:one"
    endpoint = "endpoint:admin-login"
    database.record_admin_login_failure([(account, 5, 60), (ip, 5, 60), (endpoint, 50, 300)], now)
    assert database.admin_login_rate_status([account], now + 1) == 0
    for _ in range(4):
        database.record_admin_login_failure([(account, 5, 60), (ip, 5, 60), (endpoint, 50, 300)], now + 2)
    assert database.admin_login_rate_status([account], now + 3) > 0
    assert database.admin_login_rate_status(["account:other", ip], now + 3) > 0
    assert database.admin_login_rate_status([account], now + 301) == 0


def test_password_only_login_has_no_second_challenge(security_client):
    client, _database = security_client
    assert _login(client).status_code == 200
    page = client.get("/admin").text
    assert page.count('class="credential-input"') == 2
    assert page.count('autocomplete="username"') == 1
    assert page.count('autocomplete="current-password"') == 1
    assert page.count('type="password"') == 1
    admin_auth = client.get("/health").json()["admin_auth"]
    assert admin_auth == {
        "password_configured": True,
        "machine_api_configured": True,
        "https_required": True,
    }


def test_expired_session_and_logout_revoke_access(security_client):
    client, database = security_client
    assert _login(client).status_code == 200
    session_hash = database.get_connection().execute(
        "SELECT session_hash FROM admin_sessions ORDER BY created_at DESC LIMIT 1"
    ).fetchone()[0]
    with database.get_connection() as conn:
        conn.execute("UPDATE admin_sessions SET expires_at=0 WHERE session_hash=?", (session_hash,))
        conn.commit()
    assert client.get("/admin/session").status_code == 403

    client = TestClient(app, base_url="https://testserver")
    assert _login(client).status_code == 200
    csrf = client.cookies.get("camellia_admin_csrf")
    assert client.post("/admin/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/admin/session").status_code == 403


def test_authenticated_browser_write_requires_csrf_and_accepts_valid_csrf(security_client):
    client, _database = security_client
    assert _login(client).status_code == 200
    csrf = client.cookies.get("camellia_admin_csrf")
    response = client.post("/admin/logout")
    assert response.status_code == 403
    response = client.post("/admin/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200


def test_legacy_pin_and_browser_api_key_bypass_are_blocked(security_client):
    client, _database = security_client
    assert client.post("/admin/verify-pin", json={"pin": "123456"}).status_code == 410
    assert client.get("/admin/menu/active", headers={"X-Admin-Pin": "123456"}).status_code == 403
    assert client.get("/admin/menu/active", headers={"X-Admin-Key": config.ADMIN_API_KEY}).status_code == 200
    page = client.get("/admin").text
    assert config.ADMIN_API_KEY not in page
    assert "X-Admin-Key': currentAdminKey" not in page


def test_security_headers_and_admin_endpoint_inventory(security_client):
    client, _database = security_client
    response = client.get("/admin")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"]
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers["strict-transport-security"].startswith("max-age=")

    public = {
        "/admin", "/admin/dashboard", "/admin/login", "/admin/verify-pin",
        "/admin/content-routing", "/admin/instagram-content-routing", "/admin/legacy",
    }
    missing = []
    for route in app.routes:
        if not route.path.startswith("/admin") or route.path in public:
            continue
        endpoint = getattr(route, "endpoint", None)
        source = inspect.getsource(endpoint) if endpoint else ""
        if endpoint and "require_admin" not in source and "approve_assisted_resolution" not in source:
            missing.append(route.path)
    assert missing == []
