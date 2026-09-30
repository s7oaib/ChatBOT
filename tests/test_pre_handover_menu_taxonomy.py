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
    with pytest.raises(menu_catalogue.MenuCatalogueError, match="Unlink menus"):
        menu_catalogue.update_customer_topic("SAVOURY", "Wrong family", "SONDESH")
    assert menu_catalogue.routing_key_for_topic("SAVOURY") == "SAVOURY"


def test_taxonomy_reads_preserve_legacy_catalogue_and_category_rows(taxonomy_env):
    with db.get_connection() as conn:
        before = [tuple(row) for row in conn.execute(
            "SELECT menu_id,category,updated_at FROM menu_catalogue "
            "WHERE menu_id IN ('weekly_vegetarian','weekly_non_vegetarian') ORDER BY menu_id"
        )]
        old_categories = [tuple(row) for row in conn.execute(
            "SELECT category_id,name FROM menu_categories "
            "WHERE name IN ('Weekly Vegetarian','Weekly Non-Vegetarian') ORDER BY name"
        )]
    assert old_categories
    assert menu_catalogue.get_menu("weekly_vegetarian")["category"] == "Savoury Menu"
    assert "Weekly Vegetarian" not in [row["name"] for row in menu_catalogue.list_categories()]
    menu_catalogue.list_menus()
    db.init_db()
    with db.get_connection() as conn:
        after = [tuple(row) for row in conn.execute(
            "SELECT menu_id,category,updated_at FROM menu_catalogue "
            "WHERE menu_id IN ('weekly_vegetarian','weekly_non_vegetarian') ORDER BY menu_id"
        )]
        retained_categories = [tuple(row) for row in conn.execute(
            "SELECT category_id,name FROM menu_categories "
            "WHERE name IN ('Weekly Vegetarian','Weekly Non-Vegetarian') ORDER BY name"
        )]
    assert after == before
    assert retained_categories == old_categories


def test_savoury_umbrella_cannot_be_renamed_or_deleted(taxonomy_env):
    category = next(row for row in menu_catalogue.list_categories() if row["name"] == "Savoury Menu")
    with pytest.raises(menu_catalogue.MenuCatalogueError, match="protected weekly menu umbrella"):
        menu_catalogue.update_category(category["category_id"], "Something else")
    with pytest.raises(menu_catalogue.MenuCatalogueError, match="protected weekly menu umbrella"):
        menu_catalogue.delete_category(category["category_id"])
    with pytest.raises(menu_catalogue.MenuCatalogueError, match="Use Savoury Menu"):
        menu_catalogue.create_category("Weekly Vegetarian")
    created = menu_catalogue.create_menu("Weekend savoury", "Weekly Non-Vegetarian", "Weekend menu.")
    assert created["category"] == "Savoury Menu"


def test_savoury_workflow_opens_from_its_catalogue_card(taxonomy_env):
    page = TestClient(app).get("/admin").text
    assert '<div id="mrWeeklyView">' in page
    assert "catalogueRows.forEach(menu=>" in page
    assert "menu.workspace==='weekly'?mrOpenWeekly()" not in page
    assert "catalogueRows=mrWorkspaceRows.filter(menu=>menu.workspace!=='weekly')" in page
    assert "if(weekly)weekly.hidden=true" in page
    assert "← Menu Catalogue" in page


def test_used_custom_topic_cannot_change_routing_family(taxonomy_env):
    topic = menu_catalogue.create_customer_topic("Party Savoury", "SAVOURY")
    menu_catalogue.create_menu("Party Trays", "Savoury Menu", "Party savoury trays.",
                               customer_topic=topic["topic_id"])
    with pytest.raises(menu_catalogue.MenuCatalogueError, match="Unlink menus"):
        menu_catalogue.update_customer_topic(topic["topic_id"], "Party Savoury", "SONDESH")
    assert menu_catalogue.routing_key_for_topic(topic["topic_id"]) == "SAVOURY"
