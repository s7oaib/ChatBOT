"""Exact Post/Reel bundles reuse the accepted Story candidate restrictions."""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from importlib import import_module
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from camellia import app as app_module, approved_qa, client_qa, instagram_source_qa, instagram_story_qa
from camellia.admin_source_response import SCRIPT, selector_html
from camellia.config import config
from camellia.database import Database
from camellia.instagram_routing import decode_route, route_for_inbound
from camellia.responder import responder
from camellia.session_memory import SessionMemoryManager
from camellia.instagram_ads import mapping_display

PRICE='sondesh_price_client'; DETAILS='sondesh_details_client'; PAY='general_payment_client'
HEADERS={'X-Admin-Key':'source-multi-test-key'}


@pytest.fixture
def store(tmp_path,monkeypatch):
    database=Database(str(tmp_path/'source-multi.db'))
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
    monkeypatch.setattr(config,'ADMIN_API_KEY','source-multi-test-key')
    monkeypatch.setattr(config,'INSTAGRAM_GRAPH_ACCOUNT_ID','28206314108997745')
    return database,memory,TestClient(app_module.app,base_url='https://testserver')


def mapped(store,source='reel',key='Exact123',context='sondesh',ids=None):
    database,_,api=store
    response=api.post('/admin/instagram-routes',headers=HEADERS,json=dict(
        source_type=source,url=f'https://www.instagram.com/{"reel" if source=="reel" else "p"}/{key}/',
        media_id=key,response_mode='APPROVED_QA',approved_qa_ids=[PRICE,DETAILS,PAY] if ids is None else ids,
        source_context=context,menu_mode='NONE'))
    assert response.status_code==200,response.text
    target=response.json()['route']
    assert target['qa_bundle'] and database.get_instagram_source_qa_bundle(target['routing_id'])['approved_qa_ids']==target['approved_qa_ids']
    return target


def reply(store,target,message,clear=True):
    _,memory,_=store
    if clear:memory.clear_session('source-test-customer','instagram')
    if target:memory.get_facts('source-test-customer','instagram')['source_route']=decode_route(target)
    with patch('camellia.ai_responder.ai_responder._query_groq',side_effect=AssertionError('No generative answer')):
        result=responder.match_and_respond(message,user_id='source-test-customer',channel='instagram',referral_source='ADS')
    return result,memory.get_facts('source-test-customer','instagram')


def add_qa(database,key,context,question,aliases):
    with database.get_connection() as conn:
        conn.execute('''INSERT INTO approved_qa_overrides
            (qa_id,record_type,question,answer,context,aliases_json,created_at,updated_at,created_by,updated_by)
            VALUES (?,'ADMIN_ADDED',?,'Exact approved test answer.',?,?,'now','now','test','test')''',
            (key,question,context,json.dumps(aliases)))


@pytest.mark.parametrize('source',['post','reel'])
@pytest.mark.parametrize('message,expected',[('price?',PRICE),('tell me more',DETAILS),('How do we pay?',PAY),('Hey, price?',PRICE)])
def test_selected_authority_uses_existing_canonical_output(store,source,message,expected):
    result,facts=reply(store,mapped(store,source),message)
    assert result[1] and facts['client_qa_id']==expected and facts['source_context_used']
    assert facts['source_bundle_match']=='matched' and 'story_bundle_match' not in facts
    assert client_qa.effective_entry(expected).answer[:100] in result[0]


@pytest.mark.parametrize('source',['post','reel'])
def test_exact_ad_inherits_post_or_reel_qa_bundle(store,source):
    database,_,_=store
    response=TestClient(app_module.app).post('/admin/instagram-routes',headers=HEADERS,json=dict(
        source_type=source,url=f'https://www.instagram.com/{"reel" if source=="reel" else "p"}/AdBundle123/',
        media_id='AdBundle123',response_mode='APPROVED_QA',approved_qa_ids=[PRICE,DETAILS,PAY],
        source_context='sondesh',menu_mode='NONE'))
    assert response.status_code==200,response.text
    target=response.json()['route']
    ad_id=f'exact-{source}-bundle-ad'
    database.bind_instagram_ad(ad_id,target['routing_id'],bound_by='test')

    inherited,method=route_for_inbound(ad_id=ad_id,media_id=None)
    assert method=='ad_id' and inherited['routing_id']==target['routing_id']
    assert inherited['source_type']==source and inherited['source_context']=='sondesh'
    assert inherited['approved_qa_ids']==[PRICE,DETAILS,PAY]
    assert not database.get_instagram_source_qa_bundle(target['routing_id'])['approved_qa_ids'] is None

    display=mapping_display(database.get_instagram_source_route(target['routing_id']))
    assert 'approved_opening_message' not in inherited
    assert 'display_opening_message' not in display
    assert [item['qa_id'] for item in display['display_approved_questions']]==[PRICE,DETAILS,PAY]
    assert all(item['answer']==client_qa.effective_entry(item['qa_id']).answer for item in display['display_approved_questions'])


@pytest.mark.parametrize('qa_id',['general_price','general_details','price_is_high','best_time_to_book','general_payment_client'])
def test_each_requested_selected_qa_id_returns_its_existing_canonical_answer(store,qa_id):
    entry=client_qa.effective_entry(qa_id)
    assert entry is not None
    context=entry.context if entry.context!='general' else 'sondesh'
    target=mapped(store,'post',f'Canonical{qa_id}',context=context,ids=[qa_id])

    result,facts=reply(store,target,entry.question)

    assert result[1] and facts['client_qa_id']==qa_id
    assert entry.answer in result[0]
    assert facts['source_bundle_match']=='matched'


def test_ads_follow_source_configuration_updates_without_copied_per_ad_state(store):
    database,_,api=store
    target=mapped(store,'reel','LiveConfig123',ids=[PRICE])
    database.bind_instagram_ad('live-config-ad',target['routing_id'],bound_by='test')
    response=api.post('/admin/instagram-routes',headers=HEADERS,json=dict(
        routing_id=target['routing_id'],source_type='reel',url='https://www.instagram.com/reel/LiveConfig123/',
        response_mode='APPROVED_QA',approved_qa_ids=[DETAILS,PAY],source_context='sondesh',
        menu_mode='NONE'))
    assert response.status_code==200,response.text

    inherited,method=route_for_inbound(ad_id='live-config-ad',media_id=None)
    display=mapping_display(database.get_instagram_source_route(target['routing_id']))
    assert method=='ad_id' and inherited['approved_qa_ids']==[DETAILS,PAY]
    assert [item['qa_id'] for item in display['display_approved_questions']]==[DETAILS,PAY]


def test_legacy_opening_storage_is_preserved_but_not_exposed_or_used(store):
    database,_,api=store
    target=mapped(store,'reel','LegacyOpening123',ids=[PRICE])
    with database.get_connection() as conn:
        conn.execute('UPDATE instagram_source_routes SET approved_opening_message=? WHERE routing_id=?',
                     ('legacy data stays stored',target['routing_id']))
    response=api.post('/admin/instagram-routes',headers=HEADERS,json=dict(
        routing_id=target['routing_id'],source_type='reel',url='https://www.instagram.com/reel/LegacyOpening123/',
        response_mode='APPROVED_QA',approved_qa_ids=[DETAILS],source_context='sondesh',menu_mode='NONE'))
    assert response.status_code==200,response.text
    stored=database.get_instagram_source_route(target['routing_id'])
    assert stored['approved_opening_message']=='legacy data stays stored'
    assert 'approved_opening_message' not in response.json()['route']
    assert 'approved_opening_message' not in route_for_inbound(ad_id=None,media_id=None,shortcode='LegacyOpening123')[0]
    assert 'display_opening_message' not in mapping_display(stored)


def test_opening_message_controls_and_special_template_are_absent(store):
    from camellia.admin_ads import SCRIPT as ADS_SCRIPT
    from camellia.app import _process_instagram_event
    import inspect
    source_script=SCRIPT
    assert 'OpeningMessage' not in selector_html('ig')
    assert 'approved_opening_message' not in source_script
    assert 'display_opening_message' not in ADS_SCRIPT
    assert 'source_opening_message' not in inspect.getsource(_process_instagram_event)


def test_same_wording_different_exact_sources_and_selection_precedence(store):
    reel=mapped(store);post=mapped(store,'post','Other123','home_table',['home_table_price','home_table_details'])
    first,facts=reply(store,reel,'price?');assert facts['client_qa_id']==PRICE
    second,facts=reply(store,post,'price?');assert facts['client_qa_id']=='home_table_price' and first[0]!=second[0]
    exact,method=route_for_inbound(ad_id=None,media_id=None,shortcode='Exact123',source_type='reel')
    assert method=='shared_media_url' and exact['routing_id']==reel['routing_id'] and exact['qa_bundle']
    exact,method=route_for_inbound(ad_id=None,media_id=None,post_media_id='Other123',source_type='post')
    assert method=='shared_post_media_id' and exact['routing_id']==post['routing_id'] and exact['approved_qa_ids']==['home_table_price','home_table_details']


@pytest.mark.parametrize('source',['post','reel'])
@pytest.mark.parametrize('message',['How long does it last?','Do you deliver?','What about this?'])
def test_unselected_source_bound_qa_cannot_escape_to_global_matching(store,source,message):
    result,facts=reply(store,mapped(store,source),message)
    assert not result[1] and result[2]=='source_bundle_unresolved'
    assert facts['last_route_source']=='HUMAN_HANDOFF' and facts['client_qa_id'] is None


@pytest.mark.parametrize('message,expected',[('What is the Bengali Home Table price?','home_table_price'),('What is the Bento price?','bento_price_client')])
def test_explicit_different_product_wins(store,message,expected):
    result,facts=reply(store,mapped(store),message)
    assert result[1] and facts['client_qa_id']==expected


@pytest.mark.parametrize('message',['Hiii','What are your opening hours?',
    'Price? Can you gift wrap?',
    'Would you be open to a collaboration where our products are featured in your menu?',
    'Do you have 3 places free for Sunday lunch? I can pay tomorrow.'])
def test_existing_independent_and_whole_turn_behavior_remains_first(store,message):
    plain,_=reply(store,None,message)
    bundled,facts=reply(store,mapped(store),message)
    if message=='Hiii':assert plain[1:]==bundled[1:]
    else:assert bundled==plain
    assert facts.get('source_bundle_match')!='unresolved'


@pytest.mark.parametrize('field,value',[('status','ARCHIVED'),('valid_until','2020-01-01T00:00:00Z'),('context','bento')])
def test_expiry_archive_context_change_excluded_with_remaining_valid_match(store,field,value):
    target=mapped(store);database,_,_=store
    with database.get_connection() as conn:
        conn.execute("INSERT INTO approved_qa_overrides(qa_id,record_type,created_at,updated_at,created_by,updated_by) VALUES (?,'BASE_OVERRIDE','now','now','test','test')",(PRICE,))
        conn.execute(f'UPDATE approved_qa_overrides SET {field}=? WHERE qa_id=?',(value,PRICE))
    result,_=reply(store,target,'price?');assert not result[1] and result[2]=='source_bundle_unresolved'
    result,facts=reply(store,target,'tell me more');assert result[1] and facts['client_qa_id']==DETAILS


def test_all_invalid_and_unselected_general_fail_closed(store):
    database,_,_=store
    add_qa(database,'source_storage','general','What is the shelf life?',['shelf life?'])
    target=mapped(store,ids=[PRICE])
    result,_=reply(store,target,'What is the shelf life?');assert not result[1]
    selected=mapped(store,'post','Other123',ids=[PRICE,'source_storage'])
    result,facts=reply(store,selected,'What is the shelf life?');assert result[1] and facts['client_qa_id']=='source_storage'
    approved_qa.archive_or_restore(PRICE,'test')
    result,_=reply(store,target,'price?');assert not result[1] and result[2]=='source_bundle_unresolved'


def test_equal_selected_authorities_never_use_checkbox_or_db_order(store):
    database,_,_=store
    add_qa(database,'source_a','sondesh','Alpha details?',['tell me more'])
    add_qa(database,'source_z','sondesh','Zeta details?',['tell me more'])
    for index,ids in enumerate([['source_a','source_z'],['source_z','source_a']]):
        target=mapped(store,key='Exact'+str(index),ids=ids)
        result,_=reply(store,target,'Tell me more')
        assert not result[1] and result[2]=='source_bundle_unresolved'


@pytest.mark.parametrize('operation',['delete','disable','archive'])
def test_stale_bundle_cannot_fall_back_to_compatibility_primary(store,operation):
    database,_,_=store;target=mapped(store)
    if operation=='delete':database.delete_instagram_source_route(target['routing_id'])
    elif operation=='disable':database.set_instagram_source_route_active(target['routing_id'],False)
    else:database.archive_instagram_source_route(target['routing_id'])
    result,_=reply(store,target,'Tell me more');assert not result[1] and result[2]=='source_bundle_unresolved'
    result,facts=reply(store,target,'What is the Bengali Home Table price?');assert result[1] and facts['client_qa_id']=='home_table_price'


def test_consecutive_turn_restriction_and_independent_audit_reset(store):
    target=mapped(store)
    result,facts=reply(store,target,'Tell me more');assert result[1] and not facts['source_context_superseded']
    result,_=reply(store,None,'How long does it last?',clear=False);assert not result[1] and result[2]=='source_bundle_unresolved'
    result,facts=reply(store,None,'What are your opening hours?',clear=False)
    assert 'source_bundle_match' not in facts
    control,_=reply(store,None,'What are your opening hours?');assert result==control


@pytest.mark.parametrize('source',['post','reel'])
def test_legacy_and_custom_sources_keep_existing_outputs_without_backfill(store,source):
    database,_,_=store
    target=database.save_instagram_source_route(dict(source_type=source,media_id=source,canonical_url='https://www.instagram.com/p/Legacy/',shortcode='Legacy',response_mode='APPROVED_QA',approved_qa_id=DETAILS))
    result,facts=reply(store,target,'Tell me more')
    assert result[1] and facts['client_qa_id']==DETAILS and 'source_bundle_match' not in facts
    assert not database.get_instagram_source_qa_bundle(target['routing_id'])['qa_bundle']
    custom=database.save_instagram_source_route(dict(source_type=source,media_id='custom-'+source,response_mode='CUSTOM',source_description='Existing custom response.',active=True))
    before,_=reply(store,custom,'Tell me more')
    database.init_db();after,_=reply(store,custom,'Tell me more')
    assert after==before and instagram_source_qa.diagnostic(database)['stored_bundles']==0


@pytest.mark.parametrize('fields',[{'approved_qa_ids':[]},{'approved_qa_ids':[PRICE,PRICE]},
    {'approved_qa_ids':['unknown']},{'approved_qa_ids':['home_table_price']},
    {'source_context':'arbitrary'},{'source_context':None,'story_context':'sondesh'},
    {'answer':'copied'},{'source_type':'ad'},{'response_mode':'CUSTOM'}])
def test_api_invalid_sets_rejected_without_mutation(store,fields):
    database,_,api=store
    payload=dict(source_type='reel',url='https://www.instagram.com/reel/Exact123/',media_id='Exact123',response_mode='APPROVED_QA',approved_qa_ids=[PRICE],source_context='sondesh',menu_mode='NONE')
    response=api.post('/admin/instagram-routes',headers=HEADERS,json={**payload,**fields})
    assert response.status_code==400 and database.list_instagram_source_routes()==[]
    assert instagram_source_qa.diagnostic(database)['stored_items']==0


def test_wrong_route_identity_and_late_failure_roll_back_all_route_bundle_fields(store):
    database,_,api=store;target=mapped(store)
    before=database.get_instagram_source_route(target['routing_id']);bundle=database.get_instagram_source_qa_bundle(target['routing_id'])
    response=api.post('/admin/instagram-routes',headers=HEADERS,json=dict(routing_id=target['routing_id'],media_id='Other',approved_qa_ids=[DETAILS],source_context='sondesh'))
    assert response.status_code==400
    real=instagram_source_qa.replace
    def fail(*args):real(*args);raise ValueError('injected late failure')
    with patch.object(instagram_source_qa,'replace',side_effect=fail):
        response=api.post('/admin/instagram-routes',headers=HEADERS,json=dict(routing_id=target['routing_id'],approved_qa_ids=[DETAILS],source_context='sondesh'))
    assert response.status_code==400
    assert database.get_instagram_source_route(target['routing_id'])==before
    assert database.get_instagram_source_qa_bundle(target['routing_id'])==bundle
    with patch.object(instagram_source_qa,'replace',side_effect=fail):
        response=api.post('/admin/instagram-routes',headers=HEADERS,json=dict(source_type='post',url='https://www.instagram.com/p/New123/',response_mode='APPROVED_QA',approved_qa_ids=[PRICE],source_context='sondesh'))
    assert response.status_code==400 and len(database.list_instagram_source_routes())==1


@pytest.mark.parametrize('source,media_type',[('post','IMAGE'),('reel','VIDEO')])
@pytest.mark.parametrize('manual',[False,True])
def test_owned_and_manual_catalogue_mapping_api_supports_bundle_and_read_contract(store,source,media_type,manual):
    database,_,api=store
    url=f'https://www.instagram.com/{"reel" if source=="reel" else "p"}/Catalogue123/'
    media=dict(instagram_media_id='180123456789' if not manual else 'manual:'+source+':Catalogue123',permalink=url,shortcode='Catalogue123',media_type=media_type)
    if manual:
        item=database.upsert_instagram_media({**media,'sync_status':'MANUAL_EXACT_LINK','inventory_state':'COLLABORATOR_UNVERIFIED'})
    else:
        database.apply_instagram_media_inventory([{**media,'owner_instagram_account_id':'28206314108997745','inventory_state':'CURRENT','last_sync_generation':'test-owned'}],'28206314108997745','test-owned')
        item=database.get_instagram_media_by_meta_id(media['instagram_media_id'])
    payload=dict(response_mode='APPROVED_QA',approved_qa_ids=[PRICE,DETAILS,PAY],source_context='sondesh',menu_mode='NONE')
    response=api.put(f"/admin/instagram-media/{item['id']}/mapping",headers=HEADERS,json=payload)
    assert response.status_code==200,response.text
    target=response.json()['route'];assert target['approved_qa_ids']==[PRICE,DETAILS,PAY] and target['qa_bundle']
    assert target['source_type']==source and target['media_id']==(None if manual else media['instagram_media_id'])
    listing=api.get('/admin/instagram-media?page_size=100',headers=HEADERS).json()['items']
    assert listing[0]['mapping_route']['approved_qa_ids']==[PRICE,DETAILS,PAY]
    rejected=api.put(f"/admin/instagram-media/{item['id']}/mapping",headers=HEADERS,json={**payload,'answer':'copied'})
    assert rejected.status_code==400
    assert database.get_instagram_source_qa_bundle(target['routing_id'])['approved_qa_ids']==[PRICE,DETAILS,PAY]


def test_custom_switch_retains_custom_history_and_isolates_other_sources(store):
    database,_,api=store
    custom=database.save_instagram_source_route(dict(source_type='post',canonical_url='https://www.instagram.com/p/Custom123/',shortcode='Custom123',response_mode='CUSTOM',source_description='Retained custom content.',active=True))
    other=mapped(store)
    response=api.post('/admin/instagram-routes',headers=HEADERS,json=dict(routing_id=custom['routing_id'],response_mode='APPROVED_QA',approved_qa_ids=[PRICE,DETAILS],source_context='sondesh'))
    assert response.status_code==200,response.text
    response=api.post('/admin/instagram-routes',headers=HEADERS,json=dict(routing_id=custom['routing_id'],response_mode='CUSTOM'))
    assert response.status_code==200,response.text
    assert response.json()['route']['source_description']=='Retained custom content.' and response.json()['route']['approved_qa_id'] is None
    assert not response.json()['route']['qa_bundle']
    assert database.get_instagram_source_qa_bundle(other['routing_id'])['qa_bundle']


def test_story_and_exact_ad_authority_remain_separate(store):
    database,_,api=store;post=mapped(store,'post')
    story=api.post('/admin/instagram-routes',headers=HEADERS,json=dict(source_type='story',media_id='Story123',response_mode='APPROVED_QA',approved_qa_ids=['home_table_price','home_table_details'],story_context='home_table')).json()['route']
    result,facts=reply(store,story,'price?');assert result[1] and facts['client_qa_id']=='home_table_price' and facts['story_bundle_match']=='matched'
    result,facts=reply(store,post,'price?');assert result[1] and facts['client_qa_id']==PRICE
    result,_=reply(store,story,'How long does it last?');assert not result[1]
    assert database.get_instagram_source_qa_bundle(story['routing_id']) is None
    assert database.get_instagram_story_qa_bundle(post['routing_id']) is None
    legacy=database.save_instagram_source_route(dict(source_type='reel',canonical_url='https://www.instagram.com/reel/Ad123/',shortcode='Ad123',media_id='Ad123',ad_id='120246385008280540',response_mode='APPROVED_QA',approved_qa_id=DETAILS,active=True))
    selected,method=route_for_inbound(ad_id='120246385008280540',media_id=None)
    assert method=='ad_id' and selected['routing_id']==legacy['routing_id'] and not selected['qa_bundle']
    result,facts=reply(store,selected,'Tell me more');assert result[1] and facts['client_qa_id']==DETAILS


def test_executed_checklists_search_count_context_isolation_and_ad_single_select():
    node=shutil.which('node');assert node
    script=SCRIPT.split('<script>',1)[1].split('</script>',1)[0]
    harness=r'''
const elements={};const modes={ig:'APPROVED_QA',igMedia:'APPROVED_QA',igStory:'APPROVED_QA'};let requests=0;
function element(tag='div'){return {tag,value:'',textContent:'',style:{},children:[],hidden:false,replaceChildren(...kids){this.children=kids},append(...kids){this.children.push(...kids)},add(kid){this.children.push(kid)},closest(){return {toggleAttribute(){}}}}}
const document={getElementById(id){return elements[id] ||= element()},createElement:element,createTextNode(text){return {textContent:text}},querySelector(q){const prefix=q.match(/name="(.*?)ResponseMode/)[1];return {value:modes[prefix],checked:true}}};
const Option=function(text,value){return {textContent:text,value}};const getAuthHeaders=()=>({});
const data={contexts:[{value:'general',label:'General'},{value:'sondesh',label:'Sondesh'},{value:'home_table',label:'Home Table'}],records:[
{qa_id:'price',question:'Price <unsafe>',context:'sondesh',answer:'Approved price.'},
{qa_id:'details',question:'Details?',context:'sondesh',answer:'Approved details.'},
{qa_id:'payment',question:'Payment?',context:'general',answer:'Approved payment.'},
{qa_id:'home',question:'Home price?',context:'home_table',answer:'Other answer.'}]};
const fetch=async(url,options)=>{if(options.method)throw Error('unexpected write');requests++;return {ok:true,json:async()=>data}};
function text(node){return String(node.textContent||'')+(node.children||[]).map(text).join('')}
function inputs(node){return [...(node.tag==='input'?[node]:[]),...(node.children||[]).flatMap(inputs)]}
'''
    checks=r'''
(async()=>{
 document.getElementById('igSourceType').value='post';
 for(const prefix of ['ig','igMedia','igStory'])await initSourceResponse(prefix,{response_mode:'APPROVED_QA',approved_qa_id:'price'});
 for(const prefix of ['ig','igMedia']){
  const box=document.getElementById(prefix+'QaChecklist');const checks=inputs(box);
  if(!checks.find(c=>c.value==='price').checked||checks.find(c=>c.value==='payment').checked||checks.find(c=>c.value==='home'))throw Error('legacy/filter/general');
  if(!text(box).includes('Price <unsafe>')||!text(box).includes('General — select explicitly'))throw Error('labels');
  const pay=checks.find(c=>c.value==='payment');pay.checked=true;pay.onchange();
  if(document.getElementById(prefix+'QaCount').textContent!=='Selected: 2')throw Error('count');
  document.getElementById(prefix+'QaSearch').value='details';filterSourceQa(prefix);
  if(inputs(box).length!==1||inputs(box)[0].value!=='details')throw Error('search');
  const payload=sourceResponseFields(prefix);
  if(JSON.stringify(payload.approved_qa_ids)!=='["price","payment"]'||payload.source_context!=='sondesh'||'story_context' in payload||JSON.stringify(payload).includes('Approved price'))throw Error('payload');
  document.getElementById(prefix+'QaContext').value='home_table';changeStoryQaContext(prefix);
  if(!text(box).includes('unavailable/expired or context changed'))throw Error('unavailable');
 }
 if(sourceResponseState.igStory.selectedIds.size!==1||sourceResponseState.igStory.context!=='sondesh')throw Error('Story state leakage');
 const story=sourceResponseFields('igStory');if(story.story_context!=='sondesh'||'source_context' in story)throw Error('Story contract');
 document.getElementById('igSourceType').value='ad';setSourceResponseMode('ig');filterSourceQa('ig');
 const ad=sourceResponseFields('ig');if('approved_qa_ids' in ad||document.getElementById('igApprovedQaId').hidden||!document.getElementById('igQaBundleFields').hidden)throw Error('Ad became multi');
 document.getElementById('igSourceType').value='reel';setSourceResponseMode('ig');filterSourceQa('ig');if(!sourceQaMultiple('ig'))throw Error('Reel not multi');
 modes.igMedia='CUSTOM';if('approved_qa_ids' in sourceResponseFields('igMedia'))throw Error('Custom leak');
 if(requests!==3)throw Error('unexpected write');console.log('PASS');
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result=subprocess.run([node,'-e',harness+script+checks],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert 'PASS' in result.stdout
    for prefix in ('ig','igMedia','igStory'):
        assert prefix+'QaChecklist' in selector_html(prefix) and 'max-height:40vh' in selector_html(prefix)
