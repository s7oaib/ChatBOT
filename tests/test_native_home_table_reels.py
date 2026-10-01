"""QA-M1B: verified official shares, safe fallback, existing durable generations."""
import json

import httpx
import pytest

from camellia import app as app_module, client_qa, home_table_native_reels as native
from camellia import instagram_chatbot_control as control
from camellia.config import config
from camellia.database import Database
from camellia.session_memory import SessionMemoryManager

URLS = client_qa.HOME_TABLE_REEL_URLS
USER = "3141110206000000"
BLOCKS = [{"kind": "TEXT", "text": "approved before"}] + [
    {"kind": "LINK", "url": url} for url in URLS
] + [{"kind": "TEXT", "text": "approved after"}]


@pytest.fixture
def store(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "native.db"))
    memory = SessionMemoryManager(persistence_enabled=True, database=database)
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(app_module, "session_memory", memory)
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", native.VERIFIED_ACCOUNT_ID)
    monkeypatch.setattr(config, "INSTAGRAM_EXPECTED_USERNAME", "camellia.artisanal")
    monkeypatch.setattr(config, "INSTAGRAM_AUTH_MODE", "instagram_login")
    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", "native-test-token")
    control.clear_runtime_cache()
    yield database, memory
    control.clear_runtime_cache()


def transport(monkeypatch, responder=None):
    bodies = []
    original = httpx.Client

    def handle(request):
        assert request.url.host == "graph.instagram.com"
        assert request.url.path.endswith("/messages")
        body = json.loads(request.content)
        if body["message"].get("attachment", {}).get("type") == "MEDIA_SHARE":
            assert request.url.path.endswith("/" + native.VERIFIED_ACCOUNT_ID + "/messages")
        bodies.append(body)
        if responder:
            result = responder(request, body)
            if result is not None:
                return result
        return httpx.Response(200, json={"message_id": "accepted-" + str(len(bodies))})

    monkeypatch.setattr(app_module.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs
    ))
    return bodies


def enqueue(database, event):
    job_id, _ = database.enqueue_instagram_inbound_job(event, USER, "_process_instagram_event", {})
    token = app_module._instagram_job_context.set({"job_id": job_id, "sequence": 0})
    try:
        app_module.send_instagram_response_blocks(USER, BLOCKS, inbound_message_id=123)
    finally:
        app_module._instagram_job_context.reset(token)
    return job_id


def receipts(database, memory):
    return database.home_table_reel_receipts(USER, memory.get_facts(USER, "instagram")["home_table_reel_delivery_id"])


def ids(bodies):
    return [b["message"]["attachment"]["payload"]["id"] for b in bodies if "attachment" in b["message"]]


def test_exact_mapping_and_account_boundary(store, monkeypatch):
    assert tuple(native.VERIFIED_MEDIA) == URLS
    assert list(native.VERIFIED_MEDIA.values()) == [
        "18073790945717461", "18191102770349496", "18158439172491979", "17908289592288134",
    ]
    assert len(set(native.VERIFIED_MEDIA.values())) == 4
    assert native.verified_media_id(URLS[0] + "?guess=true") is None
    assert native.verified_media_id(URLS[0].replace("DdJhXQMT4VZ", "ddjhxqmt4vz")) is None
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", "other-account")
    assert native.verified_media_id(URLS[0]) is None


def test_native_payload_order_text_and_receipts(store, monkeypatch):
    database, memory = store
    bodies = transport(monkeypatch)
    enqueue(database, "first")
    app_module._process_instagram_outbound_jobs()
    assert bodies[0]["message"] == {"text": "approved before"}
    assert bodies[-1]["message"] == {"text": "approved after"}
    assert ids(bodies) == list(native.VERIFIED_MEDIA.values())
    assert [b["message"]["attachment"]["type"] for b in bodies[1:5]] == ["MEDIA_SHARE"] * 4
    rows = receipts(database, memory)
    assert len(rows) == 4
    assert all(r["meta_message_id"] and r["outbound_job_id"] and r["confirmed_at"] for r in rows)
    details = sorted([json.loads(r["delivery_metadata"]) for r in rows], key=lambda d: d["link_index"])
    assert [d["link_index"] for d in details] == [1, 2, 3, 4]
    assert [d["verified_media_id"] for d in details] == ids(bodies)
    assert all(d["delivery_mode"] == "MEDIA_SHARE" for d in details)
    assert memory.get_facts(USER, "instagram")["home_table_videos_sent"]


def test_replay_and_new_inbound_generation(store, monkeypatch):
    database, memory = store
    bodies = transport(monkeypatch)
    enqueue(database, "same")
    app_module._process_instagram_outbound_jobs()
    first_generation = memory.get_facts(USER, "instagram")["home_table_reel_delivery_id"]
    enqueue(database, "same")
    app_module._process_instagram_outbound_jobs()
    assert len(ids(bodies)) == 4
    enqueue(database, "new")
    app_module._process_instagram_outbound_jobs()
    assert len(ids(bodies)) == 8
    assert len(database.home_table_reel_receipts(USER, first_generation)) == 4
    assert len(receipts(database, memory)) == 4


def test_partial_timeout_restart_missing_only_no_url_duplicate(store, monkeypatch):
    database, memory = store
    third = list(native.VERIFIED_MEDIA.values())[2]
    failed = [False]

    def fail_once(request, body):
        if body["message"].get("attachment", {}).get("payload", {}).get("id") == third and not failed[0]:
            failed[0] = True
            raise httpx.ReadTimeout("uncertain send", request=request)

    bodies = transport(monkeypatch, fail_once)
    enqueue(database, "partial")
    app_module._process_instagram_outbound_jobs()
    assert len(receipts(database, memory)) == 2
    assert not any(b["message"].get("text") in URLS for b in bodies)
    restarted_database = Database(database.db_path)
    restarted = SessionMemoryManager(persistence_enabled=True, database=restarted_database)
    monkeypatch.setattr(app_module, "db", restarted_database)
    monkeypatch.setattr(app_module, "session_memory", restarted)
    with restarted_database.get_connection() as conn:
        conn.execute("UPDATE instagram_outbound_jobs SET next_attempt_at=datetime('now','-1 second')")
        conn.commit()
    bodies.clear()
    app_module._process_instagram_outbound_jobs()
    assert ids(bodies) == list(native.VERIFIED_MEDIA.values())[2:]
    assert len(receipts(restarted_database, restarted)) == 4


@pytest.mark.parametrize("error", [
    {"code": 100, "message": "Unsupported attachment type MEDIA_SHARE"},
    {"code": 10, "message": "Permission denied for media sharing"},
    {"code": 200, "message": "MEDIA_SHARE not available"},
])
def test_explicit_native_rejection_falls_back_and_records_mode(store, monkeypatch, error):
    database, memory = store

    def rejection(request, body):
        if "attachment" in body["message"]:
            return httpx.Response(400, json={"error": error})

    bodies = transport(monkeypatch, rejection)
    app_module.send_instagram_response_blocks(USER, BLOCKS, inbound_message_id=1)
    assert [b["message"].get("text") for b in bodies if "text" in b["message"]] == ["approved before", *URLS, "approved after"]
    assert len(receipts(database, memory)) == 4
    assert all(json.loads(r["delivery_metadata"])["delivery_mode"] == "URL_FALLBACK" for r in receipts(database, memory))


@pytest.mark.parametrize("status,payload", [
    (200, {}), (500, {"error": {"code": 100, "message": "unsupported MEDIA_SHARE"}}),
    (400, {"error": {"code": 100, "message": "Invalid recipient"}}),
])
def test_ambiguous_or_unrelated_rejection_has_no_url_fallback(store, monkeypatch, status, payload):
    bodies = transport(monkeypatch, lambda request, body: httpx.Response(status, json=payload))
    result = app_module.send_instagram_reply(USER, URLS[0], native_media_id=native.VERIFIED_MEDIA[URLS[0]])
    assert result["status"] == "failed"
    assert len(bodies) == 1
    assert "text" not in bodies[0]["message"]


def test_wrong_media_never_sends(store, monkeypatch):
    bodies = transport(monkeypatch)
    result = app_module.send_instagram_reply(USER, URLS[0], native_media_id="invented")
    assert result["status"] == "failed"
    assert bodies == []


def test_url_mode_remains_available_for_other_account(store, monkeypatch):
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", "other")
    bodies = transport(monkeypatch)
    app_module.send_instagram_response_blocks(USER, BLOCKS)
    assert not ids(bodies)
    assert [b["message"]["text"] for b in bodies] == ["approved before", *URLS, "approved after"]


def test_test_only_blocked_sender_has_no_delivery_or_receipts(store, monkeypatch):
    database, memory = store
    control.set_mode(control.TEST_ONLY, "operator", database=database)
    bodies = transport(monkeypatch)
    app_module.send_instagram_response_blocks(USER, BLOCKS)
    assert bodies == []
    assert receipts(database, memory) == []


def test_additive_receipt_upgrade_preserves_existing_acceptance(store):
    database, _ = store
    with database.get_connection() as conn:
        conn.execute("INSERT INTO home_table_reel_receipts (recipient_id,delivery_id,url,meta_message_id) VALUES (?,?,?,?)",
                     (USER, "legacy", URLS[0], "legacy-accepted"))
        conn.execute("ALTER TABLE home_table_reel_receipts DROP COLUMN delivery_metadata")
        conn.commit()
    upgraded = Database(database.db_path)
    row = upgraded.home_table_reel_receipts(USER, "legacy")[0]
    assert row["url"] == URLS[0] and row["meta_message_id"] == "legacy-accepted"
    assert row["delivery_metadata"] is None


def test_effective_approved_home_table_copy_stays_exact(store, monkeypatch):
    database, _ = store
    monkeypatch.setattr("camellia.database.db", database)
    match = client_qa.resolve_effective_approved_qa_by_id("home_table_details")
    assert match is not None
    text = match.entry.answer
    bodies = transport(monkeypatch)
    app_module.send_instagram_response_blocks(USER, client_qa.build_home_table_reel_blocks(text))
    assert ids(bodies) == list(native.VERIFIED_MEDIA.values())
    assert "".join(b["message"].get("text", "") for b in bodies) == text


@pytest.mark.parametrize("kind", ["story", "post", "reel"])
def test_mapped_source_uses_same_native_path(store, monkeypatch, kind):
    from importlib import import_module
    from camellia.responder import responder
    database, memory = store
    monkeypatch.setattr(import_module("camellia.responder"), "session_memory", memory)
    monkeypatch.setattr("camellia.database.db", database)
    text = "Approved opening. " + client_qa.HOME_TABLE_VIDEO_ANCHOR + "\nApproved remaining text."
    route = database.save_instagram_source_route({
        "source_type": kind, "media_id": "exact-home-" + kind,
        "routing_topic": "Bengali Home Table", "source_description": text,
        "active": True, "menu_mode": "NONE", "priority_products": [], "knowledge_scope": [],
    })
    memory.get_facts(USER, "instagram")["source_route"] = route
    answer, matched, template, _ = responder.match_and_respond(
        "What is this story about?", user_id=USER, channel="instagram",
    )
    assert matched and template == "source_description" and answer == text
    blocks = memory.get_facts(USER, "instagram")["ordered_response_blocks"]
    bodies = transport(monkeypatch)
    app_module.send_instagram_response_blocks(USER, blocks)
    assert ids(bodies) == list(native.VERIFIED_MEDIA.values())
    assert "".join(b["message"].get("text", "") for b in bodies) == text


@pytest.mark.parametrize("text", ["Approved text without anchor", "Home Table price", "Home Table payment"])
def test_missing_anchor_or_continuation_never_invents_shares(store, monkeypatch, text):
    bodies = transport(monkeypatch)
    app_module.send_instagram_response_blocks(USER, client_qa.build_home_table_reel_blocks(text))
    assert not ids(bodies)
    assert [b["message"]["text"] for b in bodies] == [text]
