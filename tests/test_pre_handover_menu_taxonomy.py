"""Final operator taxonomy: one Savoury umbrella and managed message topics."""

import shutil

import pytest
from fastapi.testclient import TestClient

from camellia import menu_catalogue
from camellia.app import app
from camellia.config import config
from camellia.database import db


@pytest.fixture
def taxonomy_env(tmp_path, monkeypatch):
    menu = tmp_path / "menu.json"
    shutil.copy(config.MENU_JSON_PATH, menu)
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(menu))
    monkeypatch.setattr(db, "db_path", str(tmp_path / "taxonomy.db"))
    db.init_db()
    return menu


def test_weekly_veg_and_nonveg_are_one_savoury_menu_category(taxonomy_env):
    names = [row["name"] for row in menu_catalogue.list_categories()]
    assert "Savoury Menu" in names
    assert "Weekly Vegetarian" not in names
    assert "Weekly Non-Vegetarian" not in names
    assert menu_catalogue.get_menu("weekly_vegetarian")["category"] == "Savoury Menu"
    assert menu_catalogue.get_menu("weekly_non_vegetarian")["category"] == "Savoury Menu"
    summary = menu_catalogue.workspace_summary(menu_catalogue.list_menus())
    weekly = next(row for row in summary["workspace_rows"] if row["workspace"] == "weekly")
    assert weekly["category"] == "Savoury Menu"


def test_customer_topic_crud_is_independent_of_admin_category(taxonomy_env):
    topic = menu_catalogue.create_customer_topic("Party Savoury", "SAVOURY")
    assert topic["routing_key"] == "SAVOURY"
    menu = menu_catalogue.create_menu(
        "Party Trays", "Savoury Menu", "Party savoury trays.", customer_topic=topic["topic_id"]
    )
    assert menu["category"] == "Savoury Menu"
    assert menu["customer_topic"] == topic["topic_id"]

    updated = menu_catalogue.update_customer_topic(topic["topic_id"], "Event Savoury", "SAVOURY")
    assert updated["name"] == "Event Savoury"
    assert menu_catalogue.get_menu(menu["menu_id"])["category"] == "Savoury Menu"

    with pytest.raises(menu_catalogue.MenuCatalogueError, match="use this topic"):
        menu_catalogue.delete_customer_topic(topic["topic_id"])

    menu_catalogue.update_menu(menu["menu_id"], customer_topic="", update_topic=True)
    assert menu_catalogue.delete_customer_topic(topic["topic_id"])["deleted"] is True


def test_custom_topic_maps_to_existing_semantic_family_without_new_router(taxonomy_env):
    topic = menu_catalogue.create_customer_topic("Celebration Sweets", "SONDESH")
    assert menu_catalogue.routing_key_for_topic(topic["topic_id"]) == "SONDESH"
    assert topic["topic_id"] not in menu_catalogue.CUSTOMER_TOPICS
    assert "SONDESH" in menu_catalogue.CUSTOMER_TOPICS


def test_customer_topic_admin_routes_require_auth_and_support_crud(taxonomy_env):
    client = TestClient(app)
    assert client.get("/admin/weekly-savoury/customer-topics").status_code in (401, 403)
    headers = {"X-Admin-Key": config.ADMIN_API_KEY}
    listing = client.get("/admin/weekly-savoury/customer-topics", headers=headers)
    assert listing.status_code == 200
    assert any(item["topic_id"] == "SAVOURY" for item in listing.json()["topics"])

    created = client.post(
        "/admin/weekly-savoury/customer-topics",
        json={"name": "Savoury Events", "routing_key": "SAVOURY"},
        headers=headers,
    )
    assert created.status_code == 200
    topic_id = created.json()["topic"]["topic_id"]
    edited = client.patch(
        f"/admin/weekly-savoury/customer-topics/{topic_id}",
        json={"name": "Savoury Occasions", "routing_key": "SAVOURY"},
        headers=headers,
    )
    assert edited.status_code == 200
    assert edited.json()["topic"]["name"] == "Savoury Occasions"
    deleted = client.delete(f"/admin/weekly-savoury/customer-topics/{topic_id}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_core_topic_can_be_renamed_without_changing_its_routing_key(taxonomy_env):
    updated = menu_catalogue.update_customer_topic("SAVOURY", "Savoury Menu", "SAVOURY")
    assert updated["name"] == "Savoury Menu"
    assert updated["routing_key"] == "SAVOURY"
    assert menu_catalogue.clean_customer_topic("SAVOURY") == "SAVOURY"
