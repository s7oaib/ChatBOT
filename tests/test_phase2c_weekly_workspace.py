"""Chronological comparison and readable weekly workspace; no second timeline."""
import json
from datetime import date

import pytest

from camellia import menu_dates, menu_review, weekly_savoury
from camellia.config import config
from camellia.database import db
from camellia.admin_weekly_savoury import WEEKLY_HTML, WEEKLY_SCRIPT


@pytest.fixture
def timeline(tmp_path, monkeypatch):
    path = tmp_path / "menu.json"
    path.write_text('{"weekly_menus":[]}', encoding="utf-8")
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(path))
    monkeypatch.setattr(db, "db_path", str(tmp_path / "timeline.db"))
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    db.init_db()


def _version(start, end, item, price):
    rows = [{"item": item, "category": "vegetarian", "quantity": "250g", "price_inr": price, "aliases": []}]
    with db.get_connection() as conn:
        conn.execute("INSERT INTO menu_versions(menu_key,snapshot_json,approval_json,valid_from,valid_until) VALUES(?,?,?,?,?)",
                     ("weekly_vegetarian", "[]", json.dumps({"rows": rows}), start, end))
        conn.commit()


def test_comparison_predecessor_is_chronological_not_latest_inserted_or_future(timeline):
    _version("2026-09-18", "2026-09-24", "Preceding week", 100)
    _version("2026-10-02", "2026-10-08", "Future week", 300)
    _version("2026-09-04", "2026-09-10", "Older backfill", 50)
    assert weekly_savoury.previous_rows(valid_from="2026-09-25")[0]["item"] == "Preceding week"


def test_new_week_analysis_compares_with_correct_previous_period(timeline):
    _version("2026-09-18", "2026-09-24", "Test dish", 100)
    _version("2026-10-02", "2026-10-08", "Test dish", 900)
    draft = menu_review.extract("weekly_vegetarian", "admin", text="25 Sep - 30 Sep 2026\nVeg:\nTest dish 250g ₹120", weekly=True)
    changes = menu_review.weekly_analysis(draft)["changes"]
    assert changes["price_changed"][0]["before"]["price_inr"] == 100
    assert changes["suspicious_price"] == []


def test_comparison_records_section_change_without_fuzzy_matching():
    old = {"item": "Test dish", "category": "vegetarian", "quantity": "250g", "price_inr": 100}
    new = {**old, "category": "non_vegetarian"}
    changes = weekly_savoury.compare_rows([new], [old])
    assert len(changes["section_changed"]) == 1
    assert changes["unchanged"] == []
    unrelated = weekly_savoury.compare_rows([{**new, "item": "Test dissh"}], [old])
    assert unrelated["new"] and unrelated["removed"]


def test_weekly_tabs_and_quantities_are_explicit_in_ui():
    for pane in ("current", "upcoming", "history"):
        assert f'data-ws-pane="{pane}"' in WEEKLY_HTML
    assert "wsShowTimeline" in WEEKLY_SCRIPT
    assert "item.before.quantity" in WEEKLY_SCRIPT and "item.after.quantity" in WEEKLY_SCRIPT
    for label in ("UNCHANGED", "NEW", "REMOVED", "PRICE CHANGED", "QUANTITY CHANGED", "SECTION CHANGED", "SUSPICIOUS PRICE", "SUSPICIOUS QUANTITY"):
        assert label in WEEKLY_SCRIPT


def test_a_b_c_timeline_uses_preceding_week_for_each_comparison(timeline):
    _version("2026-09-18", "2026-09-24", "Week A", 100)
    _version("2026-10-02", "2026-10-08", "Week C", 300)
    _version("2026-09-25", "2026-09-30", "Week B", 200)
    assert weekly_savoury.previous_rows(valid_from="2026-10-02")[0]["item"] == "Week B"
    assert weekly_savoury.previous_rows(valid_from="2026-09-25")[0]["item"] == "Week A"


def test_missing_comparison_price_has_explicit_review_label():
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    assert node, "Existing Node UI harness is required"
    function = re.search(r"function wsPriceLabel\(value\)\{[^}]+\}", WEEKLY_SCRIPT).group(0)
    script = function + "console.log(JSON.stringify([null,undefined,'',0,450].map(wsPriceLabel)));"
    result = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
    assert json.loads(result.stdout) == ["Price unclear", "Price unclear", "Price unclear", "₹0", "₹450"]
