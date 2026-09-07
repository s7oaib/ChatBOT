"""
Writing the Ice Breakers Instagram shows before a customer types anything.

Every customer sees these, so the guards matter: the four defaults must be
questions the bot actually answers well, the write must be reversible, and a
tap must be recorded as a tap rather than as someone typing.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from camellia import app as app_module
from camellia.ad_gate import describe_entry_point
from camellia.responder import responder
from camellia.session_memory import session_memory


class _Body:
    def __init__(self, data):
        self._data = data

    async def json(self):
        if self._data is None:
            raise ValueError("no body")
        return self._data


def _meta(status=200, body=None):
    resp = MagicMock(status_code=status, content=b"{}")
    resp.json.return_value = body if body is not None else {"success": True}
    resp.text = json.dumps(body or {})
    client = MagicMock()
    client.__enter__.return_value.post.return_value = resp
    client.__enter__.return_value.request.return_value = resp
    return client


def _post(body=None, client=None):
    """Drives the async endpoint from a sync test; pytest-asyncio is not installed."""
    with patch.object(app_module, "require_admin"), \
         patch.object(app_module.config, "INSTAGRAM_ACCESS_TOKEN", "IGAAtoken"), \
         patch.object(app_module.httpx, "Client", return_value=client or _meta()), \
         patch.object(app_module, "get_instagram_messaging_profile", return_value={"ok": True}):
        return asyncio.run(
            app_module.set_instagram_ice_breakers(_Body(body), x_admin_key="k", x_admin_pin=None)
        )


class TestTheDefaultsAreAnswerable:
    """An opener the bot fumbles is worse than no opener, because all see it."""

    @pytest.mark.parametrize("expected_template, question", [
        ("know_more", "Show me the menu"),
        ("sondesh_price_detailed", "Sondesh flavours & prices"),
        ("delivery_area", "Do you deliver to my area?"),
        ("how_to_order", "I would like to place an order"),
    ])
    def test_each_default_routes_to_the_answer_it_promises(self, expected_template, question):
        user_id = f"ib_{abs(hash(question))}"
        session_memory.clear_session(user_id, "whatsapp")
        reply = responder.match_and_respond(question, user_id=user_id, channel="whatsapp")

        assert reply[1] is True
        assert reply[2] == expected_template

    def test_every_default_fits_metas_character_limit(self):
        for item in app_module.DEFAULT_ICE_BREAKERS:
            assert len(item["question"]) <= app_module.MAX_ICE_BREAKER_CHARS

    def test_there_are_no_more_than_meta_accepts(self):
        assert len(app_module.DEFAULT_ICE_BREAKERS) <= app_module.MAX_ICE_BREAKERS

    def test_a_tap_on_a_default_is_recorded_as_a_tap_not_as_typing(self):
        # The payloads say ICE_BREAKER so the Phase 1 entry-point logging can
        # tell a tapped prompt from someone typing the same words.
        for item in app_module.DEFAULT_ICE_BREAKERS:
            entry = describe_entry_point({
                "sender": {"id": "u1"},
                "postback": {"title": item["question"], "payload": item["payload"]},
            })
            assert entry.source == "ice_breaker"


class TestWriting:
    def test_the_defaults_are_sent_when_no_body_is_given(self):
        client = _meta()
        _post(None, client)
        sent = client.__enter__.return_value.post.call_args.kwargs["json"]
        questions = [c["question"] for c in sent["ice_breakers"][0]["call_to_actions"]]
        assert questions == [i["question"] for i in app_module.DEFAULT_ICE_BREAKERS]

    def test_custom_questions_are_accepted(self):
        client = _meta()
        _post({"questions": [{"question": "Gift boxes?"}]}, client)
        sent = client.__enter__.return_value.post.call_args.kwargs["json"]
        assert sent["ice_breakers"][0]["call_to_actions"][0]["question"] == "Gift boxes?"

    def test_more_than_four_is_refused_before_reaching_meta(self):
        client = _meta()
        with pytest.raises(HTTPException) as e:
            _post({"questions": [{"question": f"q{i}"} for i in range(5)]}, client)
        assert e.value.status_code == 400
        assert not client.__enter__.return_value.post.called

    def test_an_overlong_question_is_refused_before_reaching_meta(self):
        client = _meta()
        with pytest.raises(HTTPException) as e:
            _post({"questions": [{"question": "x" * 81}]}, client)
        assert e.value.status_code == 400
        assert not client.__enter__.return_value.post.called

    def test_a_rejection_from_meta_is_surfaced_not_reported_as_success(self):
        client = _meta(400, {"error": {"message": "Invalid parameter", "code": 100}})
        with pytest.raises(HTTPException) as e:
            _post(None, client)
        assert e.value.status_code == 502
        assert "Invalid parameter" in e.value.detail

    def test_the_live_state_is_read_back_rather_than_assumed(self):
        # Meta accepting a write is not the same as the change being live.
        result = _post(None, _meta())
        assert "now_live" in result


class TestRemoval:
    def test_the_change_can_be_undone(self):
        client = _meta()
        with patch.object(app_module, "require_admin"), \
             patch.object(app_module.config, "INSTAGRAM_ACCESS_TOKEN", "IGAAtoken"), \
             patch.object(app_module.httpx, "Client", return_value=client):
            out = app_module.clear_instagram_ice_breakers(x_admin_key="k", x_admin_pin=None)

        assert out["status"] == "cleared"
        method, _url = client.__enter__.return_value.request.call_args.args
        assert method == "DELETE"
        assert client.__enter__.return_value.request.call_args.kwargs["json"] == {"fields": ["ice_breakers"]}


class TestATappedPromptIsAnswered:
    """
    The failure this class exists for: the ice breakers went live, a customer
    tapped "Show me the menu", and got silence. Instagram sent a postback with
    no `title`, so the text extraction produced "" and the dispatcher discarded
    the event as empty before it ever reached a reply.
    """

    def test_a_tap_without_a_title_still_becomes_the_question(self):
        from camellia.ad_gate import ad_gate

        event = {
            "sender": {"id": "u1"},
            "recipient": {"id": "p1"},
            "postback": {"mid": "m1", "payload": "ICE_BREAKER_MENU"},
        }
        _ok, _sender, text, _ad = ad_gate.inspect_and_gate_message(event)
        assert text == "Show me the menu"

    def test_a_tap_with_a_title_still_prefers_the_title(self):
        from camellia.ad_gate import ad_gate

        event = {
            "sender": {"id": "u1"},
            "postback": {"mid": "m1", "title": "Show me the menu", "payload": "ICE_BREAKER_MENU"},
        }
        assert ad_gate.inspect_and_gate_message(event)[2] == "Show me the menu"

    def test_an_unknown_ice_breaker_payload_still_yields_usable_words(self):
        from camellia.ice_breakers import question_for_payload

        assert question_for_payload("ICE_BREAKER_GIFT_BOXES") == "gift boxes"

    def test_a_payload_we_do_not_recognise_at_all_is_not_invented(self):
        from camellia.ice_breakers import question_for_payload

        assert question_for_payload("") == ""

    def test_every_default_payload_maps_back_to_its_own_question(self):
        from camellia.ice_breakers import DEFAULT_ICE_BREAKERS, question_for_payload

        for item in DEFAULT_ICE_BREAKERS:
            assert question_for_payload(item["payload"]) == item["question"]

    def test_a_tap_reaches_the_reply_path_instead_of_being_dropped(self):
        tasks = MagicMock()
        tasks.queued = []
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "tap_user"},
            "recipient": {"id": "page"},
            "postback": {"mid": "m1", "payload": "ICE_BREAKER_MENU"},
        }]}]}

        with patch.object(app_module.config, "BYPASS_AD_GATE", True):
            result = app_module._dispatch_instagram_payload(payload, tasks)

        assert result["results"][0]["action"] == "queued"
        assert result["results"][0]["message_source"] == "ice_breaker"
        assert tasks.add_task.called
        assert "Show me the menu" in tasks.add_task.call_args.args


class TestDroppedEventsAreVisible:
    def test_an_event_with_no_text_is_recorded_rather_than_vanishing(self):
        app_module._DROPPED_WEBHOOK_EVENTS.clear()
        tasks = MagicMock()
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "silent_user"},
            "message": {"mid": "m9", "attachments": [{"type": "image"}]},
        }]}]}

        with patch.object(app_module.config, "BYPASS_AD_GATE", True):
            result = app_module._dispatch_instagram_payload(payload, tasks)

        assert result["results"][0]["action"] == "ignored_empty_text"
        assert len(app_module._DROPPED_WEBHOOK_EVENTS) == 1
        assert app_module._DROPPED_WEBHOOK_EVENTS[0]["reason"] == "empty_text"

    def test_no_customer_text_is_retained_in_the_drop_record(self):
        app_module._DROPPED_WEBHOOK_EVENTS.clear()
        tasks = MagicMock()
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "u2"},
            "message": {"mid": "m1", "attachments": [{"type": "image", "payload": {"url": "secret"}}]},
        }]}]}

        with patch.object(app_module.config, "BYPASS_AD_GATE", True):
            app_module._dispatch_instagram_payload(payload, tasks)

        assert "secret" not in json.dumps(app_module._DROPPED_WEBHOOK_EVENTS)

    def test_the_buffer_cannot_grow_without_bound(self):
        app_module._DROPPED_WEBHOOK_EVENTS.clear()
        for i in range(40):
            app_module._record_dropped_webhook_event("empty_text", {"postback": {}}, f"u{i}")
        assert len(app_module._DROPPED_WEBHOOK_EVENTS) == app_module._DROPPED_WEBHOOK_LIMIT
