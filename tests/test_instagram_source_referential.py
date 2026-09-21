"""Natural-language follow-ups to configured Instagram source context."""

import pytest

import camellia.app as app_module
import camellia.responder as responder_module
from camellia.config import config
from camellia.responder import responder, source_referential_intent, split_conversational_opener
from camellia.session_memory import session_memory


def _set_source(uid, *, source_type="reel", active=True):
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    facts["source_route"] = {
        "routing_id": 41,
        "source_type": source_type,
        "routing_topic": "Wedding / Event Sondesh",
        "priority_products": ["jolbhora"],
        "active": active,
    }
    facts["source_match_method"] = "shared_media_url"
    return facts


@pytest.fixture
def deterministic_responder(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "GROQ_ENABLED", False)
    monkeypatch.setattr(config, "STRUCTURED_NLU_ENABLED", False)
    monkeypatch.setattr(responder_module.db, "search_learned_knowledge", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(responder_module.db, "get_learned_knowledge", lambda *_args, **_kwargs: [])


def _reply(uid, text):
    return responder.match_and_respond(text, user_id=uid, channel="instagram")


@pytest.mark.parametrize("text", [
    "Tell me more",
    "Tell me more about this",
    "Tell me about the above",
    "I want this",
    "This one please",
    "I'm interested in this",
    "How much is this?",
    "Price for this",
    "Is this available?",
    "What flavours are in this?",
    "How do I order this?",
])
def test_natural_referential_followups_use_a_shared_reel(deterministic_responder, text):
    uid = "referential_reel_" + text[:8].replace(" ", "_")
    facts = _set_source(uid)
    reply, matched, template_id, _images = _reply(uid, text)

    assert matched is True
    assert template_id.startswith("source_")
    assert reply
    assert facts["source_context_used"] is True


@pytest.mark.parametrize("text", ["Tell me about this", "I want this"])
def test_referential_followups_use_a_shared_post(deterministic_responder, text):
    uid = "referential_post_" + text[:4]
    facts = _set_source(uid, source_type="post")
    _reply(uid, text)
    assert facts["source_context_used"] is True


@pytest.mark.parametrize("text", ["More details please", "How much is this?"])
def test_referential_followups_use_a_configured_ad(deterministic_responder, text):
    uid = "referential_ad_" + text[:4]
    facts = _set_source(uid, source_type="ad")
    _reply(uid, text)
    assert facts["source_context_used"] is True


def test_explicit_product_intent_overrides_source_context(deterministic_responder):
    facts = _set_source("referential_override_kanchagolla")
    reply, matched, template_id, _images = _reply("referential_override_kanchagolla", "What about Kanchagolla?")
    assert matched is True
    assert not template_id.startswith("source_")
    assert "kanchagolla" in reply.lower()
    assert facts["source_context_used"] is False

    facts = _set_source("referential_override_jolbhora")
    reply, matched, template_id, _images = _reply("referential_override_jolbhora", "How much is Jolbhora?")
    assert matched is True
    assert not template_id.startswith("source_")
    assert "jolbhora" in reply.lower()
    assert facts["source_context_used"] is False


def test_explicit_topic_intent_is_not_treated_as_a_source_followup(deterministic_responder):
    facts = _set_source("referential_topic_override")
    reply, _matched, template_id, _images = _reply(
        "referential_topic_override", "What about Bengali meals?",
    )
    assert not template_id.startswith("source_")
    assert "meal" in reply.lower() or "menu" in reply.lower()
    assert facts["source_context_used"] is False

    _reply("referential_topic_override", "This one please")
    assert facts["source_context_used"] is False


@pytest.mark.parametrize("text, expected_template", [
    ("tell me more about this", "bengali_home_table_details"),
    ("Whats the price?", "bengali_home_table_pricing"),
])
def test_mapped_home_table_story_uses_campaign_answers_for_ambiguous_followups(
    deterministic_responder, text, expected_template,
):
    uid = "referential_home_table_story_" + expected_template
    facts = _set_source(uid, source_type="story")
    facts["source_route"].update({
        "routing_topic": "Bengali Home Table",
        "priority_products": [],
    })

    reply, matched, template_id, _images = _reply(uid, text)

    assert matched is True
    assert template_id == expected_template
    assert facts["source_context_used"] is True
    if expected_template == "bengali_home_table_pricing":
        assert "₹1,500 per person" in reply
        assert "₹2,500 per person" in reply

def test_purchase_help_overrides_source_only_route(deterministic_responder):
    """An explicit purchase-help request must not be answered by Story copy."""
    uid = "referential_purchase_help"
    facts = _set_source(uid)
    facts["source_route"]["menu_mode"] = "NONE"

    reply, matched, template_id, _images = _reply(
        uid, "I am ready to place my order and need help booking.",
    )

    assert matched is False
    assert template_id == "order_next_steps"
    assert reply
    assert facts["source_context_used"] is False


@pytest.mark.parametrize("text", ["I want this", "How much is this?"])
def test_referential_message_without_source_clarifies(deterministic_responder, text):
    uid = "referential_no_source_" + text[:4]
    session_memory.clear_session(uid, "instagram")
    reply, matched, template_id, _images = _reply(uid, text)
    assert matched is True
    assert template_id == "clarify_request"
    assert "which item are you interested in" in reply.lower()


def test_inactive_source_context_does_not_control_a_new_message(deterministic_responder):
    uid = "referential_stale"
    _set_source(uid, active=False)
    reply, matched, template_id, _images = _reply(uid, "Tell me more")
    assert matched is True
    assert template_id == "clarify_request"
    assert "which item" in reply.lower()


@pytest.mark.parametrize("text, expected", [
    ("tell me mor!", "details"),
    ("DETIALS, please", "details"),
    ("availble?", "availability"),
    ("thsi one", "order"),
    ("PRICE for this", "price"),
])
def test_referential_detector_normalizes_typos_and_punctuation(text, expected):
    assert source_referential_intent(text) == expected


def test_source_audit_trace_preserves_routing_evidence_and_intent(deterministic_responder, monkeypatch):
    uid = "referential_audit"
    facts = _set_source(uid)
    logged = []
    sent = []
    monkeypatch.setattr(app_module.db, "log_conversation", lambda *args, **kwargs: logged.append(kwargs) or 901)
    monkeypatch.setattr(app_module, "send_instagram_reply", lambda *args, **kwargs: sent.append((args, kwargs)))

    app_module._process_instagram_event(uid, "Tell me about this", None)

    assert sent
    trace = logged[0]
    assert trace["source_context_used"] is True
    assert trace["source_routing_id"] == 41
    assert trace["source_match_method"] == "shared_media_url"
    assert facts["source_referential_intent"] == "details"


def _assert_plain_conversational_reply(reply, template_id, facts):
    lowered = reply.lower()
    assert template_id in {"greeting", "conversation_closing", "acknowledgement"}
    assert "₹" not in reply
    assert "jolbhora" not in lowered
    assert "signature collection" not in lowered
    assert "|" not in reply  # no menu table
    assert facts["source_context_used"] is False
    assert facts["source_referential_intent"] is None


def test_fresh_hi_uses_normal_greeting_not_source_context(deterministic_responder):
    uid = "greeting_fresh"
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    reply, matched, template_id, _images = _reply(uid, "hi")
    assert matched is True
    _assert_plain_conversational_reply(reply, template_id, facts)


def test_reel_then_hi_bypasses_source_but_preserves_it_for_followup(deterministic_responder):
    uid = "greeting_after_reel"
    facts = _set_source(uid)
    reply, matched, template_id, _images = _reply(uid, "hi")
    assert matched is True
    _assert_plain_conversational_reply(reply, template_id, facts)
    assert facts["source_route"]["routing_id"] == 41

    followup, followup_matched, followup_template, _images = _reply(uid, "Tell me more")
    assert followup_matched is True
    assert followup_template.startswith("source_")
    assert followup
    assert facts["source_context_used"] is True


def test_greeting_trace_records_source_available_but_not_used(deterministic_responder, monkeypatch):
    uid = "greeting_trace"
    facts = _set_source(uid)
    logged = []
    sent = []
    monkeypatch.setattr(app_module.db, "log_conversation", lambda *args, **kwargs: logged.append(kwargs) or 902)
    monkeypatch.setattr(app_module, "send_instagram_reply", lambda *args, **kwargs: sent.append((args, kwargs)))

    app_module._process_instagram_event(uid, "hi", None)

    assert sent
    trace = logged[0]
    assert trace["source_routing_id"] == 41
    assert trace["source_context_used"] is False
    assert facts["source_referential_intent"] is None
    assert facts["greeting_intent"] is True


@pytest.mark.parametrize("text", ["hello", "hey", "good morning", "good evening", "thanks", "okay"])
def test_conversational_turns_never_dump_active_source_menu(deterministic_responder, text):
    uid = "conversation_only_" + text.replace(" ", "_")
    facts = _set_source(uid)
    reply, matched, template_id, _images = _reply(uid, text)
    assert matched is True
    _assert_plain_conversational_reply(reply, template_id, facts)


@pytest.mark.parametrize("text, expected_opener, expected_remainder", [
    ("hi tell me more", "hi", "tell me more"),
    ("hello what is Jolbhora", "hello", "what is jolbhora"),
    ("hey how much is Sondesh", "hey", "how much is sondesh"),
    ("hi there, tell me more", "hi", "tell me more"),
])
def test_composite_greeting_is_split_before_business_resolution(
    deterministic_responder, text, expected_opener, expected_remainder,
):
    opener, remainder = split_conversational_opener(text)
    assert opener == expected_opener
    assert remainder.lower() == expected_remainder


def test_greeting_plus_referential_without_context_clarifies(deterministic_responder):
    uid = "composite_no_context"
    session_memory.clear_session(uid, "instagram")
    reply, matched, template_id, _images = _reply(uid, "hi tell me more")
    facts = session_memory.get_facts(uid, "instagram")
    assert matched is True
    assert template_id == "clarify_request"
    assert "which item" in reply.lower()
    assert "bengali home table" not in reply.lower()
    assert facts["conversational_opener"] == "hi"
    assert facts["semantic_remainder"] == "tell me more"
    assert facts["source_context_used"] is False


def test_greeting_plus_referential_uses_fresh_reel_context(deterministic_responder):
    uid = "composite_reel_context"
    facts = _set_source(uid)
    reply, matched, template_id, _images = _reply(uid, "hi tell me more")
    assert matched is True
    assert template_id.startswith("source_")
    assert reply
    assert facts["source_context_used"] is True
    assert facts["source_referential_intent"] == "details"


def test_composite_explicit_product_and_topic_precedence(deterministic_responder):
    uid = "composite_explicit_product"
    facts = _set_source(uid)
    reply, matched, template_id, _images = _reply(
        uid, "hi tell me more about Kanchagolla",
    )
    assert matched is True
    assert "kanchagolla" in reply.lower()
    assert not template_id.startswith("source_")
    assert facts["source_context_used"] is False

    uid = "composite_home_table"
    session_memory.clear_session(uid, "instagram")
    reply, matched, template_id, _images = _reply(
        uid, "What is the Kolkata Home Table event?",
    )
    assert matched is True
    assert template_id == "bengali_home_table_details"
    assert "kolkata" in reply.lower()


def test_composite_greeting_keeps_menu_and_handoff_intents(deterministic_responder):
    uid = "composite_menu"
    session_memory.clear_session(uid, "instagram")
    reply, matched, template_id, _images = _reply(uid, "hey show me this week's menu")
    assert matched is True
    assert template_id != "clarify_request"
    assert "weekly menu" in reply.lower() or "menu" in reply.lower()

    uid = "composite_handoff"
    session_memory.clear_session(uid, "instagram")
    reply, matched, template_id, _images = _reply(uid, "hi I want to talk to a human")
    assert template_id == "explicit_escalation"
    assert "whatsapp" in reply.lower()


@pytest.mark.parametrize("text, expected", [
    ("hi tell me more about Kanchagolla", "kanchagolla"),
    ("tell me more about Kanchagolla", "kanchagolla"),
    ("tell me more about Jolbhora", "jolbhora"),
        ("tell me more about Sondesh", "sondesh_tell_me_more"),
])
def test_named_subject_precedes_generic_detail_nlu(
    deterministic_responder, monkeypatch, text, expected,
):
    """A provider's generic know-more result cannot hide an explicit entity."""
    monkeypatch.setattr(config, "STRUCTURED_NLU_ENABLED", True)
    uid = "detail_entity_precedence_" + expected
    facts = _set_source(uid)
    facts["source_route"]["routing_topic"] = "Bengali Home Table"

    def stale_generic(*_args, **_kwargs):
        return "stale Home Table response", True, "know_more", None

    monkeypatch.setattr(responder, "_structured_nlu_response", stale_generic)
    reply, matched, template_id, _images = _reply(uid, text)

    assert matched is True
    assert ("sondesh" if expected.startswith("sondesh_") else expected) in reply.lower()
    assert template_id == expected
    assert facts["source_context_used"] is False


def test_explicit_weekly_menu_precedes_generic_detail_nlu(deterministic_responder, monkeypatch):
    monkeypatch.setattr(config, "STRUCTURED_NLU_ENABLED", True)
    uid = "detail_weekly_menu_precedence"
    session_memory.clear_session(uid, "instagram")
    monkeypatch.setattr(
        responder, "_structured_nlu_response",
        lambda *_args, **_kwargs: ("stale Home Table response", True, "know_more", None),
    )
    reply, matched, template_id, _images = _reply(uid, "can you tell me more about the weekly menu")
    assert matched is True
    assert template_id in {"weekly_menu_veg", "weekly_menu_nonveg", "savoury_food_intro"}
    assert "menu" in reply.lower()


def test_explicit_home_table_precedes_generic_detail_nlu(deterministic_responder, monkeypatch):
    monkeypatch.setattr(config, "STRUCTURED_NLU_ENABLED", True)
    uid = "detail_home_table_precedence"
    facts = _set_source(uid)
    facts["source_route"]["routing_topic"] = "Wedding / Event Sondesh"
    monkeypatch.setattr(
        responder, "_structured_nlu_response",
        lambda *_args, **_kwargs: ("stale Home Table response", True, "know_more", None),
    )
    reply, matched, template_id, _images = _reply(
        uid, "can you tell me more about the Kolkata Home Table event",
    )
    assert matched is True
    assert template_id == "bengali_home_table_details"
    assert "kolkata" in reply.lower()
    assert facts["source_context_used"] is False


def test_home_table_campaign_remains_available_even_with_stale_source(deterministic_responder):
    uid = "retired_home_table_guard"
    facts = _set_source(uid)
    facts["source_route"]["routing_topic"] = "Bengali Home Table"
    reply, matched, template_id, _images = _reply(uid, "Tell me about Bengali Home Table")
    assert matched is True
    assert template_id == "bengali_home_table_details"
    assert "bengali home table" in reply.lower()
    assert facts["source_context_used"] is False
