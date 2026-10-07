"""Post/Reel additive storage isolation, lifecycle, and rollback."""
import sqlite3
from unittest.mock import patch
import pytest
from camellia import approved_qa, instagram_source_qa as bundles, instagram_story_qa
from camellia.database import Database

PRICE='sondesh_price_client'; DETAILS='sondesh_details_client'; GENERAL='general_payment_client'


@pytest.fixture
def store(tmp_path, monkeypatch):
    database=Database(str(tmp_path/'source-bundles.db'))
    monkeypatch.setattr(approved_qa,'_database',lambda:database)
    return database


def route(store, source='post', key='123', mode='APPROVED_QA', qa=PRICE):
    return store.save_instagram_source_route(dict(source_type=source,media_id=key,
        canonical_url=None if source=='story' else 'https://www.instagram.com/p/'+key+'/',
        shortcode=None if source=='story' else key,response_mode=mode,
        approved_qa_id=qa if mode=='APPROVED_QA' else None,
        source_description='Retained custom copy.',active=True))


def save(store,target,ids=None,context='sondesh',exact=None):
    return store.replace_instagram_source_qa_bundle(target['routing_id'],
        bundles.identity(target) if exact is None else exact,context,
        [PRICE,DETAILS] if ids is None else ids,'test-admin')


@pytest.mark.parametrize('source',['post','reel'])
@pytest.mark.parametrize('mode,qa,expected',[('APPROVED_QA',PRICE,[PRICE]),('CUSTOM',None,[]),('APPROVED_QA',None,[])])
def test_initialization_and_virtual_reads_never_backfill(store,source,mode,qa,expected):
    target=route(store,source,mode=mode,qa=qa);before=store.get_instagram_source_route(target['routing_id'])
    store.init_db()
    result=store.get_instagram_source_qa_bundle(target['routing_id'])
    assert result['approved_qa_ids']==expected and not result['qa_bundle']
    assert store.get_instagram_source_route(target['routing_id'])==before
    assert bundles.diagnostic(store)==dict(schema_initialized=True,stored_bundles=0,stored_items=0)


@pytest.mark.parametrize('source',['post','reel'])
def test_explicit_bundle_primary_and_restart(store,source):
    target=route(store,source);result=save(store,target,[DETAILS,PRICE,GENERAL])
    assert result==dict(qa_bundle=True,source_context='sondesh',approved_qa_ids=[DETAILS,PRICE,GENERAL],approved_qa_id=PRICE)
    before=store.get_instagram_source_route(target['routing_id'])
    restarted=Database(store.db_path)
    assert restarted.get_instagram_source_qa_bundle(target['routing_id'])==result
    assert restarted.get_instagram_source_route(target['routing_id'])==before
    assert save(store,target,[DETAILS,GENERAL])['approved_qa_id']==DETAILS
    after=store.get_instagram_source_route(target['routing_id'])
    assert {k:v for k,v in before.items() if k!='approved_qa_id'}=={k:v for k,v in after.items() if k!='approved_qa_id'}


@pytest.mark.parametrize('ids,context',[
    ([PRICE,PRICE],'sondesh'),(['unknown'],'sondesh'),([],'sondesh'),([PRICE]*51,'sondesh'),
    ([{'answer':'copied'}],'sondesh'),([PRICE],'arbitrary'),(['home_table_price'],'sondesh')])
def test_invalid_replacement_preserves_parent_items_and_primary(store,ids,context):
    target=route(store);before=save(store,target);raw=store.get_instagram_source_route(target['routing_id'])
    with pytest.raises(ValueError):save(store,target,ids,context)
    assert store.get_instagram_source_qa_bundle(target['routing_id'])==before
    assert store.get_instagram_source_route(target['routing_id'])==raw


@pytest.mark.parametrize('field,value',[('status','ARCHIVED'),('valid_until','2020-01-01T00:00:00Z'),('valid_from','2099-01-01T00:00:00Z')])
def test_ineffective_save_rejected(store,field,value):
    target=route(store)
    with store.get_connection() as conn:
        conn.execute("INSERT INTO approved_qa_overrides(qa_id,record_type,created_at,updated_at,created_by,updated_by) VALUES (?,'BASE_OVERRIDE','now','now','test','test')",(PRICE,))
        conn.execute(f'UPDATE approved_qa_overrides SET {field}=? WHERE qa_id=?',(value,PRICE))
    with pytest.raises(ValueError):save(store,target)
    assert not store.get_instagram_source_qa_bundle(target['routing_id'])['qa_bundle']


@pytest.mark.parametrize('source',['story','ad'])
def test_other_source_types_rejected_without_touching_story_store(store,source):
    target=route(store,source)
    if source=='story':
        store.replace_instagram_story_qa_bundle(target['routing_id'],target['media_id'],'sondesh',[PRICE,DETAILS],'test')
    before=instagram_story_qa.diagnostic(store)
    with pytest.raises(ValueError):save(store,target)
    assert store.get_instagram_source_qa_bundle(target['routing_id']) is None
    store.init_db()
    assert instagram_story_qa.diagnostic(store)==before
    if source=='story':assert store.get_instagram_story_qa_bundle(target['routing_id'])['approved_qa_ids']==[PRICE,DETAILS]


@pytest.mark.parametrize('field',['source_type','media_id','canonical_url','shortcode'])
def test_exact_identity_cannot_change_or_move_to_wrong_route(store,field):
    target=route(store);before=save(store,target)
    with pytest.raises(ValueError):save(store,target,exact={**bundles.identity(target),field:'wrong'})
    with pytest.raises(sqlite3.IntegrityError):store.save_instagram_source_route({**target,field:'wrong'},target['routing_id'])
    other=route(store,'reel','456')
    with pytest.raises(ValueError):store.replace_instagram_source_qa_bundle(other['routing_id'],bundles.identity(target),'sondesh',[PRICE],'test')
    assert store.get_instagram_source_qa_bundle(target['routing_id'])==before
    assert not store.get_instagram_source_qa_bundle(other['routing_id'])['qa_bundle']


def test_late_failure_and_custom_switch_transaction(store):
    target=route(store);before=save(store,target);raw=store.get_instagram_source_route(target['routing_id'])
    real=bundles.replace
    def fail(*args):real(*args);raise RuntimeError('late failure')
    with patch.object(bundles,'replace',side_effect=fail),pytest.raises(RuntimeError):save(store,target,[DETAILS])
    assert store.get_instagram_source_qa_bundle(target['routing_id'])==before
    real_clear=bundles.clear
    def fail_clear(*args):real_clear(*args);raise RuntimeError('late cleanup failure')
    with patch.object(bundles,'clear',side_effect=fail_clear),pytest.raises(RuntimeError):
        store.save_instagram_source_route({**raw,'response_mode':'CUSTOM','approved_qa_id':None},target['routing_id'])
    assert store.get_instagram_source_qa_bundle(target['routing_id'])==before
    assert store.get_instagram_source_route(target['routing_id'])==raw
    result=store.save_instagram_source_route({**raw,'response_mode':'CUSTOM','approved_qa_id':None},target['routing_id'])
    assert result['source_description']==raw['source_description'] and result['approved_qa_id'] is None
    assert bundles.diagnostic(store)['stored_items']==0


def test_general_explicit_archive_restore_and_deletion_isolation(store):
    post=route(store);reel=route(store,'reel','456');story=route(store,'story','789')
    assert GENERAL not in save(store,post)['approved_qa_ids']
    post_bundle=save(store,post,[PRICE,GENERAL]);reel_bundle=save(store,reel)
    story_bundle=store.replace_instagram_story_qa_bundle(story['routing_id'],'789','sondesh',[PRICE,DETAILS],'test')
    store.archive_instagram_source_route(post['routing_id'])
    with pytest.raises(ValueError):save(store,post)
    assert store.get_instagram_source_qa_bundle(post['routing_id'])==post_bundle
    store.restore_instagram_source_route(post['routing_id'])
    assert store.get_instagram_source_qa_bundle(post['routing_id'])==post_bundle
    store.delete_instagram_source_route(post['routing_id'])
    assert store.get_instagram_source_qa_bundle(reel['routing_id'])==reel_bundle
    assert store.get_instagram_story_qa_bundle(story['routing_id'])==story_bundle
    store.delete_instagram_source_route(reel['routing_id'])
    assert bundles.diagnostic(store)['stored_items']==0
    assert store.get_instagram_story_qa_bundle(story['routing_id'])==story_bundle
    store.delete_instagram_source_route(story['routing_id'])
    assert instagram_story_qa.diagnostic(store)['stored_items']==0


def test_manual_exact_permalink_without_media_id_and_effective_context_rule(store):
    target=store.save_instagram_source_route(dict(source_type='post',media_id=None,canonical_url='https://www.instagram.com/p/Exact/',shortcode='Exact',response_mode='APPROVED_QA',approved_qa_id=PRICE,active=True))
    bundle=save(store,target,[PRICE,GENERAL])
    rows,allowed=bundles.effective(store,bundle)
    assert allowed=={PRICE,GENERAL} and isinstance(rows,dict)
    with store.get_connection() as conn:
        conn.execute("INSERT INTO approved_qa_overrides(qa_id,record_type,context,created_at,updated_at,created_by,updated_by) VALUES (?,'BASE_OVERRIDE','bento','now','now','test','test')",(PRICE,))
    assert bundles.effective(store,bundle)[1]=={GENERAL}


def test_existing_verified_shared_post_pairing_keeps_exact_route_bundle(store):
    target=store.save_instagram_source_route(dict(source_type='post',media_id=None,
        canonical_url='https://www.instagram.com/p/Exact/',shortcode='Exact',
        response_mode='APPROVED_QA',approved_qa_id=PRICE,active=True))
    before=save(store,target)
    pairing=store.start_post_pairing(target['routing_id'],600,'test-admin')
    bound=store.bind_post_pairing(pairing['id'],'verified-shared-post-id')
    assert bound['status']=='bound' and bound['bound_media_id']=='verified-shared-post-id'
    current=store.get_instagram_source_route(target['routing_id'])
    assert current['media_id']=='verified-shared-post-id'
    assert current['canonical_url']==target['canonical_url'] and current['shortcode']==target['shortcode']
    assert store.get_instagram_source_qa_bundle(target['routing_id'])==before
    with pytest.raises(sqlite3.IntegrityError):
        store.save_instagram_source_route({**current,'media_id':'unverified-other-id'},target['routing_id'])


@pytest.mark.parametrize('source,media_type',[('post','IMAGE'),('reel','VIDEO')])
def test_existing_owned_media_promotion_preserves_bundle_and_exact_link(store,source,media_type):
    target=store.save_instagram_source_route(dict(source_type=source,media_id=None,
        canonical_url='https://www.instagram.com/p/Exact/',shortcode='Exact',
        response_mode='APPROVED_QA',approved_qa_id=PRICE,active=True))
    before=save(store,target)
    owned={**target,'media_id':'180123456789'}
    with store.get_connection() as conn:
        assert not bundles.compatible_identity(conn,target,owned)
    store.apply_instagram_media_inventory([dict(instagram_media_id='180123456789',
        permalink=target['canonical_url'],shortcode='Exact',media_type=media_type,
        owner_instagram_account_id='28206314108997745',inventory_state='CURRENT',
        last_sync_generation='verified')],'28206314108997745','verified')
    with store.get_connection() as conn:
        assert bundles.compatible_identity(conn,target,owned)
        assert not bundles.compatible_identity(conn,target,{**owned,'canonical_url':'https://www.instagram.com/p/Other/'})
    updated=store.save_instagram_source_route(owned,target['routing_id'])
    assert updated['media_id']=='180123456789' and updated['routing_id']==target['routing_id']
    assert store.get_instagram_source_qa_bundle(target['routing_id'])==before
