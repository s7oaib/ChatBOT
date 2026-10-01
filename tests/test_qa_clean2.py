"""Audited candidates stay active; durable suppression is tested only in isolation."""
import copy
import json
import uuid
from pathlib import Path

import pytest

from camellia import app as portal, client_qa, template_retirement as lifecycle
from camellia.database import Database
from camellia.responder import responder

AUDIT = json.loads((Path(__file__).resolve().parents[1] / 'docs/QA_CLEAN2_DECISIONS.json').read_text(encoding='utf-8'))
CANDIDATES = [r for r in AUDIT['records'] if 'DUPLICATE' in r['classification']]
FIXTURE_ID = 'isolated_retirement_fixture'


@pytest.fixture
def store(tmp_path, monkeypatch):
    database = Database(str(tmp_path / 'retirement.db'))
    for module in ('database', 'app', 'responder', 'rag_fallback', 'ai_responder'):
        monkeypatch.setattr('camellia.' + module + '.db', database)
    return database


@pytest.fixture
def safe_fixture(store, monkeypatch):
    row = dict(legacy_id=FIXTURE_ID, decision='RETIRE_SAFE', retirement_safe=True,
               runtime_dependencies=[], source_mapping_references=[], special_actions_assets=[],
               matching_approved_qa_ids=['minimum_sondesh_quantity_client'], reason='Isolated reviewed fixture')
    monkeypatch.setattr(lifecycle, 'decisions', lambda: [row])
    data = copy.deepcopy(responder.qa_data)
    data['templates'].append(dict(id=FIXTURE_ID, response='LEGACY FIXTURE COPY', triggers=['isolated fixture words']))
    monkeypatch.setattr(responder, 'qa_data', data)
    return row


def test_complete_recovered_audit_and_protected_groups():
    assert AUDIT['duplicate_candidate_count'] == len(CANDIDATES) == 16
    assert AUDIT['approved_count'] == 23 and AUDIT['legacy_count'] == 67 and AUDIT['custom_count'] == 0
    assert AUDIT['decision_counts'] == dict(KEEP_DEPENDENCY=16, KEEP_NOT_DUPLICATE=50, KEEP_NEEDS_REVIEW=1)
    assert next(r for r in AUDIT['records'] if r['legacy_id']=='combo_bento')['decision']=='KEEP_NEEDS_REVIEW'
    for row in AUDIT['records']:
        if row['classification'] in ('CONVERSATIONAL','SYSTEM_OR_GUARD','UNIQUE_CUSTOM'):
            assert row['decision']=='KEEP_NOT_DUPLICATE'


@pytest.mark.parametrize('row', CANDIDATES, ids=lambda r:r['legacy_id'])
def test_each_current_candidate_is_dependency_blocked_without_writes(store, row):
    assert row['runtime_dependencies'] and row['current_status']=='ACTIVE'
    assert set(row['replacement_statuses'].values())=={'ACTIVE'}
    assert responder._template_by_id(row['legacy_id'])
    with pytest.raises(ValueError, match='dependency-safe'):
        lifecycle.retire(row['legacy_id'], 'test', store)
    assert not store.list_response_template_retirements()


def test_inactive_override_is_not_retirement(store, safe_fixture):
    store.save_response_template_override(FIXTURE_ID, 'Edited copy', 'test', status='DISABLED')
    assert responder._template_by_id(FIXTURE_ID)['response']=='LEGACY FIXTURE COPY'
    assert not store.list_response_template_retirements()


@pytest.mark.parametrize('status', ['ACTIVE','DISABLED','ARCHIVED'])
def test_retirement_suppresses_shipped_and_overridden_copy(store, safe_fixture, status):
    lifecycle.retire(FIXTURE_ID, 'test', store)
    store.save_response_template_override(FIXTURE_ID, 'Edited copy', 'test', status=status)
    assert responder._template_by_id(FIXTURE_ID)=={}
    assert FIXTURE_ID not in {r['id'] for r in responder._templates_with_overrides()}


def test_retired_entry_audit_visible_and_reversible(store, safe_fixture):
    lifecycle.retire(FIXTURE_ID, 'test', store)
    item=next(x for x in portal._response_library_system_items() if x['template_id']==FIXTURE_ID)
    assert item['status']=='RETIRED' and item['protected_runtime'] and not item['editable']
    assert item['retirement']['replacement_qa_id']=='minimum_sondesh_quantity_client'
    assert 'SYSTEM · RETIRED' in item['source_label']
    lifecycle.restore(FIXTURE_ID, 'test', store)
    assert responder._template_by_id(FIXTURE_ID)['response']=='LEGACY FIXTURE COPY'
    row=store.list_response_template_retirements()[FIXTURE_ID]
    assert not row['retired'] and len(json.loads(row['audit_json']))==2


def test_startup_does_not_resurrect_retired_copy(store, safe_fixture):
    lifecycle.retire(FIXTURE_ID, 'test', store)
    restarted=Database(store.db_path)
    assert FIXTURE_ID not in {t['id'] for t in lifecycle.customer_templates(responder.qa_data['templates'], restarted)}


@pytest.mark.parametrize('field,value', [('runtime_dependencies',['dynamic ID']), ('source_mapping_references',[{'routing_id':1}]), ('special_actions_assets',['asset'])])
def test_dependency_or_action_evidence_blocks_retirement(store, safe_fixture, field, value):
    safe_fixture[field]=value
    with pytest.raises(ValueError, match='dependency-safe'):
        lifecycle.retire(FIXTURE_ID, 'test', store)
    assert not store.list_response_template_retirements()


def test_current_source_dependency_blocks_even_old_safe_decision(store, safe_fixture, monkeypatch):
    monkeypatch.setattr(store, 'list_instagram_source_routes', lambda:[{'template_id':FIXTURE_ID}])
    with pytest.raises(ValueError, match='Current runtime/source'):
        lifecycle.retire(FIXTURE_ID, 'test', store)


def test_dynamic_knowledge_dependency_blocks_old_safe_decision(store, safe_fixture, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr('camellia.knowledge.CATALOGUE', [SimpleNamespace(template_id=FIXTURE_ID)])
    with pytest.raises(ValueError, match='Current runtime/source'):
        lifecycle.retire(FIXTURE_ID, 'test', store)


def test_inactive_canonical_replacement_blocks_retirement(store, safe_fixture, monkeypatch):
    monkeypatch.setattr(client_qa, 'effective_entry', lambda _:None)
    with pytest.raises(ValueError, match='active/effective'):
        lifecycle.retire(FIXTURE_ID, 'test', store)


def test_no_approved_qa_or_custom_rows_changed(store, safe_fixture):
    before=client_qa.effective_registry()
    lifecycle.retire(FIXTURE_ID, 'test', store)
    assert client_qa.effective_registry()==before
    assert store.list_custom_qa(status='ALL')==[]


def test_other_templates_and_combo_bento_unchanged(store, safe_fixture):
    before={t['id']:copy.deepcopy(responder._template_by_id(t['id'])) for t in responder.qa_data['templates'] if t['id']!=FIXTURE_ID}
    lifecycle.retire(FIXTURE_ID, 'test', store)
    assert all(responder._template_by_id(key)==value for key,value in before.items())
    assert 'combo_bento' in before and 'greeting' in before


def test_semantic_router_cannot_return_retired_template(store, safe_fixture, monkeypatch):
    from camellia.config import config
    from camellia.router import shadow_router
    lifecycle.retire(FIXTURE_ID, 'test', store)
    monkeypatch.setattr(config, 'SEMANTIC_ROUTER_ENABLED', True)
    monkeypatch.setattr('camellia.responder._structured_nlu_enabled_for', lambda *a:False)
    monkeypatch.setattr(shadow_router, 'classify', lambda *a:(FIXTURE_ID,'test'))
    assert responder._semantic_fallback_template('isolated fixture words', responder.qa_data['templates'], uuid.uuid4().hex, 'instagram') is None


def test_rag_cannot_retrieve_retired_template(store, safe_fixture, monkeypatch):
    from camellia.rag_fallback import rag_fallback_engine, RAGResult
    monkeypatch.setattr(rag_fallback_engine, 'qa_data', {'templates':[responder._template_by_id(FIXTURE_ID)]})
    monkeypatch.setattr(rag_fallback_engine, 'retrieve_approved_menu', lambda *a:RAGResult())
    lifecycle.retire(FIXTURE_ID, 'test', store)
    result=rag_fallback_engine.retrieve('isolated fixture words', persist_candidate=False)
    assert not result.auto_response and not any(FIXTURE_ID in s for s in result.source_refs)


def test_approved_exact_wording_assets_and_actions_survive_suppressed_legacy(store, safe_fixture, monkeypatch):
    from camellia.config import config
    from camellia.session_memory import session_memory
    # Isolated direct suppression proves canonical authority independently of
    # the current release decision (which permits no real production IDs).
    store._set_response_template_retirement('bento_details','bento_details_client','isolated test','test',True)
    monkeypatch.setattr(config,'BYPASS_AD_GATE',True)
    monkeypatch.setattr('camellia.responder.current_weekly_url', lambda:'/uploads/isolated-current-flyer.jpg')
    entry=client_qa.effective_entry('bento_details_client')
    uid=uuid.uuid4().hex
    reply=responder.match_and_respond('tell me about bento', user_id=uid, channel='instagram')
    facts=session_memory.get_facts(uid,'instagram')
    assert reply[0]==entry.answer and facts['client_qa_id']==entry.qa_id
    assert facts['ordered_response_blocks'][0]['kind']=='TEXT'
    assert reply[3]==['/uploads/isolated-current-flyer.jpg']
    assert facts['ordered_response_blocks'][-1]['urls']==['/uploads/isolated-current-flyer.jpg']
    assert facts['last_actions']==['SEND_RESPONSE','SEND_PRODUCT_IMAGE']


def test_source_selector_excludes_retired_target_and_restores(store, safe_fixture):
    from camellia.instagram_routing import product_catalogue, route_template_id
    store._set_response_template_retirement('bento_details','bento_details_client','isolated test','test',True)
    assert route_template_id('bento') is None
    assert not any(x['template_id']=='bento_details' for x in product_catalogue())
    lifecycle.restore('bento_details','test',store)
    assert route_template_id('bento')=='bento_details'


def test_protected_template_cannot_be_permanently_deleted(store, monkeypatch):
    from fastapi.testclient import TestClient
    from camellia.config import config
    monkeypatch.setattr(config, 'ADMIN_API_KEY','isolated_test')
    response=TestClient(portal.app).delete('/admin/response-library/system/combo_bento',headers={'x-admin-key':'isolated_test'})
    assert response.status_code==405


def test_ai_context_excludes_retired_copy(store, safe_fixture, monkeypatch):
    from camellia.ai_responder import ai_responder
    monkeypatch.setattr(ai_responder,'qa_data',copy.deepcopy(responder.qa_data))
    assert 'LEGACY FIXTURE COPY' in ai_responder._build_context_string()
    lifecycle.retire(FIXTURE_ID,'test',store)
    assert 'LEGACY FIXTURE COPY' not in ai_responder._build_context_string()


def test_dynamic_knowledge_cannot_resolve_retired_copy(store, safe_fixture):
    from camellia.knowledge import approved_knowledge
    from camellia.understanding import UnderstandingResult, Intent
    store._set_response_template_retirement('bento_details','bento_details_client','isolated test','test',True)
    result=approved_knowledge.resolve(UnderstandingResult(available=True,
        intents=[Intent.PRODUCT_INFORMATION.value],entities={'product':'bento'}),responder.qa_data['templates'])
    assert not result.fully_sufficient and not any(x.template_id=='bento_details' for x in result.items)
