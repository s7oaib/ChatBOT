"""Additive references, transaction rollback and exact route lifecycle."""
import sqlite3
from unittest.mock import patch

import pytest

from camellia import approved_qa, instagram_story_qa
from camellia.database import Database

PRICE='sondesh_price_client'
DETAILS='sondesh_details_client'
GENERAL='general_payment_client'


@pytest.fixture
def store(tmp_path, monkeypatch):
    database=Database(str(tmp_path/'bundles.db'))
    monkeypatch.setattr(approved_qa, '_database', lambda: database)
    return database


def route(store, story='123', mode='APPROVED_QA', qa=PRICE, source='story'):
    return store.save_instagram_source_route(dict(source_type=source, media_id=story,
        response_mode=mode, approved_qa_id=qa if mode=='APPROVED_QA' else None,
        source_description='Existing exact custom copy.', active=True))


def save(store, target, ids=None, context='sondesh', story=None):
    return store.replace_instagram_story_qa_bundle(target['routing_id'], story or target['media_id'], context,
        [PRICE,DETAILS] if ids is None else ids, 'test-admin')


@pytest.mark.parametrize('mode,qa,expected', [('APPROVED_QA',PRICE,[PRICE]),('CUSTOM',None,[]),('APPROVED_QA',None,[])])
def test_schema_and_read_compatibility_never_backfill_or_rewrite(store, mode, qa, expected):
    target=route(store,mode=mode,qa=qa)
    before=store.get_instagram_source_route(target['routing_id'])
    store.init_db()
    result=store.get_instagram_story_qa_bundle(target['routing_id'])
    assert result['approved_qa_ids']==expected and not result['story_bundle']
    assert store.get_instagram_source_route(target['routing_id'])==before
    assert instagram_story_qa.diagnostic(store)==dict(schema_initialized=True,stored_bundles=0,stored_items=0)


def test_bundle_replacement_preserves_primary_identity_and_restart_durability(store):
    target=route(store)
    result=save(store,target,[DETAILS,PRICE,GENERAL])
    assert result['approved_qa_ids']==[DETAILS,PRICE,GENERAL]
    assert result['approved_qa_id']==PRICE and result['story_context']=='sondesh'
    before=store.get_instagram_source_route(target['routing_id'])
    restarted=Database(store.db_path)
    assert restarted.get_instagram_story_qa_bundle(target['routing_id'])==result
    assert restarted.get_instagram_source_route(target['routing_id'])==before
    result=save(store,target,[DETAILS,GENERAL])
    assert result['approved_qa_id']==DETAILS
    after=store.get_instagram_source_route(target['routing_id'])
    assert {key:value for key,value in after.items() if key!='approved_qa_id'}=={key:value for key,value in before.items() if key!='approved_qa_id'}


@pytest.mark.parametrize('ids,context', [
    ([PRICE,PRICE],'sondesh'),(['unknown'],'sondesh'),([], 'sondesh'),
    ([PRICE]*51,'sondesh'),([{'answer':'copied'}],'sondesh'),
    ([PRICE],'arbitrary'),(['home_table_price'],'sondesh')])
def test_invalid_sets_roll_back_previous_bundle_and_compatibility_primary(store, ids, context):
    target=route(store);before=save(store,target)
    raw=store.get_instagram_source_route(target['routing_id'])
    with pytest.raises(ValueError):save(store,target,ids,context)
    assert store.get_instagram_story_qa_bundle(target['routing_id'])==before
    assert store.get_instagram_source_route(target['routing_id'])==raw


@pytest.mark.parametrize('fields', [{'status':'ARCHIVED'},{'valid_until':'2020-01-01T00:00:00Z'},{'valid_from':'2099-01-01T00:00:00Z'}])
def test_ineffective_qa_rejected_on_save(store, fields):
    target=route(store)
    with store.get_connection() as conn:
        conn.execute("INSERT INTO approved_qa_overrides(qa_id,record_type,created_at,updated_at,created_by,updated_by) VALUES (?,'BASE_OVERRIDE','now','now','test','test')",(PRICE,))
        for key,value in fields.items():conn.execute(f'UPDATE approved_qa_overrides SET {key}=? WHERE qa_id=?',(value,PRICE))
    with pytest.raises(ValueError):save(store,target)
    assert not store.get_instagram_story_qa_bundle(target['routing_id'])['story_bundle']


def test_general_must_be_explicit_and_nonstory_or_changed_identity_rejected(store):
    target=route(store)
    assert GENERAL not in save(store,target)['approved_qa_ids']
    assert GENERAL in save(store,target,[PRICE,GENERAL])['approved_qa_ids']
    with pytest.raises(ValueError):save(store,target,story='different')
    other=route(store,story='other',source='post')
    with pytest.raises(ValueError):save(store,other)
    assert store.get_instagram_story_qa_bundle(other['routing_id']) is None
    with pytest.raises(ValueError):store.replace_instagram_story_qa_bundle(999,'123','sondesh',[PRICE],'test')


def test_late_transaction_failure_restores_both_items_and_parent(store):
    target=route(store);before=save(store,target)
    real=instagram_story_qa.replace
    def failing(*args):
        real(*args)
        raise RuntimeError('injected late failure')
    with patch.object(instagram_story_qa,'replace',side_effect=failing),pytest.raises(RuntimeError):
        save(store,target,[DETAILS])
    assert store.get_instagram_story_qa_bundle(target['routing_id'])==before
    assert store.get_instagram_source_route(target['routing_id'])['approved_qa_id']==PRICE


def test_mapping_deletion_and_custom_switch_cleanup_only_exact_bundle(store):
    first=route(store);second=route(store,story='456');other=route(store,story='post',source='post')
    save(store,first);save(store,second)
    store.delete_instagram_source_route(other['routing_id'])
    assert instagram_story_qa.diagnostic(store)['stored_bundles']==2
    store.delete_instagram_source_route(first['routing_id'])
    assert instagram_story_qa.diagnostic(store)['stored_bundles']==1
    assert store.get_instagram_story_qa_bundle(second['routing_id'])['story_bundle']
    store.save_instagram_source_route({**second,'response_mode':'CUSTOM','approved_qa_id':None},second['routing_id'])
    assert instagram_story_qa.diagnostic(store)['stored_items']==0


def test_promotion_keeps_bundle_new_story_does_not_inherit_and_identity_guard(store):
    store.record_instagram_story_source('123')
    target=route(store);before=save(store,target)
    store.upsert_instagram_story_sync('123',media_type='IMAGE',event_metadata={'source':'owned'})
    assert store.get_instagram_story_qa_bundle(target['routing_id'])==before
    new=route(store,story='456')
    assert not store.get_instagram_story_qa_bundle(new['routing_id'])['story_bundle']
    with pytest.raises(sqlite3.IntegrityError):
        store.save_instagram_source_route({**target,'media_id':'changed'},target['routing_id'])
    assert store.get_instagram_story_qa_bundle(target['routing_id'])==before
    store.delete_instagram_story_source('123')
    assert instagram_story_qa.diagnostic(store)['stored_bundles']==0


def test_owned_removal_lifecycle_cleans_bundle_without_ttl_changes(store):
    store.upsert_instagram_story_sync('123',published_at='2020-01-01T00:00:00Z')
    target=route(store);save(store,target)
    store.reconcile_instagram_story_lifecycle([],expired_before='2026-01-01T00:00:00Z')
    store.reconcile_instagram_story_lifecycle([],expired_before='2026-01-01T00:00:00Z')
    assert instagram_story_qa.diagnostic(store)['stored_items']==0
