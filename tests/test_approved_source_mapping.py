"""QA-MAP1: effective QA references reuse canonical routing and durable delivery."""
from datetime import datetime, timezone
from importlib import import_module
from unittest.mock import patch
import sqlite3

import pytest
from fastapi.testclient import TestClient

from camellia import app as app_module, approved_qa, client_qa, instagram_chatbot_control as control
from camellia.config import config
from camellia.database import Database
from camellia.instagram_routing import decode_route
from camellia.responder import responder
from camellia.session_memory import SessionMemoryManager

USER = '3141110206009991'

@pytest.fixture
def store(tmp_path, monkeypatch):
    database = Database(str(tmp_path / 'qa-mapping.db'))
    memory = SessionMemoryManager(persistence_enabled=True, database=database)
    monkeypatch.setattr(app_module, 'db', database)
    monkeypatch.setattr(app_module, 'session_memory', memory)
    monkeypatch.setattr(import_module('camellia.responder'), 'db', database)
    monkeypatch.setattr(import_module('camellia.responder'), 'session_memory', memory)
    monkeypatch.setattr('camellia.database.db', database)
    monkeypatch.setattr(approved_qa, '_database', lambda: database)
    monkeypatch.setattr(approved_qa, 'now_utc', lambda: datetime(2026, 10, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(config, 'BYPASS_AD_GATE', True)
    monkeypatch.setattr(config, 'ADMIN_API_KEY', 'qa-mapping-test-key')
    control.clear_runtime_cache()
    yield database, memory, TestClient(app_module.app), {'X-Admin-Key': config.ADMIN_API_KEY}
    control.clear_runtime_cache()


def create(store, kind='story', qa_id='home_table_details', **fields):
    _, _, client, headers = store
    response = client.post('/admin/instagram-routes', headers=headers, json={
        'source_type': kind, 'media_id': 'exact-mapped-' + kind,
        'response_mode': 'APPROVED_QA', 'approved_qa_id': qa_id,
        'source_description': 'Untrusted copied answer must not be persisted.',
        'menu_mode': 'NONE', **fields,
    })
    assert response.status_code == 200, response.text
    assert response.json()['route']['response_mode'] == 'APPROVED_QA', response.json()
    return response.json()['route']


def resolve(store, message='Tell me more about this', route=None):
    _, memory, _, _ = store
    if route:
        memory.get_facts(USER, 'instagram')['source_route'] = route
    result = responder.match_and_respond(message, user_id=USER, channel='instagram')
    return result, memory.get_facts(USER, 'instagram')


@pytest.mark.parametrize('kind', ['story', 'post', 'reel'])
@pytest.mark.parametrize('message', ['Tell me more about this', 'What is this?', "What's this about?", 'More details please', 'Tell me about this post', 'Tell me more'])
def test_selected_id_uses_canonical_blocks_and_exact_answer(store, kind, message):
    route = create(store, kind)
    with patch.object(client_qa, 'match', side_effect=AssertionError('Mapped selection must resolve by ID')):
        result, facts = resolve(store, message, route)
    assert result[0] == client_qa.effective_entry('home_table_details').answer
    assert facts['client_qa_id'] == 'home_table_details'
    assert facts['client_qa_match_kind'] == 'source_qa_id'
    blocks = facts['ordered_response_blocks']
    assert [b['kind'] for b in blocks] == ['TEXT', 'LINK', 'LINK', 'LINK', 'LINK', 'TEXT']
    assert tuple(b['url'] for b in blocks if b['kind'] == 'LINK') == client_qa.HOME_TABLE_REEL_URLS
    assert ''.join(b['text'] for b in blocks if b['kind'] == 'TEXT') == result[0]
    assert route['source_description'] == ''


@pytest.mark.parametrize('qa_id', [entry.qa_id for entry in client_qa.REGISTRY])
def test_every_current_effective_qa_uses_existing_actions_and_assets(store, qa_id, eligible_weekly_asset):
    route = create(store, qa_id=qa_id)
    result, facts = resolve(store, route=route)
    expected = client_qa.effective_entry(qa_id).answer
    if qa_id in {'order_prebook', 'alacarte_prebook'}:
        assert result[0].startswith(expected + '\n\nContinue on WhatsApp:')
        assert result[0].count('https://wa.me/') == 1
    else:
        assert result[0] == expected, facts.get('client_qa_id')
    assert facts['client_qa_id'] == qa_id
    assert facts['client_qa_asset_status'] == 'available'
    assert facts['client_qa_handoff_exact'] is (qa_id == 'order_ready_handoff')
    assert result[1] is (qa_id != 'order_ready_handoff')
    assert set(client_qa.effective_entry(qa_id).asset_keys) <= {
        b.get('asset_key') for b in facts['ordered_response_blocks']
    }


@pytest.mark.parametrize('qa_id,followup,expected', [
    ('home_table_details', "What's the price?", 'home_table_price'),
    ('sondesh_details_client', "What's the price?", 'sondesh_price_client'),
    ('subscription_details_client', "What's the price?", 'subscription_price'),
    ('home_table_details', 'How much is chicken kosha?', 'chicken_kosha_price_client'),
])
def test_explicit_and_contextual_intent_overrides_mapping(store, qa_id, followup, expected, eligible_weekly_asset):
    resolve(store, route=create(store, qa_id=qa_id))
    _, facts = resolve(store, followup)
    assert facts['client_qa_id'] == expected
    if expected == 'home_table_price':
        _, facts = resolve(store, 'How do I pay?')
        assert facts['client_qa_id'] == 'home_table_payment'
        assert not any(b['kind'] == 'LINK' for b in facts['ordered_response_blocks'])


def publish(qa_id=None, **fields):
    body = {**fields, **({'qa_id': qa_id} if qa_id else {})}
    preview = approved_qa.preview(body)
    return approved_qa.publish({**body, 'qa_id': preview['qa_id'], 'preview_token': preview['preview_token']}, 'test-admin')


def test_dynamic_selector_and_latest_published_override(store):
    _, _, client, headers = store
    extra = publish(question='A new approved question?', answer='Current effective new answer.')
    records = client.get('/admin/instagram-routes/approved-qa-options', headers=headers).json()['records']
    assert {r['qa_id'] for r in records} == {e.qa_id for e in client_qa.effective_registry()}
    assert extra['qa_id'] in {r['qa_id'] for r in records}
    home = next(r for r in records if r['qa_id'] == 'home_table_details')
    assert [b['url'] for b in home['ordered_blocks'] if b['kind'] == 'LINK'] == list(client_qa.HOME_TABLE_REEL_URLS)
    route = create(store, qa_id=extra['qa_id'])
    assert resolve(store, route=route)[0][0] == extra['answer']
    updated = publish(extra['qa_id'], answer='Updated effective answer, no copied text.')
    assert resolve(store)[0][0] == updated['answer']
    assert store[0].get_instagram_source_route(route['routing_id'])['source_description'] == ''


@pytest.mark.parametrize('lifecycle', ['ARCHIVED', 'EXPIRED', 'SCHEDULED', 'INVALID', 'UNKNOWN'])
def test_new_save_rejects_unavailable_and_existing_mapping_fails_closed(store, lifecycle):
    _, memory, client, headers = store
    route = create(store, qa_id='bento_details_client')
    if lifecycle == 'UNKNOWN':
        route['approved_qa_id'] = 'unknown-qa'
    elif lifecycle == 'ARCHIVED':
        approved_qa.archive_or_restore('bento_details_client', 'test-admin')
    elif lifecycle == 'INVALID':
        with store[0].get_connection() as conn:
            conn.execute("UPDATE approved_qa_overrides SET valid_until='invalid' WHERE qa_id='bento_details_client'")
    else:
        publish('bento_details_client', **({'valid_until': '2026-09-30T00:00:00+05:30'} if lifecycle == 'EXPIRED' else {'valid_from': '2026-10-05T00:00:00+05:30'}))
    response = client.post('/admin/instagram-routes', headers=headers, json=route)
    assert response.status_code == 400
    route['source_description'] = 'Stale custom text must not be a fallback.'
    result, facts = resolve(store, route=route)
    assert not result[1] and result[2] == 'approved_qa_unavailable'
    assert result[0] not in {route['source_description'], client_qa.REGISTRY_BY_ID['bento_details_client'].answer}
    assert facts['ordered_response_blocks'] == []
    if lifecycle != 'INVALID':
        _, facts = resolve(store, 'Tell me about Bengali Home Table')
        assert facts['client_qa_id'] == 'home_table_details'


@pytest.mark.parametrize('qa_id', [None, '', 'does-not-exist'])
def test_server_rejects_missing_or_unknown_reference(store, qa_id):
    _, _, client, headers = store
    r = client.post('/admin/instagram-routes', headers=headers, json={
        'source_type': 'story', 'media_id': 'known-story', 'response_mode': 'APPROVED_QA', 'approved_qa_id': qa_id})
    assert r.status_code == 400


def test_custom_switches_preserve_operator_copy_and_clear_hidden_qa(store):
    database, _, client, headers = store
    original = client.post('/admin/instagram-routes', headers=headers, json={
        'source_type': 'story', 'media_id': 'existing-story', 'routing_topic': 'Original topic',
        'source_description': 'Original custom wording.\n\nSecond paragraph.', 'menu_mode': 'NONE'}).json()['route']
    assert original['response_mode'] == 'CUSTOM' and original['approved_qa_id'] is None
    switched = client.post('/admin/instagram-routes', headers=headers, json={
        'routing_id': original['routing_id'], 'response_mode': 'APPROVED_QA',
        'approved_qa_id': 'home_table_details', 'source_description': 'Do not overwrite custom history.'}).json()['route']
    assert switched['source_description'] == original['source_description']
    assert switched['response_mode'] == 'APPROVED_QA'
    restored = client.post('/admin/instagram-routes', headers=headers, json={
        'routing_id': original['routing_id'], 'response_mode': 'CUSTOM', 'approved_qa_id': 'unknown-stale-id'}).json()['route']
    assert restored['approved_qa_id'] is None and restored['source_description'] == original['source_description']
    assert restored['routing_topic'] == original['routing_topic']
    assert database.get_instagram_source_route(original['routing_id'])['response_mode'] == 'CUSTOM'


def test_additive_migration_idempotent_and_preserves_existing_content(tmp_path):
    path = tmp_path / 'old.db'
    database = Database(str(path))
    original = database.save_instagram_source_route({'source_type':'story','media_id':'old-story','source_description':'Untouched custom text.'})
    with sqlite3.connect(path) as conn:
        conn.execute('ALTER TABLE instagram_source_routes DROP COLUMN approved_qa_id')
        conn.execute('ALTER TABLE instagram_source_routes DROP COLUMN response_mode')
    database.init_db(); database.init_db()
    restored = database.get_instagram_source_route(original['routing_id'])
    assert restored['response_mode'] == 'CUSTOM' and restored['approved_qa_id'] is None
    assert restored['source_description'] == original['source_description']
    assert restored['updated_at'] == original['updated_at']


def queue(store, route, mid):
    database, memory, _, _ = store
    class Deferred:
        def add_task(self, *args, **kwargs):
            pass
    tasks = app_module._DurableInstagramTasks(Deferred())
    tasks.set_event('business', {'sender':{'id':USER}, 'message':{'mid':mid,'text':'Tell me more about this'}})
    with patch.object(app_module, 'route_for_inbound', return_value=(route,'story_media_id')):
        tasks.add_task(app_module._process_instagram_event, USER, 'Tell me more about this', None)
        with patch.object(app_module, 'send_instagram_reply', return_value={'message_id':'accepted'}) as send:
            app_module._process_instagram_inbound_jobs()
    return send, memory.get_facts(USER,'instagram')


def test_mapped_qa_new_requests_and_same_mid_replay_keep_sm11_delivery(store):
    database, _, _, _ = store
    route = create(store)
    generations=[]
    for index, mid in enumerate(['first-mid','first-mid','new-mid']):
        send, facts=queue(store,route,mid)
        assert send.call_count == (0 if index==1 else 6)
        generations.append(facts['home_table_reel_delivery_id'])
        assert len(database.home_table_reel_receipts(USER,generations[-1]))==4
    assert generations[0]==generations[1]!=generations[2]


def test_test_only_guard_prevents_all_business_processing(store):
    database, _, _, _ = store
    route=create(store)
    control.set_mode(control.TEST_ONLY,'operator',database=database)
    with patch.object(responder,'match_and_respond') as match:
        send,_=queue(store,route,'blocked-mid')
    match.assert_not_called();send.assert_not_called()
    assert database.get_conversations_count(channel='instagram',user_id=USER)==0
    control.add_test_account(sender_id=USER,actor='operator',database=database)
    assert queue(store,route,'allowed-mid')[0].call_count==6


def test_mapped_order_ready_handoff_uses_actual_human_escalation(store):
    database, _, _, _=store
    route=create(store,qa_id='order_ready_handoff')
    with patch.object(app_module.escalator,'handle_escalation') as escalate:
        send,facts=queue(store,route,'handoff-mid')
    assert facts['client_qa_handoff_exact']
    assert facts['client_qa_id']=='order_ready_handoff'
    assert send.called and escalate.called
    assert database.get_conversations_count(channel='instagram',user_id=USER)==1


def test_mapping_endpoints_require_admin_and_preview_is_read_only(store):
    _, _, client, headers=store
    assert client.get('/admin/instagram-routes/approved-qa-options').status_code in {401,403}
    assert client.post('/admin/instagram-routes',json={'response_mode':'APPROVED_QA'}).status_code in {401,403}
    page=client.get('/admin',headers=headers).text
    for prefix in ['ig','igStory']:
        assert 'name="'+prefix+'ResponseMode"' in page
        assert 'id="'+prefix+'QaSearch"' in page
        assert 'id="'+prefix+'ApprovedQaId"' in page
    assert "sourceResponseHtml('igMedia')" in page
    assert 'Read-only preview sourced from Approved' in page
    assert 'answer.textContent=record.answer' in page
    assert 'record.source_aliases' in page and 'record.qa_id' in page


@pytest.mark.parametrize('media_type', ['IMAGE', 'VIDEO'])
def test_gallery_approved_mapping_edit_and_custom_restore(store, media_type):
    database, _, client, headers = store
    media = database.upsert_instagram_media({'instagram_media_id':'gallery-'+media_type,'media_type':media_type})
    path = f"/admin/instagram-media/{media['id']}/mapping"
    response = client.put(path,headers=headers,json={'response_mode':'APPROVED_QA','approved_qa_id':'home_table_details','menu_mode':'NONE'})
    assert response.status_code==200,response.text
    route=response.json()['route']
    assert route['approved_qa_id']=='home_table_details' and route['source_description']==''
    edited=client.put(path,headers=headers,json={'response_mode':'APPROVED_QA','approved_qa_id':'home_table_payment','menu_mode':'NONE'})
    assert edited.status_code==200,edited.text
    assert edited.json()['route']['approved_qa_id']=='home_table_payment'
    topic=client.post('/admin/instagram-topics',headers=headers,json={'name':'Gallery custom','description':'Existing topic'}).json()['topic']
    restored=client.put(path,headers=headers,json={'response_mode':'CUSTOM','approved_qa_id':'stale','topic_id':topic['topic_id'],'source_description':'Custom gallery answer.','menu_mode':'NONE'})
    assert restored.status_code==200,restored.text
    assert restored.json()['route']['approved_qa_id'] is None
    switched=client.put(path,headers=headers,json={'response_mode':'APPROVED_QA','approved_qa_id':'home_table_details','menu_mode':'NONE'})
    assert switched.status_code==200,switched.text
    assert switched.json()['route']['source_description']=='Custom gallery answer.'
