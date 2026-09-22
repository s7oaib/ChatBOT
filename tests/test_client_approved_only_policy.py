from unittest.mock import patch

from camellia import app as app_module
from camellia.config import config
from camellia.handoff import customer_facing_notice
from camellia.responder import responder
from camellia.session_memory import session_memory


UNSUPPORTED_REPLY = (
    "Thanks for your question. Our Manager of Kitchens will connect with you "
    "here shortly and help you with this."
)


def test_approved_sondesh_price_paraphrase_uses_verbatim_client_answer():
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
    with patch("camellia.responder.rag_fallback_engine.retrieve") as rag:
        text, matched, template_id, images = responder.match_and_respond(
            "Can you make 200 Sondesh boxes for my wedding?",
            user_id=user_id,
            channel="instagram",
        )
    assert (text, matched, template_id, images) == (UNSUPPORTED_REPLY, False, None, None)
    rag.assert_not_called()


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
