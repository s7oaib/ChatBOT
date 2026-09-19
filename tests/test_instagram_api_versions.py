from unittest.mock import patch

from camellia import app as app_module
from camellia.config import config
from camellia.whatsapp_adapter import whatsapp_adapter


class _Response:
    status_code = 200
    headers = {}
    content = b'{"ok": true}'

    def json(self):
        return {"ok": True}

    def raise_for_status(self):
        return None


class _Client:
    def __init__(self):
        self.urls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.urls.append(url)
        return _Response()


def _send_and_capture_url(monkeypatch, token, page_id="page-123"):
    client = _Client()
    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", token)
    monkeypatch.setattr(config, "INSTAGRAM_PAGE_ID", page_id)
    monkeypatch.setattr(config, "INSTAGRAM_API_VERSION", "v25.0")
    monkeypatch.setattr(config, "WHATSAPP_API_VERSION", "v99.0")
    monkeypatch.setattr(app_module.httpx, "Client", lambda *args, **kwargs: client)
    with patch.object(app_module.db, "create_instagram_outbound_audit", return_value=None), \
         patch.object(app_module.db, "finalize_instagram_outbound_audit"), \
         patch.object(app_module, "delivery_monitor"):
        app_module.send_instagram_reply("recipient-1", "hello")
    return client.urls


def test_instagram_login_outbound_uses_instagram_api_version(monkeypatch):
    urls = _send_and_capture_url(monkeypatch, "IGAA_login_token")

    assert urls == ["https://graph.instagram.com/v25.0/me/messages"]
    assert all("v99.0" not in url for url in urls)


def test_instagram_facebook_host_outbound_uses_instagram_api_version(monkeypatch):
    urls = _send_and_capture_url(monkeypatch, "page_token")

    assert urls == ["https://graph.facebook.com/v25.0/page-123/messages"]
    assert all("v99.0" not in url for url in urls)


def test_instagram_messenger_profile_uses_instagram_api_version(monkeypatch):
    monkeypatch.setattr(config, "INSTAGRAM_API_VERSION", "v25.0")
    monkeypatch.setattr(config, "WHATSAPP_API_VERSION", "v99.0")

    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", "IGAA_login_token")
    login_url, _ = app_module._instagram_profile_url_and_identity()
    assert login_url == "https://graph.instagram.com/v25.0/me/messenger_profile"

    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", "page_token")
    monkeypatch.setattr(config, "INSTAGRAM_PAGE_ID", "page-123")
    page_url, _ = app_module._instagram_profile_url_and_identity()
    assert page_url == "https://graph.facebook.com/v25.0/page-123/messenger_profile"


def test_whatsapp_api_url_continues_to_use_whatsapp_api_version(monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_API_VERSION", "v19.0")
    monkeypatch.setattr(config, "INSTAGRAM_API_VERSION", "v25.0")
    monkeypatch.setattr(config, "WHATSAPP_PHONE_NUMBER_ID", "phone-123")

    assert whatsapp_adapter.api_url == (
        "https://graph.facebook.com/v19.0/phone-123/messages"
    )
