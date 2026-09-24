"""Focused regression tests for Instagram account filtering and hours copy."""

import logging
from unittest.mock import patch
import uuid

import pytest

from camellia import app as app_module
from camellia.config import config
from camellia.responder import responder
from camellia.session_memory import session_memory


INSTAGRAM_ACCOUNT_ID = "17841452939315044"


class _Tasks:
    def __init__(self):
        self.queued = []

    def add_task(self, fn, *args, **kwargs):
        self.queued.append((fn, args, kwargs))


def _message_event(text="What is the price of jolbhora?"):
    return {
        "sender": {"id": "customer-1"},
        "recipient": {"id": "17841452939315044"},
        "message": {"mid": "message-1", "text": text},
    }


def _payload(entry_id=None, event=None):
    entry = {"messaging": [event or _message_event()]}
    if entry_id is not None:
        entry["id"] = entry_id
    return {"object": "instagram", "entry": [entry]}


@pytest.mark.parametrize("message", [
    "opening hours",
    "What are your opening hours?",
    "What are the opening hours?",
    "When are you open?",
    "When do you open?",
    "What time are you open?",
    "What time do you open?",
    "kitchen hours",
    "kitchen opening hours",
])
def test_approved_opening_hours_variants_use_canonical_answer(message, monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "hours-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "instagram")

    response, matched, template_id, _images = responder.match_and_respond(
        message, user_id=user_id, channel="instagram",
    )

    facts = session_memory.get_facts(user_id, "instagram")
    assert matched is True
    assert template_id == "opening_hours"
    assert response == "Our kitchen is open 10:00 AM – 7:30 PM every day."
    assert facts["last_route_source"] == "explicit_rule"
    assert facts["last_grounding"] == "grounded_approved"


def test_approved_opening_hours_does_not_create_a_handoff(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "hours-no-escalation-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app_module, "send_instagram_reply") as send, patch.object(
        app_module.escalator, "handle_escalation"
    ) as escalate:
        app_module._process_instagram_event(user_id, "opening hours", None)

    assert send.called
    assert not escalate.called
    assert send.call_args.args[1] == "Our kitchen is open 10:00 AM – 7:30 PM every day."
    assert "wa.me" not in send.call_args.args[1]


def test_matching_instagram_account_entry_is_processed(monkeypatch):
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", INSTAGRAM_ACCOUNT_ID)
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    tasks = _Tasks()

    result = app_module._dispatch_instagram_payload(
        _payload(INSTAGRAM_ACCOUNT_ID), tasks,
    )

    assert result["status"] == "processed"
    assert result["results"][0]["action"] == "queued"
    assert len(tasks.queued) == 1


def test_account_guard_uses_exact_webhook_entry_identifier(monkeypatch):
    assert app_module._instagram_account_guard_matches("professional-id", "professional-id")
    assert not app_module._instagram_account_guard_matches("oauth-profile-id", "professional-id")


def test_foreign_instagram_account_entry_is_ignored_before_dispatch(monkeypatch, caplog):
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", INSTAGRAM_ACCOUNT_ID)
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    caplog.set_level(logging.INFO, logger="camellia.app")
    tasks = _Tasks()

    with patch.object(app_module.ad_gate, "inspect_and_gate_message") as inspect, patch.object(
        app_module, "_process_instagram_event"
    ) as process, patch.object(app_module, "send_instagram_reply") as send, patch.object(
        app_module.escalator, "handle_escalation"
    ) as escalate, patch.object(app_module.responder, "match_and_respond") as match, patch.object(
        app_module.db, "log_conversation"
    ) as log:
        result = app_module._dispatch_instagram_payload(
            _payload("17841402372443585"), tasks,
        )

    assert result == {"status": "processed", "results": []}
    assert tasks.queued == []
    assert not inspect.called
    assert not process.called
    assert not send.called
    assert not escalate.called
    assert not match.called
    assert not log.called
    assert "foreign account" in caplog.text
    assert "17841402372443585" not in caplog.text
    assert INSTAGRAM_ACCOUNT_ID not in caplog.text


def test_missing_instagram_account_guard_preserves_compatibility(monkeypatch):
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", "")
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    tasks = _Tasks()

    result = app_module._dispatch_instagram_payload(
        _payload(event=_message_event("hello")), tasks,
    )

    assert result["results"][0]["action"] == "queued"
    assert len(tasks.queued) == 1


@pytest.mark.parametrize("event", [
    {
        "sender": {"id": "page"},
        "message": {"mid": "echo-1", "is_echo": True, "text": "hello"},
    },
    {"sender": {"id": "customer-1"}, "read": {"watermark": 1}},
    {
        "sender": {"id": "customer-1"},
        "message": {"reaction": {"action": "react", "reaction": "❤️"}},
    },
])
def test_valid_account_protocol_events_remain_non_dispatching(monkeypatch, event):
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", INSTAGRAM_ACCOUNT_ID)
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    tasks = _Tasks()

    app_module._dispatch_instagram_payload(_payload(INSTAGRAM_ACCOUNT_ID, event), tasks)

    assert tasks.queued == []
