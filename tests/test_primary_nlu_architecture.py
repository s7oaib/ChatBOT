"""Acceptance coverage for the intent/entity-first response path."""

from unittest.mock import patch

import pytest

from camellia.config import config
from camellia.responder import responder
from camellia.rag_fallback import rag_fallback_engine
from camellia.ai_responder import ai_responder
from camellia.session_memory import session_memory
from camellia.understanding import understanding_engine


def _model_understanding(message, history_turns=None, state=None):
    result = understanding_engine.deterministic_fallback(message)
    result.reason = "model_nlu"
    result.available = True
    result.confidence = 0.91
    return result


def _reply(message, user_id):
    session_memory.clear_session(user_id, "whatsapp")
    with patch.object(understanding_engine, "analyze", side_effect=_model_understanding), patch(
        "camellia.responder.ai_responder.compose_verified_response",
        return_value=("", False, "provider_unavailable"),
    ):
        return responder.match_and_respond(message, user_id=user_id, channel="whatsapp")


@pytest.mark.parametrize(
    "message, expected_intents, expected_template, expected_text",
    [
        ("Show me the menu", ["menu_request"], "know_more", "Sondesh"),
        ("What do you have?", ["menu_request"], "know_more", "Meals"),
        ("What sweets do you have?", ["product_information"], "product_overview", "Sondesh"),
        ("What can I order from you?", ["menu_request"], "know_more", "Meals"),
        ("How can I place an order?", ["order_process"], "how_to_order", "WhatsApp"),
        ("What is the price?", ["product_price"], "price_clarify_product", "Sondesh or Bengali Meals"),
        ("What is the price of Aamar Mishti?", ["product_price"], "aamar_mishti_collection", "₹"),
        ("Do you deliver to Delhi?", ["delivery_query"], "delivery_area", "Delhi NCR"),
        (
            "Show me the menu and tell me if you deliver to Delhi",
            ["menu_request", "delivery_query"],
            "delivery_area",
            "Delhi NCR",
        ),
        (
            "What is the price of Jolbhora and do you deliver to Gurgaon?",
            ["product_price", "delivery_query"],
            "compound_product_price_delivery_query",
            "Jolbhora",
        ),
        ("I need 50 boxes for a wedding", ["event_order"], "groq_structured_event_escalation", "team"),
        ("Do you have valet parking?", ["unknown"], None, "team"),
        ("Can you give me a discount?", ["discount_request"], "compound_discount_request", "team"),
        ("Does Jolbhora contain almonds?", ["allergen_question"], "GUARD_allergen_nutfree_glutenfree", "nuts"),
        ("Is Jolbhora available today?", ["product_information", "availability_query"], "compound_product_information_availability_query", "team"),
        ("Hi, what is the price of Jolbhora?", ["product_price"], "jolbhora", "Jolbhora"),
    ],
)
def test_requested_message_set_routes_by_validated_meaning(
    message, expected_intents, expected_template, expected_text,
):
    text, _matched, template_id, _image = _reply(message, f"primary_{abs(hash(message))}")
    facts = session_memory.get_facts(f"primary_{abs(hash(message))}", "whatsapp")

    assert facts["last_intents"] == expected_intents
    assert template_id == expected_template
    assert expected_text.lower() in text.lower()
    assert facts["last_nlu_reason"] == "model_nlu"


def test_pure_greeting_skips_nlu_and_keeps_the_greeting_route():
    with patch.object(understanding_engine, "analyze") as analyze:
        text, matched, template_id, _ = _reply("Hi", "primary_pure_greeting")

    assert matched is True
    assert template_id == "greeting"
    assert "Camellia" in text
    analyze.assert_not_called()


def test_parking_is_a_gap_and_never_maps_to_packaging():
    text, matched, template_id, _ = _reply("Do you have valet parking?", "primary_parking")
    facts = session_memory.get_facts("primary_parking", "whatsapp")

    assert matched is False
    assert template_id is None
    assert "packag" not in text.lower()
    assert facts["last_knowledge_sufficiency"] == {"unknown": False}
    assert "ESCALATE_TO_TEAM" in facts["last_actions"]


def test_compound_request_records_each_action_and_knowledge_source():
    _reply("Show me the menu and tell me if you deliver to Delhi", "primary_compound")
    facts = session_memory.get_facts("primary_compound", "whatsapp")

    assert facts["last_knowledge_sufficiency"] == {
        "menu_request": True,
        "delivery_query": True,
    }
    assert facts["last_knowledge_ids"] == ["approved:know_more", "approved:delivery_area"]
    assert "SEND_MENU" in facts["last_actions"]
    assert "CONFIRM_DELIVERY_AREA" in facts["last_actions"]


def test_route_observability_is_written_after_the_reply():
    with patch.object(understanding_engine, "analyze", side_effect=_model_understanding), patch(
        "camellia.responder.ai_responder.compose_verified_response",
        return_value=("", False, "provider_unavailable"),
    ), patch("camellia.responder.db.log_message_route") as log_route:
        responder.match_and_respond("What is the price?", "primary_audit", "whatsapp")

    fields = log_route.call_args.kwargs
    assert fields["route"] == "price_clarify_product"
    assert fields["intents"] == ["product_price"]
    assert fields["knowledge_ids"] == ["conversation:price_clarification"]
    assert fields["grounding_result"].startswith("fallback:")
    assert fields["knowledge_gap"] is False


@pytest.mark.parametrize(
    "answer, context, reason_prefix",
    [
        ("Mahua is available today.", ["Ask our team about current availability."], "unsupported_availability"),
        ("We deliver to Mumbai.", ["We deliver across Delhi NCR."], "unsupported_location"),
        ("It contains almonds.", ["It is made with chhana and sugar."], "unsupported_ingredient"),
        ("Keep it refrigerated.", ["Please ask us how to store it."], "unsupported_storage"),
        ("It comes gift wrapped.", ["Food-safe packaging is used."], "unsupported_packaging"),
        ("You get a discount.", ["Prices are listed in the menu."], "unsupported_claim"),
        ("Email orders@example.com.", ["Please contact our manager."], "unsupported_contact"),
    ],
)
def test_grounding_guard_rejects_unsupported_business_claims(answer, context, reason_prefix):
    grounded, reason = rag_fallback_engine.grounding_report(answer, context)

    assert grounded is False
    assert reason.startswith(reason_prefix)


def test_grounding_guard_accepts_claims_present_in_selected_knowledge():
    grounded, reason = rag_fallback_engine.grounding_report(
        "We deliver across Delhi NCR in food-safe packaging.",
        ["We deliver across Delhi NCR. We use food-safe packaging."],
    )

    assert grounded is True
    assert reason == "grounded"


def test_verified_generation_receives_only_selected_facts_and_returns_structured_text():
    captured = {}

    def fake_query(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return ('{"status":"ok","response":"We deliver across Delhi NCR."}', True, "groq")

    with patch.object(ai_responder, "get_active_provider", return_value="groq"), patch.object(
        ai_responder, "_query_groq", side_effect=fake_query,
    ):
        text, ok, reason = ai_responder.compose_verified_response(
            "Do you deliver?",
            ["delivery_query"],
            {},
            ["We deliver across Delhi NCR."],
        )

    assert (text, ok, reason) == ("We deliver across Delhi NCR.", True, "grounded")
    assert "We deliver across Delhi NCR." in captured["prompt"]
    assert "weekly menu" not in captured["prompt"].lower()
    assert captured["kwargs"]["response_format"] == {"type": "json_object"}


class TestOpenSlotsAreAnsweredByTheirOwnHandler:
    """A reply to a question the bot asked means what the state says it means.

    "veg", "meals" and "yes i want to book" carry no business words at all.
    Sending them to generic understanding produced an unknown meaning and a
    handoff for a conversation the bot had itself opened, so an active slot
    keeps its deterministic handler and understanding stays out of the turn.
    """

    def _turn(self, message, user_id):
        with patch.object(understanding_engine, "analyze", side_effect=_model_understanding), patch(
            "camellia.responder.ai_responder.compose_verified_response",
            return_value=("", False, "provider_unavailable"),
        ):
            return responder.match_and_respond(message, user_id=user_id, channel="whatsapp")

    def test_the_price_clarification_runs_to_the_weekly_menu(self):
        user_id = "state_price_flow"
        session_memory.clear_session(user_id, "whatsapp")

        _text, _matched, clarify, _images = self._turn("how much", user_id)
        _text, _matched, meals, _images = self._turn("meals", user_id)
        _text, _matched, menu, _images = self._turn("veg", user_id)

        assert [clarify, meals, menu] == [
            "price_clarify_product", "context_meal_price_followup", "weekly_menu_veg",
        ]

    def test_a_subscription_conversation_is_not_taken_over_by_understanding(self):
        user_id = "state_subscription"
        session_memory.clear_session(user_id, "whatsapp")

        _text, _matched, details, _images = self._turn("tell me about the subscription", user_id)
        _text, _matched, booking, _images = self._turn("yes i want to book", user_id)
        text, _matched, reschedule, _images = self._turn(
            "what if i need to change a delivery date", user_id,
        )

        assert details == "subscription_details"
        assert booking == "subscription_booking_steps"
        assert reschedule == "subscription_booking_steps"
        assert "at least 5 hours in advance" in text

    def test_an_area_reply_is_still_read_as_an_area(self):
        user_id = "state_area"
        session_memory.clear_session(user_id, "whatsapp")

        self._turn("Do you deliver?", user_id)
        text, _matched, template_id, _images = self._turn("Chatra Marg", user_id)

        assert template_id == "delivery_area_confirmation"
        assert "chatra marg" in text.lower()


@pytest.mark.parametrize("message", [
    "Do you have valet parking?",
    "is there parking at your kitchen",
    "do you provide parking",
    "do you have wifi",
    "can we dine in",
])
def test_an_out_of_scope_topic_is_refused_by_the_deterministic_path_too(message):
    """With understanding switched off entirely, fuzzy matching used to answer
    a parking question with the packaging template."""
    user_id = f"legacy_scope_{abs(hash(message))}"
    session_memory.clear_session(user_id, "whatsapp")
    with patch.object(config, "STRUCTURED_NLU_ENABLED", False), patch.object(
        config, "SEMANTIC_ROUTER_ENABLED", False,
    ):
        text, matched, template_id, _images = responder.match_and_respond(
            message, user_id=user_id, channel="whatsapp",
        )

    assert matched is False
    assert template_id is None
    assert "cornstarch" not in text.lower()
    assert "team" in text.lower()
