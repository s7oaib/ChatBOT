"""Contract tests for the opt-in, documented owned-Story inventory sync."""

from unittest.mock import patch

import pytest

import camellia.app as app_module
import camellia.instagram_story_sync as sync_module
from camellia.config import config
from camellia.database import Database
from camellia.instagram_routing import route_for_inbound


STORY_ID = "18371255653212260"


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
    return Database(str(tmp_path / "story-sync.db"))


def _page():
    return FakeResponse(200, {"instagram_business_account": {"id": "ig-account-1"}})


def _stories(items):
    return FakeResponse(200, {"data": items})


def _story(story_id=STORY_ID, timestamp="2026-09-17T08:00:00+00:00"):
    return {
        "id": story_id,
        "media_type": "IMAGE",
        "media_url": "https://cdn.example/story.jpg?access_token=SECRET&expires=123",
        "thumbnail_url": "https://cdn.example/story-thumb.jpg?sig=SECRET",
        "permalink": "https://www.instagram.com/stories/camellia/123/",
        "timestamp": timestamp,
    }


def _install_meta(monkeypatch, responses, token="PAGE_TOKEN"):
    calls = []
    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", token)
    monkeypatch.setattr(
        config, "INSTAGRAM_AUTH_MODE",
        "instagram_login" if token.startswith("IGAA") else "legacy_page",
    )
    monkeypatch.setattr(config, "INSTAGRAM_PAGE_ID", "page-1")
    monkeypatch.setattr(config, "INSTAGRAM_GRAPH_ACCOUNT_ID", "123456")
    monkeypatch.setattr(config, "INSTAGRAM_EXPECTED_USERNAME", "camellia.artisanal")
    monkeypatch.setattr(config, "INSTAGRAM_API_VERSION", "v25.0")
    monkeypatch.setattr(
        sync_module.httpx,
        "Client",
        lambda timeout: FakeClient(responses, calls),
    )
    return calls


def test_new_story_is_discovered_with_exact_id_and_safe_metadata(sync_db, monkeypatch):
    calls = _install_meta(monkeypatch, [_page(), _stories([_story()])])

    result = sync_module.sync_instagram_stories(sync_db)

    assert result["status"] == "success"
    assert result["fetched"] == 1
    assert result["created"] == 1
    saved = sync_db.get_instagram_story_source(STORY_ID)
    assert saved["story_media_id"] == STORY_ID
    assert saved["source"] == "META_STORY_SYNC"
    assert saved["story_status"] == "ACTIVE"
    assert saved["media_type"] == "IMAGE"
    assert saved["published_at"] == "2026-09-17T08:00:00+00:00"
    assert saved["temporary_url"] == "https://cdn.example/story-thumb.jpg"
    assert "SECRET" not in str(saved)
    assert calls[1][0].endswith("/ig-account-1/stories")
    assert "caption" not in calls[1][1]["fields"]


def test_repeated_sync_is_idempotent_and_preserves_mapping(sync_db, monkeypatch):
    responses = [_page(), _stories([_story()]), _page(), _stories([_story()])]
    _install_meta(monkeypatch, responses)
    first = sync_module.sync_instagram_stories(sync_db)
    route = sync_db.save_instagram_source_route({
        "source_type": "story", "media_id": STORY_ID,
        "routing_topic": "Jolbhora", "active": True,
    })

    second = sync_module.sync_instagram_stories(sync_db)

    assert first["created"] == 1
    assert second["created"] == 0
    assert second["updated"] == 0
    assert second["unchanged"] == 1
    assert sync_db.get_instagram_source_route(route["routing_id"])["routing_topic"] == "Jolbhora"
    with sync_db.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM instagram_story_sources WHERE story_media_id=?", (STORY_ID,)
        ).fetchone()[0] == 1


def test_multiple_stories_are_inventory_only_and_unmapped(sync_db, monkeypatch):
    _install_meta(monkeypatch, [_page(), _stories([_story(), _story("story-2")])])

    result = sync_module.sync_instagram_stories(sync_db)

    assert result["created"] == 2
    assert {row["mapping_status"] for row in sync_db.list_instagram_story_sources()} == {"UNMAPPED"}
    assert sync_db.list_instagram_source_routes() == []


def test_missing_old_synced_story_is_hard_deleted(sync_db, monkeypatch):
    _install_meta(monkeypatch, [
        _page(), _stories([_story(timestamp="2020-01-01T00:00:00+00:00")]),
        _page(), _stories([]),
    ])
    sync_module.sync_instagram_stories(sync_db)

    result = sync_module.sync_instagram_stories(sync_db)

    assert result["expired"] == 1
    assert sync_db.get_instagram_story_source(STORY_ID) is None


def test_transient_meta_failure_does_not_expire_inventory(sync_db, monkeypatch):
    _install_meta(monkeypatch, [
        _page(), _stories([_story(timestamp="2020-01-01T00:00:00+00:00")]),
        FakeResponse(503, {"error": {"message": "temporarily unavailable"}}),
    ])
    sync_module.sync_instagram_stories(sync_db)

    result = sync_module.sync_instagram_stories(sync_db)

    assert result["status"] == "failed"
    assert result["failure"]["category"] == "meta_server_error"
    assert sync_db.get_instagram_story_source(STORY_ID)["story_status"] == "ACTIVE"


def test_page_cap_marks_sync_partial_without_reconciling_absent_stories(sync_db, monkeypatch):
    sync_db.upsert_instagram_story_sync(
        STORY_ID,
        media_type="IMAGE",
        temporary_url=None,
        permalink=None,
        published_at="2020-01-01T00:00:00+00:00",
        event_metadata={},
    )
    page = {
        "data": [_story(f"page-story-{index}") for index in range(1000)],
        "paging": {"next": "https://graph.facebook.com/v25.0/account/stories?after=next"},
    }
    _install_meta(monkeypatch, [_page(), FakeResponse(200, page)])

    result = sync_module.sync_instagram_stories(sync_db)

    assert result["status"] == "partial"
    assert result["failure"]["category"] == "story_page_limit_reached"
    assert result["fetched"] == 1000
    assert sync_db.get_instagram_story_source(STORY_ID)["sync_miss_count"] == 0


@pytest.mark.parametrize("payload", [{"data": {}}, {"unexpected": []}, []])
def test_malformed_meta_payload_is_safe(sync_db, monkeypatch, payload):
    _install_meta(monkeypatch, [_page(), FakeResponse(200, payload)])

    result = sync_module.sync_instagram_stories(sync_db)

    assert result["status"] == "failed"
    assert result["failure"]["category"] == "malformed_meta_response"
    assert sync_db.list_instagram_story_sources() == []


def test_manual_sync_is_admin_protected_and_never_sends_messages(monkeypatch, sync_db):
    with patch.object(app_module, "require_admin") as auth, patch.object(
        app_module, "sync_instagram_stories", return_value={"status": "success", "created": 0}
    ) as sync:
        result = app_module.sync_instagram_story_sources()

    auth.assert_called_once()
    sync.assert_called_once_with(app_module.db)
    assert result["status"] == "success"


def test_synced_story_id_routes_through_existing_webhook_identity(sync_db, monkeypatch):
    _install_meta(monkeypatch, [_page(), _stories([_story()])])
    sync_module.sync_instagram_stories(sync_db)
    sync_db.save_instagram_source_route({
        "source_type": "story", "media_id": STORY_ID,
        "routing_topic": "Jolbhora", "active": True,
    })
    monkeypatch.setattr("camellia.database.db", sync_db)

    route, method = route_for_inbound(
        ad_id=None, media_id=STORY_ID, source_type="story_reply",
    )

    assert route["media_id"] == STORY_ID
    assert method == "story_media_id"


def test_auto_sync_is_conservatively_disabled_by_default():
    assert config.INSTAGRAM_STORY_AUTO_SYNC_ENABLED is False


def test_story_sync_uses_instagram_api_version_facebook_login(sync_db, monkeypatch):
    """Facebook-login (Page token) Story endpoint must use INSTAGRAM_API_VERSION."""
    calls = _install_meta(monkeypatch, [_page(), _stories([_story()])], token="PAGE_TOKEN")

    result = sync_module.sync_instagram_stories(sync_db)

    assert result["status"] == "success"
    for url, _params in calls:
        assert "/v25.0/" in url
        assert "/v19.0/" not in url


def test_story_sync_uses_instagram_api_version_instagram_login(sync_db, monkeypatch):
    """Instagram-login (IGAA token) Story endpoint must use INSTAGRAM_API_VERSION."""
    calls = _install_meta(
        monkeypatch,
        [FakeResponse(200, {"id": "123456", "username": "camellia.artisanal"}), _stories([_story()])],
        token="IGAA_TOKEN",
    )

    result = sync_module.sync_instagram_stories(sync_db)

    assert result["fetched"] == 1
    assert sync_db.get_instagram_story_source(STORY_ID)["story_media_id"] == STORY_ID
    assert calls[1][0] == "https://graph.instagram.com/v25.0/123456/stories"
    assert result["status"] == "success"
    for url, _params in calls:
        assert "/v25.0/" in url
        assert "/v19.0/" not in url


def test_story_sync_does_not_use_whatsapp_api_version(sync_db, monkeypatch):
    calls = _install_meta(monkeypatch, [_page(), _stories([_story()])])

    sync_module.sync_instagram_stories(sync_db)

    assert not any("v19.0" in url for url, _params in calls)


def test_story_sync_url_uses_only_instagram_api_version(sync_db, monkeypatch):
    calls = _install_meta(monkeypatch, [_page(), _stories([_story()])])

    sync_module.sync_instagram_stories(sync_db)

    assert any("/v25.0/" in url for url, _params in calls)
    assert not any("v99.0" in url for url, _params in calls)


def test_instagram_api_version_falls_back_when_malformed(monkeypatch):
    from camellia.config import _resolve_graph_api_version, _DEFAULT_INSTAGRAM_API_VERSION

    monkeypatch.setenv("INSTAGRAM_API_VERSION", "not-a-version")
    assert _resolve_graph_api_version(
        "INSTAGRAM_API_VERSION", _DEFAULT_INSTAGRAM_API_VERSION
    ) == _DEFAULT_INSTAGRAM_API_VERSION

    monkeypatch.setenv("INSTAGRAM_API_VERSION", "v22.0")
    assert _resolve_graph_api_version(
        "INSTAGRAM_API_VERSION", _DEFAULT_INSTAGRAM_API_VERSION
    ) == "v22.0"


@pytest.mark.parametrize("account_id,username,category", [
    ("123456", "Camellia.Artisanal", None),
    ("999999", "camellia.artisanal", "linked_account_mismatch"),
    ("123456", "another.account", "linked_account_mismatch"),
])
def test_story_account_diagnostic_verifies_identity_without_inventory(
    monkeypatch, sync_db, account_id, username, category,
):
    _install_meta(monkeypatch, [FakeResponse(200, {"id": account_id, "username": username})], token="IGAA_SECRET")
    monkeypatch.setattr(config, "INSTAGRAM_GRAPH_ACCOUNT_ID", "123456")
    monkeypatch.setattr(config, "INSTAGRAM_EXPECTED_USERNAME", "@camellia.artisanal")
    sync_db.upsert_instagram_story_sync(STORY_ID, event_metadata={})
    before = sync_db.get_instagram_story_source(STORY_ID)
    result = sync_module.story_account_diagnostic()
    assert result["identity_match"] is (category is None)
    assert result.get("failure", {}).get("category") == category
    assert result["authenticated_account_id"] == account_id
    assert result["authenticated_username"] == username
    assert "IGAA_SECRET" not in str(result)
    assert sync_db.get_instagram_story_source(STORY_ID) == before


def test_story_account_diagnostic_failure_is_sanitized(monkeypatch):
    _install_meta(monkeypatch, [FakeResponse(401, {"error": {"code": 190, "message": "access token IGAA_SECRET"}})], token="IGAA_SECRET")
    result = sync_module.story_account_diagnostic()
    assert result["status"] == "failed"
    assert "IGAA_SECRET" not in str(result)


def test_story_account_diagnostic_route_requires_admin():
    with patch.object(app_module, "require_admin") as auth, patch.object(
        sync_module, "story_account_diagnostic", return_value={"identity_match": True},
    ) as diagnostic:
        assert app_module.instagram_story_account_diagnostic()["identity_match"]
    auth.assert_called_once()
    diagnostic.assert_called_once_with()


@pytest.mark.parametrize("account_id,username", [("999999", "camellia.artisanal"), ("123456", "another.account")])
def test_story_sync_identity_mismatch_preserves_inventory(monkeypatch, sync_db, account_id, username):
    calls = _install_meta(monkeypatch, [FakeResponse(200, {"id": account_id, "username": username})], token="IGAA_SECRET")
    sync_db.upsert_instagram_story_sync(STORY_ID, event_metadata={})
    before = sync_db.get_instagram_story_source(STORY_ID)
    result = sync_module.sync_instagram_stories(sync_db)
    assert result["failure"]["category"] == "linked_account_mismatch"
    assert len(calls) == 1
    assert sync_db.get_instagram_story_source(STORY_ID) == before
    assert "IGAA_SECRET" not in str(result)


def test_verified_story_sync_empty_result_explains_meta_limit(monkeypatch, sync_db):
    _install_meta(monkeypatch, [FakeResponse(200, {"id": "123456", "username": "Camellia.Artisanal"}), _stories([])], token="IGAA_SECRET")
    result = sync_module.sync_instagram_stories(sync_db)
    assert result["status"] == "success" and result["fetched"] == 0
    assert "Story sync completed for @camellia.artisanal, but Meta returned 0 active Stories." in result["message"]
    assert "reshared Stories" in result["message"]
    assert "IGAA_SECRET" not in str(result)


@pytest.mark.parametrize("legacy", [False, True])
def test_webhook_story_and_exact_route_survive_repeated_empty_owned_sync(sync_db, monkeypatch, legacy):
    sync_db.record_instagram_story_source(STORY_ID, event_metadata={"source": "story_reply"})
    assert sync_db.get_instagram_story_source(STORY_ID)["source"] == "WEBHOOK_STORY_REPLY"
    if legacy:
        # Pre-sync schemas permitted NULL provenance; retain authentic metadata.
        with sync_db.get_connection() as conn:
            conn.execute("ALTER TABLE instagram_story_sources DROP COLUMN source")
            conn.execute("ALTER TABLE instagram_story_sources ADD COLUMN source TEXT")
            conn.commit()
    route = sync_db.save_instagram_source_route({
        "source_type": "story", "media_id": STORY_ID,
        "routing_topic": "Jolbhora", "active": True,
    })
    monkeypatch.setattr("camellia.database.db", sync_db)
    _install_meta(monkeypatch, [_page(), _stories([])] * 3)
    for _ in range(3):
        result = sync_module.sync_instagram_stories(sync_db)
        assert result["status"] == "success" and result["fetched"] == 0
        assert result["removed"] == result["expired"] == 0
        assert sync_db.get_instagram_story_source(STORY_ID)["sync_miss_count"] == 0
        assert sync_db.get_instagram_source_route(route["routing_id"]) == route
        resolved, method = route_for_inbound(ad_id=None, media_id=STORY_ID, source_type="story_reply")
        assert resolved["routing_id"] == route["routing_id"] and method == "story_media_id"


def test_owned_story_promotion_and_stronger_provenance_survive_webhook(sync_db, monkeypatch):
    sync_db.record_instagram_story_source(STORY_ID, event_metadata={"source": "story_reply"})
    _install_meta(monkeypatch, [_page(), _stories([_story()]), _page(), _stories([])])
    sync_module.sync_instagram_stories(sync_db)
    sync_db.record_instagram_story_source(STORY_ID, event_metadata={"source": "story_reply"})
    assert sync_db.get_instagram_story_source(STORY_ID)["source"] == "META_STORY_SYNC"
    result = sync_module.sync_instagram_stories(sync_db)
    assert result["expired"] == 1
    assert sync_db.get_instagram_story_source(STORY_ID) is None
