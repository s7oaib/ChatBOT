"""Operational diagnostics never become knowledge or mutate runtime state."""
import json
import logging
import shutil
import sqlite3
import time

import pytest
import httpx
from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia.config import config
from camellia.database import Database
from camellia.monitoring import ProviderEvents, get_logger, provider_http_outcome
from camellia.operations import backup_database, backup_visibility, database_summary


def record_route(database, **values):
    defaults = dict(intents=[], entities={}, confidence=None, knowledge_ids=[], actions=[],
                    llm_called=False, fallback_used=False, grounding_result=None,
                    knowledge_gap=False, escalation_status="not_required")
    database.log_message_route(**{**defaults, **values})


@pytest.fixture
def database(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "isolated.sqlite"))
    monkeypatch.setattr(app_module, "db", database)
    return database


def test_operations_requires_auth():
    assert TestClient(app_module.app).get("/admin/operations").status_code in (401, 403)


def test_operations_read_only_and_bounded(database):
    connection = database.get_connection()
    connection.execute("PRAGMA query_only=ON")
    statements = []
    connection.set_trace_callback(statements.append)
    original = database.get_connection
    database.get_connection = lambda: connection
    try:
        summary = database_summary(database, 5000)
    finally:
        database.get_connection = original
    assert summary["scope"] == "latest_200_records_per_stream"
    assert len([s for s in statements if s.startswith("SELECT")]) == 7
    assert not any(s.startswith(("UPDATE", "INSERT", "DELETE")) for s in statements)


def test_operations_content_not_exposed(database):
    record_route(database, user_id="private-user", channel="instagram", user_message="PRIVATE conversation",
                               route="fixture", actions=["OBSERVABILITY_PATH:CONTROLLED_RAG"],
                               knowledge_gap=True, decision_lane="GROUNDED_APPROVED")
    summary = database_summary(database)
    assert summary["answer_paths"] == {"CONTROLLED_RAG": 1}
    assert summary["unsupported"] == 1
    assert "PRIVATE" not in json.dumps(summary)
    assert "private-user" not in json.dumps(summary)


@pytest.mark.parametrize("path", ["DETERMINISTIC", "DYNAMIC_APPROVED_MENU", "CONTROLLED_RAG", "HANDOFF"])
def test_recorded_paths_are_not_guessed(database, path):
    record_route(database, user_id="fixture", channel="instagram", user_message="fixture",
                               route="arbitrary", actions=["OBSERVABILITY_PATH:" + path])
    assert database_summary(database)["answer_paths"] == {path: 1}


def test_historical_path_unknown_not_inferred(database):
    record_route(database, user_id="fixture", channel="instagram", user_message="fixture", route="price")
    assert database_summary(database)["answer_paths"] == {"OTHER_PRE_INSTRUMENTATION": 1}


@pytest.mark.parametrize("status,outcome", [(429, "rate_limited"), (401, "auth_config_failure"),
                                            (403, "auth_config_failure"), (503, "unavailable")])
def test_provider_http_categories(status, outcome):
    assert provider_http_outcome(status) == outcome


def test_provider_events_bounded_and_timestamped():
    monitor = ProviderEvents()
    for _ in range(60):
        monitor.record("structured", "timeout", time.monotonic(), fallback=True)
    summary = monitor.snapshot()
    assert summary["counts"]["timeout"] == 60
    assert len(summary["recent"]) == 50
    assert summary["scope"] == "since_process_start"
    assert summary["recent"][0]["timestamp"].endswith("+00:00")
    assert summary["recent"][0]["fallback_used"] is True


@pytest.mark.parametrize("value", ["Bearer SECRET", "api_key=SECRET", "password=SECRET",
                                    "cookie=SECRET", "session_token=SECRET", "gsk_SECRET",
                                    "private@example.org", "+919876543210"])
def test_touched_logger_sanitizes(value, caplog):
    with caplog.at_level(logging.INFO):
        get_logger("phase2f-test").info("failure %s", value)
    assert value not in caplog.text


def test_configured_secret_and_exception_payload_not_logged(monkeypatch, caplog):
    monkeypatch.setattr(config, "GROQ_API_KEY", "arbitrary-private-key")
    with caplog.at_level(logging.ERROR):
        try:
            raise ValueError("PRIVATE CUSTOMER CONVERSATION arbitrary-private-key")
        except ValueError:
            get_logger("phase2f-test").exception("runtime failure arbitrary-private-key")
    assert "arbitrary-private-key" not in caplog.text
    assert "PRIVATE CUSTOMER" not in caplog.text
    assert "ValueError" in caplog.text


def test_backup_contains_committed_wal_and_disposable_recovery(database, tmp_path):
    source = sqlite3.connect(database.db_path)
    source.execute("PRAGMA journal_mode=WAL")
    source.execute("CREATE TABLE operational_fixture(value TEXT)")
    source.execute("INSERT INTO operational_fixture VALUES('committed-wal')")
    source.commit()
    first = backup_database(database.db_path)
    second = backup_database(database.db_path)
    assert first["verified"] and second["verified"]
    assert first["backup_path"] != second["backup_path"]
    restore = tmp_path / "disposable-restore.sqlite"
    shutil.copy2(first["backup_path"], restore)
    with sqlite3.connect(restore) as recovered:
        assert recovered.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert recovered.execute("SELECT value FROM operational_fixture").fetchone()[0] == "committed-wal"
        for table in ("conversations", "sessions", "menu_versions", "cross_channel_handoffs", "candidate_templates"):
            recovered.execute(f"SELECT * FROM {table} LIMIT 1").fetchall()
    source.close()


def test_backup_unknown_no_schedule_fabricated(database):
    assert backup_visibility(database.db_path) == {"status": "unknown", "last_modified": None,
                                                  "off_host": "unknown", "schedule": "unknown"}


def test_backup_missing_source_does_not_create_database(tmp_path):
    source = tmp_path / "absent.sqlite"
    with pytest.raises(FileNotFoundError):
        backup_database(source)
    assert not source.exists()


def test_database_failure_not_ready(monkeypatch):
    monkeypatch.setattr(app_module, "_health_details", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("PRIVATE path")))
    result = TestClient(app_module.app).get("/health")
    assert result.status_code == 503
    assert result.json()["status"] == "not_ready"
    assert "PRIVATE" not in result.text


def test_health_does_not_materialize_full_decision_history(database, monkeypatch):
    monkeypatch.setattr(database, "auto_resolve_evaluation", lambda: (_ for _ in ()).throw(AssertionError("unbounded evaluation")))
    result = TestClient(app_module.app).get("/health")
    assert result.status_code == 200
    assert result.json()["auto_resolve"]["readiness"] == "ADMIN_EVALUATION_REQUIRED"


def test_public_health_paths_and_identity_removed(database):
    result = TestClient(app_module.app).get("/health")
    assert result.status_code == 200
    assert "qa_templates_path" not in result.text
    assert "menu_json_path" not in result.text
    assert '"path"' not in result.text
    assert "detail" not in result.json()["instagram_token"]


def test_provider_failure_does_not_kill_deterministic_readiness(database, monkeypatch):
    original = app_module.ai_responder.status
    monkeypatch.setattr(app_module.ai_responder, "status", lambda: {**original(), "enabled": True, "last_call_ok": False})
    result = TestClient(app_module.app).get("/health")
    assert result.status_code == 200
    assert result.json()["status"] == "degraded"


def test_no_public_debug_or_metrics_routes():
    paths = {route.path for route in app_module.app.routes}
    assert not paths & {"/debug", "/internal-test", "/smoke-test", "/metrics"}


def test_admin_summary_retention_unknown_without_deletion(database):
    result = TestClient(app_module.app).get("/admin/operations", headers={"x-admin-key": config.ADMIN_API_KEY})
    assert result.status_code == 200
    assert result.json()["retention"]["other_history"] == "RETENTION_POLICY_UNDEFINED"
    assert result.json()["whatsapp"] == "HUMAN ONLY"


@pytest.mark.parametrize("case,outcome", [("timeout", "timeout"), (429, "rate_limited"),
                                          (401, "auth_config_failure"), (503, "unavailable"),
                                          ("bad_json", "validation_failure"), ("empty", "invalid_response"),
                                          (200, "success")])
def test_actual_groq_call_records_outcome_without_prompt(monkeypatch, case, outcome):
    from camellia import monitoring
    from camellia.ai_responder import AIResponder
    monitor = ProviderEvents()
    monkeypatch.setattr(monitoring, "provider_events", monitor)
    monkeypatch.setattr(config, "GROQ_API_KEY", "fixture-key")
    monkeypatch.setattr(config, "GROQ_TIMEOUT", 0.01)

    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, *args, **kwargs):
            if case == "timeout":
                raise httpx.ReadTimeout("PRIVATE prompt")
            if case == "bad_json":
                return httpx.Response(200, content=b"not json")
            if case == "empty":
                return httpx.Response(200, json={"choices": []})
            return httpx.Response(case, json={"choices": [{"message": {"content": "fixture answer"}}]})

    monkeypatch.setattr(httpx, "Client", Client)
    AIResponder()._query_groq("PRIVATE prompt")
    assert monitor.snapshot()["counts"] == {outcome: 1}
    assert "PRIVATE" not in json.dumps(monitor.snapshot())


def test_menu_pending_failure_and_publication_are_separate(database):
    with database.get_connection() as conn:
        conn.execute("INSERT INTO menu_drafts(menu_key,draft_json,review_json) VALUES(?,?,?)",
                     ("fixture", "[]", json.dumps({"status": "NEEDS_REVIEW", "diagnostic_codes": ["EXTRACTION_EXCEPTION"],
                                                       "raw_text": "PRIVATE OCR"})))
        conn.execute("INSERT INTO menu_versions(menu_key,snapshot_json,is_current,action) VALUES(?,?,?,?)",
                     ("fixture", "[]", 1, "publish"))
        conn.commit()
    result = database_summary(database)
    assert result["menu_reviews"][0]["status"] == "NEEDS_REVIEW"
    assert result["menu_reviews"][0]["extraction_failed"]
    assert result["menu_versions"][0]["is_current"] == 1
    assert "PRIVATE" not in json.dumps(result)


def test_operations_query_count_does_not_grow_with_history(database):
    def measure():
        conn = database.get_connection()
        statements = []
        conn.set_trace_callback(statements.append)
        original = database.get_connection
        database.get_connection = lambda: conn
        try:
            result = database_summary(database, 10)
        finally:
            database.get_connection = original
            conn.close()
        return len([s for s in statements if s.startswith("SELECT")]), result
    before, _ = measure()
    for index in range(50):
        record_route(database, user_id="fixture", channel="instagram", user_message="fixture", route=str(index))
    after, result = measure()
    assert before == after == 7
    assert result["sample_sizes"]["responses"] == 10


def test_expired_handoff_is_not_counted_as_waiting_or_mutated(database):
    database.create_handoff(handoff_ref="fixture-expired", source_channel="instagram", source_user_id="fixture",
                            reason="fixture", summary="", known_entities={}, unresolved_topic=None,
                            escalation_id=None, expires_at="2000-01-01 00:00:00")
    database.update_handoff_status("fixture-expired", "WAITING_FOR_HUMAN")
    assert database_summary(database)["handoffs"] == {"EXPIRED": 1}
    assert database.get_handoff_by_ref("fixture-expired")["status"] == "WAITING_FOR_HUMAN"


def test_admin_health_ui_shows_bounded_authenticated_operations():
    page = TestClient(app_module.app).get("/admin").text
    assert "fetch('/admin/operations', { headers: getAuthHeaders()" in page
    for label in ("WhatsApp", "Recent answer paths", "Handoff lifecycle (sample)", "Menu review status",
                  "Provider outcomes since process start", "Last local backup"):
        assert label in page


def test_valid_json_with_invalid_ocr_schema_is_validation_failure(monkeypatch):
    from camellia import monitoring
    from camellia.menu_parser import menu_parser
    monitor = ProviderEvents()
    monkeypatch.setattr(monitoring, "provider_events", monitor)
    monkeypatch.setattr(config, "GROQ_API_KEY", "fixture-key")

    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, *args, **kwargs):
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr(httpx, "Client", Client)
    result = menu_parser._parse_image_with_groq(b"synthetic fixture bytes", "image/jpeg")
    assert result["status"] == "failed"
    assert monitor.snapshot()["counts"] == {"validation_failure": 1}


def test_expired_version_flag_not_claimed_as_current_authority(database):
    with database.get_connection() as conn:
        conn.execute("INSERT INTO menu_versions(menu_key,snapshot_json,is_current,valid_from,valid_until) VALUES(?,?,?,?,?)",
                     ("fixture", "[]", 1, "2000-01-01", "2000-01-07"))
        conn.commit()
    result = database_summary(database)
    assert result["menu_versions"][0]["is_current"] == 1
    assert result["menu_versions"][0]["lifecycle_window_status"] == "EXPIRED"
    assert "flags alone are not current authority" in result["menu_authority"]


def test_selected_deterministic_response_not_mislabelled_by_stale_actions(database, monkeypatch):
    from camellia.responder import responder
    from camellia.session_memory import session_memory
    from camellia.database import db
    monkeypatch.setattr(db, "db_path", database.db_path)
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "GROQ_ENABLED", False)
    user = "phase2f-isolated-path"
    session_memory.clear_session(user, "instagram")
    session_memory.get_facts(user, "instagram")["last_actions"] = ["ESCALATE_TO_TEAM"]
    try:
        reply, matched, route, _ = responder.match_and_respond("Tell me about Bengali Home Table", user_id=user, channel="instagram")
        assert matched and reply and route == "bengali_home_table_details"
        assert database_summary(database)["answer_paths"] == {"DETERMINISTIC": 1}
    finally:
        session_memory.clear_session(user, "instagram")
