"""QA-A1 authority isolation, transactional publication and time-bound delivery."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from camellia import approved_qa, app as app_module, client_qa, grounded_answers
from camellia.admin_security import hash_password
from camellia.config import config
from camellia.database import Database
from camellia.responder import responder
from camellia.session_memory import session_memory


@pytest.fixture
def store(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "approved.db"))
    monkeypatch.setattr(approved_qa, "_database", lambda: database)
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(approved_qa, "now_utc", lambda: datetime(2026, 10, 1, tzinfo=timezone.utc))
    return database


def publish(question="Do you offer X?", answer="Approved test answer.", **fields):
    body = {"question": question, "answer": answer, **fields}
    preview = approved_qa.preview(body)
    body.update(qa_id=preview["qa_id"], preview_token=preview["preview_token"])
    return approved_qa.publish(body, "test_admin")


def test_empty_overlay_is_byte_identical_to_all_23_entries(store):
    before = client_qa.SOURCE_PATH.read_bytes()
    with store.get_connection() as conn:
        conn.execute("DELETE FROM approved_qa_overrides")
    assert len(client_qa.REGISTRY) == 23
    assert approved_qa.registry() == client_qa.parse_source() == client_qa.REGISTRY
    assert all(a is b for a, b in zip(approved_qa.registry(), client_qa.REGISTRY))
    for entry in client_qa.REGISTRY:
        facts = {"home_table_context": True} if entry.context == "home_table" else {}
        if entry.qa_id == "order_prebook":
            facts["order_flow_context"] = True
        assert client_qa.match(entry.question, facts) == client_qa._match_base(entry.question, facts)
    assert client_qa.SOURCE_PATH.read_bytes() == before


def test_bootstrap_only_bento_and_never_overwrites_operator_choice(store):
    rows = approved_qa.overlays()
    assert set(rows) == {"bento_details_client", "bento_price_client"}
    assert all(row["valid_until"] == "2026-10-11T23:59:59+05:30" for row in rows.values())
    publish(qa_id="bento_details_client", question=client_qa.REGISTRY_BY_ID["bento_details_client"].question,
            answer=client_qa.REGISTRY_BY_ID["bento_details_client"].answer, valid_until=None)
    approved_qa.archive_or_restore("bento_price_client", "owner")
    store.init_db()
    assert approved_qa.overlays()["bento_details_client"]["valid_until"] is None
    assert approved_qa.overlays()["bento_price_client"]["status"] == "ARCHIVED"


def test_add_edit_publish_archive_restore_history_and_exact_customer_answer(store):
    record = publish(aliases=["Tell me about X"])
    key = record["qa_id"]
    for message in ("Do you offer X?", "TELL me about X!!!"):
        assert client_qa.match(message).entry.answer == "Approved test answer."
    draft = {"qa_id": key, "answer": "New exact approved answer."}
    preview = approved_qa.preview(draft)
    assert preview["current"]["answer"] == "Approved test answer."
    assert preview["proposed"]["answer"] == "New exact approved answer."
    assert client_qa.match(record["question"]).entry.answer == record["answer"]
    approved_qa.publish({**draft, "preview_token": preview["preview_token"]}, "owner")
    assert client_qa.match(record["question"]).entry.answer == draft["answer"]
    assert approved_qa.archive_or_restore(key, "owner")["status"] == "ARCHIVED"
    assert client_qa.match("Tell me about X") is None
    assert approved_qa.archive_or_restore(key, "owner", restore=True)["status"] == "ACTIVE"
    history = approved_qa.detail(key)["versions"]
    original = next(v for v in history if v["snapshot"].get("answer") == "Approved test answer.")
    approved_qa.archive_or_restore(key, "owner", restore=True, version_id=original["id"])
    assert client_qa.match("Tell me about X").entry.answer == "Approved test answer."
    assert len(approved_qa.detail(key)["versions"]) > len(history)
    user = uuid.uuid4().hex
    with patch("camellia.ai_responder.ai_responder._query_groq", side_effect=AssertionError("LLM called")):
        response = responder.match_and_respond("Tell me about X", user_id=user, channel="instagram")
    assert response[0] == "Approved test answer."
    assert response[1]
    assert session_memory.get_facts(user, "instagram")["last_route_source"] == "EXACT_APPROVED"


@pytest.mark.parametrize("fields", [
    {"question": ""}, {"answer": " "}, {"valid_until": "not a date"},
    {"valid_until": "2026-02-30T00:00"}, {"valid_from": "2026-10-10", "valid_until": "2026-10-09"},
    {"aliases": ["word", "WORD!"]}, {"aliases": "word"}, {"qa_id": "sondesh_bogus"},
    {"status": "ACTIVE"}, {"asset_keys": ["invented"]}, {"answer": "[IMAGE]"},
])
def test_invalid_drafts_do_not_modify_authority(store, fields):
    before = approved_qa.overlays()
    with pytest.raises(ValueError):
        approved_qa.preview({"question": "Do you offer X?", "answer": "Approved test answer.", **fields})
    assert approved_qa.overlays() == before


@pytest.mark.parametrize("word", ["What's the price of Sondesh?", "how much for sondesh", "sandesh price"])
def test_primary_and_semantic_alias_conflicts_fail_closed(store, word):
    with pytest.raises(ValueError, match="This wording already matches: sondesh_price_client"):
        approved_qa.preview({"question": word, "answer": "Other answer"})
    with pytest.raises(ValueError, match="sondesh_price_client"):
        approved_qa.preview({"question": "Do you offer X?", "answer": "Other answer", "aliases": [word]})


def test_restore_cannot_bypass_conflict_and_runtime_ambiguity_fails_closed(store):
    first = publish(aliases=["Tell me about X"])
    approved_qa.archive_or_restore(first["qa_id"], "owner")
    second = publish(question="Another question?", aliases=["Tell me about X"])
    with pytest.raises(ValueError, match=second["qa_id"]):
        approved_qa.archive_or_restore(first["qa_id"], "owner", restore=True)
    assert approved_qa.detail(first["qa_id"])["status"] == "ARCHIVED"
    rows = approved_qa.overlays()
    rows[first["qa_id"]]["status"] = "ACTIVE"
    assert approved_qa.match("Tell me about X", rows=rows) is None


def test_publish_requires_unchanged_preview_and_fails_atomically(store):
    record = publish()
    draft = {"qa_id": record["qa_id"], "answer": "New answer"}
    with pytest.raises(ValueError, match="Preview"):
        approved_qa.publish(draft, "owner")
    preview = approved_qa.preview(draft)
    with pytest.raises(ValueError, match="changed"):
        approved_qa.publish({**draft, "answer": "Unpreviewed answer", "preview_token": preview["preview_token"]}, "owner")
    publish(question="Unrelated safe question?")
    with pytest.raises(ValueError, match="changed"):
        approved_qa.publish({**draft, "preview_token": preview["preview_token"]}, "owner")
    assert client_qa.match(record["question"]).entry.answer == record["answer"]


def test_duplicate_creation_id_and_preview_new_id_are_safe(store):
    record = publish()
    with pytest.raises(ValueError, match="Duplicate Q&A ID"):
        approved_qa.preview({"qa_id": record["qa_id"], "create": True, "question": "Different question?", "answer": "Other"})
    draft = {"create": True, "question": "A new unique question?", "answer": "Exact"}
    preview = approved_qa.preview(draft)
    approved_qa.publish({**draft, "qa_id": preview["qa_id"], "preview_token": preview["preview_token"]}, "owner")
    assert client_qa.match(draft["question"]).entry.answer == "Exact"


def test_failed_storage_transaction_preserves_live_authority_and_history(store, monkeypatch):
    record = publish()
    draft = {"qa_id": record["qa_id"], "answer": "New answer"}
    preview = approved_qa.preview(draft)
    before = approved_qa.detail(record["qa_id"])
    original = approved_qa._save
    def failed(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("Simulated transaction failure")
    monkeypatch.setattr(approved_qa, "_save", failed)
    with pytest.raises(OSError):
        approved_qa.publish({**draft, "preview_token": preview["preview_token"]}, "owner")
    assert approved_qa.detail(record["qa_id"]) == before


def test_expired_bento_webhook_sends_only_human_fallback(store, monkeypatch):
    monkeypatch.setattr(approved_qa, "now_utc", lambda: datetime.fromisoformat("2026-10-12T00:00:00+05:30"))
    monkeypatch.setattr("camellia.responder.db", store)
    monkeypatch.setattr("camellia.handoff.db", store)
    uid = uuid.uuid4().hex
    session_memory.get_facts(uid, "instagram").update(last_topic="bento", active_product_context="bento")
    sent = []
    with patch.object(app_module, "send_instagram_reply", side_effect=lambda recipient, text, **kwargs: sent.append(text)), patch.object(app_module.escalator, "handle_escalation", return_value={"escalation_id": 1}), patch("camellia.ai_responder.ai_responder._query_groq", side_effect=AssertionError("LLM called")):
        app_module._process_instagram_event(uid, "bento price", "test-ad")
    assert sent and all("₹" not in text and "950" not in text for text in sent)
    assert "Manager of Kitchens" in " ".join(sent)


@pytest.mark.parametrize("instant,available", [
    ("2026-10-11T23:59:58+05:30", True), ("2026-10-11T23:59:59+05:30", True),
    ("2026-10-12T00:00:00+05:30", False),
])
def test_bento_exact_boundary_all_matcher_routes(store, monkeypatch, instant, available):
    monkeypatch.setattr(approved_qa, "now_utc", lambda: datetime.fromisoformat(instant))
    for message, facts in [("tell me about Bengali Bento", {}), ("bento price please", {}),
                           ("how much?", {"last_topic": "bento"}), ("tell me more", {"last_topic": "bento"})]:
        result = client_qa.match(message, facts)
        assert bool(result) == available
        if result:
            assert result.entry is client_qa.REGISTRY_BY_ID[result.entry.qa_id]
    assert bool(grounded_answers._bento_prices()) == available
    assert bool(grounded_answers.derive("which bento is cheapest?", {})) == available
    assert client_qa.match("sondesh price") is not None
    records = approved_qa.listing()["records"]
    assert {r["status"] for r in records if r["qa_id"].startswith("bento_")} == {"ACTIVE" if available else "EXPIRED"}


@pytest.mark.parametrize("message", ["tell me about bento", "bento price", "how much?", "tell me more", "which bento is cheapest?", "Chicken"])
def test_expired_bento_cannot_leak_through_instagram_or_stale_context(store, monkeypatch, message):
    monkeypatch.setattr(approved_qa, "now_utc", lambda: datetime.fromisoformat("2026-10-12T00:00:00+05:30"))
    uid = uuid.uuid4().hex
    facts = session_memory.get_facts(uid, "instagram")
    facts.update(last_topic="bento", active_topic="bento", active_product_context="bento", client_qa_id="bento_price_client")
    with patch("camellia.ai_responder.ai_responder._query_groq", side_effect=AssertionError("LLM called")):
        text, matched, template, assets = responder.match_and_respond(message, user_id=uid, channel="instagram")
    assert not matched
    assert "₹" not in text and "950" not in text and "Chholar" not in text
    assert template not in {"bento_details", "grounded_derived", "bento_grounded_comparison"}


def test_scheduled_activation_and_expiry_without_restart(store, monkeypatch):
    record = publish(valid_from="2026-10-02T00:00:00", valid_until="2026-10-03T23:59:59")
    assert record["status"] == "SCHEDULED"
    assert client_qa.match(record["question"]) is None
    monkeypatch.setattr(approved_qa, "now_utc", lambda: datetime.fromisoformat("2026-10-02T00:00:00+05:30"))
    assert client_qa.match(record["question"]).entry.answer == record["answer"]
    monkeypatch.setattr(approved_qa, "now_utc", lambda: datetime.fromisoformat("2026-10-04T00:00:00+05:30"))
    assert client_qa.match(record["question"]) is None
    assert approved_qa.detail(record["qa_id"])["status"] == "EXPIRED"


def test_read_failure_retains_last_good_overlay_and_cold_failure_uses_static(store, monkeypatch, caplog):
    record = publish()
    assert client_qa.match(record["question"])
    monkeypatch.setattr(store, "get_connection", lambda: (_ for _ in ()).throw(OSError("unavailable")))
    assert client_qa.match(record["question"]).entry.answer == record["answer"]
    approved_qa._snapshots.pop(store, None)
    assert approved_qa.registry() == client_qa.REGISTRY
    assert "last known good" in caplog.text


def test_preview_dry_run_purity_and_approved_list_excludes_learned_candidates(store):
    store.add_learned_knowledge("Forbidden", "Do you offer forbidden?", "Forbidden learned answer")
    with store.get_connection() as conn:
        before = {r[0]: conn.execute('SELECT COUNT(*) FROM "' + r[0] + '"').fetchone()[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with patch("camellia.ai_responder.ai_responder._query_groq", side_effect=AssertionError("LLM called")), patch.object(session_memory, "get_facts", side_effect=AssertionError("session touched")):
        assert approved_qa.test("can u tell me abt bento")["matched_id"] == "bento_details_client"
        approved_qa.preview({"question": "Do you offer X?", "answer": "Safe"})
    assert len(approved_qa.listing()["records"]) == 23
    assert "Forbidden" not in json.dumps(approved_qa.listing())
    with store.get_connection() as conn:
        after = {name: conn.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0] for name in before}
    assert before == after


def test_assets_and_reel_order_survive_base_edit(store):
    base = client_qa.REGISTRY_BY_ID["sondesh_details_client"]
    updated = publish(qa_id=base.qa_id, question=base.question, answer=base.answer.replace("Collection 1.", "Collection one."))
    effective = client_qa.effective_entry(base.qa_id)
    assert effective.answer == updated["answer"]
    assert effective.asset_keys == base.asset_keys
    assert [b for b in effective.blocks if b.kind != "TEXT"] == [b for b in base.blocks if b.kind != "TEXT"]
    assert client_qa.effective_entry("home_table_details") is client_qa.REGISTRY_BY_ID["home_table_details"]


@pytest.fixture
def api(store, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_API_KEY", "test-machine-key")
    limiter = app_module.app.state.limiter.enabled
    app_module.app.state.limiter.enabled = False
    with TestClient(app_module.app, base_url="https://testserver") as client:
        yield client
    app_module.app.state.limiter.enabled = limiter


@pytest.mark.parametrize("path", ["/preview", "/publish", "/test", "/bento_details_client/archive", "/bento_details_client/restore", "/bento_details_client/restore/1"])
def test_all_write_routes_require_auth(api, path):
    assert api.post("/admin/approved-qa" + path, json={}).status_code == 403


def test_api_reads_auth_malformed_json_preview_and_csrf(api, store, monkeypatch):
    headers = {"X-Admin-Key": "test-machine-key"}
    assert api.get("/admin/approved-qa").status_code == 403
    assert api.get("/admin/approved-qa", headers=headers).status_code == 200
    assert api.post("/admin/approved-qa/preview", headers=headers, content="{").status_code == 400
    draft = {"question": "Do you offer X?", "answer": "Approved test answer."}
    preview = api.post("/admin/approved-qa/preview", headers=headers, json=draft)
    assert preview.status_code == 200
    data = preview.json()
    assert api.post("/admin/approved-qa/publish", headers=headers, json={**draft, "qa_id": data["qa_id"], "preview_token": data["preview_token"]}).status_code == 200
    monkeypatch.setattr(config, "ADMIN_USERNAME", "owner")
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", hash_password("safe-test-passphrase"))
    monkeypatch.setattr(config, "ADMIN_REQUIRE_HTTPS", True)
    assert api.post("/admin/login", json={"username": "owner", "password": "safe-test-passphrase"}).status_code == 200
    assert api.post("/admin/approved-qa/preview", json=draft).status_code == 403
    csrf = api.cookies.get("camellia_admin_csrf")
    assert api.post("/admin/approved-qa/test", headers={"X-CSRF-Token": csrf}, json={"message": "sondesh price"}).status_code == 200


def test_dedicated_ui_and_legacy_surface_are_separate(api):
    page = api.get("/admin").text
    assert 'id="tab-approved-qa"' in page and "Approved Q&amp;A" in page
    assert "Advanced / Legacy" in page and 'id="tab-custom-qa"' in page
    assert "Preview / Test" in page and "Publish Approved Q&amp;A" in page
    assert "Asia/Kolkata" in page and "Archived / Expired" in page
    assert 'id="aqPublishButton"' in page and 'id="aqPublish"' not in page
    assert 'if(!adminAuthenticated)return;' in page
    assert page.count('style.display !== "none") loadApprovedQa();') == 2
