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
            assert len(item["question"]) <= app_module._MAX_ICE_BREAKER_CHARS

    def test_there_are_no_more_than_meta_accepts(self):
        assert len(app_module.DEFAULT_ICE_BREAKERS) <= app_module._MAX_ICE_BREAKERS

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
