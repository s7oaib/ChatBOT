"""One-time legacy Story cleanup on the current-only release path."""

from unittest.mock import patch

import pytest

import camellia.app as app_module
import camellia.instagram_story_sync as sync_module
from camellia.config import config
from camellia.database import Database


STALE_TIMESTAMP = "2026-09-22T08:25:36.439647+00:00"


@pytest.fixture
def story_db(tmp_path):
    return Database(str(tmp_path / "story-startup-cleanup.db"))


def _source(database, media_id, *, status="ACTIVE", deleted=0,
            published_at=None, last_seen="2026-09-25T00:00:00+00:00"):
    with database.get_connection() as conn:
        conn.execute(
            "INSERT INTO instagram_story_sources "
            "(story_media_id, story_status, admin_deleted, published_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (media_id, status, deleted, published_at, last_seen),
        )
        conn.commit()


def _route(database, source_type, media_id):
    with database.get_connection() as conn:
        route_id = conn.execute(
            "INSERT INTO instagram_source_routes(source_type, media_id, routing_topic) "
            "VALUES (?, ?, 'Test topic')",
            (source_type, media_id),
        ).lastrowid
        conn.commit()
    return route_id


def _count(database, table, where="1=1", params=()):
    with database.get_connection() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", params,
        ).fetchone()[0]


def test_startup_cleanup_removes_only_legacy_story_data_and_is_idempotent(story_db):
    for media_id, status, deleted in (
        ("expired", "EXPIRED", 0),
        ("removed", "REMOVED", 0),
        ("admin-deleted", "ACTIVE", 1),
    ):
        _source(story_db, media_id, status=status, deleted=deleted)
        route_id = _route(story_db, "story", media_id)
        story_db.save_instagram_route_preview(route_id, "/tmp/story.jpg", "image/jpeg", "test")
        with story_db.get_connection() as conn:
            conn.execute(
                "INSERT INTO instagram_post_pairings(routing_id, expires_at) VALUES (?, '2099-01-01')",
                (route_id,),
            )
            conn.commit()

    _source(story_db, "current")
    current_route = _route(story_db, "story", "current")
    unrelated_routes = [
        _route(story_db, source_type, "expired")
        for source_type in ("post", "reel", "ad")
    ]
    with story_db.get_connection() as conn:
        conn.execute(
            "INSERT INTO instagram_route_removals(routing_id, source_type) VALUES (800, 'story')"
        )
        conn.execute(
            "INSERT INTO instagram_route_removals(routing_id, source_type) VALUES (801, 'post')"
        )
        conn.execute(
            "INSERT INTO sessions(user_id, channel) VALUES ('customer-test', 'instagram')"
        )
        conn.execute(
            "INSERT INTO conversations(user_id, channel, user_message, bot_response) "
            "VALUES ('customer-test', 'instagram', 'hello', 'handoff')"
        )
        conn.execute(
            "INSERT INTO escalations(user_id, channel, user_message, reason) "
            "VALUES ('customer-test', 'instagram', 'hello', 'human')"
        )
        conn.commit()

    retained_counts = {
        table: _count(story_db, table)
        for table in ("sessions", "conversations", "escalations", "menu_catalogue")
    }
    assert retained_counts["menu_catalogue"] > 0

    with patch.object(sync_module.httpx, "Client", side_effect=AssertionError("Meta called")):
        result = story_db.cleanup_legacy_instagram_story_history()

    assert result == {
        "history_rows_found": 3,
        "history_rows_deleted": 3,
        "routes_deleted": 3,
        "story_audit_rows_deleted": 1,
        "stale_matches": 0,
        "stale_deleted": 0,
        "stale_mapping_deleted": 0,
        "noncurrent_remaining": 0,
    }
    assert _count(story_db, "instagram_story_sources") == 1
    assert story_db.get_instagram_story_source("current") is not None
    assert story_db.get_instagram_source_route(current_route) is not None
    assert all(story_db.get_instagram_source_route(route_id) for route_id in unrelated_routes)
    assert _count(story_db, "instagram_source_routes", "source_type='story'") == 1
    assert _count(story_db, "instagram_route_removals", "source_type='story'") == 0
    assert _count(story_db, "instagram_route_removals", "source_type='post'") == 1
    assert _count(story_db, "instagram_route_previews") == 0
    assert _count(story_db, "instagram_post_pairings") == 0
    assert {table: _count(story_db, table) for table in retained_counts} == retained_counts

    again = story_db.cleanup_legacy_instagram_story_history()
    assert all(value == 0 for value in again.values())
    assert _count(story_db, "instagram_story_sources") == 1


@pytest.mark.parametrize("field", ["published_at", "last_seen"])
def test_unique_stale_card_uses_the_timestamp_actually_rendered(story_db, field):
    values = {
        "published_at": STALE_TIMESTAMP if field == "published_at" else None,
        "last_seen": STALE_TIMESTAMP if field == "last_seen" else "2026-09-25T00:00:00+00:00",
    }
    _source(story_db, "stale", **values)
    stale_route = _route(story_db, "story", "stale")

    result = story_db.cleanup_legacy_instagram_story_history()

    assert result["stale_matches"] == 1
    assert result["stale_deleted"] == 1
    assert result["stale_mapping_deleted"] == 1
    assert story_db.get_instagram_story_source("stale") is None
    assert story_db.get_instagram_source_route(stale_route) is None


def test_last_seen_match_is_ignored_when_admin_displays_published_at(story_db):
    _source(
        story_db, "current",
        published_at="2026-09-25T00:00:00+00:00",
        last_seen=STALE_TIMESTAMP,
    )

    result = story_db.cleanup_legacy_instagram_story_history()

    assert result["stale_matches"] == 0
    assert story_db.get_instagram_story_source("current") is not None


def test_ambiguous_stale_card_is_never_guessed(story_db):
    for media_id in ("one", "two"):
        _source(story_db, media_id, published_at=None, last_seen=STALE_TIMESTAMP)
        _route(story_db, "story", media_id)

    result = story_db.cleanup_legacy_instagram_story_history()

    assert result["stale_matches"] == 2
    assert result["stale_deleted"] == 0
    assert result["stale_mapping_deleted"] == 0
    assert _count(story_db, "instagram_story_sources") == 2
    assert _count(story_db, "instagram_source_routes", "source_type='story'") == 2


def test_cleanup_rolls_back_all_story_deletions_if_a_step_fails(story_db):
    for media_id in ("old-one", "old-two"):
        _source(story_db, media_id, status="EXPIRED")
        _route(story_db, "story", media_id)
    with story_db.get_connection() as conn:
        conn.execute(
            "INSERT INTO instagram_route_removals(routing_id, source_type) VALUES (800, 'story')"
        )
        conn.commit()

    delete_story = Database._delete_instagram_story_source_in_connection
    calls = 0

    def fail_on_second(conn, media_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated cleanup interruption")
        return delete_story(conn, media_id)

    with patch.object(Database, "_delete_instagram_story_source_in_connection", new=staticmethod(fail_on_second)):
        with pytest.raises(RuntimeError, match="simulated cleanup interruption"):
            story_db.cleanup_legacy_instagram_story_history()

    assert _count(story_db, "instagram_story_sources") == 2
    assert _count(story_db, "instagram_source_routes", "source_type='story'") == 2
    assert _count(story_db, "instagram_route_removals", "source_type='story'") == 1


@pytest.mark.asyncio
async def test_application_startup_runs_cleanup_and_logs_only_counts(
    story_db, monkeypatch, caplog, tmp_path,
):
    _source(story_db, "private-story", status="EXPIRED")
    monkeypatch.setattr(app_module, "db", story_db)
    monkeypatch.setattr(app_module, "_prune_expired_weekly_menus", lambda: None)
    monkeypatch.setattr(app_module, "migrate_legacy_routing_topics", lambda: None)
    monkeypatch.setattr(app_module.escalator, "process_pending_notifications", lambda **_kwargs: 0)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "story-startup-cleanup.db"))
    monkeypatch.setattr(config, "GOOGLE_SHEET_CSV_URL", "")
    monkeypatch.setattr(config, "INSTAGRAM_STORY_AUTO_SYNC_ENABLED", False)

    async def idle_loop(stop_event):
        await stop_event.wait()

    monkeypatch.setattr(app_module, "_human_notification_loop", idle_loop)
    monkeypatch.setattr(app_module, "_instagram_job_loop", idle_loop)

    with caplog.at_level("INFO", logger="camellia.app"):
        async with app_module.lifespan(app_module.app):
            assert story_db.get_instagram_story_source("private-story") is None

    cleanup_messages = [
        record.getMessage() for record in caplog.records
        if "story_cleanup_history_rows_found=" in record.getMessage()
    ]
    assert len(cleanup_messages) == 1
    assert "story_cleanup_history_rows_found=1" in cleanup_messages[0]
    assert "story_cleanup_noncurrent_remaining=0" in cleanup_messages[0]
    assert "private-story" not in cleanup_messages[0]
