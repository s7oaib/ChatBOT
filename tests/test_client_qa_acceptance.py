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
        ("tell me more about sondesh", "sondesh_tell_me_more"),
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
    assert template_id == "sondesh_tell_me_more"
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


def test_instagram_prebook_question_beats_stale_subscription_context(monkeypatch):
    monkeypatch.setattr("camellia.config.config.BYPASS_AD_GATE", True)
    user_id = "client-qa-prebook-stale-subscription"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["subscription_context"] = True
    facts["awaiting_subscription_booking"] = False

    response, matched, template_id, _images = responder.match_and_respond(
        "WHAT ARE THE NEXT STEPS TO PRE BOOK?",
        user_id=user_id,
        channel="instagram",
    )

    assert matched is True
    assert template_id == "alacarte_prebooking_steps"
    from camellia.client_qa import REGISTRY_BY_ID
    assert response == REGISTRY_BY_ID["alacarte_prebook"].answer


@pytest.mark.parametrize("message", [
    "how do i order",
    "how can i order",
    "how to order",
    "how do I place an order",
    "I want to place an order",
    "I want to order",
    "what are the next steps to order",
    "how do I pre book",
    "how can I pre-book",
])
def test_order_process_phrases_start_the_customer_order_draft(monkeypatch, message):
    from camellia.order_intake import ORDER_DRAFT_INCOMPLETE, instruction_message

    monkeypatch.setattr("camellia.config.config.BYPASS_AD_GATE", True)
    user_id = "client-qa-order-process-" + str(abs(hash(message)))
    session_memory.clear_session(user_id, "instagram")

    response, matched, template_id, _images = responder.match_and_respond(
        message, user_id=user_id, channel="instagram",
    )

    assert matched is True
    assert template_id == "order_intake_draft"
    assert response == instruction_message()
    assert session_memory.get_facts(user_id, "instagram")["order_intake_draft"]["status"] == ORDER_DRAFT_INCOMPLETE


@pytest.mark.parametrize(
    ("message", "expected_template"),
    [
        ("How do I pay", "GUARD_payment_method"),
        ("I've finalized my order", "order_next_steps"),
    ],
)
def test_payment_and_finalized_order_remain_distinct_from_order_process(monkeypatch, message, expected_template):
    monkeypatch.setattr("camellia.config.config.BYPASS_AD_GATE", True)
    user_id = "client-qa-distinct-order-intent-" + str(abs(hash(message)))
    session_memory.clear_session(user_id, "instagram")
    _response, _matched, template_id, _images = responder.match_and_respond(
        message, user_id=user_id, channel="instagram",
    )
    assert template_id == expected_template


def test_instagram_bengali_bento_details_beats_stale_story_context(monkeypatch):
    monkeypatch.setattr("camellia.config.config.BYPASS_AD_GATE", True)
    user_id = "client-qa-bento-stale-story"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {"routing_id": 1, "source_type": "story", "active": True}
    facts["source_context_superseded"] = True

    response, matched, template_id, _images = responder.match_and_respond(
        "tell me about BENGALI BENTO", user_id=user_id, channel="instagram",
    )

    assert matched is True
    assert template_id == "bento_details"
    assert "Vegetarian Bento" in response
    assert "₹350" in response


def test_unsupported_event_after_home_table_context_hands_off(monkeypatch):
    monkeypatch.setattr("camellia.config.config.BYPASS_AD_GATE", True)
    user_id = "client-qa-unsupported-event-after-story"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["home_table_context"] = True
    facts["source_route"] = {
        "routing_id": 5,
        "source_type": "story",
        "active": True,
        "routing_topic": "Bengali Home Table",
        "priority_products": [],
    }

    response, matched, template_id, _images = responder.match_and_respond(
        "Can you cater a wedding in Mumbai?",
        user_id=user_id,
        channel="instagram",
    )

    facts = session_memory.get_facts(user_id, "instagram")
    assert matched is False
    assert template_id == "groq_structured_event_escalation"
    assert facts["knowledge_sufficient"] is False
    assert "event_order" in facts["last_intents"]
    assert "ESCALATE_TO_TEAM" in facts["last_actions"]
    assert "bengali home table" not in response.lower()


def test_sensitive_question_after_home_table_context_uses_safety_lane(monkeypatch):
    monkeypatch.setattr("camellia.config.config.BYPASS_AD_GATE", True)
    user_id = "client-qa-sensitive-after-story"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["home_table_context"] = True
    facts["source_route"] = {
        "routing_id": 5,
        "source_type": "story",
        "active": True,
        "routing_topic": "Bengali Home Table",
        "priority_products": [],
    }

    response, matched, template_id, _images = responder.match_and_respond(
        "my child has a severe nut allergy is it safe",
        user_id=user_id,
        channel="instagram",
    )

    assert matched is True
    assert template_id == "GUARD_allergen_nutfree_glutenfree"
    assert "Bengali Home Table" not in response


def test_booking_help_after_home_table_context_uses_order_handoff(monkeypatch):
    monkeypatch.setattr("camellia.config.config.BYPASS_AD_GATE", True)
    user_id = "client-qa-booking-help-after-story"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["home_table_context"] = True
    facts["source_route"] = {
        "routing_id": 5,
        "source_type": "story",
        "active": True,
        "routing_topic": "Bengali Home Table",
        "priority_products": [],
    }

    response, matched, template_id, _images = responder.match_and_respond(
        "I am ready to place my order and need help booking.",
        user_id=user_id,
        channel="instagram",
    )

    assert matched is False
    assert template_id == "order_next_steps"
    assert "Bengali Home Table" not in response
