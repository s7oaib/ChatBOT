"""
The two-week menu archive.

Every upload becomes the live flyer and is also kept as a browsable copy, so a
customer who saw last week's menu can still be shown the thing they are asking
about. Copies age out after two weeks; the live one is never offered as a
"previous" menu.
"""
import itertools
import json
import os
import time
from unittest import mock

import pytest

from camellia.config import config
from camellia import weekly_flyer
from camellia.responder import responder
from camellia.session_memory import session_memory

_ids = itertools.count()


@pytest.fixture(autouse=True)
def isolated_uploads(tmp_path):
    """
    Points the durable-volume path at a temp dir for the duration of a test.

    weekly_flyer resolves its directories from MENU_JSON_PATH on every call, so
    redirecting that is enough to keep real uploads out of the way — and keeps
    a failing test from deleting the operator's actual flyer.
    """
    original = config.MENU_JSON_PATH
    menu_copy = tmp_path / "menu.json"
    with open(original, "r", encoding="utf-8") as src:
        menu_copy.write_text(src.read(), encoding="utf-8")
    config.MENU_JSON_PATH = str(menu_copy)
    weekly_flyer.clear()
    try:
        yield
    finally:
        weekly_flyer.clear()
        config.MENU_JSON_PATH = original


def _upload(week_of, age_days=0, payload=b"\xff\xd8\xffX"):
    """Saves a flyer and backdates it, to stand in for an upload N days ago."""
    state = weekly_flyer.save(payload, "image/jpeg", week_of=week_of)
    if age_days:
        entries = weekly_flyer._read_archive()
        entries[-1]["uploaded_at"] = time.time() - age_days * 86400
        weekly_flyer._write_archive(entries)
    return state


def _ask(message):
    uid = f"archive_user_{next(_ids)}"
    session_memory.clear_session(uid, "whatsapp")
    return responder.match_and_respond(uid=uid, message=message) if False else \
        responder.match_and_respond(message, user_id=uid, channel="whatsapp")


def test_each_upload_is_archived_and_the_newest_is_the_live_one():
    _upload("21st August - 27th August, 2026", age_days=7)
    _upload("28th August - 3rd September, 2026")

    assert weekly_flyer.get()["week_of"] == "28th August - 3rd September, 2026"
    archived = weekly_flyer.list_archive()
    assert [e["week_of"] for e in archived] == ["21st August - 27th August, 2026"], (
        "the live flyer must not be offered as a previous menu"
    )


def test_two_uploads_in_the_same_second_do_not_overwrite_each_other():
    """
    The live flyer and archive both need immutable names. A fixed live URL can
    make browsers or Meta reuse the prior week's cached image.
    """
    first = _upload("week one")
    second = _upload("week two")

    assert first["id"] != second["id"]
    assert len(os.listdir(weekly_flyer.archive_dir())) == 2


def test_archived_menus_are_dropped_after_two_weeks():
    _upload("7th August - 13th August, 2026", age_days=21)
    _upload("14th August - 20th August, 2026", age_days=15)
    _upload("21st August - 27th August, 2026", age_days=7)
    _upload("28th August - 3rd September, 2026")

    kept = [e["week_of"] for e in weekly_flyer.list_archive()]
    assert kept == ["21st August - 27th August, 2026"]
    # The files go too, not just the records — the volume is small and a
    # forgotten photo is still a photo a stale URL could serve.
    assert len(os.listdir(weekly_flyer.archive_dir())) == 2


def test_retention_is_measured_from_upload_not_from_the_printed_week():
    """An undated flyer must still age out, or it would live forever."""
    _upload(None, age_days=20)
    _upload("28th August - 3rd September, 2026")
    assert weekly_flyer.list_archive() == []


def test_asking_for_previous_menus_sends_the_archived_cards():
    _upload("21st August - 27th August, 2026", age_days=7)
    _upload("28th August - 3rd September, 2026")

    for phrasing in ("show me previous menus", "last week's menu", "do you have old menus"):
        text, matched, tmpl, images = _ask(phrasing)
        assert matched, f"{phrasing!r} dead-ended"
        assert tmpl == "previous_menus", f"{phrasing!r} routed to {tmpl}"
        assert images and len(images) == 1
        assert images[0]["url"].startswith("/uploads/archive/")
        assert "21st August" in images[0]["caption"]
        # The live week must not be handed back as a previous menu.
        assert "28th August" not in (images[0]["caption"] or "")
        assert "past two weeks" in text


def test_asking_for_this_weeks_menu_never_returns_the_archive():
    """
    "this week's menu" and "last week's menu" differ by one word, so trigger
    overlap alone would hand a request for the current menu to the archive.
    """
    _upload("21st August - 27th August, 2026", age_days=7)
    _upload("28th August - 3rd September, 2026")

    for phrasing in ("show me the menu", "menu", "this week's menu",
                     "current menu", "todays menu", "new menu"):
        _, _, tmpl, _ = _ask(phrasing)
        assert tmpl != "previous_menus", f"{phrasing!r} was answered from the archive"


def test_no_archive_means_no_promise_of_one():
    """Telling a customer "here are the past menus" and sending none reads as broken."""
    _upload("28th August - 3rd September, 2026")
    text, matched, tmpl, images = _ask("show me previous menus")

    assert matched and tmpl == "previous_menus"
    assert not images
    assert "don't have any earlier menus" in text


def test_the_menu_reply_offers_the_archive_only_when_one_exists():
    _upload("28th August - 3rd September, 2026")
    text, _, _, _ = _ask("show me the menu")
    assert "Previous menus" not in text, "offered an archive that is empty"

    _upload("21st August - 27th August, 2026", age_days=7)
    _upload("28th August - 3rd September, 2026")
    text, _, _, _ = _ask("show me the menu")
    assert "Previous menus" in text


def test_reset_to_baseline_clears_the_archive_too():
    """Otherwise the dish text resets while old flyers stay browsable."""
    _upload("21st August - 27th August, 2026", age_days=7)
    _upload("28th August - 3rd September, 2026")
    assert weekly_flyer.list_archive()

    weekly_flyer.clear()

    assert weekly_flyer.get() is None
    assert weekly_flyer.list_archive() == []
    assert not os.path.exists(weekly_flyer.archive_dir()) or \
        os.listdir(weekly_flyer.archive_dir()) == []


def test_an_archive_write_failure_never_costs_the_operator_the_upload():
    """The live flyer is the thing that matters; the copy is a nicety."""
    with mock.patch.object(weekly_flyer, "archive_dir", side_effect=OSError("disk full")):
        state = weekly_flyer.save(b"\xff\xd8\xffZ", "image/jpeg", week_of="28th August, 2026")

    assert "/weekly_flyer-" in state["url"]
    assert state["url"].endswith(".jpeg")
    assert weekly_flyer.get() is not None


def test_an_archived_menu_can_be_removed_before_it_ages_out():
    """
    Retention drops these after two weeks, but a menu can be wrong long before
    it is old — a cancelled week, a price since changed. Waiting it out was
    the only option, and customers asking for previous menus kept seeing it.
    """
    weekly_flyer.save(b"\xff\xd8\xffX", "image/jpeg", week_of="1st - 7th July, 2026")
    weekly_flyer.save(b"\xff\xd8\xffX", "image/jpeg", week_of="8th - 14th July, 2026")

    archived = weekly_flyer.list_archive()
    assert archived, "nothing was archived to remove"
    victim = archived[0]

    label = weekly_flyer.remove_from_archive(victim["id"])
    assert label == victim["week_of"]

    remaining = [e["id"] for e in weekly_flyer.list_archive()]
    assert victim["id"] not in remaining
    # The photo goes with the row, not just the listing.
    gone = os.path.join(weekly_flyer.archive_dir(), os.path.basename(victim["url"]))
    assert not os.path.exists(gone)


def test_the_live_menu_cannot_be_deleted_through_the_archive():
    """
    It is not history yet. Deleting it here would leave the bot serving a photo
    whose archive row no longer exists.
    """
    current = weekly_flyer.save(b"\xff\xd8\xffX", "image/jpeg", week_of="15th - 21st July, 2026")
    with pytest.raises(ValueError):
        weekly_flyer.remove_from_archive(current["id"])
    assert weekly_flyer.get()["id"] == current["id"]


def test_removing_a_menu_that_is_already_gone_reports_nothing_removed():
    """A double-click must not read as success."""
    assert weekly_flyer.remove_from_archive("no-such-flyer-id") is None


def test_replacing_an_archived_photo_keeps_its_date_and_its_place():
    """
    For the flyer that was right but photographed wrong. Re-uploading through
    the main form would make it this week's menu instead.
    """
    weekly_flyer.save(b"\xff\xd8\xffX", "image/jpeg", week_of="22nd - 28th July, 2026")
    weekly_flyer.save(b"\xff\xd8\xffX", "image/jpeg", week_of="29th July - 4th August, 2026")

    target = weekly_flyer.list_archive()[0]
    updated = weekly_flyer.replace_archived(target["id"], b"\xff\xd8\xffY", "image/jpeg")

    assert updated["id"] == target["id"]
    assert updated["week_of"] == target["week_of"]
    # Not restarted: the two-week clock must keep running from the original.
    assert updated["uploaded_at"] == target["uploaded_at"]

    listed = {e["id"]: e for e in weekly_flyer.list_archive()}
    assert listed[target["id"]]["url"] == updated["url"]
    assert os.path.exists(
        os.path.join(weekly_flyer.archive_dir(), os.path.basename(updated["url"]))
    )


def test_replacing_a_missing_or_empty_flyer_is_refused():
    weekly_flyer.save(b"\xff\xd8\xffX", "image/jpeg", week_of="5th - 11th August, 2026")
    assert weekly_flyer.replace_archived("no-such-flyer-id", b"\xff\xd8\xffY", "image/jpeg") is None

    archived = weekly_flyer.list_archive()
    if archived:
        with pytest.raises(ValueError):
            weekly_flyer.replace_archived(archived[0]["id"], b"", "image/jpeg")


def test_the_dashboard_offers_remove_and_replace_on_each_archived_menu():
    from fastapi.testclient import TestClient
    from camellia.app import app

    page = TestClient(app).get("/admin").text
    assert "async function removeArchivedFlyer" in page
    assert "function replaceArchivedFlyer" in page
    assert "/admin/menu/archive/" in page
    assert "btn-archive-action" in page
