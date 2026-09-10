"""
Where escalation alerts are sent, and proving which number is actually live.

The alert destination is internal — distinct from the number customers are
told to call, which stays on the kitchen's public line throughout the
templates. Changing one must not change the other.
"""

import json
from unittest.mock import MagicMock, patch

from camellia import app as app_module
from camellia.config import config
from camellia.escalator import escalator


def test_alerts_go_to_the_configured_number():
    with patch.object(config, "HUMAN_ESCALATION_PHONE", "+919902181650"), \
         patch.object(config, "WHATSAPP_ACCESS_TOKEN", "t"), \
         patch.object(config, "WHATSAPP_PHONE_NUMBER_ID", "1"):
        client = MagicMock()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"ok": True}
        client.__enter__.return_value.post.return_value = resp
        with patch("camellia.escalator.httpx.Client", return_value=client):
            escalator.send_human_alert(1, "u1", "instagram", "help", "unmatched", {})

    payload = client.__enter__.return_value.post.call_args.kwargs["json"]
    assert payload["to"] == "919902181650"


def test_changing_the_setting_takes_effect_without_a_restart():
    # escalator is a module-level singleton. Capturing the number at import
    # would keep alerting the old one for the life of the process, which looks
    # exactly like the change never having been made.
    with patch.object(config, "HUMAN_ESCALATION_PHONE", "+911111111111"):
        assert escalator.human_phone == "+911111111111"
    with patch.object(config, "HUMAN_ESCALATION_PHONE", "+912222222222"):
        assert escalator.human_phone == "+912222222222"


def test_customer_handoff_copy_never_exposes_the_internal_alert_setting():
    from camellia.responder import responder

    handover, _intent = responder.build_handover_response("i need a refund please")
    with patch.object(config, "HUMAN_ESCALATION_PHONE", "+919902181650"):
        still_handover, _ = responder.build_handover_response("i need a refund please")

    assert "assist you personally on WhatsApp" in handover
    assert "87965 51990" not in handover
    assert still_handover == handover
    assert "9902181650" not in still_handover


class TestHealthShowsTheDestination:
    def test_only_the_last_four_digits_are_published(self):
        with patch.object(config, "HUMAN_ESCALATION_PHONE", "+919902181650"):
            payload = json.loads(app_module.health_check().body)

        dest = payload["escalation_alerts_to"]
        assert dest["configured"] is True
        assert dest["ends_with"] == "1650"
        assert "9902181650" not in json.dumps(payload)

    def test_it_reports_whether_the_environment_is_overriding_the_default(self):
        with patch.dict(app_module.os.environ, {"HUMAN_ESCALATION_PHONE": "+910000000000"}):
            payload = json.loads(app_module.health_check().body)
        assert payload["escalation_alerts_to"]["from_environment"] is True

    def test_an_unset_number_reads_as_not_configured(self):
        with patch.object(config, "HUMAN_ESCALATION_PHONE", ""):
            payload = json.loads(app_module.health_check().body)
        assert payload["escalation_alerts_to"]["configured"] is False
