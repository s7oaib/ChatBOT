from unittest.mock import patch
import json
import logging
import sqlite3

from camellia.config import config
from camellia.database import Database
from fastapi.testclient import TestClient
from camellia.app import app
from camellia import app as app_module
from camellia.monitoring import delivery_monitor


def test_instagram_outbound_audit_round_trip_and_health(tmp_path):
    database = Database(str(tmp_path / "audit.db"))
    audit_id = database.create_instagram_outbound_audit(
        inbound_message_id=42,
        recipient_id="sha256:masked",
        response_type="text",
        parent_send_id="parent-1",
        chunk_number=1,
        total_chunks=2,
        character_count=934,
    )
    database.complete_instagram_outbound_audit(
        audit_id,
        http_status=200,
        success=True,
        meta_message_id="mid.123",
        meta_trace_id="trace.123",
        duration_ms=18.5,
    )
    database.finalize_instagram_outbound_audit("parent-1", overall_success=True)

    rows = database.get_instagram_outbound_audit(inbound_message_id=42)
    assert rows[0]["recipient_id"] == "sha256:masked"
    assert rows[0]["parent_send_id"] == "parent-1"
    assert rows[0]["chunk_number"] == 1
    assert rows[0]["total_chunks"] == 2
    assert rows[0]["character_count"] == 934
    assert rows[0]["http_status"] == 200
    assert rows[0]["success"] == 1
    assert rows[0]["overall_success"] == 1
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


def test_instagram_text_chunk_boundaries_and_content():
    split = app_module._split_instagram_text

    assert split("x" * 500) == ["x" * 500]
    assert split("x" * 949) == ["x" * 949]

    nine_fifty_one = "x" * 951
    chunks = split(nine_fifty_one)
    assert len(chunks) == 2
    assert "".join(chunks) == nine_fifty_one
    assert all(len(chunk) <= 1000 for chunk in chunks)

    paragraph = ("A" * 700) + "\n\n" + ("B" * 500)
    paragraph_chunks = split(paragraph)
    assert paragraph_chunks[0].endswith("\n\n")
    assert "".join(paragraph_chunks) == paragraph

    newline = ("A" * 500) + ". " + ("B" * 390) + "\n" + ("C" * 500)
    newline_chunks = split(newline)
    assert newline_chunks[0].endswith("\n")
    assert "".join(newline_chunks) == newline

    sentence = ("A" * 500) + ". " + ("B" * 500)
    sentence_chunks = split(sentence)
    assert sentence_chunks[0].endswith(".")
    assert "".join(sentence_chunks) == sentence

    words = "word " * 250
    word_chunks = split(words)
    assert all(not chunk[-1:].isalpha() or len(chunk) == len(words) for chunk in word_chunks)
    assert "".join(word_chunks) == words


def test_instagram_sender_chunks_text_at_meta_limit():

    class Response:
        status_code = 200
        headers = {"x-fb-trace-id": "trace.ok"}

        def __init__(self, message_id):
            self.message_id = message_id

        def json(self):
            return {"message_id": self.message_id}

        def raise_for_status(self):
            return None

    class Client:
        bodies = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            body = kwargs["json"]["message"]["text"]
            self.bodies.append(body)
            return Response(f"mid.{len(self.bodies)}")

    client = Client()
    created = []
    completed = []
    finalized = []
    text = ("A" * 700) + "\n\n" + ("B" * 578)

    with patch.object(config, "INSTAGRAM_ACCESS_TOKEN", "token-value"), \
         patch.object(app_module.httpx, "Client", lambda *args, **kwargs: client), \
         patch.object(app_module.db, "create_instagram_outbound_audit", side_effect=lambda **kwargs: created.append(kwargs) or len(created)), \
         patch.object(app_module.db, "complete_instagram_outbound_audit", side_effect=lambda *args, **kwargs: completed.append((args, kwargs))), \
         patch.object(app_module.db, "finalize_instagram_outbound_audit", side_effect=lambda *args, **kwargs: finalized.append((args, kwargs))):
        result = app_module.send_instagram_reply("customer-123", text, inbound_message_id=42)

    assert result["message_id"] == "mid.2"
    assert len(client.bodies) == 2
    assert all(len(chunk) <= 1000 for chunk in client.bodies)
    assert "".join(client.bodies) == text
    assert len(created) == len(completed) == 2
    assert created[0]["parent_send_id"] == created[1]["parent_send_id"]
    assert [row["chunk_number"] for row in created] == [1, 2]
    assert [row["total_chunks"] for row in created] == [2, 2]
    assert [row["character_count"] for row in created] == [len(client.bodies[0]), len(client.bodies[1])]
    assert finalized[-1][1] == {"overall_success": True}


def test_instagram_sender_stops_after_failed_chunk_and_marks_overall_failure():
    class Response:
        headers = {"x-fb-trace-id": "trace.fail"}

        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    class Client:
        bodies = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            self.bodies.append(kwargs["json"]["message"]["text"])
            if len(self.bodies) == 2:
                return Response(400, {"error": {"code": 100, "error_subcode": 9, "message": "failed"}})
            return Response(200, {"message_id": "mid.1"})

    client = Client()
    created = []
    finalized = []
    text = "A" * 950 + "B" * 950 + "C" * 100
    with patch.object(config, "INSTAGRAM_ACCESS_TOKEN", "token-value"), \
         patch.object(app_module.httpx, "Client", lambda *args, **kwargs: client), \
         patch.object(app_module.db, "create_instagram_outbound_audit", side_effect=lambda **kwargs: created.append(kwargs) or len(created)), \
         patch.object(app_module.db, "complete_instagram_outbound_audit"), \
         patch.object(app_module.db, "finalize_instagram_outbound_audit", side_effect=lambda *args, **kwargs: finalized.append((args, kwargs))):
        result = app_module.send_instagram_reply("customer-123", text, inbound_message_id=42)

    assert result["status"] == "failed"
    assert len(client.bodies) == 2
    assert [row["chunk_number"] for row in created] == [1, 2]
    assert finalized[-1][1] == {"overall_success": False, "overall_failure_chunk": 2}


def test_audit_insert_failure_does_not_block_customer_send(caplog):
    class Response:
        status_code = 200
        headers = {"x-fb-trace-id": "trace.send"}

        def json(self):
            return {"message_id": "mid.send"}

        def raise_for_status(self):
            return None

    class Client:
        calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            self.calls += 1
            return Response()

    client = Client()
    with caplog.at_level(logging.WARNING), \
         patch.object(config, "INSTAGRAM_ACCESS_TOKEN", "token-value"), \
         patch.object(app_module.httpx, "Client", lambda *args, **kwargs: client), \
         patch.object(app_module.db, "create_instagram_outbound_audit", side_effect=sqlite3.OperationalError("no such table")), \
         patch.object(app_module.db, "finalize_instagram_outbound_audit", side_effect=sqlite3.OperationalError("no such table")):
        result = app_module.send_instagram_reply("customer-123", "hello", inbound_message_id=42)

    assert result["message_id"] == "mid.send"
    assert client.calls == 1
    assert delivery_monitor.snapshot()["consecutive_failures"].get("instagram", 0) == 0
    assert "Could not create Instagram outbound audit row" in caplog.text


def test_audit_final_update_failure_does_not_turn_successful_send_into_failure(caplog):
    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {"message_id": "mid.complete"}

        def raise_for_status(self):
            return None

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return Response()

    with caplog.at_level(logging.WARNING), \
         patch.object(config, "INSTAGRAM_ACCESS_TOKEN", "token-value"), \
         patch.object(app_module.httpx, "Client", lambda *args, **kwargs: Client()), \
         patch.object(app_module.db, "complete_instagram_outbound_audit", side_effect=sqlite3.OperationalError("audit update down")), \
         patch.object(app_module.db, "finalize_instagram_outbound_audit", side_effect=sqlite3.OperationalError("audit finalize down")):
        result = app_module.send_instagram_reply("customer-123", "hello", inbound_message_id=42)

    assert result["message_id"] == "mid.complete"
    assert delivery_monitor.snapshot()["consecutive_failures"].get("instagram", 0) == 0
    assert "Could not complete Instagram outbound audit row" in caplog.text
    assert "Could not finalize Instagram outbound audit" in caplog.text


def test_audit_schema_self_heals_on_a_fresh_replaced_database(tmp_path):
    path = tmp_path / "fresh.db"
    database = Database(str(path))
    database.db_path = str(tmp_path / "replaced.db")

    assert database.instagram_outbound_health() == {
        "last_attempted": None,
        "last_success": None,
        "last_failure": None,
        "failures_last_24h": 0,
    }
    audit_id = database.create_instagram_outbound_audit(
        inbound_message_id=7,
        recipient_id="sha256:fresh",
        response_type="text",
        parent_send_id="fresh-parent",
        chunk_number=1,
        total_chunks=1,
        character_count=5,
    )
    database.complete_instagram_outbound_audit(audit_id, http_status=200, success=True)
    database.finalize_instagram_outbound_audit("fresh-parent", overall_success=True)
    rows = database.get_instagram_outbound_audit(inbound_message_id=7)
    assert rows[0]["overall_success"] == 1


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
