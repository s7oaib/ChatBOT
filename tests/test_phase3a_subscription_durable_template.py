"""Durable subscription-copy reconciliation and runtime contract tests."""

from copy import deepcopy
import json

from camellia import bootstrap
from camellia.content_integrity import (
    SUBSCRIPTION_REMEDIATION_ACTOR,
    SUBSCRIPTION_REMEDIATION_REASON,
    apply_safe_content_remediations,
)
from camellia.database import Database
from camellia.responder import DeterministicResponder


SUBSCRIPTION_ID = "subscription_details"
LEGACY_SUBSCRIPTION_RESPONSE = """Hi! Thank you for asking about our Everyday Meal Subscription.
Price of 5 MEALS (including GST) is attached in the chart below.
Option 1 Vegetarian — ₹1,500 for 5 meals
Option 2 Veg + Chicken — ₹1,750 for 5 meals
Option 3 Chicken — ₹2,000 for 5 meals
Once during the package, you can opt for luchi or polao.
For every 10 meals, we gift you the 11th.
Attached below is our à la carte menu for additional choices."""


def _seed_data():
    return json.loads((bootstrap.DATA_DIR / "qa_templates.json").read_text(encoding="utf-8"))


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _bootstrap_fixture(monkeypatch, tmp_path, templates):
    seed_path = tmp_path / "seed" / "qa_templates.json"
    seed = _seed_data()
    _write(seed_path, seed)
    target = tmp_path / "volume" / "qa_templates.json"
    _write(
        target,
        {
            "seed_version": seed["seed_version"],
            "seeded_from_hash": "old-hash",
            "operator_meta": "keep",
            "templates": templates,
        },
    )
    monkeypatch.setattr(bootstrap, "_SEEDED_FILES", (("QA_TEMPLATES_PATH", seed_path),))
    monkeypatch.setenv("QA_TEMPLATES_PATH", str(target))
    return seed, target


def _subscription(templates):
    return next(item for item in templates if item.get("id") == SUBSCRIPTION_ID)


def test_same_version_replaces_only_known_legacy_subscription_once(monkeypatch, tmp_path):
    seed = _seed_data()
    live_templates = deepcopy(seed["templates"])
    live_subscription = _subscription(live_templates)
    live_subscription["response"] = LEGACY_SUBSCRIPTION_RESPONSE
    before = deepcopy(live_templates)
    _seed, target = _bootstrap_fixture(monkeypatch, tmp_path, live_templates)

    actions = bootstrap.prepare_writable_storage()
    after = json.loads(target.read_text(encoding="utf-8"))

    assert any("replaced QA_TEMPLATES_PATH template: subscription_details" in action for action in actions)
    assert _subscription(after["templates"]) == _subscription(seed["templates"])
    for before_item, after_item in zip(before, after["templates"]):
        if before_item["id"] != SUBSCRIPTION_ID:
            assert before_item == after_item
    assert after["seed_version"] == seed["seed_version"] == 21
    assert after["seeded_from_hash"] == "old-hash"
    assert after["operator_meta"] == "keep"
    backups = list(target.parent.glob("qa_templates.subscription-template-backfill.json"))
    assert len(backups) == 1

    assert bootstrap.prepare_writable_storage() == []
    assert len(list(target.parent.glob("qa_templates.subscription-template-backfill.json"))) == 1


def test_custom_subscription_copy_is_preserved_and_reported(monkeypatch, tmp_path):
    seed = _seed_data()
    live_templates = deepcopy(seed["templates"])
    custom = "CUSTOM OPERATOR SUBSCRIPTION COPY"
    _subscription(live_templates)["response"] = custom
    _seed, target = _bootstrap_fixture(monkeypatch, tmp_path, live_templates)

    actions = bootstrap.prepare_writable_storage()
    after = json.loads(target.read_text(encoding="utf-8"))

    assert any("preserved QA_TEMPLATES_PATH operator override: subscription_details" in action for action in actions)
    assert _subscription(after["templates"])["response"] == custom
    assert not list(target.parent.glob("qa_templates.subscription-template-backfill.json"))


def test_missing_subscription_is_backfilled_from_seed(monkeypatch, tmp_path):
    seed = _seed_data()
    live_templates = [item for item in deepcopy(seed["templates"]) if item.get("id") != SUBSCRIPTION_ID]
    _seed, target = _bootstrap_fixture(monkeypatch, tmp_path, live_templates)

    actions = bootstrap.prepare_writable_storage()
    after = json.loads(target.read_text(encoding="utf-8"))

    assert any("subscription_details" in action for action in actions)
    assert _subscription(after["templates"]) == _subscription(seed["templates"])


def test_response_library_upgrades_known_legacy_subscription_and_is_idempotent(tmp_path):
    menu = json.loads((bootstrap.DATA_DIR / "menu.json").read_text(encoding="utf-8"))
    templates = _seed_data()["templates"]
    database = Database(str(tmp_path / "responses.db"))
    database.save_response_template_override(
        SUBSCRIPTION_ID, LEGACY_SUBSCRIPTION_RESPONSE, "legacy-import"
    )

    first = apply_safe_content_remediations(database, menu, templates)
    canonical = _subscription(templates)["response"]
    current = database.get_response_template_override(SUBSCRIPTION_ID)
    versions_before = database.get_response_template_override_versions(SUBSCRIPTION_ID)

    assert first[SUBSCRIPTION_ID] == "updated"
    assert current["response"] == canonical
    assert current["updated_by"] == SUBSCRIPTION_REMEDIATION_ACTOR
    assert versions_before[0]["changed_by"] == SUBSCRIPTION_REMEDIATION_ACTOR
    assert versions_before[0]["change_reason"] == SUBSCRIPTION_REMEDIATION_REASON
    assert LEGACY_SUBSCRIPTION_RESPONSE == versions_before[0]["response"]

    second = apply_safe_content_remediations(database, menu, templates)
    assert second[SUBSCRIPTION_ID] == "unchanged"
    assert len(database.get_response_template_override_versions(SUBSCRIPTION_ID)) == len(versions_before)


def test_distinct_response_library_subscription_copy_is_preserved(tmp_path):
    menu = json.loads((bootstrap.DATA_DIR / "menu.json").read_text(encoding="utf-8"))
    templates = _seed_data()["templates"]
    database = Database(str(tmp_path / "operator.db"))
    custom = "CLIENT APPROVED SUBSCRIPTION EDIT"
    database.save_response_template_override(SUBSCRIPTION_ID, custom, "operator")

    actions = apply_safe_content_remediations(database, menu, templates)

    assert actions[SUBSCRIPTION_ID] == "skipped_operator_override"
    assert database.get_response_template_override(SUBSCRIPTION_ID)["response"] == custom


def test_subscription_runtime_uses_canonical_copy_and_only_rate_chart(monkeypatch, tmp_path):
    menu = json.loads((bootstrap.DATA_DIR / "menu.json").read_text(encoding="utf-8"))
    templates = _seed_data()["templates"]
    database = Database(str(tmp_path / "runtime.db"))
    database.save_response_template_override(
        SUBSCRIPTION_ID, LEGACY_SUBSCRIPTION_RESPONSE, "legacy-import"
    )
    apply_safe_content_remediations(database, menu, templates)

    monkeypatch.setattr("camellia.responder.db", database)
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    responder = DeterministicResponder()
    responder.menu_data = menu
    text, images = responder.render_approved_template(SUBSCRIPTION_ID)

    assert text == _subscription(templates)["response"]
    assert images == ["/images/Everyday%20Meal%20Subscription%20Rate%20Chart.jpeg"]
    for forbidden in ("₹1,500", "₹1,750", "₹2,000", "luchi", "polao", "11th", "à la carte"):
        assert forbidden.casefold() not in text.casefold()
