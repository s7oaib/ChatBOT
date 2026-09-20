"""Canonical opening-hours copy, quarantine, and durable seed regressions."""

from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import camellia.responder as responder_module
from camellia import bootstrap
from camellia.config import config
from camellia.content_integrity import (
    QUARANTINE_TEMPLATE_FALLBACKS,
    contains_unconfirmed_claim,
    quarantine_template_response,
)
from camellia.knowledge import approved_knowledge
from camellia.responder import DeterministicResponder, responder
from camellia.session_memory import session_memory
from camellia.understanding import Intent, UnderstandingResult


OPENING_HOURS = "Our kitchen is open 10:00 AM – 7:30 PM every day."


def _seed_data():
    return json.loads(
        (Path("data") / "qa_templates.json").read_text(encoding="utf-8")
    )


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def test_opening_hours_is_a_safe_canonical_response():
    seed = _seed_data()
    opening_hours = next(item for item in seed["templates"] if item["id"] == "opening_hours")

    assert opening_hours["response"] == OPENING_HOURS
    assert not contains_unconfirmed_claim(OPENING_HOURS)
    assert "15-minute" not in OPENING_HOURS
    assert "Porter" not in OPENING_HOURS
    assert approved_knowledge.knowledge_id_for_template("opening_hours") == (
        "delivery:opening_hours"
    )


def test_content_integrity_preserves_opening_hours_and_quarantines_delivery_time():
    assert quarantine_template_response("opening_hours", OPENING_HOURS) == OPENING_HOURS
    assert "delivery_time" in QUARANTINE_TEMPLATE_FALLBACKS
    assert quarantine_template_response("delivery_time", "stale dispatch copy") == (
        QUARANTINE_TEMPLATE_FALLBACKS["delivery_time"]
    )


def test_production_equivalent_runtime_quarantine_keeps_opening_hours(monkeypatch, tmp_path):
    seed_path = tmp_path / "qa_templates.json"
    _write_json(seed_path, _seed_data())
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    local_responder = DeterministicResponder(
        qa_path=str(seed_path), menu_path=config.MENU_JSON_PATH,
    )
    user_id = "opening-hours-runtime"
    session_memory.clear_session(user_id, "instagram")

    text, matched, template_id, _images = local_responder.match_and_respond(
        "What are your opening hours?", user_id=user_id, channel="instagram",
    )

    assert matched is True
    assert template_id == "opening_hours"
    assert text == OPENING_HOURS


def test_opening_hours_route_does_not_call_nlu_or_groq(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "opening-hours-no-model"
    session_memory.clear_session(user_id, "instagram")

    with patch.object(
        responder_module.understanding_engine, "analyze",
    ) as analyze:
        text, matched, template_id, _images = responder.match_and_respond(
            "What are your opening hours?", user_id=user_id, channel="instagram",
        )

    assert matched is True
    assert template_id == "opening_hours"
    assert text == OPENING_HOURS
    assert not analyze.called


def test_opening_hours_overrides_stale_home_table_story_context(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user_id = "opening-hours-stale-story"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts.update({
        "home_table_context": True,
        "last_topic": "bengali_home_table",
        "source_route": {
            "active": True,
            "routing_topic": "Bengali Home Table",
            "source_type": "story",
        },
    })

    text, matched, template_id, _images = responder.match_and_respond(
        "What are your opening hours?", user_id=user_id, channel="instagram",
    )

    assert matched is True
    assert template_id == "opening_hours"
    assert text == OPENING_HOURS


def _resolve_delivery_time(entities):
    understood = UnderstandingResult(
        intents=[Intent.DELIVERY_TIME_QUESTION.value],
        entities=entities,
        confidence=0.9,
        available=True,
        reason="test",
    )
    return approved_knowledge.resolve(
        understood, responder.qa_data.get("templates", []),
    )


def test_generic_delivery_time_canonical_resolution_cannot_select_opening_hours():
    resolution = _resolve_delivery_time({})

    assert resolution.items
    assert resolution.items[0].template_id != "opening_hours"


def test_opening_hours_subtopic_selects_the_canonical_opening_hours_record():
    resolution = _resolve_delivery_time({"subtopic": "opening_hours"})

    assert [item.template_id for item in resolution.items] == ["opening_hours"]
    assert resolution.items[0].approved_response == OPENING_HOURS


def test_product_specific_delivery_time_records_remain_distinct():
    savoury = _resolve_delivery_time({"product": "bengali bento"})
    sondesh = _resolve_delivery_time({"product": "sondesh"})

    assert savoury.items[0].template_id == "savoury_delivery_time"
    assert sondesh.items[0].template_id == "sondesh_dispatch_time"


def test_equal_seed_version_backfills_opening_hours_without_overwriting_live_edits(
    monkeypatch, tmp_path,
):
    seed = _seed_data()
    seed_path = tmp_path / "seed" / "qa_templates.json"
    _write_json(seed_path, seed)

    live = deepcopy(seed)
    live["templates"] = [
        item for item in live["templates"] if item["id"] != "opening_hours"
    ]
    next(item for item in live["templates"] if item["id"] == "delivery_time")[
        "response"
    ] = "CLIENT OPERATOR DELIVERY COPY"
    next(item for item in live["templates"] if item["id"] == "greeting")[
        "response"
    ] = "CLIENT OPERATOR GREETING COPY"
    live["seeded_from_hash"] = "old-hash"
    live["operator_meta"] = "keep"
    target = tmp_path / "volume" / "qa_templates.json"
    _write_json(target, live)

    monkeypatch.setattr(
        bootstrap, "_SEEDED_FILES", (("QA_TEMPLATES_PATH", seed_path),),
    )
    monkeypatch.setenv("QA_TEMPLATES_PATH", str(target))
    actions = bootstrap.prepare_writable_storage()
    after = json.loads(target.read_text(encoding="utf-8"))

    opening_hours = next(
        item for item in after["templates"] if item["id"] == "opening_hours"
    )
    delivery_time = next(
        item for item in after["templates"] if item["id"] == "delivery_time"
    )
    greeting = next(item for item in after["templates"] if item["id"] == "greeting")

    assert opening_hours["response"] == OPENING_HOURS
    assert delivery_time["response"] == "CLIENT OPERATOR DELIVERY COPY"
    assert greeting["response"] == "CLIENT OPERATOR GREETING COPY"
    assert after["seed_version"] == _seed_data()["seed_version"]
    assert after["seeded_from_hash"] == "old-hash"
    assert after["operator_meta"] == "keep"
    assert any("opening_hours" in action for action in actions)
