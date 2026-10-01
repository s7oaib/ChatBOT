"""QA-C1: production-safe Instagram LIVE / TEST_ONLY channel control."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia import instagram_chatbot_control as control
from camellia.config import config
from camellia.database import Database


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

    assert result["results"][0]["action"] == "ignored_organic_dm"
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
