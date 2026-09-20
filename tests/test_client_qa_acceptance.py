"""Client-approved Q&A routing regressions for the September acceptance pass."""

import pytest

from camellia.responder import responder
from camellia.session_memory import session_memory


@pytest.mark.parametrize(
    ("message", "template_id"),
    [
        ("price is high", "pricing_value_context"),
        ("what is the price of chicken kosha", "chicken_kosha_price"),
        ("how much time do you take to deliver sondesh", "sondesh_dispatch_time"),
        ("tell me more about sondesh", "sondesh_details"),
        ("sondesh is unfamiliar to me", "sondesh_unfamiliar"),
        ("why is the minimum order 9 pieces 250g for some flavours", "minimum_sondesh_quantity"),
        ("share weekly menu", "savoury_food_intro"),
    ],
)
def test_client_questions_use_the_specific_approved_route(message, template_id):
    user_id = "client-qa-" + template_id
    session_memory.clear_session(user_id, "instagram")
    response, matched, actual_template, images = responder.match_and_respond(
        message, user_id=user_id, channel="instagram", referral_source="ADS",
    )
    assert matched is True
    assert actual_template == template_id
    assert response.strip()
    assert "INSERT " not in response
    assert "ATTACH " not in response
    assert "ADD LINK" not in response


def test_sondesh_overview_uses_available_menu_assets():
    session_memory.clear_session("client-qa-assets", "instagram")
    _response, matched, template_id, images = responder.match_and_respond(
        "tell me more about sondesh",
        user_id="client-qa-assets",
        channel="instagram",
        referral_source="ADS",
    )
    assert matched is True
    assert template_id == "sondesh_details"
    assert images


def test_general_can_you_tell_me_more_uses_food_overview_after_stale_instagram_route(monkeypatch):
    monkeypatch.setattr("camellia.config.config.BYPASS_AD_GATE", True)
    user_id = "client-qa-general-details"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {"routing_id": 1, "source_type": "story", "active": True}
    facts["source_context_superseded"] = True

    response, matched, template_id, _images = responder.match_and_respond(
        "Can you tell me more about this", user_id=user_id, channel="instagram",
    )

    assert matched is True
    assert template_id == "savoury_food_intro"
    assert "À LA CARTE" in response
    assert "BENGALI BENTO" in response
    assert "INSERT" not in response
