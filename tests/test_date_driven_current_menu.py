from datetime import date, datetime, timezone
import json

from fastapi.testclient import TestClient

from camellia import menu_dates
from camellia.config import config
from camellia.app import app
from camellia.responder import DeterministicResponder


def _dated(start, end, label="Thursday-Friday", **extra):
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "service_window": label,
        "vegetarian": [{"item": extra.pop("item", "Dish"), "price_inr": 100}],
        **extra,
    }


def test_date_range_includes_today_and_boundaries_are_inclusive():
    today = date(2026, 9, 21)
    menu = _dated(date(2026, 9, 21), date(2026, 9, 24))
    assert menu_dates.menu_is_active(menu, today)
    assert menu_dates.menu_is_active(menu, date(2026, 9, 24))
    assert not menu_dates.menu_is_active(menu, date(2026, 9, 20))
    assert not menu_dates.menu_is_active(menu, date(2026, 9, 25))


def test_same_weekday_expired_and_future_ranges_are_inactive():
    today = date(2026, 9, 21)
    expired = _dated(date(2026, 9, 17), date(2026, 9, 18))
    future = _dated(date(2026, 9, 24), date(2026, 9, 25))
    assert not menu_dates.menu_is_active(expired, today)
    assert not menu_dates.menu_is_active(future, today)


def test_single_date_is_active_only_on_that_date():
    menu = {"start_date": "2026-09-21", "item": "One Day"}
    assert menu_dates.parse_menu_date_range(menu) == (date(2026, 9, 21), date(2026, 9, 21))
    assert menu_dates.menu_is_active(menu, date(2026, 9, 21))
    assert not menu_dates.menu_is_active(menu, date(2026, 9, 22))


def test_legacy_week_of_range_is_supported_without_using_weekday_text():
    today = date(2026, 9, 21)
    menu = {"week_of": "20th September - 24th September, 2026", "service_window": "Thursday-Friday"}
    assert menu_dates.parse_menu_date_range(menu, today) == (date(2026, 9, 20), date(2026, 9, 24))
    assert menu_dates.menu_is_active(menu, today)
    assert not menu_dates.menu_is_active({"week_of": "Thursday-Friday"}, today)


def test_overlapping_ranges_choose_the_later_authoritative_entry():
    menus = [
        _dated(date(2026, 9, 20), date(2026, 9, 24), item="Earlier"),
        _dated(date(2026, 9, 21), date(2026, 9, 22), item="Later"),
    ]
    result = menu_dates.resolve_current_menu(menus, date(2026, 9, 21))
    assert result["status"] == "active"
    assert result["index"] == 1
    assert result["menu"]["vegetarian"][0]["item"] == "Later"


def test_invalid_or_missing_dates_are_safe_and_do_not_become_current():
    result = menu_dates.resolve_current_menu(
        [{"service_window": "Thursday-Friday", "vegetarian": [{"item": "Unsafe"}]}],
        date(2026, 9, 21),
    )
    assert result["menu"] is None
    assert result["status"] == "invalid_date"
    assert menu_dates.parse_menu_date_range({"start_date": "not-a-date"}) is None


def test_timezone_boundary_uses_asia_kolkata_business_date():
    just_before_ist_midnight = datetime(2026, 9, 20, 18, 29, tzinfo=timezone.utc)
    just_after_ist_midnight = datetime(2026, 9, 20, 18, 30, tzinfo=timezone.utc)
    assert menu_dates.business_today(just_before_ist_midnight) == date(2026, 9, 20)
    assert menu_dates.business_today(just_after_ist_midnight) == date(2026, 9, 21)


def test_admin_current_menu_endpoint_returns_date_selected_row(tmp_path, monkeypatch):
    menu_path = tmp_path / "menu.json"
    menu_path.write_text(json.dumps({"weekly_menus": [
        _dated(date(2026, 9, 20), date(2026, 9, 20), item="Expired"),
        _dated(date(2026, 9, 21), date(2026, 9, 24), item="Today"),
    ]}), encoding="utf-8")
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(menu_path))
    monkeypatch.setattr(config, "ADMIN_API_KEY", "date-menu-test-key")
    body = TestClient(app).get("/admin/menu/active", headers={"x-admin-key": "date-menu-test-key"}).json()
    assert body["current_menu"]["vegetarian"][0]["item"] == "Today"
    assert body["current_menu_status"] == "active"
    assert body["current_date_range"] == {"start_date": "2026-09-21", "end_date": "2026-09-24"}


def test_admin_update_menu_persists_explicit_date_range(tmp_path, monkeypatch):
    menu_path = tmp_path / "menu.json"
    menu_path.write_text(json.dumps({"weekly_menus": [{"week_of": "Thursday-Friday", "vegetarian": []}]}), encoding="utf-8")
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(menu_path))
    monkeypatch.setattr(config, "ADMIN_API_KEY", "date-menu-test-key")
    response = TestClient(app).post(
        "/admin/update-menu",
        headers={"x-admin-key": "date-menu-test-key"},
        json={
            "start_date": "2026-09-21",
            "end_date": "2026-09-24",
            "vegetarian": [{"item": "Dated dish", "price_inr": 100}],
        },
    )
    assert response.status_code == 200
    saved = json.loads(menu_path.read_text(encoding="utf-8"))["weekly_menus"][0]
    assert saved["start_date"] == "2026-09-21"
    assert saved["end_date"] == "2026-09-24"


def test_admin_update_menu_rejects_invalid_explicit_date_range(tmp_path, monkeypatch):
    menu_path = tmp_path / "menu.json"
    menu_path.write_text(json.dumps({"weekly_menus": [{"vegetarian": []}]}), encoding="utf-8")
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(menu_path))
    monkeypatch.setattr(config, "ADMIN_API_KEY", "date-menu-test-key")
    response = TestClient(app).post(
        "/admin/update-menu",
        headers={"x-admin-key": "date-menu-test-key"},
        json={"start_date": "not-a-date", "end_date": "2026-09-24", "vegetarian": []},
    )
    assert response.status_code == 400


def test_chatbot_current_menu_asset_uses_date_selected_menu(monkeypatch):
    import camellia.responder as responder_module

    bot = DeterministicResponder.__new__(DeterministicResponder)
    bot.menu_data = {"weekly_menus": [_dated(date(2026, 9, 21), date(2026, 9, 24))]}
    monkeypatch.setattr(responder_module.db, "get_menu_image_current", lambda key: {"image_path": "/dated/menu.png"})
    monkeypatch.setattr(responder_module.db, "has_menu_image_archive", lambda key: False)
    assert bot._current_weekly_image(today_weekday=0)[0] == "/dated/menu.png"


def test_weekday_argument_cannot_change_date_selected_asset(monkeypatch):
    import camellia.responder as responder_module

    bot = DeterministicResponder.__new__(DeterministicResponder)
    bot.menu_data = {"weekly_menus": [_dated(date(2026, 9, 21), date(2026, 9, 24))]}
    monkeypatch.setattr(responder_module.db, "get_menu_image_current", lambda key: None)
    monkeypatch.setattr(responder_module.db, "has_menu_image_archive", lambda key: False)
    first = bot._current_weekly_image(today_weekday=0)
    second = bot._current_weekly_image(today_weekday=4)
    assert first == second


def test_expired_or_undated_flyer_cannot_block_valid_menu_asset(monkeypatch):
    import camellia.responder as responder_module

    bot = DeterministicResponder.__new__(DeterministicResponder)
    bot.menu_data = {"weekly_menus": [_dated(
        date(2026, 9, 21), date(2026, 9, 24),
        menu_image_from_friday="/dated/menu.png",
    )]}
    monkeypatch.setattr(responder_module.db, "get_menu_image_current", lambda key: None)
    monkeypatch.setattr(responder_module.db, "has_menu_image_archive", lambda key: False)
    monkeypatch.setattr(responder_module.weekly_flyer, "get", lambda: {"week_of": "Thursday-Friday"})
    assert bot._current_weekly_image(today_weekday=0)[0] == "/dated/menu.png"
