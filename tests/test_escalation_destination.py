"""Manual-only escalation and health truthfulness."""

import json

from camellia import app as app_module
from camellia.escalator import escalator


def test_human_alert_is_always_record_only_and_never_cloud_api():
    result = escalator.send_human_alert(1, "u1", "instagram", "help", "unmatched", {})

    assert result == {
        "status": "skipped",
        "retryable": False,
        "reason": "manual_handoff_only",
    }


def test_customer_handoff_copy_remains_manual_and_does_not_expose_internal_alert_data():
    from camellia.responder import responder

    handover, _intent = responder.build_handover_response("i need a refund please")

    assert "assist you personally on WhatsApp" in handover
    assert "9902181650" not in handover


def test_health_reports_removed_automation_and_available_manual_handoff():
    payload = json.loads(app_module.health_check().body)

    assert payload["status"] in {"healthy", "degraded"}
    assert payload["whatsapp"] == {
        "automation": "removed",
        "manual_handoff": "available",
        "business_number_configured": bool(app_module.config.WHATSAPP_BUSINESS_NUMBER),
        "cloud_api": "unavailable",
    }
    assert payload["human_escalation_notifications"] == {
        "status": "SKIPPED",
        "reason": "WhatsApp automation removed; staff alerts are manual-only.",
    }
