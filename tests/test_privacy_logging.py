"""Production logging must expose safe metadata, never payload contents."""

import logging

import pytest
from fastapi import HTTPException

from camellia import app as app_module
from camellia import ai_responder as ai_responder_module
from camellia.config import config


class _Tasks:
    def add_task(self, *_args, **_kwargs):
        pass


def test_signature_mismatch_log_does_not_expose_secret_material(caplog, monkeypatch):
    secret = "private-app-secret-value"
    monkeypatch.setattr(config, "META_APP_SECRET", secret)
    caplog.set_level(logging.WARNING, logger="camellia.app")

    with pytest.raises(HTTPException):
        app_module.verify_meta_signature(b"payload", "sha256=not-the-real-signature")

    assert secret not in caplog.text
    assert "not-the-real-signature" not in caplog.text
    assert "Expected HMAC" not in caplog.text


def test_instagram_webhook_log_contains_shape_not_customer_text(caplog, monkeypatch):
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", "17841452939315044")
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    private_text = "customer-private-message-9f6a"
    payload = {
        "object": "instagram",
        "entry": [{
            "id": "17841452939315044",
            "messaging": [{
                "sender": {"id": "customer-id"},
                "recipient": {"id": "17841452939315044"},
                "message": {"mid": "mid-1", "text": private_text},
            }],
        }],
    }
    caplog.set_level(logging.INFO, logger="camellia.app")

    result = app_module._dispatch_instagram_payload(payload, _Tasks())

    assert result["status"] == "processed"
    assert private_text not in caplog.text
    assert "Instagram Webhook Raw Payload" not in caplog.text
    assert "has_text" in caplog.text
    assert "sender_hash" in caplog.text


def test_ai_logs_use_hashes_not_customer_text(caplog, monkeypatch):
    private_text = "customer-private-ai-message-1a2b"
    monkeypatch.setattr(
        ai_responder_module.db,
        "search_learned_knowledge",
        lambda _message: {"verified_answer": "Approved answer.", "topic": "Safe topic"},
    )
    caplog.set_level(logging.INFO)

    response, matched, reason = ai_responder_module.ai_responder.generate_response(private_text)

    assert (response, matched, reason) == ("Approved answer.", True, "learned_knowledge")
    assert private_text not in caplog.text
    assert "message_hash=" in caplog.text
    assert "Body:" not in caplog.text
