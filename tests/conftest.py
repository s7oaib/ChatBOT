"""
Makes the repo root importable for the test suite.

Tests import the bot as `camellia.*` and the helper scripts as `scripts.*`,
both of which live at the repo root rather than beside the tests, so pytest
needs the root on sys.path regardless of where it was invoked from.
"""

import sys
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set DB_PATH before pytest imports application modules. Several legacy modules
# initialize the shared Database singleton during collection, so a fixture
# would be too late to prevent tests from touching the developer's ignored
# camellia.db. Keep all suite-level DB work in an OS temporary directory.
_ORIGINAL_DB_PATH = os.environ.get("DB_PATH")
_TEST_ENV_KEYS = (
    "GROQ_API_KEY",
    "GROQ_ENABLED",
    "SEMANTIC_ROUTER_ENABLED",
    "LLM_TRIAGE_ENABLED",
    "LLM_ASSISTED_RESOLUTION_ENABLED",
    "LLM_AUTO_RESOLVE_SIMULATION_ENABLED",
    "ENABLE_WEB_TESTER",
    "ADMIN_API_KEY",
    "INSTAGRAM_ACCESS_TOKEN",
    "META_APP_SECRET",
    "META_VERIFY_TOKEN",
)
_ORIGINAL_TEST_ENV = {key: os.environ.get(key) for key in _TEST_ENV_KEYS}
_PYTEST_DB_DIR = tempfile.TemporaryDirectory(
    prefix="camellia-pytest-", ignore_cleanup_errors=True,
)
_PYTEST_DB_PATH = str(Path(_PYTEST_DB_DIR.name) / "camellia-test.db")

# config.py normally reloads .env with override=True. Do not load developer
# credentials or service flags into normal tests; they must use explicit
# per-test mocks. Pin the SQLite path and external-service settings before
# importing application modules.
os.environ["DB_PATH"] = _PYTEST_DB_PATH
os.environ.update({
    "GROQ_API_KEY": "",
    "GROQ_ENABLED": "false",
    "SEMANTIC_ROUTER_ENABLED": "true",
    "LLM_TRIAGE_ENABLED": "false",
    "LLM_ASSISTED_RESOLUTION_ENABLED": "false",
    "LLM_AUTO_RESOLVE_SIMULATION_ENABLED": "false",
    "ENABLE_WEB_TESTER": "true",
    "ADMIN_API_KEY": "pytest-only-admin-api-key-7f410b33",
    "INSTAGRAM_ACCESS_TOKEN": "",
    "META_APP_SECRET": "",
    "META_VERIFY_TOKEN": "",
})

# Application modules can be imported by other pytest plugins before ordinary
# fixtures run. Pin the singleton as well as the environment before test-module
# collection, when some legacy tests call init_db() at module import time.
import dotenv as _dotenv_module

_original_dotenv_loader = _dotenv_module.load_dotenv
_dotenv_module.load_dotenv = lambda *args, **kwargs: False
try:
    from camellia.config import config as _app_config
    from camellia.database import db as _app_db
finally:
    _dotenv_module.load_dotenv = _original_dotenv_loader

_ORIGINAL_CONFIG_DB_PATH = _app_config.DB_PATH
_ORIGINAL_DATABASE_PATH = _app_db.db_path
_app_config.DB_PATH = _PYTEST_DB_PATH
_app_db.db_path = _PYTEST_DB_PATH


def pytest_unconfigure(config):
    _app_db.db_path = _ORIGINAL_DATABASE_PATH
    _app_config.DB_PATH = _ORIGINAL_CONFIG_DB_PATH
    if _ORIGINAL_DB_PATH is None:
        os.environ.pop("DB_PATH", None)
    else:
        os.environ["DB_PATH"] = _ORIGINAL_DB_PATH
    for key, value in _ORIGINAL_TEST_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _PYTEST_DB_DIR.cleanup()


@pytest.fixture(autouse=True)
def approved_qa_acceptance_clock(monkeypatch):
    """Canonical acceptance copy is asserted as of QA-A1, before Bento expiry.

    Lifecycle tests override this clock explicitly to prove scheduled/expired
    behavior. Production uses the real timezone-aware clock.
    """
    from camellia import approved_qa
    monkeypatch.setattr(approved_qa, "now_utc", lambda: datetime(2026, 10, 1, tzinfo=timezone.utc))


@pytest.fixture(autouse=True)
def isolate_process_session_cache(monkeypatch, request):
    """Normal tests must not restore state written by an earlier test run."""
    from camellia import app as app_module, database as db_module, handoff as handoff_module
    from camellia.monitoring import delivery_monitor
    from camellia.session_memory import session_memory

    monkeypatch.setattr(session_memory, "persistence_enabled", False)
    # Security tests explicitly use HTTPS; ordinary unit tests use the local
    # HTTP TestClient and must not be mistaken for production edge traffic.
    monkeypatch.setattr(app_module.config, "ADMIN_REQUIRE_HTTPS", False)
    # Unit responder tests are not Meta ad-gate integration tests. The handful
    # of tests for the production gate set this explicitly to False.
    monkeypatch.setattr(app_module.config, "BYPASS_AD_GATE", True)
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
        # Review drafts are per-test fixtures, not shared customer knowledge.
        # An unapproved upload left by an earlier test must not turn unrelated
        # responder cases into pending-menu handoffs.
        conn.execute("DELETE FROM menu_drafts")
        conn.commit()
    yield
    session_memory._sessions.clear()
    app_module._ig_token_state["checked_at"] = 0.0
    app_module._ig_token_state["result"] = None
    delivery_monitor._consecutive_failures.clear()
    delivery_monitor._alerted.clear()


@pytest.fixture
def current_legacy_baseline(monkeypatch, tmp_path):
    """A dated published baseline, not a replacement for its real selector."""
    import json
    from datetime import date
    from camellia import menu_dates, weekly_savoury
    from camellia.config import config
    from camellia.responder import responder
    from camellia.rag_fallback import rag_fallback_engine
    # Routing tests already use this explicit business date; authority tests
    # control their own dates and never receive this fixture implicitly.
    business_date = date(2026, 6, 21)
    monkeypatch.setattr(menu_dates, "today_ist", lambda: business_date)
    baseline = json.loads((ROOT / "data" / "menu.json").read_text(encoding="utf-8"))
    baseline["weekly_menus"][-1].update(
        week_of="19 Jun - 25 Jun 2026", is_published=True,
    )
    menu_path = tmp_path / "eligible-baseline.json"
    menu_path.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(menu_path))
    for engine in (responder, rag_fallback_engine):
        monkeypatch.setattr(engine, "menu_path", str(menu_path))
        monkeypatch.setattr(engine, "menu_data", engine._load_json(str(menu_path)))
    assert weekly_savoury.legacy_baseline_current()
    return menu_path


@pytest.fixture
def eligible_weekly_asset(monkeypatch, tmp_path, current_legacy_baseline):
    """Real image rows and authority queries share one isolated database."""
    from camellia import menu_images, menu_review, menu_catalogue, weekly_savoury
    from camellia.responder import responder
    db = _app_db
    monkeypatch.setattr(db, "db_path", str(tmp_path / "eligible-weekly.db"))
    db.init_db()
    for module in (menu_images, menu_review, menu_catalogue, weekly_savoury):
        monkeypatch.setattr(module, "db", db)

    url = "/uploads/test-reviewed-current-weekly.jpeg"
    db.save_menu_image_draft("weekly_vegetarian", url, "test-fixture")
    db.publish_menu_image("weekly_vegetarian")
    assert weekly_savoury.legacy_baseline_current()
    assert not responder._weekly_menu_expired()
    assert menu_images.current_weekly()["image_path"] == url
    return url
