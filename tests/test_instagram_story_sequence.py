"""Phase 3C-A.1 contract tests: Story sequence numbering.

The sequence is an admin-facing convenience label only ("2nd Story"),
derived strictly from published_at (Asia/Kolkata publication date) with
story_media_id as a deterministic tie-breaker. It never participates in
routing and, once assigned, is never renumbered.
"""

import pytest

import camellia.instagram_story_sync as sync_module
from camellia.config import config
from camellia.database import (
    Database,
    format_story_sequence_date_display,
    format_story_sequence_label,
)
from camellia.instagram_routing import route_for_inbound


@pytest.fixture
def sync_db(tmp_path):
    return Database(str(tmp_path / "story-sequence.db"))


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = responses

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, params=None):
        return self.responses.pop(0)


def _page():
    return FakeResponse(200, {"instagram_business_account": {"id": "ig-account-1"}})


def _stories(items):
    return FakeResponse(200, {"data": items})


def _story(story_id, timestamp):
    return {
        "id": story_id, "media_type": "IMAGE",
        "media_url": "https://cdn.example/s.jpg",
        "thumbnail_url": "https://cdn.example/s-thumb.jpg",
        "permalink": "https://www.instagram.com/stories/camellia/1/",
        "timestamp": timestamp,
    }


def _install_meta(monkeypatch, responses):
    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", "PAGE_TOKEN")
    monkeypatch.setattr(config, "INSTAGRAM_AUTH_MODE", "legacy_page")
    monkeypatch.setattr(config, "INSTAGRAM_PAGE_ID", "page-1")
    monkeypatch.setattr(config, "INSTAGRAM_API_VERSION", "v25.0")
    monkeypatch.setattr(sync_module.httpx, "Client", lambda timeout: FakeClient(responses))


# --- 1-3: ordering ignores Meta's array order, uses published_at + ID tie-break

def test_api_response_order_is_irrelevant(sync_db, monkeypatch):
    # Returned newest-first on purpose; sequence must still be oldest-first.
    # Both timestamps stay within the same Asia/Kolkata publication date
    # (UTC+5:30) so this isolates ordering, not the date-reset behavior.
    _install_meta(monkeypatch, [_page(), _stories([
        _story("s-late", "2026-09-17T14:00:00+00:00"),
        _story("s-early", "2026-09-17T08:00:00+00:00"),
    ])])
    sync_module.sync_instagram_stories(sync_db)
    early = sync_db.get_instagram_story_source("s-early")
    late = sync_db.get_instagram_story_source("s-late")
    assert early["story_sequence_number"] == 1
    assert late["story_sequence_number"] == 2


def test_published_timestamp_determines_sequence(sync_db):
    sync_db.upsert_instagram_story_sync("s-mid", published_at="2026-09-17T12:00:00+00:00")
    sync_db.upsert_instagram_story_sync("s-first", published_at="2026-09-17T06:00:00+00:00")
    sync_db.backfill_instagram_story_sequence_numbers()
    assert sync_db.get_instagram_story_source("s-first")["story_sequence_number"] == 1
    assert sync_db.get_instagram_story_source("s-mid")["story_sequence_number"] == 2


def test_ids_break_equal_timestamp_ties_deterministically(sync_db):
    same_ts = "2026-09-17T12:00:00+00:00"
    sync_db.upsert_instagram_story_sync("b-story", published_at=same_ts)
    sync_db.upsert_instagram_story_sync("a-story", published_at=same_ts)
    sync_db.backfill_instagram_story_sequence_numbers()
    # story_media_id "a-story" < "b-story" lexicographically -> tie-break first.
    assert sync_db.get_instagram_story_source("a-story")["story_sequence_number"] == 1
    assert sync_db.get_instagram_story_source("b-story")["story_sequence_number"] == 2


# --- 4: ordinal suffixes -----------------------------------------------------

@pytest.mark.parametrize("number,label", [
    (1, "1st Story"), (2, "2nd Story"), (3, "3rd Story"), (4, "4th Story"),
    (11, "11th Story"), (12, "12th Story"), (13, "13th Story"), (21, "21st Story"),
])
def test_sequence_labels(number, label):
    assert format_story_sequence_label(number) == label


# --- 5: resets by Asia/Kolkata publication date -----------------------------

def test_sequence_resets_by_kolkata_publication_date(sync_db):
    # 23:30 UTC on the 17th is already the 18th in Asia/Kolkata (+05:30).
    sync_db.upsert_instagram_story_sync("s-17-a", published_at="2026-09-17T08:00:00+00:00")
    sync_db.upsert_instagram_story_sync("s-17-b", published_at="2026-09-17T10:00:00+00:00")
    sync_db.upsert_instagram_story_sync("s-18-a", published_at="2026-09-17T23:30:00+00:00")
    sync_db.backfill_instagram_story_sequence_numbers()
    assert sync_db.get_instagram_story_source("s-17-a")["story_sequence_number"] == 1
    assert sync_db.get_instagram_story_source("s-17-b")["story_sequence_number"] == 2
    row_18 = sync_db.get_instagram_story_source("s-18-a")
    assert row_18["story_sequence_number"] == 1
    assert row_18["story_sequence_date"] == "2026-09-18"


# --- 6-7: stability under removal, next-number assignment for later Stories

def test_removing_second_story_does_not_renumber_third(sync_db):
    sync_db.upsert_instagram_story_sync("s1", published_at="2026-09-17T06:00:00+00:00")
    sync_db.upsert_instagram_story_sync("s2", published_at="2026-09-17T09:00:00+00:00")
    sync_db.upsert_instagram_story_sync("s3", published_at="2026-09-17T12:00:00+00:00")
    sync_db.backfill_instagram_story_sequence_numbers()
    assert sync_db.get_instagram_story_source("s3")["story_sequence_number"] == 3

    sync_db.reconcile_instagram_story_lifecycle(
        ["s1", "s3"], expired_before="2026-09-16T00:00:00+00:00",
    )
    sync_db.reconcile_instagram_story_lifecycle(
        ["s1", "s3"], expired_before="2026-09-16T00:00:00+00:00",
    )
    assert sync_db.get_instagram_story_source("s2")["story_status"] == "REMOVED"
    assert sync_db.get_instagram_story_source("s1")["story_sequence_number"] == 1
    assert sync_db.get_instagram_story_source("s3")["story_sequence_number"] == 3


def test_new_later_story_receives_next_number(sync_db):
    sync_db.upsert_instagram_story_sync("s1", published_at="2026-09-17T06:00:00+00:00")
    sync_db.backfill_instagram_story_sequence_numbers()
    sync_db.upsert_instagram_story_sync("s2", published_at="2026-09-17T18:00:00+00:00")
    sync_db.backfill_instagram_story_sequence_numbers()
    assert sync_db.get_instagram_story_source("s2")["story_sequence_number"] == 2


# --- 8: repeated sync does not change assigned sequence ---------------------

def test_repeated_sync_does_not_change_assigned_sequence(sync_db, monkeypatch):
    _install_meta(monkeypatch, [_page(), _stories([_story("s1", "2026-09-17T06:00:00+00:00")])])
    sync_module.sync_instagram_stories(sync_db)
    first_number = sync_db.get_instagram_story_source("s1")["story_sequence_number"]
    assert first_number == 1

    _install_meta(monkeypatch, [_page(), _stories([_story("s1", "2026-09-17T06:00:00+00:00")])])
    sync_module.sync_instagram_stories(sync_db)
    assert sync_db.get_instagram_story_source("s1")["story_sequence_number"] == first_number


# --- 9: mapping does not affect sequence ------------------------------------

def test_mapping_does_not_affect_sequence(sync_db):
    sync_db.upsert_instagram_story_sync("s1", published_at="2026-09-17T06:00:00+00:00")
    sync_db.upsert_instagram_story_sync("s2", published_at="2026-09-17T09:00:00+00:00")
    sync_db.backfill_instagram_story_sequence_numbers()
    before = sync_db.get_instagram_story_source("s2")["story_sequence_number"]

    sync_db.save_instagram_source_route({
        "source_type": "story", "media_id": "s1",
        "routing_topic": "Jolbhora", "active": True,
    })
    sync_db.backfill_instagram_story_sequence_numbers()
    assert sync_db.get_instagram_story_source("s2")["story_sequence_number"] == before


# --- 10: Story ID remains routing identity, sequence never participates ----

def test_story_id_remains_routing_identity_not_sequence(sync_db, monkeypatch):
    story_id = "18371255653212260"
    sync_db.upsert_instagram_story_sync(story_id, published_at="2026-09-17T06:00:00+00:00")
    sync_db.backfill_instagram_story_sequence_numbers()
    sync_db.save_instagram_source_route({
        "source_type": "story", "media_id": story_id,
        "routing_topic": "Jolbhora", "active": True,
    })
    monkeypatch.setattr("camellia.database.db", sync_db)

    route, method = route_for_inbound(ad_id=None, media_id=story_id, source_type="story_reply")

    assert route["media_id"] == story_id
    assert method == "story_media_id"
    # The routing match is keyed only by the exact media ID.
    assert route["media_id"] == story_id
    assert method == "story_media_id"


# --- 11: legacy existing rows can be safely backfilled ----------------------

def test_legacy_existing_rows_can_be_safely_backfilled(sync_db):
    sync_db.upsert_instagram_story_sync("legacy1", published_at="2026-09-17T06:00:00+00:00")
    route = sync_db.save_instagram_source_route({
        "source_type": "story", "media_id": "legacy1",
        "routing_topic": "Jolbhora", "active": True,
    })
    # Simulate a pre-existing DB that predates sequence numbering (columns
    # exist but numbering was never assigned).
    with sync_db.get_connection() as conn:
        conn.execute(
            "UPDATE instagram_story_sources SET story_sequence_number=NULL, story_sequence_date=NULL "
            "WHERE story_media_id='legacy1'"
        )
        conn.commit()

    updated = sync_db.backfill_instagram_story_sequence_numbers()

    assert updated == 1
    row = sync_db.get_instagram_story_source("legacy1")
    assert row["story_sequence_number"] == 1
    preserved_route = sync_db.get_instagram_source_route(route["routing_id"])
    assert preserved_route["routing_topic"] == "Jolbhora"
    assert preserved_route["media_id"] == "legacy1"


# --- 12: missing published timestamp is not guessed -------------------------

def test_missing_published_timestamp_is_not_guessed(sync_db):
    sync_db.record_instagram_story_source("webhook-only-story")
    sync_db.backfill_instagram_story_sequence_numbers()
    row = sync_db.get_instagram_story_source("webhook-only-story")
    assert row["story_sequence_number"] is None
    assert format_story_sequence_date_display(row.get("story_sequence_date")) is None


def test_timezone_naive_published_timestamp_is_not_guessed(sync_db):
    sync_db.upsert_instagram_story_sync(
        "naive-story", published_at="2026-09-17T08:00:00",
    )
    sync_db.backfill_instagram_story_sequence_numbers()
    row = sync_db.get_instagram_story_source("naive-story")
    assert row["story_sequence_number"] is None
    assert row["story_sequence_date"] is None
