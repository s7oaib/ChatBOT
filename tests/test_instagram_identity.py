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
    match = re.search(r"CAM-\d{5,6}", send_reply.call_args.args[1])
    assert match and escalated.call_count == 1
    return user_id, match.group(0)


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
    assert "customer_external_id" not in item
    assert sender_id not in str(item)


def test_username_unavailable_degrades_to_safe_anonymous_customer_label():
    sender_id = _sender_id()
    _user_id, handoff_ref = _create_cam({
        "customer_external_id": sender_id,
        "customer_username": None,
    })
    with patch.object(app_module, "require_admin", lambda *args, **kwargs: None):
        response = TestClient(app_module.app).get("/admin/handoffs?limit=200")
    item = next(entry for entry in response.json()["handoffs"] if entry["handoff_ref"] == handoff_ref)
    assert "customer_external_id" not in item
    assert item["customer_display_name"] == "Instagram customer"
    assert sender_id not in str(item)


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
