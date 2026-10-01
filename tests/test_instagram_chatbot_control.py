"""QA-C1: production-safe Instagram LIVE / TEST_ONLY channel control."""
from unittest.mock import patch
import hashlib
import hmac
import json
import logging

import pytest
from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia import instagram_chatbot_control as control
from camellia.config import config
from camellia.database import Database
from camellia.session_memory import session_memory
from camellia.admin_security import hash_password


client = TestClient(app_module.app)


def _auth():
    return {"X-Admin-Key": config.ADMIN_API_KEY, "X-Admin-Pin": config.ADMIN_API_KEY}


def _event(sender_id="REALCUSTOMER", username="real_customer", text="Tell me about Sondesh"):
    return {
        "sender": {"id": sender_id, "username": username},
        "message": {"mid": f"mid-{sender_id}-{abs(hash(text))}", "text": text},
    }


def _payload(event):
    return {"object": "instagram", "entry": [{"id": "business-account", "messaging": [event]}]}


class Collector:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


class MustRemainSilent:
    def add_task(self, *_args, **_kwargs):
        raise AssertionError("blocked customer reached chatbot work queue")


@pytest.fixture
def store(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "qa_c1.db"))
    control.clear_runtime_cache()
    monkeypatch.setattr(control, "_database", lambda: database)
    monkeypatch.setattr("camellia.ad_gate.db", database)
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr("camellia.handoff.db", database)
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", "")
    monkeypatch.setattr(config, "ADMIN_API_KEY", "qa-c1-admin-key")
    yield database
    control.clear_runtime_cache()


def _counts(database):
    with database.get_connection() as conn:
        return {
            name: conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
            for name in (
                "conversations", "cross_channel_handoffs", "escalations",
                "candidate_templates", "active_conversation_sessions",
                "instagram_inbound_jobs", "instagram_outbound_jobs",
            )
        }


def test_fresh_bootstrap_is_live_and_preserves_current_bypass_behavior(store):
    state = control.get_state(database=store, strict=True)
    assert state["mode"] == control.LIVE
    assert state["tester_count"] == 0
    ok, sender, text, ad_id = app_module.ad_gate.inspect_and_gate_message(_event())
    assert ok is True
    assert sender == "REALCUSTOMER"
    assert text == "Tell me about Sondesh"
    assert ad_id is None


def test_test_only_blocked_customer_never_reaches_chatbot_business_logic(store):
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    before = _counts(store)
    result = app_module._dispatch_instagram_payload(_payload(_event()), MustRemainSilent())
    after = _counts(store)

    assert result["results"][0]["action"] == "ignored_test_only"
    assert before == after
    identity = store.get_instagram_customer_identity("REALCUSTOMER")
    assert identity and identity["customer_username"] == "real_customer"


def test_test_only_blocks_order_human_and_attachment_without_queueing(store):
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    for event in (
        _event(text="I want 2 boxes Sondesh Friday dinner"),
        _event(text="human please"),
        {"sender": {"id": "REALCUSTOMER", "username": "real_customer"},
         "message": {"mid": "attachment-mid", "attachments": [{"type": "image"}]}},
    ):
        app_module._dispatch_instagram_payload(_payload(event), MustRemainSilent())
    counts = _counts(store)
    assert counts["conversations"] == 0
    assert counts["cross_channel_handoffs"] == 0
    assert counts["escalations"] == 0
    assert counts["candidate_templates"] == 0
    assert counts["active_conversation_sessions"] == 0
    assert counts["instagram_inbound_jobs"] == 0
    assert counts["instagram_outbound_jobs"] == 0


def test_exact_allowlisted_sender_is_queued_in_test_only(store):
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    control.add_test_account(sender_id="TEST123", username="chatbot_testing.168", actor="owner", database=store)
    tasks = Collector()
    result = app_module._dispatch_instagram_payload(
        _payload(_event("TEST123", "chatbot_testing.168")), tasks,
    )
    assert result["results"][0]["action"] == "queued"
    assert len(tasks.tasks) == 1


def test_username_alone_never_authorizes_different_sender(store):
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    control.add_test_account(sender_id="111", username="tester", actor="owner", database=store)
    assert control.is_sender_allowed("111", database=store)
    assert not control.is_sender_allowed("222", database=store)
    ok, *_ = app_module.ad_gate.inspect_and_gate_message(_event("222", "tester"))
    assert ok is False


def test_same_sender_remains_allowed_after_username_change(store):
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    control.add_test_account(sender_id="111", username="old_name", actor="owner", database=store)
    ok, *_ = app_module.ad_gate.inspect_and_gate_message(_event("111", "new_name"))
    assert ok is True
    assert control.is_sender_allowed("111", database=store)
    assert store.get_instagram_customer_identity("111")["customer_username"] == "new_name"


def test_recent_blocked_account_can_be_promoted_by_exact_observed_username(store):
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    ok, *_ = app_module.ad_gate.inspect_and_gate_message(_event("555", "chatbot_testing.168"))
    assert ok is False
    recent = control.recent_accounts(database=store)
    assert any(row["sender_id"] == "555" and not row["allowed"] for row in recent)

    state = control.add_test_account(username="@chatbot_testing.168", actor="owner", database=store)
    assert state["tester_count"] == 1
    assert control.is_sender_allowed("555", database=store)


def test_mode_changes_apply_immediately_and_tester_removal_is_immediate(store):
    event = _event("A", "tester_a")
    assert app_module.ad_gate.inspect_and_gate_message(event)[0] is True

    control.set_mode(control.TEST_ONLY, "owner", database=store)
    assert app_module.ad_gate.inspect_and_gate_message(event)[0] is False

    control.add_test_account(sender_id="A", username="tester_a", actor="owner", database=store)
    assert app_module.ad_gate.inspect_and_gate_message(event)[0] is True

    control.remove_test_account("A", "owner", database=store)
    assert app_module.ad_gate.inspect_and_gate_message(event)[0] is False

    control.set_mode(control.LIVE, "owner", database=store)
    assert app_module.ad_gate.inspect_and_gate_message(_event("B", "other"))[0] is True


def test_persistent_test_only_survives_runtime_cache_reset(store):
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    control.add_test_account(sender_id="TEST123", username="tester", actor="owner", database=store)
    control.clear_runtime_cache(store)
    state = control.get_state(database=store, strict=True)
    assert state["mode"] == control.TEST_ONLY
    assert [row["sender_id"] for row in state["test_accounts"]] == ["TEST123"]


def test_read_failure_uses_last_good_and_cold_failure_fails_closed(store, monkeypatch):
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    control.add_test_account(sender_id="TEST123", actor="owner", database=store)
    assert control.is_sender_allowed("TEST123", database=store)

    monkeypatch.setattr(store, "get_connection", lambda: (_ for _ in ()).throw(OSError("storage unavailable")))
    state = control.get_state(database=store)
    assert state["mode"] == control.TEST_ONLY
    assert state["storage_available"] is False
    assert control.is_sender_allowed("TEST123", database=store)
    assert not control.is_sender_allowed("OTHER", database=store)

    class BrokenDatabase:
        db_path = "cold-broken"
        def get_connection(self):
            raise OSError("cold storage unavailable")

    broken = BrokenDatabase()
    control.clear_runtime_cache(broken)
    cold = control.get_state(database=broken)
    assert cold["mode"] == control.TEST_ONLY
    assert cold["tester_count"] == 0
    assert control.is_sender_allowed("ANYONE", database=broken) is False


def test_control_actions_are_audited_without_raw_sender_id(store):
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    control.add_test_account(sender_id="17840000123456789", username="tester", actor="owner", database=store)
    control.remove_test_account("17840000123456789", "owner", database=store)
    rows = control.audit_log(database=store)
    actions = [row["action"] for row in rows]
    assert actions[:3] == ["TEST_ACCOUNT_REMOVED", "TEST_ACCOUNT_ADDED", "MODE_CHANGED"]
    assert all("17840000123456789" not in str(row) for row in rows)


def test_admin_api_is_authenticated_and_supports_zero_tester_test_mode(store):
    assert client.get("/admin/chatbot-control").status_code == 403
    summary = client.get("/admin/chatbot-control", headers=_auth())
    assert summary.status_code == 200
    assert summary.json()["mode"] == control.LIVE

    changed = client.post(
        "/admin/chatbot-control/mode", json={"mode": "TEST_ONLY"}, headers=_auth()
    )
    assert changed.status_code == 200
    assert changed.json()["mode"] == control.TEST_ONLY
    assert changed.json()["tester_count"] == 0

    added = client.post(
        "/admin/chatbot-control/test-accounts",
        json={"sender_id": "TEST123", "username": "chatbot_testing.168"}, headers=_auth(),
    )
    assert added.status_code == 200
    assert added.json()["tester_count"] == 1

    removed = client.post(
        "/admin/chatbot-control/test-accounts/remove",
        json={"sender_id": "TEST123"}, headers=_auth(),
    )
    assert removed.status_code == 200
    assert removed.json()["tester_count"] == 0


def test_admin_card_is_global_below_header_not_inside_qa_tab(store):
    page = client.get("/admin").text
    assert "Instagram Chatbot" in page
    assert "ON — LIVE" in page
    assert "OFF — TEST ACCOUNTS ONLY" in page
    assert "document.querySelector('.admin-header')" in page
    assert "header.insertAdjacentElement('afterend',card)" in page
    assert "Manage Test Accounts" in page
    assert "Test access is bound to the exact Meta sender identity" in page


def test_live_mode_does_not_change_existing_ad_gate_when_bypass_is_off(store, monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", False)
    assert control.get_state(database=store, strict=True)["mode"] == control.LIVE
    organic = _event("ORGANIC", "organic")
    assert app_module.ad_gate.inspect_and_gate_message(organic)[0] is False
    ad = _event("ADUSER", "aduser")
    ad["message"]["referral"] = {"source": "ADS", "ad_id": "ad-1"}
    ok, sender, _, ad_id = app_module.ad_gate.inspect_and_gate_message(ad)
    assert ok is True and sender == "ADUSER" and ad_id == "ad-1"


@pytest.mark.parametrize("boundary", ["message", "shared", "unreadable", "reply"])
def test_direct_processing_and_reply_boundaries_cannot_bypass_test_only(store, boundary):
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    before = _counts(store)
    with patch.object(session_memory, "get_facts", side_effect=AssertionError("session mutated")), \
         patch.object(app_module.responder, "match_and_respond", side_effect=AssertionError("responder called")), \
         patch.object(app_module, "send_instagram_reply", side_effect=AssertionError("bot replied")), \
         patch.object(app_module, "_cached_instagram_identity", side_effect=AssertionError("identity lookup called")):
        if boundary == "message":
            app_module._process_instagram_event("REALCUSTOMER", "human please", None)
        elif boundary == "shared":
            app_module._handle_shared_attachment("REALCUSTOMER", {"shortcode": "shared"})
        elif boundary == "unreadable":
            app_module._reply_to_unreadable_attachment("REALCUSTOMER")
        else:
            assert app_module._send_or_queue_instagram_reply("REALCUSTOMER", "test")["status"] == "blocked"
    assert before == _counts(store)


def test_previously_queued_inbound_and_outbound_are_suppressed_after_switch(store):
    job_id, _ = store.enqueue_instagram_inbound_job("queue-event", "REALCUSTOMER", "_process_instagram_event", {"args": ["REALCUSTOMER", "Tell me about Sondesh", None], "kwargs": {}})
    store.enqueue_instagram_outbound_job(job_id, 1, "REALCUSTOMER", {"text": "old queued reply"})
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    with patch.object(session_memory, "get_facts", side_effect=AssertionError("session mutated")), patch.object(app_module, "send_instagram_reply", side_effect=AssertionError("bot replied")):
        app_module._process_instagram_inbound_jobs(job_id=job_id)
    assert not store.claim_instagram_inbound_jobs(limit=1)
    assert not store.claim_instagram_outbound_jobs(limit=1)
    assert _counts(store)["conversations"] == 0


def test_failed_commit_preserves_previous_live_state_and_cache(store, monkeypatch):
    assert control.get_state(database=store)["mode"] == control.LIVE
    original = store.get_connection
    class FailingCommit:
        def __init__(self):
            self.conn = original()
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, traceback):
            self.conn.rollback()
            self.conn.close()
        def execute(self, *args):
            return self.conn.execute(*args)
        def commit(self):
            raise OSError("commit failed")
    monkeypatch.setattr(store, "get_connection", FailingCommit)
    with pytest.raises(OSError):
        control.set_mode(control.TEST_ONLY, "owner", database=store)
    assert control.get_state(database=store)["mode"] == control.LIVE
    monkeypatch.setattr(store, "get_connection", original)
    assert control.get_state(database=store, strict=True)["mode"] == control.LIVE
    assert not control.audit_log(database=store)


def test_blocked_webhook_returns_200_and_logs_only_hash(store, monkeypatch, caplog):
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    monkeypatch.setattr(config, "META_APP_SECRET", "qa-c1-signature-secret")
    caplog.set_level(logging.INFO)
    event = _event("1784000987654321", "private_username", "private message human please")
    raw = json.dumps(_payload(event)).encode()
    signature = "sha256=" + hmac.new(b"qa-c1-signature-secret", raw, hashlib.sha256).hexdigest()
    with patch.object(app_module.responder, "match_and_respond", side_effect=AssertionError("responder called")), patch.object(app_module, "send_instagram_reply", side_effect=AssertionError("bot replied")):
        response = client.post("/webhook/instagram", content=raw, headers={"X-Hub-Signature-256": signature})
    assert response.status_code == 200
    assert response.json()["results"][0]["action"] == "ignored_test_only"
    assert "1784000987654321" not in caplog.text
    assert "private_username" not in caplog.text and "private message" not in caplog.text


@pytest.mark.parametrize("event", [
    {"sender": {"id": "REALCUSTOMER"}, "message": {"is_echo": True, "text": "echo"}},
    {"sender": {"id": "REALCUSTOMER"}, "read": {"watermark": 123}},
    {"sender": {"id": "REALCUSTOMER"}, "delivery": {"mids": ["delivery-id"]}},
])
def test_meta_receipts_and_echoes_do_not_enter_business_logic(store, event):
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    before = _counts(store)
    app_module._dispatch_instagram_payload(_payload(event), MustRemainSilent())
    assert before == _counts(store)


def test_allowed_tester_retains_exact_qa_order_and_handoff_flow(store):
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    control.add_test_account(sender_id="TEST123", actor="owner", database=store)
    sent = []
    with patch.object(app_module, "send_instagram_reply", side_effect=lambda recipient, text, **kwargs: sent.append(text)), patch.object(app_module.escalator, "handle_escalation", return_value={"escalation_id": 1}), patch("camellia.ai_responder.ai_responder._query_groq", side_effect=AssertionError("LLM called")):
        app_module._process_instagram_event("TEST123", "How do I order?", None)
        assert "https://wa.me/" in " ".join(sent)
        sent.clear()
        app_module._process_instagram_event("TEST123", "human please", None)
    assert store.get_active_handoff("instagram", "TEST123") is not None


def test_username_resolution_limit_and_restart_reinitialization(store):
    with pytest.raises(ValueError, match="not found"):
        control.add_test_account(username="missing", actor="owner", database=store)
    control.observe_sender("111", username="duplicate", database=store)
    control.observe_sender("222", username="duplicate", database=store)
    with pytest.raises(ValueError, match="more than one"):
        control.add_test_account(username="duplicate", actor="owner", database=store)
    for number in range(20):
        control.add_test_account(sender_id=f"tester{number}", actor="owner", database=store)
    with pytest.raises(ValueError, match="At most 20"):
        control.add_test_account(sender_id="tester21", actor="owner", database=store)
    control.set_mode(control.TEST_ONLY, "owner", database=store)
    reopened = Database(store.db_path)
    state = control.get_state(database=reopened)
    assert state["mode"] == control.TEST_ONLY and state["tester_count"] == 20


def test_browser_csrf_and_authenticated_actor(store, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "control-owner")
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", hash_password("qa-c1-test-passphrase"))
    monkeypatch.setattr(config, "ADMIN_REQUIRE_HTTPS", True)
    with TestClient(app_module.app, base_url="https://testserver") as browser:
        assert browser.post("/admin/login", json={"username": "control-owner", "password": "qa-c1-test-passphrase"}).status_code == 200
        assert browser.post("/admin/chatbot-control/mode", json={"mode": "TEST_ONLY"}).status_code == 403
        csrf = {"X-CSRF-Token": browser.cookies.get("camellia_admin_csrf")}
        result = browser.post("/admin/chatbot-control/mode", json={"mode": "TEST_ONLY"}, headers=csrf)
        assert result.status_code == 200 and result.json()["updated_by"] == "control-owner"
        assert browser.post("/admin/chatbot-control/test-accounts", json={"sender_id": "111"}).status_code == 403
        assert browser.post("/admin/chatbot-control/test-accounts/remove", json={"sender_id": "111"}).status_code == 403


@pytest.mark.parametrize("path", ["/mode", "/test-accounts", "/test-accounts/remove"])
def test_unauthenticated_writes_fail_closed(store, path):
    assert client.post("/admin/chatbot-control" + path, json={}).status_code == 403
