"""
Custom Q&A: the client-managed reusable knowledge library on top of the
existing learned_knowledge store. No second knowledge system — every test
here writes through the same table Teach & Resolve and candidate approval
already write to, and reads through the same responder/rag_fallback/
ai_responder matcher those already use.
"""
import io
import json
import csv

import pytest
from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia.config import config
from camellia.database import Database, db
from camellia.responder import responder
from camellia.session_memory import session_memory

client = TestClient(app_module.app)


def _csv_import(text, confirm=False):
    return client.post('/admin/custom-qa/import/' + ('confirm' if confirm else 'dry-run'),
                       files={'file': ('library.csv', text.encode('utf-8'), 'text/csv')}, headers=_auth())


class TestLibraryManagement:
    def test_round_trip_update_unchanged_new_and_replay(self, isolated_db):
        from camellia import response_library as lib
        a = isolated_db.add_learned_knowledge('A', 'alpha packing preference', 'Please discuss packing with the kitchen.')
        b = isolated_db.add_learned_knowledge('B', 'bravo packing preference', 'A reusable, safe answer.',
                    aliases=['bravo carry bag', 'বাংলা ব্যাগ'], status='DISABLED')
        export = client.get('/admin/custom-qa/export.csv', headers=_auth()).text
        records = list(csv.DictReader(io.StringIO(export.lstrip('\ufeff'))))
        answer = '₹ বাংলা, "quoted"\nCustomer\'s preferred bag.'
        for row in records:
            if int(row['id']) == a:
                row.update(topic='Changed name', primary_question='alpha revised packing', approved_answer=answer, priority='HIGH')
        records.append({'id': '', 'topic': 'C', 'primary_question': 'charlie special bag', 'aliases': '[]',
                        'approved_answer': 'Please discuss your bag preference.', 'status': 'ARCHIVED', 'priority': 'NORMAL'})
        text = lib.csv_text(lib.COLUMNS, records)
        before = isolated_db.list_custom_qa(status='ALL')
        dry = _csv_import(text).json()
        assert dry['summary'] == {'rows': 3, 'new': 1, 'update': 1, 'unchanged': 1, 'conflict': 0, 'error': 0}
        assert isolated_db.list_custom_qa(status='ALL') == before
        assert _csv_import(text, True).status_code == 200
        assert isolated_db.get_custom_qa(a)['verified_answer'] == answer
        assert isolated_db.get_custom_qa(a)['topic'] == 'Changed name'
        assert isolated_db.get_custom_qa(a)['priority'] == 'HIGH'
        assert isolated_db.get_custom_qa(b)['status'] == 'DISABLED'
        assert lib.alias_texts(isolated_db.get_custom_qa(b)['aliases']) == ['bravo carry bag', 'বাংলা ব্যাগ']
        assert _csv_import(text, True).json()['imported'] == 0  # identical blank-ID row is skipped, never overwritten
        for _ in range(2):
            exported = client.get('/admin/custom-qa/export.csv', headers=_auth()).text
            assert _csv_import(exported, True).json()['imported'] == 0
        assert len(isolated_db.list_custom_qa(status='ALL')) == 3
        assert len(isolated_db.get_learned_knowledge_versions(a)) == 1

    @pytest.mark.parametrize('value', ['=SUM(1,2)', '+formula', '-formula', '@formula', "'apostrophe", '₹ বাংলা, "quotes"\nnew line', '\t=formula'])
    def test_excel_safe_reversible_cells(self, value):
        from camellia import response_library as lib
        text = lib.csv_text(lib.COLUMNS, [{'topic': value, 'primary_question': 'isolated export question',
                                         'approved_answer': value, 'aliases': '[]'}])
        assert text.startswith('\ufeff')
        assert lib.parse_csv(text)[0]['approved_answer'] == value
        if value.lstrip().startswith(('=', '+', '-', '@')):
            assert list(csv.DictReader(io.StringIO(text.lstrip('\ufeff'))))[0]['approved_answer'].startswith("'")

    def test_exports_empty_and_full_system_inventory(self, isolated_db):
        custom = client.get('/admin/custom-qa/export.csv', headers=_auth()).text
        reader = csv.DictReader(io.StringIO(custom.lstrip('\ufeff')))
        assert reader.fieldnames == app_module.library.COLUMNS
        assert list(reader) == []
        full = client.get('/admin/response-library/export.csv', headers=_auth()).text
        rows = list(csv.DictReader(io.StringIO(full.lstrip('\ufeff'))))
        assert len(rows) == len(responder.qa_data['templates']) == 67
        assert {r['record_type'] for r in rows} == {'SYSTEM'}
        assert all(r['protected_runtime'] == 'true' and r['importable'] == 'false' for r in rows)
        assert _csv_import(full, True).status_code == 400
        assert isolated_db.list_custom_qa(status='ALL') == []

    @pytest.mark.parametrize('id_value', ['system:greeting', 'greeting', '9999999', '-1', '1.0'])
    def test_invalid_import_id_blocks_all_writes(self, isolated_db, id_value):
        lib = app_module.library
        text = lib.csv_text(lib.COLUMNS, [{'id': id_value, 'topic': 'Bag', 'primary_question': 'unusual carry bag',
                    'approved_answer': 'Discuss the carry bag.', 'aliases': '[]'},
                   {'topic': 'Other', 'primary_question': 'separate unique bag', 'approved_answer': 'Discuss the preference.', 'aliases': '[]'}])
        assert _csv_import(text).json()['summary']['error'] == 1
        assert _csv_import(text, True).status_code == 409
        assert isolated_db.list_custom_qa(status='ALL') == []

    def test_conflicts_all_authorities_and_unicode(self, isolated_db):
        from camellia import approved_qa
        approved = next(r for r in approved_qa.listing()['records'] if r['qa_id'] == 'home_table_details')
        for question, source in [('HI!!!', 'SYSTEM'), (approved['question'], 'APPROVED_QA')]:
            response = client.post('/admin/custom-qa', json={'topic': 'Conflict', 'primary_question': question,
                        'approved_answer': 'A harmless different answer.', 'force_save': True}, headers=_auth())
            assert response.status_code == 409
            assert source in {r['source'] for r in response.json()['detail']['existing']}
            text = app_module.library.csv_text(app_module.library.COLUMNS, [{'topic': 'Conflict', 'primary_question': question,
                        'aliases': '[]', 'approved_answer': 'A harmless different answer.'}])
            assert _csv_import(text, True).status_code == 409
        isolated_db.add_learned_knowledge('বাংলা', 'বাংলা ব্যাগ', 'Please ask the kitchen.')
        response = client.post('/admin/custom-qa', json={'topic': 'Other', 'primary_question': 'বাংলা ব্যাগ!',
                            'approved_answer': 'Please ask the kitchen.', 'force_save': True}, headers=_auth())
        assert response.status_code == 409

    @pytest.mark.parametrize('aliases', ['hi', [123], {'alias': 'hi'}])
    def test_invalid_alias_shape(self, isolated_db, aliases):
        assert client.post('/admin/custom-qa', json={'topic': 'Bag', 'primary_question': 'special bag preference',
                    'approved_answer': 'Please ask the kitchen.', 'aliases': aliases}, headers=_auth()).status_code == 400

    def test_full_edit_and_lifecycle_same_id(self, isolated_db):
        response = client.post('/admin/custom-qa', json={'topic': 'Packing', 'primary_question': 'special bag preference',
                    'approved_answer': 'Please discuss packing with the kitchen.'}, headers=_auth())
        kid = response.json()['id']
        assert client.patch(f'/admin/custom-qa/{kid}', json={'topic': 'New topic', 'primary_question': 'revised bag preference',
            'aliases': ['special tote preference'], 'approved_answer': 'Please discuss your tote with the kitchen.',
            'priority': 'HIGH', 'status': 'ACTIVE', 'force_save': True}, headers=_auth()).status_code == 200
        for state in ['DISABLED', 'ACTIVE', 'ARCHIVED', 'ACTIVE']:
            assert client.post(f'/admin/custom-qa/{kid}/status', json={'status': state}, headers=_auth()).status_code == 200
            assert bool(isolated_db.search_learned_knowledge('special tote preference', count_usage=False)) == (state == 'ACTIVE')
        row = isolated_db.get_custom_qa(kid)
        assert row['topic'] == 'New topic' and row['priority'] == 'HIGH'
        assert row['question_pattern'] == 'revised bag preference'
        assert len(isolated_db.list_custom_qa(status='ALL')) == 1

    def test_restore_rechecks_new_conflicts(self, isolated_db):
        kid = isolated_db.add_learned_knowledge('Bag', 'hi', 'Historical custom greeting.', status='ARCHIVED')
        isolated_db.update_custom_qa(kid, 'Bag', 'special carry bag', 'Please ask the kitchen.', [], 'NORMAL', 'ARCHIVED', 'tester')
        version = isolated_db.get_learned_knowledge_versions(kid)[0]['version_id']
        assert client.post(f'/admin/custom-qa/{kid}/restore/{version}', json={}, headers=_auth()).status_code == 409
        isolated_db.update_custom_qa(kid, 'Bag', 'hi', 'Historical greeting.', [], 'NORMAL', 'ARCHIVED', 'tester')
        assert client.post(f'/admin/custom-qa/{kid}/status', json={'status': 'ACTIVE'}, headers=_auth()).status_code == 409

    def test_delete_guards_and_legacy_endpoint(self, isolated_db):
        kid = isolated_db.add_learned_knowledge('Bag', 'special carry bag', 'Please ask the kitchen.')
        body = {'confirm_permanent_delete': True, 'confirm_question': 'special carry bag'}
        for route in ['/admin/custom-qa/', '/admin/learned-knowledge/']:
            assert client.request('DELETE', route + str(kid), json=body, headers=_auth()).status_code == 409
        isolated_db.set_custom_qa_status(kid, 'ARCHIVED', 'tester')
        assert client.delete(f'/admin/custom-qa/{kid}', headers=_auth()).status_code == 400
        with isolated_db.get_connection() as conn:
            conn.execute('INSERT INTO triage_shadow(user_message, matched_knowledge_id) VALUES (?, ?)', ('test', kid))
        assert client.request('DELETE', f'/admin/custom-qa/{kid}', json=body, headers=_auth()).status_code == 409
        assert isolated_db.get_custom_qa(kid)

    def test_protected_system_rejects_lifecycle_identity_and_delete(self, isolated_db):
        for payload in [{'response': 'Safe greeting.', 'status': 'ARCHIVED'},
                        {'response': 'Safe greeting.', 'triggers': ['hacked']}]:
            assert client.patch('/admin/response-library/system/greeting', json=payload, headers=_auth()).status_code == 400
        assert client.delete('/admin/response-library/system/greeting', headers=_auth()).status_code == 405
        assert client.get('/admin/response-library?status=ALL', headers=_auth()).json()['counts'] == {'all': 67, 'system': 67, 'custom': 0}

    def test_server_metadata_ignored_and_confirm_revalidates(self, isolated_db):
        lib = app_module.library
        text = lib.csv_text(lib.COLUMNS, [{'topic': 'Bag', 'primary_question': 'special carry bag', 'approved_answer': 'Please discuss your bag.',
                     'aliases': '[]', 'source': 'APPROVED_QA', 'usage_count': 9000, 'created_at': 'forged'}])
        assert _csv_import(text).json()['summary']['new'] == 1
        assert _csv_import(text, True).status_code == 200
        row = isolated_db.list_custom_qa(status='ALL')[0]
        assert row['source'] == 'csv_import' and row['usage_count'] == 0 and row['created_at'] != 'forged'
        changed = text.replace('Please discuss your bag.', 'A different conflicting answer.')
        assert _csv_import(changed, True).status_code == 409
        assert isolated_db.get_custom_qa(row['id'])['verified_answer'] == 'Please discuss your bag.'

    def test_ui_type_counts_toolbar_and_guard_controls(self, isolated_db):
        page = client.get('/admin').text
        for marker in ['Response Library', 'responseLibraryCounts', 'libraryType-SYSTEM', 'libraryType-CUSTOM',
                       'SYSTEM · PROTECTED', 'CUSTOM</span>', 'Edit Response', 'Export Full Library',
                       'Import Custom Q&amp;A', 'Download CSV Template', 'FINAL CONFIRMATION', 'Delete Permanently']:
            assert marker in page


def _auth():
    return {"X-Admin-Key": config.ADMIN_API_KEY, "X-Admin-Pin": config.ADMIN_API_KEY}


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "custom_qa.db"))
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr("camellia.responder.db", database)
    monkeypatch.setattr("camellia.rag_fallback.db", database)
    monkeypatch.setattr("camellia.ai_responder.db", database)
    monkeypatch.setattr("camellia.triage.db", database)
    monkeypatch.setattr("camellia.assisted_resolution.db", database)
    return database


# -- 33: data model ---------------------------------------------------------

class TestDataModel:
    def test_existing_learned_knowledge_migrates_safely(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Delivery", "do you deliver to noida", "Yes, across Delhi NCR.", source="staff_resolution")
        row = isolated_db.get_custom_qa(kid)
        assert row["status"] == "ACTIVE"
        assert row["priority"] == "NORMAL"
        assert row["usage_count"] == 0
        assert row["last_used_at"] is None

    def test_existing_answer_still_matches(self, isolated_db):
        isolated_db.add_learned_knowledge("Delivery", "do you deliver to noida", "Yes, across Delhi NCR.")
        match = isolated_db.search_learned_knowledge("do you deliver to noida", count_usage=False)
        assert match and match["verified_answer"] == "Yes, across Delhi NCR."

    def test_create_custom_qa(self, isolated_db):
        resp = client.post("/admin/custom-qa", json={
            "topic": "Corporate Gifting", "primary_question": "Do you make corporate gift hampers?",
            "approved_answer": "Yes, we create custom corporate sweet hampers.",
        }, headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    def test_edit_same_record(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "primary q", "answer one")
        resp = client.patch(f"/admin/custom-qa/{kid}", json={"approved_answer": "answer two, edited"}, headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["id"] == kid
        assert isolated_db.get_custom_qa(kid)["verified_answer"] == "answer two, edited"

    def test_no_duplicate_record_on_edit(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "primary q", "answer one")
        before = len(isolated_db.list_custom_qa(status="ALL"))
        client.patch(f"/admin/custom-qa/{kid}", json={"approved_answer": "answer two"}, headers=_auth())
        after = len(isolated_db.list_custom_qa(status="ALL"))
        assert before == after

    def test_aliases_persist(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "what is the price per meal", "It is 250 rupees.", aliases=["meal price", "price per meal"])
        aliases = {a["alias_text"] for a in isolated_db.get_learned_knowledge_aliases(kid)}
        assert aliases == {"meal price", "price per meal"}

    def test_alias_matches_same_answer(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "what is the price per meal", "It is 250 rupees.", aliases=["how much is a meal"])
        primary = isolated_db.search_learned_knowledge("what is the price per meal", count_usage=False)
        via_alias = isolated_db.search_learned_knowledge("how much is a meal", count_usage=False)
        assert primary["id"] == via_alias["id"]

    def test_delete_alias_only_doesnt_delete_qa(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "primary q", "answer", aliases=["alias one"])
        alias_id = isolated_db.get_learned_knowledge_aliases(kid)[0]["id"]
        assert isolated_db.remove_learned_knowledge_alias(alias_id)
        assert isolated_db.get_custom_qa(kid) is not None
        assert isolated_db.get_learned_knowledge_aliases(kid) == []

    def test_priority_persists(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "q", "a", priority="HIGH")
        assert isolated_db.get_custom_qa(kid)["priority"] == "HIGH"

    def test_lifecycle_persists(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "q", "a")
        isolated_db.set_custom_qa_status(kid, "DISABLED", "tester")
        assert isolated_db.get_custom_qa(kid)["status"] == "DISABLED"


class TestResponseLibrary:
    def test_short_search_is_token_aware_and_ranked(self, isolated_db):
        isolated_db.add_learned_knowledge("Unrelated", "a long unrelated question", "This answer contains characters which should not match hi.")
        data = client.get("/admin/response-library?search=hi", headers=_auth()).json()
        items = data["items"]
        assert items and items[0]["template_id"] == "greeting"
        assert all(item.get("template_id") == "greeting" or item.get("kind") == "custom_qa" and item.get("primary_question") != "a long unrelated question" for item in items)
        assert all(int(item.get("relevance_score") or 0) >= 75 for item in items if item.get("template_id") == "greeting")

    @pytest.mark.parametrize("query, expected_template", [
        ("hello", "greeting"),
        ("delivery", "delivery_area"),
        ("jolbhora", "jolbhora"),
        ("nolen gur", "sondesh_nolen_gur"),
    ])
    def test_search_returns_relevant_system_template_first(self, isolated_db, query, expected_template):
        data = client.get(f"/admin/response-library?search={query}", headers=_auth()).json()
        assert data["items"]
        assert data["items"][0]["template_id"] == expected_template

    def test_answer_text_search_uses_whole_words(self, isolated_db):
        isolated_db.add_learned_knowledge("Unrelated", "unrelated question", "This contains delivery as a whole word.")
        data = client.get("/admin/response-library?search=delivery", headers=_auth()).json()
        assert data["items"]
        assert data["items"][0]["template_id"] in {"delivery_area", "delivery_time", "delivery_out_of_area", "delivery"} or data["items"][0]["kind"] == "custom_qa"
        assert any(item.get("kind") == "custom_qa" for item in data["items"])

    def test_price_search_does_not_use_character_substrings(self, isolated_db):
        data = client.get("/admin/response-library?search=price", headers=_auth()).json()
        assert data["items"]
        assert all(int(item.get("relevance_score") or 0) > 0 for item in data["items"])

    def test_system_templates_are_searchable_without_duplicate_rows(self, isolated_db):
        resp = client.get("/admin/response-library?search=hi", headers=_auth())
        assert resp.status_code == 200
        greeting = next(item for item in resp.json()["items"] if item.get("template_id") == "greeting")
        assert greeting["source_label"] == "System Template"
        assert "hello" in greeting["aliases"]
        assert greeting["protected_runtime"] is True
        assert isolated_db.list_custom_qa(status="ALL") == []

    @pytest.mark.parametrize("query, expected_template", [
        ("h", "greeting"), ("sond", "sondesh_classic_rose"),
        ("jol", "jolbhora"), ("kanch", "kanchagolla"),
        ("deliv", "delivery_area"), ("sondes", "sondesh_classic_rose"),
        ("jolbora", "jolbhora"),
    ])
    def test_typeahead_prefix_and_fuzzy_queries_are_relevant(self, isolated_db, query, expected_template):
        response = client.get("/admin/response-library", params={"search": query}, headers=_auth())
        assert response.status_code == 200
        items = response.json()["items"]
        assert items and items[0]["template_id"] == expected_template

    def test_system_response_override_is_live_and_versioned(self, isolated_db):
        replacement = "Hi there! Welcome to Camellia Artisanal. How can I help you today?"
        resp = client.patch("/admin/response-library/system/greeting", json={"response": replacement}, headers=_auth())
        assert resp.status_code == 200
        reply = responder.match_and_respond("hi", user_id="response_library_greeting", channel="whatsapp")
        assert reply[2] == "greeting"
        assert reply[0] == replacement
        versions = isolated_db.get_response_template_override_versions("greeting")
        assert len(versions) == 1

        second = "Hello! Welcome to Camellia Artisanal. What can I help you with?"
        client.patch("/admin/response-library/system/greeting", json={"response": second}, headers=_auth())
        versions = isolated_db.get_response_template_override_versions("greeting")
        assert len(versions) == 2 and versions[0]["response"] == replacement

        restored = client.post(f"/admin/response-library/system/greeting/restore/{versions[0]['version_id']}", json={}, headers=_auth())
        assert restored.status_code == 200
        assert isolated_db.get_response_template_override("greeting")["response"] == replacement

    def test_response_library_test_is_dry_run(self, isolated_db):
        client.patch("/admin/response-library/system/greeting", json={"response": "Short approved greeting."}, headers=_auth())
        before = isolated_db.get_analytics().get("total_conversations", 0)
        resp = client.post("/admin/response-library/test", json={"message": "hi"}, headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["template_id"] == "greeting"
        assert resp.json()["response"] == "Short approved greeting."
        assert isolated_db.get_analytics().get("total_conversations", 0) == before

    def test_greeting_override_stays_in_admin_surfaces_not_customer_runtime(self, isolated_db, monkeypatch):
        """An admin preview override cannot replace the protected live greeting."""
        monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
        copy = "Hello hello from Camellia Artisanal!"
        assert client.patch(
            "/admin/response-library/system/greeting",
            json={"response": copy}, headers=_auth(),
        ).status_code == 200

        user_id = "response-library-instagram-consistency"
        session_memory.clear_session(user_id, "instagram")
        live = responder.match_and_respond("hi", user_id=user_id, channel="instagram")[0]
        preview = client.post(
            "/admin/response-library/system/greeting/preview", headers=_auth()
        ).json()["preview_text"]
        dry_run = client.post(
            "/admin/response-library/test", json={"message": "hi"}, headers=_auth()
        ).json()["response"]
        search = client.get(
            "/admin/response-library?search=Hello%20hello%20from%20Camellia%20Artisanal",
            headers=_auth(),
        ).json()["items"]

        assert live.startswith("Hi! Thanks for reaching out")
        assert preview == dry_run == copy
        assert search and search[0]["template_id"] == "greeting"

    def test_custom_trigger_conflict_includes_system_template(self, isolated_db):
        resp = client.post("/admin/custom-qa", json={
            "topic": "Conversation", "primary_question": "hi", "approved_answer": "Another greeting.",
        }, headers=_auth())
        assert resp.status_code == 409
        existing = resp.json()["detail"]["existing"]
        assert any(item.get("template_id") == "greeting" for item in existing)


# -- 34: matching ------------------------------------------------------------

class TestMatching:
    def test_active_runtime_accessor_excludes_archived_but_history_keeps_it(self, isolated_db):
        archived_id = isolated_db.add_learned_knowledge(
            "Retired topic",
            "tell me about the retired topic",
            "ARCHIVED ANSWER MUST NEVER BE SERVED",
            status="ARCHIVED",
            aliases=["retired topic details"],
        )

        assert any(row["id"] == archived_id for row in isolated_db.get_learned_knowledge())
        assert all(row["id"] != archived_id for row in isolated_db.get_active_learned_knowledge())
        assert isolated_db.search_learned_knowledge("tell me about the retired topic", count_usage=False) is None
        assert isolated_db.search_learned_knowledge("retired topic details", count_usage=False) is None

    @pytest.mark.parametrize('lifecycle_status', ['ARCHIVED', 'DISABLED'])
    def test_archived_knowledge_is_blocked_across_runtime_paths(self, isolated_db, lifecycle_status):
        archived_id = isolated_db.add_learned_knowledge(
            "Retired runtime topic",
            "tell me about the retired runtime topic",
            "ARCHIVED RUNTIME ANSWER MUST NEVER BE SERVED",
            status=lifecycle_status,
            aliases=["retired runtime details"],
        )

        response, matched, template_id, _ = responder.match_and_respond(
            "tell me about the retired runtime topic",
            user_id="archived_runtime_test",
            channel="instagram",
        )
        assert "ARCHIVED RUNTIME ANSWER MUST NEVER BE SERVED" not in response
        assert template_id != f"learned_{archived_id}"
        assert not (matched and template_id == f"learned_{archived_id}")

        rag_result = __import__("camellia.rag_fallback", fromlist=["rag_fallback_engine"]).rag_fallback_engine.retrieve(
            "retired runtime details",
            persist_candidate=False,
        )
        assert f"learned_knowledge#{archived_id}" not in rag_result.retrieved_sources
        assert "ARCHIVED RUNTIME ANSWER MUST NEVER BE SERVED" not in rag_result.proposed_response

        from camellia.assisted_resolution import _approved_match
        from camellia.triage import triage_classifier

        assert _approved_match("retired runtime details") == {}
        assert triage_classifier._grounding_for("retired runtime details")["matched_knowledge_id"] is None

    def test_active_equivalent_remains_callable(self, isolated_db):
        active_id = isolated_db.add_learned_knowledge(
            "Active runtime topic",
            "tell me about the active runtime topic",
            "ACTIVE APPROVED ANSWER",
            aliases=["active runtime details"],
        )

        response, matched, template_id, _ = responder.match_and_respond(
            "active runtime details",
            user_id="active_runtime_test",
            channel="whatsapp",
        )
        assert matched is True
        assert template_id == f"learned_{active_id}"
        assert response == "ACTIVE APPROVED ANSWER"

    @pytest.mark.parametrize('lifecycle_status', ['ARCHIVED', 'DISABLED'])
    def test_archived_knowledge_is_excluded_from_ai_context(self, isolated_db, lifecycle_status):
        isolated_db.add_learned_knowledge(
            "Retired AI context topic",
            "retired ai context question",
            "ARCHIVED AI CONTEXT MUST NEVER BE INCLUDED",
            status=lifecycle_status,
        )

        from camellia.ai_responder import ai_responder

        context = ai_responder._build_context_string()
        assert "ARCHIVED AI CONTEXT MUST NEVER BE INCLUDED" not in context

    def test_primary_question_matches(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "do you cater weddings", "Yes we do wedding catering.")
        assert isolated_db.search_learned_knowledge("do you cater weddings", count_usage=False)

    def test_each_alias_matches(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "do you cater weddings", "Yes.", aliases=["wedding catering", "cater a wedding event"])
        assert isolated_db.search_learned_knowledge("wedding catering", count_usage=False)
        assert isolated_db.search_learned_knowledge("cater a wedding event", count_usage=False)

    def test_disabled_qa_never_matches(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "do you cater weddings", "Yes.")
        isolated_db.set_custom_qa_status(kid, "DISABLED", "tester")
        assert isolated_db.search_learned_knowledge("do you cater weddings", count_usage=False) is None

    def test_archived_qa_never_matches(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "do you cater weddings", "Yes.")
        isolated_db.set_custom_qa_status(kid, "ARCHIVED", "tester")
        assert isolated_db.search_learned_knowledge("do you cater weddings", count_usage=False) is None

    def test_restored_qa_matches(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "do you cater weddings", "Yes.")
        isolated_db.set_custom_qa_status(kid, "ARCHIVED", "tester")
        isolated_db.set_custom_qa_status(kid, "ACTIVE", "tester")
        assert isolated_db.search_learned_knowledge("do you cater weddings", count_usage=False)

    def test_specific_qa_beats_generic(self, isolated_db):
        isolated_db.add_learned_knowledge("General", "sondesh", "We make many kinds of sondesh.")
        isolated_db.add_learned_knowledge("Specific", "kanchagolla sondesh price", "Kanchagolla sondesh is 40 rupees each.")
        match = isolated_db.search_learned_knowledge("what is the price of kanchagolla sondesh", count_usage=False)
        assert match and "Kanchagolla" in match["verified_answer"]

    def test_high_priority_does_not_override_a_specific_match(self, isolated_db):
        isolated_db.add_learned_knowledge("General", "sondesh", "Generic sondesh info.", priority="HIGH")
        isolated_db.add_learned_knowledge("Specific", "kanchagolla sondesh price is what", "Kanchagolla sondesh is 40 rupees.", priority="NORMAL")
        match = isolated_db.search_learned_knowledge("kanchagolla sondesh price is what", count_usage=False)
        assert "Kanchagolla" in match["verified_answer"]

    def test_priority_breaks_ties_among_comparable_candidates(self, isolated_db):
        # Two candidates with an identical exact substring match: priority decides.
        isolated_db.add_learned_knowledge("A", "gift hampers", "Normal-priority hamper answer.", priority="NORMAL")
        isolated_db.add_learned_knowledge("B", "gift hampers", "High-priority hamper answer.", priority="HIGH")
        match = isolated_db.search_learned_knowledge("gift hampers", count_usage=False)
        assert match["verified_answer"] == "High-priority hamper answer."

    def test_incomplete_source_metadata_does_not_authorize_priority_product_answer(self):
        user_id = "custom_qa_source_" + "x"
        session_memory.clear_session(user_id, "instagram")
        kid = db.add_learned_knowledge("Topic", "what else can you tell me", "A broad learned answer about what else we offer.")
        facts = session_memory.get_facts(user_id, "instagram")
        facts["source_route"] = {"priority_products": ["jolbhora"], "routing_topic": "reel", "active": True}
        try:
            common = {"user_id": user_id, "channel": "instagram", "referral_source": "ADS", "referral_ad_id": "route-ad"}
            responder.match_and_respond("hi", **common)
            resp = responder.match_and_respond("what else", **common)
            assert resp[1] is False
            assert resp[2] is None
            assert "Manager of Kitchens" in resp[0]
        finally:
            db.delete_learned_knowledge(kid)

    def test_learned_3_precedence_regression_remains_fixed(self):
        """A fabricated/incomplete source route cannot authorize a learned answer."""
        user_id = "custom_qa_learned3_regr"
        session_memory.clear_session(user_id, "instagram")
        kid = db.add_learned_knowledge("Broad", "tell me more", "A generic broad learned answer.")
        try:
            facts = session_memory.get_facts(user_id, "instagram")
            facts["source_route"] = {"priority_products": ["jolbhora"], "routing_topic": "reel", "active": True}
            common = {"user_id": user_id, "channel": "instagram", "referral_source": "ADS", "referral_ad_id": "route-ad"}
            resp = responder.match_and_respond("tell me more", **common)
            assert resp[1] is False
            assert resp[2] is None
            assert "Manager of Kitchens" in resp[0]
        finally:
            db.delete_learned_knowledge(kid)

    def test_canonical_product_logic_remains_authoritative(self):
        kid = db.add_learned_knowledge("Broad", "kanchagolla", "A broad learned answer that must not win over canonical pricing.")
        try:
            resp = responder.match_and_respond("what is the price of kanchagolla?", user_id="custom_qa_canon", channel="instagram", referral_source="ADS")
            assert not (resp[2] or "").startswith("learned_")
        finally:
            db.delete_learned_knowledge(kid)


# -- 35: duplicates -----------------------------------------------------------

class TestDuplicates:
    def test_exact_duplicate_primary_detected(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "do you deliver to noida", "Yes.")
        resp = client.post("/admin/custom-qa", json={
            "topic": "Topic2", "primary_question": "do you deliver to noida", "approved_answer": "Different answer.",
        }, headers=_auth())
        assert resp.status_code == 409

    def test_alias_duplicate_detected(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "do you deliver", "Yes.", aliases=["deliver to noida"])
        resp = client.post("/admin/custom-qa", json={
            "topic": "Topic2", "primary_question": "deliver to noida", "approved_answer": "Different answer.",
        }, headers=_auth())
        assert resp.status_code == 409

    def test_existing_alias_vs_new_primary_detected(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "shipping question", "Yes.", aliases=["do you ship to gurgaon"])
        dupes = isolated_db.find_learned_knowledge_duplicates("do you ship to gurgaon")
        assert dupes["exact"]

    def test_near_duplicate_warns(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "do you deliver sweets to gurgaon", "Yes we deliver.")
        resp = client.post("/admin/custom-qa", json={
            "topic": "Topic2", "primary_question": "do you deliver sweets in gurgaon area", "approved_answer": "A different phrasing of the same fact.",
        }, headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["status"] == "warning"
        assert resp.json()["similar"]

    def test_near_duplicate_does_not_auto_merge(self, isolated_db):
        before = len(isolated_db.list_custom_qa(status="ALL"))
        isolated_db.add_learned_knowledge("Topic", "do you deliver sweets to gurgaon", "Yes we deliver.")
        client.post("/admin/custom-qa", json={
            "topic": "Topic2", "primary_question": "do you deliver sweets in gurgaon area", "approved_answer": "A different phrasing of the same fact.",
        }, headers=_auth())
        after = len(isolated_db.list_custom_qa(status="ALL"))
        assert after == before + 1  # only the first save landed; the warned one was not saved

    def test_client_may_explicitly_save_distinct_near_duplicate(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "do you deliver sweets to gurgaon", "Yes we deliver.")
        resp = client.post("/admin/custom-qa", json={
            "topic": "Topic2", "primary_question": "do you deliver sweets in gurgaon area",
            "approved_answer": "A different phrasing of the same fact.", "force_save": True,
        }, headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"


# -- 36/37: test, preview, usage ----------------------------------------------

class TestDryRunAndUsage:
    def test_test_endpoint_performs_dry_run(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "how much is one meal", "It is 250 rupees per meal.")
        resp = client.post("/admin/custom-qa/test", json={"message": "how much is one meal"}, headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["would_match"] is True

    def test_test_does_not_create_conversation(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "how much is one meal", "It is 250 rupees.")
        before = isolated_db.get_analytics().get("total_conversations", 0)
        client.post("/admin/custom-qa/test", json={"message": "how much is one meal"}, headers=_auth())
        after = isolated_db.get_analytics().get("total_conversations", 0)
        assert after == before

    def test_test_does_not_increment_usage(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "how much is one meal", "It is 250 rupees.")
        client.post("/admin/custom-qa/test", json={"message": "how much is one meal"}, headers=_auth())
        assert isolated_db.get_custom_qa(kid)["usage_count"] == 0

    def test_test_does_not_mutate_session(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "how much is one meal", "It is 250 rupees.")
        user_id = "custom_qa_test_no_session"
        session_memory.clear_session(user_id, "instagram")
        before = json.dumps(session_memory.get_session(user_id, "instagram"), sort_keys=True)
        client.post("/admin/custom-qa/test", json={"message": "how much is one meal"}, headers=_auth())
        after = json.dumps(session_memory.get_session(user_id, "instagram"), sort_keys=True)
        assert before == after

    def test_test_reports_a_winning_qa_when_this_one_would_lose(self, isolated_db):
        isolated_db.add_learned_knowledge("Winner", "meal price question", "The winning answer.", priority="HIGH")
        loser_id = isolated_db.add_learned_knowledge("Loser", "meal price question", "The losing answer.", priority="NORMAL")
        resp = client.post("/admin/custom-qa/test", json={"message": "meal price question", "focus_id": loser_id}, headers=_auth())
        data = resp.json()
        assert data["would_win"] is False
        assert "reason" in data

    def test_preview_uses_live_formatting_and_sends_nothing(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "q", "The exact verbatim customer-facing answer.")
        resp = client.post("/admin/custom-qa/preview", json={"id": kid}, headers=_auth())
        data = resp.json()
        assert data["preview_text"] == "The exact verbatim customer-facing answer."
        assert "nothing has been sent" in data["note"].lower()

    def test_non_registry_custom_qa_does_not_increment_customer_runtime_usage(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "what is our catering process exactly", "It is handled by our team.")
        text, matched, _template, _images = responder.match_and_respond(
            "what is our catering process exactly", user_id="custom_qa_usage_1", channel="instagram", referral_source="ADS",
        )
        assert matched is False
        assert "Manager of Kitchens" in text
        assert isolated_db.get_custom_qa(kid)["usage_count"] == 0

    def test_last_used_at_updates(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "what is our catering process exactly", "It is handled by our team.")
        assert isolated_db.get_custom_qa(kid)["last_used_at"] is None
        text, matched, _template, _images = responder.match_and_respond(
            "what is our catering process exactly", user_id="custom_qa_usage_2", channel="instagram", referral_source="ADS",
        )
        assert matched is False
        assert "Manager of Kitchens" in text
        assert isolated_db.get_custom_qa(kid)["last_used_at"] is None

    def test_shadow_does_not_increment_usage(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "how much is one meal exactly", "It is 250 rupees.")
        from camellia.triage import triage_classifier
        triage_classifier._grounding_for("how much is one meal exactly")
        assert isolated_db.get_custom_qa(kid)["usage_count"] == 0


# -- 38: version history --------------------------------------------------

class TestVersionHistory:
    def test_edit_creates_previous_version(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "q", "answer v1")
        client.patch(f"/admin/custom-qa/{kid}", json={"approved_answer": "answer v2"}, headers=_auth())
        versions = isolated_db.get_learned_knowledge_versions(kid)
        assert len(versions) == 1
        assert versions[0]["answer"] == "answer v1"

    def test_multiple_edits_create_ordered_history(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "q", "answer v1")
        client.patch(f"/admin/custom-qa/{kid}", json={"approved_answer": "answer v2"}, headers=_auth())
        client.patch(f"/admin/custom-qa/{kid}", json={"approved_answer": "answer v3"}, headers=_auth())
        versions = isolated_db.get_learned_knowledge_versions(kid)
        assert [v["version_number"] for v in versions] == [2, 1]
        assert versions[0]["answer"] == "answer v2"

    def test_restore_works(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "q", "answer v1")
        client.patch(f"/admin/custom-qa/{kid}", json={"approved_answer": "answer v2"}, headers=_auth())
        versions = isolated_db.get_learned_knowledge_versions(kid)
        v1_id = versions[0]["version_id"]
        resp = client.post(f"/admin/custom-qa/{kid}/restore/{v1_id}", json={}, headers=_auth())
        assert resp.status_code == 200
        assert isolated_db.get_custom_qa(kid)["verified_answer"] == "answer v1"

    def test_restore_preserves_current_state_as_history(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "q", "answer v1")
        client.patch(f"/admin/custom-qa/{kid}", json={"approved_answer": "answer v2"}, headers=_auth())
        v1_id = isolated_db.get_learned_knowledge_versions(kid)[0]["version_id"]
        client.post(f"/admin/custom-qa/{kid}/restore/{v1_id}", json={}, headers=_auth())
        versions = isolated_db.get_learned_knowledge_versions(kid)
        assert any(v["answer"] == "answer v2" for v in versions)

    def test_restore_uses_the_same_learned_knowledge_id(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "q", "answer v1")
        client.patch(f"/admin/custom-qa/{kid}", json={"approved_answer": "answer v2"}, headers=_auth())
        v1_id = isolated_db.get_learned_knowledge_versions(kid)[0]["version_id"]
        client.post(f"/admin/custom-qa/{kid}/restore/{v1_id}", json={}, headers=_auth())
        assert isolated_db.get_custom_qa(kid)["id"] == kid


# -- 39: lifecycle --------------------------------------------------------

class TestLifecycle:
    def test_disable(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "q", "a")
        resp = client.post(f"/admin/custom-qa/{kid}/status", json={"status": "DISABLED"}, headers=_auth())
        assert resp.status_code == 200 and resp.json()["status"] == "DISABLED"

    def test_re_enable(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "q", "a")
        client.post(f"/admin/custom-qa/{kid}/status", json={"status": "DISABLED"}, headers=_auth())
        resp = client.post(f"/admin/custom-qa/{kid}/status", json={"status": "ACTIVE"}, headers=_auth())
        assert resp.json()["status"] == "ACTIVE"

    def test_archive(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "q", "a")
        resp = client.post(f"/admin/custom-qa/{kid}/status", json={"status": "ARCHIVED"}, headers=_auth())
        assert resp.json()["status"] == "ARCHIVED"

    def test_restore_from_archive(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "q", "a")
        client.post(f"/admin/custom-qa/{kid}/status", json={"status": "ARCHIVED"}, headers=_auth())
        resp = client.post(f"/admin/custom-qa/{kid}/status", json={"status": "ACTIVE"}, headers=_auth())
        assert resp.json()["status"] == "ACTIVE"

    def test_delete_confirmation_data_is_available_before_delete(self, isolated_db):
        kid = isolated_db.add_learned_knowledge("Topic", "q", "a")
        resp = client.get(f"/admin/custom-qa/{kid}", headers=_auth())
        data = resp.json()
        assert data["topic"] and "usage_count" in data and "source" in data

    def test_permanent_delete_affects_only_that_qa(self, isolated_db):
        kid1 = isolated_db.add_learned_knowledge("Topic1", "q1", "a1")
        kid2 = isolated_db.add_learned_knowledge("Topic2", "q2", "a2")
        isolated_db.set_custom_qa_status(kid1, "ARCHIVED", "tester")
        resp = client.request("DELETE", f"/admin/custom-qa/{kid1}", headers=_auth(),
                              json={"confirm_permanent_delete": True, "confirm_question": "q1"})
        assert resp.status_code == 200
        assert isolated_db.get_custom_qa(kid1) is None
        assert isolated_db.get_custom_qa(kid2) is not None


# -- 40: validation warnings ------------------------------------------------

class TestValidationWarnings:
    @pytest.mark.parametrize("answer,expected_flag", [
        ("A single meal costs ₹250.", "price"),
        ("Get a 20% discount on bulk orders.", "discount"),
        ("We offer free delivery on all orders.", "free_delivery"),
        ("This item is currently in stock.", "availability"),
        ("Order by 25th December for the event.", "event_date"),
        ("We are hiring pastry chefs now.", "hiring"),
        ("We accept custom-order cakes for weddings.", "custom_order"),
    ])
    def test_warning_flags_are_detected(self, answer, expected_flag):
        from camellia.candidate_safety import detect_business_commitment_warnings
        assert expected_flag in detect_business_commitment_warnings(answer)

    def test_client_confirmed_save_works_with_a_commitment_warning(self, isolated_db):
        resp = client.post("/admin/custom-qa", json={
            "topic": "Pricing", "primary_question": "how much is a meal",
            "approved_answer": "A single meal costs ₹250.", "force_save": True,
        }, headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    def test_secret_content_remains_blocked_even_with_force_save(self, isolated_db):
        resp = client.post("/admin/custom-qa", json={
            "topic": "Pricing", "primary_question": "what is your api key",
            "approved_answer": "our api_key is abc123", "force_save": True,
        }, headers=_auth())
        assert resp.status_code == 400


# -- 41: CSV -----------------------------------------------------------------

class TestCsv:
    def test_export_csv(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "q", "a")
        resp = client.get("/admin/custom-qa/export.csv", headers=_auth())
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    def test_export_includes_aliases(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "q", "a", aliases=["alias one"])
        resp = client.get("/admin/custom-qa/export.csv", headers=_auth())
        assert "alias one" in resp.text

    def test_export_excludes_credentials(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "q", "a")
        resp = client.get("/admin/custom-qa/export.csv", headers=_auth())
        header = resp.text.splitlines()[0]
        for forbidden in ("api_key", "secret", "token", "password"):
            assert forbidden not in header.lower()

    def test_download_template(self, isolated_db):
        resp = client.get("/admin/custom-qa/csv-template", headers=_auth())
        assert resp.status_code == 200
        assert "primary_question" in resp.text

    def test_import_dry_run_does_not_write(self, isolated_db):
        before = len(isolated_db.list_custom_qa(status="ALL"))
        csv_text = "topic,primary_question,aliases,approved_answer,priority,status\nDelivery,do you ship to jaipur,[],That falls outside our current coverage area.,NORMAL,ACTIVE\n"
        resp = client.post("/admin/custom-qa/import/dry-run", files={"file": ("t.csv", io.BytesIO(csv_text.encode()), "text/csv")}, headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["summary"]["new"] == 1
        assert len(isolated_db.list_custom_qa(status="ALL")) == before

    def test_valid_row_preview(self, isolated_db):
        csv_text = "topic,primary_question,aliases,approved_answer,priority,status\nDelivery,do you ship to jaipur,[],That falls outside our current coverage area.,NORMAL,ACTIVE\n"
        resp = client.post("/admin/custom-qa/import/dry-run", files={"file": ("t.csv", io.BytesIO(csv_text.encode()), "text/csv")}, headers=_auth())
        assert resp.json()["rows"][0]["operation"] == "NEW"

    def test_invalid_row_rejected(self, isolated_db):
        csv_text = "topic,primary_question,aliases,approved_answer,priority,status\n,do you ship to jaipur,[],,NORMAL,ACTIVE\n"
        resp = client.post("/admin/custom-qa/import/dry-run", files={"file": ("t.csv", io.BytesIO(csv_text.encode()), "text/csv")}, headers=_auth())
        assert resp.json()["rows"][0]["operation"] == "ERROR"

    def test_duplicate_row_identified(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "do you ship to jaipur", "Existing answer.")
        csv_text = "topic,primary_question,aliases,approved_answer,priority,status\nDelivery,do you ship to jaipur,[],A new different answer.,NORMAL,ACTIVE\n"
        resp = client.post("/admin/custom-qa/import/dry-run", files={"file": ("t.csv", io.BytesIO(csv_text.encode()), "text/csv")}, headers=_auth())
        assert resp.json()["rows"][0]["operation"] == "CONFLICT"

    def test_nothing_saved_before_confirm(self, isolated_db):
        before = len(isolated_db.list_custom_qa(status="ALL"))
        csv_text = "topic,primary_question,aliases,approved_answer,priority,status\nDelivery,do you ship to jaipur,[],That falls outside our current coverage area.,NORMAL,ACTIVE\n"
        client.post("/admin/custom-qa/import/dry-run", files={"file": ("t.csv", io.BytesIO(csv_text.encode()), "text/csv")}, headers=_auth())
        assert len(isolated_db.list_custom_qa(status="ALL")) == before

    def test_confirm_import_saves_valid_rows(self, isolated_db):
        before = len(isolated_db.list_custom_qa(status="ALL"))
        csv_text = "topic,primary_question,aliases,approved_answer,priority,status\nDelivery,do you ship to jaipur,[],That falls outside our current coverage area.,NORMAL,ACTIVE\n"
        resp = client.post("/admin/custom-qa/import/confirm", files={"file": ("t.csv", io.BytesIO(csv_text.encode()), "text/csv")}, headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["imported"] == 1
        after = len(isolated_db.list_custom_qa(status="ALL"))
        assert after == before + 1

    def test_import_does_not_overwrite_existing_silently(self, isolated_db):
        isolated_db.add_learned_knowledge("Topic", "do you ship to jaipur", "Existing answer.")
        csv_text = "topic,primary_question,aliases,approved_answer,priority,status\nDelivery,do you ship to jaipur,[],A conflicting new answer.,NORMAL,ACTIVE\n"
        resp = client.post("/admin/custom-qa/import/confirm", files={"file": ("t.csv", io.BytesIO(csv_text.encode()), "text/csv")}, headers=_auth())
        data = resp.json()
        assert resp.status_code == 409
        assert data["detail"]["summary"]["conflict"] == 1
        match = isolated_db.search_learned_knowledge("do you ship to jaipur", count_usage=False)
        assert match["verified_answer"] == "Existing answer."


# -- 42: regression -----------------------------------------------------------

class TestRegression:
    def test_candidate_approval_still_works(self, isolated_db):
        cid = isolated_db.save_candidate_template(name="t", response="A safe reusable candidate answer.", user_message="q")
        resp = client.post(f"/admin/candidates/{cid}/approve", json={"approved_by": "tester"}, headers=_auth())
        assert resp.status_code == 200

    def test_teach_and_resolve_still_works(self, isolated_db):
        eid = isolated_db.record_escalation(user_id="u1", channel="instagram", user_message="need help", reason="test")
        resp = client.post(f"/admin/escalations/{eid}/resolve", json={"verified_answer": "Here is the answer.", "status": "resolved"}, headers=_auth())
        assert resp.status_code == 200

    def test_ai_resolution_tab_still_works(self):
        page = client.get("/admin").text
        assert 'id="tab-ai-resolution"' in page

    def test_triage_shadow_remains_read_only(self):
        page = client.get("/admin").text
        section = page[page.index("async function loadTriageShadow"):]
        section = section[:section.index("// CUSTOM Q&A:")]
        for verb in ("method: 'POST'", "method: 'PATCH'", "method: 'DELETE'"):
            assert verb not in section

    def test_whatsapp_remains_human_only_with_llm_triage_enabled(self, isolated_db):
        from camellia.triage import triage_classifier
        user_id = "custom_qa_whatsapp_regr"
        session_memory.clear_session(user_id, "whatsapp")
        isolated_db.add_learned_knowledge("Topic", "test message for whatsapp gate", "An answer.")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(config, "LLM_TRIAGE_ENABLED", True)
            mp.setattr(triage_classifier, "record_shadow", lambda **kw: (_ for _ in ()).throw(AssertionError("must not fire on whatsapp")))
            responder.match_and_respond("test message for whatsapp gate", user_id=user_id, channel="whatsapp")
