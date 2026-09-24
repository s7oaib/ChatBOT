"""A webhook acknowledgement must leave replayable work on disk."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia.ad_gate import EntryPoint
from camellia.database import Database
from camellia.config import config


class DeferredTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


def test_inbound_replay_after_restart_and_duplicate_delivery(tmp_path, monkeypatch):
    db_path = tmp_path / "delivery.db"
    database = Database(str(db_path))
    monkeypatch.setattr(app_module, "db", database)
    deferred = DeferredTasks()
    tasks = app_module._DurableInstagramTasks(deferred)
    event = {"sender": {"id": "customer-1"}, "message": {"mid": "mid-1", "text": "price?"}}
    tasks.set_event("account-1", event)

    def _process_instagram_event(sender_id, text, ad_id, entry_point):
        assert (sender_id, text, ad_id, entry_point.source) == (
            "customer-1", "price?", None, "typed",
        )
        app_module._send_or_queue_instagram_reply(sender_id, "Approved answer")

    monkeypatch.setattr(app_module, "_process_instagram_event", _process_instagram_event)
    tasks.add_task(_process_instagram_event, "customer-1", "price?", None, EntryPoint(source="typed"))
    assert len(deferred.tasks) == 1
    assert database.instagram_job_counts()["inbound"] == {"PENDING": 1}

    # The process can disappear after the HTTP 200. A fresh Database instance
    # recovers the committed job; Meta's replay does not create another one.
    restarted = Database(str(db_path))
    monkeypatch.setattr(app_module, "db", restarted)
    sent = []
    monkeypatch.setattr(app_module, "send_instagram_reply", lambda *a, **k: sent.append((a, k)) or {"message_id": "sent-1"})
    tasks.set_event("account-1", event)
    tasks.add_task(_process_instagram_event, "customer-1", "price?", None, EntryPoint(source="typed"))
    app_module._process_instagram_inbound_jobs()
    assert len(sent) == 1
    assert restarted.instagram_job_counts()["inbound"] == {"DONE": 1}
    assert restarted.instagram_job_counts()["outbound"] == {"SENT": 1}


def test_signed_webhook_commits_before_ack_even_if_worker_does_not_run(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "delivery.db"))
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", "")
    monkeypatch.setattr(config, "META_APP_SECRET", "test-secret")
    monkeypatch.setattr(app_module, "_process_instagram_inbound_jobs", lambda **_kwargs: None)
    payload = {"object": "instagram", "entry": [{"id": "account-1", "messaging": [{
        "sender": {"id": "customer-1"},
        "message": {"mid": "mid-1", "text": "price?"},
    }]}]}
    body = json.dumps(payload).encode("utf-8")
    signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    client = TestClient(app_module.app)
    first = client.post("/webhook/instagram", content=body, headers={"X-Hub-Signature-256": signature})
    second = client.post("/webhook/instagram", content=body, headers={"X-Hub-Signature-256": signature})
    assert first.status_code == second.status_code == 200
    assert database.instagram_job_counts()["inbound"] == {"PENDING": 1}


def test_no_id_and_no_timestamp_does_not_suppress_repeated_customer_text():
    event = {"sender": {"id": "customer-1"}, "message": {"text": "help"}}
    assert app_module._instagram_event_key("account-1", event) != app_module._instagram_event_key("account-1", event)


def test_failed_outbound_send_retries_without_rebuilding_answer(tmp_path, monkeypatch):
    db_path = tmp_path / "delivery.db"
    database = Database(str(db_path))
    monkeypatch.setattr(app_module, "db", database)
    job_id, _ = database.enqueue_instagram_inbound_job(
        "event-1", "customer-1", "_process_instagram_event", {"args": [], "kwargs": {}},
    )
    database.enqueue_instagram_outbound_job(job_id, 1, "customer-1", {"text": "Approved answer", "image_url": None})
    attempts = []

    def send(*args, **kwargs):
        attempts.append((args, kwargs))
        return {"status": "failed", "error": "temporary"} if len(attempts) == 1 else {"message_id": "sent-1"}

    monkeypatch.setattr(app_module, "send_instagram_reply", send)
    app_module._process_instagram_outbound_jobs()
    assert database.instagram_job_counts()["outbound"] == {"RETRY": 1}
    database = Database(str(db_path))
    monkeypatch.setattr(app_module, "db", database)
    with database.get_connection() as conn:
        conn.execute("UPDATE instagram_outbound_jobs SET next_attempt_at = datetime('now', '-1 second')")
    app_module._process_instagram_outbound_jobs()
    assert database.instagram_job_counts()["outbound"] == {"SENT": 1}
    assert [call[0][1] for call in attempts] == ["Approved answer", "Approved answer"]


def test_permanent_recipient_error_is_not_retried(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "delivery.db"))
    monkeypatch.setattr(app_module, "db", database)
    inbound, _ = database.enqueue_instagram_inbound_job("event-1", "customer-1", "task", {})
    database.enqueue_instagram_outbound_job(inbound, 1, "customer-1", {"text": "Approved answer"})
    monkeypatch.setattr(
        app_module, "send_instagram_reply",
        lambda *a, **k: {"status": "failed", "delivery_status": "PERMANENT_RECIPIENT_FAILURE"},
    )
    app_module._process_instagram_outbound_jobs()
    assert database.instagram_job_counts()["outbound"] == {"FAILED": 1}


def test_inbound_claims_are_ordered_per_sender(tmp_path):
    database = Database(str(tmp_path / "delivery.db"))
    first, _ = database.enqueue_instagram_inbound_job("event-1", "customer-1", "task", {})
    second, _ = database.enqueue_instagram_inbound_job("event-2", "customer-1", "task", {})
    assert [row["id"] for row in database.claim_instagram_inbound_jobs(limit=10)] == [first]
    assert database.claim_instagram_inbound_jobs(limit=10) == []
    database.complete_instagram_inbound_job(first, success=True)
    assert [row["id"] for row in database.claim_instagram_inbound_jobs(limit=10)] == [second]


def test_failed_first_block_holds_later_blocks(tmp_path):
    database = Database(str(tmp_path / "delivery.db"))
    inbound, _ = database.enqueue_instagram_inbound_job("event-1", "customer-1", "task", {})
    first = database.enqueue_instagram_outbound_job(inbound, 1, "customer-1", {"text": "first"})
    database.enqueue_instagram_outbound_job(inbound, 2, "customer-1", {"text": "second"})
    assert [job["id"] for job in database.claim_instagram_outbound_jobs(limit=10)] == [first]
    database.complete_instagram_outbound_job(first, success=False, error="temporary")
    assert database.claim_instagram_outbound_jobs(limit=10) == []


def test_customer_data_deletion_removes_queued_message_and_reply(tmp_path):
    database = Database(str(tmp_path / "delivery.db"))
    inbound, _ = database.enqueue_instagram_inbound_job(
        "event-1", "customer-1", "task", {"message": "customer text"},
    )
    database.enqueue_instagram_outbound_job(inbound, 1, "customer-1", {"text": "Approved answer"})
    database.delete_user_data("customer-1")
    assert database.instagram_job_counts() == {"inbound": {}, "outbound": {}}
