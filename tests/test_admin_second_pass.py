"""Focused regressions for the second-pass Admin workspace cleanup."""

from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia.config import config
from camellia.handoff import get_or_create_handoff, mark_referred
from camellia.session_memory import session_memory


client = TestClient(app_module.app)
config.ADMIN_API_KEY = "test_admin_second_pass_key"
ADMIN_HEADERS = {"x-admin-key": config.ADMIN_API_KEY}


def test_old_content_routing_entry_lands_in_posts_reels_routing():
    response = client.get("/admin/content-routing", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/admin?tab=content-routing"


def test_handoff_fallback_opens_waiting_and_resumed_cam_records(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_API_KEY", "test_admin_second_pass_key")
    user_id = "second-pass-instagram-customer"
    session_memory.clear_session(user_id, "instagram")
    handoff = get_or_create_handoff(
        user_id=user_id,
        channel="instagram",
        reason="Needs human support",
        user_message="Please have someone help me",
        facts=session_memory.get_facts(user_id, "instagram"),
    )
    mark_referred(handoff["handoff_ref"])

    waiting = client.get("/admin/handoffs?include_resumed=true", headers=ADMIN_HEADERS)
    assert waiting.status_code == 200
    item = next(row for row in waiting.json()["handoffs"] if row["handoff_ref"] == handoff["handoff_ref"])
    assert item["customer_display_name"] == "Instagram customer"
    assert item["status"] == "WAITING_FOR_HUMAN"

    resumed = client.post(f"/admin/handoffs/{handoff['handoff_ref']}/resume", headers=ADMIN_HEADERS)
    assert resumed.status_code == 200
    history = client.get("/admin/handoffs?include_resumed=true", headers=ADMIN_HEADERS)
    item = next(row for row in history.json()["handoffs"] if row["handoff_ref"] == handoff["handoff_ref"])
    assert item["status"] == "RESUMED"
    assert history.json()["unread_count"] == 0
