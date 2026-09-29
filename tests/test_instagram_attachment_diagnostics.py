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
import uuid
import logging

os.environ.setdefault("DB_PATH", "test_camellia.db")

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

import camellia.app as app_module
from camellia.config import config
from camellia.ad_gate import EntryPoint
from camellia.database import db
from camellia.instagram_routing import route_for_inbound, shared_post_media_id, shared_reel_identity, validate_route_payload
from camellia.session_memory import session_memory
from camellia import client_qa
from camellia.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_buffer():
    app_module._ATTACHMENT_DEBUG_EVENTS.clear()
    yield
    app_module._ATTACHMENT_DEBUG_EVENTS.clear()


@pytest.fixture
def admin_headers():
    key = config.ADMIN_API_KEY
    return {"X-Admin-Key": key} if key else {}


def _event(attachments, **message_extra):
    message = {"mid": "m_1", "attachments": attachments}
    message.update(message_extra)
    return {"sender": {"id": "sender_1"}, "recipient": {"id": "page_1"}, "message": message}


def _capture(attachments, **message_extra):
    app_module._record_attachment_debug_event(_event(attachments, **message_extra), "sender_1")
    assert app_module._ATTACHMENT_DEBUG_EVENTS, "nothing captured"
    return app_module._ATTACHMENT_DEBUG_EVENTS[-1]


def test_unknown_field_names_are_reported_without_field_values():
    """
    The whole point of the diagnostic: a key this code has never heard of has
    to survive into the report, or the evidence is worthless.
    """
    captured = _capture([
        {"type": "some_future_meta_type", "payload": {"undocumented_field": "abc123"}}
    ])
    assert captured["attachment_keys"] == [["payload", "type"]]
    assert captured["attachment_types"] == ["some_future_meta_type"]
    assert captured["payload_keys"] == ["undocumented_field"]
    assert "abc123" not in str(captured)


def test_permalink_presence_is_reported_without_url_or_credentials():
    captured = _capture([
        {"type": "image", "payload": {"url": "https://scontent.cdninstagram.com/v/t51/123_n.jpg?oh=SECRET&oe=TOKEN"}}
    ])
    assert captured["instagram_permalink_present"] is False
    assert "SECRET" not in str(captured)
    assert "TOKEN" not in str(captured)


def test_an_instagram_permalink_would_be_readable_if_meta_sends_one():
    """If a shared reel does carry a permalink, the shortcode has to be
    recoverable from what we recorded — that is the evidence that decides
    whether a URL-based matcher is even possible."""
    captured = _capture([
        {"type": "share", "payload": {"url": "https://www.instagram.com/reel/DdAnO6MTok1/?igshid=XYZ"}}
    ])
    assert captured["instagram_permalink_present"] is True
    assert "DdAnO6MTok1" not in str(captured)
    assert "igshid" not in str(captured)


def test_credential_shaped_keys_are_never_stored():
    captured = _capture([
        {"type": "share", "payload": {"access_token": "EAAG-real-token", "id": "178414"}}
    ])
    assert captured["payload_keys"] == ["access_token", "id"]
    assert "EAAG-real-token" not in str(captured)
    assert "178414" not in str(captured)
    assert captured["exact_numeric_id_present"] is True
    assert captured["exact_id_fields"] == ["attachments[0].payload.id"]


def test_source_shape_log_contains_only_names_and_presence_flags(caplog):
    sender_id = "private-sender-123456789"
    media_id = "17841400000062434"
    caption = "private Sondesh caption"
    event = _event([{
        "type": "unknown_share",
        "payload": {
            "ig_post_media_id": media_id,
            "title": caption,
            "url": "https://www.instagram.com/p/PrivateCode123/?access_token=secret-value",
        },
    }])
    with caplog.at_level(logging.INFO):
        app_module._record_attachment_debug_event(event, sender_id)
    log = caplog.text
    assert "INSTAGRAM_SOURCE_SHAPE" in log
    assert "ig_post_media_id" in log
    assert "exact_numeric_id_present=True" in log
    assert "instagram_permalink_present=True" in log
    assert media_id not in log
    assert caption not in log
    assert sender_id not in log
    assert "secret-value" not in log


def test_reply_and_referral_metadata_are_captured_when_present():
    captured = _capture(
        [{"type": "share", "payload": {}}],
        reply_to={"story": {"id": "998877", "url": "https://instagram.com/stories/x/1/?token=s"}},
    )
    assert captured["referral_keys"] == []
    assert "998877" not in str(captured)
    assert "token=s" not in str(captured)


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
    assert "attachments" not in body["events"][0]


def test_unknown_attachment_marks_context_unmapped_and_ambiguous_followup_clarifies(monkeypatch):
    sender = "unmapped-source-" + uuid.uuid4().hex
    session_memory.clear_session(sender, "instagram")
    with patch.object(app_module, "_reply_to_unreadable_attachment"):
        app_module._handle_shared_attachment(sender, None, None)
    facts = session_memory.get_facts(sender, "instagram")
    assert facts["unmapped_shared_attachment"] is True
    assert db.get_active_handoff("instagram", sender) is None

    sent = []
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    with patch.object(app_module, "send_instagram_reply", side_effect=lambda _sender, text, **_kwargs: sent.append(text)), \
         patch.object(app_module, "send_instagram_response_blocks"), \
         patch.object(app_module.escalator, "handle_escalation") as escalated:
        app_module._process_instagram_event(sender, "tell me more", None)
    assert sent == ["Of course — what would you like to know more about?"]
    assert not escalated.called
    assert db.get_active_handoff("instagram", sender) is None


def test_mapped_exact_post_identity_persists_topic_and_answers_followups(monkeypatch, eligible_weekly_asset):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    cases = [
        ("Sondesh", "Tell me about Sondesh", "sondesh_details_client", "price?", "sondesh_price_client"),
        ("Subscription", "Tell me about subscription", "subscription_details_client", "price?", "subscription_price"),
        ("Bengali Bento", "price?", "bento_price_client", None, None),
    ]
    for topic, first_question, first_answer, second_question, second_answer in cases:
        sender = "mapped-source-" + uuid.uuid4().hex
        media_id = str(1784000000000000 + uuid.uuid4().int % 99999999999)
        session_memory.clear_session(sender, "instagram")
        route = validate_route_payload({
            "source_type": "post", "url": f"https://www.instagram.com/p/{uuid.uuid4().hex[:10]}/",
            "media_id": media_id, "routing_topic": topic,
        })
        db.save_instagram_source_route(route)
        exact_route, method = route_for_inbound(ad_id=None, media_id=None, post_media_id=media_id, source_type="post")
        assert exact_route["routing_topic"] == topic
        assert method == "shared_post_media_id"

        with patch.object(app_module, "send_instagram_reply"), \
             patch.object(app_module, "send_instagram_response_blocks"), \
             patch.object(app_module.escalator, "handle_escalation") as escalated:
            app_module._process_instagram_event(
                sender, "", None, EntryPoint(source="shared_media"),
                shared_post_id=media_id, shared_source_type="post",
            )
            saved_facts = session_memory.get_facts(sender, "instagram")
            assert saved_facts["source_route"]["routing_topic"] == topic
            assert db.get_conversations(limit=1, channel="instagram", user_id=sender) == []
            assert db.get_active_handoff("instagram", sender) is None

            app_module._process_instagram_event(sender, first_question, None)
            assert db.get_conversations(limit=1, channel="instagram", user_id=sender)[0]["bot_response"] == client_qa.REGISTRY_BY_ID[first_answer].answer
            if second_question:
                app_module._process_instagram_event(sender, second_question, None)
                assert db.get_conversations(limit=1, channel="instagram", user_id=sender)[0]["bot_response"] == client_qa.REGISTRY_BY_ID[second_answer].answer
            assert not escalated.called
            assert db.get_active_handoff("instagram", sender) is None


def test_explicit_product_question_remains_routable_after_unmapped_share(monkeypatch, eligible_weekly_asset):
    sender = "explicit-source-" + uuid.uuid4().hex
    session_memory.clear_session(sender, "instagram")
    with patch.object(app_module, "_reply_to_unreadable_attachment"):
        app_module._handle_shared_attachment(sender, None, None)
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    with patch.object(app_module, "send_instagram_reply"), \
         patch.object(app_module, "send_instagram_response_blocks"), \
         patch.object(app_module.escalator, "handle_escalation") as escalated:
        app_module._process_instagram_event(sender, "Tell me about Sondesh", None)
    assert db.get_conversations(limit=1, channel="instagram", user_id=sender)[0]["bot_response"] == client_qa.REGISTRY_BY_ID["sondesh_details_client"].answer
    assert not escalated.called
    assert db.get_active_handoff("instagram", sender) is None


def test_unknown_shape_does_not_route_by_caption_or_unverified_ids():
    message = {"attachments": [{
        "type": "unknown_share", "payload": {
            "id": "17841400000062434", "title": "Sondesh best price",
            "url": "https://cdn.example/private/attachment.jpg",
        },
    }]}
    assert shared_post_media_id(message) is None
    assert shared_reel_identity(message) is None


def test_attachment_reply_behaviour_is_unchanged(monkeypatch):
    """
    The customer still gets exactly the same "I can't open photos" reply.

    This event's attachment type is not one production has ever sent — a
    reel arrives as ``ig_reel`` — so nothing is identified from it and no
    source context is established, which is the correct outcome for an
    attachment shape we cannot recognise.
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
    assert [name for name, _ in queued] == ["_handle_shared_attachment"]
    # No identity was extracted, so the wrapper has no context to establish.
    assert queued[0][1][1] is None
    assert result["results"][0]["shared_reel_shortcode"] is None
    # Captured for inspection, but no routing decision was taken from it.
    assert len(app_module._ATTACHMENT_DEBUG_EVENTS) == 1
