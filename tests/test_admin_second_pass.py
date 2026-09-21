"""Focused regressions for the second-pass Admin workspace cleanup."""

import json
import uuid

from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia.config import config
from camellia.database import db
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


def test_handoff_username_is_reused_for_waiting_and_resumed_identity(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_API_KEY", "test_admin_second_pass_key")
    user_id = "178900001234567"
    session_memory.clear_session(user_id, "instagram")
    handoff = get_or_create_handoff(
        user_id=user_id,
        channel="instagram",
        reason="Needs human support",
        user_message="Please have someone help me",
        facts=session_memory.get_facts(user_id, "instagram"),
        instagram_username="camellia_customer",
    )
    mark_referred(handoff["handoff_ref"])

    waiting = client.get("/admin/handoffs?include_resumed=true", headers=ADMIN_HEADERS)
    assert waiting.status_code == 200
    item = next(row for row in waiting.json()["handoffs"] if row["handoff_ref"] == handoff["handoff_ref"])
    assert item["customer_display_name"] == "camellia_customer"
    assert item["status"] == "WAITING_FOR_HUMAN"
    assert "source_user_id" not in item
    assert user_id not in json.dumps(item)

    resumed = client.post(f"/admin/handoffs/{handoff['handoff_ref']}/resume", headers=ADMIN_HEADERS)
    assert resumed.status_code == 200
    history = client.get("/admin/handoffs?include_resumed=true", headers=ADMIN_HEADERS)
    item = next(row for row in history.json()["handoffs"] if row["handoff_ref"] == handoff["handoff_ref"])
    assert item["customer_display_name"] == "camellia_customer"
    assert item["status"] == "RESUMED"


def test_handoff_raw_meta_id_never_becomes_customer_identity(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_API_KEY", "test_admin_second_pass_key")
    user_id = "178900009876543"
    session_memory.clear_session(user_id, "instagram")
    handoff = get_or_create_handoff(
        user_id=user_id,
        channel="instagram",
        reason="Needs human support",
        user_message="Please have someone help me",
        facts=session_memory.get_facts(user_id, "instagram"),
        instagram_username=user_id,
    )
    response = client.get("/admin/handoffs?include_resumed=true", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    item = next(row for row in response.json()["handoffs"] if row["handoff_ref"] == handoff["handoff_ref"])
    assert item["customer_display_name"] == "Instagram customer"
    assert user_id not in json.dumps(item)


def test_instagram_webhook_propagates_upstream_username_to_handoff_worker(monkeypatch):
    queued = []

    monkeypatch.setattr(
        app_module.ad_gate,
        "inspect_and_gate_message",
        lambda _event: (True, "178900001112223", "I need a person", "ad-1"),
    )
    monkeypatch.setattr(
        app_module,
        "describe_entry_point",
        lambda _event: app_module.EntryPoint(source="text"),
    )

    class BackgroundTasks:
        def add_task(self, function, *args, **kwargs):
            queued.append((function, args, kwargs))

    result = app_module._dispatch_instagram_payload(
        {
            "entry": [{
                "messaging": [{
                    "sender": {"id": "178900001112223", "username": "kaido514"},
                    "message": {"text": "I need a person"},
                }],
            }],
        },
        BackgroundTasks(),
    )

    assert result["status"] == "processed"
    assert len(queued) == 1
    assert queued[0][0] is app_module._process_instagram_event
    assert queued[0][2]["instagram_username"] == "kaido514"


def test_new_handoff_creation_persists_profile_username_when_event_omits_it(monkeypatch):
    user_id = f"17890000{uuid.uuid4().int % 100000000:08d}"
    session_memory.clear_session(user_id, "instagram")
    monkeypatch.setattr(app_module, "fetch_instagram_username", lambda _sender_id: "kaido514")
    monkeypatch.setattr(
        app_module.responder,
        "match_and_respond",
        lambda *_args, **_kwargs: ("handoff", False, None, None),
    )
    monkeypatch.setattr(app_module, "send_instagram_reply", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        app_module.escalator,
        "handle_escalation",
        lambda **_kwargs: {"escalation_id": 1},
    )

    app_module._process_instagram_event(user_id, "I need a person", "ad-1")

    handoff = db.get_active_handoff("instagram", user_id)
    assert handoff is not None
    assert handoff["instagram_username"] == "kaido514"
    assert "Instagram username: @kaido514" in handoff["summary"]


def test_cam_like_admin_row_backfills_verified_username_without_source_id(monkeypatch):
    user_id = f"17890000{uuid.uuid4().int % 100000000:08d}"
    session_memory.clear_session(user_id, "instagram")
    handoff = get_or_create_handoff(
        user_id=user_id,
        channel="instagram",
        reason="Needs human support",
        user_message="Please have someone help me",
        facts=session_memory.get_facts(user_id, "instagram"),
    )
    mark_referred(handoff["handoff_ref"])
    monkeypatch.setattr(app_module, "fetch_instagram_username", lambda sender: "kaido514" if sender == user_id else None)

    response = client.get("/admin/handoffs?include_resumed=true", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    item = next(row for row in response.json()["handoffs"] if row["handoff_ref"] == handoff["handoff_ref"])
    assert item["customer_display_name"] == "kaido514"
    assert user_id not in json.dumps(item)
    assert db.get_handoff_by_ref(handoff["handoff_ref"])["instagram_username"] == "kaido514"
