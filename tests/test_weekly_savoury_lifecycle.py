"""Weekly Savoury remains one approved menu-version timeline, never a live OCR write."""

import copy
import io
import json
import shutil
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from camellia import menu_dates, menu_images, menu_review, order_calculator, weekly_savoury
from camellia.app import app
from camellia.config import config
from camellia.database import db
from camellia.rag_fallback import rag_fallback_engine
from camellia.responder import responder
from camellia.admin_weekly_savoury import WEEKLY_HTML, WEEKLY_SCRIPT


@pytest.fixture
def weekly_env(tmp_path, monkeypatch):
    path = tmp_path / "menu.json"
    shutil.copy(config.MENU_JSON_PATH, path)
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(path))
    monkeypatch.setattr(db, "db_path", str(tmp_path / "weekly.db"))
    db.init_db()
    for engine in (responder, rag_fallback_engine):
        monkeypatch.setattr(engine, "menu_path", str(path))
        monkeypatch.setattr(engine, "menu_data", copy.deepcopy(engine.menu_data))
    return path


def _draft(label, price=850, item="Chicken Kosha"):
    return menu_review.extract(weekly_savoury.WEEKLY_KEY, "admin",
        text=f"{label}\nVeg:\n{item} 500g ₹{price}", weekly=True)


def _publish(draft, **options):
    return menu_review.publish_approved_menu(weekly_savoury.WEEKLY_KEY,
        draft["draft_id"], draft["revision"], "admin", weekly_options=options)


@pytest.mark.parametrize("label,start,end", [
    ("22nd September – 28th September 2026", date(2026, 9, 22), date(2026, 9, 28)),
    ("22 Sep - 28 Sep 2026", date(2026, 9, 22), date(2026, 9, 28)),
    ("22/09/2026 - 28/09/2026", date(2026, 9, 22), date(2026, 9, 28)),
    ("September 22-28", date(2026, 9, 22), date(2026, 9, 28)),
    ("29 Sep – 5 Oct 2026", date(2026, 9, 29), date(2026, 10, 5)),
    ("29th September to 5th October 2026", date(2026, 9, 29), date(2026, 10, 5)),
    ("29 Dec – 4 Jan 2027", date(2026, 12, 29), date(2027, 1, 4)),
])
def test_full_range_parser_is_conservative(label, start, end):
    assert menu_dates.parse_week_date_range(label, today=date(2026, 9, 27)) == (start, end)
    assert menu_dates.parse_week_date_range("31 September – 5 October 2026") is None
    assert menu_dates.parse_week_date_range("5 October – 3 October 2026") is None
    assert menu_dates.parse_week_date_range("unreadable date") is None


def test_confirmed_current_and_future_versions_transition_without_admin_open(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    before = weekly_env.read_bytes()
    current = _draft("22 Sep - 28 Sep 2026", 850)
    assert current["weekly_dates"] == {"valid_from": "2026-09-22", "valid_until": "2026-09-28", "date_source": "TEXT_PARSED"}
    assert menu_review.current_versions() == []
    with pytest.raises(ValueError, match="Confirm the Weekly Savoury"):
        menu_review.publish_approved_menu(weekly_savoury.WEEKLY_KEY, current["draft_id"], current["revision"], "admin")
    first = _publish(current)
    assert first["status"] == "PUBLISHED"
    assert weekly_savoury.current_version()["id"] == first["version_id"]
    upcoming = _draft("29 Sep - 5 Oct 2026", 900)
    second = _publish(upcoming)
    assert weekly_savoury.version_status(db.get_menu_version(second["version_id"])) == "UPCOMING"
    assert weekly_savoury.current_version()["id"] == first["version_id"]
    assert [v["id"] for v in menu_review.current_versions() if v["menu_key"] in weekly_savoury.LEGACY_WEEKLY_KEYS] == [first["version_id"]]
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 29))
    assert weekly_savoury.current_version()["id"] == second["version_id"]
    assert menu_review.canonical_menu()["weekly_menus"][-1]["vegetarian"][0]["price_inr"] == 900
    assert weekly_env.read_bytes() == before
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 10, 6))
    assert weekly_savoury.current_version() is None
    assert menu_review.canonical_menu()["weekly_menus"][-1]["is_published"] is False
    assert menu_review.canonical_menu()["weekly_menus"][-1]["vegetarian"] == []


def test_overlap_anomaly_and_undated_require_explicit_confirmation(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    first = _publish(_draft("29 Sep - 5 Oct 2026", 650))
    overlapping = _draft("3 Oct - 9 Oct 2026", 6500)
    analysis = menu_review.weekly_analysis(overlapping)
    assert analysis["overlaps"][0]["id"] == first["version_id"]
    assert analysis["changes"]["suspicious_price"]
    with pytest.raises(menu_review.ReviewConflict):
        _publish(overlapping, confirm_suspicious=True)
    with pytest.raises(ValueError, match="Unusual price"):
        _publish(overlapping, replace_version_id=first["version_id"])
    replaced = _publish(overlapping, replace_version_id=first["version_id"], confirm_suspicious=True)
    assert weekly_savoury.version_status(db.get_menu_version(first["version_id"])) == "SUPERSEDED"
    assert db.get_menu_version(replaced["version_id"])["valid_from"] == "2026-10-03"
    assert weekly_savoury.version_status(db.get_menu_version(replaced["version_id"])) == "UPCOMING"


def test_undated_requires_operator_acceptance_and_expires(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    no_date = _draft("", 850)
    with pytest.raises(ValueError, match="Date unclear"):
        _publish(no_date)
    accepted = _publish(no_date, accept_undated=True)
    assert db.get_menu_version(accepted["version_id"])["date_provenance"] == "UNDATED_FALLBACK"


def test_manual_dates_and_expired_restore_stay_drafts(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    draft = _draft("", 850)
    published = _publish(draft, valid_from="2026-09-27", valid_until="2026-10-03")
    row = db.get_menu_version(published["version_id"])
    assert row["date_provenance"] == "OPERATOR_EDITED"
    assert row["approval_json"] and json.loads(row["approval_json"])["weekly_dates"]["valid_until"] == "2026-10-03"
    assert menu_review.canonical_menu()["weekly_menus"][-1]["week_of"] == "27 Sep – 3 Oct 2026"
    assert responder._weekly_menu_expired() is False
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 10, 10))
    restored = menu_review.restore_as_draft(weekly_savoury.WEEKLY_KEY, published["version_id"], "admin")
    assert restored["status"] == "NEEDS_REVIEW"
    with pytest.raises(ValueError, match="already expired"):
        _publish(restored)
    refreshed = _publish(restored, valid_from="2026-10-10", valid_until="2026-10-16")
    assert db.get_menu_version(refreshed["version_id"])["date_provenance"] == "OPERATOR_EDITED"


def test_future_overlap_cannot_retire_current_week_early(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    live = _publish(_draft("22 Sep - 28 Sep 2026", 650))
    future = _draft("28 Sep - 4 Oct 2026", 850)
    with pytest.raises(menu_review.ReviewConflict, match="cannot retire the current"):
        _publish(future, replace_version_id=live["version_id"])
    assert weekly_savoury.current_version()["id"] == live["version_id"]
    assert menu_review.canonical_menu()["weekly_menus"][-1]["vegetarian"][0]["price_inr"] == 650
    with pytest.raises(ValueError, match="Restore the approved Weekly Savoury"):
        menu_images.archive(weekly_savoury.WEEKLY_KEY)


def test_unique_alias_uses_one_current_approved_price(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    draft = _draft("22 Sep - 28 Sep 2026", 850, "Kosha Mangsho")
    rows = copy.deepcopy(draft["rows"])
    rows[0]["aliases"] = ["Mutton Kosha", "Mangsho"]
    edited = menu_review.edit(weekly_savoury.WEEKLY_KEY, draft["draft_id"], draft["revision"], rows, "admin")
    published = _publish(edited)
    result = rag_fallback_engine.retrieve_approved_menu("How much is Mutton Kosha this week?")
    assert result.auto_response == "Kosha Mangsho 500g is ₹850."
    assert f"version:{published['version_id']}" in result.source_refs[0]
    assert len(menu_review.current_versions()) == 1
    assert len(json.loads(db.get_menu_version(published["version_id"])["approval_json"])["rows"]) == 1


def test_quantity_change_is_not_price_ocr_anomaly(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    _publish(_draft("22 Sep - 28 Sep 2026", 650))
    next_draft = _draft("29 Sep - 5 Oct 2026", 6500)
    rows = copy.deepcopy(next_draft["rows"])
    rows[0]["quantity"] = "5kg"
    edited = menu_review.edit(weekly_savoury.WEEKLY_KEY, next_draft["draft_id"], next_draft["revision"], rows, "admin")
    changes = menu_review.weekly_analysis(edited)["changes"]
    assert len(changes["quantity_changed"]) == 1
    assert changes["price_changed"] == []
    assert changes["suspicious_price"] == []
    assert len(changes["suspicious_quantity"]) == 1


def test_change_summary_normal_price_and_alias_collision(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    _publish(_draft("22 Sep - 28 Sep 2026", 650))
    next_draft = _draft("29 Sep - 5 Oct 2026", 700)
    rows = copy.deepcopy(next_draft["rows"])
    rows.append({**copy.deepcopy(rows[0]), "row_id": "another-row", "item": "Fish Fry",
                 "price_inr": 450, "aliases": []})
    edited = menu_review.edit(weekly_savoury.WEEKLY_KEY, next_draft["draft_id"], next_draft["revision"], rows, "admin")
    changes = menu_review.weekly_analysis(edited)["changes"]
    assert len(changes["price_changed"]) == 1
    assert changes["suspicious_price"] == []
    assert len(changes["new"]) == 1
    published = _publish(edited)
    assert published["status"] == "PUBLISHED"
    colliding = _draft("6 Oct - 12 Oct 2026", 720, "Kosha Mangsho")
    ambiguous = copy.deepcopy(colliding["rows"])
    ambiguous[0]["aliases"] = ["Fish Fry"]
    ambiguous.append({**copy.deepcopy(ambiguous[0]), "row_id": "third-row", "item": "Fish Fry",
                      "aliases": []})
    edited = menu_review.edit(weekly_savoury.WEEKLY_KEY, colliding["draft_id"],
                              colliding["revision"], ambiguous, "admin")
    assert any("alias" in error for error in menu_review.weekly_analysis(edited)["errors"])
    with pytest.raises(ValueError, match="alias"):
        _publish(edited)


@pytest.mark.parametrize("start,end", [("2026-10-03", "2026-09-27"), ("2026-09-27", "bad")])
def test_invalid_operator_dates_cannot_publish(weekly_env, monkeypatch, start, end):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    draft = _draft("", 850)
    with pytest.raises(ValueError):
        _publish(draft, valid_from=start, valid_until=end)
    assert weekly_savoury.lifecycle_versions() == []


def test_bento_catalogue_facts_cannot_be_replaced_by_weekly_upload(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    draft = _draft("22 Sep - 28 Sep 2026", 850)
    rows = copy.deepcopy(draft["rows"])
    rows[0]["category"] = "bengali_bento"
    edited = menu_review.edit(weekly_savoury.WEEKLY_KEY, draft["draft_id"], draft["revision"], rows, "admin")
    assert any("Bengali Bento belongs in Menu Catalogue" in error
               for error in menu_review.weekly_analysis(edited)["errors"])
    with pytest.raises(ValueError, match="Bengali Bento"):
        _publish(edited)


def test_unavailable_weekly_price_never_uses_expired_or_upcoming(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    _publish(_draft("29 Sep - 5 Oct 2026", 850))
    assert rag_fallback_engine.retrieve_approved_menu("How much is Chicken Kosha this week?").auto_response == ""
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 30))
    assert "₹850" in rag_fallback_engine.retrieve_approved_menu("How much is Chicken Kosha this week?").auto_response
    assert rag_fallback_engine.retrieve_approved_menu("Is Chicken Kosha available right now?").auto_response == ""
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 10, 6))
    result = rag_fallback_engine.retrieve_approved_menu("How much is Chicken Kosha this week?")
    assert result.auto_response == ""
    assert result.proposed_intent == "approved_menu"
    reply, image = responder.render_approved_template("weekly_menu_veg")
    assert "finalising this week's menu" in reply.lower()
    assert "₹850" not in reply
    assert image is None


def test_admin_weekly_api_requires_auth_and_preview_does_not_send(weekly_env, monkeypatch):
    from camellia.admin_menu_review import weekly_preview
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    client = TestClient(app)
    assert client.get("/admin/weekly-savoury").status_code in (401, 403)
    draft = _draft("29 Sep - 5 Oct 2026", 850)
    with db.get_connection() as conn:
        before = conn.execute("SELECT COUNT(*) FROM instagram_outbound_audit").fetchone()[0]
    result = weekly_preview({"draft_id": draft["draft_id"], "revision": draft["revision"],
                             "question": "How much is Chicken Kosha?"}, actor="admin")
    assert result["label"] == "Preview only — not sent to customers."
    assert result["grounded"] is True
    assert result["upcoming"] is True
    assert result["answer"] == "Chicken Kosha 500g is ₹850."
    unsupported = weekly_preview({"draft_id": draft["draft_id"], "revision": draft["revision"],
                                  "question": "Can I order Chicken Kosha for delivery?"}, actor="admin")
    assert unsupported["grounded"] is False
    assert "₹850" not in unsupported["answer"]
    list_preview = weekly_preview({"draft_id": draft["draft_id"], "revision": draft["revision"],
                                   "question": "What's on the weekly savoury menu?"}, actor="admin")
    assert list_preview["grounded"] is True
    assert "Chicken Kosha 500g is ₹850." in list_preview["answer"]
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM instagram_outbound_audit").fetchone()[0] == before


def test_additive_weekly_migration_is_idempotent(weekly_env):
    db.init_db()
    db.init_db()
    with db.get_connection() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(menu_versions)")}
        assert {"valid_from", "valid_until", "date_provenance", "weekly_state", "fallback_until_at"} <= columns
        image_columns = {row[1] for row in conn.execute("PRAGMA table_info(menu_image_versions)")}
        assert "review_version_id" in image_columns
        assert conn.execute("SELECT COUNT(*) FROM menu_versions").fetchone()[0] == 0


def test_tombstoned_legacy_catalogue_key_does_not_break_weekly_timeline(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    with db.get_connection() as conn:
        conn.execute("DELETE FROM menu_catalogue WHERE menu_id=?", (weekly_savoury.WEEKLY_KEY,))
        conn.execute("INSERT INTO menu_deletion_tombstones(menu_id,deleted_by) VALUES(?,?)",
                     (weekly_savoury.WEEKLY_KEY, "operator"))
        conn.commit()
    db.init_db()
    assert db.get_menu_catalogue_entry(weekly_savoury.WEEKLY_KEY) is None
    client = TestClient(app)
    response = client.get("/admin/weekly-savoury", headers={"X-Admin-Key": config.ADMIN_API_KEY})
    assert response.status_code == 200
    assert response.json()["current"]["status"] == "CURRENT"
    draft = _draft("22 Sep - 28 Sep 2026", 850)
    approved = _publish(draft)
    assert weekly_savoury.current_version()["id"] == approved["version_id"]
    assert menu_review.current_versions()[-1]["id"] == approved["version_id"]
    assert "₹850" in rag_fallback_engine.retrieve_approved_menu(
        "How much is Chicken Kosha this week?").auto_response
    assert db.get_menu_catalogue_entry(weekly_savoury.WEEKLY_KEY) is None


def test_current_and_scheduled_flyer_stay_bound_to_their_own_facts(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    source = {"week_of": "22 Sep - 28 Sep 2026", "price": 650}
    def parsed(*_args):
        return {"status": "success", "week_of": source["week_of"], "parsed": {
            "vegetarian": [{"item": "Chicken Kosha", "quantity": "500g", "price_inr": source["price"], "description": ""}]}}
    monkeypatch.setattr(menu_review.menu_parser, "parse_image_and_update_menu", parsed)
    def photo(color):
        buffer = io.BytesIO()
        Image.new("RGB", (120, 160), color).save(buffer, format="PNG")
        return buffer.getvalue()
    current = menu_review.extract(weekly_savoury.WEEKLY_KEY, "admin", data=photo("red"), mime="image/png", weekly=True)
    first = _publish(current)
    first_version = db.get_menu_version(first["version_id"])
    first_url = json.loads(first_version["approval_json"])["image_path"]
    assert first_url == menu_images.current_weekly_url()
    source.update(week_of="29 Sep - 5 Oct 2026", price=850)
    future = menu_review.extract(weekly_savoury.WEEKLY_KEY, "admin", data=photo("blue"), mime="image/png", weekly=True)
    second = _publish(future)
    second_version = db.get_menu_version(second["version_id"])
    second_url = json.loads(second_version["approval_json"])["image_path"]
    assert first_url != second_url
    assert menu_images.current_weekly_url() == first_url
    assert menu_review.canonical_menu()["weekly_menus"][-1]["vegetarian"][0]["price_inr"] == 650
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 29))
    assert menu_images.current_weekly_url() == second_url
    assert menu_review.canonical_menu()["weekly_menus"][-1]["vegetarian"][0]["price_inr"] == 850
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 10, 6))
    assert menu_images.current_weekly_url() is None


def test_order_price_cache_follows_business_date_without_menu_json_write(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    _publish(_draft("22 Sep - 28 Sep 2026", 650))
    _publish(_draft("29 Sep - 5 Oct 2026", 850))
    order_calculator._weekly_cache["mtime"] = None
    assert order_calculator.weekly_meal_entries()["chicken kosha"]["price"] == 650
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 29))
    assert order_calculator.weekly_meal_entries()["chicken kosha"]["price"] == 850
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 10, 6))
    assert "chicken kosha" not in order_calculator.weekly_meal_entries()


def test_weekly_pdf_keeps_raster_flyer_and_printed_dates(weekly_env, monkeypatch):
    import pypdfium2 as pdfium
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    monkeypatch.setattr(pdfium.PdfTextPage, "get_text_range", lambda self: "22 Sep - 28 Sep 2026\nVeg:\nChicken Kosha 500g Rs650")
    buffer = io.BytesIO()
    Image.new("RGB", (200, 300), "white").save(buffer, format="PDF")
    draft = menu_review.extract(weekly_savoury.WEEKLY_KEY, "admin", data=buffer.getvalue(),
                                mime="application/pdf", filename="weekly.pdf", weekly=True)
    assert draft["status"] == "NEEDS_REVIEW"
    assert draft["weekly_dates"]["valid_until"] == "2026-09-28"
    assert draft["source_asset"].endswith(".pdf")
    assert draft["flyer_asset"].endswith(".png")
    assert draft["rows"][0]["provenance"]["price_inr"] == "TEXT_PARSED"
    published = _publish(draft)
    approval = json.loads(db.get_menu_version(published["version_id"])["approval_json"])
    assert approval["image_path"].endswith(".png")
    assert menu_images.current_weekly_url() == approval["image_path"]


def test_future_reviewed_week_does_not_hide_still_valid_legacy_week(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    assert weekly_savoury.legacy_baseline_current()
    _publish(_draft("29 Sep - 5 Oct 2026", 850))
    week = menu_review.canonical_menu()["weekly_menus"][-1]
    assert week["week_of"] == "22nd September - 28th September, 2026"
    from camellia.admin_menu_review import _weekly_view
    view = _weekly_view()
    assert view["current"]["display_date_range"] == "22 Sep – 28 Sep 2026"
    assert view["upcoming"][0]["display_date_range"] == "29 Sep – 5 Oct 2026"
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 29))
    assert not weekly_savoury.legacy_baseline_current()
    assert menu_review.canonical_menu()["weekly_menus"][-1]["vegetarian"][0]["price_inr"] == 850


@pytest.mark.parametrize("end,upcoming,expected", [
    ("2026-09-29", False, None),
    ("2026-09-27", False, "expires within 24 hours"),
    ("2026-09-27", True, "Next menu is already scheduled"),
])
def test_admin_expiry_reminder_uses_live_version_and_upcoming(weekly_env, monkeypatch, end, upcoming, expected):
    import camellia.admin_menu_review as admin_weekly
    class Noon(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 27, 12, tzinfo=tz or timezone.utc)
    monkeypatch.setattr(admin_weekly, "datetime", Noon)
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    _publish(_draft("", 650), valid_from="2026-09-27", valid_until=end)
    if upcoming:
        _publish(_draft("", 700), valid_from="2026-09-28", valid_until="2026-10-04")
    view = admin_weekly._weekly_view()
    assert (expected in view["reminder"]) if expected else view["reminder"] is None
    assert bool(view["upcoming"]) is upcoming


def test_weekly_admin_panel_has_all_safe_input_and_confirmation_paths():
    assert 'Weekly Savoury Menu' in WEEKLY_HTML
    for label in ("Drop this week's menu here", "Paste image", "application/pdf",
                  "Paste Menu Text", "Valid from", "Valid until", "Needs Review",
                  "Preview only — not sent to customers.", "Update Weekly Menu",
                  "Restore previous weekly menu"):
        assert label in WEEKLY_HTML + WEEKLY_SCRIPT
    for handler in ("dragover", "drop", "clipboardData", "wsExtractFile", "wsExtractText",
                    "wsSaveCorrections", "wsPreview", "wsPublish", "wsRestore"):
        assert handler in WEEKLY_SCRIPT
    assert "send_instagram" not in WEEKLY_SCRIPT
    assert "wa.me" not in WEEKLY_SCRIPT
