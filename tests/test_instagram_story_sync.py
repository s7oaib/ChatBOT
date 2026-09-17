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
    monkeypatch.setattr(config, "INSTAGRAM_PAGE_ID", "page-1")
    # Deliberately mismatched from INSTAGRAM_API_VERSION so any accidental
    # reintroduction of config.WHATSAPP_API_VERSION into Story sync surfaces
    # as a wrong Graph version in the requested URLs.
    monkeypatch.setattr(config, "WHATSAPP_API_VERSION", "v19.0")
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


def test_missing_old_synced_story_is_expired_but_retained(sync_db, monkeypatch):
    _install_meta(monkeypatch, [
        _page(), _stories([_story(timestamp="2020-01-01T00:00:00+00:00")]),
        _page(), _stories([]),
    ])
    sync_module.sync_instagram_stories(sync_db)

    result = sync_module.sync_instagram_stories(sync_db)

    assert result["expired"] == 1
    saved = sync_db.get_instagram_story_source(STORY_ID)
    assert saved["story_status"] == "EXPIRED"
    assert saved["admin_deleted"] == 0


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
        [FakeResponse(200, {"id": "ig-account-1"}), _stories([_story()])],
        token="IGAA_TOKEN",
    )

    result = sync_module.sync_instagram_stories(sync_db)

    assert result["status"] == "success"
    for url, _params in calls:
        assert "/v25.0/" in url
        assert "/v19.0/" not in url


def test_story_sync_does_not_use_whatsapp_api_version(sync_db, monkeypatch):
    calls = _install_meta(monkeypatch, [_page(), _stories([_story()])])

    sync_module.sync_instagram_stories(sync_db)

    assert not any("v19.0" in url for url, _params in calls)


def test_changing_whatsapp_api_version_does_not_change_story_sync_url(sync_db, monkeypatch):
    calls = _install_meta(monkeypatch, [_page(), _stories([_story()])])
    monkeypatch.setattr(config, "WHATSAPP_API_VERSION", "v99.0")

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
