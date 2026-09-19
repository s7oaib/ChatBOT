"""
Direct by-id media verification.

A stronger check than presence in a paginated /me/media listing: given a
media id (e.g. captured off a real ig_post attachment as
payload.ig_post_media_id), fetch that object directly and report what it
actually is. Diagnostic only — never wired into a customer-facing matcher.
"""
import os

os.environ.setdefault("DB_PATH", "test_camellia.db")

import httpx
import pytest

from camellia import instagram_routing
from camellia.instagram_routing import verify_media_by_id


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_a_real_media_id_resolves_its_permalink_and_shortcode(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAAtoken")
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_API_VERSION", "v25.0")
    monkeypatch.setattr(instagram_routing.config, "WHATSAPP_API_VERSION", "v99.0")
    captured = {}

    class _Client:
        def get(self, url, params=None):
            captured["url"] = url
            return _FakeResponse({
                "id": "17930882778146779",
                "permalink": "https://www.instagram.com/p/Dc2PmrQE3Y4/",
                "media_type": "IMAGE",
                "timestamp": "2026-09-10T10:00:00+0000",
            })

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **kw: _Client())
    result = verify_media_by_id("17930882778146779")

    assert captured["url"] == "https://graph.instagram.com/v25.0/17930882778146779"
    assert "v99.0" not in captured["url"]
    assert result["status"] == "verified"
    assert result["canonical_url"] == "https://www.instagram.com/p/Dc2PmrQE3Y4/"
    assert result["shortcode"] == "Dc2PmrQE3Y4"


def test_a_page_token_asks_graph_facebook_instead(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "EAATpagetoken")
    captured = {}

    class _Client:
        def get(self, url, params=None):
            captured["url"] = url
            return _FakeResponse({"id": "123", "permalink": None})

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **kw: _Client())
    verify_media_by_id("123")
    assert captured["url"].startswith("https://graph.facebook.com/")


def test_an_id_that_is_not_ours_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAAtoken")

    class _Client:
        def get(self, url, params=None):
            return _FakeResponse({}, status_code=400)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **kw: _Client())
    result = verify_media_by_id("99999999999999999")
    assert result["status"] == "not_found_or_unauthorized"
    assert result["http_status"] == 400


def test_a_non_numeric_id_is_rejected_without_a_network_call(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAAtoken")

    def _boom(**kw):
        raise AssertionError("must not make a network call for an invalid id")

    monkeypatch.setattr(instagram_routing.httpx, "Client", _boom)
    assert verify_media_by_id("'; DROP TABLE x--")["status"] == "invalid_media_id"


def test_no_token_is_reported_as_not_configured(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "")
    assert verify_media_by_id("123")["status"] == "not_configured"


def test_a_transport_failure_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAAtoken")

    class _Client:
        def get(self, url, params=None):
            raise httpx.ConnectError("down")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **kw: _Client())
    assert verify_media_by_id("123")["status"] == "lookup_failed"


@pytest.mark.parametrize(("code", "expected"), [
    (100, "unsupported media ID"),
    (190, "invalid/expired token"),
])
def test_graph_failure_is_reported_as_a_sanitized_category(monkeypatch, code, expected):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAAtoken")

    class _Client:
        def get(self, url, params=None):
            return _FakeResponse({"error": {"code": code, "type": "OAuthException", "message": "secret-free test error"}}, status_code=400)

        def __enter__(self): return self
        def __exit__(self, *exc): return False

    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **kw: _Client())
    result = verify_media_by_id("123")
    assert result["failure_category"] == expected
    assert "message" not in result


def test_endpoint_requires_admin_auth():
    from fastapi.testclient import TestClient
    import camellia.database as db_module
    db_module.db.init_db()
    from camellia.app import app
    client = TestClient(app)
    resp = client.get("/admin/debug/instagram-media/123")
    assert resp.status_code in (401, 403, 503)
