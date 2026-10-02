"""Owner-scoped Instagram media inventory sync and safe reconciliation tests."""

from __future__ import annotations

from camellia.config import config
from camellia.database import Database
from camellia.instagram_media import sync_instagram_media
from camellia.instagram_routing import validate_route_payload


OWNER_ID = "17841455615744812"
OWNER_USERNAME = "camellia.artisanal"
BASE = "https://graph.instagram.com/v25.0"
MEDIA_URL = f"{BASE}/me/media"
TARGET = "https://www.instagram.com/p/Ddy0Z8pkR-g/"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.get(url)
        if isinstance(response, list):
            return response.pop(0)
        return response


def _media(media_id, permalink, *, caption="verified post"):
    return {
        "id": media_id,
        "caption": caption,
        "media_type": "IMAGE",
        "media_url": f"https://cdn.example/{media_id}.jpg",
        "permalink": permalink,
        "timestamp": "2026-10-01T12:00:00+0000",
    }


def _install_graph(monkeypatch, pages, *, identity=None):
    client = FakeClient({
        f"{BASE}/me": FakeResponse(identity or {"id": OWNER_ID, "username": OWNER_USERNAME}),
        **pages,
    })
    monkeypatch.setattr("camellia.instagram_media.httpx.Client", lambda **_kwargs: client)
    monkeypatch.setattr(config, "INSTAGRAM_AUTH_MODE", "instagram_login", raising=False)
    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", OWNER_ID)
    monkeypatch.setattr(config, "INSTAGRAM_EXPECTED_USERNAME", OWNER_USERNAME)
    monkeypatch.setattr(config, "INSTAGRAM_API_VERSION", "v25.0")
    return client


def test_complete_paginated_sync_stamps_owner_and_finds_exact_permalink(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "inventory.db"))
    second_page = f"{BASE}/me/media?after=next"
    client = _install_graph(monkeypatch, {
        MEDIA_URL: FakeResponse({
            "data": [_media("owned-1", "https://www.instagram.com/p/Ddy0Z8pkR-g/?igsh=tracking")],
            "paging": {"next": second_page},
        }),
        second_page: FakeResponse({"data": [_media("owned-2", "https://www.instagram.com/reel/ABC123/")]}),
    })

    result = sync_instagram_media(database, TARGET + "?utm_source=diagnostic")

    assert result["status"] == "COMPLETED"
    assert result["pagination_complete"] is True
    assert result["pages"] == 2
    assert result["discovered"] == 2
    assert result["diagnostic_permalink"]["status"] == "FOUND"
    assert result["diagnostic_permalink"]["media_id"] == "owned-1"
    assert [call[0] for call in client.calls] == [f"{BASE}/me", MEDIA_URL, second_page]
    owned = database.get_instagram_media_by_meta_id("owned-1")
    assert owned["owner_instagram_account_id"] == OWNER_ID
    assert owned["owner_username"] == OWNER_USERNAME
    assert owned["last_sync_generation"]
    assert owned["inventory_state"] == "CURRENT"
    assert database.list_instagram_media(owner_account_id=OWNER_ID)[1] == 2


def test_complete_sync_hides_foreign_and_stale_rows_without_deleting_routes(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "inventory.db"))
    foreign = database.upsert_instagram_media({
        "instagram_media_id": "old-account-post", "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/OLD001/", "shortcode": "OLD001",
        "owner_instagram_account_id": "111111111111", "owner_username": "old.account",
        "inventory_state": "CURRENT", "last_sync_generation": "previous",
    })
    stale = database.upsert_instagram_media({
        "instagram_media_id": "legacy-post", "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/LEGACY1/", "shortcode": "LEGACY1",
        "inventory_state": "UNVERIFIED", "last_sync_generation": None,
    })
    route = database.save_instagram_source_route(validate_route_payload({
        "source_type": "post", "media_id": "old-account-post",
        "routing_topic": "Historical custom mapping", "source_description": "Keep this route",
        "active": True,
    }))
    _install_graph(monkeypatch, {
        MEDIA_URL: FakeResponse({"data": [_media("current-post", "https://www.instagram.com/p/CUR001/")]}),
    })

    result = sync_instagram_media(database)

    assert result["hidden_foreign"] == 1
    assert result["hidden_stale"] == 1
    assert database.get_instagram_media(foreign["id"])["inventory_state"] == "FOREIGN"
    assert database.get_instagram_media(stale["id"])["inventory_state"] == "STALE"
    assert database.get_instagram_source_route(route["routing_id"])["media_id"] == "old-account-post"
    items, total = database.list_instagram_media(owner_account_id=OWNER_ID, search="OLD001")
    assert items == [] and total == 0
    assert database.list_instagram_media(owner_account_id=OWNER_ID, search="LEGACY1")[1] == 0
    assert database.list_instagram_media(owner_account_id=OWNER_ID, search="CUR001")[1] == 1


def test_failed_later_page_never_reconciles_existing_inventory(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "inventory.db"))
    foreign = database.upsert_instagram_media({
        "instagram_media_id": "foreign", "owner_instagram_account_id": "111111111111",
        "inventory_state": "CURRENT",
    })
    legacy = database.upsert_instagram_media({
        "instagram_media_id": "legacy", "inventory_state": "UNVERIFIED",
    })
    next_page = f"{BASE}/me/media?after=broken"
    _install_graph(monkeypatch, {
        MEDIA_URL: FakeResponse({"data": [], "paging": {"next": next_page}}),
        next_page: FakeResponse({}, status_code=503),
    })

    result = sync_instagram_media(database)

    assert result["status"] == "FAILED"
    assert result["pagination_complete"] is False
    assert result["hidden_foreign"] == result["hidden_stale"] == 0
    assert database.get_instagram_media(foreign["id"])["inventory_state"] == "CURRENT"
    assert database.get_instagram_media(legacy["id"])["inventory_state"] == "UNVERIFIED"


def test_untrusted_pagination_link_is_rejected_without_reconciliation(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "inventory.db"))
    foreign = database.upsert_instagram_media({
        "instagram_media_id": "foreign", "owner_instagram_account_id": "111111111111",
        "inventory_state": "CURRENT",
    })
    _install_graph(monkeypatch, {
        MEDIA_URL: FakeResponse({"data": [], "paging": {"next": "https://graph.instagram.com:444/v25.0/me/media?after=x"}}),
    })

    result = sync_instagram_media(database)

    assert result["status"] == "FAILED"
    assert result["pagination_complete"] is False
    assert database.get_instagram_media(foreign["id"])["inventory_state"] == "CURRENT"


def test_account_identity_mismatch_stops_before_media_sync_or_reconciliation(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "inventory.db"))
    row = database.upsert_instagram_media({
        "instagram_media_id": "existing", "inventory_state": "CURRENT",
        "owner_instagram_account_id": OWNER_ID,
    })
    client = _install_graph(monkeypatch, {}, identity={"id": "999999999999", "username": "someone.else"})

    result = sync_instagram_media(database)

    assert result["failure_category"] == "linked_account_mismatch"
    assert result["pagination_complete"] is False
    assert [call[0] for call in client.calls] == [f"{BASE}/me"]
    assert database.get_instagram_media(row["id"])["inventory_state"] == "CURRENT"


def test_missing_exact_permalink_is_reported_only_after_owned_media_pagination(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "inventory.db"))
    _install_graph(monkeypatch, {
        MEDIA_URL: FakeResponse({"data": [_media("another-post", "https://www.instagram.com/p/OTHER1/")]}),
    })

    result = sync_instagram_media(database, TARGET)

    assert result["pagination_complete"] is True
    assert result["diagnostic_permalink"] == {
        "canonical_permalink": TARGET,
        "status": "NOT_RETURNED_BY_OWNED_MEDIA_PAGINATION",
    }
    assert "caption" not in result["diagnostic_permalink"]
