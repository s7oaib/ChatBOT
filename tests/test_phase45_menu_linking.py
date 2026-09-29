"""Phase 4.5: menu organisation is separate from customer authority."""

import json
import io
import shutil
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from camellia import app as app_module, menu_catalogue, menu_dates, menu_review
from camellia.config import config
from camellia.database import db
from camellia.responder import responder
from camellia.session_memory import session_memory


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    menu_path = tmp_path / "menu.json"
    shutil.copy(config.MENU_JSON_PATH, menu_path)
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(menu_path))
    monkeypatch.setattr(db, "db_path", str(tmp_path / "phase45.db"))
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    db.init_db()
    return TestClient(app_module.app), {"X-Admin-Key": config.ADMIN_API_KEY}


def _publish(menu_id, image_url=None, start=None, end=None, state=None):
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO menu_versions(menu_key,snapshot_json,approval_json,is_current,valid_from,valid_until,weekly_state) "
            "VALUES(?,?,?,?,?,?,?)",
            (menu_id, "{}", json.dumps({"rows": [], "approved_at": "2026-09-25T10:00:00Z",
                                        "image_path": image_url or f"/uploads/{menu_id}.jpg"}),
             1, start, end, state),
        )
        conn.commit()


def _create(client, headers, name, topic=""):
    response = client.post("/admin/menu-catalogue", headers=headers, data={
        "name": name, "category": "Other", "description": "Review-only menu",
        "customer_topic": topic,
    })
    assert response.status_code == 200, response.text
    return response.json()["menu"]


def test_referenced_category_cannot_be_deleted_even_with_replacement(workspace):
    client, headers = workspace
    source = client.post("/admin/menu-categories", headers=headers, json={"name": "Seasonal"}).json()["category"]
    replacement = client.post("/admin/menu-categories", headers=headers, json={"name": "Gift"}).json()["category"]
    created = _create(client, headers, "Seasonal menu")
    updated = client.patch(f"/admin/menu-catalogue/{created['menu_id']}", headers=headers,
                           json={"category": "Seasonal", "customer_topic": "SONDESH"})
    assert updated.status_code == 200
    blocked = client.request("DELETE", f"/admin/menu-categories/{source['category_id']}", headers=headers,
                             json={"replacement_category_id": replacement["category_id"]})
    assert blocked.status_code == 409
    assert "Move those menus" in blocked.json()["detail"]
    renamed = client.patch(f"/admin/menu-categories/{source['category_id']}", headers=headers,
                           json={"name": "Seasonal specials"})
    assert renamed.status_code == 200
    menu = menu_catalogue.get_menu(created["menu_id"])
    assert menu["category"] == "Seasonal specials"
    assert menu["customer_topic"] == "SONDESH"
    db.init_db()
    assert menu_catalogue.get_menu(created["menu_id"])["category"] == "Seasonal specials"
    assert menu_catalogue.list_categories() and menu_catalogue.category_usage(source["category_id"])["category"]["name"] == "Seasonal specials"
    client.patch(f"/admin/menu-catalogue/{created['menu_id']}", headers=headers, json={"category": "Gift"})
    assert client.delete(f"/admin/menu-categories/{source['category_id']}", headers=headers).status_code == 200


def test_topic_is_allowlisted_and_new_menu_is_unlinked_by_default(workspace):
    client, headers = workspace
    plain = _create(client, headers, "Unlinked menu")
    assert plain["customer_topic"] is None
    assert plain["menu_id"] not in {menu["menu_id"] for menu in menu_catalogue.eligible_topic_menus("SONDESH")}
    _publish(plain["menu_id"])
    assert plain["menu_id"] not in {version["menu_key"] for version in menu_review.customer_versions()}
    assert plain["menu_id"] not in menu_review.canonical_menu().get("collections", {})
    invalid = client.patch(f"/admin/menu-catalogue/{plain['menu_id']}", headers=headers,
                           json={"customer_topic": "arbitrary customer instruction"})
    assert invalid.status_code == 400
    assert menu_catalogue.get_menu(plain["menu_id"])["customer_topic"] is None
    assert client.patch(f"/admin/menu-catalogue/{plain['menu_id']}", headers=headers,
                        json={"customer_topic": "SONDESH"}).status_code == 200
    assert plain["menu_id"] in {version["menu_key"] for version in menu_review.customer_versions()}
    assert client.patch(f"/admin/menu-catalogue/{plain['menu_id']}", headers=headers,
                        json={"customer_topic": ""}).status_code == 200
    assert plain["menu_id"] not in {version["menu_key"] for version in menu_review.customer_versions()}


def test_reply_order_is_stable_and_authority_filtered(workspace):
    client, headers = workspace
    first = _create(client, headers, "Wedding collection", "SONDESH")
    second = _create(client, headers, "Festival collection", "SONDESH")
    assert menu_catalogue.eligible_topic_menus("SONDESH")  # standing built-ins
    assert first["menu_id"] not in {menu["menu_id"] for menu in menu_catalogue.eligible_topic_menus("SONDESH")}
    _publish(first["menu_id"])
    _publish(second["menu_id"])
    assert client.post(f"/admin/menu-catalogue/{second['menu_id']}/move", headers=headers,
                       json={"position": 1}).status_code == 200
    assert client.post(f"/admin/menu-catalogue/{first['menu_id']}/move", headers=headers,
                       json={"position": 2}).status_code == 200
    ids = [menu["menu_id"] for menu in menu_catalogue.eligible_topic_menus("SONDESH")]
    assert ids[:2] == [second["menu_id"], first["menu_id"]]
    assert db.get_menu_catalogue_entry(second["menu_id"])["display_order"] == 10
    assert client.post(f"/admin/menu-catalogue/{second['menu_id']}/move", headers=headers,
                       json={"position": 4}).status_code == 200
    assert [menu["menu_id"] for menu in menu_catalogue.eligible_topic_menus("SONDESH")][-1] == second["menu_id"]
    db.init_db()
    assert [menu["menu_id"] for menu in menu_catalogue.eligible_topic_menus("SONDESH")][-1] == second["menu_id"]
    client.patch(f"/admin/menu-catalogue/{second['menu_id']}", headers=headers, json={"active": False})
    assert second["menu_id"] not in {menu["menu_id"] for menu in menu_catalogue.eligible_topic_menus("SONDESH")}
    assert first["menu_id"] in {menu["menu_id"] for menu in menu_catalogue.eligible_topic_menus("SONDESH")}


@pytest.mark.parametrize("state", ["pending", "expired", "future", "superseded"])
def test_non_authoritative_linked_menu_is_excluded(workspace, state):
    client, headers = workspace
    menu = _create(client, headers, f"{state} menu", "SAVOURY")
    if state == "expired":
        _publish(menu["menu_id"], start="2026-09-18", end="2026-09-24")
    elif state == "future":
        _publish(menu["menu_id"], start="2026-10-01", end="2026-10-07")
    elif state == "superseded":
        _publish(menu["menu_id"], state="SUPERSEDED")
    else:
        _publish(menu["menu_id"])
        with db.get_connection() as conn:
            conn.execute("INSERT INTO menu_drafts(menu_key,draft_json,review_json) VALUES(?,'[]',?)",
                         (menu["menu_id"], json.dumps({"status": "NEEDS_REVIEW", "created_at": "2026-09-26T10:00:00Z"})))
            conn.commit()
    assert menu["menu_id"] not in {item["menu_id"] for item in menu_catalogue.eligible_topic_menus("SAVOURY")}


def test_explicit_sondesh_menu_request_uses_sorted_approved_assets(workspace):
    client, headers = workspace
    new = _create(client, headers, "Wedding collection", "SONDESH")
    _publish(new["menu_id"])
    assert client.post(f"/admin/menu-catalogue/{new['menu_id']}/move", headers=headers,
                       json={"position": 1}).status_code == 200
    user = "phase45-explicit-menu"
    session_memory.clear_session(user, "instagram")
    text, matched, template, images = responder.match_and_respond(
        "Show me Sondesh menus", user_id=user, channel="instagram")
    assert matched and template == "customer_topic_menus"
    assert images[0] == f"/uploads/{new['menu_id']}.jpg"
    assert "price" not in text.lower()
    assert session_memory.get_facts(user, "instagram")["last_route_source"] == "EXPLICIT_CUSTOMER_TOPIC"
    session_memory.clear_session("phase45-stale-source", "instagram")
    session_memory.get_facts("phase45-stale-source", "instagram")["source_route"] = {
        "active": True, "routing_topic": "Home Table", "menu_mode": "NONE",
    }
    explicit = responder.match_and_respond("Show me Sondesh menus", user_id="phase45-stale-source",
                                           channel="instagram")
    assert explicit[2] == "customer_topic_menus" and explicit[3][0] == images[0]
    unsafe = responder.match_and_respond("What vegan Sondesh options do you have?",
                                         user_id="phase45-unsupported", channel="instagram")
    assert unsafe[2] != "customer_topic_menus"


def test_admin_topic_mutations_require_authentication(workspace):
    client, _ = workspace
    assert client.post("/admin/menu-categories", json={"name": "No auth"}).status_code == 403
    assert client.post("/admin/menu-catalogue/aamar_mishti/move", json={"position": 1}).status_code == 403


@pytest.mark.parametrize("fmt,mime,extension", [
    ("JPEG", "image/jpeg", "jpg"), ("PNG", "image/png", "png"),
    ("WEBP", "image/webp", "webp"), ("PDF", "application/pdf", "pdf"),
])
def test_add_menu_upload_starts_review_without_publication(workspace, monkeypatch, fmt, mime, extension):
    client, headers = workspace
    output = io.BytesIO()
    Image.new("RGB", (80, 120), "white").save(output, format=fmt)
    monkeypatch.setattr(menu_review.menu_parser, "parse_image_and_update_menu",
                        lambda *args: {"status": "failed", "diagnostic_codes": ["INVALID_JSON"]})
    response = client.post("/admin/menu-catalogue", headers=headers,
                           data={"name": f"{fmt} draft", "category": "Other",
                                 "description": "Review source", "customer_topic": "SONDESH"},
                           files={"file": (f"../private.{extension}", output.getvalue(), mime)})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["published"] is False
    assert payload["draft"]["status"] == "NEEDS_REVIEW"
    assert payload["draft"]["original_filename"] == f"private.{extension}"
    assert payload["menu"]["menu_id"] not in {
        item["menu_id"] for item in menu_catalogue.eligible_topic_menus("SONDESH")
    }


def test_bad_or_oversize_upload_does_not_create_menu(workspace):
    client, headers = workspace
    fields = {"name": "Rejected upload", "category": "Other", "description": "Not public"}
    invalid = client.post("/admin/menu-catalogue", headers=headers, data=fields,
                          files={"file": ("bad.gif", b"GIF89a", "image/gif")})
    assert invalid.status_code == 400
    assert menu_catalogue.get_menu("rejected_upload") is None
    bad_pdf = client.post("/admin/menu-catalogue", headers=headers, data=fields,
                          files={"file": ("bad.pdf", b"%PDF-this-is-not-a-document", "application/pdf")})
    assert bad_pdf.status_code == 400
    assert menu_catalogue.get_menu("rejected_upload") is None
    oversized = client.post("/admin/menu-catalogue", headers=headers, data=fields,
                            files={"file": ("large.jpg", b"x" * (10 * 1024 * 1024 + 1), "image/jpeg")})
    assert oversized.status_code == 413
    assert menu_catalogue.get_menu("rejected_upload") is None


def test_additive_topic_migration_keeps_existing_custom_menu_unlinked(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE menu_catalogue(menu_id TEXT PRIMARY KEY,name TEXT NOT NULL,category TEXT NOT NULL,"
                     "description TEXT NOT NULL,active INTEGER NOT NULL,is_builtin INTEGER NOT NULL,"
                     "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO menu_catalogue(menu_id,name,category,description,active,is_builtin) "
                     "VALUES('legacy_custom','Legacy Custom','Other','Keep this row',1,0)")
    monkeypatch.setattr(db, "db_path", str(path))
    db.init_db()
    original = db.get_menu_catalogue_entry("legacy_custom")
    assert original["description"] == "Keep this row"
    assert original["customer_topic"] is None
    assert db.get_menu_catalogue_entry("aamar_mishti")["customer_topic"] == "SONDESH"
    db.init_db()
    assert db.get_menu_catalogue_entry("legacy_custom") == original


def test_additive_migration_grandfathers_previously_approved_custom_menu(tmp_path, monkeypatch):
    path = tmp_path / "approved-legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE menu_catalogue(menu_id TEXT PRIMARY KEY,name TEXT NOT NULL,category TEXT NOT NULL,"
                     "description TEXT NOT NULL,active INTEGER NOT NULL,is_builtin INTEGER NOT NULL,"
                     "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO menu_catalogue(menu_id,name,category,description,active,is_builtin) "
                     "VALUES('legacy_approved','Legacy Approved','Other','Existing reviewed menu',1,0)")
        conn.execute("CREATE TABLE menu_versions(id INTEGER PRIMARY KEY AUTOINCREMENT,menu_key TEXT NOT NULL,"
                     "snapshot_json TEXT NOT NULL,action TEXT NOT NULL DEFAULT 'publish',changed_by TEXT,"
                     "changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,approval_json TEXT,is_current INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO menu_versions(menu_key,snapshot_json,approval_json,is_current) VALUES(?,?,?,1)",
                     ("legacy_approved", "{}", json.dumps({"rows": [], "approved_at": "2026-09-25T10:00:00Z",
                                                           "image_path": "/uploads/legacy-approved.jpg"})))
    monkeypatch.setattr(db, "db_path", str(path))
    db.init_db()
    assert db.get_menu_catalogue_entry("legacy_approved")["customer_topic"] is None
    assert db.get_menu_catalogue_entry("legacy_approved")["legacy_customer_eligible"] == 1
    assert "legacy_approved" in {version["menu_key"] for version in menu_review.customer_versions()}
    db.init_db()
    assert db.get_menu_catalogue_entry("legacy_approved")["legacy_customer_eligible"] == 1
