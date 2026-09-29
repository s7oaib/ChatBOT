"""Regression coverage for the durable Admin menu-category registry."""
import os

os.environ.setdefault("DB_PATH", "test_camellia.db")

import pytest
from fastapi.testclient import TestClient

import camellia.app as app_module
import camellia.database as db_module
from camellia import menu_catalogue
from camellia.database import Database


@pytest.fixture
def category_db(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "categories.db"))
    monkeypatch.setattr(db_module, "db", database)
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(menu_catalogue, "db", database)
    return database


@pytest.fixture
def category_client(category_db, monkeypatch):
    monkeypatch.setattr(app_module.config, "ADMIN_API_KEY", "category-test-key")
    monkeypatch.setattr(app_module.config, "ADMIN_MAINTENANCE_MODE", False)
    return TestClient(app_module.app), {"X-Admin-Key": "category-test-key"}


def _category_id(client, headers, name):
    categories = client.get("/admin/menu-categories", headers=headers).json()["categories"]
    return next(item["category_id"] for item in categories if item["name"] == name)


def test_seeded_categories_are_listed_and_available_to_menu_forms(category_client):
    client, headers = category_client
    response = client.get("/admin/menu-categories", headers=headers)
    assert response.status_code == 200
    assert {item["name"] for item in response.json()["categories"]} == {
        "Sondesh", "Savoury Menu", "Bento / Meal", "Subscription", "Event", "Other",
    }
    page = client.get("/admin").text
    assert "openCategoryModal()" in page and "openCategoryDeleteModal()" in page
    assert "/admin/menu-categories" in page and "refreshMenuCategoryPicker" in page


def test_create_category_is_immediate_durable_and_selectable(category_client):
    client, headers = category_client
    created = client.post("/admin/menu-categories", headers=headers, json={"name": "Festival Specials"})
    assert created.status_code == 200
    assert created.json()["category"]["name"] == "Festival Specials"
    assert _category_id(client, headers, "Festival Specials") == created.json()["category"]["category_id"]
    menu = menu_catalogue.create_menu("Festival Menu", "Festival Specials", "A seasonal customer description.")
    assert menu["category"] == "Festival Specials"


def test_rename_follows_existing_menu_without_touching_its_identity(category_client):
    client, headers = category_client
    created = client.post("/admin/menu-categories", headers=headers, json={"name": "Holiday"}).json()["category"]
    menu = menu_catalogue.create_menu("Holiday Menu", "Holiday", "A customer description.")
    renamed = client.patch(f"/admin/menu-categories/{created['category_id']}", headers=headers, json={"name": "Celebrations"})
    assert renamed.status_code == 200
    assert menu_catalogue.get_menu(menu["menu_id"])["category"] == "Celebrations"
    assert _category_id(client, headers, "Celebrations") == created["category_id"]


def test_delete_unused_category_and_reject_duplicate_or_protected_category(category_client):
    client, headers = category_client
    made = client.post("/admin/menu-categories", headers=headers, json={"name": "One Off"}).json()["category"]
    duplicate = client.post("/admin/menu-categories", headers=headers, json={"name": " one off "})
    assert duplicate.status_code == 400
    assert client.request("DELETE", f"/admin/menu-categories/{made['category_id']}", headers=headers, json={}).status_code == 200
    other_id = _category_id(client, headers, "Other")
    protected = client.request("DELETE", f"/admin/menu-categories/{other_id}", headers=headers, json={})
    assert protected.status_code == 400
    assert "protected" in protected.json()["detail"].lower()


def test_delete_in_use_requires_separate_reassignment(category_client):
    client, headers = category_client
    old = client.post("/admin/menu-categories", headers=headers, json={"name": "Temporary"}).json()["category"]
    replacement = _category_id(client, headers, "Event")
    menu = menu_catalogue.create_menu("Temporary Menu", "Temporary", "A customer description.")
    blocked = client.request("DELETE", f"/admin/menu-categories/{old['category_id']}", headers=headers, json={})
    assert blocked.status_code == 409
    assert blocked.json()["menus"][0]["menu_id"] == menu["menu_id"]
    still_blocked = client.request(
        "DELETE",
        f"/admin/menu-categories/{old['category_id']}", headers=headers,
        json={"replacement_category_id": replacement},
    )
    assert still_blocked.status_code == 409
    assert menu_catalogue.get_menu(menu["menu_id"])["category"] == "Temporary"
    moved = client.patch(f"/admin/menu-catalogue/{menu['menu_id']}", headers=headers,
                         json={"category": "Event"})
    assert moved.status_code == 200
    deleted = client.delete(f"/admin/menu-categories/{old['category_id']}", headers=headers)
    assert deleted.status_code == 200 and deleted.json()["reassigned_menus"] == 0
    assert menu_catalogue.get_menu(menu["menu_id"])["category"] == "Event"


def test_custom_creation_and_deletion_survive_reinitialization(category_db):
    created = menu_catalogue.create_category("Persistent Custom")
    same_database_after_restart = Database(category_db.db_path)
    assert same_database_after_restart.get_menu_category(created["category_id"])["name"] == "Persistent Custom"
    same_database_after_restart.delete_menu_category(created["category_id"])
    after_delete_restart = Database(category_db.db_path)
    assert after_delete_restart.get_menu_category(created["category_id"]) is None


def test_legacy_menu_category_is_migrated_to_a_durable_record(tmp_path):
    path = str(tmp_path / "legacy.db")
    database = Database(path)
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO menu_catalogue (menu_id, name, category, description) VALUES (?, ?, ?, ?)",
            ("legacy", "Legacy menu", "Legacy Category", "Existing menu"),
        )
        conn.commit()
    restarted = Database(path)
    assert restarted.find_menu_category_by_name("Legacy Category") is not None


def test_category_endpoints_require_auth_and_maintenance_still_blocks(category_client, monkeypatch):
    client, headers = category_client
    assert client.get("/admin/menu-categories").status_code == 403
    monkeypatch.setattr(app_module.config, "ADMIN_MAINTENANCE_MODE", True)
    blocked = client.get("/admin/menu-categories", headers=headers)
    assert blocked.status_code == 503
    assert "Traceback" not in blocked.text
