"""Monitoring records Instagram delivery failures without external alerts."""
from camellia.monitoring import DeliveryHealthMonitor


def test_delivery_health_alert_is_log_only():
    monitor = DeliveryHealthMonitor(failure_threshold=1)
    monitor.record_failure("instagram", "HTTP 503")

    assert monitor.snapshot()["channels_alerting"] == ["instagram"]
