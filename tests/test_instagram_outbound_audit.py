from unittest.mock import patch
import json

from camellia.config import config
from camellia.database import Database
from fastapi.testclient import TestClient
from camellia.app import app


def test_instagram_outbound_audit_round_trip_and_health(tmp_path):
    database = Database(str(tmp_path / "audit.db"))
    audit_id = database.create_instagram_outbound_audit(
        inbound_message_id=42,
        recipient_id="sha256:masked",
        response_type="text",
    )
    database.complete_instagram_outbound_audit(
        audit_id,
        http_status=200,
        success=True,
        meta_message_id="mid.123",
        meta_trace_id="trace.123",
        duration_ms=18.5,
    )

    rows = database.get_instagram_outbound_audit(inbound_message_id=42)
    assert rows[0]["recipient_id"] == "sha256:masked"
    assert rows[0]["http_status"] == 200
    assert rows[0]["success"] == 1
    assert rows[0]["meta_message_id"] == "mid.123"
    assert rows[0]["meta_trace_id"] == "trace.123"
    assert database.instagram_outbound_health()["failures_last_24h"] == 0


def test_instagram_sender_records_meta_error_without_credentials_in_audit():
    from camellia import app as app_module

    class Response:
        status_code = 400
        headers = {"x-fb-trace-id": "trace.error"}

        def json(self):
            return {
                "error": {
                    "code": 100,
                    "error_subcode": 33,
                    "message": "Invalid recipient",
                }
            }

        def raise_for_status(self):
            raise RuntimeError("HTTP 400")

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return Response()

    created = []
    completed = []

    with patch.object(config, "INSTAGRAM_ACCESS_TOKEN", "token-value"), \
         patch.object(app_module.httpx, "Client", lambda *args, **kwargs: Client()), \
         patch.object(app_module.db, "create_instagram_outbound_audit", side_effect=lambda **kwargs: created.append(kwargs) or 7), \
         patch.object(app_module.db, "complete_instagram_outbound_audit", side_effect=lambda *args, **kwargs: completed.append((args, kwargs))):
        result = app_module.send_instagram_reply(
            "customer-123",
            "hello",
            inbound_message_id=42,
        )

    assert result["status"] == "failed"
    assert created[0]["inbound_message_id"] == 42
    assert created[0]["recipient_id"].startswith("sha256:")
    assert "customer-123" not in created[0]["recipient_id"]
    assert completed[0][1]["http_status"] == 400
    assert completed[0][1]["success"] is False
    assert completed[0][1]["meta_error_code"] == 100
    assert completed[0][1]["meta_error_subcode"] == 33
    assert completed[0][1]["meta_trace_id"] == "trace.error"


def test_admin_outbound_audit_is_authenticated_and_read_only():
    client = TestClient(app)
    assert client.get("/admin/instagram-outbound-audit").status_code == 403


def test_health_exposes_instagram_outbound_summary_without_degrading():
    from camellia.app import health_check

    response = health_check()
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert set(payload["instagram_outbound"]) == {
        "last_attempted", "last_success", "last_failure", "failures_last_24h",
    }
