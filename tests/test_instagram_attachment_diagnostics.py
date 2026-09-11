"""
Evidence collection for shared-reel DMs, and the guarantees that go with it.

A reel shared into a DM arrives as an attachment with no message text, so it
never reaches source routing. Nothing in this repo has ever captured what Meta
puts inside that attachment, so the matcher cannot be designed yet. These
tests cover the diagnostic that answers that question: it must record whatever
keys actually arrive (never a guessed field name), must not leak signed URL
credentials into the buffer, must stay bounded and admin-only, and must leave
the customer's experience byte-for-byte unchanged.
"""
import os

os.environ.setdefault("DB_PATH", "test_camellia.db")

import pytest
from fastapi.testclient import TestClient

import camellia.app as app_module
from camellia.config import config
from camellia.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_buffer():
    app_module._ATTACHMENT_DEBUG_EVENTS.clear()
    yield
    app_module._ATTACHMENT_DEBUG_EVENTS.clear()


@pytest.fixture
def admin_headers():
    key = config.ADMIN_API_KEY or config.ADMIN_PIN
    return {"X-Admin-Key": key} if key else {}


def _event(attachments, **message_extra):
    message = {"mid": "m_1", "attachments": attachments}
    message.update(message_extra)
    return {"sender": {"id": "sender_1"}, "recipient": {"id": "page_1"}, "message": message}


def _capture(attachments, **message_extra):
    app_module._record_attachment_debug_event(_event(attachments, **message_extra), "sender_1")
    assert app_module._ATTACHMENT_DEBUG_EVENTS, "nothing captured"
    return app_module._ATTACHMENT_DEBUG_EVENTS[-1]


def test_unknown_field_names_are_reported_rather_than_guessed():
    """
    The whole point of the diagnostic: a key this code has never heard of has
    to survive into the report, or the evidence is worthless.
    """
    captured = _capture([
        {"type": "some_future_meta_type", "payload": {"undocumented_field": "abc123"}}
    ])
    assert captured["attachment_keys"] == [["payload", "type"]]
    assert captured["attachments"][0]["type"] == "some_future_meta_type"
    assert captured["attachments"][0]["payload"]["undocumented_field"] == "abc123"


def test_signed_url_credentials_are_stripped_but_the_path_survives():
    """A Meta CDN URL identifies the object in its path and carries
    short-lived credentials in its query string."""
    captured = _capture([
        {"type": "image", "payload": {"url": "https://scontent.cdninstagram.com/v/t51/123_n.jpg?oh=SECRET&oe=TOKEN"}}
    ])
    recorded = captured["attachments"][0]["payload"]["url"]
    assert recorded == "https://scontent.cdninstagram.com/v/t51/123_n.jpg?<query-removed>"
    assert "SECRET" not in recorded
    assert "TOKEN" not in recorded


def test_an_instagram_permalink_would_be_readable_if_meta_sends_one():
    """If a shared reel does carry a permalink, the shortcode has to be
    recoverable from what we recorded — that is the evidence that decides
    whether a URL-based matcher is even possible."""
    captured = _capture([
        {"type": "share", "payload": {"url": "https://www.instagram.com/reel/DdAnO6MTok1/?igshid=XYZ"}}
    ])
    recorded = captured["attachments"][0]["payload"]["url"]
    assert recorded.startswith("https://www.instagram.com/reel/DdAnO6MTok1/")
    assert "igshid" not in recorded


def test_credential_shaped_keys_are_never_stored():
    captured = _capture([
        {"type": "share", "payload": {"access_token": "EAAG-real-token", "id": "178414"}}
    ])
    payload = captured["attachments"][0]["payload"]
    assert payload["access_token"] == "<redacted-by-key-name>"
    assert "EAAG-real-token" not in str(captured)
    assert payload["id"] == "178414"


def test_reply_and_referral_metadata_are_captured_when_present():
    captured = _capture(
        [{"type": "share", "payload": {}}],
        reply_to={"story": {"id": "998877", "url": "https://instagram.com/stories/x/1/?token=s"}},
    )
    assert captured["reply_to"]["story"]["id"] == "998877"
    assert "token=s" not in str(captured["reply_to"])


def test_buffer_is_bounded():
    for index in range(app_module._ATTACHMENT_DEBUG_LIMIT + 7):
        _capture([{"type": "image", "payload": {"url": f"https://x.test/{index}.jpg"}}])
    assert len(app_module._ATTACHMENT_DEBUG_EVENTS) == app_module._ATTACHMENT_DEBUG_LIMIT


def test_capture_never_raises_on_a_malformed_event():
    """A diagnostic that can break the webhook is worse than missing evidence."""
    app_module._record_attachment_debug_event({"message": {"attachments": "not-a-list"}}, None)
    app_module._record_attachment_debug_event({}, None)
    assert len(app_module._ATTACHMENT_DEBUG_EVENTS) == 2


def test_debug_endpoint_requires_admin_auth():
    assert client.get("/admin/debug/instagram-attachments").status_code in (401, 403, 503)


def test_debug_endpoint_returns_captured_events_for_admin(admin_headers):
    if not admin_headers:
        pytest.skip("no admin credentials configured in this environment")
    _capture([{"type": "share", "payload": {"url": "https://www.instagram.com/reel/DdAnO6MTok1/"}}])
    resp = client.get("/admin/debug/instagram-attachments", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["events"][0]["attachment_count"] == 1


def test_attachment_reply_behaviour_is_unchanged(monkeypatch):
    """
    Evidence collection only: the customer still gets exactly the same
    "I can't open photos" reply, and nothing routes, classifies, or infers a
    source from the attachment.
    """
    queued = []
    monkeypatch.setattr(app_module, "_process_instagram_event", lambda *a, **k: queued.append(("routed", a)))

    class _Tasks:
        def add_task(self, fn, *args, **kwargs):
            queued.append((fn.__name__, args))

    payload = {
        "object": "instagram",
        "entry": [{"messaging": [{
            "sender": {"id": "sender_1"}, "recipient": {"id": "page_1"},
            "referral": {"source": "ADS", "ad_id": "ad_1"},
            "message": {"mid": "m_1", "attachments": [
                {"type": "share", "payload": {"url": "https://www.instagram.com/reel/DdAnO6MTok1/"}}
            ]},
        }]}],
    }
    result = app_module._dispatch_instagram_payload(payload, _Tasks())

    assert result["results"][0]["action"] == "queued_attachment_reply"
    assert [name for name, _ in queued] == ["_reply_to_unreadable_attachment"]
    # Captured for inspection, but no routing decision was taken from it.
    assert len(app_module._ATTACHMENT_DEBUG_EVENTS) == 1
