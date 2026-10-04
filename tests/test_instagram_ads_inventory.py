"""Read-only Marketing discovery, atomic snapshots and exact PR #58 binding UI."""
import json
import re
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from camellia import app as app_module, instagram_ads as ads
from camellia.config import config
from camellia.database import Database

_HTTPX_CLIENT = httpx.Client


@pytest.fixture
def store(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "ads.db"))
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(config, "META_MARKETING_ACCESS_TOKEN", "private-marketing-fixture")
    monkeypatch.setattr(config, "META_AD_ACCOUNT_ID", "12345")
    monkeypatch.setattr(config, "ADMIN_API_KEY", "ads-admin-fixture")
    monkeypatch.setattr(config, "INSTAGRAM_API_VERSION", "v25.0")
    return database


def item(ad_id="120246385008280540", effective="ACTIVE", **creative):
    return {"id": ad_id, "name": "Norom Pak Sondesh", "status": "ACTIVE", "effective_status": effective,
            "creative": {"id": "900", "effective_instagram_media_id": "180001", "instagram_permalink_url": "https://www.instagram.com/p/ExactSondesh/", **creative}}


def mock_pages(monkeypatch, responses):
    requests = []
    def handler(request):
        requests.append(request)
        answer = responses[len(requests) - 1]
        if isinstance(answer, Exception):
            raise answer
        status, payload = answer if isinstance(answer, tuple) else (200, answer)
        return httpx.Response(status, json=payload)
    monkeypatch.setattr(ads.httpx, "Client", lambda **kwargs: _HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    return requests


def route(store, code="ExactSondesh", media_id="180001", **extra):
    return store.save_instagram_source_route({"source_type": "post", "canonical_url": f"https://www.instagram.com/p/{code}/",
        "shortcode": code, "media_id": media_id, "routing_topic": "Sondesh", "source_description": "Approved source description",
        "active": True, "priority_products": [], "knowledge_scope": [], **extra})


def sync(store, monkeypatch, rows=None):
    mock_pages(monkeypatch, [{"data": rows if rows is not None else [item()]}])
    return ads.sync_instagram_ads(store)


@pytest.mark.parametrize("value,expected", [("12345", "act_12345"), ("act_12345", "act_12345"), (" act_12345 ", "act_12345"), ("", ""), ("act_act_123", ""), ("../123", "")])
def test_account_normalization(store, monkeypatch, value, expected):
    monkeypatch.setattr(config, "META_AD_ACCOUNT_ID", value)
    assert ads.ad_account_id() == expected


def test_read_only_requests_use_existing_version_and_server_header(store, monkeypatch):
    calls = mock_pages(monkeypatch, [{"data": [item()]}])
    result = ads.sync_instagram_ads(store)
    assert result["status"] == "success" and result["active_ad_count"] == 1
    assert calls[0].method == "GET"
    assert str(calls[0].url).startswith("https://graph.facebook.com/v25.0/act_12345/ads?")
    assert "access_token" not in str(calls[0].url)
    assert calls[0].headers["Authorization"] == "Bearer private-marketing-fixture"
    assert "private-marketing-fixture" not in json.dumps(result)
    assert store.list_instagram_ad_bindings() == []
    assert store.list_instagram_source_routes() == []


def test_complete_pagination_drops_next_url_token(store, monkeypatch):
    next_url = "https://graph.facebook.com/v25.0/act_12345/ads?after=cursor1&access_token=private-marketing-fixture"
    calls = mock_pages(monkeypatch, [{"data": [item()], "paging": {"next": next_url}}, {"data": [item("200")]}])
    assert ads.sync_instagram_ads(store)["status"] == "success"
    assert len(store.list_instagram_ads_inventory("act_12345")) == 2
    assert calls[1].url.params["after"] == "cursor1" and "access_token" not in str(calls[1].url)


@pytest.mark.parametrize("effective", ["PAUSED", "CAMPAIGN_PAUSED", "ADSET_PAUSED", "ARCHIVED", "DELETED", "DISAPPROVED", "IN_PROCESS", "WITH_ISSUES"])
def test_only_effective_active_is_active(store, monkeypatch, effective):
    sync(store, monkeypatch, [item(effective=effective)])
    assert ads.ads_workspace(store, "active")["items"] == []
    assert ads.ads_workspace(store, "inactive")["items"][0]["effective_status"] == effective


@pytest.mark.parametrize("failure,category", [((400, {"error": {"code": 190, "message": "private-marketing-fixture"}}), "INVALID_OR_EXPIRED_TOKEN"),
    ((403, {"error": {"code": 200}}), "PERMISSION_DENIED"), ((429, {"error": {"code": 4}}), "RATE_LIMITED"),
    (httpx.ConnectError("private-marketing-fixture"), "NETWORK_OR_INVALID_RESPONSE"), ({"data": "bad"}, "INVALID_RESPONSE")])
def test_partial_page_failure_preserves_inventory_and_bindings(store, monkeypatch, failure, category):
    saved = route(store)
    store.bind_instagram_ad(item()["id"], saved["routing_id"])
    sync(store, monkeypatch)
    before = store.list_instagram_ads_inventory("act_12345")
    mock_pages(monkeypatch, [{"data": [item("999")], "paging": {"next": "https://graph.facebook.com/v25.0/act_12345/ads?after=x"}}, failure])
    result = ads.sync_instagram_ads(store)
    assert result["error_category"] == category and result["status"] == "error"
    assert store.list_instagram_ads_inventory("act_12345") == before
    assert store.instagram_ad_binding_status(item()["id"]) == "BOUND"
    assert "private-marketing-fixture" not in json.dumps(result)


def test_optional_fields_fallback_restarts_and_keeps_safe_diagnostic(store, monkeypatch):
    calls = mock_pages(monkeypatch, [(400, {"error": {"code": 100, "message": "Tried accessing nonexisting field thumbnail_url on node type AdCreative private-marketing-fixture"}}), {"data": [item()]}])
    result = ads.sync_instagram_ads(store)
    assert result["status"] == "success" and result["optional_fields_reduced"] == 1
    assert "thumbnail_url" in calls[0].url.params["fields"] and "thumbnail_url" not in calls[1].url.params["fields"]


def test_complete_absence_marks_ended_retains_history_and_route(store, monkeypatch):
    saved = route(store)
    store.bind_instagram_ad(item()["id"], saved["routing_id"])
    sync(store, monkeypatch)
    history = store.list_instagram_ad_binding_history(item()["id"])
    sync(store, monkeypatch, [])
    entry = ads.ads_workspace(store, "inactive")["items"][0]
    assert entry["effective_status"] == "ENDED" and entry["mapped"]
    assert store.list_instagram_ad_binding_history(item()["id"]) == history
    assert store.instagram_ad_binding_status(item()["id"]) == "BOUND"
    assert len(store.list_instagram_source_routes()) == 1


@pytest.mark.parametrize("identity", ["media", "url", "shortcode"])
def test_suggestions_use_only_exact_identity_without_binding(store, monkeypatch, identity):
    saved = route(store, media_id="180001" if identity == "media" else None,
                  canonical_url=None if identity != "url" else "https://www.instagram.com/p/ExactSondesh/")
    sync(store, monkeypatch)
    result = ads.ads_workspace(store, "unmapped")["items"][0]
    assert result["suggested_routing_id"] == saved["routing_id"]
    assert result["identity_status"] == "EXACT_MATCH"
    assert not result["mapped"] and store.list_instagram_ad_bindings() == []


def test_matching_title_does_not_supply_identity(store, monkeypatch):
    route(store, code="DifferentPost", media_id="999999", routing_topic="Norom Pak Sondesh")
    sync(store, monkeypatch)
    result = ads.ads_workspace(store, "unmapped")["items"][0]
    assert result["suggested_routing_id"] is None and result["identity_status"] == "NO_EXACT_MATCH"


def test_conflicting_exact_identifiers_fail_closed(store, monkeypatch):
    route(store, code="OtherPost", media_id="180001")
    sync(store, monkeypatch)
    result = ads.ads_workspace(store, "unmapped")["items"][0]
    assert result["suggested_routing_id"] is None and result["identity_status"] == "AMBIGUOUS"


def test_multiple_exact_candidates_fail_closed(store, monkeypatch):
    route(store)
    with store.get_connection() as conn:
        conn.execute("INSERT INTO instagram_source_routes(source_type,shortcode,media_id,routing_topic,active) VALUES ('post','ExactSondesh','180001','duplicate',1)")
        conn.commit()
    sync(store, monkeypatch)
    assert ads.ads_workspace(store, "unmapped")["items"][0]["identity_status"] == "AMBIGUOUS"


def test_observed_inventory_binding_union_deduplicates_and_counts(store, monkeypatch):
    saved = route(store)
    store.observe_instagram_ad(item()["id"], "wrong display title")
    store.observe_instagram_ad("observed-only")
    store.bind_instagram_ad(item()["id"], saved["routing_id"])
    sync(store, monkeypatch)
    result = ads.ads_workspace(store, "all")
    assert len(result["items"]) == 2
    assert result["counts"] == {"active": 1, "raw_active": 1, "mapped": 1, "unmapped": 1, "inactive": 0, "all": 2,
                                "running": None, "raw_running": None}
    assert ads.ads_workspace(store, "mapped")["items"][0]["name"] == "Norom Pak Sondesh"


def test_admin_authenticated_endpoints_and_pr58_binding_history(store, monkeypatch):
    monkeypatch.setattr(ads, "delivery_evidence", lambda: {"status": "unavailable", "ads": {}, "error_category": "PERMISSION_DENIED"})
    client = TestClient(app_module.app)
    headers = {"x-admin-key": config.ADMIN_API_KEY}
    saved = route(store)
    other = route(store, code="Other", media_id="180002")
    sync(store, monkeypatch)
    for path in ("/admin/instagram-ads", "/admin/instagram-ads/sync-status"):
        assert client.get(path).status_code in {401, 403}
        result = client.get(path, headers=headers)
        assert result.status_code == 200 and "private-marketing-fixture" not in result.text
    assert client.post("/admin/instagram-ads/sync").status_code in {401, 403}
    assert client.get("/admin/instagram-ads?view=bad", headers=headers).status_code == 400
    ad_id = item()["id"]
    response = client.post(f"/admin/instagram-ads/{ad_id}/bind", headers=headers, json={"routing_id": saved["routing_id"]})
    assert response.status_code == 200 and response.json()["binding"]["active"] == 1
    assert response.json()["binding"]["bound_by"] == "admin"
    assert client.post(f"/admin/instagram-ads/{ad_id}/bind", headers=headers, json={"routing_id": other["routing_id"]}).status_code == 400
    assert client.post(f"/admin/instagram-ads/{ad_id}/bind", headers=headers, json={"routing_id": other["routing_id"], "change_binding": True}).status_code == 200
    assert client.patch(f"/admin/instagram-ads/{ad_id}/active", headers=headers, json={"active": False}).status_code == 200
    assert store.instagram_ad_binding_status(ad_id) == "UNBOUND"
    assert len(store.list_instagram_source_routes()) == 2
    assert [row["action"] for row in store.list_instagram_ad_binding_history(ad_id)] == ["DISABLED", "BOUND", "REBOUND", "BOUND"]


@pytest.mark.parametrize("next_url", ["https://attacker.example/?after=x", "http://graph.facebook.com/v25.0/act_12345/ads?after=x", "https://graph.facebook.com/v25.0/act_other/ads?after=x", "https://graph.facebook.com/v25.0/act_12345/ads"])
def test_pagination_cannot_redirect_credential(store, monkeypatch, next_url):
    calls = mock_pages(monkeypatch, [{"data": [item()], "paging": {"next": next_url}}])
    assert ads.sync_instagram_ads(store)["error_category"] == "INVALID_PAGINATION"
    assert len(calls) == 1 and store.list_instagram_ads_inventory("act_12345") == []


def test_not_configured_never_calls_meta(store, monkeypatch):
    monkeypatch.setattr(config, "META_MARKETING_ACCESS_TOKEN", "")
    calls = mock_pages(monkeypatch, [])
    result = ads.sync_instagram_ads(store)
    assert result["error_category"] == "NOT_CONFIGURED" and not result["configured"] and not calls


def test_sync_lock_prevents_overlapping_snapshot(store, monkeypatch):
    assert store.acquire_instagram_ads_sync("act_12345")
    calls = mock_pages(monkeypatch, [])
    assert ads.sync_instagram_ads(store)["status"] == "busy" and not calls


@pytest.mark.parametrize("identity", ["media", "pointer", "url", "shortcode"])
def test_posts_mapping_cards_and_filters_use_same_route(store, identity):
    saved = route(store, media_id="180001" if identity == "media" else None,
                  canonical_url="https://www.instagram.com/p/ExactSondesh/" if identity == "url" else None,
                  shortcode="ExactSondesh" if identity == "shortcode" else None)
    media = store.upsert_instagram_media({"instagram_media_id": "180001", "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/ExactSondesh/", "shortcode": "ExactSondesh"})
    if identity == "pointer":
        store.update_instagram_media_route(media["id"], route_mode="CUSTOM", topic_id=None, custom_route_id=saved["routing_id"], needs_review=False)
    mapped, total = store.list_instagram_media(mapping_status="mapped")
    assert total == 1 and mapped[0]["mapping_status"] == "MAPPED"
    assert mapped[0]["mapping_route"]["routing_id"] == saved["routing_id"]
    assert store.list_instagram_media(mapping_status="unmapped")[1] == 0


def test_stale_media_topic_does_not_claim_a_route(store):
    media = store.upsert_instagram_media({"instagram_media_id": "180002", "media_type": "IMAGE"})
    store.update_instagram_media_route(media["id"], route_mode="CUSTOM", topic_id="stale", custom_route_id=888, needs_review=False)
    assert store.list_instagram_media(mapping_status="mapped")[1] == 0
    assert store.list_instagram_media(mapping_status="unmapped")[1] == 1


def test_mapping_edits_resolve_exact_identity_instead_of_unrelated_pointer(store):
    unrelated = route(store, code="Other", media_id="999999")
    exact = route(store, media_id=None)
    media = store.upsert_instagram_media({"instagram_media_id": "180001", "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/ExactSondesh/", "shortcode": "ExactSondesh"})
    store.update_instagram_media_route(media["id"], route_mode="CUSTOM", topic_id=None,
        custom_route_id=unrelated["routing_id"], needs_review=False)
    assert store.get_instagram_media_mapping(media["id"], for_edit=True)["routing_id"] == exact["routing_id"]
    store.archive_instagram_source_route(exact["routing_id"])
    assert store.get_instagram_media_mapping(media["id"], for_edit=True) is None
    assert store.get_instagram_source_route(unrelated["routing_id"])["canonical_url"].endswith("Other/")


def test_approved_qa_route_with_empty_custom_topic_has_authoritative_display(store, monkeypatch):
    saved = route(store, routing_topic="", source_description="", response_mode="APPROVED_QA", approved_qa_id="sondesh_details_client")
    store.upsert_instagram_media({"instagram_media_id": "180001", "media_type": "IMAGE",
        "permalink": "https://www.instagram.com/p/ExactSondesh/", "shortcode": "ExactSondesh"})
    response = TestClient(app_module.app).get("/admin/instagram-media?mapping_status=mapped", headers={"x-admin-key": config.ADMIN_API_KEY})
    mapped = response.json()["items"][0]
    assert mapped["mapping_status"] == "MAPPED"
    assert mapped["mapping_route"]["routing_id"] == saved["routing_id"]
    assert mapped["mapping_route"]["display_label"] not in ("", "Unmapped", "Mapped source")
    assert mapped["mapping_route"]["display_description"]
    sync(store, monkeypatch)
    suggestion = ads.ads_workspace(store, "unmapped")
    assert suggestion["items"][0]["suggested_routing_id"] == saved["routing_id"]
    assert suggestion["routes"][0]["display_label"] == mapped["mapping_route"]["display_label"]


def test_mapped_card_never_renders_unmapped_for_blank_topic(store, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable; separate JavaScript gate is required")
    page = TestClient(app_module.app).get("/admin").text
    status = page.split("function instagramContentStatus(route)", 1)[1].split("function setInstagramContentFilter", 1)[0]
    card = page.split("function renderInstagramContentCard(route)", 1)[1].split("function renderInstagramContentGallery", 1)[0]
    script = "const assert=require('assert'); const escapeHtml=value=>String(value||'');\nfunction instagramContentStatus(route)" + status + "\nfunction renderInstagramContentCard(route)" + card + """
const html=renderInstagramContentCard({gallery_media:true,media_type:'IMAGE',routing_id:1,routing_topic:'',mapping_route:{routing_id:39,active:true}});
assert(html.includes('MAPPED') && html.includes('✓'));
assert(html.includes('Edit Mapping'));
assert(!html.includes('Unmapped') && !html.includes('Not mapped'));
"""
    target = tmp_path / "mapped-card.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(target)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_many_ad_ids_share_one_existing_source_and_idempotent_bind(store, monkeypatch):
    saved = route(store)
    for ad_id in ("100", "200"):
        store.bind_instagram_ad(ad_id, saved["routing_id"])
        store.bind_instagram_ad(ad_id, saved["routing_id"])
    sync(store, monkeypatch, [item("100"), item("200")])
    assert len(ads.ads_workspace(store, "mapped")["items"]) == 2
    assert len(store.list_instagram_source_routes()) == 1
    assert len(store.list_instagram_ad_binding_history("100")) == 1


def test_optional_fallback_later_page_failure_keeps_last_snapshot(store, monkeypatch):
    sync(store, monkeypatch)
    before = store.list_instagram_ads_inventory("act_12345")
    mock_pages(monkeypatch, [(400, {"error": {"code": 100, "message": "unsupported field thumbnail_url"}}),
        {"data": [item("999")], "paging": {"next": "https://graph.facebook.com/v25.0/act_12345/ads?after=x"}},
        (403, {"error": {"code": 200}})])
    assert ads.sync_instagram_ads(store)["status"] == "error"
    assert store.list_instagram_ads_inventory("act_12345") == before


def test_repeated_cursor_or_duplicate_id_does_not_publish(store, monkeypatch):
    next_url = "https://graph.facebook.com/v25.0/act_12345/ads?after=x"
    mock_pages(monkeypatch, [{"data": [item()], "paging": {"next": next_url}}, {"data": [item("200")], "paging": {"next": next_url}}])
    assert ads.sync_instagram_ads(store)["error_category"] == "INVALID_PAGINATION"
    assert store.list_instagram_ads_inventory("act_12345") == []


def test_ads_ui_filter_and_picker_execute_pr58_endpoint(store, tmp_path):
    from camellia.admin_ads import SCRIPT
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable; separate JavaScript gate is required")
    fixture = r'''
const assert = require('assert');
const nodes = {};
const calls = [];
global.adminAuthenticated = true;
global.getAuthHeaders = () => ({'X-CSRF-Token':'test-csrf'});
global.escapeHtml = value => String(value || '');
global.confirm = () => true;
global.document = {querySelectorAll:()=>[],getElementById:id=>nodes[id] ||= {innerHTML:'',textContent:'',style:{},querySelectorAll:()=>[],close:()=>{},showModal:()=>{}}};
const route = {routing_id:7,source_type:'post',active:true,canonical_url:'https://www.instagram.com/p/Exact/',routing_topic:'Source',media_id:'180001'};
const ad = {ad_id:'120246385008280540',name:'Display',mapped:false,active:true,history:[],suggested_routing_id:7};
global.fetch = async (url,options) => { calls.push({url,options}); return {ok:true,json:async()=>url.endsWith('/bind') ? {binding:{ad_id:ad.ad_id,routing_id:7,active:1}} : {items:[ad],routes:[route],counts:{active:1,unmapped:1},sync:{}}};};
'''
    checks = r'''
(async()=>{
 await loadAdsWorkspace();
 assert(calls[0].url.includes('view=running'));
 assert(nodes.adsList.innerHTML.includes('Map Ad'));
 const sibling={...ad,ad_id:'120246385008280541',thumbnail_url:'https://example.test/creative.jpg'};
 adsWorkspace.items=[ad,sibling];
 adsWorkspace.active_groups=[{identity:['180001'],member_ad_ids:[ad.ad_id,sibling.ad_id]}];
 adsWorkspace.counts={active:1,raw_active:2};
 adsFilter='active';
 renderAdsWorkspace();
 assert.equal((nodes.adsList.innerHTML.match(/data-active-group=/g)||[]).length,1);
 assert(nodes.adsList.innerHTML.includes(sibling.ad_id));
 assert(nodes.adsList.innerHTML.includes('src="https://example.test/creative.jpg"'));
 assert(nodes.adsList.innerHTML.includes('AD · Display'));
 assert(nodes.adsList.innerHTML.includes('<summary>2 Meta Ad IDs ▾</summary>'));
 assert(nodes.adsSummary.innerHTML.includes('Meta-reported active creative groups: <b>1</b>'));
 assert(nodes.adsSummary.innerHTML.includes('Raw Meta Ad ID counts'));
 assert(nodes.adsSummary.innerHTML.includes('Meta-reported active: 2'));
 nodes.adsExactId.value=ad.ad_id; renderAdsWorkspace();
 assert(nodes.adsList.innerHTML.includes(`data-ad-id="${ad.ad_id}"`));
 assert(nodes.adsList.innerHTML.includes(`data-ad-id="${sibling.ad_id}"`));
 assert(nodes.adsList.innerHTML.includes('<details open>'));
 nodes.adsExactId.value='999'; renderAdsWorkspace();
 assert(nodes.adsList.innerHTML.includes('No ads match'));
 nodes.adsExactId.value='';
 adsFilter='unmapped'; await loadAdsWorkspace();
 assert(calls[1].url.includes('view=unmapped'));
 openAdsMapping(ad,true);
 assert(nodes.adsMappingChoices.innerHTML.includes('data-ads-route="7"'));
 await bindAdsMapping(7);
 const call=calls.find(value=>value.url.endsWith('/bind'));
 assert.equal(call.url,'/admin/instagram-ads/120246385008280540/bind');
 assert.deepEqual(JSON.parse(call.options.body),{routing_id:7,change_binding:false});
 assert.equal(call.options.headers['X-CSRF-Token'],'test-csrf');
 assert(!calls.some(value=>value.url==='/admin/instagram-routes'));
 const paired={...ad,effective_instagram_media_id:'180001',instagram_permalink_url:'https://www.instagram.com/p/Exact/'};
 const second={...paired,ad_id:'120246385008280541',mapped:true,routing_id:8};
 const group={identity:['180001',paired.instagram_permalink_url],verified_identity:true,member_ad_ids:[paired.ad_id,second.ad_id],delivering_ad_ids:[paired.ad_id]};
 adsWorkspace={items:[paired,second],routes:[route],running_groups:[group],counts:{running:1,raw_running:1,active:1,raw_active:2},delivery:{status:'available',unresolved_ad_ids:[]},sync:{}};
 adsFilter='running'; renderAdsWorkspace();
 assert(nodes.adsList.innerHTML.includes('DELIVERED TODAY'));
 assert(nodes.adsList.innerHTML.includes('data-ads-group-map'));
 assert(nodes.adsList.innerHTML.includes('Instagram media ID: 180001'));
 assert(nodes.adsSummary.innerHTML.includes('Running Now: <b>1</b>'));
 openAdsGroupMapping(paired.ad_id);
 assert(nodes.adsMappingIdentity.textContent.includes(paired.ad_id));
 assert(nodes.adsMappingIdentity.textContent.includes(second.ad_id));
 assert(nodes.adsMappingIdentity.textContent.includes('currently mapped to source 8'));
 const start=calls.length; let confirmation='';
 global.confirm=text=>{confirmation=text;return false;};
 await bindAdsMapping(7); assert.equal(calls.length,start);
 assert(confirmation.includes(paired.ad_id)&&confirmation.includes(second.ad_id)&&confirmation.includes('Selected existing source 7'));
 global.confirm=()=>true;
 global.fetch=async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>url.endsWith('/bind')?{binding:{ad_id:url.split('/').at(-2),routing_id:7,active:1}}:{...adsWorkspace}};};
 await bindAdsMapping(7);
 const individual=calls.slice(start).filter(value=>value.url.endsWith('/bind'));
 assert.equal(individual.length,2);
 assert.equal(individual[0].url,`/admin/instagram-ads/${paired.ad_id}/bind`);
 assert.equal(individual[1].url,`/admin/instagram-ads/${second.ad_id}/bind`);
 assert.deepEqual(JSON.parse(individual[1].options.body),{routing_id:7,change_binding:true});
 assert(individual.every(value=>!('ad_ids' in JSON.parse(value.options.body))));
 // Failed second request stops the loop, reports confirmed first ID, and disables blind retry.
 openAdsGroupMapping(paired.ad_id); let attempts=0;
 global.fetch=async(url,options)=>{if(url.endsWith('/bind')){attempts++;return {ok:attempts===1,json:async()=>({binding:{ad_id:paired.ad_id,routing_id:7,active:1}})};}return {ok:true,json:async()=>({...adsWorkspace})};};
 await bindAdsMapping(7);
 assert.equal(attempts,2); assert(nodes.adsMappingResult.textContent.includes(`Confirmed IDs: ${paired.ad_id}`));
 assert.equal(adsChoosingId,null); assert.equal(adsChoosingGroup,null);
 second.instagram_permalink_url='https://www.instagram.com/p/Conflict/';
 openAdsGroupMapping(paired.ad_id); assert.equal(adsChoosingGroup,null);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    target = tmp_path / "ads-behavior.js"
    target.write_text(fixture + SCRIPT.replace("<script>", "").replace("</script>", "") + checks, encoding="utf-8")
    result = subprocess.run([node, str(target)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_ads_workspace_script_parses_and_classic_nav_is_separate(store, tmp_path):
    page = TestClient(app_module.app).get("/admin").text
    assert '<span class="nav-label">Ads</span>' in page and 'id="tab-ads"' in page
    assert 'id="igObservedAds"' not in page
    assert "Enter an existing active source mapping ID" not in page
    assert "data-ads-route" in page and "Use Existing Mapping" in page
    search = re.search(r'<input id="adsExactId"[^>]+>', page).group()
    assert 'width:100%' in search and 'box-sizing:border-box' in search
    assert 'Manage one exact Ad ID' not in page
    assert '>Meta Active</button>' in page and '>Running Now</button>' in page
    dialog = re.search(r'<dialog id="adsMappingDialog"[^>]+>', page).group()
    for style in ('position:fixed', 'left:50%', 'top:50%', 'transform:translate(-50%, -50%)',
                  'margin:0', 'width:min(850px, 95vw)', 'max-height:90vh', 'overflow:auto'):
        assert style in dialog
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable; separate JavaScript gate is required")
    for index, script in enumerate(re.findall(r"<script>(.*?)</script>", page, re.S)):
        target = tmp_path / f"admin-{index}.js"
        target.write_text(script, encoding="utf-8")
        result = subprocess.run([node, "--check", str(target)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_active_grouping_preserves_raw_inventory_and_binding_authority(store, monkeypatch):
    sync(store, monkeypatch, [item(), item('200', id='901'),
                             item('300', effective_instagram_media_id='180002', instagram_permalink_url='https://www.instagram.com/p/Other/'),
                             item('400', effective='PAUSED')])
    target = route(store)
    store.bind_instagram_ad('200', target['routing_id'], bound_by='test')
    active = ads.ads_workspace(store, 'active')
    assert active['counts']['active'] == len(active['active_groups']) == 2
    assert active['counts']['raw_active'] == 3
    assert {tuple(g['member_ad_ids']) for g in active['active_groups']} == {('120246385008280540', '200'), ('300',)}
    assert len(active['items']) == 3
    assert len(ads.ads_workspace(store, 'all')['items']) == 4
    assert [r['ad_id'] for r in ads.ads_workspace(store, 'mapped')['items']] == ['200']
    assert len(ads.ads_workspace(store, 'unmapped')['items']) == 3
    assert [r['ad_id'] for r in ads.ads_workspace(store, 'inactive')['items']] == ['400']
    assert store.instagram_ad_binding_status('120246385008280540') == 'UNBOUND'


def test_active_groups_are_deterministic_and_fail_closed_on_conflicting_identity():
    rows = [dict(ad_id=str(i), active=True, **fields) for i, fields in enumerate([
        {'effective_instagram_media_id':'1','instagram_permalink_url':'https://www.instagram.com/p/A/'},
        {'effective_instagram_media_id':'1','instagram_permalink_url':'https://www.instagram.com/p/B/'},
        {'effective_instagram_media_id':'2','instagram_permalink_url':'https://www.instagram.com/p/A/'},
        {'instagram_permalink_url':'https://www.instagram.com/p/A/'},
        {'creative_id':'7'}, {'creative_id':'7'}, {}, {},
    ])]
    groups = ads.active_presentation_groups(rows)
    assert groups == ads.active_presentation_groups(list(reversed(rows)))
    assert len(groups) == 7
    assert sorted(id for group in groups for id in group['member_ad_ids']) == [str(i) for i in range(8)]
    assert any(group['member_ad_ids'] == ['4','5'] for group in groups)


def test_active_groups_use_canonical_permalink_and_partial_exact_identity():
    rows = [dict(ad_id=str(i), active=True, **fields) for i, fields in enumerate([
        {'effective_instagram_media_id':'1','instagram_permalink_url':'https://www.instagram.com/p/A/?utm_source=test'},
        {'effective_instagram_media_id':'1'},
        {'instagram_permalink_url':'https://www.instagram.com/p/A/'},
    ])]
    assert ads.active_presentation_groups(rows)[0]['member_ad_ids'] == ['0','1','2']


def delivery_day():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat()


def insight(ad_id, impressions='10', **extra):
    return {'ad_id': ad_id, 'impressions': impressions, 'date_start': delivery_day(), 'date_stop': delivery_day(), **extra}


def test_delivery_read_requires_positive_impressions_and_fresh_active_status(store, monkeypatch):
    calls = mock_pages(monkeypatch, [{'timezone_name': 'Asia/Kolkata'},
        {'data': [{'id':'1','effective_status':'ACTIVE'}, {'id':'2','effective_status':'PAUSED'}, {'id':'3','effective_status':'ACTIVE'}]},
        {'data':[insight('1'), insight('2'), insight('3','0')]}])
    result = ads.delivery_evidence()
    assert result['status'] == 'available' and result['ads'] == {'1': {'impressions':10}}
    assert result['reporting_date'] == delivery_day() and result['account_timezone'] == 'Asia/Kolkata'
    assert all(call.method == 'GET' and 'access_token' not in str(call.url) for call in calls)
    assert calls[-1].url.params['level'] == 'ad'
    assert json.loads(calls[-1].url.params['time_range']) == {'since':delivery_day(),'until':delivery_day()}
    assert 'private-marketing-fixture' not in json.dumps(result)
    assert store.list_instagram_ad_bindings() == [] and store.list_instagram_source_routes() == []


def test_delivery_pagination_reconstructs_safe_cursor_and_reads_all_pages(store, monkeypatch):
    calls = mock_pages(monkeypatch, [{'timezone_name':'Asia/Kolkata'},
        {'data':[{'id':'1','effective_status':'ACTIVE'}], 'paging':{'next':'https://graph.facebook.com/v25.0/act_12345/ads?after=s&access_token=private-marketing-fixture'}},
        {'data':[{'id':'2','effective_status':'ACTIVE'}]},
        {'data':[insight('1')], 'paging':{'next':'https://graph.facebook.com/v25.0/act_12345/insights?after=i&access_token=private-marketing-fixture'}},
        {'data':[insight('2')]}])
    result = ads.delivery_evidence()
    assert result['raw_running'] == 2
    assert calls[2].url.params['after'] == 's' and calls[4].url.params['after'] == 'i'
    assert all('access_token' not in str(call.url) for call in calls)


@pytest.mark.parametrize('failure', [
    (403, {'error':{'code':200,'message':'private-marketing-fixture'}}),
    {'data':'bad'}, {'data':[insight('1','-1')]},
    {'data':[insight('1',date_start='2000-01-01')]},
    {'data':[insight('1'), insight('1')]},
    {'data':[insight('1')], 'paging':{'next':'https://evil.test/insights?after=x'}},
    httpx.ConnectError('private-marketing-fixture'),
])
def test_delivery_fails_closed_without_partial_evidence_or_secrets(store, monkeypatch, failure):
    mock_pages(monkeypatch, [{'timezone_name':'Asia/Kolkata'}, {'data':[{'id':'1','effective_status':'ACTIVE'}]}, failure])
    result = ads.delivery_evidence()
    assert result['status'] == 'unavailable' and result['ads'] == {}
    assert 'private-marketing-fixture' not in json.dumps(result)


def test_running_groups_keep_verified_siblings_and_display_is_not_authority():
    rows = [{'ad_id':str(i),'effective_instagram_media_id':media,'instagram_permalink_url':url,
             'name':'Same display', 'thumbnail_url':'https://example.test/same.jpg'} for i, (media,url) in enumerate([
        ('1','https://www.instagram.com/p/A/'), ('1','https://www.instagram.com/p/A/?utm_source=test'),
        ('2','https://www.instagram.com/p/B/'), ('3',None), ('3',None)])]
    groups = ads.running_presentation_groups(rows, {'0':{},'2':{},'3':{},'4':{}})
    assert groups == ads.running_presentation_groups(list(reversed(rows)), {'0':{},'2':{},'3':{},'4':{}})
    assert len(groups) == 4
    paired = next(g for g in groups if '0' in g['member_ad_ids'])
    assert paired['member_ad_ids'] == ['0','1'] and paired['delivering_ad_ids'] == ['0'] and paired['verified_identity']
    assert all(not g['verified_identity'] for g in groups if '3' in g['member_ad_ids'] or '4' in g['member_ad_ids'])


def test_running_identity_conflicts_never_group_even_without_delivery_on_conflicting_row():
    rows = [{'ad_id':str(i),'effective_instagram_media_id':media,'instagram_permalink_url':url} for i,(media,url) in enumerate([
        ('1','https://www.instagram.com/p/A/'), ('1','https://www.instagram.com/p/A/'),
        ('1','https://www.instagram.com/p/B/'), ('2','https://www.instagram.com/p/A/')])]
    groups = ads.running_presentation_groups(rows, {'0':{},'1':{}})
    assert len(groups) == 2 and all(len(g['member_ad_ids']) == 1 and not g['verified_identity'] for g in groups)


def test_running_workspace_preserves_meta_inventory_routes_and_bindings(store, monkeypatch):
    sync(store, monkeypatch, [item(),item('200'),item('300',effective_instagram_media_id='2',instagram_permalink_url='https://www.instagram.com/p/B/')])
    before = store.list_instagram_ads_inventory('act_12345')
    monkeypatch.setattr(ads, 'delivery_evidence', lambda: {'status':'available','ads':{item()['id']:{'impressions':10}},'raw_running':1})
    workspace = ads.ads_workspace(store, 'running')
    assert workspace['counts']['running'] == 1 and workspace['counts']['raw_running'] == 1
    assert workspace['running_groups'][0]['member_ad_ids'] == [item()['id'],'200']
    assert workspace['counts']['active'] == 2 and workspace['counts']['raw_active'] == 3
    assert len(ads.ads_workspace(store, 'all')['items']) == 3
    assert store.list_instagram_ads_inventory('act_12345') == before
    assert store.list_instagram_ad_bindings() == [] and store.list_instagram_source_routes() == []
    monkeypatch.setattr(ads, 'delivery_evidence', lambda: {'status':'unavailable','ads':{},'error_category':'PERMISSION_DENIED'})
    workspace = ads.ads_workspace(store, 'running')
    assert workspace['counts']['running'] is None and workspace['items'] == []
    assert workspace['counts']['raw_active'] == 3
    monkeypatch.setattr(ads, 'delivery_evidence', lambda: {'status':'available','ads':{},'raw_running':0})
    assert ads.ads_workspace(store, 'running')['counts']['running'] == 0
    monkeypatch.setattr(ads, 'delivery_evidence', lambda: {'status':'available','ads':{'999':{'impressions':1}},'raw_running':1})
    workspace = ads.ads_workspace(store, 'running')
    assert workspace['counts']['running'] is None
    assert workspace['delivery']['unresolved_ad_ids'] == ['999']
    assert workspace['delivery']['resolved_group_count'] == 0
