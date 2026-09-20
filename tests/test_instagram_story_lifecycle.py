"""Phase 3C-A.1 contract tests: Story lifecycle reconciliation.

Covers automatic ACTIVE -> REMOVED / EXPIRED reconciliation without ever
hard-deleting a row, and the two-successful-miss safety rule that protects
against a single flaky Meta response wrongly hiding a live Story.
"""

from unittest.mock import patch

import pytest

import camellia.instagram_story_sync as sync_module
from camellia.config import config
from camellia.database import Database


STORY_ID = "18371255653212260"
YOUNG_PUBLISHED_AT = "2026-09-17T08:00:00+00:00"
OLD_PUBLISHED_AT = "2020-01-01T00:00:00+00:00"
EXPIRED_BEFORE = "2026-09-16T00:00:00+00:00"


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses, calls):
        self.responses = responses
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, params=None):
        self.calls.append((url, params or {}))
        return self.responses.pop(0)


@pytest.fixture
def sync_db(tmp_path):
    return Database(str(tmp_path / "story-lifecycle.db"))


def _page():
    return FakeResponse(200, {"instagram_business_account": {"id": "ig-account-1"}})


def _stories(items):
    return FakeResponse(200, {"data": items})


def _story(story_id=STORY_ID, timestamp=YOUNG_PUBLISHED_AT):
    return {
        "id": story_id,
        "media_type": "IMAGE",
        "media_url": "https://cdn.example/story.jpg",
        "thumbnail_url": "https://cdn.example/story-thumb.jpg",
        "permalink": "https://www.instagram.com/stories/camellia/123/",
        "timestamp": timestamp,
    }


def _install_meta(monkeypatch, responses):
    calls = []
    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", "PAGE_TOKEN")
    monkeypatch.setattr(config, "INSTAGRAM_PAGE_ID", "page-1")
    monkeypatch.setattr(config, "INSTAGRAM_API_VERSION", "v25.0")
    monkeypatch.setattr(
        sync_module.httpx,
        "Client",
        lambda timeout: FakeClient(responses, calls),
    )
    return calls


# --- 1-2: a returned Story stays ACTIVE and resets its miss count ----------

def test_returned_story_remains_active(sync_db, monkeypatch):
    _install_meta(monkeypatch, [_page(), _stories([_story()])])
    result = sync_module.sync_instagram_stories(sync_db)
    assert result["status"] == "success"
    assert sync_db.get_instagram_story_source(STORY_ID)["story_status"] == "ACTIVE"


def test_returned_story_resets_miss_count(sync_db, monkeypatch):
    _install_meta(monkeypatch, [_page(), _stories([_story()])])
    sync_module.sync_instagram_stories(sync_db)
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert sync_db.get_instagram_story_source(STORY_ID)["sync_miss_count"] == 1

    _install_meta(monkeypatch, [_page(), _stories([_story()])])
    sync_module.sync_instagram_stories(sync_db)
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["sync_miss_count"] == 0
    assert row["story_status"] == "ACTIVE"


# --- 3-4: two-successful-miss rule -----------------------------------------

def test_one_complete_missing_sync_does_not_remove_story(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    counts = sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert counts == {"expired": 0, "removed": 0, "miss_pending": 1}
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["story_status"] == "ACTIVE"
    assert row["sync_miss_count"] == 1


def test_second_consecutive_complete_missing_sync_marks_young_story_removed(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    counts = sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert counts == {"expired": 0, "removed": 1, "miss_pending": 0}
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["story_status"] == "REMOVED"
    assert row["removed_reason"] == "missing_from_two_complete_meta_syncs"
    assert row["removed_at"]


# --- 5: naturally expired missing Story becomes EXPIRED, not REMOVED -------

def test_naturally_expired_missing_story_becomes_expired(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=OLD_PUBLISHED_AT)
    counts = sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert counts == {"expired": 1, "removed": 0, "miss_pending": 0}
    assert sync_db.get_instagram_story_source(STORY_ID)["story_status"] == "EXPIRED"


# --- 6-10: failures must never count as a miss -----------------------------

def test_failed_meta_api_request_does_not_increment_misses(sync_db, monkeypatch):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    _install_meta(monkeypatch, [
        _page(), FakeResponse(503, {"error": {"message": "temporarily unavailable"}}),
    ])
    result = sync_module.sync_instagram_stories(sync_db)
    assert result["status"] == "failed"
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["sync_miss_count"] == 0
    assert row["story_status"] == "ACTIVE"


def test_rate_limited_response_does_not_increment_misses(sync_db, monkeypatch):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    _install_meta(monkeypatch, [
        _page(), FakeResponse(429, {"error": {"message": "rate limited"}}),
    ])
    result = sync_module.sync_instagram_stories(sync_db)
    assert result["failure"]["category"] == "rate_limited"
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["sync_miss_count"] == 0


@pytest.mark.parametrize("payload", [{"data": {}}, {"unexpected": []}, []])
def test_malformed_response_does_not_increment_misses(sync_db, monkeypatch, payload):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    _install_meta(monkeypatch, [_page(), FakeResponse(200, payload)])
    result = sync_module.sync_instagram_stories(sync_db)
    assert result["failure"]["category"] == "malformed_meta_response"
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["sync_miss_count"] == 0


def test_partial_malformed_story_page_does_not_increment_misses(sync_db, monkeypatch):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    _install_meta(monkeypatch, [_page(), _stories([{"no_id": "oops"}])])
    result = sync_module.sync_instagram_stories(sync_db)
    assert result["status"] == "partial"
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["sync_miss_count"] == 0
    assert row["story_status"] == "ACTIVE"


def test_lock_busy_run_does_not_increment_misses(sync_db, monkeypatch):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", "PAGE_TOKEN")
    with patch.object(sync_db, "try_acquire_instagram_story_sync_lock", return_value=False):
        result = sync_module.sync_instagram_stories(sync_db)
    assert result["status"] == "busy"
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["sync_miss_count"] == 0


def test_application_exception_does_not_increment_misses(sync_db, monkeypatch):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", "PAGE_TOKEN")
    monkeypatch.setattr(config, "INSTAGRAM_PAGE_ID", "page-1")

    class RaisingClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            raise ValueError("boom")

    monkeypatch.setattr(sync_module.httpx, "Client", lambda timeout: RaisingClient())
    result = sync_module.sync_instagram_stories(sync_db)
    assert result["status"] == "failed"
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["sync_miss_count"] == 0


# --- 11-12: reappearance safety --------------------------------------------

def test_story_reappearing_after_one_miss_resets_state(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert sync_db.get_instagram_story_source(STORY_ID)["sync_miss_count"] == 1

    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["sync_miss_count"] == 0
    assert row["story_status"] == "ACTIVE"


def test_removed_story_reappearing_with_exact_id_reactivates_safely(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert sync_db.get_instagram_story_source(STORY_ID)["story_status"] == "REMOVED"

    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["story_status"] == "ACTIVE"
    assert row["sync_miss_count"] == 0
    # No duplicate row was created for the same exact Story ID.
    assert len(sync_db.list_instagram_story_sources(include_deleted=True)) == 1


# --- 13-14: mapping survives lifecycle transitions -------------------------

def test_mapping_survives_active_to_removed(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    sync_db.save_instagram_source_route({
        "source_type": "story", "media_id": STORY_ID,
        "routing_topic": "Jolbhora", "active": True,
    })
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert sync_db.get_instagram_story_source(STORY_ID)["story_status"] == "REMOVED"
    route = sync_db.get_instagram_source_route(
        sync_db.list_instagram_story_sources(include_deleted=True)[0]["routing_id"]
    )
    assert route["routing_topic"] == "Jolbhora"


def test_mapping_survives_active_to_expired(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=OLD_PUBLISHED_AT)
    route = sync_db.save_instagram_source_route({
        "source_type": "story", "media_id": STORY_ID,
        "routing_topic": "Jolbhora", "active": True,
    })
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert sync_db.get_instagram_story_source(STORY_ID)["story_status"] == "EXPIRED"
    assert sync_db.get_instagram_source_route(route["routing_id"])["routing_topic"] == "Jolbhora"


# --- 15: admin-deleted Story is never silently restored by sync -----------

def test_admin_deleted_story_never_silently_restored_by_sync(sync_db, monkeypatch):
    _install_meta(monkeypatch, [_page(), _stories([_story()])])
    sync_module.sync_instagram_stories(sync_db)
    sync_db.delete_instagram_story_source(STORY_ID, removed_by="admin")

    _install_meta(monkeypatch, [_page(), _stories([_story()])])
    sync_module.sync_instagram_stories(sync_db)

    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["admin_deleted"] == 1
    assert STORY_ID not in {item["story_media_id"] for item in sync_db.list_instagram_story_sources()}


# --- 16-17: History retention, no hard deletes -----------------------------

def test_removed_and_expired_remain_in_history(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    ids = {item["story_media_id"] for item in sync_db.list_instagram_story_sources(include_deleted=True)}
    assert STORY_ID in ids


def test_no_database_hard_delete_occurs(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    sync_db.delete_instagram_story_source(STORY_ID, removed_by="admin")
    with sync_db.get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM instagram_story_sources WHERE story_media_id=?", (STORY_ID,)
        ).fetchone()[0]
    assert count == 1


# --- Unknown-age safety correction ------------------------------------------
#
# REMOVED is a claim that a Story went missing *before* its normal expiry.
# That claim requires knowing the Story's age. A missing or malformed
# published_at means age is unknown, so the Story must stay ACTIVE forever —
# never EXPIRED, never REMOVED — no matter how many complete misses pile up.

def test_missing_published_at_first_miss_stays_active(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID)  # no published_at at all
    counts = sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert counts == {"expired": 0, "removed": 0, "miss_pending": 1}
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["story_status"] == "ACTIVE"
    assert row["sync_miss_count"] == 1


def test_missing_published_at_second_miss_still_active(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID)
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    counts = sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert counts == {"expired": 0, "removed": 0, "miss_pending": 1}
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["story_status"] == "ACTIVE"
    assert row["sync_miss_count"] == 2


def test_missing_published_at_many_misses_never_removed_or_expired(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID)
    for _ in range(10):
        counts = sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
        assert counts["removed"] == 0
        assert counts["expired"] == 0
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["story_status"] == "ACTIVE"
    assert row["sync_miss_count"] == 10
    assert row["removed_at"] is None


@pytest.mark.parametrize("bad_value", ["not-a-date", "2026-13-99", "", "   ", "yesterday"])
def test_malformed_published_at_never_removed_or_expired(sync_db, bad_value):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=bad_value)
    for _ in range(3):
        counts = sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
        assert counts["removed"] == 0
        assert counts["expired"] == 0
    assert sync_db.get_instagram_story_source(STORY_ID)["story_status"] == "ACTIVE"


def test_valid_young_timestamp_two_misses_removed(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    counts = sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert counts["removed"] == 1
    assert sync_db.get_instagram_story_source(STORY_ID)["story_status"] == "REMOVED"


def test_valid_old_timestamp_expired(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=OLD_PUBLISHED_AT)
    counts = sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert counts["expired"] == 1
    assert sync_db.get_instagram_story_source(STORY_ID)["story_status"] == "EXPIRED"


def test_z_and_offset_utc_timestamps_classify_identically(sync_db):
    sync_db.upsert_instagram_story_sync("z-story", published_at="2020-01-01T00:00:00Z")
    sync_db.upsert_instagram_story_sync("offset-story", published_at="2020-01-01T00:00:00+00:00")
    sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert sync_db.get_instagram_story_source("z-story")["story_status"] == "EXPIRED"
    assert sync_db.get_instagram_story_source("offset-story")["story_status"] == "EXPIRED"


def test_timezone_offset_compares_by_instant_not_lexical_string(sync_db):
    # 2026-09-16T04:00:00+05:30 is 2026-09-15T22:30:00 UTC — before the
    # 2026-09-16T00:00:00+00:00 cutoff — so it must classify EXPIRED, even
    # though the raw string "2026-09-16T04:00:00+05:30" sorts lexically
    # *after* "2026-09-16T00:00:00+00:00" and a naive string comparison
    # would have kept it ACTIVE.
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at="2026-09-16T04:00:00+05:30")
    counts = sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert counts["expired"] == 1
    assert sync_db.get_instagram_story_source(STORY_ID)["story_status"] == "EXPIRED"


def test_negative_timezone_offset_compares_by_instant(sync_db):
    # 2026-09-15T20:00:00-04:00 is 2026-09-16T00:00:00 UTC and is exactly
    # at the cutoff, so it is trustworthy and naturally EXPIRED.
    sync_db.upsert_instagram_story_sync(
        STORY_ID, published_at="2026-09-15T20:00:00-04:00",
    )
    counts = sync_db.reconcile_instagram_story_lifecycle(
        [], expired_before=EXPIRED_BEFORE,
    )
    assert counts["expired"] == 1
    assert sync_db.get_instagram_story_source(STORY_ID)["story_status"] == "EXPIRED"


def test_timezone_naive_published_at_is_unknown_age(sync_db):
    sync_db.upsert_instagram_story_sync(
        STORY_ID, published_at="2026-09-17T08:00:00",
    )
    for _ in range(3):
        counts = sync_db.reconcile_instagram_story_lifecycle(
            [], expired_before=EXPIRED_BEFORE,
        )
        assert counts["expired"] == 0
        assert counts["removed"] == 0
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["story_status"] == "ACTIVE"
    assert row["sync_miss_count"] == 3


def test_unknown_age_lifecycle_does_not_touch_mapping(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID)
    route = sync_db.save_instagram_source_route({
        "source_type": "story", "media_id": STORY_ID,
        "routing_topic": "Jolbhora", "active": True,
    })
    for _ in range(5):
        sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    assert sync_db.get_instagram_source_route(route["routing_id"])["routing_topic"] == "Jolbhora"


def test_unknown_age_lifecycle_does_not_touch_admin_tombstone(sync_db, monkeypatch):
    _install_meta(monkeypatch, [_page(), _stories([_story()])])
    sync_module.sync_instagram_stories(sync_db)
    with sync_db.get_connection() as conn:
        conn.execute(
            "UPDATE instagram_story_sources SET published_at=NULL WHERE story_media_id=?",
            (STORY_ID,),
        )
        conn.commit()
    sync_db.delete_instagram_story_source(STORY_ID, removed_by="admin")
    for _ in range(3):
        sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    row = sync_db.get_instagram_story_source(STORY_ID)
    assert row["admin_deleted"] == 1


def test_unknown_age_lifecycle_does_not_touch_sequence_numbering(sync_db):
    sync_db.upsert_instagram_story_sync(STORY_ID, published_at=YOUNG_PUBLISHED_AT)
    sync_db.backfill_instagram_story_sequence_numbers()
    before = sync_db.get_instagram_story_source(STORY_ID)["story_sequence_number"]
    sync_db.upsert_instagram_story_sync("unknown-age-story")  # no published_at
    for _ in range(3):
        sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    sync_db.backfill_instagram_story_sequence_numbers()
    assert sync_db.get_instagram_story_source(STORY_ID)["story_sequence_number"] == before
    assert sync_db.get_instagram_story_source("unknown-age-story")["story_sequence_number"] is None


def test_unknown_age_lifecycle_does_not_touch_exact_id_routing(sync_db, monkeypatch):
    from camellia.instagram_routing import route_for_inbound

    sync_db.upsert_instagram_story_sync(STORY_ID)  # no published_at
    sync_db.save_instagram_source_route({
        "source_type": "story", "media_id": STORY_ID,
        "routing_topic": "Jolbhora", "active": True,
    })
    for _ in range(3):
        sync_db.reconcile_instagram_story_lifecycle([], expired_before=EXPIRED_BEFORE)
    monkeypatch.setattr("camellia.database.db", sync_db)

    route, method = route_for_inbound(ad_id=None, media_id=STORY_ID, source_type="story_reply")

    assert route["media_id"] == STORY_ID
    assert method == "story_media_id"
