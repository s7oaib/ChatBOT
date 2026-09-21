"""
The menu photo and the dish text are two separate outcomes of one upload.

The vision model fails on perfectly good flyers — a malformed-JSON completion
is enough — and the photo used to be saved only when it succeeded. One bad
model run therefore made the whole upload do nothing: the operator posted this
week's menu, got a "failed" toast, and customers kept being sent last week's.
The photo is what customers actually read, so it no longer depends on the
reader succeeding.
"""
import json
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from camellia.config import config
from camellia import weekly_flyer
from camellia.menu_parser import menu_parser
import camellia.database as db_module
from camellia.app import app

db_module.db.init_db()
client = TestClient(app)


TEST_ADMIN_API_KEY = "test_admin_api_key_decouple_long_enough"
ADMIN = {"x-admin-key": TEST_ADMIN_API_KEY}

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


@pytest.fixture(autouse=True)
def isolated(tmp_path):
    original = config.MENU_JSON_PATH
    original_key = config.ADMIN_API_KEY
    with open(original, "r", encoding="utf-8") as f:
        (tmp_path / "menu.json").write_text(f.read(), encoding="utf-8")
    config.MENU_JSON_PATH = str(tmp_path / "menu.json")
    config.ADMIN_API_KEY = TEST_ADMIN_API_KEY
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
    return resp


def test_a_failed_reader_still_puts_the_photo_in_front_of_customers():
    resp = _upload({"status": "failed", "error": "json_validate_failed"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["flyer_saved"] is True
    assert weekly_flyer.get() is not None, "the upload did nothing at all"


def test_hiding_current_menu_removes_its_flyer_and_stops_customer_menu_delivery():
    _upload({"status": "failed", "error": "unreadable"})
    assert weekly_flyer.get() is not None

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
    _upload({"status": "failed", "error": "first"})
    first_url = weekly_flyer.get()["url"]
    _upload({"status": "failed", "error": "second"})
    second_url = weekly_flyer.get()["url"]

    assert first_url != second_url
    assert second_url.startswith("/uploads/weekly_flyer-")
    assert second_url.endswith(".jpeg")


def test_the_operator_is_told_the_photo_went_live_anyway():
    """A bare "failed" reads as "nothing happened" and sends them re-uploading."""
    body = _upload({"status": "failed", "error": "json_validate_failed"}).json()

    notice = body.get("flyer_notice") or ""
    assert "photo is live" in notice
    assert "previous menu" in notice


def test_an_unreadable_date_gets_a_generic_caption_not_a_wrong_one():
    _upload({"status": "failed", "error": "json_validate_failed"})

    state = weekly_flyer.get()
    assert state["week_of"] is None
    assert state["caption"] in weekly_flyer.CAPTIONS, (
        "a caption with an empty or invented date slot reads as broken"
    )


def test_a_successful_read_still_captions_with_the_printed_week():
    _upload({
        "status": "success",
        "week_of": "2nd September - 8th September, 2026",
        "service_window": "Friday Lunch - Thursday Dinner",
    })

    state = weekly_flyer.get()
    assert state["week_of"] == "2nd September - 8th September, 2026"
    assert "2nd September - 8th September, 2026" in state["caption"]
    assert "flyer_notice" not in _upload({
        "status": "success", "week_of": "2nd September - 8th September, 2026",
    }).json(), "a clean upload must not carry a degraded-mode notice"


def test_a_failed_read_is_still_archived_so_it_can_be_shown_later():
    _upload({"status": "failed", "error": "boom"})
    _upload({"status": "success", "week_of": "2nd September - 8th September, 2026"})

    archived = weekly_flyer.list_archive()
    assert len(archived) == 1, "the superseded photo was lost because its text failed"


def test_a_flyer_save_failure_is_reported_rather_than_silently_swallowed():
    with mock.patch.object(weekly_flyer, "save", side_effect=OSError("disk full")):
        body = _upload({"status": "failed", "error": "boom"}).json()

    assert body["flyer_saved"] is False
    assert "flyer_notice" not in body, "must not claim the photo is live when it is not"


def test_a_stale_dish_list_does_not_make_the_uploaded_photo_current():
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
    assert url is None, "an expired configured menu must not send an uploaded photo"


def test_an_undated_flyer_stops_being_shown_once_it_is_stale():
    """Without a printed date the upload time is what says how old it is."""
    import time

    _upload({"status": "failed", "error": "boom"})
    state = weekly_flyer.get()
    assert weekly_flyer.is_current(state) is True

    eight_days_on = time.time() + 8 * 86400
    assert weekly_flyer.is_current(state, now=eight_days_on) is False


def test_a_printed_date_still_beats_the_upload_time():
    """A flyer whose own printed week has ended is never shown, however fresh."""
    _upload({"status": "success", "week_of": "19, 20, 21 JUNE"})

    state = weekly_flyer.get()
    assert state["week_of"] == "19, 20, 21 JUNE"
    assert weekly_flyer.is_current(state) is False, (
        "a just-uploaded flyer printed with a finished week must not be sent"
    )
