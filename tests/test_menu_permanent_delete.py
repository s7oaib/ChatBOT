"""Regression coverage for irreversible Menu Catalogue deletion."""

import io

import pytest
from PIL import Image

from camellia import app as app_module
from camellia import database as db_module
from camellia import instagram_routing, menu_catalogue, menu_images, menu_manager, responder as responder_module
from camellia.database import Database


def _png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def deletion_db(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "menu-delete.db"))
    uploads = tmp_path / "uploads"
    for module in (db_module, app_module, instagram_routing, menu_catalogue,
                   menu_images, menu_manager, responder_module):
        monkeypatch.setattr(module, "db", database, raising=False)
    monkeypatch.setattr(menu_images.weekly_flyer, "uploads_dir", lambda: str(uploads))
    return database


def test_custom_hard_delete_removes_catalogue_assets_drafts_and_history(deletion_db):
    entry = menu_catalogue.create_menu("Seasonal Test Menu", "Event", "Temporary test menu.")
    menu_id = entry["menu_id"]

    first = menu_images.save_draft(menu_id, _png(), "image/png", "tester")
    published = menu_images.publish(menu_id)
    second = menu_images.save_draft(menu_id, _png("blue"), "image/png", "tester")
    menu_images.publish(menu_id)
    deletion_db.save_menu_draft(menu_id, "[{\"item\": \"stale draft\"}]")
    deletion_db.record_menu_version(menu_id, "[{\"item\": \"stale history\"}]")

    first_path = menu_images._dir() + "/" + first["image_path"].rsplit("/", 1)[-1]
    published_path = menu_images._dir() + "/" + published["image_path"].rsplit("/", 1)[-1]
    second_path = menu_images._dir() + "/" + second["image_path"].rsplit("/", 1)[-1]
    assert all(__import__("os").path.exists(path) for path in (first_path, published_path, second_path))

    result = menu_catalogue.permanently_delete_menu(menu_id, deleted_by="tester")

    assert result["deleted"] is True
    assert deletion_db.get_menu_catalogue_entry(menu_id) is None
    assert deletion_db.is_menu_deleted(menu_id) is True
    with deletion_db.get_connection() as conn:
        assert conn.execute("SELECT 1 FROM menu_image_versions WHERE menu_key = ?", (menu_id,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM menu_drafts WHERE menu_key = ?", (menu_id,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM menu_versions WHERE menu_key = ?", (menu_id,)).fetchone() is None
    assert menu_images.current_url(menu_id) is None
    assert result["image_files_removed"] == 2
    assert not any(__import__("os").path.exists(path) for path in (first_path, published_path, second_path))


def test_archived_menu_restores_until_it_is_hard_deleted(deletion_db):
    entry = menu_catalogue.create_menu("Restorable Test Menu", "Event", "Can be archived.")
    menu_id = entry["menu_id"]
    menu_images.save_draft(menu_id, _png(), "image/png", "tester")
    published = menu_images.publish(menu_id)
    archived = menu_images.archive(menu_id)
    assert menu_images.restore(menu_id, archived["id"])["status"] == "current"

    menu_images.archive(menu_id)
    menu_catalogue.permanently_delete_menu(menu_id, deleted_by="tester")
    assert deletion_db.get_menu_catalogue_entry(menu_id) is None
    with pytest.raises(KeyError):
        menu_images.restore(menu_id, published["id"])


def test_weekly_vegetarian_hard_delete_is_not_reseeded_after_restart(deletion_db):
    menu_images.save_draft("weekly_vegetarian", _png(), "image/png", "tester")
    menu_images.publish("weekly_vegetarian")
    menu_catalogue.permanently_delete_menu("weekly_vegetarian", deleted_by="tester")

    restarted = Database(deletion_db.db_path)
    assert restarted.get_menu_catalogue_entry("weekly_vegetarian") is None
    assert restarted.is_menu_deleted("weekly_vegetarian") is True
    assert menu_images.current_url("weekly_vegetarian", "/images/fallback.jpeg") is None
    text, images = responder_module.responder._format_dynamic_response(
        "weekly_menu_veg", "approved weekly copy", user_message="vegetarian menu"
    )
    assert not images
    assert "Manager of Kitchens" in text


def test_menu_manager_key_alias_deletes_the_matching_catalogue_menu(deletion_db):
    assert deletion_db.get_menu_catalogue_entry("weekly_vegetarian") is not None

    menu_catalogue.permanently_delete_menu("weekly.vegetarian", deleted_by="tester")

    assert deletion_db.get_menu_catalogue_entry("weekly_vegetarian") is None
    assert deletion_db.is_menu_deleted("weekly_vegetarian") is True


def test_active_story_post_reel_and_ad_references_block_deletion(deletion_db):
    entry = menu_catalogue.create_menu("Routed Test Menu", "Event", "Still routed.")
    menu_id = entry["menu_id"]
    for source_type, media_id in (("story", "story-delete-ref"), ("post", "post-delete-ref"),
                                  ("reel", "reel-delete-ref"), ("ad", "ad-delete-ref")):
        deletion_db.save_instagram_source_route({
            "source_type": source_type,
            "media_id": media_id,
            "ad_id": media_id if source_type == "ad" else None,
            "menu_id": menu_id,
            "menu_mode": "SELECTED",
            "active": True,
            "routing_topic": f"{source_type} test route",
        })

    with pytest.raises(menu_catalogue.MenuCatalogueInUseError) as caught:
        menu_catalogue.permanently_delete_menu(menu_id)

    assert len(caught.value.references) == 4
    assert deletion_db.get_menu_catalogue_entry(menu_id) is not None
    assert all(route["menu_id"] == menu_id for route in deletion_db.list_instagram_source_routes())


def test_inactive_references_are_cleared_without_leaving_dangling_ids(deletion_db):
    entry = menu_catalogue.create_menu("Inactive Routed Menu", "Event", "No active references.")
    menu_id = entry["menu_id"]
    topic = deletion_db.create_instagram_topic("inactive_delete_topic", "Inactive delete topic", "desc", menu_id, [], [], active=False)
    route = deletion_db.save_instagram_source_route({
        "source_type": "post", "media_id": "inactive-delete-ref", "menu_id": menu_id,
        "menu_mode": "SELECTED", "active": False,
    })

    menu_catalogue.permanently_delete_menu(menu_id)

    assert deletion_db.get_instagram_topic(topic["topic_id"])["default_menu_id"] is None
    assert deletion_db.get_instagram_source_route(route["routing_id"])["menu_id"] is None
    assert deletion_db.get_instagram_source_route(route["routing_id"])["menu_mode"] == "NONE"
