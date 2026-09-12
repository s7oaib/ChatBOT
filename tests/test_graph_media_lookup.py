"""
Which Graph host a token is allowed to ask.

An Instagram Login token ("IGAA...") lives on graph.instagram.com and is
rejected by graph.facebook.com. The lookup used to ask graph.facebook.com
unconditionally, so on a deployment using such a token every permalink check
returned "permission_or_account_unavailable" — indistinguishable from a real
missing permission, and the reason shared-post verification looked impossible.
"""
import os

os.environ.setdefault("DB_PATH", "test_camellia.db")

import httpx
import pytest

from camellia import instagram_routing
from camellia.instagram_routing import _media_list_url, lookup_instagram_media

POST = {
    "canonical_url": "https://www.instagram.com/p/Dc2PmrQE3Y4/",
    "shortcode": "Dc2PmrQE3Y4",
    "source_type": "post",
}


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeClient:
    """Records every URL asked for, so the host itself can be asserted."""

    def __init__(self, responses):
        self.responses = responses
        self.asked = []

    def get(self, url, params=None):
        self.asked.append(url)
        for prefix, response in self.responses.items():
            if url.startswith(prefix):
                return response
        return _FakeResponse({}, status_code=400)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_an_instagram_login_token_uses_graph_instagram_directly():
    client = _FakeClient({})
    url = _media_list_url(client, "IGAAabc123", "v21.0")
    assert url == "https://graph.instagram.com/v21.0/me/media"
    # It must not have gone shopping on graph.facebook.com first.
    assert client.asked == []


def test_a_page_token_still_resolves_through_the_linked_business_account():
    client = _FakeClient({
        "https://graph.facebook.com": _FakeResponse(
            {"instagram_business_account": {"id": "17841400000000000"}}
        ),
    })
    url = _media_list_url(client, "EAATpagetoken", "v21.0")
    assert url == "https://graph.facebook.com/v21.0/17841400000000000/media"
    assert client.asked and client.asked[0].startswith("https://graph.facebook.com/")


def test_a_page_token_with_no_linked_account_is_unavailable():
    client = _FakeClient({"https://graph.facebook.com": _FakeResponse({})})
    assert _media_list_url(client, "EAATpagetoken", "v21.0") is None


def test_lookup_verifies_a_permalink_over_the_instagram_host(monkeypatch):
    """End to end with an IGAA token: the post is found and its id returned."""
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAAtoken")

    captured = {}

    class _Client(_FakeClient):
        def get(self, url, params=None):
            captured["url"] = url
            return _FakeResponse({"data": [
                {"id": "17930882778146779",
                 "permalink": "https://www.instagram.com/p/Dc2PmrQE3Y4/",
                 "caption": "FOR THOSE WHO LOVE MISHTI.",
                 "media_url": "https://cdn.example/x.jpg"},
            ]})

    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **kw: _Client({}))
    result = lookup_instagram_media(POST)

    assert captured["url"].startswith("https://graph.instagram.com/")
    assert result["metadata_status"] == "verified_graph_api"
    assert result["verified_media_identifier"] == "17930882778146779"


def test_a_permalink_absent_from_the_account_is_not_invented(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAAtoken")

    class _Client(_FakeClient):
        def get(self, url, params=None):
            return _FakeResponse({"data": [
                {"id": "999", "permalink": "https://www.instagram.com/p/SomethingElse/"},
            ]})

    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **kw: _Client({}))
    result = lookup_instagram_media(POST)
    assert result["metadata_status"] == "not_found_on_connected_account"
    assert "verified_media_identifier" not in result


def test_a_transport_failure_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAAtoken")

    class _Client(_FakeClient):
        def get(self, url, params=None):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **kw: _Client({}))
    assert lookup_instagram_media(POST)["metadata_status"] == "lookup_failed"


def test_no_token_is_reported_as_not_configured(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "")
    assert lookup_instagram_media(POST)["metadata_status"] == "not_configured"
