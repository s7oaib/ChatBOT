"""Explicit Sondesh overview requests outrank stale conversational state."""

import pytest

from camellia import client_qa
from camellia.config import config
from camellia.responder import responder
from camellia.session_memory import session_memory


EXPECTED_ASSETS = [
    "/images/AAMAR%20MISHTI.jpeg",
    "/images/Signature%20Sondesh.jpeg",
    "/images/From%20Friday.jpeg",
]


@pytest.mark.parametrize(
    "message",
    [
        "Tell me more about sondesh",
        "Can you tell me more about Sondesh?",
        "I want to know more about your sondesh",
        "More details about Sondesh please",
    ],
)
def test_explicit_sondesh_overview_uses_frozen_canonical_route(message, monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "sondesh-tell-more-clean-" + str(abs(hash(message)))
    session_memory.clear_session(user_id, "instagram")

    response, matched, route_id, assets = responder.match_and_respond(
        message, user_id=user_id, channel="instagram", referral_source="ADS",
    )
    facts = session_memory.get_facts(user_id, "instagram")

    assert matched is True
    assert route_id == "sondesh_tell_me_more"
    assert response == client_qa.REGISTRY_BY_ID["sondesh_details_client"].answer
    assert assets == EXPECTED_ASSETS
    assert "Which one would you like to see" not in response
    assert "CAM-" not in response
    assert facts["last_route_source"] == "EXACT_APPROVED"
    assert facts["last_llm_called"] is False


@pytest.mark.parametrize(
    "state",
    [
        {
            "pending_clarification": "product_topic",
            "clarification_options": ["SONDESH", "BENGALI_MEAL"],
            "presented_options": ["SONDESH", "BENGALI_MEAL"],
            "last_bot_question": "Which one would you like to see — Sondesh, Meals, or shall I help you place an order?",
            "active_topic": "savoury",
            "last_topic": "savoury",
            "active_entity": "BENGALI_MEAL",
        },
        {
            "active_topic": "savoury",
            "last_topic": "savoury",
            "active_entity": "BENGALI_MEAL",
        },
        {
            "source_route": {
                "routing_topic": "unrelated Story topic",
                "source_type": "story",
                "active": True,
            },
            "last_topic": "savoury",
            "active_topic": "savoury",
        },
    ],
    ids=["pending_product_choice", "previous_meals_topic", "unrelated_story_route"],
)
def test_explicit_sondesh_overview_clears_stale_state(state, monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "sondesh-tell-more-stale-" + str(abs(hash(repr(state))))
    session_memory.clear_session(user_id, "instagram")
    session_memory.get_facts(user_id, "instagram").update(state)

    response, matched, route_id, assets = responder.match_and_respond(
        "Tell me more about sondesh",
        user_id=user_id,
        channel="instagram",
        referral_source="ADS",
    )
    facts = session_memory.get_facts(user_id, "instagram")

    assert matched is True
    assert route_id == "sondesh_tell_me_more"
    assert response == client_qa.REGISTRY_BY_ID["sondesh_details_client"].answer
    assert assets == EXPECTED_ASSETS
    assert facts["pending_clarification"] is None
    assert facts["clarification_options"] == []
    assert facts["presented_options"] == []
    assert facts["last_bot_question"] is None
    assert facts["active_entity"] == "SONDESH"
    assert facts["active_topic"] == "sondesh_general"
    assert facts["source_context_superseded"] is True
    assert facts["last_llm_called"] is False
    assert "Which one would you like to see" not in response
    assert "CAM-" not in response
