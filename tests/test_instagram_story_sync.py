"""
Active Instagram Story sync (automatic discovery) and the two-consecutive-
successful-sync removal-confirmation lifecycle.

A Story must reach the dedicated Instagram Stories admin tab without any
customer ever replying to it -- purely from an active Meta sync. A Story
discovered by sync and one captured off a webhook reply must converge on
the same row (identity is the exact Meta story_media_id, nothing else). A
Meta failure of any kind (timeout, HTTP error, token/permission error,
malformed or incomplete response) must never remove a Story or its mapping;
only two separate, complete, successful syncs that both miss the same
Story confirm it EXPIRED.
"""
import os

os.environ.setdefault("DB_PATH", "test_camellia.db")

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import camellia.app as app_module
from camellia.database import Database
from camellia import instagram_routing
from camellia.instagram_routing import fetch_active_instagram_stories, run_story_sync


# ---------------------------------------------------------------------------
# Database-level lifecycle
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "story-sync.db"))


def test_meta_sync_creates_a_new_active_unmapped_story_row(db):
    row = db.sync_upsert_story_source("18300000000000001", media_type="STORY")
    assert row["story_media_id"] == "18300000000000001"
    assert row["lifecycle_state"] == "ACTIVE"
    assert row["discovery_source"] == "meta_sync"
    assert row["consecutive_sync_misses"] == 0
    sources = db.list_instagram_story_sources()
    assert [s["story_media_id"] for s in sources] == ["18300000000000001"]
    assert sources[0]["mapping_status"] == "UNMAPPED"


def test_repeated_sync_of_the_same_story_does_not_duplicate(db):
    db.sync_upsert_story_source("18300000000000002")
    db.sync_upsert_story_source("18300000000000002")
    db.sync_upsert_story_source("18300000000000002")
    rows = db.list_instagram_story_sources()
    assert len(rows) == 1


def test_webhook_capture_and_later_meta_sync_converge_on_one_row(db):
    """A customer reply captures the Story first; a sync later observes the
    same exact media id. No duplicate -- same row, same identity."""
    db.record_instagram_story_source("18300000000000003", story_status="ACTIVE", sender_id="fan-1")
    db.sync_upsert_story_source("18300000000000003", media_type="STORY")
    rows = db.list_instagram_story_sources()
    assert len(rows) == 1
    assert rows[0]["last_sender_id"] == "fan-1"  # webhook evidence preserved
    assert rows[0]["discovery_source"] == "webhook"  # first-seen origin is not overwritten


def test_meta_sync_before_any_webhook_reply_also_converges_on_one_row(db):
    """The reverse order: sync discovers it first, a customer replies later."""
    db.sync_upsert_story_source("18300000000000004")
    db.record_instagram_story_source("18300000000000004", story_status="ACTIVE", sender_id="fan-2")
    rows = db.list_instagram_story_sources()
    assert len(rows) == 1
    assert rows[0]["last_sender_id"] == "fan-2"


def test_existing_mapping_survives_a_sync(db):
    db.sync_upsert_story_source("18300000000000005")
    saved = db.save_instagram_source_route({
        "source_type": "story", "canonical_url": None, "shortcode": None,
        "media_id": "18300000000000005", "ad_id": None, "campaign_id": None,
        "routing_topic": "Jolbhora", "knowledge_scope": [], "escalation_policy": "human_if_unsupported",
        "priority_products": [], "active": True, "topic_id": "t1", "menu_id": None,
        "menu_mode": "INHERIT", "preview_url": None, "media_type": "story",
    })
    routing_id = saved["routing_id"]
    db.sync_upsert_story_source("18300000000000005")  # a later sync sighting
    row = db.get_instagram_story_source("18300000000000005")
    assert row["mapping_status"] == "MAPPED"
    assert row["routing_id"] == routing_id


def test_one_successful_missing_sync_does_not_expire(db):
    db.sync_upsert_story_source("18300000000000006")
    expired = db.mark_story_sync_misses([])  # a complete sync that saw nothing
    assert expired == []
    row = db.get_instagram_story_source("18300000000000006")
    assert row["lifecycle_state"] == "ACTIVE"
    assert row["consecutive_sync_misses"] == 1
    assert "18300000000000006" in [s["story_media_id"] for s in db.list_instagram_story_sources()]


def test_second_consecutive_missing_sync_confirms_expiry(db):
    db.sync_upsert_story_source("18300000000000007")
    db.mark_story_sync_misses([])
    expired = db.mark_story_sync_misses([])
    assert expired == ["18300000000000007"]
    row = db.get_instagram_story_source("18300000000000007")
    assert row["lifecycle_state"] == "EXPIRED"
    assert row["expired_at"]


def test_expired_story_disappears_from_default_active_view_but_not_from_history(db):
    db.sync_upsert_story_source("18300000000000008")
    db.mark_story_sync_misses([])
    db.mark_story_sync_misses([])
    active = db.list_instagram_story_sources()
    assert "18300000000000008" not in [s["story_media_id"] for s in active]
    history = db.list_instagram_story_sources(include_expired=True)
    ids = [s["story_media_id"] for s in history]
    assert "18300000000000008" in ids
    row = next(s for s in history if s["story_media_id"] == "18300000000000008")
    assert row["lifecycle_state"] == "EXPIRED"


def test_expired_mapped_story_route_is_archived_not_deleted(db):
    db.sync_upsert_story_source("18300000000000009")
    saved = db.save_instagram_source_route({
        "source_type": "story", "canonical_url": None, "shortcode": None,
        "media_id": "18300000000000009", "ad_id": None, "campaign_id": None,
        "routing_topic": "Kanchagolla", "knowledge_scope": [], "escalation_policy": "human_if_unsupported",
        "priority_products": [], "active": True, "topic_id": "t1", "menu_id": None,
        "menu_mode": "INHERIT", "preview_url": None, "media_type": "story",
    })
    routing_id = saved["routing_id"]
    db.mark_story_sync_misses([])
    db.mark_story_sync_misses([])
    route = db.find_instagram_source_route("media_id", "18300000000000009", source_type="story")
    assert route is None  # archived_at set => no longer an active match
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT archived_at, routing_topic FROM instagram_source_routes WHERE routing_id = ?", (routing_id,)
        ).fetchone()
    assert row["archived_at"] is not None
    assert row["routing_topic"] == "Kanchagolla"  # nothing about the route was destroyed


def test_a_reappearing_story_after_expiry_reactivates_the_same_row_without_duplicating(db):
    db.sync_upsert_story_source("18300000000000010")
    db.mark_story_sync_misses([])
    db.mark_story_sync_misses([])
    assert db.get_instagram_story_source("18300000000000010")["lifecycle_state"] == "EXPIRED"
    assert "18300000000000010" not in [s["story_media_id"] for s in db.list_instagram_story_sources()]

    db.sync_upsert_story_source("18300000000000010")  # Meta shows it active again
    rows = db.list_instagram_story_sources(include_expired=True)
    matching = [r for r in rows if r["story_media_id"] == "18300000000000010"]
    assert len(matching) == 1  # deterministic, no duplicate row
    assert matching[0]["lifecycle_state"] == "ACTIVE"
    assert matching[0]["consecutive_sync_misses"] == 0


def test_a_seen_story_is_never_advanced_as_a_miss(db):
    """mark_story_sync_misses must only touch stories NOT in the seen set."""
    db.sync_upsert_story_source("18300000000000011")
    db.mark_story_sync_misses(["18300000000000011"])  # this sync saw it
    row = db.get_instagram_story_source("18300000000000011")
    assert row["consecutive_sync_misses"] == 0
    assert row["lifecycle_state"] == "ACTIVE"


# ---------------------------------------------------------------------------
# Graph fetch (fetch_active_instagram_stories) -- pure read, token-mode aware
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Client:
    def __init__(self, by_suffix):
        self.by_suffix = by_suffix
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append((url, headers or {}, params or {}))
        for suffix, response in self.by_suffix.items():
            if url.endswith(suffix) or suffix in url:
                return response
        raise AssertionError(f"unexpected URL: {url}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_active_stories_igaa_token_success(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAA-token")
    client = _Client({
        "/me/stories": _Response({"data": [
            {"id": "18400000000000001", "media_type": "IMAGE", "media_product_type": "STORY", "timestamp": "2026-09-15T10:00:00+0000"},
        ]}),
    })
    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **_: client)

    result = fetch_active_instagram_stories()

    assert result["status"] == "success"
    assert result["stories"] == [{
        "id": "18400000000000001", "media_type": "IMAGE",
        "media_product_type": "STORY", "timestamp": "2026-09-15T10:00:00+0000",
    }]
    assert all(c[1].get("Authorization") == "Bearer IGAA-token" for c in client.calls)
    assert all("access_token" not in (c[2] or {}) for c in client.calls)


def test_fetch_active_stories_page_token_resolves_ig_business_account(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "EAAT-page-token")
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_PAGE_ID", "778899")
    client = _Client({
        "/778899": _Response({"instagram_business_account": {"id": "17841400000000000"}}),
        "/17841400000000000/stories": _Response({"data": [
            {"id": "18400000000000002", "media_type": "VIDEO", "media_product_type": "STORY", "timestamp": "2026-09-15T11:00:00+0000"},
        ]}),
    })
    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **_: client)

    result = fetch_active_instagram_stories()

    assert result["status"] == "success"
    assert [s["id"] for s in result["stories"]] == ["18400000000000002"]


def test_fetch_active_stories_customer_reply_not_required(monkeypatch):
    """The whole point of this feature: an active Story is discovered here
    with zero customer messages, zero webhook events."""
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAA-token")
    client = _Client({"/me/stories": _Response({"data": [{"id": "18400000000000003"}]})})
    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **_: client)

    result = fetch_active_instagram_stories()
    assert result["status"] == "success"
    assert result["stories"][0]["id"] == "18400000000000003"


@pytest.mark.parametrize("status_code", [401, 403, 500, 503])
def test_fetch_active_stories_http_errors_are_reported_as_failure_not_empty_success(monkeypatch, status_code):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAA-token")
    client = _Client({"/me/stories": _Response({"error": {"code": 190, "message": "bad"}}, status_code=status_code)})
    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **_: client)

    result = fetch_active_instagram_stories()
    assert result["status"] == "error"
    assert "stories" not in result


def test_fetch_active_stories_malformed_response_is_a_failure(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAA-token")
    client = _Client({"/me/stories": _Response({"unexpected": "shape"})})  # no "data" list
    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **_: client)

    result = fetch_active_instagram_stories()
    assert result["status"] == "error"
    assert result["failure_category"] == "MALFORMED_RESPONSE"


def test_fetch_active_stories_incomplete_pagination_is_a_failure_not_a_partial_success(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAA-token")

    class _PagingClient:
        def __init__(self):
            self.calls = 0

        def get(self, url, headers=None, params=None):
            self.calls += 1
            # Every page (even the last one allowed) still advertises a
            # next page -- more data exists than the bounded fetch can
            # safely retrieve, so this must never be treated as complete.
            return _Response({
                "data": [{"id": f"page-{self.calls}"}],
                "paging": {"next": f"https://graph.instagram.com/v19.0/me/stories?after=page{self.calls}"},
            })

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **_: _PagingClient())

    result = fetch_active_instagram_stories()
    assert result["status"] == "error"
    assert result["failure_category"] == "INCOMPLETE_RESPONSE"


def test_fetch_active_stories_network_exception_is_a_failure(monkeypatch):
    import httpx as httpx_module
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAA-token")

    class _FailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *a, **k):
            raise httpx_module.ConnectTimeout("timed out")

    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **_: _FailingClient())
    result = fetch_active_instagram_stories()
    assert result["status"] == "error"
    assert result["failure_category"] == "PROVIDER_FAILURE"


# ---------------------------------------------------------------------------
# run_story_sync -- orchestration: DB writes only follow a complete success
# ---------------------------------------------------------------------------

def test_run_story_sync_failure_never_touches_the_database(db, monkeypatch):
    db.sync_upsert_story_source("18500000000000001")  # pre-existing Active story
    monkeypatch.setattr(
        instagram_routing, "fetch_active_instagram_stories",
        lambda: {"status": "error", "failure_category": "PROVIDER_FAILURE"},
    )
    result = run_story_sync(db)
    assert result["status"] == "error"
    assert result["synced"] == 0
    assert result["expired"] == []
    row = db.get_instagram_story_source("18500000000000001")
    assert row["lifecycle_state"] == "ACTIVE"
    assert row["consecutive_sync_misses"] == 0


def test_run_story_sync_success_upserts_and_advances_misses(db, monkeypatch):
    db.sync_upsert_story_source("18500000000000002")  # will be missing this sync
    monkeypatch.setattr(
        instagram_routing, "fetch_active_instagram_stories",
        lambda: {"status": "success", "stories": [{"id": "18500000000000003", "media_type": "STORY"}], "pages_checked": 1},
    )
    result = run_story_sync(db)
    assert result["status"] == "success"
    assert result["synced"] == 1
    ids = [s["story_media_id"] for s in db.list_instagram_story_sources()]
    assert "18500000000000003" in ids  # newly discovered, no customer reply involved
    missed = db.get_instagram_story_source("18500000000000002")
    assert missed["consecutive_sync_misses"] == 1


def test_run_story_sync_token_or_permission_failure_does_not_alter_lifecycle(db, monkeypatch):
    db.sync_upsert_story_source("18500000000000004")
    db.mark_story_sync_misses([])  # one miss already recorded
    monkeypatch.setattr(
        instagram_routing, "fetch_active_instagram_stories",
        lambda: {"status": "error", "failure_category": "TOKEN_OR_ACCOUNT_ERROR"},
    )
    run_story_sync(db)  # a failed sync must never advance the miss count further
    row = db.get_instagram_story_source("18500000000000004")
    assert row["consecutive_sync_misses"] == 1
    assert row["lifecycle_state"] == "ACTIVE"


# ---------------------------------------------------------------------------
# Admin endpoint wiring
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "story-sync-admin.db"))
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(app_module.config, "ADMIN_API_KEY", "story-sync-admin-key")
    return TestClient(app_module.app), database


def test_sync_endpoint_requires_admin(admin_client):
    client, _ = admin_client
    resp = client.post("/admin/instagram-story-sources/sync")
    assert resp.status_code == 403


def test_sync_endpoint_discovers_a_story_with_zero_customer_messages(admin_client, monkeypatch):
    client, database = admin_client
    monkeypatch.setattr(
        app_module, "run_story_sync",
        lambda db_arg: {"status": "success", "synced": 1, "expired": []},
    )
    # Seed what run_story_sync would have written, since it's mocked here --
    # this test is about the endpoint wiring, not the sync internals
    # (covered above).
    database.sync_upsert_story_source("18600000000000001")
    resp = client.post(
        "/admin/instagram-story-sources/sync",
        headers={"X-Admin-Key": "story-sync-admin-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert any(s["story_media_id"] == "18600000000000001" for s in body["story_sources"])


def test_sync_endpoint_failure_is_reported_without_a_500(admin_client, monkeypatch):
    client, _ = admin_client
    monkeypatch.setattr(
        app_module, "run_story_sync",
        lambda db_arg: {"status": "error", "reason": "PROVIDER_FAILURE", "synced": 0, "expired": []},
    )
    resp = client.post(
        "/admin/instagram-story-sources/sync",
        headers={"X-Admin-Key": "story-sync-admin-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


def test_list_story_sources_endpoint_defaults_to_active_and_supports_history(admin_client):
    client, database = admin_client
    database.sync_upsert_story_source("18600000000000002")
    database.mark_story_sync_misses([])
    database.mark_story_sync_misses([])  # confirmed EXPIRED

    active = client.get(
        "/admin/instagram-story-sources", headers={"X-Admin-Key": "story-sync-admin-key"},
    ).json()
    assert "18600000000000002" not in [s["story_media_id"] for s in active["story_sources"]]

    history = client.get(
        "/admin/instagram-story-sources?include_expired=true",
        headers={"X-Admin-Key": "story-sync-admin-key"},
    ).json()
    assert "18600000000000002" in [s["story_media_id"] for s in history["story_sources"]]


def test_posts_and_reels_still_manageable_after_story_removed_from_content_routing(admin_client):
    """Regression: removing Story from the general routing form must not
    touch the backend's handling of post/reel routes at all."""
    client, database = admin_client
    for source_type, url, shortcode in (
        ("post", "https://www.instagram.com/p/Dc2PmrQE3Y4/", "Dc2PmrQE3Y4"),
        ("reel", "https://www.instagram.com/reel/DdAnO6MTok1/", "DdAnO6MTok1"),
    ):
        resp = client.post(
            "/admin/instagram-routes",
            headers={"X-Admin-Key": "story-sync-admin-key", "Content-Type": "application/json"},
            json={
                "source_type": source_type, "url": url, "media_id": None, "ad_id": None, "campaign_id": None,
                "preview_url": None, "media_type": source_type, "topic_id": None,
                "routing_topic": f"{source_type} topic", "menu_id": None, "menu_mode": "INHERIT",
                "escalation_policy": "human_if_unsupported", "priority_products": [], "knowledge_scope": [],
                "active": True,
            },
        )
        assert resp.status_code == 200, resp.text

    routes = client.get(
        "/admin/instagram-routes", headers={"X-Admin-Key": "story-sync-admin-key"},
    ).json()["routes"]
    assert {r["source_type"] for r in routes} == {"post", "reel"}


def test_dedicated_stories_tab_can_still_create_a_story_route(admin_client):
    """Backend Story routing support must remain -- only the general
    Content Routing UI stops offering it. The dedicated Stories tab posts
    to the same endpoint the way it always has."""
    client, database = admin_client
    database.sync_upsert_story_source("18600000000000003")
    resp = client.post(
        "/admin/instagram-routes",
        headers={"X-Admin-Key": "story-sync-admin-key", "Content-Type": "application/json"},
        json={
            "source_type": "story", "url": "", "media_id": "18600000000000003", "ad_id": None, "campaign_id": None,
            "preview_url": None, "media_type": "story", "topic_id": None,
            "routing_topic": "Story topic", "menu_id": None, "menu_mode": "INHERIT",
            "escalation_policy": "human_if_unsupported", "priority_products": [], "knowledge_scope": [],
            "active": True,
        },
    )
    assert resp.status_code == 200, resp.text
    row = database.get_instagram_story_source("18600000000000003")
    assert row["mapping_status"] == "MAPPED"
