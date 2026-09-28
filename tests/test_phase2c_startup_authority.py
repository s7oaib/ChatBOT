"""Restart must preserve stored facts; stale overrides never become authority."""
import copy
import json
from datetime import date

import pytest

from camellia import app as app_module, menu_dates, menu_review, order_calculator
from camellia.config import config
from camellia.database import db
from camellia.responder import responder
from camellia.rag_fallback import rag_fallback_engine


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    path = tmp_path / "menu.json"
    baseline = json.loads(open(config.MENU_JSON_PATH, encoding="utf-8").read())
    baseline["weekly_menus"] = [
        {"week_of": "18 Sep - 24 Sep 2026", "vegetarian": []},
        {"week_of": "25 Sep - 30 Sep 2026", "is_published": True,
         "vegetarian": [{"item": "Synthetic dish", "price_inr": 400, "quantity": "123g"}],
         "bengali_bento": [{"name": "Synthetic Bento", "price_inr": 999, "includes": ["Synthetic contents"]}]},
    ]
    baseline["collections"]["aamar_mishti"]["products"].append(
        {"item": "Jolbhora", "prices": [{"pack_size": "250g", "pieces": 3, "price_inr": 123}]})
    path.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(path))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "startup.db"))
    monkeypatch.setattr(db, "db_path", config.DB_PATH)
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    for engine in (responder, rag_fallback_engine):
        monkeypatch.setattr(engine, "menu_path", str(path))
        monkeypatch.setattr(engine, "menu_data", copy.deepcopy(baseline))
    monkeypatch.setattr(order_calculator, "_weekly_cache", {"mtime": None, "table": {}})
    db.init_db()
    return path


def _version(start="2026-09-25", end="2026-09-30", state=None):
    approval = {"rows": [{"item": "Synthetic dish", "category": "vegetarian", "quantity": "250g",
                          "price_inr": 450, "description": "", "aliases": []}],
                "approved_at": "2026-09-25T10:00:00Z", "image_path": "/uploads/synthetic-approved-menu.jpeg"}
    with db.get_connection() as conn:
        conn.execute("INSERT INTO menu_versions(menu_key,snapshot_json,approval_json,is_current,valid_from,valid_until,weekly_state) VALUES(?,?,?,?,?,?,?)",
                     ("weekly_vegetarian", "[]", json.dumps(approval), 0, start, end, state))
        conn.commit()


def _pending(status="NEEDS_REVIEW"):
    with db.get_connection() as conn:
        conn.execute("INSERT INTO menu_drafts(menu_key,draft_json,review_json) VALUES(?,?,?)",
                     ("weekly_vegetarian", "[]", json.dumps({"status": status, "rows": [], "revision": 1})))
        conn.commit()


def _facts():
    with db.get_connection() as conn:
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("response_template_overrides", "response_template_override_versions",
                              "menu_versions", "menu_drafts", "menu_catalogue", "menu_image_versions")}


@pytest.mark.asyncio
async def test_repeated_real_lifespan_preserves_all_menu_facts(isolated, monkeypatch):
    for key in ("weekly_menu_veg", "weekly_menu_nonveg"):
        db.save_response_template_override(key, "Synthetic stored price ₹400", "operator")
    _version()
    _version("2026-09-18", "2026-09-24", "SUPERSEDED")
    _pending()
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    monkeypatch.setattr(config, "GOOGLE_SHEET_CSV_URL", "https://docs.google.com/synthetic.csv")
    monkeypatch.setattr(config, "INSTAGRAM_STORY_AUTO_SYNC_ENABLED", False)
    monkeypatch.setattr(app_module.escalator, "process_pending_notifications", lambda **kwargs: 0)
    monkeypatch.setattr(app_module, "migrate_legacy_routing_topics", lambda: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("Startup must not import, prune or migrate business facts")

    monkeypatch.setattr(app_module.sheet_sync, "sync_from_csv_url", forbidden)
    monkeypatch.setattr(app_module, "_prune_expired_weekly_menus", forbidden)
    from camellia import content_integrity
    monkeypatch.setattr(content_integrity, "apply_confirmed_menu_facts", forbidden)
    monkeypatch.setattr(content_integrity, "apply_safe_content_remediations", forbidden)

    async def idle(stop):
        await stop.wait()

    monkeypatch.setattr(app_module, "_human_notification_loop", idle)
    monkeypatch.setattr(app_module, "_instagram_job_loop", idle)
    before, raw = _facts(), isolated.read_bytes()
    for _ in range(2):
        async with app_module.lifespan(app_module.app):
            assert _facts() == before
            assert isolated.read_bytes() == raw
        assert _facts() == before
        assert isolated.read_bytes() == raw


@pytest.mark.parametrize("state", ["current", "pending", "expired"])
def test_stale_weekly_override_never_beats_reviewed_authority(isolated, state):
    db.save_response_template_override("weekly_menu_veg", "Synthetic dish ₹400", "legacy")
    if state == "expired":
        _version("2026-09-18", "2026-09-24")
    else:
        _version()
    if state == "pending":
        _pending()
    responder.reload_data()
    text, _ = responder.render_approved_template("weekly_menu_veg")
    result = rag_fallback_engine.retrieve_approved_menu("Synthetic dish price")
    if state == "current":
        assert "450" in text and "400" not in text
        assert "450" in result.auto_response and "400" not in result.auto_response
    else:
        assert "400" not in text and "450" not in text
        assert not result.auto_response
    assert db.get_response_template_override("weekly_menu_veg")["response"] == "Synthetic dish ₹400"
    if state == "pending":
        assert "weekly_vegetarian" in menu_review.customer_blocked_keys()
