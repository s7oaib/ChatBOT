"""Natural-language follow-ups to configured Instagram source context."""

import pytest

import camellia.app as app_module
import camellia.responder as responder_module
from camellia.config import config
from camellia.responder import responder, source_referential_intent
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
