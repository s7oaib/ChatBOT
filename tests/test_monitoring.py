"""Monitoring alerts remain log-only unless explicitly enabled."""

from unittest.mock import patch

from camellia.config import config
from camellia.monitoring import DeliveryHealthMonitor


def test_delivery_health_alert_does_not_call_meta_when_disabled():
    monitor = DeliveryHealthMonitor(failure_threshold=1)
    with patch.object(config, "HUMAN_ESCALATION_WHATSAPP_API_ENABLED", False), \
         patch.object(config, "WHATSAPP_ACCESS_TOKEN", "token"), \
         patch.object(config, "WHATSAPP_PHONE_NUMBER_ID", "phone-id"), \
         patch("httpx.Client") as client:
        monitor.record_failure("instagram", "HTTP 503")

    client.assert_not_called()
    assert monitor.snapshot()["channels_alerting"] == ["instagram"]
