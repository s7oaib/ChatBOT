"""
Bento Meals had no way to be managed from the "what the bot is serving right
now" tab: liveMenu only ever loaded vegetarian and non_vegetarian, so the
Remove button's own payload silently sent bengali_bento nowhere, and there was
no Add capability for any category, bento or otherwise, outside the
paste-and-parse flow. Bento is its own confirmed product line (₹350/₹400/₹600)
and deserves the same visibility and control as the à la carte dishes.

These pin the backend contract the new admin buttons rely on: adding and
removing a bengali_bento dish through /admin/update-menu, exactly as the
Active Menu tab's JS now does, round-trips correctly and leaves the other
two categories untouched.

Every test here writes to the real config.MENU_JSON_PATH — the same file the
live bot reads — because that is the pattern the rest of this test file
already established (see test_parse_menu_card_text, test_reset_menu_defaults).
Each test reads the file's raw bytes before it touches anything and restores
them in a `finally`, so a failure mid-test still leaves the real menu exactly
as it was found rather than mutated with test placeholder dishes.
"""

import json

import pytest
from fastapi.testclient import TestClient

from camellia.app import app
from camellia.config import config
from camellia.responder import responder

client = TestClient(app)
TEST_ADMIN_PIN = "test_pin_bento_98765"
ADMIN_HEADERS = {"x-admin-pin": TEST_ADMIN_PIN}


@pytest.fixture(autouse=True)
def _admin_pin_set():
    # Set per-test rather than at import time: another test module also
    # assigns config.ADMIN_PIN at its own import time, and whichever module
    # collection happens to import last silently wins for the whole run,
    # turning every ADMIN_HEADERS call here into a 403.
    original = config.ADMIN_PIN
    config.ADMIN_PIN = TEST_ADMIN_PIN
    yield
    config.ADMIN_PIN = original


@pytest.fixture()
def preserved_menu_file():
    """
    Reads the real menu.json's raw bytes before a test runs and restores them
    exactly afterward, success or failure. This is the same pattern
    test_parse_menu_card_text and test_reset_menu_defaults already use in
    this suite — no isolated tmp path exists for this file, so restoring the
    exact bytes is what keeps a test run from leaving the real weekly menu
    mutated with dishes like "Test Kitchen Special Bento".
    """
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        original_bytes = f.read()
    try:
        yield
    finally:
        with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
            f.write(original_bytes)
        responder.reload_templates()


def _current_week():
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["weekly_menus"][-1]


def _update_menu(veg, nonveg, bento):
    resp = client.post(
        "/admin/update-menu",
        json={"vegetarian": veg, "non_vegetarian": nonveg, "bengali_bento": bento},
        headers=ADMIN_HEADERS,
    )
    return resp


class TestAddingABentoDish:
    def test_a_new_bento_box_is_added_alongside_existing_ones(self, preserved_menu_file):
        starting = _current_week()
        resp = _update_menu(
            starting.get("vegetarian", []),
            starting.get("non_vegetarian", []),
            [
                {"item": "Vegetarian Bento", "price_inr": 350},
                {"item": "Mangsho Bento", "description": "Slow-cooked mutton, luchi", "quantity": "1 Bento Box", "price_inr": 600},
            ],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "success"

        names = [d["item"] for d in _current_week()["bengali_bento"]]
        assert "Vegetarian Bento" in names
        assert "Mangsho Bento" in names

    def test_adding_bento_does_not_touch_vegetarian_or_non_vegetarian(self, preserved_menu_file):
        # Seeded rather than reusing the pre-existing bengali_bento block: the
        # live data there is legacy shape ({"name", "includes"}, no "item"),
        # which _reviewed_menu_items rightly refuses — that mismatch is a
        # separate, real finding, not something to route around here.
        starting = _current_week()
        veg_before = starting.get("vegetarian", [])
        nonveg_before = starting.get("non_vegetarian", [])
        seed = _update_menu(veg_before, nonveg_before, [{"item": "Vegetarian Bento", "price_inr": 350}])
        assert seed.status_code == 200

        resp = _update_menu(
            veg_before,
            nonveg_before,
            _current_week()["bengali_bento"] + [{"item": "Chicken Bento", "price_inr": 400}],
        )
        assert resp.status_code == 200

        week = _current_week()
        assert week["vegetarian"] == veg_before
        assert week["non_vegetarian"] == nonveg_before

    def test_a_bento_box_with_no_price_is_refused_not_silently_dropped(self, preserved_menu_file):
        starting = _current_week()
        resp = _update_menu(
            starting.get("vegetarian", []),
            starting.get("non_vegetarian", []),
            [{"item": "Mystery Bento", "price_inr": None}],
        )
        assert resp.status_code == 400
        assert "no readable price" in resp.json()["detail"]


class TestTheLiveBentoDataMatchesWhatCustomersAreToldAndCanRoundTrip:
    """
    menu.json's bengali_bento block used to be {"name", "includes"} at Veg
    ₹400 / Non-Veg ₹700 — a different shape from every other category, and
    different prices from what bento_details actually answers customers
    (Vegetarian ₹350, Chicken ₹400, Mangsho ₹600, client-confirmed). Anyone
    clicking the new Remove button on one of those old rows would have hit a
    validation error immediately, because the *other* untouched row in the
    same list still had no "item" key. Migrated to the {item, description,
    quantity, price_inr} shape so the Active Menu tab can show and edit these
    rows without that trap, and so the two numbers agree with each other.
    """

    def test_the_stored_rows_use_the_same_shape_as_every_other_category(self):
        with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
            week = json.load(f)["weekly_menus"][-1]
        bento = week.get("bengali_bento", [])

        assert bento
        for row in bento:
            assert "item" in row and row["item"], row
            assert isinstance(row["price_inr"], (int, float))

    def test_the_stored_prices_agree_with_what_bento_details_tells_customers(self):
        with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
            week = json.load(f)["weekly_menus"][-1]
        prices = {row["item"]: row["price_inr"] for row in week.get("bengali_bento", [])}

        assert prices.get("Vegetarian Bento") == 350
        assert prices.get("Chicken Bento") == 400
        assert prices.get("Mangsho Bento") == 600

    def test_removing_one_real_row_no_longer_fails_on_the_row_left_behind(self, preserved_menu_file):
        # This is the exact click a real admin would make first: Remove on
        # one of the rows that used to be legacy-shaped, leaving the other
        # two to round-trip through the same request.
        week = _current_week()
        bento = week["bengali_bento"][:]
        bento.pop(0)

        resp = _update_menu(week["vegetarian"], week["non_vegetarian"], bento)
        assert resp.status_code == 200, resp.text

        names = [d["item"] for d in _current_week()["bengali_bento"]]
        assert "Vegetarian Bento" not in names
        assert "Chicken Bento" in names
        assert "Mangsho Bento" in names


class TestRemovingABentoDish:
    def test_removing_one_bento_dish_keeps_the_others(self, preserved_menu_file):
        starting = _current_week()
        setup = _update_menu(
            starting.get("vegetarian", []),
            starting.get("non_vegetarian", []),
            [
                {"item": "Vegetarian Bento", "price_inr": 350},
                {"item": "Chicken Bento", "price_inr": 400},
            ],
        )
        assert setup.status_code == 200

        week = _current_week()
        remaining = week["bengali_bento"][:]
        remaining.pop(0)
        resp = _update_menu(week["vegetarian"], week["non_vegetarian"], remaining)
        assert resp.status_code == 200

        names = [d["item"] for d in _current_week()["bengali_bento"]]
        assert names == ["Chicken Bento"]


class TestTheBentoAddedDishReachesTheBot:
    def test_the_responder_reloads_after_a_bento_write(self, preserved_menu_file):
        starting = _current_week()
        resp = _update_menu(
            starting.get("vegetarian", []),
            starting.get("non_vegetarian", []),
            [{"item": "Test Kitchen Special Bento", "description": "A test dish", "quantity": "1 Bento Box", "price_inr": 555}],
        )
        assert resp.status_code == 200

        # bento_details answers with the client-confirmed fixed tiers rather
        # than the live JSON, so this only proves the write landed and the
        # bot's own responder can still answer afterward — not that this
        # specific price appears verbatim in a reply.
        text, matched, template_id, _ = responder.match_and_respond(
            "mutton bento", user_id="bento_admin_probe", channel="whatsapp"
        )
        assert matched is True


class TestTheActiveMenuAdminPageOffersBothAddButtons:
    """Read-only checks against the dashboard's own HTML — no menu.json write."""

    def test_the_dashboard_html_has_a_separate_button_for_each_category(self):
        from camellia.app import get_admin_dashboard

        page = get_admin_dashboard().body.decode("utf-8")

        assert "addLiveSavouryDish()" in page
        assert "addLiveBentoDish()" in page
        assert "+ Add Savoury Dish" in page
        assert "+ Add Bento Meal Box" in page

    def test_the_active_menu_view_renders_a_bento_section(self):
        from camellia.app import get_admin_dashboard

        page = get_admin_dashboard().body.decode("utf-8")

        assert "Bento Meals" in page
        assert "removeLiveDish('bengali_bento'" in page
