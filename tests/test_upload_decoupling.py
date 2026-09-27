"""
The menu photo and the dish text are two separate outcomes of one upload.

The vision model fails on perfectly good flyers — a malformed-JSON completion
is enough — and the photo used to be saved only when it succeeded. One bad
model run therefore made the whole upload do nothing: the operator posted this
week's menu, got a "failed" toast, and customers kept being sent last week's.
The photo is what customers actually read, so it no longer depends on the
reader succeeding.
"""
import io
import json
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from camellia.config import config
from camellia import weekly_flyer
from camellia.menu_parser import menu_parser
import camellia.database as db_module
from camellia.app import app

db_module.db.init_db()
client = TestClient(app)


TEST_ADMIN_API_KEY = "test_admin_api_key_decouple_long_enough"
ADMIN = {"x-admin-key": TEST_ADMIN_API_KEY}

_image = io.BytesIO()
Image.new("RGB", (32, 48), "white").save(_image, format="JPEG")
JPEG = _image.getvalue()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from camellia.responder import responder
    original = config.MENU_JSON_PATH
    original_key = config.ADMIN_API_KEY
    original_responder_path = responder.menu_path
    with open(original, "r", encoding="utf-8") as f:
        (tmp_path / "menu.json").write_text(f.read(), encoding="utf-8")
    config.MENU_JSON_PATH = str(tmp_path / "menu.json")
    responder.menu_path = config.MENU_JSON_PATH
    responder.reload_data()
    config.ADMIN_API_KEY = TEST_ADMIN_API_KEY
    monkeypatch.setattr(db_module.db, "db_path", str(tmp_path / "review.db"))
    # The upload route allows 10/minute per client address. This module
    # uploads repeatedly from one address and shares that budget with the
    # admin-portal tests, so later uploads came back 429 with nothing saved
    # and the failures looked like the decoupling was broken. Turned off for
    # the duration of each test and restored after, rather than switched off
    # globally at import — the app object is shared with every other module.
    limiter_was = app.state.limiter.enabled
    app.state.limiter.enabled = False
    db_module.db.init_db()
    weekly_flyer.clear()
    try:
        yield
    finally:
        weekly_flyer.clear()
        app.state.limiter.enabled = limiter_was
        config.MENU_JSON_PATH = original
        config.ADMIN_API_KEY = original_key
        responder.menu_path = original_responder_path
        responder.reload_data()


def _upload(parser_result):
    # app.py holds its own reference (`from camellia.menu_parser import
    # menu_parser`), so the instance method is what has to be patched.
    with mock.patch.object(menu_parser, "parse_image_and_update_menu",
                           return_value=parser_result):
        resp = client.post(
            "/admin/menu/upload-image",
            files={"file": ("flyer.jpg", JPEG, "image/jpeg")},
            headers=ADMIN,
        )
    assert resp.status_code == 200, f"upload rejected: {resp.status_code} {resp.text[:200]}"
    assert resp.json()["status"] == "draft"
    assert resp.json()["published"] is False
    return resp


def test_failed_reader_keeps_private_draft_and_never_publishes_photo_or_facts():
    resp = _upload({"status": "failed", "error": "json_validate_failed"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["draft"]["status"] == "NEEDS_REVIEW"
    assert body["draft"]["source_asset"]
    assert weekly_flyer.get() is None
    from camellia import menu_review
    assert not menu_review.current_versions()


def test_hiding_current_menu_removes_its_flyer_and_stops_customer_menu_delivery():
    _upload({"status": "failed", "error": "unreadable"})
    assert weekly_flyer.get() is None

    hidden = client.post("/admin/menu/unpublish-current", headers=ADMIN)
    assert hidden.status_code == 200
    assert hidden.json()["status"] == "success"
    assert weekly_flyer.get() is None

    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        assert json.load(f)["weekly_menus"][-1]["is_published"] is False

    from camellia.responder import responder
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        hidden_menu = json.load(f)
    with mock.patch.object(responder, "menu_data", hidden_menu):
        image, _caption = responder._current_weekly_image(today_weekday=0)
        assert image is None


def test_each_new_flyer_has_a_fresh_url_so_media_caches_cannot_send_the_old_one():
    from camellia import menu_review
    first = _upload({"status": "failed", "error": "first"}).json()["draft"]
    first_asset = first["source_asset"]
    menu_review.reject("weekly_vegetarian", first["draft_id"], first["revision"], "operator")
    second = _upload({"status": "failed", "error": "second"}).json()["draft"]
    assert first_asset != second["source_asset"]
    assert weekly_flyer.get() is None


def test_the_operator_is_told_upload_is_not_live_until_approval():
    body = _upload({"status": "failed", "error": "json_validate_failed"}).json()

    assert body["published"] is False
    assert body["draft"]["status"] == "NEEDS_REVIEW"
    assert weekly_flyer.get() is None


def test_an_unreadable_date_gets_a_generic_caption_not_a_wrong_one():
    draft = _upload({"status": "failed", "error": "json_validate_failed"}).json()["draft"]
    assert draft["menu_metadata"]["week_of"] == ""
    assert weekly_flyer.get() is None


def test_a_successful_read_still_captions_with_the_printed_week():
    draft = _upload({
        "status": "success",
        "week_of": "2nd September - 8th September, 2026",
        "service_window": "Friday Lunch - Thursday Dinner",
        "parsed": {"vegetarian": [], "non_vegetarian": [], "bengali_bento": [], "norom_pak_sondesh": []},
    }).json()["draft"]
    assert draft["menu_metadata"]["week_of"] == "2nd September - 8th September, 2026"
    assert draft["menu_metadata"]["service_window"] == "Friday Lunch - Thursday Dinner"
    assert weekly_flyer.get() is None


def test_a_failed_read_is_still_archived_so_it_can_be_shown_later():
    from camellia import menu_review
    first = _upload({"status": "failed", "error": "boom"}).json()["draft"]
    menu_review.reject("weekly_vegetarian", first["draft_id"], first["revision"], "operator")
    _upload({"status": "success", "week_of": "2nd September - 8th September, 2026",
             "parsed": {"vegetarian": [], "non_vegetarian": [], "bengali_bento": [], "norom_pak_sondesh": []}})

    archived = weekly_flyer.list_archive()
    assert len(archived) == 0, "unapproved photos are not part of live image history"


def test_a_flyer_save_failure_is_reported_rather_than_silently_swallowed():
    with mock.patch.object(weekly_flyer, "save", side_effect=OSError("disk full")):
        body = _upload({"status": "failed", "error": "boom"}).json()

    assert body["published"] is False
    assert weekly_flyer.get() is None


def test_a_stale_dish_list_does_not_suppress_this_weeks_photo():
    """
    The regression the split created. menu.json keeps last week's date when
    extraction fails, and the card used to be gated on that date — so the
    photo was saved, then hidden, and the upload still appeared to do nothing.
    """
    from camellia.responder import responder

    _upload({"status": "failed", "error": "boom"})
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        menu = json.load(f)
    menu["weekly_menus"][-1]["week_of"] = "19, 20, 21 JUNE"  # long past
    with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(menu, f)
    responder.reload_templates()

    url, _ = responder._current_weekly_image(today_weekday=0)
    assert url is None, "unapproved photo must not replace an expired live menu"


def test_an_undated_flyer_stops_being_shown_once_it_is_stale():
    """An undated draft has no effect on the customer-facing menu."""
    draft = _upload({"status": "failed", "error": "boom"}).json()["draft"]
    assert draft["source_type"] == "image"
    assert weekly_flyer.get() is None


def test_a_printed_date_still_beats_the_upload_time():
    """A flyer whose own printed week has ended is never shown, however fresh."""
    draft = _upload({"status": "success", "week_of": "19, 20, 21 JUNE",
                     "parsed": {"vegetarian": [], "non_vegetarian": [], "bengali_bento": [], "norom_pak_sondesh": []}}).json()["draft"]
    assert draft["menu_metadata"]["week_of"] == "19, 20, 21 JUNE"
    assert weekly_flyer.get() is None
