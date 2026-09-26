"""Home Table follow-ups use the current approved context, not an old FAQ."""

from unittest.mock import patch

import pytest

from camellia import app as app_module
from camellia import client_qa
from camellia.config import config
from camellia.database import Database
from camellia.responder import responder
from camellia.session_memory import SessionMemoryManager, session_memory


def _reply(user_id, message):
    with patch.object(responder, "_template_by_id", wraps=responder._template_by_id) as lookup:
        text, matched, template_id, _images = responder.match_and_respond(
            message, user_id=user_id, channel="instagram",
        )
    facts = session_memory.get_facts(user_id, "instagram")
    return {
        "text": text,
        "matched": matched,
        "template_id": template_id,
        "decision_lane": facts.get("decision_lane"),
        "route_source": facts.get("last_route_source"),
        "home_table_context": facts.get("home_table_context"),
        "last_topic": facts.get("last_topic"),
        "source_context_superseded": facts.get("source_context_superseded"),
        "client_qa_id": facts.get("client_qa_id"),
        "fallback_executed": facts.get("last_route_source") == "canonical_home_table",
        "legacy_details_lookup": any(
            call.args and call.args[0] == "bengali_home_table_details"
            for call in lookup.call_args_list
        ),
    }


@pytest.mark.parametrize("provider_unavailable", [False, True])
def test_details_then_generic_price_then_payment(monkeypatch, provider_unavailable):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    if provider_unavailable:
        monkeypatch.setattr(config, "GROQ_ENABLED", True)
        monkeypatch.setattr(config, "GROQ_API_KEY", "test-key")
        monkeypatch.setattr("camellia.ai_responder.ai_responder.get_active_provider", lambda: "none")
    user_id = f"home-table-context-provider-{provider_unavailable}"
    session_memory.clear_session(user_id, "instagram")
    details = _reply(user_id, "Tell me about the Bengali Home Table")
    with patch("camellia.ai_responder.ai_responder._query_groq", side_effect=AssertionError("LLM called")):
        pricing = _reply(user_id, "What's the price?")
        payment = _reply(user_id, "How do I pay?")
    assert details["template_id"] == "bengali_home_table_details", details
    assert pricing["template_id"] == "bengali_home_table_pricing", pricing
    assert pricing["text"] == client_qa.REGISTRY_BY_ID["home_table_price"].answer
    assert pricing["route_source"] == "EXACT_APPROVED", pricing
    assert pricing["decision_lane"] == "EXACT_APPROVED", pricing
    assert pricing["home_table_context"] is True, pricing
    assert pricing["last_topic"] == "bengali_home_table", pricing
    assert pricing["fallback_executed"] is False, pricing
    assert pricing["legacy_details_lookup"] is False, pricing
    assert payment["template_id"] == "bengali_home_table_payment", payment
    assert payment["text"] == client_qa.REGISTRY_BY_ID["home_table_payment"].answer


def test_exact_mapped_source_details_then_generic_price(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "home-table-exact-source-context"
    session_memory.clear_session(user_id, "instagram")
    route = {
        "routing_id": 501,
        "source_type": "post",
        "media_id": "exact-home-table-post",
        "active": True,
        "routing_topic": "Bengali Home Table",
        "source_description": "",
    }
    with patch.object(app_module, "route_for_inbound", return_value=(route, "shared_post_media_id")), patch.object(
        app_module, "send_instagram_reply"
    ) as send:
        app_module._process_instagram_event(user_id, "", None, shared_post_id="exact-home-table-post")
    send.assert_not_called()
    details = _reply(user_id, "Tell me more about this")
    pricing = _reply(user_id, "What's the price?")
    assert details["template_id"] == "bengali_home_table_details", details
    assert pricing["template_id"] == "bengali_home_table_pricing", pricing
    assert pricing["route_source"] == "EXACT_APPROVED", pricing
    assert pricing["decision_lane"] == "EXACT_APPROVED", pricing
    assert pricing["home_table_context"] is True, pricing
    assert pricing["last_topic"] == "bengali_home_table", pricing
    assert pricing["fallback_executed"] is False, pricing
    assert pricing["legacy_details_lookup"] is False, pricing


def test_exact_mapped_source_description_still_allows_home_table_price(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "home-table-source-description-price"
    session_memory.clear_session(user_id, "instagram")
    route = {
        "routing_id": 503,
        "source_type": "post",
        "media_id": "exact-home-table-described-post",
        "active": True,
        "routing_topic": "Bengali Home Table",
        "source_description": "Approved source Description",
    }
    with patch.object(app_module, "route_for_inbound", return_value=(route, "shared_post_media_id")), patch.object(
        app_module, "send_instagram_reply"
    ) as send:
        app_module._process_instagram_event(user_id, "", None, shared_post_id="exact-home-table-described-post")
    send.assert_not_called()
    details = _reply(user_id, "Tell me more about this")
    pricing = _reply(user_id, "What's the price?")
    assert details["template_id"] == "source_description", details
    assert pricing["template_id"] == "bengali_home_table_pricing", pricing
    assert pricing["fallback_executed"] is False, pricing


def test_source_route_alone_authorizes_price_but_old_source_can_be_superseded(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "home-table-source-only-price"
    session_memory.clear_session(user_id, "instagram")
    route = {
        "routing_id": 502,
        "source_type": "post",
        "media_id": "exact-home-table-story",
        "active": True,
        "routing_topic": "Bengali Home Table",
    }
    with patch.object(app_module, "route_for_inbound", return_value=(route, "shared_post_media_id")), patch.object(
        app_module, "send_instagram_reply"
    ) as send:
        app_module._process_instagram_event(user_id, "", None, shared_post_id="exact-home-table-story")
    send.assert_not_called()
    pricing = _reply(user_id, "What's the price?")
    assert pricing["template_id"] == "bengali_home_table_pricing", pricing
    assert pricing["fallback_executed"] is False, pricing

    facts = session_memory.get_facts(user_id, "instagram")
    facts.update({"source_context_superseded": True, "last_topic": "sondesh_general", "home_table_context": False})
    later = _reply(user_id, "What's the price?")
    assert later["template_id"] != "bengali_home_table_pricing", later
    assert later["template_id"] != "bengali_home_table_details", later


def test_fresh_generic_price_does_not_enter_home_table(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "home-table-fresh-general-price"
    session_memory.clear_session(user_id, "instagram")
    pricing = _reply(user_id, "What's the price?")
    assert pricing["template_id"] != "bengali_home_table_pricing", pricing
    assert pricing["template_id"] != "bengali_home_table_details", pricing
    assert pricing["client_qa_id"] == "general_price", pricing
    assert pricing["fallback_executed"] is False, pricing


@pytest.mark.parametrize("question", [
    "What's the price of Kanchagolla 250g?",
    "What's the price of Sondesh?",
    "What's the price of Bengali Bento?",
    "What's the subscription price?",
    "Show this week's Savoury menu",
])
def test_explicit_new_subject_overrides_home_table(monkeypatch, question):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "home-table-explicit-" + str(abs(hash(question)))
    session_memory.clear_session(user_id, "instagram")
    assert _reply(user_id, "Tell me about the Bengali Home Table")["template_id"] == "bengali_home_table_details"
    reply = _reply(user_id, question)
    assert reply["template_id"] not in {
        "bengali_home_table_details", "bengali_home_table_pricing", "bengali_home_table_payment",
    }, reply
    if question == "Show this week's Savoury menu":
        assert reply["template_id"] == "asset_request_weekly_menu", reply


def test_sondesh_topic_switch_does_not_resurrect_home_table(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "home-table-then-sondesh-price"
    session_memory.clear_session(user_id, "instagram")
    assert _reply(user_id, "Tell me about the Bengali Home Table")["template_id"] == "bengali_home_table_details"
    assert _reply(user_id, "Tell me about Sondesh")["template_id"] not in {
        "bengali_home_table_details", "bengali_home_table_pricing",
    }
    followup = _reply(user_id, "What's the price?")
    assert followup["template_id"] not in {
        "bengali_home_table_details", "bengali_home_table_pricing",
    }, followup


def test_pricing_remains_one_approved_text_block_and_no_automatic_payment():
    price = client_qa.REGISTRY_BY_ID["home_table_price"]
    assert [(block.kind, block.value) for block in price.blocks] == [("TEXT", price.answer)]
    assert "Pricing of our Bengali home lunch tables, kolkata" in price.answer
    assert "₹1,500" in price.answer and "₹2,500" in price.answer
    assert "9625083171" not in price.answer
    assert "razorpay.me" not in price.answer


def test_unsupported_parking_question_stays_out_of_approved_home_table_answers(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "home-table-parking-unsupported"
    session_memory.clear_session(user_id, "instagram")
    assert _reply(user_id, "Tell me about the Bengali Home Table")["template_id"] == "bengali_home_table_details"
    answer = _reply(user_id, "Is parking free?")
    assert answer["template_id"] not in {
        "bengali_home_table_details", "bengali_home_table_pricing", "bengali_home_table_payment",
    }, answer
    assert "parking is free" not in answer["text"].lower()


def test_persisted_home_table_context_survives_reload_and_expires(tmp_path, monkeypatch):
    import camellia.responder as responder_module

    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    database = Database(str(tmp_path / "home-table-context.db"))
    user_id = "home-table-persisted-context"
    first = SessionMemoryManager(database=database, persistence_enabled=True)
    monkeypatch.setattr(responder_module, "session_memory", first)
    first.clear_session(user_id, "instagram")
    details = responder.match_and_respond(
        "Tell me about the Bengali Home Table", user_id=user_id, channel="instagram",
    )
    assert details[2] == "bengali_home_table_details"

    reloaded = SessionMemoryManager(database=database, persistence_enabled=True)
    monkeypatch.setattr(responder_module, "session_memory", reloaded)
    with patch.object(responder, "_template_by_id", wraps=responder._template_by_id) as lookup:
        pricing = responder.match_and_respond("What's the price?", user_id=user_id, channel="instagram")
    facts = reloaded.get_facts(user_id, "instagram")
    record = {
        "template_id": pricing[2],
        "decision_lane": facts.get("decision_lane"),
        "route_source": facts.get("last_route_source"),
        "home_table_context": facts.get("home_table_context"),
        "last_topic": facts.get("last_topic"),
        "source_context_superseded": facts.get("source_context_superseded"),
        "fallback_executed": facts.get("last_route_source") == "canonical_home_table",
        "legacy_details_lookup": any(
            call.args and call.args[0] == "bengali_home_table_details"
            for call in lookup.call_args_list
        ),
    }
    assert pricing[2] == "bengali_home_table_pricing", record
    assert record["route_source"] == "EXACT_APPROVED", record
    assert record["decision_lane"] == "EXACT_APPROVED", record
    assert record["home_table_context"] is True, record
    assert record["last_topic"] == "bengali_home_table", record
    assert record["fallback_executed"] is False, record
    assert record["legacy_details_lookup"] is False, record

    import time

    after_ttl = time.time() + config.SESSION_TTL_SECONDS + 1
    with patch("camellia.session_memory.time.time", return_value=after_ttl):
        assert not reloaded.get_facts(user_id, "instagram").get("home_table_context")
        expired_price = responder.match_and_respond("What's the price?", user_id=user_id, channel="instagram")
    assert expired_price[2] not in {"bengali_home_table_pricing", "bengali_home_table_details"}
