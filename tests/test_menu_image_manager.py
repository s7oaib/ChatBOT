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
def image_store(tmp_path, monkeypatch, current_legacy_baseline):
    database = Database(str(tmp_path / "menu.db"))
    monkeypatch.setattr(menu_images, "db", database)
    from camellia import menu_review, weekly_savoury, menu_catalogue
    for module in (menu_review, weekly_savoury, menu_catalogue):
        monkeypatch.setattr(module, "db", database)
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
    lookup = responder_module.current_menu_image("aamar_mishti", "/images/AAMAR%20MISHTI.jpeg")
    assert lookup == restored["image_path"] == first["image_path"]


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


def test_weekly_resolver_chooses_newest_current_published_record(image_store, monkeypatch):
    """Bento/weekly delivery follows lifecycle time, not catalogue order."""
    menu_images.save_draft("weekly_vegetarian", _png("red"), "image/png", "tester")
    old = menu_images.publish("weekly_vegetarian")
    menu_images.save_draft("weekly_non_vegetarian", _png("blue"), "image/png", "tester")
    newer = menu_images.publish("weekly_non_vegetarian")

    assert menu_images.current_weekly()["image_path"] == newer["image_path"]
    assert menu_images.current_weekly_url("/images/From%20Friday.jpeg") == newer["image_path"]
    assert old["image_path"] != newer["image_path"]

    restarted = Database(image_store.db_path)
    monkeypatch.setattr(menu_images, "db", restarted)
    assert menu_images.current_weekly()["image_path"] == newer["image_path"]


def test_weekly_draft_and_archive_never_replace_customer_asset(image_store):
    """Only a current row can be sent; drafts and archived rows are excluded."""
    menu_images.save_draft("weekly_vegetarian", _png("red"), "image/png", "tester")
    current = menu_images.publish("weekly_vegetarian")
    menu_images.save_draft("weekly_non_vegetarian", _png("blue"), "image/png", "tester")
    assert menu_images.current_weekly_url() == current["image_path"]

    menu_images.archive("weekly_vegetarian")
    assert menu_images.current_weekly() is None
    assert menu_images.current_weekly_url("/images/From%20Friday.jpeg") is None


def test_bento_formatter_preserves_approved_text_and_uses_latest_weekly_image(image_store):
    from camellia import client_qa
    from camellia import responder as responder_module

    menu_images.save_draft("weekly_vegetarian", _png("red"), "image/png", "tester")
    menu_images.publish("weekly_vegetarian")
    menu_images.save_draft("weekly_non_vegetarian", _png("blue"), "image/png", "tester")
    newer = menu_images.publish("weekly_non_vegetarian")

    approved = client_qa.REGISTRY_BY_ID["bento_details_client"].answer
    bot = responder_module.DeterministicResponder.__new__(responder_module.DeterministicResponder)
    rendered, images = bot._format_dynamic_response(
        "bento_details", approved, user_id="bento-test", channel="instagram",
        user_message="tell me about BENGALI BENTO",
    )

    assert rendered == approved
    image_items = images if isinstance(images, list) else [images]
    urls = [item.get("url") if isinstance(item, dict) else item for item in image_items]
    assert newer["image_path"] in urls
