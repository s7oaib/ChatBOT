"""Operational Instagram identity survives CAM creation without log leakage."""

import logging
import re
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia.ad_gate import describe_entry_point
from camellia.config import config
from camellia.database import db
from camellia.instagram_identity import resolve_instagram_identity
from camellia.session_memory import session_memory


def _sender_id():
    return f"178414{uuid.uuid4().int % 10**10:010d}"


def test_webhook_normalization_preserves_sender_identity():
    sender_id = _sender_id()
    entry = describe_entry_point({"sender": {"id": sender_id, "username": "from_event"}, "message": {"text": "help"}})
    assert entry.sender_id == sender_id
    assert entry.customer_username == "from_event"


def _create_cam(identity):
    user_id = identity["customer_external_id"]
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app_module, "send_instagram_reply") as send_reply, patch.object(
        app_module.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ) as escalated, patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"), \
         patch.object(config, "GROQ_ENABLED", False), patch.object(config, "STRUCTURED_NLU_ENABLED", False), \
         patch.object(config, "SEMANTIC_ROUTER_ENABLED", False):
        app_module._process_instagram_event(
            user_id, "Do you offer wedding gift wrapping?", "ad-identity",
            customer_identity=identity,
        )
    rows = [row for row in db.get_handoffs(limit=200) if row["source_user_id"] == user_id]
    assert len(rows) == 1 and escalated.call_count == 1
    return user_id, rows[0]["handoff_ref"]


def test_username_available_is_stored_and_authenticated_admin_can_display_it():
    sender_id = _sender_id()
    user_id, handoff_ref = _create_cam({
        "customer_external_id": sender_id,
        "customer_username": "bengalifoodie",
    })
    row = db.get_handoff_by_ref(handoff_ref)
    assert row["source_channel"] == "instagram"
    assert row["customer_username"] == "bengalifoodie"
    assert row["customer_external_id"] == sender_id
    assert row["source_user_id"] == user_id

    with patch.object(app_module, "require_admin", lambda *args, **kwargs: None):
        response = TestClient(app_module.app).get("/admin/handoffs?limit=200")
    assert response.status_code == 200
    item = next(entry for entry in response.json()["handoffs"] if entry["handoff_ref"] == handoff_ref)
    assert item["customer_display_name"] == "@bengalifoodie"
    assert item["customer_username"] == "bengalifoodie"
    assert item["customer_external_id"] == sender_id


def test_username_unavailable_uses_neutral_label_and_retains_separate_sender_id():
    sender_id = _sender_id()
    _user_id, handoff_ref = _create_cam({
        "customer_external_id": sender_id,
        "customer_username": None,
    })
    with patch.object(app_module, "require_admin", lambda *args, **kwargs: None):
        response = TestClient(app_module.app).get("/admin/handoffs?limit=200")
    item = next(entry for entry in response.json()["handoffs"] if entry["handoff_ref"] == handoff_ref)
    assert item["customer_external_id"] == sender_id
    assert item["customer_display_name"] == "Instagram customer"
    assert item["identity_status"] == "UNRESOLVED"


def test_resolved_display_name_is_used_only_when_username_is_missing():
    sender_id = _sender_id()
    db.save_instagram_customer_identity(sender_id, display_name="A Customer")
    _, ref = _create_cam({"customer_external_id": sender_id, "customer_username": None})
    with patch.object(app_module, "require_admin", lambda *args, **kwargs: None):
        response = TestClient(app_module.app).get("/admin/handoffs?limit=200")
    item = next(entry for entry in response.json()["handoffs"] if entry["handoff_ref"] == ref)
    assert item["customer_display_name"] == "A Customer"
    assert item["identity_status"] == "UNRESOLVED"


def test_identity_lookup_returns_username_without_exposing_provider_payload(monkeypatch):
    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "17841400000062434", "username": "lookup_name"}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _url, params=None):
            assert params["fields"] == "username"
            return FakeResponse()

    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr("camellia.instagram_identity.httpx.Client", FakeClient)
    sender_id = _sender_id()
    identity = resolve_instagram_identity(sender_id)
    assert identity.external_id == sender_id
    assert identity.username == "lookup_name"


def test_identity_and_username_are_not_emitted_into_ordinary_logs(caplog):
    sender_id = _sender_id()
    username = "private_customer_name"
    with caplog.at_level(logging.INFO):
        _create_cam({"customer_external_id": sender_id, "customer_username": username})
    assert sender_id not in caplog.text
    assert username not in caplog.text


def test_cam_identity_preserves_deduplication():
    sender_id = _sender_id()
    user_id = f"identity_dedup_{uuid.uuid4().hex[:10]}"
    session_memory.clear_session(user_id, "instagram")
    identity = {"customer_external_id": sender_id, "customer_username": "dedup_name"}
    with patch.object(app_module, "send_instagram_reply"), patch.object(
        app_module.escalator, "handle_escalation", return_value={"escalation_id": 1},
    ) as escalated, patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"), \
         patch.object(config, "GROQ_ENABLED", False), patch.object(config, "STRUCTURED_NLU_ENABLED", False), \
         patch.object(config, "SEMANTIC_ROUTER_ENABLED", False):
        app_module._process_instagram_event(user_id, "unknown first ask", "ad-identity", customer_identity=identity)
        app_module._process_instagram_event(user_id, "unknown second ask", "ad-identity", customer_identity=identity)
    rows = [row for row in db.get_handoffs(limit=200) if row["source_user_id"] == user_id]
    assert len(rows) == 1
    assert escalated.call_count == 1


def test_inbound_meta_lookup_persists_username_and_exact_igsid():
    sender_id = _sender_id()
    with patch.object(app_module, "resolve_instagram_identity") as lookup:
        lookup.return_value.username = "meta_customer"
        identity = app_module._cached_instagram_identity(sender_id, lookup=True)
    assert identity["customer_external_id"] == sender_id
    assert identity["customer_username"] == "meta_customer"
    assert db.get_instagram_customer_identity(sender_id)["identity_status"] == "RESOLVED"
    _, ref = _create_cam(identity)
    assert db.get_handoff_by_ref(ref)["customer_username"] == "meta_customer"


def test_failed_lookup_preserves_cached_username_and_does_not_repeat_immediately():
    sender_id = _sender_id()
    db.save_instagram_customer_identity(sender_id, username="known_name", lookup_at="2020-01-01T00:00:00")
    with patch.object(app_module, "resolve_instagram_identity") as lookup:
        lookup.return_value.username = None
        identity = app_module._cached_instagram_identity(sender_id, lookup=True)
        app_module._cached_instagram_identity(sender_id, lookup=True)
    assert lookup.call_count == 1
    assert identity["customer_username"] == "known_name"


def test_username_change_updates_same_igsid_and_active_handoff():
    sender_id = _sender_id()
    _, ref = _create_cam({"customer_external_id": sender_id, "customer_username": "old_name"})
    with patch.object(app_module, "send_instagram_reply") as reply:
        app_module._process_instagram_event(sender_id, "Another question", "ad-identity", instagram_username="new_name")
    assert not reply.called  # Staff still own the active conversation.
    assert db.get_handoff_by_ref(ref)["customer_username"] == "new_name"
    assert db.get_instagram_customer_identity(sender_id)["customer_username"] == "new_name"
    assert len([row for row in db.get_handoffs(limit=200) if row["source_user_id"] == sender_id]) == 1


def test_handoff_ui_search_and_profile_link_are_username_guarded():
    html = TestClient(app_module.app).get("/admin").text
    assert "handoffMatchesSearch(item, query)" in html
    assert "replace(/^@+/, '')" in html
    assert "item.customer_external_id, item.handoff_ref" in html
    assert "Open Instagram Profile" in html
    assert "test(username) ?" in html
    assert "Instagram ID: ${escapeHtml(item.customer_external_id" in html


def test_lookup_outage_does_not_block_handoff_creation():
    sender_id = _sender_id()
    with patch.object(app_module, "resolve_instagram_identity", side_effect=RuntimeError("Meta unavailable")):
        identity = app_module._cached_instagram_identity(sender_id, lookup=True)
    assert identity["identity_status"] == "UNRESOLVED"
    _, ref = _create_cam(identity)
    assert db.get_handoff_by_ref(ref)["customer_external_id"] == sender_id


def test_new_handoff_retries_missing_username_once_then_replies():
    sender_id = _sender_id()
    session_memory.clear_session(sender_id, "instagram")
    with patch.object(app_module, "resolve_instagram_identity") as lookup, \
         patch.object(app_module, "send_instagram_reply") as reply, \
         patch.object(app_module.escalator, "handle_escalation", return_value={"escalation_id": 1}), \
         patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"), \
         patch.object(config, "GROQ_ENABLED", False), \
         patch.object(config, "STRUCTURED_NLU_ENABLED", False), \
         patch.object(config, "SEMANTIC_ROUTER_ENABLED", False):
        lookup.return_value.username = None
        app_module._process_instagram_event(sender_id, "Do you offer wedding gift wrapping?", "ad-identity", resolve_identity=True)
    assert lookup.call_count == 2
    assert reply.called
    rows = [row for row in db.get_handoffs(limit=200) if row["source_user_id"] == sender_id]
    assert len(rows) == 1
    assert db.get_instagram_customer_identity(sender_id)["identity_status"] == "UNRESOLVED"
