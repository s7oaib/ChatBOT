"""Security and behavior tests for the read-only Story capability probe."""

from unittest.mock import Mock

import httpx
from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia import instagram_routing


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append((url, headers or {}, params or {}))
        for suffix, response in self.responses.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected probe URL: {url}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _successful_client():
    return _Client({
        "/me": _Response({"id": "17841400000000000"}),
        "/me/media": _Response({
            "data": [{
                "id": "feed-1",
                "media_type": "IMAGE",
                "timestamp": "2026-09-15T10:00:00+0000",
                "media_url": "https://cdn.example/feed.jpg?sig=secret",
            }],
        }),
        "/17841400000000000/stories": _Response({
            "data": [{
                "id": "18371255653212260",
                "media_type": "IMAGE",
                "media_product_type": "STORY",
                "timestamp": "2026-09-15T10:01:00+0000",
                "media_url": "https://cdn.example/story.jpg?token=signed-secret",
                "thumbnail_url": "https://cdn.example/thumb.jpg?token=signed-secret",
            }],
        }),
    })


def test_probe_lists_story_ids_with_bearer_auth_and_no_sensitive_fields(monkeypatch):
    token = "IGAA-production-secret"
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", token)
    client = _successful_client()
    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **_: client)

    result = instagram_routing.probe_instagram_story_capability()

    assert result["status"] == "success"
    assert result["story_listing_supported"] is True
    assert result["discovery_method"] == "instagram_login_story_edge"
    assert result["count"] == 1
    assert result["stories"] == [{
        "id": "18371255653212260",
        "media_type": "IMAGE",
        "media_product_type": "STORY",
        "timestamp": "2026-09-15T10:01:00+0000",
    }]
    output = str(result)
    assert token not in output
    assert "media_url" not in output
    assert "signed-secret" not in output
    assert all("access_token" not in url for url, _, _ in client.calls)
    assert all(headers["Authorization"] == f"Bearer {token}" for _, headers, _ in client.calls)


def test_probe_distinguishes_supported_zero_from_unsupported_edge(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAA-token")
    client = _Client({
        "/me": _Response({"id": "17841400000000000"}),
        "/me/media": _Response({"data": []}),
        "/17841400000000000/stories": _Response({"data": []}),
    })
    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **_: client)
    supported = instagram_routing.probe_instagram_story_capability()
    assert supported["status"] == "success"
    assert supported["story_listing_supported"] is True
    assert supported["count"] == 0

    client = _Client({
        "/me": _Response({"id": "17841400000000000"}),
        "/me/media": _Response({"data": []}),
        "/17841400000000000/stories": _Response(
            {"error": {"code": 100, "error_subcode": 2534014, "message": "private provider detail"}},
            status_code=400,
        ),
    })
    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **_: client)
    unsupported = instagram_routing.probe_instagram_story_capability()
    assert unsupported["status"] == "error"
    assert unsupported["story_listing_supported"] is False
    assert unsupported["meta_code"] == 100
    assert unsupported["meta_subcode"] == 2534014
    assert unsupported["message_class"] == "WRONG_ACCOUNT_OR_TOKEN_CONTEXT"
    assert "private provider detail" not in str(unsupported)


def test_probe_sanitizes_permission_errors_and_does_not_touch_db_or_sender(monkeypatch):
    token = "IGAA-token"
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", token)
    client = _Client({
        "/me": _Response({"id": "17841400000000000"}),
        "/me/media": _Response({
            "error": {"code": 10, "error_subcode": 123, "message": "raw permission detail"}
        }, status_code=403),
    })
    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **_: client)
    db_call = Mock(side_effect=AssertionError("probe must not use the database"))
    sender_call = Mock(side_effect=AssertionError("probe must not send a message"))
    monkeypatch.setattr(app_module.db, "get_connection", db_call)
    monkeypatch.setattr(app_module, "send_instagram_reply", sender_call)

    result = instagram_routing.probe_instagram_story_capability()

    assert result["status"] == "error"
    assert result["stage"] == "owned_media"
    assert result["meta_http_status"] == 403
    assert result["meta_code"] == 10
    assert result["message_class"] == "PERMISSION_OR_ENDPOINT_ERROR"
    assert "raw permission detail" not in str(result)
    db_call.assert_not_called()
    sender_call.assert_not_called()


def test_capability_probe_is_not_wired_to_an_admin_endpoint(monkeypatch):
    """The probe determined the connected account's Page-token config is
    UNSUPPORTED_TOKEN_MODE for /me/stories -- there is no live capability to
    surface as an admin action, so this stays an internal diagnostic
    function only (reused by the active-sync feature in
    instagram_routing.fetch_active_instagram_stories) and is deliberately
    not exposed as its own admin endpoint."""
    token = "IGAA-token-never-rendered"
    monkeypatch.setattr(app_module.config, "INSTAGRAM_ACCESS_TOKEN", token)
    monkeypatch.setattr(app_module.config, "ADMIN_API_KEY", "probe-admin-key")
    client = TestClient(app_module.app)

    assert client.get("/admin/instagram-story-capability").status_code == 404
    assert token not in client.get("/admin").text


def test_probe_transport_failure_is_sanitized(monkeypatch):
    monkeypatch.setattr(instagram_routing.config, "INSTAGRAM_ACCESS_TOKEN", "IGAA-token")

    class _FailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *args, **kwargs):
            raise httpx.ConnectError("url=https://graph.instagram.com/private?access_token=secret")

    monkeypatch.setattr(instagram_routing.httpx, "Client", lambda **_: _FailingClient())
    result = instagram_routing.probe_instagram_story_capability()
    assert result == {
        "status": "error",
        "story_listing_supported": False,
        "message_class": "PROVIDER_FAILURE",
    }
