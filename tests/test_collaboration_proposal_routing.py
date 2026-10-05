import uuid
from unittest.mock import patch

import pytest

from camellia.ai_responder import ai_responder
from camellia.responder import responder
from camellia.session_memory import session_memory
from camellia.understanding import Intent, is_collaboration_proposal, understanding_engine


MOM_JAR_PROPOSAL = """I've been following along with the beautiful grazing tables you've been creating, your eye for detail and the way you curate everything is truly inspiring! I'm currently starting my Mother's brand, The Mom Jar, where I'm focused on creating wholesome, traditional snacks like Chidva, Millet suhali and Sattu Ladoos along with pickles (mango and mirchi). As I grow this passion project, I was wondering if you would be open to a collaboration where some of my products are featured in your upcoming events? I would love to see my creations paired with your curated menu, and I truly believe they would add a unique, traditional touch for your members as well. Let me know if you'd be open to this idea. Thank you."""

EXACT_MOM_JAR_PROPOSAL = """I’ve been following along with the beautiful grazing tables you’ve been
creating, your eye for detail and the way you curate everything is truly
inspiring!

I’m currently starting my Mother’s brand, The Mom Jar, where I’m focused on
creating wholesome, traditional snacks like Chidva, Millet suhali and Sattu
Ladoos along with pickles (mango and mirchi)

As I grow this passion project, I was wondering if you would be open to a
collaboration where some of my products are featured in your upcoming events?

I would love to see my creations paired with your curated menu, and I truly
believe they would add a unique, traditional touch for your members as well

Let me know if you’d be open to this idea.

Thank you ☺️"""


@pytest.mark.parametrize("message", [
    EXACT_MOM_JAR_PROPOSAL,
    # Synthetic platform-shaped suffix, not a claim about production payloads.
    EXACT_MOM_JAR_PROPOSAL + ' <img src="https://static.cdninstagram.com/emoji.png" alt="☺️">',
    "Would you be open to a collaboration where our products are featured in your curated menu?",
    "We'd love to partner with Camellia for your upcoming events and have our snacks included in the menu.",
    "Can we work together? We'd like you to stock our products: Chidva and Sattu Ladoos.",
    "I run a food brand and would like to collaborate with you.",
])
@pytest.mark.parametrize("context", ["weekly", "product", "source"])
def test_proposal_precedes_available_flyer_and_stale_context(message, context, monkeypatch, eligible_weekly_asset):
    user_id = "collab-precedence-" + uuid.uuid4().hex
    facts = session_memory.get_facts(user_id, "instagram")
    if context == "weekly":
        facts.update(active_topic="savoury", last_topic="savoury", awaiting_menu_choice=True)
    elif context == "product":
        facts.update(active_product_context="bento", ad_product="bento", awaiting_product_price=True)
    else:
        facts["source_route"] = {"routing_id": "existing-test-route", "source_type": "story",
                                 "active": True, "source_description": "Old product menu"}
    # The deterministic collaboration handoff must work even with NLU disabled.
    monkeypatch.setattr("camellia.responder.config.SEMANTIC_ROUTER_ENABLED", False)
    monkeypatch.setattr("camellia.responder.config.STRUCTURED_NLU_ENABLED", False)
    with patch.object(ai_responder, "_query_groq", side_effect=AssertionError("No model needed")):
        text, matched, template_id, images = responder.match_and_respond(message, user_id=user_id, channel="instagram")
    assert matched is False
    assert template_id not in {"approved_menu", "weekly_menu", "weekly_menu_veg", "weekly_menu_nonveg"}
    assert text == responder._generic_fallback()
    assert "Manager of Kitchens" in text
    assert images in (None, [])
    assert "WEEKLY_SAVOURY_BROWSE" not in facts["last_intents"]
    assert facts["last_intents"] == [Intent.BUSINESS_POLICY_UNKNOWN.value]
    assert facts["last_entities"] == {"topic": "collaboration proposal"}
    assert "SEND_PRODUCT_IMAGE" not in facts["last_actions"]


@pytest.mark.parametrize("message", [
    "Can I see your menu?", "Please send me this week's menu.",
    "What is on the weekly menu?", "Veg menu please.",
    "What's on your menu this week?", "Can you send the current menu?",
])
def test_genuine_menu_still_sends_available_approved_flyer(message, eligible_weekly_asset):
    user_id = "menu-precedence-" + uuid.uuid4().hex
    text, matched, template_id, images = responder.match_and_respond(message, user_id=user_id, channel="instagram")
    assert matched is True
    assert template_id == "approved_menu"
    assert images == [eligible_weekly_asset]
    assert session_memory.get_facts(user_id, "instagram")["last_route_source"] == "APPROVED_MENU"


@pytest.mark.parametrize("message", [
    "collaboration", "collaborate", "partnership", "partner with you",
    "partner with Camellia", "work together", "brand collaboration",
    "feature our products", "stock my products", "carry our products",
    "supplier proposal", "vendor proposal",
])
def test_shared_proposal_rule(message):
    assert is_collaboration_proposal(message)
    assert understanding_engine.analyze(message).entities == {"topic": "collaboration proposal"}


@pytest.mark.parametrize("message", ["event", "brand", "products", "menu", "Can I see your menu?"])
def test_shared_rule_does_not_claim_vague_business_words(message):
    assert not is_collaboration_proposal(message)


def test_the_mom_jar_proposal_is_human_only_not_menu_request():
    reading = understanding_engine.deterministic_fallback(MOM_JAR_PROPOSAL)

    assert reading.intents == [Intent.BUSINESS_POLICY_UNKNOWN.value]
    assert Intent.MENU_REQUEST.value not in reading.intents
    assert reading.entities == {"topic": "collaboration proposal"}
    assert reading.requires_human is True
    assert reading.knowledge_gap is True
    assert reading.reason == "deterministic_fallback_collaboration_guard"


def test_collaboration_guard_runs_before_model_and_configuration():
    with patch.object(ai_responder, "_query_groq", side_effect=AssertionError("NLU model must not run")):
        reading = understanding_engine.analyze(
            "Would you be open to a brand collaboration? We'd love to feature some of our products in your curated menu."
        )

    assert reading.intents == [Intent.BUSINESS_POLICY_UNKNOWN.value]
    assert reading.entities["topic"] == "collaboration proposal"
    assert reading.requires_human is True
    assert reading.knowledge_gap is True
    assert reading.reason == "deterministic_collaboration_guard"


@pytest.mark.parametrize(
    "message",
    [
        "Can I see your menu?",
        "Please send me this week's menu",
        "What is on the weekly menu?",
        "Veg menu please",
    ],
)
def test_real_menu_requests_still_classify_as_menu_requests(message):
    reading = understanding_engine.deterministic_fallback(message)
    assert Intent.MENU_REQUEST.value in reading.intents
    assert reading.entities.get("topic") != "collaboration proposal"


def test_collaboration_proposal_end_to_end_cannot_return_weekly_menu():
    user_id = "collab-" + uuid.uuid4().hex
    session_memory.clear_session(user_id, "instagram")

    with patch.object(ai_responder, "_query_groq", side_effect=AssertionError("LLM must not be needed")):
        text, _matched, template_id, images = responder.match_and_respond(
            MOM_JAR_PROPOSAL,
            user_id=user_id,
            channel="instagram",
        )

    # Validate the customer-visible contract. Some deterministic handoff paths
    # intentionally do not persist structured-NLU bookkeeping such as
    # last_intents; that internal memory detail is not the safety boundary.
    assert "weekly_menu" not in str(template_id or "")
    assert "menu is" not in text.lower()
    assert "Manager of Kitchens" in text
    assert images in (None, [])
