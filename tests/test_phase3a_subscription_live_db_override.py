"""Exact-fingerprint remediation for the live subscription override."""

import json

from camellia import bootstrap
from camellia.content_integrity import (
    KNOWN_LEGACY_SUBSCRIPTION_SHA256,
    KNOWN_LEGACY_SUBSCRIPTION_SHA256S,
    KNOWN_LIVE_STALE_SUBSCRIPTION_SHA256,
    LEGACY_SUBSCRIPTION_RESPONSE,
    SUBSCRIPTION_LIVE_STALE_REMEDIATION_REASON,
    SUBSCRIPTION_REMEDIATION_ACTOR,
    apply_safe_content_remediations,
    is_known_legacy_subscription_response,
    subscription_response_fingerprint,
)
from camellia.database import Database
from camellia.responder import DeterministicResponder


SUBSCRIPTION_ID = "subscription_details"


def _templates():
    return json.loads((bootstrap.DATA_DIR / "qa_templates.json").read_text(encoding="utf-8"))["templates"]


def _subscription_template():
    return next(item for item in _templates() if item["id"] == SUBSCRIPTION_ID)


def test_live_production_fingerprint_is_an_exact_allowlist_member():
    assert KNOWN_LEGACY_SUBSCRIPTION_SHA256 in KNOWN_LEGACY_SUBSCRIPTION_SHA256S
    assert KNOWN_LIVE_STALE_SUBSCRIPTION_SHA256 in KNOWN_LEGACY_SUBSCRIPTION_SHA256S
    assert subscription_response_fingerprint(LEGACY_SUBSCRIPTION_RESPONSE) == KNOWN_LEGACY_SUBSCRIPTION_SHA256
    assert is_known_legacy_subscription_response(LEGACY_SUBSCRIPTION_RESPONSE)
    assert not is_known_legacy_subscription_response(
        LEGACY_SUBSCRIPTION_RESPONSE + "\nOperator note"
    )


def test_live_stale_fingerprint_uses_versioned_upgrade_reason(monkeypatch, tmp_path):
    menu = json.loads((bootstrap.DATA_DIR / "menu.json").read_text(encoding="utf-8"))
    database = Database(str(tmp_path / "live-stale.db"))
    live_stale_copy = "EXACT PRODUCTION STALE OVERRIDE FIXTURE"
    original_fingerprint = subscription_response_fingerprint

    # The production copy is intentionally not reproduced in test output.
    # Its read-only normalized SHA is the immutable allowlist boundary.
    monkeypatch.setattr(
        "camellia.content_integrity.subscription_response_fingerprint",
        lambda text: (
            KNOWN_LIVE_STALE_SUBSCRIPTION_SHA256
            if text == live_stale_copy
            else original_fingerprint(text)
        ),
    )
    database.save_response_template_override(SUBSCRIPTION_ID, live_stale_copy, "legacy-import")

    actions = apply_safe_content_remediations(database, menu, _templates())

    assert actions[SUBSCRIPTION_ID] == "updated"
    current = database.get_response_template_override(SUBSCRIPTION_ID)
    assert current["updated_by"] == SUBSCRIPTION_REMEDIATION_ACTOR
    versions = database.get_response_template_override_versions(SUBSCRIPTION_ID)
    assert versions[0]["change_reason"] == SUBSCRIPTION_LIVE_STALE_REMEDIATION_REASON
    assert versions[0]["response"] == live_stale_copy


def test_known_legacy_runtime_bypasses_override_until_startup_reconciliation(monkeypatch, tmp_path):
    database = Database(str(tmp_path / "runtime-bypass.db"))
    database.save_response_template_override(SUBSCRIPTION_ID, LEGACY_SUBSCRIPTION_RESPONSE, "legacy-import")
    monkeypatch.setattr("camellia.responder.db", database)
    responder = DeterministicResponder()

    text, images = responder.render_approved_template(SUBSCRIPTION_ID)

    assert text == _subscription_template()["response"]
    assert images == ["/images/Everyday%20Meal%20Subscription%20Rate%20Chart.jpeg"]
    assert "₹1,500" not in text


def test_distinct_operator_override_is_not_bypassed(monkeypatch, tmp_path):
    database = Database(str(tmp_path / "operator-copy.db"))
    custom = "CLIENT APPROVED SUBSCRIPTION COPY"
    database.save_response_template_override(SUBSCRIPTION_ID, custom, "operator")
    monkeypatch.setattr("camellia.responder.db", database)
    responder = DeterministicResponder()

    assert responder._template_by_id(SUBSCRIPTION_ID)["response"] == custom
