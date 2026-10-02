"""Exact manual source routing and ordered eligible Custom Q&A replies."""
import json
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from camellia import app as app_module, custom_response_sequence as sequence, menu_images, menu_catalogue, client_qa
from camellia.database import Database
from camellia.config import config
from camellia.responder import responder
from camellia.session_memory import session_memory
from camellia.instagram_routing import validate_route_payload, route_for_inbound


@pytest.fixture
def store(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "followup.db"))
    for module in (app_module, menu_images, menu_catalogue):
        monkeypatch.setattr(module, "db", database)
    monkeypatch.setattr("camellia.database.db", database)
    monkeypatch.setattr("camellia.responder.db", database)
    monkeypatch.setattr("camellia.handoff.db", database)
    monkeypatch.setattr("camellia.rag_fallback.db", database)
    monkeypatch.setattr(config, "ADMIN_API_KEY", "followup-unit-admin-key")
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "INSTAGRAM_GRAPH_ACCOUNT_ID", "28206314108997745")
    monkeypatch.setattr(config, "INSTAGRAM_ACCOUNT_ID", "17841455615744812")
    session_memory._sessions.clear()
    return database


def auth():
    return {"X-Admin-Key": config.ADMIN_API_KEY}


@pytest.fixture
def assets(store, monkeypatch):
    urls = {"aamar_mishti": "/uploads/aamar.jpeg", "signature": "/uploads/signature.jpeg"}
    monkeypatch.setattr(menu_images, "current_url", lambda key, *args: urls.get(key))
    return urls


def blocks():
    return [{"type":"text", "text":"Collection 1"}, {"type":"menu", "menu_id":"aamar_mishti"},
            {"type":"text", "text":"Collection 2"}, {"type":"menu", "menu_id":"signature"},
            {"type":"text", "text":"Remaining answer"}]


def create(client, values):
    return client.post('/admin/custom-qa', headers=auth(), json={"topic":"Special information", "primary_question":"unique librarian special information", "approved_answer":"Original text answer", "response_sequence":values, "force_save":True})


def test_text_only_compatibility_and_migration(store):
    kid = store.add_learned_knowledge("Custom", "unique older question", "Original text")
    assert store.get_custom_qa(kid)["verified_answer"] == "Original text"
    assert json.loads(store.get_custom_qa(kid)["response_sequence"]) == []
    assert Database(store.db_path).get_custom_qa(kid)["verified_answer"] == "Original text"


def test_create_edit_reorder_delete_restore_blocks(store, assets):
    client = TestClient(app_module.app)
    result = create(client, blocks()); assert result.status_code == 200, result.text
    kid = result.json()["id"]
    assert json.loads(store.get_custom_qa(kid)["response_sequence"]) == blocks()
    changed = [blocks()[2], blocks()[3], {"type":"text", "text":"Edited ending"}]
    response = client.patch(f'/admin/custom-qa/{kid}', headers=auth(), json={"response_sequence":changed,"force_save":True})
    assert response.status_code == 200, response.text
    row = store.get_custom_qa(kid)
    assert row["verified_answer"] == "Original text answer"
    assert json.loads(row["response_sequence"]) == changed
    version = store.get_learned_knowledge_versions(kid)[0]
    assert json.loads(version["response_sequence"]) == blocks()
    assert store.restore_learned_knowledge_version(kid, version["version_id"], "unit")
    assert json.loads(store.get_custom_qa(kid)["response_sequence"]) == blocks()


def test_preview_preserves_order_and_sends_nothing(store, assets):
    client = TestClient(app_module.app)
    with patch.object(app_module, 'send_instagram_reply') as send:
        response = client.post('/admin/custom-qa/preview',headers=auth(),json={"approved_answer":"Original", "response_sequence":blocks()})
    assert response.status_code == 200, response.text
    result = response.json()["response_blocks"]
    assert [b["kind"] for b in result] == ["TEXT","IMAGE","TEXT","IMAGE","TEXT"]
    assert result[1]["url"] == assets["aamar_mishti"]
    assert result[3]["url"] == assets["signature"]
    send.assert_not_called()


def test_runtime_emits_ordered_images(store, assets):
    row = {"id":100, "response_sequence":json.dumps(blocks())}
    facts = session_memory.get_facts("sequence-unit", "instagram")
    reply = responder._custom_sequence_reply(row, facts, "sequence-unit", "instagram", "unique question")
    assert reply[1] is True
    with patch.object(app_module, '_send_or_queue_instagram_reply', return_value={"status":"sent"}) as send:
        app_module.send_instagram_response_blocks("sequence-unit", facts["ordered_response_blocks"])
    calls = send.call_args_list
    assert [c.args[1] for c in calls] == ["Collection 1", "", "Collection 2", "", "Remaining answer"]
    assert calls[1].kwargs["image_url"] == [assets["aamar_mishti"]]
    assert calls[3].kwargs["image_url"] == [assets["signature"]]


def test_missing_asset_fails_before_any_partial_reply(store, assets):
    assets.pop("signature")
    facts = session_memory.get_facts("missing-sequence", "instagram")
    result = responder._custom_sequence_reply({"id":101,"response_sequence":json.dumps(blocks())}, facts, "missing-sequence", "instagram", "question")
    assert result[1] is False
    assert "Manager of Kitchens" in result[0]
    assert not facts.get("ordered_response_blocks")


@pytest.mark.parametrize('value', [[{"type":"image","url":"https://foreign.invalid/image"}], [{"type":"menu","menu_id":"https://foreign.invalid/image"}], [{"type":"text","text":""}], [{"type":"menu","menu_id":"signature","url":"https://foreign.invalid/image"}]])
def test_raw_urls_and_invalid_blocks_rejected(store, assets, value):
    with pytest.raises(ValueError): sequence.validate(value)


def test_weekly_attachment_promise_warns_without_block(store, assets):
    assert sequence.attachment_warning("I've attached this week's menu", [])
    assert not sequence.attachment_warning("Collection information", blocks())


@pytest.mark.parametrize('product,qa', [('sondesh','sondesh_details_client'),('sandesh','sondesh_details_client'),('bento','bento_details_client'),('subscription','subscription_details_client')])
def test_bare_product_is_information_after_tell_me_more(store, product, qa):
    uid = 'bare-' + product
    with patch('camellia.ai_responder.ai_responder._query_groq', return_value=('',False,'disabled')):
        responder.match_and_respond('tell me more',user_id=uid,channel='instagram')
        text, matched, _, _ = responder.match_and_respond(product,user_id=uid,channel='instagram')
    assert matched is True
    facts = session_memory.get_facts(uid, 'instagram')
    assert facts['client_qa_id'] == qa
    rendered = "".join(b.get('text','') for b in facts.get('ordered_response_blocks', []) if b.get('kind') == 'TEXT')
    assert text.strip()
    assert facts.get('last_grounding') == 'exact_approved'
    assert 'ORDER DRAFT' not in text
    assert not session_memory.get_facts(uid,'instagram').get('order_intake_draft')


def test_explicit_order_still_drafts(store):
    text, matched, route, _ = responder.match_and_respond('I want to order 2 boxes Sondesh Gurgaon Friday dinner', user_id='genuine-order',channel='instagram')
    assert matched and route == 'order_intake_draft'
    assert session_memory.get_facts('genuine-order','instagram')['order_intake_draft']['quantity'] == '2 boxes'


@pytest.mark.parametrize('kind', ['p','reel'])
def test_manual_link_stays_unverified_after_complete_owned_sync(store, kind):
    client = TestClient(app_module.app)
    response = client.post('/admin/instagram-media/manual-link',headers=auth(),json={"url":f'https://www.instagram.com/{kind}/Ddy0Z8pkR-g/?igsh=discard'})
    assert response.status_code == 200, response.text
    item = response.json()['item']
    assert item['permalink'] == f'https://www.instagram.com/{kind}/Ddy0Z8pkR-g/'
    assert item['owner_instagram_account_id'] is None
    assert item['inventory_state'] == 'COLLABORATOR_UNVERIFIED'
    store.apply_instagram_media_inventory([], '28206314108997745', 'complete-unit')
    gallery,total=store.list_instagram_media(owner_account_id='28206314108997745')
    assert total == 1
    assert gallery[0]['inventory_verification'] == 'MANUAL_EXACT_LINK'
    assert gallery[0]['inventory_state'] == 'COLLABORATOR_UNVERIFIED'
    app_module._require_verified_current_instagram_media(gallery[0])


def test_manual_post_route_pairs_only_explicit_meta_identity(store):
    route=store.save_instagram_source_route(validate_route_payload({"url":"https://www.instagram.com/p/Ddy0Z8pkR-g/","routing_topic":"Custom topic","source_description":"Custom reply","response_mode":"CUSTOM","menu_mode":"NONE","active":True}))
    assert route['media_id'] is None
    assert route_for_inbound(post_media_id='99887766',media_id=None,ad_id=None)[0] is None
    pairing=store.start_post_pairing(route['routing_id'],600,'unit')
    store.bind_post_pairing(pairing['id'],'99887766')
    assert route_for_inbound(post_media_id='99887766',media_id=None,ad_id=None)[0]['routing_id'] == route['routing_id']
    assert route_for_inbound(post_media_id='99887767',media_id=None,ad_id=None)[0] is None


def test_editor_controls_present_and_system_delete_protected(store):
    page=TestClient(app_module.app).get('/admin').text
    for label in ['Response Sequence / Menu & Images','+ Add Text Block','+ Add Menu / Image','Move Up','Move Down','Delete Block','+ Add Post / Reel by Link']:
        assert label in page
    assert TestClient(app_module.app).delete('/admin/response-library/system/greeting',headers=auth()).status_code in (404,405)


def test_event_dispatches_complete_stored_sequence(store, assets):
    client = TestClient(app_module.app)
    result = create(client, blocks())
    assert result.status_code == 200
    with patch.object(app_module, '_send_or_queue_instagram_reply', return_value={'status':'sent'}) as send, patch('camellia.ai_responder.ai_responder._query_groq', side_effect=AssertionError('unexpected LLM')):
        app_module._process_instagram_event('sequence-event', 'unique librarian special information', 'unit-ad')
    assert send.call_count == 5, (send.call_args_list, session_memory.get_facts('sequence-event', 'instagram'))
    assert [call.args[1] for call in send.call_args_list] == ['Collection 1', '', 'Collection 2', '', 'Remaining answer']
    assert send.call_args_list[1].kwargs['image_url'] == [assets['aamar_mishti']]
    assert send.call_args_list[3].kwargs['image_url'] == [assets['signature']]
    assert store.get_active_handoff('instagram', 'sequence-event') is None


def test_sequence_archive_and_safe_delete(store, assets):
    client = TestClient(app_module.app)
    kid = create(client, blocks()).json()['id']
    confirmation = {'confirm_permanent_delete':True, 'confirm_question':'unique librarian special information'}
    assert client.request('DELETE', f'/admin/custom-qa/{kid}', headers=auth(), json=confirmation).status_code == 409
    assert client.post(f'/admin/custom-qa/{kid}/status', headers=auth(), json={'status':'ARCHIVED'}).status_code == 200
    assert json.loads(store.get_learned_knowledge_versions(kid)[0]['response_sequence']) == blocks()
    assert client.request('DELETE', f'/admin/custom-qa/{kid}', headers=auth(), json=confirmation).status_code == 200


def test_test_endpoint_returns_ordered_sequence_without_sending(store, assets):
    client = TestClient(app_module.app)
    kid = create(client, blocks()).json()['id']
    with patch.object(app_module, 'send_instagram_reply') as send:
        data = client.post('/admin/custom-qa/test', headers=auth(), json={'message':'unique librarian special information','focus_id':kid}).json()
    assert data['would_send'] is True
    assert [block['kind'] for block in data['response_blocks']] == ['TEXT','IMAGE','TEXT','IMAGE','TEXT']
    send.assert_not_called()


@pytest.mark.parametrize('kind', ['p', 'reel'])
def test_manual_mapping_edit_archive_preserves_unrelated_route(store, assets, kind):
    client = TestClient(app_module.app)
    prior = store.save_instagram_source_route(validate_route_payload({'url':'https://www.instagram.com/reel/Existing123/', 'routing_topic':'Prior', 'response_mode':'CUSTOM', 'source_description':'Prior description', 'menu_mode':'NONE','active':True}))
    topic = store.create_instagram_topic('manual-unit', 'Manual topic', '', None, [], [])
    item = client.post('/admin/instagram-media/manual-link',headers=auth(),json={'url':f'https://www.instagram.com/{kind}/Ddy0Z8pkR-g/'}).json()['item']
    payload = {'topic_id':topic['topic_id'],'response_mode':'CUSTOM','source_description':'Operator exact description','menu_mode':'SPECIFIC','menu_id':'aamar_mishti'}
    response = client.put(f"/admin/instagram-media/{item['id']}/mapping",headers=auth(),json=payload)
    assert response.status_code == 200, response.text
    route = response.json()['route']
    assert route['media_id'] is None
    assert route['source_description'] == payload['source_description']
    payload['response_mode'] = 'APPROVED_QA'; payload['approved_qa_id'] = 'bento_details_client'
    response = client.put(f"/admin/instagram-media/{item['id']}/mapping",headers=auth(),json=payload)
    assert response.status_code == 200, response.text
    assert response.json()['route']['routing_id'] == route['routing_id']
    assert client.delete(f"/admin/instagram-media/{item['id']}/mapping",headers=auth()).status_code == 200
    assert store.get_instagram_source_route(route['routing_id'])['archived_at']
    assert store.get_instagram_source_route(prior['routing_id']) == prior


def test_owned_sync_upgrades_manual_card_without_losing_mapping(store):
    item = store.upsert_instagram_media({'instagram_media_id':'manual:reel:Owned123','permalink':'https://www.instagram.com/reel/Owned123/','shortcode':'Owned123','media_type':'VIDEO','sync_status':'MANUAL_EXACT_LINK','inventory_state':'COLLABORATOR_UNVERIFIED'})
    route = store.save_instagram_source_route(validate_route_payload({'url':item['permalink'],'routing_topic':'Mapped','response_mode':'CUSTOM','source_description':'Keep mapping','menu_mode':'NONE','active':True}))
    store.update_instagram_media_route(item['id'],route_mode='CUSTOM',topic_id=None,custom_route_id=route['routing_id'],needs_review=False)
    store.apply_instagram_media_inventory([{'instagram_media_id':'180123456789','permalink':item['permalink'],'media_type':'VIDEO', 'inventory_state':'CURRENT', 'owner_instagram_account_id':'28206314108997745', 'last_sync_generation':'unit-owned'}],'28206314108997745','unit-owned')
    updated = store.get_instagram_media(item['id'])
    assert updated['inventory_state'] == 'CURRENT'
    assert updated['instagram_media_id'] == '180123456789'
    assert updated['custom_route_id'] == route['routing_id']
    assert store.get_instagram_source_route(route['routing_id']) == route


def test_inactive_catalogue_asset_rejected(store, assets, monkeypatch):
    monkeypatch.setattr(menu_catalogue, 'list_menus', lambda **kwargs: [{'menu_id':'aamar_mishti', 'name':'Aamar'}])
    with pytest.raises(ValueError):
        sequence.validate([{'type':'menu','menu_id':'signature'}])


def test_approved_product_information_outranks_custom_sequence(store, assets):
    store.add_learned_knowledge('Legacy product', 'sondesh', 'Legacy independent prose', source='client_admin', response_sequence=blocks())
    text, matched, _, _ = responder.match_and_respond('sondesh', user_id='canonical-first', channel='instagram')
    facts = session_memory.get_facts('canonical-first','instagram')
    assert matched and facts['client_qa_id'] == 'sondesh_details_client'
    assert facts['last_route_source'] != 'CUSTOM_SEQUENCE'
    assert 'Legacy independent prose' not in text
    assert text != 'Collection 1\n\nCollection 2\n\nRemaining answer'


def test_bare_product_does_not_edit_active_genuine_draft(store):
    responder.match_and_respond('I want to order 2 boxes Sondesh Gurgaon Friday dinner', user_id='order-information', channel='instagram')
    facts = session_memory.get_facts('order-information','instagram')
    original = dict(facts['order_intake_draft'])
    text, matched, _, _ = responder.match_and_respond('sondesh', user_id='order-information', channel='instagram')
    assert matched and facts['client_qa_id'] == 'sondesh_details_client'
    assert facts['order_intake_draft'] == original
    assert 'ORDER DRAFT' not in text


def test_unavailable_sequence_event_sends_only_handoff(store, assets):
    client = TestClient(app_module.app)
    assert create(client, blocks()).status_code == 200
    assets.pop('signature')
    with patch.object(app_module, '_send_or_queue_instagram_reply', return_value={'status':'sent'}) as send, patch.object(app_module.escalator, 'handle_escalation'):
        app_module._process_instagram_event('missing-event', 'unique librarian special information', 'unit-ad')
    assert send.call_count == 1
    assert 'Manager of Kitchens' in send.call_args.args[1]
    assert send.call_args.kwargs.get('image_url') is None
    assert store.get_active_handoff('instagram','missing-event') is not None


def test_activation_warns_weekly_promise_without_attachment(store):
    kid = store.add_learned_knowledge('Weekly promise', 'unique parcel updates', "I've attached this week's menu", source='client_admin', status='DISABLED')
    response = TestClient(app_module.app).post(f'/admin/custom-qa/{kid}/status', headers=auth(), json={'status':'ACTIVE'})
    assert response.status_code == 200
    assert response.json()['status'] == 'warning'
    assert store.get_custom_qa(kid)['status'] == 'DISABLED'


@pytest.mark.parametrize('state', ['STALE', 'FOREIGN', 'UNVERIFIED'])
def test_manual_link_reuses_historical_card_without_losing_mapping(store, state):
    url = 'https://www.instagram.com/p/Ddy0Z8pkR-g/'
    route = store.save_instagram_source_route(validate_route_payload({'url':url, 'routing_topic':'Historical', 'response_mode':'CUSTOM', 'source_description':'Historical description', 'menu_mode':'NONE','active':True}))
    item = store.upsert_instagram_media({'instagram_media_id':'180historical','permalink':url,'inventory_state':state})
    store.update_instagram_media_route(item['id'], route_mode='CUSTOM', topic_id=None, custom_route_id=route['routing_id'], needs_review=False)
    response = TestClient(app_module.app).post('/admin/instagram-media/manual-link',headers=auth(),json={'url':url})
    assert response.status_code == 200
    updated = response.json()['item']
    assert updated['id'] == item['id']
    assert updated['instagram_media_id'] == item['instagram_media_id']
    assert updated['inventory_state'] == 'COLLABORATOR_UNVERIFIED'
    assert updated['sync_status'] == 'MANUAL_EXACT_LINK'
    assert updated['custom_route_id'] == route['routing_id']
    assert store.get_instagram_source_route(route['routing_id']) == route
    app_module._require_verified_current_instagram_media(updated)


def test_manual_add_does_not_downgrade_verified_owned_card(store):
    url = 'https://www.instagram.com/reel/Owned123/'
    store.apply_instagram_media_inventory([{'instagram_media_id':'180123456789','permalink':url,'media_type':'VIDEO','inventory_state':'CURRENT','owner_instagram_account_id':'28206314108997745','last_sync_generation':'unit-verified'}], '28206314108997745', 'unit-verified')
    item = store.list_instagram_media(owner_account_id='28206314108997745')[0][0]
    result = TestClient(app_module.app).post('/admin/instagram-media/manual-link',headers=auth(),json={'url':url})
    assert result.status_code == 200
    assert result.json()['item'] == store.get_instagram_media(item['id'])
    assert result.json()['item']['inventory_state'] == 'CURRENT'
    assert result.json()['item']['sync_status'] != 'MANUAL_EXACT_LINK'
