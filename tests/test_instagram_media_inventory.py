"""Owner-scoped Instagram media inventory sync and safe reconciliation tests."""

from __future__ import annotations

from camellia.config import config
from camellia.database import Database
from camellia.instagram_media import sync_instagram_media
from camellia.instagram_routing import validate_route_payload


OWNER_ID = "17841455615744812"
GRAPH_OWNER_ID = "28206314108997745"
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
    monkeypatch.setattr(config, "INSTAGRAM_GRAPH_ACCOUNT_ID", OWNER_ID)
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
    assert database.has_successful_instagram_media_inventory_sync(OWNER_ID) is True


def test_legacy_gallery_is_visible_as_unverified_before_first_owner_sync(tmp_path):
    database = Database(str(tmp_path / "inventory.db"))
    database.upsert_instagram_media({
        "instagram_media_id": "legacy-1", "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/LEGACY1/", "shortcode": "LEGACY1",
    })

    items, total = database.list_instagram_media(owner_account_id=OWNER_ID)

    assert total == 1
    assert items[0]["instagram_media_id"] == "legacy-1"
    assert items[0]["inventory_state"] == "UNVERIFIED"
    assert items[0]["inventory_verification"] == "LEGACY_UNVERIFIED"
    assert items[0]["owner_instagram_account_id"] is None
    assert items[0]["owner_username"] is None
    assert database.has_successful_instagram_media_inventory_sync(OWNER_ID) is False


def test_failed_first_owner_sync_keeps_legacy_gallery_visible(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "inventory.db"))
    database.upsert_instagram_media({
        "instagram_media_id": "legacy-1", "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/LEGACY1/", "shortcode": "LEGACY1",
    })
    _install_graph(monkeypatch, {}, identity={"id": "999999999999", "username": OWNER_USERNAME})

    result = sync_instagram_media(database)
    items, total = database.list_instagram_media(owner_account_id=OWNER_ID)

    assert result["failure_category"] == "linked_account_mismatch"
    assert total == 1
    assert items[0]["inventory_verification"] == "LEGACY_UNVERIFIED"
    assert items[0]["owner_instagram_account_id"] is None
    assert database.get_instagram_media_by_meta_id("legacy-1")["inventory_state"] == "UNVERIFIED"


def test_partial_first_owner_sync_keeps_legacy_gallery_visible_and_unverified(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "inventory.db"))
    database.upsert_instagram_media({
        "instagram_media_id": "legacy-1", "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/LEGACY1/", "shortcode": "LEGACY1",
    })
    next_page = f"{BASE}/me/media?after=broken"
    _install_graph(monkeypatch, {
        MEDIA_URL: FakeResponse({
            "data": [_media("partial-1", "https://www.instagram.com/p/PARTIAL1/")],
            "paging": {"next": next_page},
        }),
        next_page: FakeResponse({}, status_code=503),
    })

    result = sync_instagram_media(database)
    items, total = database.list_instagram_media(owner_account_id=OWNER_ID)
    by_id = {item["instagram_media_id"]: item for item in items}

    assert result["status"] == "FAILED"
    assert result["pagination_complete"] is False
    assert total == 1
    assert set(by_id) == {"legacy-1"}
    assert database.get_instagram_media_by_meta_id("partial-1") is None
    assert all(item["inventory_state"] == "UNVERIFIED" for item in items)
    assert all(item["inventory_verification"] == "LEGACY_UNVERIFIED" for item in items)
    assert all(item["owner_instagram_account_id"] is None for item in items)
    assert database.has_successful_instagram_media_inventory_sync(OWNER_ID) is False


def test_first_complete_owner_sync_hides_legacy_and_enters_strict_gallery(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "inventory.db"))
    database.upsert_instagram_media({
        "instagram_media_id": "legacy-1", "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/LEGACY1/", "shortcode": "LEGACY1",
    })
    database.upsert_instagram_media({
        "instagram_media_id": "foreign-1", "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/FOREIGN1/", "shortcode": "FOREIGN1",
        "owner_instagram_account_id": "111111111111", "owner_username": "old.account",
        "inventory_state": "CURRENT",
    })
    _install_graph(monkeypatch, {
        MEDIA_URL: FakeResponse({"data": [_media("owned-1", "https://www.instagram.com/p/OWNED1/")]}),
    })

    result = sync_instagram_media(database)
    items, total = database.list_instagram_media(owner_account_id=OWNER_ID)

    assert result["status"] == "COMPLETED"
    assert result["pagination_complete"] is True
    assert total == 1
    assert items[0]["instagram_media_id"] == "owned-1"
    assert items[0]["inventory_state"] == "CURRENT"
    assert "inventory_verification" not in items[0]
    assert database.get_instagram_media_by_meta_id("legacy-1")["inventory_state"] == "STALE"
    assert database.get_instagram_media_by_meta_id("foreign-1")["inventory_state"] == "FOREIGN"
    assert database.has_successful_instagram_media_inventory_sync(OWNER_ID) is True


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
    _install_graph(monkeypatch, {
        MEDIA_URL: FakeResponse({"data": [_media("known-good", "https://www.instagram.com/p/GOOD001/")]}),
    })
    first = sync_instagram_media(database)
    assert first["status"] == "COMPLETED"
    known_good = database.get_instagram_media_by_meta_id("known-good")
    next_page = f"{BASE}/me/media?after=broken"
    client = FakeClient({
        f"{BASE}/me": FakeResponse({"id": OWNER_ID, "username": OWNER_USERNAME}),
        MEDIA_URL: FakeResponse({"data": [{**_media("known-good", "https://www.instagram.com/p/GOOD001/"), "caption": "partial change"}, _media("new-partial", "https://www.instagram.com/p/NEW001/")], "paging": {"next": next_page}}),
        next_page: FakeResponse({}, status_code=503),
    })
    monkeypatch.setattr("camellia.instagram_media.httpx.Client", lambda **_kwargs: client)

    result = sync_instagram_media(database)
    items, total = database.list_instagram_media(owner_account_id=OWNER_ID)

    assert result["status"] == "FAILED"
    assert result["pagination_complete"] is False
    assert result["hidden_foreign"] == result["hidden_stale"] == 0
    assert database.has_successful_instagram_media_inventory_sync(OWNER_ID) is True
    assert total == 1
    assert items[0]["instagram_media_id"] == "known-good"
    assert items[0]["inventory_state"] == "CURRENT"
    assert database.get_instagram_media(known_good["id"]) == known_good
    assert database.get_instagram_media_by_meta_id("new-partial") is None


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


def test_webhook_and_instagram_login_graph_identity_ids_are_separately_bound(tmp_path, monkeypatch):
    from camellia import app as app_module, home_table_native_reels

    database = Database(str(tmp_path / "split-identities.db"))
    _install_graph(monkeypatch, {
        MEDIA_URL: FakeResponse({"data": [_media("graph-owned", "https://www.instagram.com/p/GRAPH001/")]}),
    }, identity={"id": GRAPH_OWNER_ID, "username": OWNER_USERNAME})
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", OWNER_ID)
    monkeypatch.setattr(config, "INSTAGRAM_GRAPH_ACCOUNT_ID", GRAPH_OWNER_ID)

    result = sync_instagram_media(database)

    assert result["status"] == "COMPLETED"
    assert result["current_account_id"] == GRAPH_OWNER_ID
    assert database.get_instagram_media_by_meta_id("graph-owned")["owner_instagram_account_id"] == GRAPH_OWNER_ID
    assert config.INSTAGRAM_ACCOUNT_ID == OWNER_ID
    assert app_module._instagram_account_guard_matches(OWNER_ID, config.INSTAGRAM_ACCOUNT_ID)
    assert not app_module._instagram_account_guard_matches(GRAPH_OWNER_ID, config.INSTAGRAM_ACCOUNT_ID)
    assert home_table_native_reels.VERIFIED_ACCOUNT_ID == OWNER_ID


def test_instagram_login_graph_identity_must_be_explicitly_bound(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "unbound-graph-id.db"))
    client = _install_graph(monkeypatch, {
        MEDIA_URL: FakeResponse({"data": [_media("must-not-fetch", "https://www.instagram.com/p/NOPE001/")]}),
    }, identity={"id": GRAPH_OWNER_ID, "username": OWNER_USERNAME})
    monkeypatch.setattr(config, "INSTAGRAM_GRAPH_ACCOUNT_ID", "")

    result = sync_instagram_media(database)

    assert result["status"] == "FAILED"
    assert result["failure_category"] == "linked_account_not_configured"
    assert result["pages"] == 0
    assert client.calls == []  # no Graph media/identity request without an explicit ID binding
    assert database.get_instagram_media_by_meta_id("must-not-fetch") is None


def test_wrong_graph_id_or_username_fails_closed(tmp_path, monkeypatch):
    cases = (
        ({"id": "999999999999", "username": OWNER_USERNAME}, GRAPH_OWNER_ID, OWNER_USERNAME),
        ({"id": GRAPH_OWNER_ID, "username": "different.account"}, GRAPH_OWNER_ID, OWNER_USERNAME),
    )
    for index, (identity, expected_graph_id, expected_username) in enumerate(cases):
        database = Database(str(tmp_path / f"wrong-identity-{index}.db"))
        _install_graph(monkeypatch, {
            MEDIA_URL: FakeResponse({"data": [_media("must-not-fetch", "https://www.instagram.com/p/NOPE002/")]}),
        }, identity=identity)
        monkeypatch.setattr(config, "INSTAGRAM_GRAPH_ACCOUNT_ID", expected_graph_id)
        monkeypatch.setattr(config, "INSTAGRAM_EXPECTED_USERNAME", expected_username)

        result = sync_instagram_media(database)

        assert result["status"] == "FAILED"
        assert result["failure_category"] == "linked_account_mismatch"
        assert result["pages"] == 0
        assert database.get_instagram_media_by_meta_id("must-not-fetch") is None


def test_missing_graph_binding_preserves_unverified_legacy_gallery(tmp_path):
    database = Database(str(tmp_path / "inventory.db"))
    database.upsert_instagram_media({"instagram_media_id": "legacy-unbound", "media_type": "IMAGE"})
    items, total = database.list_instagram_media(owner_account_id="")
    assert total == 1
    assert items[0]["inventory_verification"] == "LEGACY_UNVERIFIED"
    assert items[0]["owner_instagram_account_id"] is None


def test_complete_inventory_publish_rolls_back_on_write_failure(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "inventory.db"))
    database.upsert_instagram_media({"instagram_media_id": "legacy", "caption": "original"})
    before = database.get_instagram_media_by_meta_id("legacy")
    original = database.upsert_instagram_media
    def fail_second(row, **kwargs):
        if row["instagram_media_id"] == "broken":
            raise ValueError("failed write")
        return original(row, **kwargs)
    monkeypatch.setattr(database, "upsert_instagram_media", fail_second)
    import pytest
    with pytest.raises(ValueError, match="failed write"):
        database.apply_instagram_media_inventory([
            {"instagram_media_id": "legacy", "caption": "changed"},
            {"instagram_media_id": "broken"},
        ], OWNER_ID, "new-generation")
    assert database.get_instagram_media_by_meta_id("legacy") == before
    assert not database.has_successful_instagram_media_inventory_sync(OWNER_ID)
