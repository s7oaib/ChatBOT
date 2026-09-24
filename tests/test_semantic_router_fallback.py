"""Live semantic routing may interpret intent, but never invents the reply."""

from unittest.mock import patch

from camellia.config import config
from camellia.rag_fallback import RAGResult
from camellia.responder import responder
from camellia.router import shadow_router
from camellia.session_memory import session_memory
from camellia.understanding import UnderstandingResult, understanding_engine


def _ask(message: str, user_id: str):
    session_memory.clear_session(user_id, "whatsapp")
    return responder.match_and_respond(message, user_id=user_id, channel="whatsapp")


def test_provider_failure_does_not_try_a_second_llm_router():
    # A router-model failure must fail closed instead of retrying the same
    # provider through a second classifier or using aggressive fuzzy matching.
    with patch.object(config, "SEMANTIC_ROUTER_ENABLED", True), \
         patch.object(understanding_engine, "analyze", return_value=UnderstandingResult(reason="provider_unavailable")), \
         patch.object(shadow_router, "classify", return_value=("weekly_menu_veg", "ok")) as classify:
        text, matched, template_id, _ = _ask("khana wala card bhejna", "semantic_hinglish")

    assert not classify.called
    assert matched is False
    assert template_id is None
    assert "manager of kitchens" in text.lower()


def test_semantic_fallback_refuses_to_auto_route_a_guarded_template():
    # Payment, allergy, staff and other GUARD answers need deterministic
    # anchors. An LLM router may suggest them, but cannot make them live.
    with patch.object(config, "SEMANTIC_ROUTER_ENABLED", True), \
         patch.object(config, "GROQ_ENABLED", False), \
         patch.object(shadow_router, "classify", return_value=("GUARD_payment_method", "ok")), \
         patch("camellia.responder.rag_fallback_engine.retrieve", return_value=RAGResult()):
        _, matched, template_id, _ = _ask("random unusual wording", "semantic_guard")

    assert matched is False
    assert template_id is None


def test_semantic_fallback_can_be_disabled_without_touching_normal_routing():
    with patch.object(config, "SEMANTIC_ROUTER_ENABLED", False), \
         patch.object(shadow_router, "classify") as classify, \
         patch("camellia.responder.rag_fallback_engine.retrieve", return_value=RAGResult()):
        _, matched, template_id, _ = _ask("khana wala card bhejna", "semantic_off")

    assert not classify.called
    assert matched is False
    assert template_id is None
