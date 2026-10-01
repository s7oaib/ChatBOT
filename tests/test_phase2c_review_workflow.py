"""Human-controlled retries, aliases and immutable history reuse review storage."""
import io
import json
import shutil

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from camellia import menu_review
from camellia.app import app
from camellia.config import config
from camellia.database import db
from camellia.admin_menu_review import REVIEW_HTML, REVIEW_SCRIPT
from camellia.admin_weekly_savoury import WEEKLY_HTML, WEEKLY_SCRIPT


@pytest.fixture
def review_env(tmp_path, monkeypatch):
    path = tmp_path / "menu.json"
    shutil.copy(config.MENU_JSON_PATH, path)
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(path))
    monkeypatch.setattr(db, "db_path", str(tmp_path / "review.db"))
    db.init_db()
    monkeypatch.setattr(menu_review, "reload_knowledge", lambda: None)
    return TestClient(app), {"X-Admin-Key": config.ADMIN_API_KEY}


def test_retry_text_requires_revision_and_never_publishes(review_env):
    draft = menu_review.extract("bento", "admin", text="Veg:\nTest meal 250g ₹100")
    with pytest.raises(menu_review.ReviewConflict):
        menu_review.retry_extraction("bento", draft["draft_id"], 99, "reviewer")
    retried = menu_review.retry_extraction("bento", draft["draft_id"], draft["revision"], "reviewer")
    assert retried["status"] == "NEEDS_REVIEW"
    assert retried["draft_id"] != draft["draft_id"]
    assert retried["rows"][0]["price_inr"] == 100
    assert db.get_menu_versions("bento") == []
    with db.get_connection() as conn:
        details = json.loads(conn.execute("SELECT details_json FROM menu_review_audit WHERE action='extraction_retried'").fetchone()[0])
    assert details["previous_draft_id"] == draft["draft_id"]


def test_failed_image_retry_retains_private_source_and_requires_human_approval(review_env, monkeypatch):
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(stream, "PNG")
    monkeypatch.setattr(menu_review.menu_parser, "parse_image_and_update_menu", lambda *_: {"status": "error"})
    draft = menu_review.extract("bento", "admin", data=stream.getvalue(), mime="image/png")
    retried = menu_review.retry_extraction("bento", draft["draft_id"], draft["revision"], "admin")
    assert retried["status"] == "NEEDS_REVIEW" and retried["source_asset"]
    assert retried["rows"] == []
    assert db.get_menu_versions("bento") == []
    with pytest.raises(ValueError):
        menu_review.publish_approved_menu("bento", retried["draft_id"], retried["revision"], "admin")


def test_retry_endpoint_auth_and_terminal_draft_protection(review_env):
    client, headers = review_env
    draft = menu_review.extract("bento", "admin", text="Veg:\nTest meal 250g ₹100")
    path = "/admin/menu-manager/menus/bento/review/retry-extraction"
    body = {"draft_id": draft["draft_id"], "revision": draft["revision"]}
    assert client.post(path, json=body).status_code in {401, 403}
    menu_review.reject("bento", draft["draft_id"], draft["revision"], "admin")
    assert client.post(path, json=body, headers=headers).status_code == 409


def test_standing_menu_alias_collision_blocks_approval(review_env):
    draft = menu_review.extract("bento", "admin", text="Veg:\nFirst meal 250g ₹100\nSecond meal 250g ₹120")
    rows = draft["rows"]
    rows[0]["aliases"] = ["shared alias"]
    rows[1]["aliases"] = ["shared alias"]
    edited = menu_review.edit("bento", draft["draft_id"], draft["revision"], rows, "admin")
    assert edited["status"] == "NEEDS_REVIEW"
    assert any("alias" in warning for warning in edited["warnings"])
    with pytest.raises(ValueError, match="alias"):
        menu_review.publish_approved_menu("bento", edited["draft_id"], edited["revision"], "admin")


def test_history_pagination_keeps_approved_snapshots_immutable(review_env):
    client, headers = review_env
    for index in range(25):
        db.record_menu_version("bento", json.dumps({"marker": index}), "test")
    first = client.get("/admin/menu-manager/menus/bento/review/versions?limit=10", headers=headers)
    assert first.status_code == 200
    assert len(first.json()["versions"]) == 10 and first.json()["has_more"]
    before = first.json()["next_before_id"]
    second = client.get(f"/admin/menu-manager/menus/bento/review/versions?limit=10&before_id={before}", headers=headers)
    assert len(second.json()["versions"]) == 10
    assert not {v["id"] for v in first.json()["versions"]} & {v["id"] for v in second.json()["versions"]}
    assert len(db.get_menu_versions("bento", limit=100)) == 25


def test_review_ui_exposes_retry_aliases_friendly_warnings_and_load_more():
    assert "Retry Extraction" in REVIEW_HTML and "mrRetryExtraction" in REVIEW_SCRIPT
    assert "<th>Section</th>" in REVIEW_HTML and "<th>Aliases</th>" in REVIEW_HTML
    assert "mrWarningLabel" in REVIEW_SCRIPT
    assert "Load more versions" in REVIEW_SCRIPT
    assert "Source flyer" in REVIEW_SCRIPT and "mrPreviewVersionSource" in REVIEW_SCRIPT
    assert "Preview flyer" in WEEKLY_SCRIPT
    assert "Source flyer" not in WEEKLY_SCRIPT
    weekly_open = REVIEW_SCRIPT.split("function mrShowCatalogue(){", 1)[1].split("async function mrManageCategories", 1)[0]
    assert "document.getElementById('menuReviewPanel').hidden=false" in weekly_open
    assert "document.getElementById('mrCatalogueView').hidden=false" in weekly_open
    assert "if(weekly)weekly.hidden=false" in weekly_open
    assert "retry-extraction" in WEEKLY_SCRIPT and "Retry Extraction" in WEEKLY_HTML
    assert 'id="wsNeedsReview"' in WEEKLY_HTML
    assert "document.getElementById('wsNeedsReview').hidden=false" in WEEKLY_SCRIPT


def test_approved_history_source_preview_is_private_and_read_only(review_env, monkeypatch):
    client, headers = review_env
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(stream, "PNG")
    monkeypatch.setattr(menu_review.menu_parser, "parse_image_and_update_menu", lambda *_: {
        "status": "success", "parsed": {"vegetarian": [{"item": "Test meal", "quantity": "250g", "price_inr": 100}]}})
    draft = menu_review.extract("bento", "admin", data=stream.getvalue(), mime="image/png")
    published = menu_review.publish_approved_menu("bento", draft["draft_id"], draft["revision"], "admin")
    path = f'/admin/menu-manager/menus/bento/review/versions/{published["version_id"]}/source'
    assert client.get(path).status_code in {401, 403}
    with db.get_connection() as conn:
        before = tuple(conn.iterdump())
    response = client.get(path, headers=headers)
    assert response.status_code == 200 and response.headers["content-type"].startswith("image/png")
    with db.get_connection() as conn:
        assert tuple(conn.iterdump()) == before
    assert client.get(path.replace("/bento/", "/signature/"), headers=headers).status_code == 404


def test_version_source_path_cannot_escape_private_storage(review_env):
    with db.get_connection() as conn:
        cursor = conn.execute("INSERT INTO menu_versions(menu_key,snapshot_json,approval_json) VALUES('bento','[]',?)",
                              (json.dumps({"source_asset": "../menu.json"}),))
        conn.commit()
        version_id = cursor.lastrowid
    with pytest.raises(KeyError):
        menu_review.version_source_file("bento", version_id)
