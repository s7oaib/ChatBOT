from unittest.mock import patch

import pytest

from camellia import app as app_module
from camellia.ai_responder import ai_responder
from camellia.config import config
from camellia.handoff import customer_facing_notice
from camellia.responder import responder
from camellia.session_memory import session_memory


UNSUPPORTED_REPLY = (
    "Thanks for your question. Our Manager of Kitchens will connect with you "
    "here shortly and help you with this."
)


def test_approved_sondesh_price_paraphrase_uses_verbatim_client_answer(eligible_weekly_asset):
    user_id = "approved-only-price"
    session_memory.clear_session(user_id, "instagram")
    text, matched, template_id, _images = responder.match_and_respond(
        "What does Sondesh cost?", user_id=user_id, channel="instagram"
    )
    from camellia import client_qa
    assert matched is True
    assert template_id == "sondesh_price_detailed"
    assert text == client_qa.REGISTRY_BY_ID["sondesh_price_client"].answer


def test_unapproved_business_question_cannot_reach_rag_or_groq():
    user_id = "approved-only-wedding"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {
        "routing_id": 99,
        "source_type": "post",
        "media_id": "mapped-wedding-post",
        "active": True,
        "routing_topic": "Sondesh",
        "source_description": "Approved Sondesh post description",
        "priority_products": ["sondesh_details"],
    }
    with patch("camellia.responder.rag_fallback_engine.retrieve") as rag, patch.object(
        ai_responder, "_query_groq", return_value=("", False, "unexpected")
    ) as groq:
        text, matched, template_id, images = responder.match_and_respond(
            "Can you make 200 Sondesh boxes for my wedding?",
            user_id=user_id,
            channel="instagram",
        )
    assert (text, matched, template_id, images) == (UNSUPPORTED_REPLY, False, None, None)
    rag.assert_not_called()
    groq.assert_not_called()
    assert facts["last_route_source"] == "HUMAN_HANDOFF"
    assert facts["last_llm_called"] is False


def test_live_menu_uses_only_the_documented_deterministic_menu_source(eligible_weekly_asset):
    from datetime import date
    user_id = "approved-only-live-menu"
    session_memory.clear_session(user_id, "instagram")
    with patch("camellia.menu_dates.today_ist", return_value=date(2026, 6, 21)), patch("camellia.responder.rag_fallback_engine.retrieve") as rag, patch.object(
        ai_responder, "_query_groq", return_value=("", False, "unexpected")
    ) as groq:
        text, matched, template_id, _images = responder.match_and_respond(
            "Show me the vegetarian menu", user_id=user_id, channel="instagram",
        )

    assert matched is True
    assert template_id == "weekly_menu_veg"
    assert "This Week's Vegetarian Menu" in text
    assert "*Packaging*:" not in text and "*Delivery*:" not in text
    facts = session_memory.get_facts(user_id, "instagram")
    assert facts["last_route_source"] == "APPROVED_MENU_SOURCE"
    assert facts["last_grounding"] == "menu_json_deterministic"
    assert facts["last_llm_called"] is False
    rag.assert_not_called()
    groq.assert_not_called()


def test_unsupported_instagram_question_creates_handoff_with_exact_reply():
    user_id = "approved-only-handoff"
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app_module, "send_instagram_reply") as send, patch.object(
        app_module.escalator, "handle_escalation", return_value={"escalation_id": 1}
    ), patch.object(config, "LLM_ASSISTED_RESOLUTION_ENABLED", False), patch.object(
        config, "LLM_AUTO_RESOLVE_SIMULATION_ENABLED", False
    ):
        app_module._process_instagram_event(
            user_id, "Can you make 200 Sondesh boxes for my wedding?", "approved-only-ad",
            customer_identity={"customer_external_id": user_id, "customer_username": "qa_customer"},
        )
    send.assert_called_once()
    assert send.call_args.args[1] == UNSUPPORTED_REPLY == customer_facing_notice()
    assert app_module.db.get_active_handoff("instagram", user_id) is not None


def _set_jolbhora_source(user_id):
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {
        "routing_id": 123,
        "source_type": "reel",
        "active": True,
        "routing_topic": "Wedding / Event Sondesh",
        "source_description": "Mapped wedding Sondesh Reel description",
        "priority_products": ["jolbhora"],
    }
    return facts


@pytest.mark.parametrize("question", ["How much is this?", "Is this available?"])
def test_priority_product_cannot_answer_unsupported_source_references(question):
    user_id = f"priority-fact-{question.casefold().replace(' ', '-')}"
    facts = _set_jolbhora_source(user_id)
    with patch("camellia.responder.rag_fallback_engine.retrieve") as rag, patch.object(
        ai_responder, "_query_groq", return_value=("", False, "unexpected")
    ) as groq, patch.object(responder, "_template_by_id", wraps=responder._template_by_id) as template:
        text, matched, template_id, images = responder.match_and_respond(
            question, user_id=user_id, channel="instagram",
        )

    assert (text, matched, template_id, images) == (UNSUPPORTED_REPLY, False, None, None)
    assert facts["last_route_source"] == "HUMAN_HANDOFF"
    rag.assert_not_called()
    groq.assert_not_called()
    assert not any(call.args[0] == "jolbhora" for call in template.call_args_list)


def test_explicit_sondesh_price_beats_mapped_priority_product(eligible_weekly_asset):
    user_id = "priority-explicit-sondesh-price"
    _set_jolbhora_source(user_id)
    with patch("camellia.responder.rag_fallback_engine.retrieve") as rag, patch.object(
        ai_responder, "_query_groq", return_value=("", False, "unexpected")
    ) as groq:
        text, matched, template_id, _images = responder.match_and_respond(
            "What is the price of Sondesh?", user_id=user_id, channel="instagram",
        )

    from camellia import client_qa
    assert matched is True
    assert template_id == "sondesh_price_detailed"
    assert text == client_qa.REGISTRY_BY_ID["sondesh_price_client"].answer
    assert session_memory.get_facts(user_id, "instagram")["last_route_source"] == "EXACT_APPROVED"
    rag.assert_not_called()
    groq.assert_not_called()
