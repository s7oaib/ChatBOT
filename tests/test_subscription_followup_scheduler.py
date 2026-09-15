from datetime import datetime, timedelta
import pytest

from camellia import app as app_module
from camellia import subscription_followups as followups
from camellia.database import Database
from camellia.session_memory import session_memory


BASE = datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture
def database(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "followups.db"))
    monkeypatch.setattr(followups, "db", database)
    monkeypatch.setattr(app_module, "db", database)
    return database


def schedule(database, customer="customer-1", sent_at=BASE, **kwargs):
    return followups.schedule_subscription_followup(
        customer_id=customer,
        successful_send_at=sent_at,
        source_conversation_id=kwargs.get("source_conversation_id", 42),
        source_event_id=kwargs.get("source_event_id", "mid.1"),
    )


def run(database, sender, resolver, now=BASE + timedelta(hours=24)):
    return followups.run_due_subscription_followups(
        sender=sender,
        template_resolver=resolver,
        now=now,
    )


def test_subscription_trigger_creates_one_pending_task(database, monkeypatch):
    user_id = "ig-sub-trigger"
    session_memory.clear_session(user_id, "instagram")
    monkeypatch.setattr(followups, "utc_now", lambda: BASE)
    monkeypatch.setattr(
        app_module.responder,
        "match_and_respond",
        lambda *args, **kwargs: ("subscription answer", True, "subscription_details", None),
    )
    sent = []
    monkeypatch.setattr(
        app_module,
        "send_instagram_reply",
        lambda *args, **kwargs: sent.append((args, kwargs)) or {"message_id": "mid.reply"},
    )

    app_module._process_instagram_event(
        user_id, "tell me about subscription", "ad-1", inbound_event_id="mid.1",
    )

    rows = database.list_subscription_followups()
    assert len(rows) == 1
    assert rows[0]["status"] == "PENDING"
    assert rows[0]["followup_type"] == followups.FOLLOWUP_TYPE
    assert rows[0]["scheduled_for"] == "2026-01-02T12:00:00"
    assert len(sent) == 1


def test_duplicate_trigger_creates_no_second_task(database, monkeypatch):
    user_id = "ig-sub-duplicate"
    session_memory.clear_session(user_id, "instagram")
    monkeypatch.setattr(
        app_module.responder,
        "match_and_respond",
        lambda *args, **kwargs: ("subscription answer", True, "subscription_details", None),
    )
    monkeypatch.setattr(app_module, "send_instagram_reply", lambda *args, **kwargs: {"message_id": "reply"})

    for event_id in ("mid.1", "mid.2"):
        app_module._process_instagram_event(
            user_id, "tell me about subscription", "ad-1", inbound_event_id=event_id,
        )

    assert database.count_subscription_followups() == 1


def test_failed_initial_send_creates_no_task(database, monkeypatch):
    user_id = "ig-sub-failed-initial"
    session_memory.clear_session(user_id, "instagram")
    monkeypatch.setattr(
        app_module.responder,
        "match_and_respond",
        lambda *args, **kwargs: ("subscription answer", True, "subscription_details", None),
    )
    monkeypatch.setattr(
        app_module,
        "send_instagram_reply",
        lambda *args, **kwargs: {"status": "failed", "error": "mock failure"},
    )

    app_module._process_instagram_event(user_id, "subscription", "ad-1", inbound_event_id="mid.fail")

    assert database.count_subscription_followups() == 0


def test_non_subscription_response_creates_no_task(database, monkeypatch):
    user_id = "ig-not-subscription"
    session_memory.clear_session(user_id, "instagram")
    monkeypatch.setattr(
        app_module.responder,
        "match_and_respond",
        lambda *args, **kwargs: ("price answer", True, "pricing_general", None),
    )
    monkeypatch.setattr(app_module, "send_instagram_reply", lambda *args, **kwargs: {"message_id": "reply"})

    app_module._process_instagram_event(user_id, "price", "ad-1", inbound_event_id="mid.price")

    assert database.count_subscription_followups() == 0


def test_task_is_not_due_at_23h59m(database):
    schedule(database)
    calls = []
    result = run(
        database,
        lambda *args, **kwargs: calls.append(args),
        lambda _kind: {"text": "approved later copy", "media": None},
        now=BASE + timedelta(hours=23, minutes=59),
    )
    assert result["claimed"] == 0
    assert calls == []


def test_task_is_due_after_24h(database):
    schedule(database)
    result = run(
        database,
        lambda *args, **kwargs: {"message_id": "followup.1"},
        lambda _kind: {"text": "approved later copy", "media": None},
    )
    assert result["sent"] == 1
    assert database.list_subscription_followups()[0]["status"] == "SENT"


def test_successful_send_marks_sent_and_sent_task_is_not_sent_again(database):
    schedule(database)
    calls = []
    sender = lambda *args, **kwargs: calls.append((args, kwargs)) or {"status": "sent"}
    resolver = lambda _kind: {"text": "approved later copy", "media": None}

    assert run(database, sender, resolver)["sent"] == 1
    assert run(database, sender, resolver)["claimed"] == 0
    assert len(calls) == 1


def test_worker_restart_does_not_duplicate_completed_send(database):
    schedule(database)
    calls = []
    sender = lambda *args, **kwargs: calls.append(args) or {"message_id": "once"}
    resolver = lambda _kind: {"text": "approved later copy", "media": None}

    run(database, sender, resolver)
    restarted = Database(database.db_path)
    # The durable SENT state is sufficient for a newly-created worker; no
    # in-process cache is consulted by the second run.
    original_db = followups.db
    followups.db = restarted
    try:
        assert run(restarted, sender, resolver)["claimed"] == 0
    finally:
        followups.db = original_db
    assert len(calls) == 1


def test_missing_approved_copy_is_deferred_and_never_sends(database):
    schedule(database)
    calls = []
    result = run(database, lambda *args, **kwargs: calls.append(args), lambda _kind: None)
    row = database.list_subscription_followups()[0]
    assert result["deferred"] == 1
    assert row["status"] == "PENDING"
    assert row["last_error"] == "approved_template_unavailable"
    assert calls == []


def test_failed_send_is_preserved_and_explicit_retry_can_succeed(database):
    schedule(database, customer="retry-customer")
    result = run(
        database,
        lambda *args, **kwargs: {"status": "failed", "error": "temporary Meta failure"},
        lambda _kind: {"text": "approved later copy", "media": None},
    )
    failed = database.list_subscription_followups()[0]
    assert result["failed"] == 1
    assert failed["status"] == "FAILED"
    assert failed["failure_count"] == 1
    assert database.retry_subscription_followup(failed["id"])

    calls = []
    result = run(
        database,
        lambda *args, **kwargs: calls.append(args) or {"message_id": "retry-ok"},
        lambda _kind: {"text": "approved later copy", "media": None},
    )
    assert result["sent"] == 1
    assert len(calls) == 1
    assert database.list_subscription_followups()[0]["status"] == "SENT"


def test_cancelled_task_never_sends(database):
    task = schedule(database, customer="cancel-customer")
    assert database.cancel_subscription_followup(task["id"], "manual_cancel")
    calls = []
    result = run(
        database,
        lambda *args, **kwargs: calls.append(args),
        lambda _kind: {"text": "approved later copy", "media": None},
    )
    assert result["claimed"] == 0
    assert calls == []
    assert database.list_subscription_followups()[0]["status"] == "CANCELLED"


def test_whatsapp_task_is_cancelled_and_never_auto_sends(database):
    database.schedule_subscription_followup(
        customer_id="wa-customer", channel="whatsapp", followup_type=followups.FOLLOWUP_TYPE,
        scheduled_for="2026-01-02T12:00:00",
        idempotency_key="whatsapp-followup-test",
    )
    calls = []
    result = run(
        database,
        lambda *args, **kwargs: calls.append(args),
        lambda _kind: {"text": "approved later copy", "media": None},
    )
    assert result["cancelled"] == 1
    assert calls == []
    assert database.list_subscription_followups()[0]["status"] == "CANCELLED"


def test_process_and_database_restart_preserve_pending_task(database):
    scheduled = schedule(database, customer="restart-customer")
    restarted = Database(database.db_path)
    rows = restarted.list_subscription_followups()
    assert rows[0]["id"] == scheduled["id"]
    assert rows[0]["status"] == "PENDING"
    assert rows[0]["scheduled_for"] == "2026-01-02T12:00:00"


def test_idempotency_survives_database_restart(database):
    first = schedule(database, customer="idempotent-customer")
    restarted = Database(database.db_path)
    followups.db = restarted
    second = schedule(restarted, customer="idempotent-customer", source_conversation_id=999)
    assert second["id"] == first["id"]
    assert restarted.count_subscription_followups() == 1


def test_media_capable_payload_is_forwarded_without_publishing_copy(database):
    schedule(database, customer="media-customer")
    captured = []
    media = [{"url": "/images/approved.png", "caption": "approved asset"}]
    result = run(
        database,
        lambda *args, **kwargs: captured.append((args, kwargs)) or {"message_id": "media-ok"},
        lambda _kind: {"text": "approved later copy", "media": media},
    )
    assert result["sent"] == 1
    assert captured[0][1]["image_url"] == media
    assert captured[0][1]["response_type"] == "scheduled_followup"
