"""Explicit Story sets restrict existing authority; they never authorize a context."""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from contextlib import contextmanager
from importlib import import_module
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from camellia import app as app_module, approved_qa, client_qa, instagram_story_qa
from camellia.admin_source_response import SCRIPT, selector_html
from camellia.config import config
from camellia.database import Database
from camellia.instagram_routing import decode_route, route_for_inbound
from camellia.responder import responder
from camellia.session_memory import SessionMemoryManager

PRICE='sondesh_price_client'; DETAILS='sondesh_details_client'; PAY='general_payment_client'


@pytest.fixture
def store(tmp_path, monkeypatch):
    database=Database(str(tmp_path/'multi.db'))
    memory=SessionMemoryManager(persistence_enabled=True,database=database)
    monkeypatch.setattr('camellia.database.db',database)
    monkeypatch.setattr(app_module,'db',database)
    monkeypatch.setattr(import_module('camellia.responder'),'db',database)
    monkeypatch.setattr(import_module('camellia.responder'),'session_memory',memory)
    monkeypatch.setattr(approved_qa,'_database',lambda:database)
    monkeypatch.setattr(approved_qa,'now_utc',lambda:datetime(2026,10,1,tzinfo=timezone.utc))
    for key in ('GROQ_ENABLED','SEMANTIC_ROUTER_ENABLED','STRUCTURED_NLU_ENABLED'):
        monkeypatch.setattr(config,key,False)
    monkeypatch.setattr(config,'BYPASS_AD_GATE',True)
    monkeypatch.setattr(config,'ADMIN_API_KEY','multi-test-key')
    return database,memory,TestClient(app_module.app,base_url='https://testserver')


def mapped(store, story='123', context='sondesh', ids=None):
    database,_,api=store
    response=api.post('/admin/instagram-routes',headers={'X-Admin-Key':'multi-test-key'},json={
        'source_type':'story','media_id':story,'response_mode':'APPROVED_QA',
        'approved_qa_ids':ids if ids is not None else [PRICE,DETAILS,PAY],
        'story_context':context,'menu_mode':'NONE','source_description':'Do not copy into authority.'})
    assert response.status_code==200,response.text
    result=response.json()['route']
    assert result['source_description']==''
    assert database.get_instagram_story_qa_bundle(result['routing_id'])['story_bundle']
    return result


def reply(store, target, message):
    _,memory,_=store
    memory.clear_session('test-customer','instagram')
    if target:memory.get_facts('test-customer','instagram')['source_route']=decode_route(target)
    with patch('camellia.ai_responder.ai_responder._query_groq',side_effect=AssertionError('No generative answer')):
        result=responder.match_and_respond(message,user_id='test-customer',channel='instagram',referral_source='ADS')
    return result,memory.get_facts('test-customer','instagram')


@pytest.mark.parametrize('message,expected', [('price?',PRICE),('tell me more',DETAILS),('How do we pay?',PAY),('Hey, price?',PRICE)])
def test_selected_sondesh_authority_reuses_canonical_response_path(store,message,expected):
    result,facts=reply(store,mapped(store),message)
    assert result[1] and facts['client_qa_id']==expected
    assert facts['source_context_used']
    assert facts['story_bundle_match']=='matched'
    assert client_qa.effective_entry(expected).answer[:100] in result[0]


@pytest.mark.parametrize('context,ids,expected', [
    ('home_table',['home_table_price','home_table_details'],'home_table_price'),
    ('bento',['bento_price_client','bento_details_client'],'bento_price_client')])
def test_same_price_wording_uses_only_exact_story_selected_context(store,context,ids,expected):
    sondesh=mapped(store,'123')
    other=mapped(store,'456',context,ids)
    first,facts=reply(store,sondesh,'price?');assert facts['client_qa_id']==PRICE
    second,facts=reply(store,other,'price?');assert facts['client_qa_id']==expected
    assert first[0]!=second[0]
    exact,method=route_for_inbound(ad_id=None,media_id='456',source_type='story_reply')
    assert method=='story_media_id' and exact['approved_qa_ids']==ids


@pytest.mark.parametrize('message', ['How long does it last?','Do you deliver?','What about this?'])
def test_unselected_source_bound_questions_fail_closed_without_global_leak(store,message):
    result,facts=reply(store,mapped(store),message)
    assert not result[1] and facts['last_route_source']=='HUMAN_HANDOFF'
    assert result[2]=='story_bundle_unresolved' and not facts['client_qa_id']


@pytest.mark.parametrize('message,expected', [('What is the Bengali Home Table price?','home_table_price'),('What is the Bento price?','bento_price_client')])
def test_explicit_named_current_subject_beats_story_set(store,message,expected):
    result,facts=reply(store,mapped(store),message)
    assert result[1] and facts['client_qa_id']==expected


@pytest.mark.parametrize('message', [
    'Price? Can you gift wrap?',
    'Would you be open to a collaboration where our products are featured in your menu?',
    'Do you have 3 places free for Sunday lunch? I can pay tomorrow.'])
def test_existing_whole_turn_safety_precedes_bundle_matching(store,message):
    result,facts=reply(store,mapped(store),message)
    assert not result[1] and facts['last_route_source']=='HUMAN_HANDOFF'
    assert not facts['source_context_used']


@pytest.mark.parametrize('message', ['Hiii','What are your opening hours?'])
def test_independent_global_intents_keep_existing_behavior(store,message):
    plain,_=reply(store,None,message)
    bundled,facts=reply(store,mapped(store),message)
    if message=='Hiii':
        assert plain[1]==bundled[1] and plain[2]==bundled[2]
    else:assert bundled==plain
    assert facts.get('story_bundle_match')!='unresolved'


@pytest.mark.parametrize('field,value', [('status','ARCHIVED'),('valid_until','2020-01-01T00:00:00Z'),('context','bento')])
def test_selected_qa_lifecycle_or_cross_context_mutation_cannot_leak(store,field,value):
    target=mapped(store)
    database,_,_=store
    with database.get_connection() as conn:
        conn.execute("INSERT INTO approved_qa_overrides(qa_id,record_type,created_at,updated_at,created_by,updated_by) VALUES (?,'BASE_OVERRIDE','now','now','test','test')",(PRICE,))
        conn.execute(f'UPDATE approved_qa_overrides SET {field}=? WHERE qa_id=?',(value,PRICE))
    result,facts=reply(store,target,'price?')
    assert not result[1] and result[2]=='story_bundle_unresolved'
    result,facts=reply(store,target,'tell me more')
    assert result[1] and facts['client_qa_id']==DETAILS


def test_all_invalid_selected_ids_fail_closed_even_when_legacy_primary_exists(store):
    target=mapped(store,ids=[PRICE])
    approved_qa.archive_or_restore(PRICE,'test')
    result,facts=reply(store,target,'price?')
    assert not result[1] and facts['last_grounding']=='story_bundle_unresolved'


def add_qa(database,key,context,question,aliases):
    with database.get_connection() as conn:
        conn.execute('''INSERT INTO approved_qa_overrides
            (qa_id,record_type,question,answer,context,aliases_json,created_at,updated_at,created_by,updated_by)
            VALUES (?,'ADMIN_ADDED',?,'Exact approved test answer.',?,?,'now','now','test','test')''',
            (key,question,context,json.dumps(aliases)))


def test_savoury_and_selected_general_aliases_work_without_authorizing_all_context(store):
    database,_,_=store
    add_qa(database,'admin_savoury_price','savoury','Savoury sample price?',['price?'])
    add_qa(database,'admin_general_life','general','General storage guidelines?',['how long does it last?'])
    target=mapped(store,context='savoury',ids=['admin_savoury_price','admin_general_life'])
    result,facts=reply(store,target,'price?');assert result[1] and facts['client_qa_id']=='admin_savoury_price'
    result,facts=reply(store,target,'How long does it last?');assert result[1] and facts['client_qa_id']=='admin_general_life'
    other=mapped(store,'456',ids=[PRICE])
    result,_=reply(store,other,'How long does it last?');assert not result[1]


def test_equal_selected_matches_never_resolve_by_position_or_id(store):
    database,_,_=store
    add_qa(database,'admin_a','sondesh','Alpha details?',['tell me more'])
    add_qa(database,'admin_z','sondesh','Zeta details?',['tell me more'])
    for index,ids in enumerate([['admin_a','admin_z'],['admin_z','admin_a']]):
        target=mapped(store,str(index+123),ids=ids)
        result,facts=reply(store,target,'Tell me more')
        assert not result[1] and result[2]=='story_bundle_unresolved'


@pytest.mark.parametrize('fields', [
    {'approved_qa_ids':[]},{'approved_qa_ids':[PRICE,PRICE]},
    {'approved_qa_ids':['unknown']},{'approved_qa_ids':['home_table_price']},
    {'story_context':'arbitrary'},{'answer':'copied business facts'},
    {'source_type':'post'},{'source_type':'reel'},{'source_type':'ad'}])
def test_api_rejects_invalid_story_sets_without_creating_route(store,fields):
    database,_,api=store
    payload=dict(source_type='story',media_id='123',response_mode='APPROVED_QA',
                 approved_qa_ids=[PRICE],story_context='sondesh',menu_mode='NONE')
    result=api.post('/admin/instagram-routes',headers={'X-Admin-Key':'multi-test-key'},json={**payload,**fields})
    assert result.status_code==400
    assert database.list_instagram_source_routes()==[]
    assert instagram_story_qa.diagnostic(database)['stored_bundles']==0


def test_create_or_update_bundle_late_failure_rolls_back_entire_route(store):
    database,_,api=store
    real=instagram_story_qa.replace
    def fail(*args):real(*args);raise ValueError('injected failure')
    with patch.object(instagram_story_qa,'replace',side_effect=fail):
        response=api.post('/admin/instagram-routes',headers={'X-Admin-Key':'multi-test-key'},json={
            'source_type':'story','media_id':'123','response_mode':'APPROVED_QA',
            'approved_qa_ids':[PRICE,DETAILS],'story_context':'sondesh'})
    assert response.status_code==400 and database.list_instagram_source_routes()==[]
    assert instagram_story_qa.diagnostic(database)['stored_items']==0


def test_legacy_custom_post_reel_and_unmapped_paths_do_not_gain_bundle_authority(store):
    database,_,_=store
    legacy=database.save_instagram_source_route(dict(source_type='story',media_id='legacy',response_mode='APPROVED_QA',approved_qa_id=DETAILS))
    result,facts=reply(store,legacy,'Tell me more')
    assert result[1] and facts['client_qa_id']==DETAILS
    assert not database.get_instagram_story_qa_bundle(legacy['routing_id'])['story_bundle']
    for kind in ('post','reel'):
        other=database.save_instagram_source_route(dict(source_type=kind,media_id=kind,response_mode='APPROVED_QA',approved_qa_id=DETAILS))
        result,facts=reply(store,other,'Tell me more');assert result[1] and facts['client_qa_id']==DETAILS
        assert database.get_instagram_story_qa_bundle(other['routing_id']) is None


def test_custom_switch_clears_bundle_atomically_and_retains_custom_history(store):
    database,_,api=store
    custom=database.save_instagram_source_route(dict(source_type='story',media_id='123',response_mode='CUSTOM',source_description='Exact existing custom text.'))
    headers={'X-Admin-Key':'multi-test-key'}
    response=api.post('/admin/instagram-routes',headers=headers,json=dict(routing_id=custom['routing_id'],response_mode='APPROVED_QA',approved_qa_ids=[PRICE,DETAILS],story_context='sondesh'))
    assert response.status_code==200,response.text
    restored=api.post('/admin/instagram-routes',headers=headers,json=dict(routing_id=custom['routing_id'],response_mode='CUSTOM'))
    assert restored.status_code==200,restored.text
    assert restored.json()['route']['source_description']=='Exact existing custom text.'
    assert restored.json()['route']['approved_qa_id'] is None
    assert not database.get_instagram_story_qa_bundle(custom['routing_id'])['story_bundle']


@pytest.mark.parametrize('operation', ['delete','disable'])
def test_stale_bundle_snapshot_cannot_fall_back_to_legacy_primary(store,operation):
    database,_,_=store
    target=mapped(store)
    if operation=='delete':database.delete_instagram_source_route(target['routing_id'])
    else:database.set_instagram_source_route_active(target['routing_id'],False)
    result,facts=reply(store,target,'Tell me more')
    assert not result[1] and result[2]=='story_bundle_unresolved'
    assert facts['client_qa_id'] is None
    result,facts=reply(store,target,'What is the Bengali Home Table price?')
    assert result[1] and facts['client_qa_id']=='home_table_price'


def test_details_then_unselected_followup_retains_bundle_restriction_and_resets_audit(store):
    target=mapped(store)
    _,memory,_=store
    result,facts=reply(store,target,'Tell me more')
    assert result[1] and facts['source_context_used'] and not facts['source_context_superseded']
    result=responder.match_and_respond('How long does it last?',user_id='test-customer',channel='instagram',referral_source='ADS')
    assert not result[1] and result[2]=='story_bundle_unresolved'
    result=responder.match_and_respond('What are your opening hours?',user_id='test-customer',channel='instagram',referral_source='ADS')
    audit=memory.get_facts('test-customer','instagram').get('story_bundle_match')
    control,_=reply(store,None,'What are your opening hours?')
    assert result==control and audit is None


def test_general_context_does_not_make_unselected_ambiguous_storage_globally_authorized(store):
    database,_,_=store
    add_qa(database,'admin_storage','general','What is the shelf life?',['shelf life?'])
    assert approved_qa.match('What is the shelf life?').entry.qa_id=='admin_storage'
    target=mapped(store)
    result,facts=reply(store,target,'What is the shelf life?')
    assert not result[1] and result[2]=='story_bundle_unresolved' and facts['client_qa_id'] is None
    selected=mapped(store,'456',ids=[PRICE,'admin_storage'])
    result,facts=reply(store,selected,'What is the shelf life?')
    assert result[1] and facts['client_qa_id']=='admin_storage'


def test_custom_switch_late_failure_rolls_back_route_and_bundle(store,monkeypatch):
    database,_,_=store
    target=mapped(store)
    before=database.get_instagram_source_route(target['routing_id'])
    bundle=database.get_instagram_story_qa_bundle(target['routing_id'])
    original=database.get_connection
    class ConnectionProxy:
        def __init__(self,connection):self.connection=connection
        def execute(self,sql,*args):
            if sql.startswith('DELETE FROM instagram_story_qa_bundles'):
                raise ValueError('injected failure after item deletion')
            return self.connection.execute(sql,*args)
        def __getattr__(self,key):return getattr(self.connection,key)
    @contextmanager
    def failing_connection():
        with original() as connection:yield ConnectionProxy(connection)
    with monkeypatch.context() as mutation:
        mutation.setattr(database,'get_connection',failing_connection)
        with pytest.raises(ValueError):
            database.save_instagram_source_route({**before,'response_mode':'CUSTOM','approved_qa_id':None},target['routing_id'])
    assert database.get_instagram_source_route(target['routing_id'])==before
    assert database.get_instagram_story_qa_bundle(target['routing_id'])==bundle


def test_optional_match_restriction_leaves_all_unrestricted_callers_identical(store):
    for entry in approved_qa.registry():
        facts={'home_table_context':True} if entry.context=='home_table' else {}
        assert approved_qa.match(entry.question,facts)==approved_qa.match(entry.question,facts,allowed_qa_ids=None)
    assert approved_qa.match('How do we pay?',allowed_qa_ids=set()) is None


def test_display_topic_cannot_replace_selected_delivery_authority(store):
    database,_,_=store
    target=mapped(store,ids=['sondesh_delivery_time'])
    raw=database.get_instagram_source_route(target['routing_id'])
    database.save_instagram_source_route({**raw,'routing_topic':'Savoury deliveries'},target['routing_id'])
    target=database.get_instagram_source_route(target['routing_id'])
    result,facts=reply(store,target,'delivery time')
    assert result[1] and facts['client_qa_id']=='sondesh_delivery_time'
    assert client_qa.effective_entry('sondesh_delivery_time').answer==result[0]
    assert facts['source_context_used'] and facts['story_bundle_match']=='matched'


def test_story_checklist_executed_filter_search_selection_payload_and_safe_labels():
    node=shutil.which('node');assert node
    script=SCRIPT.split('<script>',1)[1].split('</script>',1)[0]
    harness=r'''
const elements={};let mode='APPROVED_QA';let requests=0;
function element(tag='div'){return {tag,value:'',textContent:'',style:{},children:[],hidden:false,replaceChildren(...kids){this.children=kids},append(...kids){this.children.push(...kids)},closest(){return {toggleAttribute(){}}}}}
const document={getElementById(id){return elements[id] ||= element()},createElement:element,createTextNode(text){return {textContent:text}},querySelector(){return {value:mode,checked:true}}};
const Option=function(text,value){return {textContent:text,value}};
const getAuthHeaders=()=>({});
const data={contexts:[{value:'general',label:'General'},{value:'sondesh',label:'Sondesh'},{value:'bento',label:'Bento'}],records:[
{qa_id:'price',question:'Price <unsafe>',context:'sondesh',answer:'Approved price.'},
{qa_id:'details',question:'Details?',context:'sondesh',answer:'Approved details.'},
{qa_id:'payment',question:'Payment?',context:'general',answer:'Approved payment.'},
{qa_id:'other',question:'Other product?',context:'bento',answer:'Other answer.'}]};
const fetch=async()=>{requests++;return {ok:true,json:async()=>data}};
function text(node){return String(node.textContent||'')+(node.children||[]).map(text).join('')}
function inputs(node){return [...(node.tag==='input'?[node]:[]),...(node.children||[]).flatMap(inputs)]}
'''
    check=r'''
(async()=>{
 await initSourceResponse('igStory',{response_mode:'APPROVED_QA',approved_qa_id:'price'});
 if(requests!==1||sourceResponseState.igStory.context!=='sondesh')throw Error('legacy read');
 let box=document.getElementById('igStoryQaChecklist');let checkboxes=inputs(box);
 if(checkboxes.find(c=>c.value==='payment').checked)throw Error('General auto-selected');
 if(checkboxes.find(c=>c.value==='other'))throw Error('cross context');
 if(!checkboxes.find(c=>c.value==='price').checked)throw Error('legacy not checked');
 if(!text(box).includes('General — select explicitly')||!text(box).includes('Price <unsafe>'))throw Error('groups/labels');
 const payment=checkboxes.find(c=>c.value==='payment');payment.checked=true;payment.onchange();
 if(document.getElementById('igStoryQaCount').textContent!=='Selected: 2')throw Error('count');
 document.getElementById('igStoryQaSearch').value='details';filterSourceQa('igStory');
 if(inputs(box).length!==1||inputs(box)[0].value!=='details')throw Error('search');
 const payload=sourceResponseFields('igStory');
 if(JSON.stringify(payload.approved_qa_ids)!=='["price","payment"]'||payload.story_context!=='sondesh'||JSON.stringify(payload).includes('Approved price'))throw Error('IDs only');
 document.getElementById('igStoryQaContext').value='bento';changeStoryQaContext();
 if(!text(box).includes('unavailable/expired or context changed'))throw Error('unavailable hidden');
 if(requests!==1)throw Error('unexpected write');
 mode='CUSTOM';if('approved_qa_ids' in sourceResponseFields('igStory'))throw Error('Custom set leak');
 console.log('PASS');
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result=subprocess.run([node,'-e',harness+script+check],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert 'igStoryQaChecklist' in selector_html('igStory')
    assert 'igQaChecklist' in selector_html('ig') and 'igMediaQaChecklist' in selector_html('igMedia')
