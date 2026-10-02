"""What the bot may and may not do while structured understanding is down.

Every shape of provider failure — a rate limit, an HTTP error, a dropped
connection, a truncated body, a schema violation — means the same thing: the
meaning of the message was never established. The rules that follow from that:

  * no second LLM router guessing a template from the same message
  * no unsafe near-miss answer (the parking/packaging failure)
  * open conversation states still finish their own flows
  * complaints, cancellations and "speak to a human" still reach a human
  * only questions whose answer states no business fact are answered blind
"""

import uuid
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from camellia.ai_responder import ai_responder
from camellia.config import config
from camellia.responder import responder
from camellia.router import shadow_router
from camellia.session_memory import session_memory
from camellia.understanding import understanding_engine

# Transport-level failures: the call itself did not come back.
PROVIDER_FAILURES = [
    ("", False, "groq_http_429"),          # rate limited
    ("", False, "groq_http_503"),          # provider outage
    ("", False, "groq_exception_ConnectError"),
    ("", False, "groq_exception_ReadTimeout"),
    ("", False, "groq_no_choices"),
]

# Body-level failures: a reply arrived, but it is not a meaning we can trust.
UNUSABLE_BODIES = [
    ("not json at all", True, "groq_rag"),
    ('{"intents":["menu_request"],"entities":{},"confidence":0.9,'
     '"requires_human":false,"knowledge_gap":"false"}', True, "groq_rag"),
]


class _Outage(ExitStack):
    """A reachable provider whose calls fail, which is the interesting case.

    A provider already in cooldown never gets asked and is covered by the
    plain provider_unavailable path; these tests pin the failure shape.
    """

    def __init__(self, failure):
        super().__init__()
        self._failure = failure

    def __enter__(self):
        super().__enter__()
        self.enter_context(patch.object(ai_responder, "get_active_provider", return_value="groq"))
        self.enter_context(patch.object(config, "STRUCTURED_NLU_FALLBACK_MODEL", ""))
        self.enter_context(patch.object(ai_responder, "_query_groq", return_value=self._failure))
        return self


def _outage(failure):
    """Patch the understanding provider only; the rest of the stack is real."""
    return _Outage(failure)


def _unusable_understanding(body):
    """Only the understanding call sees the bad body, as in production."""
    def analyze(user_message, history_turns=None, state=None):
        with _Outage(body):
            return understanding_engine.__class__.analyze(
                understanding_engine, user_message,
                history_turns=history_turns, state=state,
            )
    return patch.object(understanding_engine, "analyze", side_effect=analyze)


def _ask(message, user_id, channel="whatsapp"):
    return responder.match_and_respond(message, user_id=user_id, channel=channel)


def _fresh(prefix):
    user_id = f"{prefix}_{uuid.uuid4().hex[:8]}"
    session_memory.clear_session(user_id, "whatsapp")
    return user_id


@pytest.mark.parametrize("failure", PROVIDER_FAILURES)
def test_no_second_llm_router_runs_after_understanding_fails(failure):
    user_id = _fresh("outage_router")
    with _outage(failure), patch.object(config, "SEMANTIC_ROUTER_ENABLED", True), patch.object(
        shadow_router, "classify", return_value=("weekly_menu_veg", "ok"),
    ) as classify:
        _text, matched, template_id, _images = _ask("khana wala card bhejna", user_id)

    assert not classify.called
    assert matched is False
    assert template_id is None


@pytest.mark.parametrize("body", UNUSABLE_BODIES)
def test_an_untrusted_model_body_also_stops_the_second_router(body):
    user_id = _fresh("outage_body")
    with _unusable_understanding(body), patch.object(
        config, "SEMANTIC_ROUTER_ENABLED", True,
    ), patch.object(
        shadow_router, "classify", return_value=("weekly_menu_veg", "ok"),
    ) as classify:
        _ask("khana wala card bhejna", user_id)

    facts = session_memory.get_facts(user_id, "whatsapp")
    assert not classify.called
    assert facts["structured_nlu_provider_failed"] is True
    assert facts["last_nlu_reason"] in {"invalid_json", "invalid_schema"}


@pytest.mark.parametrize("failure", PROVIDER_FAILURES)
def test_an_unsupported_topic_is_never_answered_with_a_near_miss(failure):
    user_id = _fresh("outage_parking")
    with _outage(failure):
        text, matched, template_id, _images = _ask("Do you have valet parking?", user_id)

    assert matched is False
    assert template_id is None
    assert "packag" not in text.lower()
    assert "Manager of Kitchens" in text


@pytest.mark.parametrize("message", [
    "cancel my order",
    "wrong order delivered",
    "my order is missing",
    "I want to speak to a human",
])
def test_a_complaint_reaches_a_human_even_with_no_open_flow(message):
    """A customer cancelling must never be answered with ordering instructions."""
    user_id = _fresh("outage_complaint")
    with _outage(PROVIDER_FAILURES[0]):
        text, matched, _template_id, _images = _ask(message, user_id)

    assert matched is False
    assert "Manager of Kitchens" in text


@pytest.mark.parametrize("message", [
    "how much",
    "kitna padega",
    "kitne paise",
    "what does it cost",
    "iska price",
])
def test_a_bare_price_question_asks_which_product_instead_of_guessing(message):
    user_id = _fresh("outage_price")
    with _outage(PROVIDER_FAILURES[0]):
        text, matched, template_id, _images = _ask(message, user_id)

    assert matched is True
    assert template_id == "price_clarify_product"
    assert "sondesh" in text.lower() and "meal" in text.lower()
    assert "₹" not in text


def test_a_named_flavour_price_is_still_answered_during_an_outage():
    """The clarification is for bare questions only, not for a named product."""
    user_id = _fresh("outage_named_price")
    with _outage(PROVIDER_FAILURES[0]):
        _text, matched, template_id, _images = _ask("real vanilla price", user_id)

    assert matched is False
    assert "Manager of Kitchens" in _text
    assert "₹" not in _text
    assert template_id == "sondesh_mango_kesar"


class TestOpenStatesKeepTheirOwnFlow:
    def test_an_area_answer_still_completes_the_delivery_slot(self):
        user_id = _fresh("outage_area")
        with _outage(PROVIDER_FAILURES[0]):
            _ask("Do you deliver?", user_id)
            text, _matched, template_id, _images = _ask("Chatra Marg", user_id)

        assert template_id is None
        assert "Manager of Kitchens" in text
        assert "Delhi NCR" not in text

    def test_a_price_question_inside_the_delivery_slot_is_not_read_as_an_area(self):
        user_id = _fresh("outage_area_price")
        with _outage(PROVIDER_FAILURES[0]):
            _ask("Do you deliver?", user_id)
            _text, _matched, template_id, _images = _ask("what is the price", user_id)

        assert template_id == "price_clarify_product"

    def test_the_price_clarification_continues_into_the_meal_menu(self):
        user_id = _fresh("outage_price_flow")
        with _outage(PROVIDER_FAILURES[0]):
            _ask("how much", user_id)
            _text, _matched, meal_route, _images = _ask("meals", user_id)
            _text, _matched, menu_route, _images = _ask("veg", user_id)

        assert meal_route == "context_meal_price_followup"
        assert menu_route == "weekly_menu_veg"

    def test_delivery_wording_inside_a_subscription_flow_stays_on_the_subscription(self):
        user_id = _fresh("outage_subscription")
        with _outage(PROVIDER_FAILURES[0]):
            _ask("tell me about the subscription", user_id)
            _ask("yes i want to book", user_id)
            text, _matched, template_id, _images = _ask(
                "what if i need to change a delivery date", user_id,
            )

        assert template_id == "subscription_booking_steps"
        assert "Manager of Kitchens" in text
        assert "5 hours" not in text


def test_the_audit_row_records_the_failure_instead_of_claiming_a_model_answer():
    user_id = _fresh("outage_audit")
    with _outage(PROVIDER_FAILURES[0]), patch(
        "camellia.responder.db.log_message_route",
    ) as log_route:
        _ask("Do you have valet parking?", user_id)

    fields = log_route.call_args.kwargs
    facts = session_memory.get_facts(user_id, "whatsapp")
    assert fields["fallback_used"] is True
    assert fields["knowledge_gap"] is True
    assert fields["escalation_status"] == "required"
    assert facts["last_nlu_reason"] == "groq_http_429"
    assert facts["structured_nlu_provider_failed"] is True


def test_understanding_rejects_a_schema_violation_rather_than_coercing_it():
    with _Outage((
        '{"intents":["menu_request"],"entities":{},"confidence":0.9,'
        '"requires_human":false,"knowledge_gap":"false"}', True, "groq_rag",
    )):
        result = understanding_engine.analyze("show me the menu")

    assert result.available is False
    assert result.reason == "invalid_schema"
