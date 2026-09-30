"""Production-equivalent Phase 3B content-integrity remediation tests."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from camellia import app as app_module
from camellia import handoff as handoff_module
from camellia import responder as responder_module
from camellia.config import config
from camellia.content_integrity import (
    CLIENT_CONFIRMED_EXACT_RESPONSES,
    HOME_TABLE_REMEDIATION_ACTOR,
    HOME_TABLE_REMEDIATION_REASON,
    HOME_TABLE_TEMPLATE_IDS,
    PHASE3B_REMEDIATION_ACTOR,
    PHASE3B_REMEDIATION_REASON,
    QUARANTINE_TEMPLATE_FALLBACKS,
    apply_safe_content_remediations,
    is_client_confirmed_exact_response,
    quarantine_template_response,
    runtime_quarantine_enabled,
)
from camellia.database import Database
from camellia.responder import DeterministicResponder
from camellia.session_memory import session_memory


PHASE3B_CASES = [
    ("Do you also cook meals?", "sondesh_crosssell_savoury"),
    ("do you deliver to Bangalore", "delivery_out_of_area"),
    ("when should I book?", "savoury_best_time_to_book"),
    ("how long will delivery take?", "savoury_delivery_time"),
    ("who runs the kitchen now that you're moving to kolkata", "kolkata_expansion_info"),
    ("who can i speak to directly", "GUARD_staff_names_manager_contact"),
]


def _fixtures(tmp_path):
    menu = json.loads(Path("data/menu.json").read_text(encoding="utf-8"))
    templates = json.loads(Path("data/qa_templates.json").read_text(encoding="utf-8"))["templates"]
    return Database(str(tmp_path / "phase3b-integrity.db")), menu, templates


def _run_instagram_worker(monkeypatch, database, message, user_id):
    """Exercise the production Instagram worker path with outbound capture."""
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(responder_module, "db", database)
    monkeypatch.setattr(handoff_module, "db", database)
    session_memory.clear_session(user_id, "instagram")
    sent = []
    with patch.object(
        app_module,
        "send_instagram_reply",
        side_effect=lambda recipient, text, image_url=None, **kwargs: sent.append(
            {"recipient": recipient, "text": text, "image_url": image_url, **kwargs}
        ),
    ), patch.object(
        app_module.escalator, "handle_escalation", return_value={"escalation_id": 1}
    ) as escalated, patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app_module._process_instagram_event(user_id, message, "phase3b-output-ad")
    return sent, escalated


@pytest.mark.parametrize("message, template_id", PHASE3B_CASES)
def test_exact_phase3b_copy_survives_active_runtime_quarantine(tmp_path, monkeypatch, message, template_id):
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    database, _menu, _templates = _fixtures(tmp_path)
    responder = DeterministicResponder()

    monkeypatch.setattr("camellia.responder.db", database)
    assert runtime_quarantine_enabled(database.db_path) is True

    user_id = f"phase3b-runtime-{template_id}"
    test_channel = "instagram" if template_id == "sondesh_crosssell_savoury" else "whatsapp"
    session_memory.clear_session(user_id, test_channel)
    if template_id == "sondesh_crosssell_savoury":
        responder.match_and_respond(
            "What is the sondesh price?", user_id=user_id, channel=test_channel
        )
    text, matched, actual_id, _images = responder.match_and_respond(
        message, user_id=user_id, channel=test_channel
    )

    if template_id == "sondesh_crosssell_savoury":
        # The standalone cross-sell exists only in the retired template path;
        # it is not a canonical client-QA question. Instagram must hand off.
        assert matched is False
        assert actual_id is None
        assert "manager of kitchens" in text.lower()
        assert _images is None
        return

    assert matched is True
    assert actual_id == template_id
    assert text == CLIENT_CONFIRMED_EXACT_RESPONSES[template_id]


def test_exact_allowlist_does_not_bypass_modified_unsafe_copy():
    approved = CLIENT_CONFIRMED_EXACT_RESPONSES["sondesh_crosssell_savoury"]
    modified = approved + " It is made by trusted dairies."
    assert is_client_confirmed_exact_response("sondesh_crosssell_savoury", modified) is False
    safe = quarantine_template_response("sondesh_crosssell_savoury", modified)
    assert safe == approved
    assert "trusted dairies" not in safe


def test_legacy_phase3b_overrides_upgrade_once_and_preserve_history(tmp_path):
    database, menu, templates = _fixtures(tmp_path)
    affected = tuple(
        template_id for template_id in CLIENT_CONFIRMED_EXACT_RESPONSES
        if template_id in QUARANTINE_TEMPLATE_FALLBACKS
    )
    for template_id in affected:
        database.save_response_template_override(
            template_id, QUARANTINE_TEMPLATE_FALLBACKS[template_id], "legacy_integrity_audit"
        )

    first = apply_safe_content_remediations(database, menu, templates)
    assert all(first[template_id] == "updated" for template_id in affected)
    for template_id in affected:
        current = database.get_response_template_override(template_id)
        assert current["response"] == CLIENT_CONFIRMED_EXACT_RESPONSES[template_id]
        versions = database.get_response_template_override_versions(template_id)
        assert versions[0]["response"] == QUARANTINE_TEMPLATE_FALLBACKS[template_id]
        if template_id in HOME_TABLE_TEMPLATE_IDS:
            assert versions[0]["changed_by"] == HOME_TABLE_REMEDIATION_ACTOR
            assert versions[0]["change_reason"] == HOME_TABLE_REMEDIATION_REASON
        else:
            assert versions[0]["changed_by"] == PHASE3B_REMEDIATION_ACTOR
            assert versions[0]["change_reason"] == PHASE3B_REMEDIATION_REASON

    version_counts = {
        template_id: len(database.get_response_template_override_versions(template_id))
        for template_id in affected
    }
    second = apply_safe_content_remediations(database, menu, templates)
    assert all(second[template_id] == "unchanged" for template_id in affected)
    assert {
        template_id: len(database.get_response_template_override_versions(template_id))
        for template_id in affected
    } == version_counts


def test_home_table_stale_handoff_overrides_upgrade_idempotently(tmp_path):
    database, menu, templates = _fixtures(tmp_path)
    stale = "Our team can assist you personally on WhatsApp. Please continue your enquiry using the link below."
    for template_id in HOME_TABLE_TEMPLATE_IDS:
        database.save_response_template_override(
            template_id, stale, "legacy_instagram_handoff", change_reason="legacy_seed"
        )

    first = apply_safe_content_remediations(database, menu, templates)
    assert {first[template_id] for template_id in HOME_TABLE_TEMPLATE_IDS} == {"updated"}
    for template_id in HOME_TABLE_TEMPLATE_IDS:
        current = database.get_response_template_override(template_id)
        assert current["response"] == CLIENT_CONFIRMED_EXACT_RESPONSES[template_id]
        versions = database.get_response_template_override_versions(template_id)
        assert versions[0]["response"] == stale
        assert versions[0]["changed_by"] == HOME_TABLE_REMEDIATION_ACTOR
        assert versions[0]["change_reason"] == HOME_TABLE_REMEDIATION_REASON

    version_counts = {
        template_id: len(database.get_response_template_override_versions(template_id))
        for template_id in HOME_TABLE_TEMPLATE_IDS
    }
    second = apply_safe_content_remediations(database, menu, templates)
    assert {second[template_id] for template_id in HOME_TABLE_TEMPLATE_IDS} == {"unchanged"}
    assert {
        template_id: len(database.get_response_template_override_versions(template_id))
        for template_id in HOME_TABLE_TEMPLATE_IDS
    } == version_counts


@pytest.mark.parametrize("template_id", sorted(HOME_TABLE_TEMPLATE_IDS))
def test_home_table_operator_override_is_preserved(tmp_path, template_id):
    database, menu, templates = _fixtures(tmp_path)
    custom = f"CUSTOM OPERATOR {template_id} COPY"
    database.save_response_template_override(template_id, custom, "client_operator")

    actions = apply_safe_content_remediations(database, menu, templates)

    assert actions[template_id] == "skipped"
    assert database.get_response_template_override(template_id)["response"] == custom


@pytest.mark.parametrize(
    ("message", "template_id"),
    [
        ("Tell me about the Bengali Home Table", "bengali_home_table_details"),
        ("How much per person for the Bengali home lunch?", "bengali_home_table_pricing"),
        ("How do I pay for the Home Table?", "bengali_home_table_payment"),
    ],
)
def test_home_table_final_output_uses_exact_copy_after_stale_override(
    tmp_path, monkeypatch, message, template_id
):
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    database, _menu, _templates = _fixtures(tmp_path)
    stale = "Our team can assist you personally on WhatsApp. Please continue your enquiry using the link below."
    database.save_response_template_override(template_id, stale, "legacy_instagram_handoff")
    user_id = f"home-table-final-output-{template_id}"
    sent, escalated = _run_instagram_worker(monkeypatch, database, message, user_id)

    assert not escalated.called
    from camellia.client_qa import REGISTRY_BY_ID, HOME_TABLE_REEL_URLS, HOME_TABLE_VIDEO_ANCHOR
    source_id = {
        "bengali_home_table_details": "home_table_details",
        "bengali_home_table_pricing": "home_table_price",
        "bengali_home_table_payment": "home_table_payment",
    }[template_id]
    if source_id == "home_table_payment":
        assert len(sent) == 1
        assert sent[0]["text"] == REGISTRY_BY_ID[source_id].answer
    else:
        assert len(sent) == 6
        assert tuple(item["text"] for item in sent[1:5]) == HOME_TABLE_REEL_URLS
        assert sent[0]["text"].endswith(HOME_TABLE_VIDEO_ANCHOR)
        assert sent[0]["text"] + sent[5]["text"] == REGISTRY_BY_ID[source_id].answer
    assert all(stale not in item["text"] and "wa.me" not in item["text"] for item in sent)
    assert all(item["image_url"] is None for item in sent)


def test_home_table_booking_remains_handoff_after_copy_remediation(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    database, _menu, _templates = _fixtures(tmp_path)
    user_id = "home-table-booking-handoff"
    sent, escalated = _run_instagram_worker(
        monkeypatch, database, "I want to book the Home Table", user_id
    )

    assert sent
    assert escalated.called
    assert any("wa.me" in payload["text"] for payload in sent)


def test_distinct_operator_override_is_preserved_by_phase3b_migration(tmp_path):
    database, menu, templates = _fixtures(tmp_path)
    template_id = "sondesh_crosssell_savoury"
    custom = "Client-approved temporary wording."
    database.save_response_template_override(template_id, custom, "client_operator")

    actions = apply_safe_content_remediations(database, menu, templates)

    assert actions[template_id] == "skipped"
    assert database.get_response_template_override(template_id)["response"] == custom


def test_confirmed_phase3b_route_is_independent_of_ai_provider_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    database, _menu, _templates = _fixtures(tmp_path)
    responder = DeterministicResponder()
    monkeypatch.setattr("camellia.responder.db", database)
    monkeypatch.setattr(config, "GROQ_ENABLED", True)
    monkeypatch.setattr(config, "GROQ_API_KEY", "test-key")
    ai = __import__("camellia.responder", fromlist=["ai_responder"]).ai_responder
    monkeypatch.setattr(ai, "get_active_provider", lambda: "groq")

    def fail_refinement(*_args, **_kwargs):
        raise RuntimeError("simulated Groq outage")

    monkeypatch.setattr(ai, "refine_template_response", fail_refinement)
    user_id = "phase3b-ai-outage"
    session_memory.clear_session(user_id, "instagram")
    responder.match_and_respond(
        "What is the sondesh price?", user_id=user_id, channel="instagram"
    )
    text, matched, template_id, _images = responder.match_and_respond(
        "Do you also cook meals?", user_id=user_id, channel="instagram"
    )

    assert matched is False
    assert template_id is None
    assert "manager of kitchens" in text.lower()
    assert session_memory.get_facts(user_id, "instagram")["last_llm_called"] is False
