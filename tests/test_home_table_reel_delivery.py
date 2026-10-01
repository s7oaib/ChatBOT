"""QA-M1.1: delivery receipts, not enqueue success, complete Home Table reels."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from camellia import app as app_module, client_qa, instagram_chatbot_control as control
from camellia.config import config
from camellia.database import Database
from camellia.session_memory import SessionMemoryManager

URLS = client_qa.HOME_TABLE_REEL_URLS
USER = '3141110206000000'
BLOCKS = [{'kind': 'TEXT', 'text': 'approved before'}] + [
    {'kind': 'LINK', 'url': url} for url in URLS
] + [{'kind': 'TEXT', 'text': 'approved after'}]


@pytest.fixture
def store(tmp_path, monkeypatch):
    database = Database(str(tmp_path / 'reel-delivery.db'))
    memory = SessionMemoryManager(persistence_enabled=True, database=database)
    monkeypatch.setattr(app_module, 'db', database)
    monkeypatch.setattr(app_module, 'session_memory', memory)
    monkeypatch.setattr(config, 'ADMIN_API_KEY', 'reel-admin-key')
    control.clear_runtime_cache()
    yield database, memory
    control.clear_runtime_cache()


def enqueue(database, blocks=BLOCKS, event='first'):
    job_id, _ = database.enqueue_instagram_inbound_job(event, USER, '_process_instagram_event', {})
    token = app_module._instagram_job_context.set({'job_id': job_id, 'sequence': 0})
    try:
        with patch.object(app_module, 'send_instagram_reply') as send:
            app_module.send_instagram_response_blocks(USER, blocks, inbound_message_id=123)
        send.assert_not_called()
    finally:
        app_module._instagram_job_context.reset(token)
    return job_id


def step(database, result=None):
    original = database.claim_instagram_outbound_jobs
    once = [False]
    def claim(**kwargs):
        if once[0]:
            return []
        once[0] = True
        return original(**kwargs)
    with patch.object(database, 'claim_instagram_outbound_jobs', side_effect=claim), patch.object(
        app_module, 'send_instagram_reply', return_value=result or {'message_id': 'accepted'}
    ) as send:
        app_module._process_instagram_outbound_jobs()
    return send


def receipts(database, memory):
    delivery_id = memory.get_facts(USER, 'instagram')['home_table_reel_delivery_id']
    return database.home_table_reel_receipts(USER, delivery_id)


def test_enqueue_and_each_actual_completion_preserve_order(store):
    database, memory = store
    enqueue(database)
    facts = memory.get_facts(USER, 'instagram')
    assert facts['home_table_videos_sent'] is False
    with database.get_connection() as conn:
        import json
        jobs = conn.execute('SELECT * FROM instagram_outbound_jobs ORDER BY id').fetchall()
        metadata = [json.loads(job['payload']).get('_response_delivery') for job in jobs][1:5]
    assert [d['family'] for d in metadata] == ['HOME_TABLE_REELS'] * 4
    assert [d['link_index'] for d in metadata] == [1, 2, 3, 4]
    assert [d['url'] for d in metadata] == list(URLS)
    sent = []
    for index in range(6):
        send = step(database)
        sent.append(send.call_args.args[1])
        assert facts['home_table_videos_sent'] is (index >= 4)
        assert len(receipts(database, memory)) == min(max(index, 0), 4)
    assert sent == ['approved before', *URLS, 'approved after']
    assert all(r['inbound_message_id'] == 123 for r in receipts(database, memory))


def test_partial_failure_restart_and_retry_only_missing_links(store, monkeypatch):
    database, memory = store
    enqueue(database)
    for _ in range(3):
        step(database)  # text and first two links
    step(database, {'status': 'failed', 'delivery_status': 'RETRYABLE_FAILURE'})
    assert len(receipts(database, memory)) == 2
    assert not memory.get_facts(USER, 'instagram')['home_table_videos_sent']
    restarted = SessionMemoryManager(persistence_enabled=True, database=database)
    monkeypatch.setattr(app_module, 'session_memory', restarted)
    assert len(receipts(database, restarted)) == 2
    with database.get_connection() as conn:
        conn.execute("UPDATE instagram_outbound_jobs SET next_attempt_at = datetime('now', '-1 second')")
        conn.commit()
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id': 'retry-ok'}) as send:
        app_module._process_instagram_outbound_jobs()
    assert [call.args[1] for call in send.call_args_list] == [*URLS[2:], 'approved after']
    assert restarted.get_facts(USER, 'instagram')['home_table_videos_sent']
    enqueue(database, event='followup')
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id': 'text'}) as send:
        app_module._process_instagram_outbound_jobs()
    assert [c.args[1] for c in send.call_args_list] == ['approved before', *URLS, 'approved after']


def test_terminal_failure_does_not_strand_new_full_delivery(store):
    database, memory = store
    enqueue(database)
    for _ in range(3):
        step(database)
    step(database, {'status': 'failed', 'delivery_status': 'PERMANENT_RECIPIENT_FAILURE'})
    assert not memory.get_facts(USER, 'instagram')['home_table_videos_sent']
    assert len(receipts(database, memory)) == 2
    enqueue(database, event='customer-retry')
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id': 'ok'}) as send:
        app_module._process_instagram_outbound_jobs()
    assert [c.args[1] for c in send.call_args_list] == ['approved before', *URLS, 'approved after']
    assert memory.get_facts(USER, 'instagram')['home_table_videos_sent']


def test_blocked_queued_links_are_never_receipts(store):
    database, memory = store
    control.add_test_account(sender_id=USER, actor='operator', database=database)
    control.set_mode(control.TEST_ONLY, 'operator', database=database)
    enqueue(database)
    control.remove_test_account(USER, 'operator', database=database)
    with patch.object(app_module, 'send_instagram_reply') as send:
        app_module._process_instagram_outbound_jobs()
    send.assert_not_called()
    assert not receipts(database, memory)
    assert not memory.get_facts(USER, 'instagram')['home_table_videos_sent']


def test_duplicate_queued_answers_do_not_resend_confirmed_urls(store):
    database, memory = store
    enqueue(database)
    enqueue(database, event='first')
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id': 'ok'}) as send:
        app_module._process_instagram_outbound_jobs()
    urls = [c.args[1] for c in send.call_args_list if c.args[1] in URLS]
    assert urls == list(URLS)
    assert memory.get_facts(USER, 'instagram')['home_table_videos_sent']


@pytest.mark.parametrize('result', [{'status': 'queued'}, {'status': 'blocked'}, {'status': 'no_content'}, {'status': 'failed', 'message_id': 'untrusted'}, None])
def test_sync_without_meta_acceptance_never_completes(store, result):
    database, memory = store
    with patch.object(app_module, 'send_instagram_reply', return_value=result):
        app_module.send_instagram_response_blocks(USER, BLOCKS)
    assert not receipts(database, memory)
    assert not memory.get_facts(USER, 'instagram')['home_table_videos_sent']


def test_expired_session_cannot_be_completed_by_old_jobs(store):
    database, memory = store
    enqueue(database)
    memory.clear_session(USER, 'instagram')
    new = memory.get_facts(USER, 'instagram')
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id': 'ok'}):
        app_module._process_instagram_outbound_jobs()
    assert not new['home_table_videos_sent']


def test_scoped_legacy_repair_authenticated_one_time_and_audited(store):
    database, memory = store
    control.set_mode(control.TEST_ONLY, 'operator', database=database)
    control.add_test_account(sender_id=USER, actor='operator', username='test_user', database=database)
    facts = memory.get_facts(USER, 'instagram')
    facts['home_table_videos_sent'] = True
    memory._persist(USER, 'instagram')
    client = TestClient(app_module.app)
    path = '/admin/home-table-reel-delivery/repair'
    assert client.post(path, json={'recipient_id': USER}).status_code in {401, 403}
    headers = {'X-Admin-Key': config.ADMIN_API_KEY, 'X-Admin-Pin': config.ADMIN_API_KEY}
    response = client.post(path, headers=headers, json={'recipient_id': USER})
    assert response.status_code == 200, response.text
    assert response.json()['repaired']
    assert client.post(path, headers=headers, json={'recipient_id': USER}).status_code == 409
    state = client.get('/admin/home-table-reel-delivery', headers=headers, params={'recipient_id': USER}).json()
    assert state['home_table_videos_sent'] is False
    assert state['repair']['previous_value'] is True
    assert state['delivery_id']
    assert USER not in str(state)
    assert control.audit_log(database=database)[0]['action'] == 'HOME_TABLE_REEL_COMPLETION_REPAIRED'
    assert control.get_state(database=database)['mode'] == control.TEST_ONLY


@pytest.mark.parametrize('mode,allowed,inflight', [
    (control.LIVE, True, False), (control.TEST_ONLY, False, False),
    (control.TEST_ONLY, True, True),
])
def test_legacy_repair_refuses_live_blocked_or_inflight(store, mode, allowed, inflight):
    database, memory = store
    if allowed:
        control.add_test_account(sender_id=USER, actor='operator', database=database)
    control.set_mode(mode, 'operator', database=database)
    facts = memory.get_facts(USER, 'instagram')
    facts['home_table_videos_sent'] = True
    memory._persist(USER, 'instagram')
    if inflight:
        database.enqueue_instagram_outbound_job(100, 1, USER, {'text': URLS[0]})
    response = TestClient(app_module.app).post(
        '/admin/home-table-reel-delivery/repair', json={'recipient_id': USER},
        headers={'X-Admin-Key': config.ADMIN_API_KEY, 'X-Admin-Pin': config.ADMIN_API_KEY},
    )
    assert response.status_code == 409
    assert database.load_active_session(USER, 'instagram')['facts']['home_table_videos_sent']
    assert not any(a['action'] == 'HOME_TABLE_REEL_COMPLETION_REPAIRED' for a in control.audit_log(database=database))


def test_customer_erasure_deletes_delivery_receipts(store):
    database, memory = store
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id': 'ok'}):
        app_module.send_instagram_response_blocks(USER, BLOCKS)
    assert len(receipts(database, memory)) == 4
    database.delete_user_data(USER)
    assert not receipts(database, memory)


def test_independent_requests_retain_both_receipt_sets(store):
    database, memory = store
    generations = []
    for event in ['new-one', 'new-two']:
        enqueue(database, event=event)
        generation = memory.get_facts(USER, 'instagram')['home_table_reel_delivery_id']
        generations.append(generation)
        with patch.object(app_module, 'send_instagram_reply', return_value={'message_id': event}) as send:
            app_module._process_instagram_outbound_jobs()
        assert [c.args[1] for c in send.call_args_list] == ['approved before', *URLS, 'approved after']
        assert len(database.home_table_reel_receipts(USER, generation)) == 4
    assert generations[0] != generations[1]
    assert len(database.home_table_reel_receipts(USER, generations[0])) == 4


def test_same_inbound_retry_after_partial_restart_keeps_queue_positions(store, monkeypatch):
    database, memory = store
    enqueue(database)
    generation = memory.get_facts(USER, 'instagram')['home_table_reel_delivery_id']
    for _ in range(3):
        step(database)
    restarted = SessionMemoryManager(persistence_enabled=True, database=database)
    monkeypatch.setattr(app_module, 'session_memory', restarted)
    enqueue(database)
    assert restarted.get_facts(USER, 'instagram')['home_table_reel_delivery_id'] == generation
    with database.get_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM instagram_outbound_jobs').fetchone()[0] == 6
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id': 'ok'}) as send:
        app_module._process_instagram_outbound_jobs()
    assert [c.args[1] for c in send.call_args_list] == [*URLS[2:], 'approved after']
    assert len(database.home_table_reel_receipts(USER, generation)) == 4


def test_same_meta_event_replay_and_new_mid(store, monkeypatch):
    database, memory = store
    class Deferred:
        def add_task(self, *args, **kwargs):
            pass
    tasks = app_module._DurableInstagramTasks(Deferred())
    def _process_instagram_event(sender, text, ad_id):
        app_module.send_instagram_response_blocks(sender, BLOCKS)
    monkeypatch.setattr(app_module, '_process_instagram_event', _process_instagram_event)
    generations = []
    for index, mid in enumerate(['meta-one', 'meta-one', 'meta-two']):
        tasks.set_event('business', {'sender': {'id': USER}, 'message': {'mid': mid, 'text': 'Home Table'}})
        tasks.add_task(_process_instagram_event, USER, 'Home Table', None)
        with patch.object(app_module, 'send_instagram_reply', return_value={'message_id': mid}) as send:
            app_module._process_instagram_inbound_jobs()
        assert send.call_count == (0 if index == 1 else 6)
        generations.append(memory.get_facts(USER, 'instagram')['home_table_reel_delivery_id'])
    assert generations[0] == generations[1] != generations[2]
    assert database.instagram_job_counts()['inbound'] == {'DONE': 2}
