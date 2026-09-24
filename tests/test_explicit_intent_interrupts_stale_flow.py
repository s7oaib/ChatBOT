"""A named business subject interrupts unrelated persisted conversation slots."""

import pytest

from camellia.config import config
from camellia.responder import responder
from camellia.session_memory import session_memory


@pytest.fixture(autouse=True)
def deterministic(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "GROQ_ENABLED", False)
    monkeypatch.setattr(config, "STRUCTURED_NLU_ENABLED", False)


@pytest.mark.parametrize(
    "flow_flag,message",
    [
        ("awaiting_instagram_contact_confirmation", "hi tell me more about Kanchagolla"),
        ("awaiting_area_confirmation", "tell me more about Jolbhora"),
        ("awaiting_menu_choice", "show me Sondesh"),
        ("awaiting_product_price", "Jolbhora price"),
        ("awaiting_name", "hi tell me more about Kanchagolla"),
    ],
)
def test_explicit_subject_interrupts_unrelated_stale_flow(
    flow_flag, message,
):
    user_id = f"stale_flow_{flow_flag}"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts[flow_flag] = True
    reply, matched, template_id, _images = responder.match_and_respond(
        message, user_id=user_id, channel="instagram",
    )

    assert matched is False
    assert template_id is None
    assert "Manager of Kitchens will connect" in reply
    assert facts[flow_flag] is False
    assert facts["last_flow_interrupted"] == [flow_flag]


def test_bare_confirmation_is_not_interrupted():
    user_id = "stale_flow_bare_yes"
    session_memory.clear_session(user_id, "whatsapp")
    facts = session_memory.get_facts(user_id, "whatsapp")
    facts["awaiting_product_price"] = True

    _reply, _matched, _template_id, _images = responder.match_and_respond(
        "yes", user_id=user_id, channel="whatsapp",
    )

    # The named-subject interrupt is deliberately not involved for a bare
    # confirmation; the existing conversation-flow machinery owns it.
    assert "last_flow_interrupted" not in facts


def test_no_flow_unregistered_explicit_subject_still_fails_closed():
    user_id = "stale_flow_clean"
    session_memory.clear_session(user_id, "instagram")
    reply, matched, template_id, _images = responder.match_and_respond(
        "hi tell me more about Kanchagolla", user_id=user_id, channel="instagram",
    )
    assert matched is False
    assert template_id is None
    assert "Manager of Kitchens will connect" in reply
