"""Local-only release gates for extraction, approval, atomicity and provenance."""
import copy
import io
import json
import shutil

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from camellia import app as app_module, menu_review, menu_images
from camellia.config import config
from camellia.database import db
from camellia.rag_fallback import rag_fallback_engine
from camellia.responder import responder


@pytest.fixture
def review_env(tmp_path, monkeypatch):
    path = tmp_path / "menu.json"
    shutil.copy(config.MENU_JSON_PATH, path)
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(path))
    monkeypatch.setattr(db, "db_path", str(tmp_path / "review.db"))
    db.init_db()
    for engine in (responder, rag_fallback_engine):
        monkeypatch.setattr(engine, "menu_path", str(path))
        monkeypatch.setattr(engine, "menu_data", copy.deepcopy(engine.menu_data))
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    return path


def create_draft(text="Kanchagolla 250g ₹650", key="aamar_mishti"):
    return menu_review.extract(key, "test-operator", text=text)


def ready(draft, **edits):
    rows = copy.deepcopy(draft["rows"])
    for row in rows:
        row["category"] = "Sondesh"
        row.update(edits)
    return menu_review.edit(draft["menu_key"], draft["draft_id"], draft["revision"], rows, "reviewer")


def approve(draft):
    return menu_review.publish_approved_menu(draft["menu_key"], draft["draft_id"], draft["revision"], "approver")


def test_text_draft_never_publishes_and_retains_missing_facts(review_env):
    baseline = review_env.read_bytes()
    draft = create_draft("Kanchagolla ₹650\nJolbhora 500g\n₹450")
    assert draft["status"] == "NEEDS_REVIEW"
    assert draft["rows"][0]["quantity"] == ""
    assert draft["rows"][0]["description"] == ""
    assert draft["rows"][1]["price_inr"] is None
    assert draft["rows"][2]["item"] == ""
    assert all(r["category"] == "" for r in draft["rows"])
    assert "Freshly handcrafted" not in json.dumps(draft)
    assert "Artisanal Specialty" not in json.dumps(draft)
    assert menu_review.current_versions() == []
    assert review_env.read_bytes() == baseline


def test_edit_approval_hot_reload_version_and_audit(review_env):
    baseline = review_env.read_bytes()
    draft = ready(create_draft(), price_inr=700)
    assert draft["rows"][0]["provenance"]["price_inr"] == "OPERATOR_EDITED"
    assert draft["rows"][0]["provenance"]["item"] == "EXTRACTED"
    published = approve(draft)
    assert published["status"] == "PUBLISHED"
    assert approve(draft)["version_id"] == published["version_id"]
    assert "₹700" in rag_fallback_engine.retrieve_approved_menu("How much is Kanchagolla?").auto_response
    assert menu_images.current_url("aamar_mishti") == ""
    assert review_env.read_bytes() == baseline  # Legacy baseline is never overwritten.
    second = approve(ready(create_draft(), price_inr=800))
    assert second["version_id"] != published["version_id"]
    assert len(menu_review.version_history("aamar_mishti")) == 2
    assert "₹800" in rag_fallback_engine.retrieve_approved_menu("Kanchagolla price?").auto_response
    restored = menu_review.restore_as_draft("aamar_mishti", published["version_id"], "reviewer")
    assert restored["rows"][0]["price_inr"] == 700
    assert "₹800" in rag_fallback_engine.retrieve_approved_menu("Kanchagolla price?").auto_response
    approve(restored)
    assert "₹700" in rag_fallback_engine.retrieve_approved_menu("Kanchagolla price?").auto_response
    with db.get_connection() as conn:
        actions = {r[0] for r in conn.execute("SELECT action FROM menu_review_audit")}
    assert {"draft_created", "draft_edited", "draft_approved", "menu_published"} <= actions


@pytest.mark.parametrize("field,value", [("item", ""), ("price_inr", -1), ("price_inr", 0),
    ("price_inr", "abc"), ("price_inr", "NaN"), ("price_inr", True), ("price_inr", "2.456"), ("category", "invented")])
def test_bad_facts_cannot_be_approved(review_env, field, value):
    draft = ready(create_draft(), **{field: value})
    with pytest.raises(ValueError):
        approve(draft)
    assert menu_review.current_versions() == []


def test_stale_revision_rejection_and_duplicates(review_env):
    draft = create_draft()
    edited = ready(draft)
    with pytest.raises(menu_review.ReviewConflict):
        approve(draft)
    rows = copy.deepcopy(edited["rows"])
    other = {**rows[0], "row_id": "another", "price_inr": 900}
    edited = menu_review.edit("aamar_mishti", edited["draft_id"], edited["revision"], rows + [other], "operator")
    with pytest.raises(ValueError):
        approve(edited)
    menu_review.reject("aamar_mishti", edited["draft_id"], edited["revision"], "operator")
    with pytest.raises(menu_review.ReviewConflict):
        approve(edited)
    assert not menu_review.current_versions()


def image_bytes(fmt="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (200, 300), "white").save(output, format=fmt)
    return output.getvalue()


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_image_success_is_private_draft_only_then_atomic_publication(review_env, monkeypatch, fmt, mime):
    monkeypatch.setattr(menu_review.menu_parser, "parse_image_and_update_menu", lambda *a: {"status": "success",
        "parsed": {"norom_pak_sondesh": [{"item": "Kanchagolla", "quantity": "250g", "price_inr": 650}]}})
    draft = menu_review.extract("aamar_mishti", "operator", data=image_bytes(fmt), mime=mime, filename="../../menu.jpg")
    assert draft["original_filename"] == "menu.jpg"
    assert menu_review.source_file("aamar_mishti", draft["draft_id"]).exists()
    assert db.get_menu_image_current("aamar_mishti") is None
    assert not menu_review.current_versions()
    approve(draft)
    assert db.get_menu_image_current("aamar_mishti")["image_path"].startswith("/uploads/menu-images/")
    assert menu_review.current_versions()[0]["approval_json"]


def test_transaction_failure_rolls_back_facts_image_and_draft(review_env, monkeypatch):
    first = approve(ready(create_draft()))
    image_before = db.get_menu_image_current("aamar_mishti")
    draft = ready(create_draft(), price_inr=800)
    original_audit = menu_review._audit
    def fail(conn, draft, action, *args):
        if action == "menu_published":
            raise RuntimeError("simulated storage failure")
        original_audit(conn, draft, action, *args)
    monkeypatch.setattr(menu_review, "_audit", fail)
    with pytest.raises(RuntimeError):
        approve(draft)
    assert db.get_menu_image_current("aamar_mishti") == image_before
    assert menu_review.current_versions()[0]["id"] == first["version_id"]
    assert menu_review.get_draft("aamar_mishti")["status"] == "READY_TO_APPROVE"


def test_pdf_text_first_preserves_page_provenance(review_env, monkeypatch):
    import pypdfium2 as pdfium
    # Use the actual renderer on a two-page scanned PDF; no live provider.
    output = io.BytesIO()
    Image.new("RGB", (200, 300), "white").save(output, format="PDF", save_all=True,
        append_images=[Image.new("RGB", (200, 300), "white")])
    monkeypatch.setattr(pdfium.PdfTextPage, "get_text_range", lambda self: "Kanchagolla 250g Rs650")
    monkeypatch.setattr(menu_review.menu_parser, "parse_image_and_update_menu", lambda *a: pytest.fail("Text must run first"))
    draft = menu_review.extract("aamar_mishti", "operator", data=output.getvalue(), mime="application/pdf", filename="menu.pdf")
    assert draft["status"] == "NEEDS_REVIEW"
    assert [r["source_page"] for r in draft["rows"]] == [1, 2]
    assert not menu_review.current_versions()
    assert db.get_menu_image_current("aamar_mishti") is None


def test_pdf_scanned_vision_failure_keeps_private_manual_review_preview(review_env, monkeypatch):
    output = io.BytesIO()
    Image.new("RGB", (200, 300), "white").save(output, format="PDF")
    calls = []
    def vision(*args):
        calls.append(args)
        return {"status": "failed", "reason": "private provider token error"}
    monkeypatch.setattr(menu_review.menu_parser, "parse_image_and_update_menu", vision)
    draft = menu_review.extract("aamar_mishti", "operator", data=output.getvalue(), mime="application/pdf")
    assert calls and draft["status"] == "NEEDS_REVIEW"
    assert draft["rows"] == []
    assert draft["warnings"]
    assert draft["source_asset"].endswith(".pdf")
    assert draft["flyer_asset"].endswith(".png")
    assert "private provider" not in json.dumps(draft)
    assert not menu_review.current_versions()


def test_idempotent_migration_preserves_approved_and_legacy_versions(review_env):
    db.record_menu_version("weekly.vegetarian", "[]", changed_by="legacy")
    published = approve(ready(create_draft()))
    db.init_db()
    db.init_db()
    assert menu_review.current_versions()[0]["id"] == published["version_id"]
    assert db.get_menu_versions("weekly.vegetarian")[0]["snapshot_json"] == "[]"
    assert menu_review.get_draft("aamar_mishti")["status"] == "PUBLISHED"


def test_review_api_auth_and_table_ui(review_env):
    client = TestClient(app_module.app)
    root = "/admin/menu-manager/menus/aamar_mishti/review"
    assert client.post(root + "/extract-text", json={"text": "Test ₹10"}).status_code in (401, 403)
    headers = {"X-Admin-Key": config.ADMIN_API_KEY}
    response = client.post(root + "/extract-text", headers=headers, json={"text": "Test ₹10"})
    assert response.status_code == 200
    assert response.json()["draft"]["created_by"] == "admin-api"
    html = client.get("/admin").text
    for label in ("Review Menu", "Approve &amp; Publish", "Save Draft", "Reject Draft", "Add item", "application/pdf"):
        assert label in html
    assert "row.provenance" in html


@pytest.mark.parametrize("mime,data", [("application/x-msdownload", b"MZ"), ("image/png", b"not image"),
    ("image/jpeg", image_bytes("PNG")), ("application/pdf", b"not pdf")])
def test_invalid_upload_never_live(review_env, mime, data):
    try:
        draft = menu_review.extract("aamar_mishti", "operator", data=data, mime=mime, filename="../../bad.exe")
        assert draft["status"] == "FAILED"
    except ValueError:
        pass
    assert not menu_review.current_versions()
    assert db.get_menu_image_current("aamar_mishti") is None
