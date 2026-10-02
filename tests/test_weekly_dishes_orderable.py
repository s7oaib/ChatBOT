"""
Dishes from the uploaded weekly menu have to be orderable.

MEAL_PRICING_TABLE is a standing catalogue and cannot know what is on the
flyer the operator uploaded this morning. Before this, a new dish was shown
with its price in the menu text and then refused when the customer tried to
order it — and any dish the catalogue did know was priced from the catalogue,
not from the menu the customer had just been shown.
"""
import json
import copy
from datetime import date, timedelta

import pytest

from camellia.config import config
from camellia import order_calculator
from camellia.order_calculator import OrderCalculator
from camellia.responder import responder
from camellia.session_memory import session_memory


@pytest.fixture(autouse=True)
def isolated_menu(tmp_path, monkeypatch):
    from camellia import menu_dates
    from camellia.database import db
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 6, 21))
    monkeypatch.setattr(db, "db_path", str(tmp_path / "weekly-order.db"))
    db.init_db()
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(tmp_path / "menu.json"))
    # The responder captured its menu path at construction, so redirecting the
    # config alone would leave it reading the real file.
    monkeypatch.setattr(responder, "menu_path", config.MENU_JSON_PATH)
    monkeypatch.setattr(responder, "menu_data", copy.deepcopy(responder.menu_data))
    monkeypatch.setattr(order_calculator, "_weekly_cache", {"mtime": None, "table": {}})
    yield


def _write_menu(week_of, veg=None, nonveg=None):
    menu = {
        "weekly_menus": [{
            "week_of": week_of,
            "vegetarian": veg or [],
            "non_vegetarian": nonveg or [],
        }]
    }
    with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(menu, f)
    order_calculator._weekly_cache["mtime"] = None
    responder.reload_templates()


def _ordinal(day):
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


_today = date(2026, 6, 21)
_week_end = _today + timedelta(days=6)
CURRENT = (
    f"{_ordinal(_today.day)} {_today.strftime('%B')} - "
    f"{_ordinal(_week_end.day)} {_week_end.strftime('%B')}, {_week_end.year}"
)
SHUKTO = {"item": "Shukto", "description": "Bitter-sweet medley", "price_inr": 280, "quantity": "250g"}
MANGSHO = {"item": "Robibarer Mangsho", "description": "Sunday mutton", "price_inr": 640, "quantity": "200g"}


def _order(text):
    return OrderCalculator()._parse_single_clause(text)


def test_a_dish_from_this_weeks_upload_can_be_ordered():
    _write_menu(CURRENT, veg=[SHUKTO])

    item = _order("i want 2 shukto")
    assert item is not None, "a dish on this week's menu could not be ordered"
    assert item["unit_price"] == 280
    assert item["quantity"] == 2
    assert item["total_price"] == 560
    assert "Shukto" in item["flavour"]


def test_the_price_quoted_is_the_price_charged(eligible_weekly_asset):
    """
    The bento bug in another costume: the menu text is built from menu.json
    while ordering used a separate hardcoded table, so the two could disagree.
    """
    _write_menu(CURRENT, nonveg=[MANGSHO])
    # Explicit item facts require reviewed authority, not an unreviewed JSON row.
    from camellia.database import db
    row = {"item": "Robibarer Mangsho", "quantity": "200g", "price_inr": 640,
           "category": "non_vegetarian", "description": "Sunday mutton", "aliases": []}
    approval = {"rows": [row], "approved_at": "2026-06-21T10:00:00Z", "image_path": eligible_weekly_asset}
    with db.get_connection() as conn:
        conn.execute("INSERT INTO menu_versions(menu_key,snapshot_json,approval_json,is_current,valid_from,valid_until) VALUES(?,?,?,?,?,?)",
                     ("weekly_vegetarian", "[]", json.dumps(approval), 1, "2026-06-21", "2026-06-27"))
        conn.commit()

    uid = "quote_vs_charge"
    session_memory.clear_session(uid, "instagram")
    menu_text, _, _, _ = responder.match_and_respond(
        "What is the price of Robibarer Mangsho this week?", user_id=uid, channel="instagram"
    )
    assert "640" in menu_text, "menu text did not quote the uploaded price"

    item = _order("1 robibarer mangsho")
    assert item["unit_price"] == 640, (
        f"quoted ₹640 in the menu but charges ₹{item['unit_price']} at checkout"
    )


def test_an_expired_week_is_not_orderable():
    """An expired menu does not quote prices, so it must not take orders either."""
    _write_menu("19, 20, 21 JUNE", veg=[SHUKTO])
    assert order_calculator.weekly_meal_entries() == {}
    assert _order("2 shukto") is None


def test_a_new_upload_takes_effect_without_a_restart():
    _write_menu(CURRENT, veg=[SHUKTO])
    assert _order("1 shukto") is not None

    _write_menu(CURRENT, veg=[{"item": "Dhokar Dalna", "price_inr": 300, "quantity": "250g"}])
    assert _order("1 shukto") is None, "last week's dish is still orderable"
    assert _order("1 dhokar dalna")["unit_price"] == 300


def test_a_qualifier_on_the_flyer_line_does_not_have_to_be_typed():
    """Flyer lines carry qualifiers a customer will never repeat verbatim."""
    _write_menu(CURRENT, nonveg=[
        {"item": "Mangsho (Mutton Kosha)", "price_inr": 650, "quantity": "350g"},
        {"item": "Chanar Kofta / Dalna", "price_inr": 360, "quantity": "250g"},
    ])

    for phrasing in ("1 mangsho", "1 mutton kosha", "1 chanar kofta", "1 dalna"):
        item = _order(phrasing)
        assert item is not None, f"{phrasing!r} could not be ordered"


def test_spelling_of_transliterated_names_is_forgiving():
    _write_menu(CURRENT, veg=[{"item": "Chholar Daal", "price_inr": 260, "quantity": "250g"}])

    for phrasing in ("1 chholar daal", "1 chholar dal"):
        assert _order(phrasing) is not None, f"{phrasing!r} could not be ordered"


def test_the_standing_catalogue_still_answers_when_the_menu_is_unreadable():
    """A broken menu.json must not take ordering down with it."""
    with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    order_calculator._weekly_cache["mtime"] = None

    assert order_calculator.weekly_meal_entries() == {}
    item = _order("1 veg bento")
    assert item is not None and item["unit_price"] == 350


def test_the_uploaded_price_wins_over_the_standing_catalogue():
    """menu.json is what the customer was just shown."""
    _write_menu(CURRENT, nonveg=[
        {"item": "Contai Dhaba Chicken", "price_inr": 430, "quantity": "150g"},
    ])
    assert order_calculator.MEAL_PRICING_TABLE["contai dhaba chicken"]["price"] == 400

    item = _order("1 contai dhaba chicken")
    assert item["unit_price"] == 430, "charged the catalogue price, not this week's"
