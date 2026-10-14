"""The handoff message is editable in Admin and every handoff path uses it."""
from unittest.mock import patch

from fastapi.testclient import TestClient

from camellia import app as app_module, handoff
from camellia.ad_gate import EntryPoint
from camellia.config import config
from camellia.responder import responder
from test_ad_mapped_source_context import ad_customer, _process  # noqa: F401

NEW_MESSAGE = ("Thank you for your question. Our Manager of Kitchens will get in touch with you here "
               "shortly to help. In the meantime, you can also call or WhatsApp him on 8796551990.")


def _client():
    return TestClient(app_module.app), {"x-admin-key": config.ADMIN_API_KEY}


def test_default_message_is_unchanged_until_admin_edits_it(ad_customer):
    client, headers = _client()
    state = client.get("/admin/handoff-message", headers=headers).json()
    assert state["message"] == handoff.DEFAULT_HANDOFF_MESSAGE
    assert state["is_custom"] is False
    assert responder._generic_fallback() == handoff.customer_facing_notice() == handoff.DEFAULT_HANDOFF_MESSAGE


def test_saved_message_is_what_a_handed_off_customer_receives(ad_customer):
    sender, facts, db = ad_customer
    client, headers = _client()
    saved = client.put("/admin/handoff-message", headers=headers, json={"message": f"  {NEW_MESSAGE}  "})
    assert saved.status_code == 200, saved.text
    assert saved.json()["message"] == NEW_MESSAGE and saved.json()["is_custom"] is True

    send = _process(ad_customer, "Can you arrange a violin player?", EntryPoint(source="typed"))

    assert facts["last_route_source"] == "HUMAN_HANDOFF"
    assert db.get_active_handoff("instagram", sender)
    assert db.get_conversations(limit=1, channel="instagram", user_id=sender)[0]["bot_response"] == NEW_MESSAGE
    assert send.call_args_list[0].args[1] == NEW_MESSAGE
    assert handoff.customer_facing_notice() == NEW_MESSAGE
    # Order-specific notices keep their own approved copy.
    assert handoff.customer_facing_notice(order_receipt=True) != NEW_MESSAGE


def test_reset_restores_the_original_message(ad_customer):
    client, headers = _client()
    client.put("/admin/handoff-message", headers=headers, json={"message": NEW_MESSAGE})
    reset = client.post("/admin/handoff-message/reset", headers=headers)
    assert reset.status_code == 200
    assert reset.json()["message"] == handoff.DEFAULT_HANDOFF_MESSAGE
    assert reset.json()["is_custom"] is False
    assert responder._generic_fallback() == handoff.DEFAULT_HANDOFF_MESSAGE


def test_blank_or_overlong_message_is_rejected_and_nothing_changes(ad_customer):
    client, headers = _client()
    for message in ("", "   ", "x" * (handoff.HANDOFF_MESSAGE_MAX_LENGTH + 1)):
        assert client.put("/admin/handoff-message", headers=headers, json={"message": message}).status_code == 400
    assert handoff.default_handoff_message() == handoff.DEFAULT_HANDOFF_MESSAGE


def test_handoff_message_requires_admin(ad_customer):
    client = TestClient(app_module.app)
    assert client.get("/admin/handoff-message").status_code in (401, 403)
    assert client.put("/admin/handoff-message", json={"message": NEW_MESSAGE}).status_code in (401, 403)
    assert handoff.default_handoff_message() == handoff.DEFAULT_HANDOFF_MESSAGE


def test_unreadable_override_falls_back_to_the_default(ad_customer):
    with patch.object(handoff.db, "get_response_template_override", side_effect=RuntimeError("db down")):
        assert handoff.default_handoff_message() == handoff.DEFAULT_HANDOFF_MESSAGE
