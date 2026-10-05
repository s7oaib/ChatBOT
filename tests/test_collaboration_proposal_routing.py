import uuid
from unittest.mock import patch

import pytest

from camellia.ai_responder import ai_responder
from camellia.responder import responder
from camellia.session_memory import session_memory
from camellia.understanding import Intent, understanding_engine


MOM_JAR_PROPOSAL = """I've been following along with the beautiful grazing tables you've been creating, your eye for detail and the way you curate everything is truly inspiring! I'm currently starting my Mother's brand, The Mom Jar, where I'm focused on creating wholesome, traditional snacks like Chidva, Millet suhali and Sattu Ladoos along with pickles (mango and mirchi). As I grow this passion project, I was wondering if you would be open to a collaboration where some of my products are featured in your upcoming events? I would love to see my creations paired with your curated menu, and I truly believe they would add a unique, traditional touch for your members as well. Let me know if you'd be open to this idea. Thank you."""


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

    facts = session_memory.get_facts(user_id, "instagram")
    assert "weekly_menu" not in str(template_id or "")
    assert "menu is" not in text.lower()
    assert "Manager of Kitchens" in text
    assert facts["last_intents"] == [Intent.BUSINESS_POLICY_UNKNOWN.value]
    assert "ESCALATE_TO_TEAM" in facts["last_actions"]
    assert images in (None, [])
