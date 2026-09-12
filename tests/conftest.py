"""
Makes the repo root importable for the test suite.

Tests import the bot as `camellia.*` and the helper scripts as `scripts.*`,
both of which live at the repo root rather than beside the tests, so pytest
needs the root on sys.path regardless of where it was invoked from.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolate_process_session_cache(monkeypatch, request):
    """Normal tests must not restore state written by an earlier test run."""
    from camellia import app as app_module, database as db_module, handoff as handoff_module
    from camellia.monitoring import delivery_monitor
    from camellia.session_memory import session_memory

    monkeypatch.setattr(session_memory, "persistence_enabled", False)
    # Keep modules that imported the singleton before a test swapped it on a
    # single canonical object. Individual tests can still monkeypatch these
    # attributes and pytest will restore them afterwards.
    monkeypatch.setattr(app_module, "db", db_module.db)
    monkeypatch.setattr(handoff_module, "db", db_module.db)
    # CI supplies a deliberately non-production token so health tests must
    # not call Meta. Production validation remains real; only this explicit
    # test-mode value gets a deterministic helper response.
    if str(app_module.config.INSTAGRAM_ACCESS_TOKEN or "").startswith("mock_"):
        monkeypatch.setattr(
            app_module,
            "_check_instagram_token",
            lambda token, url, identity: {
                "state": "valid",
                "identity": identity,
                "detail": "mock token accepted by test helper",
            },
        )
    session_memory._sessions.clear()
    app_module._ig_token_state["checked_at"] = 0.0
    app_module._ig_token_state["result"] = None
    delivery_monitor._consecutive_failures.clear()
    delivery_monitor._alerted.clear()
    # A unittest module removes its temporary SQLite file at class teardown,
    # while later pytest modules still share the long-lived Database object.
    # Recreate the ordinary test schema only when that replacement left the
    # shared file without the handoff table; production sends never do this.
    with db_module.db.get_connection() as conn:
        has_handoffs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cross_channel_handoffs'"
        ).fetchone()
    if not has_handoffs:
        db_module.db.init_db()
    # These tests use stable customer IDs and inspect the handoff table with a
    # bounded listing. Clear prior test data so a previous run cannot push the
    # current handoff outside that bound or make the test appear deduplicated.
    with db_module.db.get_connection() as conn:
        conn.execute("DELETE FROM cross_channel_handoffs")
        conn.commit()
    yield
    session_memory._sessions.clear()
    app_module._ig_token_state["checked_at"] = 0.0
    app_module._ig_token_state["result"] = None
    delivery_monitor._consecutive_failures.clear()
    delivery_monitor._alerted.clear()
