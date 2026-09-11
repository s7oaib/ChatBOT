import io

import pytest
from PIL import Image

from camellia import menu_images
from camellia.database import Database


def _png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def image_store(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "menu.db"))
    monkeypatch.setattr(menu_images, "db", database)
    monkeypatch.setattr(menu_images.weekly_flyer, "uploads_dir", lambda: str(tmp_path / "uploads"))
    return database


def test_image_lifecycle_is_draft_publish_archive_restore(image_store):
    draft = menu_images.save_draft("aamar_mishti", _png(), "image/png", "tester")
    assert draft["status"] == "draft"
    current = menu_images.publish("aamar_mishti")
    assert current["status"] == "current"
    archived = menu_images.archive("aamar_mishti")
    assert archived["status"] == "archived"
    restored = menu_images.restore("aamar_mishti", archived["id"])
    assert restored["status"] == "current"
    assert menu_images.current_url("aamar_mishti") == archived["image_path"]


def test_invalid_upload_is_rejected(image_store):
    with pytest.raises(ValueError):
        menu_images.save_draft("aamar_mishti", b"not-an-image", "image/png")


def test_each_supported_menu_type_is_listed(image_store):
    keys = {entry["menu_key"] for entry in menu_images.list_menus()}
    assert keys == {"aamar_mishti", "signature", "weekly_vegetarian", "weekly_non_vegetarian", "bento"}


def test_chatbot_picks_up_a_published_image_with_no_restart(image_store, monkeypatch):
    """
    Publishing through Menu Manager must be visible to the responder on the
    very next lookup — no process restart, no redeploy. Restoring an older
    archived version must flip the chatbot's answer back just as immediately.
    """
    from camellia import responder as responder_module

    monkeypatch.setattr(responder_module, "db", image_store)

    # Before any publish, the responder falls back to the built-in default.
    assert responder_module.current_menu_image("aamar_mishti", "/images/AAMAR%20MISHTI.jpeg") == "/images/AAMAR%20MISHTI.jpeg"

    menu_images.save_draft("aamar_mishti", _png(), "image/png", "tester")
    first = menu_images.publish("aamar_mishti")
    assert responder_module.current_menu_image("aamar_mishti", "/images/AAMAR%20MISHTI.jpeg") == first["image_path"]

    # A second publish supersedes the first, and the lookup reflects it at once.
    menu_images.save_draft("aamar_mishti", _png("blue"), "image/png", "tester")
    second = menu_images.publish("aamar_mishti")
    assert second["image_path"] != first["image_path"]
    assert responder_module.current_menu_image("aamar_mishti", "/images/AAMAR%20MISHTI.jpeg") == second["image_path"]

    # Restoring the first (now archived) version immediately restores it as
    # the chatbot's answer too — no code edit, no redeploy.
    restored = menu_images.restore("aamar_mishti", first["id"])
    assert responder_module.current_menu_image("aamar_mishti", "/images/AAMAR%20MISHTI.jpeg") == restored["image_path"] == first["image_path"]


def test_weekly_vegetarian_publish_overrides_the_flyer_rotation(image_store, monkeypatch):
    """The weekly-menu card path (_current_weekly_image) must prefer a
    Menu Manager-published image over the day-of-week static rotation."""
    from camellia import responder as responder_module

    monkeypatch.setattr(responder_module.db, "get_menu_image_current", image_store.get_menu_image_current)
    monkeypatch.setattr(responder_module.db, "has_menu_image_archive", image_store.has_menu_image_archive)

    menu_images.save_draft("weekly_vegetarian", _png(), "image/png", "tester")
    published = menu_images.publish("weekly_vegetarian")

    bot = responder_module.DeterministicResponder.__new__(responder_module.DeterministicResponder)
    image_url, caption = bot._current_weekly_image(today_weekday=0)
    assert image_url == published["image_path"]
    assert caption is None
