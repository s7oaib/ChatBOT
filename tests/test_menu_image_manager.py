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
