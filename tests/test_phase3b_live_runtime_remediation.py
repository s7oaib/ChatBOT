"""Production-equivalent Phase 3B content-integrity remediation tests."""

import json
from pathlib import Path

import pytest

from camellia.config import config
from camellia.content_integrity import (
    CLIENT_CONFIRMED_EXACT_RESPONSES,
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


@pytest.mark.parametrize("message, template_id", PHASE3B_CASES)
def test_exact_phase3b_copy_survives_active_runtime_quarantine(tmp_path, monkeypatch, message, template_id):
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    database, _menu, _templates = _fixtures(tmp_path)
    responder = DeterministicResponder()

    monkeypatch.setattr("camellia.responder.db", database)
    assert runtime_quarantine_enabled(database.db_path) is True

    user_id = f"phase3b-runtime-{template_id}"
    session_memory.clear_session(user_id, "whatsapp")
    if template_id == "sondesh_crosssell_savoury":
        responder.match_and_respond(
            "What is the sondesh price?", user_id=user_id, channel="whatsapp"
        )
    text, matched, actual_id, _images = responder.match_and_respond(
        message, user_id=user_id, channel="whatsapp"
    )

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
    session_memory.clear_session(user_id, "whatsapp")
    responder.match_and_respond(
        "What is the sondesh price?", user_id=user_id, channel="whatsapp"
    )
    text, matched, template_id, _images = responder.match_and_respond(
        "Do you also cook meals?", user_id=user_id, channel="whatsapp"
    )

    assert matched is True
    assert template_id == "sondesh_crosssell_savoury"
    assert text == CLIENT_CONFIRMED_EXACT_RESPONSES[template_id]
