"""The WhatsApp surface is a manual continuation, never a chatbot."""

import json
import urllib.parse
from unittest.mock import patch

from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia.config import config
from camellia.escalator import escalator
from camellia.handoff import get_or_create_handoff, whatsapp_link
from camellia.session_memory import session_memory


def test_whatsapp_webhook_surface_is_removed_and_instagram_route_remains():
    client = TestClient(app_module.app)

    assert client.get("/webhook/whatsapp").status_code == 404
    assert client.post("/webhook/whatsapp", json={"object": "whatsapp_business_account"}).status_code == 404
    assert any(route.path == "/webhook/instagram" for route in app_module.app.routes)


def test_web_tester_reports_manual_only_without_calling_responder(monkeypatch):
    client = TestClient(app_module.app)
    monkeypatch.setattr(config, "ENABLE_WEB_TESTER", True)

    with patch.object(app_module.responder, "match_and_respond") as respond:
        response = client.post(
            "/api/test-chat",
            json={"message": "what is the price", "user_id": "wa-cleanup", "channel": "whatsapp"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "manual_only",
        "is_ad_qualified": False,
        "response": None,
        "was_matched": False,
        "template_id": None,
        "image_url": None,
        "whatsapp_automation": "removed",
        "manual_handoff": "available",
    }
    respond.assert_not_called()


def test_health_states_removed_automation_and_manual_handoff():
    payload = json.loads(app_module.health_check().body)

    assert payload["whatsapp"]["automation"] == "removed"
    assert payload["whatsapp"]["manual_handoff"] == "available"
    assert payload["whatsapp"]["cloud_api"] == "unavailable"
    assert payload["human_escalation_notifications"]["status"] == "SKIPPED"


def test_manual_handoff_keeps_cam_dedup_waiting_state_and_link():
    user_id = "whatsapp-cleanup-handoff"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["order_state"] = "CONFIRMED"
    with patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        first = get_or_create_handoff(
            user_id=user_id,
            channel="instagram",
            reason="Explicit human request",
            user_message="I need a person",
            facts=facts,
        )
        second = get_or_create_handoff(
            user_id=user_id,
            channel="instagram",
            reason="Repeated human request",
            user_message="Please connect me",
            facts=facts,
        )
        link = whatsapp_link(first["handoff_ref"], order_state=facts["order_state"])

    assert second["handoff_ref"] == first["handoff_ref"]
    assert link and link.startswith("https://wa.me/919800000000?text=")
    assert first["handoff_ref"] in urllib.parse.unquote(link)


def test_escalation_record_is_skipped_without_cloud_send_dependency():
    result = escalator.send_human_alert(1, "u", "instagram", "help", "human", {})

    assert result["status"] == "skipped"
    assert result["reason"] == "manual_handoff_only"
