"""Response Library SYSTEM copy must defer to effective Approved Q&A."""

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from camellia import app as app_module
from camellia import approved_qa, approved_response_alignment, client_qa
from camellia.config import config
from camellia import database as database_module
from camellia.database import Database
from camellia.responder import responder
from camellia.session_memory import session_memory


def _auth():
    return {"X-Admin-Key": config.ADMIN_API_KEY, "X-Admin-Pin": config.ADMIN_API_KEY}


@pytest.fixture
def isolated_alignment_db(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "alignment.db"))
    database.init_db()
    monkeypatch.setattr(database_module, "db", database)
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr("camellia.responder.db", database)
    monkeypatch.setattr("camellia.handoff.db", database)
    return database


def test_audit_accounts_for_all_67_system_entries_and_greeting_is_preserved():
    templates = responder.qa_data["templates"]
    seed_ids = {str(row["id"]) for row in templates}
    report = json.loads(
        (Path(__file__).resolve().parents[1] / "docs" / "QA_FINAL1_RESPONSE_LIBRARY_ALIGNMENT.json")
        .read_text(encoding="utf-8")
    )
    records = report["records"]
    report_ids = [row["template_id"] for row in records]

    assert len(templates) == report["system_entry_count"] == 67
    assert len(records) == 67
    assert len(set(report_ids)) == 67
    assert set(report_ids) == seed_ids
    assert {row["classification"] for row in records} == {
        "APPROVED_QA_OVERLAP", "SYSTEM_GUARD", "NO_APPROVED_AUTHORITY", "MENU_ASSET_DELEGATION",
    }
    for record in records:
        template_id = record["template_id"]
        if template_id in approved_response_alignment.MENU_DELEGATION_TEMPLATE_IDS:
            assert record["effective_runtime_authority"] == "MENU_ASSET_DELEGATION"
            assert record["classification"] == "MENU_ASSET_DELEGATION"
            continue
        if template_id in approved_response_alignment.SYSTEM_GUARD_TEMPLATE_IDS:
            assert record["classification"] == "SYSTEM_GUARD"
            assert record["action"] == ("DYNAMIC_MENU_DELEGATION" if template_id in approved_response_alignment.MENU_DELEGATION_TEMPLATE_IDS else "PRESERVED")
            continue
        if template_id in approved_response_alignment.APPROVED_QA_BINDINGS:
            qa_id, answer = approved_response_alignment.effective_binding(template_id)
            if qa_id and answer:
                assert record["classification"] == "APPROVED_QA_OVERLAP"
                assert record["approved_qa_ids"] == [qa_id]
                assert record["action"] == "ALIGNED"
                assert record["authority_resolution"] == "DYNAMIC_DELEGATION"
            else:
                assert record["classification"] == "NO_APPROVED_AUTHORITY"
                assert record["approved_qa_ids"] == []
                assert record["action"] == "HANDOFF_ONLY"
            continue
        assert record["classification"] == "NO_APPROVED_AUTHORITY"
        assert record["approved_qa_ids"] == []
        assert record["action"] == "HANDOFF_ONLY"
    greeting = next(row for row in records if row["template_id"] == "greeting")
    assert greeting["classification"] == "SYSTEM_GUARD"
    assert greeting["action"] == "PRESERVED"
    assert greeting["customer_facing_wording_changed"] is False
    assert all(row["authority_resolution"] != "STATIC_COPY" for row in records)


@pytest.mark.parametrize("template_id", sorted(approved_response_alignment.APPROVED_QA_BINDINGS))
@pytest.mark.parametrize("lifecycle", ["SCHEDULED", "EXPIRED", "ARCHIVED"])
def test_saved_system_copy_cannot_survive_qa_lifecycle(isolated_alignment_db, template_id, lifecycle):
    database = isolated_alignment_db
    database.save_response_template_override(template_id, approved_response_alignment.effective_binding(template_id)[1] or 'Former copy.', 'test')
    before = database.get_response_template_override_versions(template_id)
    with database.get_connection() as conn:
        for qa_id in approved_response_alignment.APPROVED_QA_BINDINGS[template_id]:
            conn.execute("""INSERT INTO approved_qa_overrides
                (qa_id, record_type, status, valid_from, valid_until, created_at, updated_at, created_by, updated_by)
                VALUES (?, 'BASE_OVERRIDE', ?, ?, ?, 'test', 'test', 'test', 'test')
                ON CONFLICT(qa_id) DO UPDATE SET status=excluded.status,
                    valid_from=excluded.valid_from, valid_until=excluded.valid_until""",
                (qa_id, 'ARCHIVED' if lifecycle == 'ARCHIVED' else 'ACTIVE',
                 '2026-11-01T00:00:00+00:00' if lifecycle == 'SCHEDULED' else None,
                 '2026-09-01T00:00:00+00:00' if lifecycle == 'EXPIRED' else None))
        conn.commit()
    # No startup re-alignment: the persisted active override still holds old copy.
    assert approved_response_alignment.effective_binding(template_id) == (None, None)
    assert responder._template_by_id(template_id)["response"] == responder._generic_fallback()
    assert next(t for t in responder._customer_templates() if t['id'] == template_id)['response'] == responder._generic_fallback()
    materialized = next(t for t in responder._templates_with_overrides() if t['id'] == template_id)
    assert materialized['escalate'] is True
    assert materialized['response'] == responder._generic_fallback()
    assert responder._format_dynamic_response(template_id, 'STALE COPY', channel='instagram') == (responder._generic_fallback(), None)
    assert responder.render_approved_template(template_id)[0] == responder._generic_fallback()
    card = app_module._system_template_public(materialized, database.get_response_template_override(template_id))
    assert card['approved_answer'] == responder._generic_fallback()
    assert card['response_variants'] == [responder._generic_fallback()]
    assert database.get_response_template_override_versions(template_id) == before


@pytest.mark.parametrize("template_id", sorted(approved_response_alignment.APPROVED_QA_BINDINGS))
def test_runtime_reads_current_qa_without_startup_or_saved_copy(isolated_alignment_db, monkeypatch, template_id):
    database = isolated_alignment_db
    database.save_response_template_override(template_id, 'Former copy.', 'test')
    qa_id, answer = approved_response_alignment.effective_binding(template_id)
    if not qa_id:
        assert template_id in {'pack_size_500g', 'pack_size_1kg'}
        return
    approved_qa.archive_or_restore(qa_id, 'test')  # create the ordinary overlay row
    with database.get_connection() as conn:
        conn.execute("UPDATE approved_qa_overrides SET status='ACTIVE', answer=? WHERE qa_id=?", (answer + '\nCurrent approved revision.', qa_id))
        conn.commit()
    expected = answer + '\nCurrent approved revision.'
    assert responder._template_by_id(template_id)['response'] == expected
    assert next(t for t in responder._templates_with_overrides() if t['id']==template_id)['response'] == expected
    assert responder._format_dynamic_response(template_id, 'CONFLICTING COPY', channel='instagram')[0] == expected
    monkeypatch.setattr('camellia.responder.runtime_quarantine_enabled', lambda *args: True)
    assert responder.render_approved_template(template_id)[0] == expected
    seed = next(t for t in responder.qa_data['templates'] if t['id'] == template_id)
    if seed.get('escalate'):
        assert responder._template_by_id(template_id)['escalate'] is True


@pytest.mark.parametrize('message', ['500g', '1kg', '500g sondesh price', 'Do you offer a 1kg pack?'])
def test_unsupported_pack_cannot_borrow_general_sondesh_price(isolated_alignment_db, message):
    for template_id in ('pack_size_500g', 'pack_size_1kg'):
        isolated_alignment_db.save_response_template_override(template_id, 'Old unsupported pack availability.', 'test')
        assert responder._template_by_id(template_id)['response'] == responder._generic_fallback()
    uid = 'alignment-pack-' + message
    result = responder.match_and_respond(message, user_id=uid, channel='instagram')
    assert result == (responder._generic_fallback(), False, None, None)
    assert session_memory.get_facts(uid, 'instagram')['last_route_source'] == 'HUMAN_HANDOFF'


def test_250g_uses_exact_approved_price_and_bento_details_cannot_borrow_active_price(isolated_alignment_db):
    assert approved_response_alignment.effective_binding('pack_size_250g') == (
        'sondesh_price_client', client_qa.effective_entry('sondesh_price_client').answer)
    approved_qa.archive_or_restore('bento_details_client', 'test')
    assert client_qa.effective_entry('bento_price_client') is not None
    assert responder.render_approved_template('bento_details') == (responder._generic_fallback(), None)


@pytest.mark.parametrize('subject,qa_id', [(None, None), ('sondesh', 'sondesh_delivery_time'), ('savoury', 'savoury_delivery_time')])
@pytest.mark.parametrize('message', ['how much time', 'delivery time', 'when will it arrive'])
def test_generic_delivery_needs_a_safe_product_context(isolated_alignment_db, subject, qa_id, message):
    uid = 'alignment-delivery-' + str(subject) + message
    session_memory.get_facts(uid, 'instagram')['active_topic'] = subject
    result = responder.match_and_respond(message, user_id=uid, channel='instagram')
    if not qa_id:
        assert result[0] == responder._generic_fallback()
        assert result[1] is False
    else:
        assert result[0] == client_qa.effective_entry(qa_id).answer


def test_alignment_uses_exact_effective_answer_and_preserves_identity_and_history(isolated_alignment_db):
    isolated_db = isolated_alignment_db
    template_snapshot = {
        row["id"]: (tuple(row.get("triggers") or ()), row.get("response"), tuple(row.get("responses") or ()))
        for row in responder.qa_data["templates"]
    }
    source_path = client_qa.SOURCE_PATH
    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    customer_id = isolated_db.add_learned_knowledge(
        "Alignment test", "alignment custom-library question", "Custom library remains separate."
    )
    custom_before = isolated_db.get_custom_qa(customer_id)

    changes = approved_response_alignment.apply_approved_qa_alignment(
        isolated_db, responder.qa_data["templates"],
    )
    assert set(changes) == set(approved_response_alignment.APPROVED_QA_BINDINGS)
    assert set(changes.values()) <= {"ALIGNED", "UNCHANGED"}

    for template_id in approved_response_alignment.APPROVED_QA_BINDINGS:
        qa_id, answer = approved_response_alignment.effective_binding(template_id)
        override = isolated_db.get_response_template_override(template_id)
        versions = isolated_db.get_response_template_override_versions(template_id)
        assert override["status"] == "ACTIVE"
        assert override["response"] == (answer or approved_response_alignment.customer_facing_notice())
        assert versions
        assert versions[-1]["change_reason"] == "initial_override_default"
        assert versions[-1]["response"] == template_snapshot[template_id][1] or versions[-1]["response"] == (template_snapshot[template_id][2] or ("",))[0]

    assert isolated_db.get_response_template_override("greeting") is None
    assert isolated_db.get_custom_qa(customer_id) == custom_before
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_digest
    assert {
        row["id"]: (tuple(row.get("triggers") or ()), row.get("response"), tuple(row.get("responses") or ()))
        for row in responder.qa_data["templates"]
    } == template_snapshot

    first_versions = {
        template_id: len(isolated_db.get_response_template_override_versions(template_id))
        for template_id in approved_response_alignment.APPROVED_QA_BINDINGS
    }
    rerun = approved_response_alignment.apply_approved_qa_alignment(
        isolated_db, responder.qa_data["templates"],
    )
    assert set(rerun.values()) == {"UNCHANGED"}
    assert {
        template_id: len(isolated_db.get_response_template_override_versions(template_id))
        for template_id in approved_response_alignment.APPROVED_QA_BINDINGS
    } == first_versions


def test_effective_price_qa_wins_and_conflicting_system_copy_is_rejected(isolated_alignment_db):
    isolated_db = isolated_alignment_db
    approved_response_alignment.apply_approved_qa_alignment(isolated_db, responder.qa_data["templates"])
    client = TestClient(app_module.app)
    match = client_qa.match("What is the price of Chicken Kosha?", {})
    assert match and match.entry.qa_id == "chicken_kosha_price_client"

    session_memory.clear_session("alignment-price", "instagram")
    text = responder.match_and_respond(
        "What is the price of Chicken Kosha?", user_id="alignment-price", channel="instagram",
    )[0]
    assert text == client_qa.effective_entry("chicken_kosha_price_client").answer

    canonical = approved_response_alignment.effective_binding("chicken_kosha_price")
    response = client.patch(
        "/admin/response-library/system/chicken_kosha_price",
        json={"response": "An alternative unsupported price."}, headers=_auth(),
    )
    assert response.status_code == 409
    assert isolated_db.get_response_template_override("chicken_kosha_price")["response"] == canonical[1]

    # Pricing, delivery, payment, and prebooking compatibility templates all
    # reject independently authored business copy, including exact old text.
    for template_id in approved_response_alignment.APPROVED_QA_BINDINGS:
        _qa_id, effective_answer = approved_response_alignment.effective_binding(template_id)
        if not effective_answer:
            continue
        template = next(row for row in responder.qa_data["templates"] if row["id"] == template_id)
        if template.get("escalate") or not (template.get("response") or template.get("responses")):
            continue
        response = client.patch(
            f"/admin/response-library/system/{template_id}",
            json={"response": "Conflicting legacy wording."}, headers=_auth(),
        )
        assert response.status_code == 409, template_id


def test_representative_price_delivery_payment_and_prebooking_use_exact_qa():
    for index, message in enumerate((
        "What is the price of Chicken Kosha?",
        "How do I pay?",
        "What's the delivery time?",
        "What are the next steps to pre book?",
    )):
        expected = client_qa.match(message, {})
        assert expected is not None, message
        user_id = f"alignment-authority-{index}"
        session_memory.clear_session(user_id, "instagram")
        actual = responder.match_and_respond(message, user_id=user_id, channel="instagram")[0]
        assert actual == expected.entry.answer or actual.startswith(expected.entry.answer + "\n\nContinue on WhatsApp:"), message


def test_inactive_approved_answer_is_not_resurrected_by_legacy_template(isolated_alignment_db):
    isolated_db = isolated_alignment_db
    approved_qa.archive_or_restore("home_table_details", "alignment-test")
    assert approved_response_alignment.effective_binding("bengali_home_table_details") == (None, None)
    approved_response_alignment.apply_approved_qa_alignment(isolated_db, responder.qa_data["templates"])

    user_id = "alignment-expired-home-table"
    session_memory.clear_session(user_id, "instagram")
    response = responder.match_and_respond(
        "Tell me more about Bengali Home Table", user_id=user_id, channel="instagram",
    )
    facts = session_memory.get_facts(user_id, "instagram")
    assert response[0] == responder._generic_fallback()
    assert facts["last_route_source"] == "HUMAN_HANDOFF"
    assert "home_table_details" not in (facts.get("client_qa_id"),)


def test_unsupported_business_fact_hands_off_and_pure_greeting_guard_survives(isolated_alignment_db):
    isolated_db = isolated_alignment_db
    baseline_uid = "alignment-greeting-baseline"
    session_memory.clear_session(baseline_uid, "instagram")
    baseline_greeting = responder.match_and_respond(
        "hi", user_id=baseline_uid, channel="instagram",
    )
    approved_response_alignment.apply_approved_qa_alignment(isolated_db, responder.qa_data["templates"])

    uid = "alignment-unsupported-storage"
    session_memory.clear_session(uid, "instagram")
    unsupported = responder.match_and_respond(
        "What is the shelf life of sondesh?", user_id=uid, channel="instagram",
    )
    assert unsupported[0] == responder._generic_fallback()
    assert session_memory.get_facts(uid, "instagram")["last_route_source"] == "HUMAN_HANDOFF"

    mapped_uid = "alignment-unsupported-with-source"
    session_memory.clear_session(mapped_uid, "instagram")
    session_memory.get_facts(mapped_uid, "instagram")["source_route"] = {
        "routing_id": "mapped-source", "active": True, "route_mode": "CUSTOM",
    }
    mapped_unsupported = responder.match_and_respond(
        "What is the shelf life of sondesh?", user_id=mapped_uid, channel="instagram",
    )
    assert mapped_unsupported[0] == responder._generic_fallback()
    assert session_memory.get_facts(mapped_uid, "instagram")["last_route_source"] == "HUMAN_HANDOFF"

    greeting_uid = "alignment-greeting"
    session_memory.clear_session(greeting_uid, "instagram")
    greeting = responder.match_and_respond("hi", user_id=greeting_uid, channel="instagram")
    assert greeting[0] == baseline_greeting[0]
    assert isolated_db.get_response_template_override("greeting") is None


@pytest.mark.parametrize("message", ["Aamar Mishti menu", "Aamar menu", "show me Aamar Mishti", "Signature menu", "show me Signature"])
def test_named_collection_asset_uses_menu_authority_without_legacy_prose(message, monkeypatch):
    monkeypatch.setattr("camellia.responder.current_menu_image", lambda key: "/uploads/verified-" + key + ".jpeg")
    result = responder.match_and_respond(message, user_id="named-" + message, channel="instagram")
    assert result[1] is True
    assert result[0] == ""
    key = "signature" if "Signature" in message else "aamar_mishti"
    assert result[3] == ["/uploads/verified-" + key + ".jpeg"]


def test_named_collection_missing_asset_fails_closed(monkeypatch):
    monkeypatch.setattr("camellia.responder.current_menu_image", lambda key: None)
    result = responder.match_and_respond("Aamar menu", user_id="missing-aamar", channel="instagram")
    assert result[1] is False
    assert result[3] is None
    assert "Manager of Kitchens" in result[0]


@pytest.mark.parametrize("template_id", ["aamar_mishti_collection", "signature_collection", "weekly_menu_veg", "weekly_menu_nonveg"])
def test_asset_delegate_ignores_independent_library_business_copy(template_id, monkeypatch):
    monkeypatch.setattr("camellia.responder.current_menu_image", lambda *args: "/uploads/current-collection.jpeg")
    monkeypatch.setattr("camellia.responder.current_weekly_url", lambda: "/uploads/current-weekly.jpeg")
    text, images = responder._format_dynamic_response(template_id, "LEGACY unsupported prices and packs")
    assert text == ""
    assert images


def test_previous_menu_delegate_uses_existing_archive_authority(monkeypatch):
    monkeypatch.setattr("camellia.responder.weekly_flyer.list_archive", lambda: [
        {"url": "/uploads/archived-reviewed.jpeg", "week_of": "Archived week"},
    ])
    text, images = responder._format_dynamic_response("previous_menus", "LEGACY business facts")
    assert "LEGACY" not in text
    assert images == [{"url": "/uploads/archived-reviewed.jpeg", "caption": "*Menu — Archived week*"}]
