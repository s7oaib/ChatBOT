"""Actual Meta-supplied images are display only; exact Story IDs stay authoritative."""
import json
import socket
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia import instagram_story_previews as previews
from camellia.config import config
from camellia.database import Database

MEDIA_URL = "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=123&signature=SECRET"


@pytest.fixture
def preview_db(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "stories.db"))
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(tmp_path / "menu.json"))
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    return database


def _image():
    stream = BytesIO()
    Image.new("RGB", (80, 120), "green").save(stream, format="PNG")
    return stream.getvalue(), "image/png"


def _record(database, story_id="123", **kwargs):
    return database.record_instagram_story_source(story_id, event_metadata={"source": "story_reply"}, **kwargs)


@pytest.mark.parametrize("field", ["url", "media_url", "thumbnail_url"])
def test_webhook_supplied_image_is_cached_without_signed_url_persistence(preview_db, field, caplog):
    event = {"message": {"mid": "message-1", "reply_to": {"story": {"id": "123", field: MEDIA_URL, "media_type": "IMAGE"}}}}
    with patch.object(previews, "_download", return_value=_image()) as download:
        app_module._record_story_reply(event, "replier", "123")
    download.assert_called_once_with(MEDIA_URL)
    assert previews.cached_path("123") is not None
    saved = preview_db.get_instagram_story_source("123")
    assert "SECRET" not in str(saved) and "signature=" not in str(saved)
    assert "SECRET" not in caplog.text
    assert previews.enrich(saved, preview_db)["preview_url"] == "/admin/instagram-story-sources/123/preview"
    with Image.open(previews.cached_path("123")) as image:
        assert image.format == "JPEG" and max(image.size) <= 640
        assert not image.getexif()
    assert preview_db.list_instagram_source_routes() == []


def test_webhook_caching_is_ephemeral_background_work_after_customer_tasks(preview_db):
    from fastapi import BackgroundTasks
    tasks = BackgroundTasks()
    durable = app_module._DurableInstagramTasks(tasks)
    event = {"sender": {"id": "replier"}, "recipient": {"id": "page"}, "message": {
        "mid": "preview-background", "text": "Tell me more",
        "reply_to": {"story": {"id": "123", "url": MEDIA_URL}},
    }}
    with patch.object(config, "INSTAGRAM_ACCOUNT_ID", ""), \
         patch.object(app_module.instagram_chatbot_control, "is_sender_allowed", return_value=True), \
         patch.object(previews, "_download") as download:
        app_module._dispatch_instagram_payload({"entry": [{"messaging": [event]}]}, durable)
    download.assert_not_called()
    assert tasks.tasks[-1].func is previews.cache_preview
    assert tasks.tasks[-1].args[:2] == ("123", MEDIA_URL)
    with preview_db.get_connection() as connection:
        rows = connection.execute("SELECT payload FROM instagram_inbound_jobs").fetchall()
    assert all("SECRET" not in row["payload"] for row in rows)


@pytest.mark.parametrize("story", [{"id": "123"}, {"id": "123", "url": "javascript:alert(1)"}, {"id": "123", "media_type": "VIDEO", "media_url": MEDIA_URL}])
def test_missing_invalid_or_full_video_media_keeps_placeholder(preview_db, story):
    with patch.object(previews, "_download") as download:
        app_module._record_story_reply({"message": {"reply_to": {"story": story}}}, "replier", "123")
    download.assert_not_called()
    assert previews.enrich(preview_db.get_instagram_story_source("123"), preview_db)["preview_url"] is None


@pytest.mark.parametrize("url", [
    "http://lookaside.fbsbx.com/a", "https://user:secret@lookaside.fbsbx.com/a",
    "https://127.0.0.1/a", "https://169.254.169.254/a", "https://[::1]/a",
    "https://fbcdn.net.attacker.example/a", "https://evilfbcdn.net/a",
    "https://lookaside.fbsbx.com:8080/a", "file:///etc/passwd",
])
def test_unsafe_hosts_schemes_credentials_and_ports_are_never_fetched(preview_db, url):
    _record(preview_db)
    with patch.object(previews, "_download") as download:
        assert previews.cache_preview("123", url, preview_db) is False
    download.assert_not_called()


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.2", "169.254.169.254", "::1", "::ffff:127.0.0.1"])
def test_trusted_cdn_resolving_to_private_address_is_rejected(preview_db, address):
    _record(preview_db)
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
    with patch.object(previews.socket, "getaddrinfo", return_value=addresses), \
         patch.object(previews.socket, "create_connection") as connect:
        assert not previews.cache_preview("123", MEDIA_URL, preview_db)
    connect.assert_not_called()


class _Response:
    def __init__(self, status=200, headers=None, chunks=None):
        self.status = status
        self.headers = headers or {"Content-Type": "image/png"}
        self.chunks = list(chunks or [])

    def getheader(self, key):
        return self.headers.get(key)

    def read(self, _size):
        return self.chunks.pop(0) if self.chunks else b""


def _network(responses):
    connection = MagicMock()
    connection.getresponse.side_effect = responses
    return connection


def test_download_uses_pinned_public_address_tls_hostname_and_safe_redirect():
    data, mime = _image()
    connection = _network([_Response(302, {"Location": "https://scontent.cdninstagram.com/thumb?sig=SECRET"}), _Response(chunks=[data])])
    with patch.object(previews, "_public_address", return_value="8.8.8.8") as resolve, \
         patch.object(previews.socket, "create_connection") as connect, \
         patch.object(previews.ssl, "create_default_context") as tls, \
         patch.object(previews.http.client, "HTTPSConnection", return_value=connection):
        assert previews._download(MEDIA_URL) == (data, mime)
    assert resolve.call_count == 2
    assert connect.call_args.args[0] == ("8.8.8.8", 443)
    assert connect.call_args.kwargs["timeout"] == 3
    assert tls.return_value.wrap_socket.call_args.kwargs["server_hostname"] == "scontent.cdninstagram.com"
    assert connection.close.call_count == 2


def test_redirect_to_unsafe_host_is_rejected_before_second_connection():
    connection = _network([_Response(302, {"Location": "https://127.0.0.1/private"})])
    with patch.object(previews, "_public_address", return_value="8.8.8.8"), \
         patch.object(previews.socket, "create_connection") as connect, \
         patch.object(previews.ssl, "create_default_context"), \
         patch.object(previews.http.client, "HTTPSConnection", return_value=connection):
        with pytest.raises(ValueError):
            previews._download(MEDIA_URL)
    assert connect.call_count == 1


@pytest.mark.parametrize("response", [
    _Response(403), _Response(404), _Response(200, {"Content-Type": "text/html"}),
    _Response(200, {"Content-Type": "video/mp4"}),
    _Response(200, {"Content-Type": "image/png", "Content-Length": str(previews.MAX_BYTES + 1)}),
])
def test_expired_broken_nonimage_and_oversized_responses_are_rejected(response):
    connection = _network([response])
    with patch.object(previews, "_public_address", return_value="8.8.8.8"), \
         patch.object(previews.socket, "create_connection"), \
         patch.object(previews.ssl, "create_default_context"), \
         patch.object(previews.http.client, "HTTPSConnection", return_value=connection):
        with pytest.raises(ValueError):
            previews._download(MEDIA_URL)


def test_streamed_image_body_cannot_exceed_byte_limit(monkeypatch):
    monkeypatch.setattr(previews, "MAX_BYTES", 4)
    connection = _network([_Response(chunks=[b"12345"])])
    with patch.object(previews, "_public_address", return_value="8.8.8.8"), \
         patch.object(previews.socket, "create_connection"), \
         patch.object(previews.ssl, "create_default_context"), \
         patch.object(previews.http.client, "HTTPSConnection", return_value=connection):
        with pytest.raises(ValueError, match="Oversized"):
            previews._download(MEDIA_URL)


@pytest.mark.parametrize("result", [(b"not-an-image", "image/png"), (b"x" * (previews.MAX_BYTES + 1), "image/jpeg")])
def test_invalid_image_bytes_do_not_create_preview_or_interrupt_observation(preview_db, result):
    with patch.object(previews, "_download", return_value=result):
        app_module._record_story_reply({"message": {"reply_to": {"story": {"id": "123", "url": MEDIA_URL}}}}, "replier", "123")
    assert preview_db.get_instagram_story_source("123")
    assert previews.cached_path("123") is None


def test_source_metadata_requires_explicit_fields_and_keeps_replier_separate(preview_db):
    preview_db.save_instagram_customer_identity("replier", username="customer.reply")
    payload = {"id": "123", "original_username": "original.owner", "original_account_id": "101", "original_media_id": "202", "permalink": "https://www.instagram.com/stories/owner/123/?signature=SECRET", "username": "not.source", "owner": {"username": "not.source.either"}}
    app_module._record_story_reply({"message": {"reply_to": {"story": payload}}}, "replier", "123")
    item = previews.enrich(preview_db.get_instagram_story_source("123"), preview_db)
    assert item["original_username"] == "original.owner"
    assert item["original_account_id"] == "101" and item["original_media_id"] == "202"
    assert item["replier_username"] == "customer.reply"
    assert "source_username" not in item
    assert "SECRET" not in json.dumps(item)
    assert previews.display_metadata({"username": "sender", "owner": {"username": "owner"}}) == {}
    assert preview_db.list_instagram_source_routes() == []


def test_identical_image_on_different_exact_ids_never_associates_routes(preview_db):
    _record(preview_db, "123")
    _record(preview_db, "456")
    route = preview_db.save_instagram_source_route({"source_type": "story", "media_id": "123", "routing_topic": "Chosen", "active": True})
    with patch.object(previews, "_download", return_value=_image()):
        assert previews.cache_preview("123", MEDIA_URL, preview_db)
        assert previews.cache_preview("456", MEDIA_URL, preview_db)
    assert previews.cached_path("123") != previews.cached_path("456")
    assert previews.cached_path("123").read_bytes() == previews.cached_path("456").read_bytes()
    assert preview_db.find_instagram_source_route("media_id", "456", source_type="story") is None
    assert preview_db.get_instagram_source_route(route["routing_id"])["media_id"] == "123"


def test_private_endpoint_and_permanent_delete_only_affect_exact_preview(preview_db):
    for story_id in ("123", "456"):
        _record(preview_db, story_id)
        with patch.object(previews, "_download", return_value=_image()):
            assert previews.cache_preview(story_id, MEDIA_URL, preview_db)
    client = TestClient(app_module.app)
    assert client.get("/admin/instagram-story-sources/123/preview").status_code in (401, 403)
    with patch.object(app_module, "require_admin"):
        response = client.get("/admin/instagram-story-sources/123/preview")
        assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
        # The existing Admin middleware enforces its stronger no-cache policy.
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert client.delete("/admin/instagram-story-sources/123").status_code == 200
        assert client.get("/admin/instagram-story-sources/123/preview").status_code == 404
    assert previews.cached_path("123") is None and previews.cached_path("456") is not None


def test_expiry_cleanup_retains_current_story_preview(preview_db):
    _record(preview_db, "old", seen_at="2026-01-01T00:00:00+00:00")
    _record(preview_db, "current")
    with patch.object(previews, "_download", return_value=_image()):
        assert previews.cache_preview("old", MEDIA_URL, preview_db)
        assert previews.cache_preview("current", MEDIA_URL, preview_db)
    preview_db.expire_webhook_instagram_stories()
    previews.cleanup(preview_db)
    assert previews.cached_path("old") is None
    assert previews.cached_path("current") is not None


def test_cleanup_failure_and_capacity_never_delete_current_or_break_processing(preview_db, monkeypatch):
    _record(preview_db, "123")
    _record(preview_db, "456")
    with patch.object(previews, "_download", return_value=_image()):
        assert previews.cache_preview("123", MEDIA_URL, preview_db)
    monkeypatch.setattr(previews, "MAX_FILES", 1)
    with patch.object(previews, "_download") as download:
        assert not previews.cache_preview("456", MEDIA_URL, preview_db)
    download.assert_not_called()
    assert previews.cached_path("123") is not None
    with patch.object(previews.Path, "unlink", side_effect=OSError("filesystem unavailable")):
        previews.delete_preview("123")
        previews.cleanup(preview_db)
    assert preview_db.get_instagram_story_source("123")


def test_cache_paths_cannot_follow_symlinks_or_customer_text(preview_db, tmp_path):
    story_id = "../../private-file"
    _record(preview_db, story_id)
    with patch.object(previews, "_download", return_value=_image()):
        assert previews.cache_preview(story_id, MEDIA_URL, preview_db)
    path = previews.cached_path(story_id)
    assert path.parent == tmp_path / "instagram_story_previews"
    assert "private-file" not in path.name and len(path.stem) == 64
    with patch.object(previews.Path, "is_symlink", return_value=True):
        assert previews.cached_path(story_id) is None
