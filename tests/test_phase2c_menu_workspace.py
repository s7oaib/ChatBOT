"""Phase 2C-1 is an Admin-only projection, never a publication path."""

import json
import re
import shutil
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from camellia import menu_dates, menu_catalogue
from camellia import menu_images
from camellia import app as app_module
from camellia.admin_menu_review import REVIEW_HTML, REVIEW_SCRIPT
from camellia.config import config
from camellia.database import db


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    path = tmp_path / "menu.json"
    shutil.copy(config.MENU_JSON_PATH, path)
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(path))
    monkeypatch.setattr(db, "db_path", str(tmp_path / "workspace.db"))
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    db.init_db()
    return TestClient(app_module.app), {"X-Admin-Key": config.ADMIN_API_KEY}, path


def _read(workspace):
    client, headers, _ = workspace
    response = client.get("/admin/menu-catalogue", headers=headers)
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("state", ["fallback", "published", "draft", "archived", "custom", "inactive", "alias"])
def test_batch_cards_match_single_semantics(workspace, state):
    key = "aamar_mishti"
    if state in ("custom", "inactive"):
        key = menu_catalogue.create_menu("Synthetic card", "Other", "Synthetic description")["menu_id"]
    if state in ("published", "draft", "archived", "inactive"):
        db.save_menu_image_draft(key, "/uploads/test-image.jpeg", "test")
        db.publish_menu_image(key)
    if state == "draft":
        db.save_menu_image_draft(key, "/uploads/test-draft.jpeg", "test")
    if state == "archived":
        with db.get_connection() as conn:
            conn.execute("UPDATE menu_image_versions SET status='archived' WHERE menu_key=?", (key,))
            conn.commit()
    if state == "inactive":
        with db.get_connection() as conn:
            conn.execute("UPDATE menu_catalogue SET active=0 WHERE menu_id=?", (key,))
            conn.commit()
    if state == "alias":
        key = "collections.aamar_mishti"
    assert menu_images.cards([key])[key] == menu_images.card(key)


@pytest.mark.parametrize("period", [("2026-09-25", "2026-09-30"), ("2026-10-01", "2026-10-07"), ("2026-09-18", "2026-09-24")])
def test_batch_weekly_cards_match_lifecycle(workspace, period):
    version = _version("weekly_vegetarian", *period)
    with db.get_connection() as conn:
        conn.execute("UPDATE menu_versions SET approval_json=? WHERE id=?", (json.dumps({
            "rows": [], "approved_at": "2026-09-25T10:00:00Z",
            "image_path": "/uploads/test-approved-weekly.jpeg"}), version))
        conn.commit()
    keys = ["weekly_vegetarian", "weekly_non_vegetarian", "weekly.vegetarian"]
    expected = {key: menu_images.card(key) for key in keys}
    assert menu_images.cards(keys) == expected


def test_batch_aliases_and_missing_keys(workspace):
    keys = list(menu_images.CANONICAL_ALIASES)
    keys += list(menu_images.CANONICAL_ALIASES.values())
    assert menu_images.cards(keys) == {key: menu_images.card(key) for key in keys}
    assert menu_images.cards([]) == {}
    with pytest.raises(KeyError):
        menu_images.cards(["not-registered"])


def test_catalogue_image_query_growth_is_zero_and_read_is_pure(workspace, monkeypatch):
    from contextlib import contextmanager
    import socket

    def deny_network(*args, **kwargs):
        raise AssertionError("Catalogue read must not access external network")

    original_connect = socket.socket.connect

    def local_connect(sock, address):
        # Windows asyncio builds its internal wake-up socket on loopback.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        return deny_network()

    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket.socket, "connect", local_connect)
    statements = []
    original = db.get_connection

    @contextmanager
    def traced():
        with original() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(db, "get_connection", traced)
    counts = []
    for amount in (5, 45):
        for i in range(amount):
            menu_catalogue.create_menu(f"Audit {amount}-{i}", "Other", "Synthetic audit menu")
        with original() as conn:
            before = tuple(conn.iterdump())
        menu_before = workspace[2].read_bytes()
        qa_before = Path(config.QA_TEMPLATES_PATH).read_bytes()
        statements.clear()
        _read(workspace)
        reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        counts.append((len(reads), sum("menu_image_versions" in sql for sql in reads)))
        with original() as conn:
            assert tuple(conn.iterdump()) == before
        assert workspace[2].read_bytes() == menu_before
        assert Path(config.QA_TEMPLATES_PATH).read_bytes() == qa_before
    assert counts[1][1] - counts[0][1] == 0
    assert counts[1][0] - counts[0][0] == 0


def _rows(workspace):
    return {row["menu_id"]: row for row in _read(workspace)["workspace_rows"]}


def _draft(key, status):
    with db.get_connection() as conn:
        conn.execute("INSERT INTO menu_drafts(menu_key,draft_json,review_json) VALUES(?,'[]',?)",
                     (key, json.dumps({"status": status, "created_at": "2026-09-27T10:00:00Z"})))
        conn.commit()


def _version(key, start=None, end=None, state=None):
    with db.get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO menu_versions(menu_key,snapshot_json,approval_json,is_current,"
            "valid_from,valid_until,weekly_state) VALUES(?,?,?,?,?,?,?)",
            (key, "{}", json.dumps({"rows": [], "approved_at": "2026-09-25T10:00:00Z",
                                   "source_type": "text"}), int(not start), start, end, state))
        conn.commit()
        return cursor.lastrowid


@pytest.mark.parametrize("function", ["submitCategory", "confirmCategoryDelete"])
def test_category_changes_refresh_the_menu_workspace(workspace, function):
    page = workspace[0].get("/admin").text
    handler = page.split("async function " + function + "()", 1)[1].split("catch (err)", 1)[0]
    assert "await mrMenus();" in handler


def test_primary_menu_navigation_is_middle_entry_and_weekly_visible_above_catalogue(workspace):
    page = workspace[0].get("/admin").text
    sidebar = page.split('<aside class="admin-sidebar"', 1)[1].split('</aside>', 1)[0]
    labels = re.findall(r'<span class="nav-label">(.*?)</span>', sidebar)
    assert labels[:6] == ["Dashboard", "Instagram Stories", "Menu", "Posts &amp; Reels", "Ads", "Handoffs"]
    assert labels.count("Menu") == 1
    assert "Menu Manager" not in labels and "Menu V2" not in labels and "Catalogue" not in labels
    assert 'id="menuSidebarBadge"' in sidebar
    menu = page.split('<div id="tab-image"', 1)[1].split('<div id="tab-ad-routing"', 1)[0]
    assert menu.index('id="weeklySavouryPanel"') < menu.index('id="mrCatalogueView"')
    assert 'id="mrWeeklyView">' in menu
    assert "menu.workspace==='weekly'?mrOpenWeekly()" not in page
    assert "catalogueRows=mrWorkspaceRows.filter(menu=>menu.workspace!=='weekly')" in page
    assert "mrShowCatalogue();" in page.split("function switchTab", 1)[1].split("function setInstagram", 1)[0]


def test_classic_cards_and_mobile_stacking_preserve_selected_workflows():
    assert 'id="mrCatalogueTable"' not in REVIEW_HTML
    assert '<div id="mrCatalogueList" class="mr-catalogue-list">' in REVIEW_HTML
    assert "const card=document.createElement('article')" in REVIEW_SCRIPT
    assert "menu.active?'Active':'Inactive'" in REVIEW_SCRIPT
    assert "menu.review_required?menu.review_label:null" in REVIEW_SCRIPT
    assert '.mr-card{grid-template-columns:minmax(0,1fr)' in REVIEW_HTML
    assert 'overflow-y:auto;margin:auto' in REVIEW_HTML
    assert 'onclick="mrManageCategories()"' in REVIEW_HTML
    assert "Manage categories" in REVIEW_HTML
    assert "@media(max-width:600px)" in REVIEW_HTML
    assert "content:attr(data-label)" in REVIEW_HTML
    assert "min-height:44px" in REVIEW_HTML
    for target in ("mrSelectedView", "mrCurrentPane", "mrUploadPane", "mrVersionsPane", "mrMenuDialog"):
        assert f'id="{target}"' in REVIEW_HTML
    assert "mrOpenWeekly" not in REVIEW_SCRIPT


@pytest.mark.parametrize("status,label,required", [
    ("NEEDS_REVIEW", "Needs review", True), ("READY_TO_APPROVE", "Ready to approve", True),
    ("EXTRACTING", "Extracting", True), ("FAILED", "Needs review", True),
    ("PUBLISHED", "—", False), ("REJECTED", "—", False),
])
def test_review_count_uses_only_unresolved_current_drafts(workspace, status, label, required):
    _draft("aamar_mishti", status)
    data = _read(workspace)
    row = next(row for row in data["workspace_rows"] if row["menu_id"] == "aamar_mishti")
    assert row["review_label"] == label
    assert row["review_required"] is required
    assert data["review_count"] == int(required)
    assert row["pending_review_status"] == (status if required else None)


def test_empty_custom_standing_and_inactive_are_distinct(workspace):
    custom = menu_catalogue.create_menu("Festival", "Other", "Operator description")
    menu_catalogue.update_menu("signature", active=False)
    rows = _rows(workspace)
    assert rows["aamar_mishti"]["validity_label"] == "Standing"
    assert rows[custom["menu_id"]]["validity_label"] == "Not published"
    assert rows[custom["menu_id"]]["display_status"] == "Not published"
    assert rows["signature"]["display_status"] == "INACTIVE"
    assert rows[custom["menu_id"]]["last_approved_version"] is None


@pytest.mark.parametrize("start,end,state,expected", [
    ("2026-09-25", "2026-09-30", None, "CURRENT"),
    ("2026-10-01", "2026-10-07", None, "UPCOMING"),
    ("2026-09-18", "2026-09-24", None, "EXPIRED"),
    ("2026-09-18", "2026-09-24", "SUPERSEDED", "SUPERSEDED"),
])
def test_weekly_summary_uses_existing_approved_lifecycle(workspace, start, end, state, expected):
    # Keep legacy baseline from masking this isolated version selection case.
    workspace[2].write_text(json.dumps({"collections": {}, "weekly_menus": []}), encoding="utf-8")
    version = _version("weekly_vegetarian", start, end, state)
    row = _rows(workspace)["__weekly_savoury__"]
    assert row["lifecycle_status"] == expected
    assert row["valid_from"] == start and row["valid_until"] == end
    assert row["last_approved_version"] == version
    assert row["source_type"] == "text"


def test_weekly_current_wins_over_later_scheduled_version(workspace):
    current = _version("weekly_vegetarian", "2026-09-25", "2026-09-30")
    future = _version("weekly_vegetarian", "2026-10-01", "2026-10-07")
    row = _rows(workspace)["__weekly_savoury__"]
    assert row["lifecycle_status"] == "CURRENT"
    assert row["current_version"] == current
    assert row["last_approved_version"] == future


def test_catalogue_get_is_zero_write_even_with_tombstoned_weekly(workspace):
    for key in ("weekly_vegetarian", "weekly_non_vegetarian"):
        menu_catalogue.permanently_delete_menu(key, "test")
    _draft("weekly_vegetarian", "NEEDS_REVIEW")
    with db.get_connection() as conn:
        before = tuple(conn.iterdump())
    menu_before = workspace[2].read_bytes()
    qa_before = Path(config.QA_TEMPLATES_PATH).read_bytes()
    data = _read(workspace)
    _read(workspace)
    with db.get_connection() as conn:
        assert tuple(conn.iterdump()) == before
    assert workspace[2].read_bytes() == menu_before
    assert Path(config.QA_TEMPLATES_PATH).read_bytes() == qa_before
    assert not {"weekly_vegetarian", "weekly_non_vegetarian", "weekly_savoury"} & {
        row["menu_id"] for row in data["menus"]}
    weekly = next(row for row in data["workspace_rows"] if row["workspace"] == "weekly")
    assert weekly["review_required"] is True
    assert weekly["actions"] == {"edit": False, "toggle_active": False, "delete": False}
    assert data["review_count"] == 1


def test_new_menu_creation_remains_metadata_only(workspace):
    client, headers, _ = workspace
    response = client.post("/admin/menu-catalogue", headers=headers, data={
        "name": "New Menu", "category": "Other", "description": "Customer-safe description"})
    assert response.status_code == 200
    assert response.json()["published"] is False and response.json()["draft"] is None
    key = response.json()["menu"]["menu_id"]
    assert db.get_menu_draft(key) is None and db.get_menu_versions(key) == []
    assert _rows(workspace)[key]["validity_label"] == "Not published"


def test_projection_does_not_expose_unapproved_facts_or_errors(workspace):
    with db.get_connection() as conn:
        conn.execute("INSERT INTO menu_drafts(menu_key,draft_json,review_json) VALUES(?,'[]',?)",
                     ("aamar_mishti", json.dumps({"status": "FAILED", "rows": [{"price_inr": 999999}],
                                                 "warnings": ["PRIVATE_PROVIDER_ERROR"]})))
        conn.commit()
    data = _read(workspace)
    assert "PRIVATE_PROVIDER_ERROR" not in json.dumps(data)
    assert "999999" not in json.dumps(data)


def test_reference_availability_is_read_only_and_delete_stays_protected(workspace):
    with db.get_connection() as conn:
        conn.execute("INSERT INTO instagram_topics(topic_id,name,default_menu_id,active) VALUES(?,?,?,1)",
                     ("test-menu-reference", "Test topic", "aamar_mishti"))
        conn.commit()
    row = _rows(workspace)["aamar_mishti"]
    assert row["reference_count"] >= 1 and row["actions"]["delete"] is False
    assert workspace[0].delete("/admin/menu-catalogue/aamar_mishti/permanent", headers=workspace[1]).status_code == 409


def test_fifty_menu_projection_has_bounded_reference_queries(workspace, monkeypatch):
    from contextlib import contextmanager
    for index in range(50):
        menu_catalogue.create_menu(f"Audit menu {index}", "Other", "Synthetic query audit")
    menus = menu_catalogue.list_menus()
    statements = []
    original = db.get_connection

    @contextmanager
    def traced_connection():
        with original() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(db, "get_connection", traced_connection)
    result = menu_catalogue.workspace_summary(menus)
    reference_reads = [sql for sql in statements if "FROM instagram_source_routes" in sql
                       or "FROM instagram_topics" in sql]
    assert len(reference_reads) <= 2
    assert len(result["workspace_rows"]) >= 50
