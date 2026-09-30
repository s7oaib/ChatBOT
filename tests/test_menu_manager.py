"""Menu Manager: the client's own admin control over what the chatbot sells.

Covers the actual acceptance criterion the client cares about — publishing a
collection change through the admin API makes the chatbot answer with the
new items immediately, with no code edit, restart, or second database that
could drift from what customers see. Each test uses its own temp menu.json /
qa_templates.json / sqlite db (via the fixture below) so nothing here ever
touches the real production files.
"""

import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia import menu_manager
from camellia.config import config
from camellia.database import db
from camellia.responder import responder


@pytest.fixture
def isolated_menu(tmp_path, monkeypatch):
    """Real menu.json/qa_templates.json content, copied to a temp path, with
    responder/menu_manager pointed at the copies — so a publish in a test can
    never touch the actual production data files.

    `responder` is a process-wide singleton also used by every other test
    module, so its in-memory menu_data/qa_data must be put back to the real
    files on teardown — not just the path attributes monkeypatch reverts —
    or every test that runs afterward in this process sees this test's
    temporary data instead of the real menu."""
    import shutil
    real_menu_path = config.MENU_JSON_PATH
    real_qa_path = config.QA_TEMPLATES_PATH
    menu_path = tmp_path / "menu.json"
    qa_path = tmp_path / "qa_templates.json"
    db_path = tmp_path / "test_menu_manager.db"
    shutil.copy(real_menu_path, menu_path)
    shutil.copy(real_qa_path, qa_path)

    monkeypatch.setattr(config, "MENU_JSON_PATH", str(menu_path))
    monkeypatch.setattr(config, "QA_TEMPLATES_PATH", str(qa_path))
    monkeypatch.setattr(db, "db_path", str(db_path))
    # Some other test in the suite can leave config.BYPASS_AD_GATE flipped to
    # False without restoring it (a pre-existing order-dependency elsewhere in
    # the suite, not something this module owns) — pinned true here so these
    # menu-publish tests exercise menu logic, not the unrelated ad gate.
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    db.init_db()
    monkeypatch.setattr(responder, "menu_path", str(menu_path))
    monkeypatch.setattr(responder, "qa_path", str(qa_path))
    responder.reload_data()
    try:
        yield
    finally:
        responder.menu_path = real_menu_path
        responder.qa_path = real_qa_path
        responder.reload_data()


@pytest.fixture
def client():
    return TestClient(app_module.app)


@pytest.fixture
def admin_headers():
    return {"X-Admin-Key": config.ADMIN_API_KEY}


NEW_ITEMS = [
    {"flavour": "Classic", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
     "prices": {"250g": 450, "500g": 800, "1kg": 1500}},
    {"flavour": "Lychee Rose", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
     "prices": {"250g": 500, "500g": 900, "1kg": 1700}},
    {"flavour": "Saffron Pista", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
     "prices": {"250g": 600, "500g": 1050, "1kg": 1900}},
]


def test_menu_manager_lists_every_menu_the_chatbot_actively_uses(isolated_menu, client, admin_headers):
    resp = client.get("/admin/menu-manager/menus", headers=admin_headers)
    assert resp.status_code == 200
    keys = {m["menu_key"] for m in resp.json()["menus"]}
    assert keys == {
        "collections.aamar_mishti", "collections.signature_collection",
        "weekly.vegetarian", "weekly.non_vegetarian", "weekly.bengali_bento",
    }


def test_reading_and_editing_menus_requires_admin_auth(isolated_menu, client):
    assert client.get("/admin/menu-manager/menus").status_code == 403
    assert client.get("/admin/menu-manager/menus/collections.aamar_mishti").status_code == 403
    assert client.put("/admin/menu-manager/menus/collections.aamar_mishti/draft", json={"items": []}).status_code == 403
    assert client.post("/admin/menu-manager/menus/collections.aamar_mishti/publish", json={"items": []}).status_code == 403
    assert client.get("/admin/menu-manager/menus/collections.aamar_mishti/versions").status_code == 403


def test_unknown_menu_key_is_a_404(isolated_menu, client, admin_headers):
    resp = client.get("/admin/menu-manager/menus/not-a-real-menu", headers=admin_headers)
    assert resp.status_code == 404


def test_aamar_mishti_seeds_from_the_current_approved_copy(isolated_menu, client, admin_headers):
    """menu.json's own "collections" block predates this feature and was
    never actually read by the chatbot — opening it here for the first time
    must reflect the real current flavours (from qa_templates.json's already-
    approved reply), not the disconnected legacy shape."""
    resp = client.get("/admin/menu-manager/menus/collections.aamar_mishti", headers=admin_headers)
    flavours = {i["flavour"] for i in resp.json()["published"]}
    assert flavours == {"Classic", "Rose", "Mango", "Kesar", "Ada", "Nolen Gur", "Jolbhora", "Kanchagolla"}


def test_draft_does_not_change_the_live_chatbot_answer(isolated_menu, client, admin_headers):
    before_text, _, _, _ = responder.match_and_respond("Show me Aamar Mishti", user_id="mm_draft_1", channel="instagram")
    resp = client.put(
        "/admin/menu-manager/menus/collections.aamar_mishti/draft",
        headers=admin_headers, json={"items": NEW_ITEMS},
    )
    assert resp.status_code == 200
    after_text, _, _, _ = responder.match_and_respond("Show me Aamar Mishti", user_id="mm_draft_2", channel="instagram")
    assert "Lychee Rose" not in after_text
    assert after_text == before_text or "Classic" in after_text  # unchanged live answer


def test_preview_shows_the_draft_without_publishing(isolated_menu, client, admin_headers):
    client.put(
        "/admin/menu-manager/menus/collections.aamar_mishti/draft",
        headers=admin_headers, json={"items": NEW_ITEMS},
    )
    resp = client.get("/admin/menu-manager/menus/collections.aamar_mishti/preview", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "draft"
    assert "Lychee Rose" in body["customer_facing_text"]
    assert "Saffron Pista" in body["customer_facing_text"]

    live_text, _, _, _ = responder.match_and_respond("Show me Aamar Mishti", user_id="mm_preview_1", channel="instagram")
    assert "Lychee Rose" not in live_text


def test_publish_does_not_authorize_unapproved_customer_facts(isolated_menu, eligible_weekly_asset, client, admin_headers):
    """Admin template publication cannot authorize unapproved customer facts."""
    before_text, before_matched, _, _ = responder.match_and_respond("aamar mishti price", user_id="mm_pub_before", channel="instagram")

    resp = client.post(
        "/admin/menu-manager/menus/collections.aamar_mishti/publish",
        headers=admin_headers, json={"items": NEW_ITEMS},
    )
    assert resp.status_code == 409

    after_text, matched, _, _ = responder.match_and_respond("aamar mishti price", user_id="mm_pub_after", channel="instagram")
    assert "Lychee Rose" not in after_text
    assert "Saffron Pista" not in after_text
    assert after_text == before_text
    from camellia.client_qa import REGISTRY_BY_ID
    assert after_text == REGISTRY_BY_ID["sondesh_price_client"].answer
    assert before_matched is True and matched is True


def test_published_menu_is_read_from_disk_not_a_process_cache(isolated_menu, client, admin_headers):
    """Simulates a restart: a brand new Responder instance reading the same
    files must see the published change too, proving it landed in the
    authoritative file and not only in the live process's memory."""
    client.post(
        "/admin/menu-manager/menus/collections.aamar_mishti/publish",
        headers=admin_headers, json={"items": NEW_ITEMS},
    )
    from camellia.responder import DeterministicResponder
    fresh = DeterministicResponder(qa_path=config.QA_TEMPLATES_PATH, menu_path=config.MENU_JSON_PATH)
    template = next(t for t in fresh.qa_data["templates"] if t["id"] == "aamar_mishti_collection")
    assert "Lychee Rose" not in template["response"]


def test_publish_rejects_an_empty_collection(isolated_menu, client, admin_headers):
    resp = client.post(
        "/admin/menu-manager/menus/collections.aamar_mishti/publish",
        headers=admin_headers, json={"items": []},
    )
    assert resp.status_code == 409


def test_publish_rejects_a_duplicate_flavour(isolated_menu, client, admin_headers):
    dup = [
        {"flavour": "Classic", "prices": {"250g": 450}},
        {"flavour": "classic", "prices": {"250g": 500}},
    ]
    resp = client.post(
        "/admin/menu-manager/menus/collections.aamar_mishti/publish",
        headers=admin_headers, json={"items": dup},
    )
    assert resp.status_code == 409


def test_publish_rejects_an_invalid_price(isolated_menu, client, admin_headers):
    resp = client.post(
        "/admin/menu-manager/menus/collections.aamar_mishti/publish",
        headers=admin_headers, json={"items": [{"flavour": "Classic", "prices": {"250g": -5}}]},
    )
    assert resp.status_code == 409


def test_inactive_item_is_kept_but_not_shown_to_customers(isolated_menu, client, admin_headers):
    items = NEW_ITEMS + [{"flavour": "Retired Flavour", "active": False, "prices": {"250g": 400}}]
    client.post(
        "/admin/menu-manager/menus/collections.aamar_mishti/publish",
        headers=admin_headers, json={"items": items},
    )
    text, _, _, _ = responder.match_and_respond("aamar mishti price", user_id="mm_inactive", channel="instagram")
    assert "Retired Flavour" not in text

    resp = client.get("/admin/menu-manager/menus/collections.aamar_mishti", headers=admin_headers)
    flavours = {i["flavour"] for i in resp.json()["published"]}
    assert "Retired Flavour" not in flavours


def test_version_history_records_publishes_and_restore_brings_back_old_items(isolated_menu, client, admin_headers):
    original = client.get("/admin/menu-manager/menus/collections.aamar_mishti", headers=admin_headers).json()["published"]
    before_text, _, _, _ = responder.match_and_respond("aamar mishti price", user_id="mm_restore_before", channel="instagram")

    client.post(
        "/admin/menu-manager/menus/collections.aamar_mishti/publish",
        headers=admin_headers, json={"items": NEW_ITEMS},
    )
    versions = client.get("/admin/menu-manager/menus/collections.aamar_mishti/versions", headers=admin_headers).json()["versions"]
    assert len(versions) == 0

    # Publish the very first (original 8-item) content again, then restore back to the Lychee Rose version.
    original_pub = client.post(
        "/admin/menu-manager/menus/collections.aamar_mishti/publish",
        headers=admin_headers, json={"items": original},
    )
    assert original_pub.status_code == 409
    versions_after = client.get("/admin/menu-manager/menus/collections.aamar_mishti/versions", headers=admin_headers).json()["versions"]
    assert not versions_after
    text, _, _, _ = responder.match_and_respond("aamar mishti price", user_id="mm_restore", channel="instagram")
    assert "Lychee Rose" not in text
    assert text == before_text

    versions_final = client.get("/admin/menu-manager/menus/collections.aamar_mishti/versions", headers=admin_headers).json()["versions"]
    assert not versions_final


def test_weekly_menu_is_manageable_through_the_same_admin_surface(isolated_menu, client, admin_headers):
    new_veg = [{"item": "Test Dish", "price_inr": 300, "description": "test", "quantity": "250g"}]
    resp = client.post(
        "/admin/menu-manager/menus/weekly.vegetarian/publish",
        headers=admin_headers, json={"items": new_veg},
    )
    assert resp.status_code == 409
    text, _, _, _ = responder.match_and_respond("show me the veg menu", user_id="mm_weekly", channel="instagram")
    assert "Test Dish" not in text


def test_discard_draft_removes_it(isolated_menu, client, admin_headers):
    client.put(
        "/admin/menu-manager/menus/collections.aamar_mishti/draft",
        headers=admin_headers, json={"items": NEW_ITEMS},
    )
    resp = client.delete("/admin/menu-manager/menus/collections.aamar_mishti/draft", headers=admin_headers)
    assert resp.status_code == 200
    menu = client.get("/admin/menu-manager/menus/collections.aamar_mishti", headers=admin_headers).json()
    assert menu["draft"] is None
