"""Golden coverage for the canonical QA ambiguity resolver."""

from dataclasses import replace

import pytest

from camellia import client_qa
from camellia.config import config
from camellia.handoff import handoff_contact_mode
from camellia.responder import responder
from camellia.session_memory import session_memory


def test_registry_has_stable_canonical_records_and_auditable_conflict_status():
    assert len(client_qa.REGISTRY) == 23
    assert client_qa.REGISTRY_BY_CANONICAL_ID["HOME_TABLE_PRICE"].status == "ACTIVE"
    assert client_qa.REGISTRY_BY_CANONICAL_ID["GENERAL_MENU_PRICE"].status == "ACTIVE"
    assert client_qa.REGISTRY_BY_CANONICAL_ID["PREBOOK_LEGACY"].status == "SUPERSEDED"
    assert client_qa.REGISTRY_BY_CANONICAL_ID["PREBOOK_CURRENT"].status == "ACTIVE"
    assert client_qa.REGISTRY_BY_CANONICAL_ID["PREBOOK_CURRENT"].answer.find("invoice") >= 0
    assert client_qa.REGISTRY_BY_CANONICAL_ID["PREBOOK_LEGACY"].answer != client_qa.REGISTRY_BY_CANONICAL_ID["PREBOOK_CURRENT"].answer


@pytest.mark.parametrize(
    ("message", "facts", "canonical_id"),
    [
        ("tell me more about this", {"home_table_context": True}, "HOME_TABLE_DETAILS"),
        ("Can you tell me more about this", {}, "GENERAL_CAMELLIA_DETAILS"),
        ("Whats the price?", {"home_table_context": True}, "HOME_TABLE_PRICE"),
        ("whats the price", {"general_context": True}, "GENERAL_MENU_PRICE"),
        ("What is the price of Sondesh?", {"home_table_context": True}, "SONDESH_PRICE"),
        ("What is the price of Bengali Bento?", {"home_table_context": True}, "BENTO_PRICE"),
        ("How to pay", {"home_table_context": True}, "HOME_TABLE_PAYMENT"),
        ("How do I pay?", {"general_context": True}, "GENERAL_PAYMENT"),
        ("How long will Sondesh delivery take?", {}, "SONDESH_DELIVERY_TIME"),
        ("What does Bengali Bento cost?", {}, "BENTO_PRICE"),
    ],
)
def test_explicit_entity_and_context_precedence(message, facts, canonical_id):
    result = client_qa.match(message, facts)
    assert result and result.entry
    assert result.entry.canonical_id == canonical_id


def test_unscoped_punctuated_price_clarifies_instead_of_guessing():
    result = client_qa.match("What's the price?", {})
    assert result and result.entry is None
    assert result.match_kind == "clarification"
    assert "Bengali Home Table" in result.clarification


def test_subscription_to_prebook_uses_latest_current_variant():
    result = client_qa.match("WHAT ARE THE NEXT STEPS TO PRE BOOK?", {"subscription_context": True})
    assert result and result.entry
    assert result.entry.canonical_id == "PREBOOK_CURRENT"
    assert "invoice" in result.entry.answer.lower()


def test_placeholder_lines_are_metadata_not_customer_copy():
    forbidden = (
        "INSERT CURRENT MENU HERE",
        "INSERT 3 PICTURES OF SONDESH",
        "ATTACH RATE CHART",
        "INSERT AAMAR MENU HERE",
        "INSERT SIGNATURE MENU HERE",
        "INSERT LATEST WEEKLY LUNCH/ DINNER MENU HERE",
        "ADD LINK TO WHATSAPP",
        "24 HOURS AFTER SENDING ABOVE, SEND THIS",
    )
    for entry in client_qa.REGISTRY:
        assert not any(marker in entry.answer for marker in forbidden)
    assert "weekly_menu" in client_qa.REGISTRY_BY_CANONICAL_ID["GENERAL_MENU_PRICE"].asset_keys
    assert "subscription_rate_chart" in client_qa.REGISTRY_BY_CANONICAL_ID["SUBSCRIPTION_PRICE"].asset_keys
    assert "SEND_ASSET" in client_qa.REGISTRY_BY_CANONICAL_ID["SONDESH_DETAILS"].actions


def _respond(monkeypatch, message, seed=None, user_id=None):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = user_id or "ambiguity-upgrade-" + str(abs(hash(message)))
    session_memory.clear_session(user_id, "instagram")
    session_memory.get_facts(user_id, "instagram").update(seed or {})
    response = responder.match_and_respond(message, user_id=user_id, channel="instagram")
    return response, session_memory.get_facts(user_id, "instagram")


def test_responder_keeps_explicit_sondesh_after_home_table_and_uses_exact_lane(monkeypatch):
    (text, matched, template_id, _images), facts = _respond(
        monkeypatch,
        "What is the price of Sondesh?",
        {"home_table_context": True},
    )
    assert matched is True
    assert template_id == "sondesh_price_detailed"
    assert facts["client_qa_canonical_id"] == "SONDESH_PRICE"
    assert facts["decision_lane"] == "EXACT_APPROVED"
    assert "INSERT " not in text


def test_explicit_jolbhora_after_home_table_stays_on_its_product_route(monkeypatch):
    (text, matched, template_id, _images), facts = _respond(
        monkeypatch,
        "What is the price of Jolbhora?",
        {"home_table_context": True},
        "ambiguity-jolbhora-switch",
    )
    assert matched is True
    assert template_id == "jolbhora"
    assert facts.get("source_context_used") is not True
    assert "Jolbhora" in text


def test_responder_grounded_comparison_is_not_a_generated_qa_answer(monkeypatch):
    (text, matched, template_id, _images), facts = _respond(monkeypatch, "Which Bento is cheapest?")
    assert matched is True
    assert template_id == "bento_grounded_comparison"
    assert facts["decision_lane"] == "GROUNDED_APPROVED"
    assert "₹350" in text


def test_responder_clarifies_unscoped_price(monkeypatch):
    (text, matched, template_id, _images), facts = _respond(monkeypatch, "What's the price?")
    assert matched is True
    assert template_id == "price_clarify_product"
    assert facts["decision_lane"] == "CLARIFICATION_REQUIRED"
    assert "regular menu" in text


def test_stale_story_unsupported_and_sensitive_questions_hand_off(monkeypatch):
    (event_text, event_matched, event_template, _), event_facts = _respond(
        monkeypatch,
        "Can you cater a wedding in Mumbai?",
        {"home_table_context": True},
        "ambiguity-event-stale",
    )
    (allergy_text, allergy_matched, allergy_template, _), allergy_facts = _respond(
        monkeypatch,
        "Is this safe for someone with a severe milk allergy?",
        {"home_table_context": True},
        "ambiguity-allergy-stale",
    )
    assert event_matched is False and event_facts["knowledge_sufficient"] is False
    assert allergy_matched is False and allergy_facts["knowledge_sufficient"] is False
    assert event_template == "groq_structured_event_escalation"
    assert "wa.me" not in event_text.lower()
    assert "wa.me" not in allergy_text.lower()


def test_generic_support_has_no_whatsapp_link_but_booking_is_marked_manual():
    current = client_qa.REGISTRY_BY_CANONICAL_ID["PREBOOK_CURRENT"]
    assert "MANUAL_WHATSAPP_HANDOFF" in current.actions
    assert "wa.me" not in current.answer.lower()
    assert "MANUAL_WHATSAPP_HANDOFF" not in client_qa.REGISTRY_BY_CANONICAL_ID["GENERAL_CAMELLIA_DETAILS"].actions
    assert handoff_contact_mode("Can you cater my wedding for 100 people?") == "support_callback"
    assert handoff_contact_mode("I want to place an order for a Bengali Bento") == "whatsapp_order"


def test_admin_exposes_canonical_registry_as_read_only(monkeypatch):
    from fastapi.testclient import TestClient
    from camellia.app import app

    monkeypatch.setattr(config, "ADMIN_REQUIRE_HTTPS", False)
    monkeypatch.setattr(config, "ADMIN_API_KEY", "ambiguity-admin-test-key-123456")
    response = TestClient(app).get(
        "/admin/canonical-qa",
        headers={"x-admin-key": config.ADMIN_API_KEY},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["registry"]["canonical_entry_count"] == 23
    item = next(entry for entry in body["entries"] if entry["canonical_id"] == "HOME_TABLE_PRICE")
    assert item["lane"] == "EXACT_APPROVED"
    assert item["editable"] is False
    assert item["generation_allowed"] is False
    assert "context_requirements" in item and "source" in item


def test_asset_missing_is_fail_closed(monkeypatch):
    original = client_qa.match
    source_entry = client_qa.REGISTRY_BY_CANONICAL_ID["SONDESH_DETAILS"]
    missing_entry = replace(source_entry, asset_keys=("missing_asset",))

    def fake_match(message, facts=None):
        return client_qa.ApprovedMatch(missing_entry, "exact", missing_entry.asset_keys)

    monkeypatch.setattr("camellia.responder.client_qa.match", fake_match)
    (text, matched, template_id, images), facts = _respond(monkeypatch, "Tell me more about sondesh", {}, "ambiguity-missing-asset")
    monkeypatch.setattr(client_qa, "match", original)
    assert matched is False
    assert template_id == "client_approved_asset_missing"
    assert images is None
    assert facts["client_qa_asset_status"] == "ASSET-MISSING"
    assert "missing_asset" in facts["client_qa_missing_assets"]
    assert "INSERT" not in text
