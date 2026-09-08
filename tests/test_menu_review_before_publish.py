"""
Pasted text must be readable, correctable, and only then published.

Pasting used to parse and write menu.json in one press, so whatever the regex
made of a flyer became the prices customers were quoted before anyone saw it.
The read is now preview-only and the publish is a separate, validated call.
These tests hold that separation: a preview that writes anything, or a publish
that accepts a priceless row, puts wrong numbers in front of customers.
"""
import json
import re

import pytest
from fastapi.testclient import TestClient

import camellia.database as db_module
from camellia.app import app
from camellia.config import config

db_module.db.init_db()
client = TestClient(app)

TEST_ADMIN_PIN = "test_pin_98765"
config.ADMIN_PIN = TEST_ADMIN_PIN
ADMIN_HEADERS = {"x-admin-pin": TEST_ADMIN_PIN}

PASTED = """Veg:
- Shukto (250g) - 280
- Chholar Daal (250g) - 250
Non-Veg:
- Kosha Mangsho (350g) - 650
"""


@pytest.fixture
def menu_restored():
    """Every test here writes the live menu; put it back exactly as it was."""
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        original = f.read()
    yield
    with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
        f.write(original)


def _live_week():
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["weekly_menus"][-1]


def test_preview_reads_dishes_without_touching_the_live_menu():
    """The whole point of the split: reading must cost the customer nothing."""
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        before = f.read()

    resp = client.post(
        "/admin/menu/preview-text", json={"raw_text": PASTED}, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"

    names = [d["item"] for d in data["parsed"]["vegetarian"] + data["parsed"]["non_vegetarian"]]
    assert any("Shukto" in n for n in names), names
    assert any("Mangsho" in n for n in names), names

    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        assert f.read() == before, "preview wrote to menu.json"


def test_preview_says_so_when_it_finds_no_priced_dish():
    """Silence would look like success and leave the client waiting."""
    resp = client.post(
        "/admin/menu/preview-text",
        json={"raw_text": "just some words with no prices at all"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"


def test_preview_and_publish_keep_bento_items_in_their_own_category(menu_restored):
    pasted = """Bento Menu:
- Vegetarian Bengali Bento (1 box) - ₹350
- Chicken Bengali Bento (1 box) - ₹400
"""
    preview = client.post(
        "/admin/menu/preview-text", json={"raw_text": pasted}, headers=ADMIN_HEADERS
    )
    assert preview.status_code == 200
    bentos = preview.json()["parsed"]["bengali_bento"]
    assert [item["price_inr"] for item in bentos] == [350, 400]

    published = client.post(
        "/admin/update-menu",
        json={"bengali_bento": bentos},
        headers=ADMIN_HEADERS,
    )
    assert published.status_code == 200
    assert [item["price_inr"] for item in _live_week()["bengali_bento"]] == [350, 400]


def test_preview_requires_admin():
    """Menu content is not public, and neither is the parser."""
    resp = client.post("/admin/menu/preview-text", json={"raw_text": PASTED})
    assert resp.status_code in (401, 403)


def test_publishing_the_reviewed_rows_replaces_the_live_menu(menu_restored):
    """The edit the client makes in the table is what customers get."""
    resp = client.post(
        "/admin/update-menu",
        json={
            "vegetarian": [
                {"item": "Shukto", "description": "Bitter medley", "quantity": "250g", "price_inr": 280}
            ],
            "non_vegetarian": [{"item": "Kosha Mangsho", "quantity": "350g", "price_inr": 600}],
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"

    week = _live_week()
    assert [d["item"] for d in week["vegetarian"]] == ["Shukto"]
    assert week["non_vegetarian"][0]["price_inr"] == 600


def test_a_row_with_no_price_is_refused_by_name(menu_restored):
    """
    Dropping the row instead would report success while the dish never
    published — the client would find out from a customer.
    """
    before = _live_week()
    resp = client.post(
        "/admin/update-menu",
        json={"vegetarian": [{"item": "Mystery Dish", "price_inr": ""}]},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400
    assert "Mystery Dish" in resp.json()["detail"]
    assert _live_week() == before, "a refused publish still changed the menu"


def test_a_nameless_or_free_dish_is_refused(menu_restored):
    """A blank name and a zero price are both typos, not menu items."""
    nameless = client.post(
        "/admin/update-menu",
        json={"vegetarian": [{"item": "   ", "price_inr": 300}]},
        headers=ADMIN_HEADERS,
    )
    assert nameless.status_code == 400

    free = client.post(
        "/admin/update-menu",
        json={"vegetarian": [{"item": "Free Sondesh", "price_inr": 0}]},
        headers=ADMIN_HEADERS,
    )
    assert free.status_code == 400
    assert "Free Sondesh" in free.json()["detail"]


def test_a_sparse_dish_gains_no_empty_fields(menu_restored):
    """
    A hand-added row leaves description and size blank. Storing "" would print
    an empty bracket in the menu card the customer reads.
    """
    resp = client.post(
        "/admin/update-menu",
        json={"vegetarian": [{"item": "Plain Rice", "price_inr": 150, "description": "", "quantity": ""}]},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    stored = _live_week()["vegetarian"][0]
    assert stored == {"item": "Plain Rice", "price_inr": 150}


def test_the_dashboard_offers_review_before_publish():
    """
    The controls that make the flow safe. Without the Publish button the parse
    result is unreachable; without Discard the client cannot back out.
    """
    page = client.get("/admin").text
    assert "/admin/menu/preview-text" in page, "paste still calls the instant-publish endpoint"
    assert "publishMenuReview" in page and "discardMenuReview" in page
    assert "deleteReviewRow" in page and "addReviewRow" in page
    assert 'id="menuReviewBody"' in page


def test_writing_an_answer_sits_above_the_list_of_answers():
    """
    The add form was a collapsed panel below the answer list, and the client
    never found it. It must be its own card, ahead of the list.
    """
    page = client.get("/admin").text
    order = re.findall(r'<div id="(tab-[\w-]+)" class="panel-card"', page)
    assert "tab-write" in order, "the write-an-answer panel is missing"
    assert order.index("tab-write") < order.index("tab-knowledge")

    assert "toggleAddKnowledgeForm" not in page, "the form is still hidden behind a toggle"
    assert 'id="addKnowledgeForm"' not in page


def test_a_live_dish_can_be_removed_one_at_a_time(menu_restored):
    """
    Dropping one sold-out dish used to mean re-pasting the whole menu or
    pressing Reset to Baseline, which throws the week away. Removing one must
    leave the rest exactly as they were.
    """
    client.post(
        "/admin/update-menu",
        json={
            "vegetarian": [
                {"item": "Shukto", "price_inr": 280},
                {"item": "Chholar Daal", "price_inr": 250},
            ],
            "non_vegetarian": [{"item": "Kosha Mangsho", "price_inr": 600}],
        },
        headers=ADMIN_HEADERS,
    )

    # What the Remove button sends: every surviving dish, minus the one dropped.
    resp = client.post(
        "/admin/update-menu",
        json={
            "vegetarian": [{"item": "Shukto", "price_inr": 280}],
            "non_vegetarian": [{"item": "Kosha Mangsho", "price_inr": 600}],
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200

    week = _live_week()
    assert [d["item"] for d in week["vegetarian"]] == ["Shukto"]
    assert [d["item"] for d in week["non_vegetarian"]] == ["Kosha Mangsho"]


def test_removing_the_last_dish_is_allowed_but_empties_the_menu(menu_restored):
    """
    The dashboard warns before this, but the endpoint must still accept it —
    refusing would strand a client whose whole week really is sold out.
    """
    resp = client.post(
        "/admin/update-menu",
        json={"vegetarian": [], "non_vegetarian": []},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    week = _live_week()
    assert week["vegetarian"] == [] and week["non_vegetarian"] == []


def test_the_live_menu_offers_a_remove_button_per_dish():
    """Both lists, or half the menu is undeletable."""
    page = client.get("/admin").text
    assert "removeLiveDish('vegetarian'" in page
    assert "removeLiveDish('non_vegetarian'" in page
    assert "async function removeLiveDish" in page
    # It must send what survives, not just the deletion.
    handler = page[page.index("async function removeLiveDish"):]
    handler = handler[:handler.index("async function confirmResetDefaults")]
    assert "/admin/update-menu" in handler
    assert "splice" in handler and "confirm(" in handler
    assert "last dish" in handler, "no warning before emptying the whole menu"
