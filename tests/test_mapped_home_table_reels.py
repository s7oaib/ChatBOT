"""QA-SM1: exact source copy uses the shared, durable Home Table URL path."""
from importlib import import_module
from unittest.mock import patch

import pytest
from camellia import app as app_module, client_qa, instagram_chatbot_control as control
from camellia.config import config
from camellia.database import Database
from camellia.responder import responder
from camellia.session_memory import SessionMemoryManager

USER = '3141110206001111'
DESCRIPTION = 'Operator opening.\n\n' + client_qa.HOME_TABLE_VIDEO_ANCHOR + '\n\nNext steps to book a seat\nPlease let me know how many guests\nNext steps to book a seat\nPlease let me know how many guests'

@pytest.fixture
def source_store(tmp_path, monkeypatch):
    database = Database(str(tmp_path / 'mapped-home.db'))
    memory = SessionMemoryManager(persistence_enabled=True, database=database)
    monkeypatch.setattr(app_module, 'db', database)
    monkeypatch.setattr(app_module, 'session_memory', memory)
    monkeypatch.setattr(import_module('camellia.responder'), 'session_memory', memory)
    monkeypatch.setattr('camellia.database.db', database)
    monkeypatch.setattr(config, 'BYPASS_AD_GATE', True)
    control.clear_runtime_cache()
    yield database, memory
    control.clear_runtime_cache()


def mapped(source_store, kind='story', topic='Bengali Home Table', text=DESCRIPTION):
    database, memory = source_store
    route = database.save_instagram_source_route({
        'source_type': kind, 'media_id': 'exact-home-' + kind,
        'routing_topic': topic, 'source_description': text, 'active': True,
        'menu_mode': 'NONE', 'priority_products': [], 'knowledge_scope': [],
    })
    memory.get_facts(USER, 'instagram')['source_route'] = route
    return route


def resolve(memory):
    answer, matched, template, _ = responder.match_and_respond('What is this story about?', user_id=USER, channel='instagram')
    assert matched and template == 'source_description'
    return answer, memory.get_facts(USER, 'instagram')['ordered_response_blocks']


@pytest.mark.parametrize('kind', ['story', 'post', 'reel'])
def test_exact_mapped_sources_preserve_text_urls_and_order(source_store, kind):
    database, memory = source_store
    mapped(source_store, kind)
    answer, blocks = resolve(memory)
    assert answer == DESCRIPTION
    assert [b['kind'] for b in blocks] == ['TEXT', 'LINK', 'LINK', 'LINK', 'LINK', 'TEXT']
    assert tuple(b['url'] for b in blocks if b['kind'] == 'LINK') == client_qa.HOME_TABLE_REEL_URLS
    assert ''.join(b['text'] for b in blocks if b['kind'] == 'TEXT') == DESCRIPTION
    assert blocks[0]['text'].endswith(client_qa.HOME_TABLE_VIDEO_ANCHOR)
    assert answer.count('Next steps to book a seat') == 2  # Stored duplicates are authority.


@pytest.mark.parametrize('topic,text', [('Sondesh', DESCRIPTION), ('Bengali Home Table', 'Operator text without video anchor.')])
def test_ineligible_sources_do_not_invent_enrichment(source_store, topic, text):
    _, memory = source_store
    mapped(source_store, topic=topic, text=text)
    memory.get_facts(USER, 'instagram')['ordered_response_blocks'] = [{'kind': 'LINK', 'url': 'stale'}]
    answer, blocks = resolve(memory)
    assert answer == text
    assert blocks == []


def queue_source(database, route, event='mapped-first'):
    job, _ = database.enqueue_instagram_inbound_job(event, USER, '_process_instagram_event', {})
    token = app_module._instagram_job_context.set({'job_id': job, 'sequence': 0})
    try:
        with patch.object(app_module, 'route_for_inbound', return_value=(route, 'story_media_id')), patch.object(
            app_module, 'send_instagram_reply'
        ) as send:
            app_module._process_instagram_event(USER, 'What is this story about?', None)
        send.assert_not_called()
    finally:
        app_module._instagram_job_context.reset(token)
    return job


def test_real_source_send_path_queues_and_completes_durably(source_store):
    database, memory = source_store
    route = mapped(source_store)
    job = queue_source(database, route)
    facts = memory.get_facts(USER, 'instagram')
    assert not facts['home_table_videos_sent']
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id': 'accepted'}) as send:
        app_module._process_instagram_outbound_jobs(inbound_job_id=job)
    end = DESCRIPTION.index(client_qa.HOME_TABLE_VIDEO_ANCHOR) + len(client_qa.HOME_TABLE_VIDEO_ANCHOR)
    assert [c.args[1] for c in send.call_args_list] == [DESCRIPTION[:end], *client_qa.HOME_TABLE_REEL_URLS, DESCRIPTION[end:]]
    assert facts['home_table_videos_sent']
    assert len(database.home_table_reel_receipts(USER, facts['home_table_reel_delivery_id'])) == 4
    answer, blocks = resolve(memory)
    assert answer == DESCRIPTION and not blocks
    queue_source(database, route, 'repeat')
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id': 'text'}) as send:
        app_module._process_instagram_outbound_jobs()
    assert [c.args[1] for c in send.call_args_list] == [DESCRIPTION]


def test_partial_source_completion_retries_missing_only(source_store):
    database, memory = source_store
    route = mapped(source_store)
    queue_source(database, route)
    with patch.object(app_module, 'send_instagram_reply', side_effect=[
        {'message_id': 'text'}, {'message_id': 'one'}, {'message_id': 'two'}, {'status': 'failed'}
    ]):
        app_module._process_instagram_outbound_jobs()
    facts = memory.get_facts(USER, 'instagram')
    assert not facts['home_table_videos_sent']
    assert len(database.home_table_reel_receipts(USER, facts['home_table_reel_delivery_id'])) == 2
    with database.get_connection() as conn:
        conn.execute("UPDATE instagram_outbound_jobs SET next_attempt_at = datetime('now', '-1 second')")
        conn.commit()
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id': 'accepted'}) as send:
        app_module._process_instagram_outbound_jobs()
    assert [c.args[1] for c in send.call_args_list][:-1] == list(client_qa.HOME_TABLE_REEL_URLS[2:])
    assert facts['home_table_videos_sent']


def test_blocked_mapped_source_has_zero_outbound_blocks(source_store):
    database, _ = source_store
    route = mapped(source_store)
    control.set_mode(control.TEST_ONLY, 'operator', database=database)
    queue_source(database, route)
    with database.get_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM instagram_outbound_jobs').fetchone()[0] == 0


def test_builder_without_anchor_returns_original_text():
    assert client_qa.build_home_table_reel_blocks('unchanged') == [{'kind': 'TEXT', 'text': 'unchanged'}]
