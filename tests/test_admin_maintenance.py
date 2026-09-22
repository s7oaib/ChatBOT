"""The Admin maintenance switch closes only the Admin surface."""

from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia.config import config


def test_admin_maintenance_off_preserves_admin_behavior(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_MAINTENANCE_MODE", False)
    client = TestClient(app_module.app)
    response = client.get("/admin")
    assert response.status_code == 200
    assert "Kitchen &amp; Menu Admin Portal" in response.text or "Kitchen & Menu Admin Portal" in response.text


def test_admin_maintenance_on_blocks_shell_apis_and_auth_state_without_details(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_MAINTENANCE_MODE", True)
    client = TestClient(app_module.app)
    paths = (
        ("GET", "/admin"),
        ("GET", "/admin/dashboard"),
        ("POST", "/admin/login"),
        ("GET", "/admin/session"),
        ("GET", "/admin/menu-catalogue"),
        ("DELETE", "/admin/menu-catalogue/weekly_vegetarian/permanent"),
        ("GET", "/admin/content-routing"),
        ("GET", "/admin/legacy"),
    )
    for method, path in paths:
        for headers in ({}, {"X-Admin-Key": "machine-key"}):
            response = client.request(method, path, headers=headers)
            assert response.status_code == 503, (method, path, headers, response.text)
            assert "Camellia Admin Portal" in response.text
            assert "Currently Under Maintenance" in response.text
            assert "temporarily unavailable while updates and improvements are being completed" in response.text
            assert "Please check back later." in response.text
            assert "Customer-facing chatbot services continue to operate normally." in response.text
            assert "Traceback" not in response.text
            assert "camellia.db" not in response.text
            assert "machine-key" not in response.text


def test_admin_maintenance_does_not_block_health_or_instagram_webhook_verification(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_MAINTENANCE_MODE", True)
    client = TestClient(app_module.app)

    health = client.get("/health")
    assert health.status_code in {200, 503}
    assert "Camellia Admin Portal" not in health.text

    webhook = client.get(
        "/webhook/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": config.META_VERIFY_TOKEN,
            "hub.challenge": "maintenance-still-works",
        },
    )
    assert webhook.status_code == 200
    assert webhook.text == "maintenance-still-works"


def test_admin_logout_remains_available_for_session_cleanup(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_MAINTENANCE_MODE", True)
    client = TestClient(app_module.app)
    # It still returns the same sanitized maintenance state, while the
    # middleware can revoke a presented session and clear its cookies.
    response = client.post("/admin/logout")
    assert response.status_code == 503
    assert "Camellia Admin Portal" in response.text
    assert "Currently Under Maintenance" in response.text
