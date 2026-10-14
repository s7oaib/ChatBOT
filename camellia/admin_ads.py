"""Classic Admin Ads workspace; all mutations reuse PR #58 endpoints."""
HTML = r'''
<div id="tab-ads" class="panel-card" data-group="ads" style="display:none;">
  <div class="admin-page-heading"><div><h2>Ads</h2><p>Map exact Meta Ad IDs to existing Instagram sources.</p></div>
    <button id="adsSyncButton" class="btn-secondary" type="button" onclick="syncAdsNow()">↻ Sync Now</button></div>
  <div id="adsSummary" class="gallery-toolbar" aria-label="Ads summary"></div>
  <div style="width:100%;margin:12px 0;"><label for="adsExactId" style="display:block;margin-bottom:6px;">Exact Meta Ad ID</label><input id="adsExactId" class="field-control" type="search" inputmode="numeric" placeholder="Search by exact Meta Ad ID" style="display:block;width:100%;box-sizing:border-box;" oninput="renderAdsWorkspace()"></div>
  <p class="ig-media-muted">Running Now means current Meta ACTIVE status, within the supplied Meta schedule, plus reported impressions today in the ad-account timezone; reporting may lag. End time is exclusive. Meta Active is technical inventory. Expand any card to manage individual exact Ad IDs.</p>
  <div class="gallery-toolbar" role="group" aria-label="Ads filters">
    <button class="btn-secondary active" data-ads-filter="running" onclick="setAdsFilter('running')">Running Now</button>
    <button class="btn-secondary" data-ads-filter="active" onclick="setAdsFilter('active')">Meta Active</button>
    <button class="btn-secondary" data-ads-filter="unmapped" onclick="setAdsFilter('unmapped')">Unmapped</button>
    <button class="btn-secondary" data-ads-filter="mapped" onclick="setAdsFilter('mapped')">Mapped</button>
    <button class="btn-secondary" data-ads-filter="inactive" onclick="setAdsFilter('inactive')">Inactive</button>
    <button class="btn-secondary" data-ads-filter="all" onclick="setAdsFilter('all')">All</button>
  </div>
  <p id="adsSyncStatus" class="ig-media-muted" role="status"></p>
  <div id="adsList" class="instagram-gallery"></div>
  <dialog id="adsMappingDialog" style="position:fixed;left:50%;top:50%;transform:translate(-50%, -50%);margin:0;width:min(850px, 95vw);max-height:90vh;overflow:auto;box-sizing:border-box;border:1px solid var(--border);border-radius:12px;padding:24px;">
    <div class="admin-page-heading"><h3>Use Existing Mapping</h3><button class="btn-secondary" onclick="document.getElementById('adsMappingDialog').close()">Close</button></div>
    <p id="adsMappingIdentity"></p><p class="source-map-help">Verify the exact Post, Reel or Story identity before selecting a mapping.</p>
    <div id="adsMappingChoices" class="instagram-gallery"></div>
    <p id="adsMappingResult" role="status"></p>
  </dialog>
</div>
'''

SCRIPT = r'''
<script>
let adsWorkspace = {items: [], routes: [], counts: {}, sync: {}};
let adsFilter = 'running';
let adsRequestGeneration = 0;
let adsSyncBusy = false;
let adsMutationBusy = false;
let adsChoosingId = null;
let adsChoosingGroup = null;
let adsExpiryTimer = null;
let adsDeliveryClock = null;

function expireRunningSchedules() {
    clearTimeout(adsExpiryTimer);
    if (adsFilter !== 'running' || !adsDeliveryClock) return;
    const now = adsDeliveryClock.checked + (performance.now() - adsDeliveryClock.received);
    const evidence = adsWorkspace.delivery?.ads || {};
    const ends = [];
    for (const [id, proof] of Object.entries(evidence)) {
        const end = Date.parse(proof.end_time || '');
        if (Number.isFinite(end)) {
            if (now >= end) delete evidence[id];
            else ends.push(end);
        }
    }
    adsWorkspace.running_groups = (adsWorkspace.running_groups || []).map(group => ({...group,
        delivering_ad_ids: group.delivering_ad_ids.filter(id => evidence[id])
    })).filter(group => group.delivering_ad_ids.length);
    if (adsWorkspace.delivery.status === 'available' && !adsWorkspace.delivery.unresolved_ad_ids?.length)
        adsWorkspace.counts.running = adsWorkspace.running_groups.length;
    if (adsWorkspace.delivery.status === 'available') adsWorkspace.counts.raw_running = Object.keys(evidence).length;
    if (ends.length) adsExpiryTimer = setTimeout(() => {
        expireRunningSchedules();
        renderAdsWorkspace();
        loadAdsWorkspace();
    }, Math.min(2147483647, Math.max(1, Math.min(...ends) - now)));
}

function setAdsFilter(view) {
    adsFilter = view;
    document.querySelectorAll('[data-ads-filter]').forEach(button => button.classList.toggle('active', button.dataset.adsFilter === view));
    loadAdsWorkspace();
}
async function loadAdsWorkspace() {
    if (adsFilter !== 'running') clearTimeout(adsExpiryTimer);
    if (!adminAuthenticated) return;
    const generation = ++adsRequestGeneration;
    try {
        const response = await fetch(`/admin/instagram-ads?view=${encodeURIComponent(adsFilter)}`, {headers:getAuthHeaders(), cache:'no-store'});
        if (!response.ok) throw Error('Ads inventory unavailable.');
        const payload = await response.json();
        if (generation !== adsRequestGeneration) return;
        adsWorkspace = payload;
        const checked = Date.parse(payload.delivery?.checked_at || '');
        adsDeliveryClock = Number.isFinite(checked) ? {checked, received:performance.now()} : null;
        expireRunningSchedules();
        renderAdsWorkspace();
    } catch (_) {
        if (generation === adsRequestGeneration) document.getElementById('adsSyncStatus').textContent = 'Could not refresh Ads. Previously loaded inventory is retained.';
    }
}
async function syncAdsNow() {
    if (adsSyncBusy) return;
    adsSyncBusy = true;
    const button = document.getElementById('adsSyncButton'); button.disabled = true; button.textContent = 'Syncing…';
    try {
        const response = await fetch('/admin/instagram-ads/sync', {method:'POST',headers:getAuthHeaders()});
        if (!response.ok) throw Error('Sync unavailable');
        const result = await response.json();
        await loadAdsWorkspace();
        if (result.status === 'busy') document.getElementById('adsSyncStatus').textContent += ' · A sync is already running.';
    } catch (_) { document.getElementById('adsSyncStatus').textContent = 'Sync unavailable. Last complete inventory and bindings are retained.'; }
    finally { adsSyncBusy = false; button.disabled = false; button.textContent = '↻ Sync Now'; }
}
function renderAdsWorkspace() {
    const rawCounts = [['all','All'],['raw_running','Schedule-eligible + delivered today + ACTIVE'],['raw_active','Meta-reported active'],['unmapped','Unmapped'],['mapped','Mapped'],['inactive','Inactive / ended']];
    document.getElementById('adsSummary').innerHTML = `<div style="width:100%;">Running Now: <b>${adsWorkspace.counts.running == null ? 'Not checked / unavailable' : Number(adsWorkspace.counts.running)}</b> creative groups · Meta-reported active creative groups: <b>${Number(adsWorkspace.counts.active || 0)}</b></div><div style="width:100%;"><b>Raw Meta Ad ID counts</b> · ${rawCounts.map(([key,label]) => `${label}: ${adsWorkspace.counts[key] == null ? 'Not checked' : Number(adsWorkspace.counts[key])}`).join(' · ')}</div>`;
    const sync = adsWorkspace.sync || {};
    document.getElementById('adsSyncStatus').textContent = `Last successful sync: ${sync.last_success_at || 'Never'}${sync.error_category ? ' · Sync warning: ' + sync.error_category + ' · Last complete inventory retained.' : ''}${sync.optional_fields_reduced ? ' · Some creative fields are unavailable.' : ''}`;
    const delivery = adsWorkspace.delivery || {};
    if (adsFilter === 'running') document.getElementById('adsSyncStatus').textContent += delivery.status === 'available' ? ` · Delivery window: ${delivery.reporting_date} (${delivery.account_timezone}) · Checked: ${delivery.checked_at}${delivery.unresolved_ad_ids?.length ? ' · Some delivering IDs lack inventory; Sync Now to resolve discovery.' : ''}` : ` · Delivery evidence unavailable: ${delivery.error_category || 'Not checked'}. Use Meta Active / All for retained inventory.`;
    const list = document.getElementById('adsList');
    const exactId = document.getElementById('adsExactId').value?.trim() || '';
    const renderAd = ad => {
        const image = ad.thumbnail_url || ad.mapping?.preview_url;
        const preview = image ? `<img class="ig-media-thumb" loading="lazy" src="${escapeHtml(image)}" alt="Ad preview">` : '<div class="ig-media-placeholder">Preview unavailable</div>';
        const identity = ad.instagram_permalink_url || ad.effective_instagram_media_id || 'Not supplied by Meta';
        const suggestedRoute = adsWorkspace.routes.find(route => route.routing_id === ad.suggested_routing_id);
        const linked = suggestedRoute ? `Linked Instagram content: FOUND · Existing content mapping: ${suggestedRoute.display_label || suggestedRoute.routing_topic || suggestedRoute.source_type}` : `Linked Instagram content: ${ad.identity_status === 'AMBIGUOUS' ? 'Ambiguous exact identity' : 'Not linked'}`;
        const mapping = ad.mapping ? `${ad.mapping.display_label || ad.mapping.routing_topic || 'Mapped source'} · ${ad.mapping.display_description || ad.mapping.source_description || ''}` : ad.mapped ? 'Mapping retained; source unavailable' : 'Not mapped';
        const inherited = ad.mapping ? `<details class="ads-source-config"><summary>Effective linked source responses · ${ad.binding_active ? 'binding on' : 'binding off'}</summary><p><b>Selected approved questions:</b></p>${(ad.mapping.display_approved_questions || []).map(item => `<details><summary>${escapeHtml(item.question)} · ${escapeHtml(item.qa_id)}</summary><p style="white-space:pre-wrap;">${escapeHtml(item.answer)}</p></details>`).join('') || '<p>No currently effective selected Q&amp;As.</p>'}</details>` : '';
        const action = ad.mapped ? '<button class="btn-secondary" data-ads-action="edit">Edit Mapping</button><button class="btn-secondary" data-ads-action="map">Change Mapping</button><button class="btn-secondary" data-ads-action="toggle">' + (ad.binding_active ? 'Disable Binding' : 'Enable Binding') + '</button><button class="btn-secondary" data-ads-action="history">History</button>' : '<button class="btn-primary" data-ads-action="map">Map Ad</button>';
        const suggested = ad.suggested_routing_id && !ad.mapped ? '<button class="btn-secondary" data-ads-action="suggestion">Use Existing Mapping</button>' : '';
        return `<article class="ig-media-card" data-ad-id="${escapeHtml(ad.ad_id)}"><div class="ig-media-preview">${preview}</div><div class="ig-media-main"><b>AD · ${escapeHtml(ad.name)}</b><div class="ig-media-summary">Meta Ad ID: ${escapeHtml(ad.ad_id)}</div><div class="ig-media-facts"><span>${ad.inventory_state === 'OBSERVED' ? 'OBSERVED' : ad.active ? 'ACTIVE' : 'INACTIVE'} · ${escapeHtml(ad.effective_status)}</span><span>${ad.mapped ? 'MAPPED ✓' : 'UNMAPPED'} · ${escapeHtml(ad.binding_status)}</span><span>Linked source: ${escapeHtml(ad.source_type || 'unknown')}</span></div><p style="overflow-wrap:anywhere;">${escapeHtml(identity)}</p><p>${escapeHtml(linked)}</p><p>Mapped to: ${escapeHtml(mapping)}${ad.mapped ? ` · Binding: ${ad.binding_active ? 'ON' : 'OFF'}` : ''}</p>${inherited}<small>Last synced: ${escapeHtml(ad.last_synced_at || 'Not yet synced')}</small></div><div class="ig-media-actions">${action}${suggested}</div></article>`;
    };
    const groups = adsFilter === 'running' ? adsWorkspace.running_groups : adsWorkspace.active_groups;
    if ((adsFilter === 'active' || adsFilter === 'running') && groups) {
        list.innerHTML = groups.filter(group => !exactId || group.member_ad_ids.includes(exactId)).map(group => {
            const members = group.member_ad_ids.map(id => adsWorkspace.items.find(ad => ad.ad_id === id)).filter(Boolean);
            const first = members[0];
            if (!first) return '';
            const imageMember = members.find(ad => ad.thumbnail_url || ad.mapping?.preview_url);
            const image = imageMember?.thumbnail_url || imageMember?.mapping?.preview_url;
            const preview = image ? `<img class="ig-media-thumb" loading="lazy" src="${escapeHtml(image)}" alt="Promoted creative preview">` : '<div class="ig-media-placeholder">Preview unavailable</div>';
            const displayName = members.find(ad => ad.name)?.name || 'Promoted creative';
            const running = adsFilter === 'running';
            const indicator = running ? `<p style="overflow-wrap:anywhere;">WITHIN META SCHEDULE + DELIVERED TODAY + currently Meta ACTIVE · Evidence IDs: ${group.delivering_ad_ids.map(id => escapeHtml(id)).join(', ')}. ${members.length} exact member IDs. Mapping: ${members.filter(ad => ad.mapped).length}/${members.length} bound.${group.verified_identity ? '' : ' Incomplete/conflicting identity: individual mapping only.'}</p><button class="btn-primary" data-ads-group-map="${escapeHtml(first.ad_id)}">Map Ad</button>` : '';
            return `<section class="ig-media-card" data-active-group="true"><div class="ig-media-preview">${preview}</div><div class="ig-media-main"><b>AD · ${escapeHtml(displayName)}</b><p style="overflow-wrap:anywhere;">Permalink: ${first.instagram_permalink_url ? `<a href="${escapeHtml(first.instagram_permalink_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(first.instagram_permalink_url)}</a>` : 'Unavailable'}<br>Instagram media ID: ${escapeHtml(first.effective_instagram_media_id || 'Unavailable')}</p>${indicator}<details${exactId ? ' open' : ''}><summary>${members.length} Meta Ad IDs ▾</summary>${members.map(renderAd).join('')}</details></div></section>`;
        }).join('');
    } else {
        list.innerHTML = (adsWorkspace.items || []).filter(ad => !exactId || ad.ad_id === exactId).map(renderAd).join('');
    }
    if (!list.innerHTML) list.innerHTML = '<p class="ig-media-muted">No ads match this filter. Exact IDs without delivery evidence remain searchable in Meta Active or All.</p>';
    list.querySelectorAll('[data-ads-group-map]').forEach(button => button.addEventListener('click', () => openAdsGroupMapping(button.dataset.adsGroupMap)));
    list.querySelectorAll('[data-ad-id]').forEach(card => card.querySelectorAll('[data-ads-action]').forEach(button => button.addEventListener('click', () => {
        const ad = adsWorkspace.items.find(item => item.ad_id === card.dataset.adId);
        const action = button.dataset.adsAction;
        if (action === 'map' || action === 'suggestion') openAdsMapping(ad, action === 'suggestion');
        else if (action === 'history') alert(ad.history.length ? ad.history.map(event => `${event.occurred_at}: ${event.action} · source ${event.routing_id}`).join('\n') : 'No binding changes recorded.');
        else if (action === 'toggle') toggleAdsBinding(ad);
        else if (action === 'edit') editAdsSource(ad);
    })));
}
function openAdsMapping(ad, suggestedOnly = false) {
    if (adsMutationBusy) return;
    adsChoosingGroup = null;
    adsChoosingId = ad.ad_id;
    const dialog = document.getElementById('adsMappingDialog');
    document.getElementById('adsMappingResult').textContent = '';
    document.getElementById('adsMappingIdentity').textContent = `Ad ${ad.ad_id} · ${ad.instagram_permalink_url || ad.effective_instagram_media_id || 'Meta has not supplied source identity'}`;
    const routes = adsWorkspace.routes.filter(route => !suggestedOnly || route.routing_id === ad.suggested_routing_id);
    const choices = document.getElementById('adsMappingChoices');
    choices.innerHTML = routes.map(route => `<article class="ig-media-card"><div class="ig-media-main">${route.preview_url ? `<img loading="lazy" src="${escapeHtml(route.preview_url)}" alt="Source preview" style="max-width:120px;max-height:120px;">` : ''}<b>${escapeHtml(route.display_label || route.routing_topic || route.source_type)}</b><p>${escapeHtml(route.display_description || route.source_description || 'No description')}</p><small style="overflow-wrap:anywhere;">${escapeHtml(route.source_type)} · ${escapeHtml(route.media_id || 'Media ID unavailable')}<br>${escapeHtml(route.canonical_url || route.shortcode || 'Permalink unavailable')}</small></div><button class="btn-primary" data-ads-route="${Number(route.routing_id)}">Use This Mapping</button></article>`).join('') || '<p>No active existing mappings are available.</p>';
    choices.querySelectorAll('[data-ads-route]').forEach(button => button.addEventListener('click', () => bindAdsMapping(Number(button.dataset.adsRoute))));
    dialog.showModal();
}
function openAdsGroupMapping(memberId) {
    if (adsMutationBusy) return;
    const group = (adsWorkspace.running_groups || []).find(item => item.member_ad_ids.includes(memberId));
    if (!group) return;
    const members = group.member_ad_ids.map(id => adsWorkspace.items.find(ad => ad.ad_id === id));
    if (members.some(ad => !ad)) return;
    if (!group.verified_identity || members.length === 1) { openAdsMapping(members[0]); return; }
    const first = members[0];
    if (!first.effective_instagram_media_id || !first.instagram_permalink_url || members.some(ad => ad.effective_instagram_media_id !== first.effective_instagram_media_id || ad.instagram_permalink_url !== first.instagram_permalink_url)) return;
    openAdsMapping(first);
    adsChoosingGroup = [...group.member_ad_ids];
    document.getElementById('adsMappingIdentity').textContent = `Verified creative · media ${first.effective_instagram_media_id} · ${first.instagram_permalink_url}\nAffected exact Meta Ad IDs:\n${members.map(ad => `${ad.ad_id} · ${ad.mapped ? 'currently mapped to source ' + ad.routing_id : 'unmapped'}`).join('\n')}`;
    document.getElementById('adsMappingIdentity').style.whiteSpace = 'pre-wrap';
}
async function bindAdsMapping(routingId) {
    if (adsMutationBusy) return;
    const ad = adsWorkspace.items.find(item => item.ad_id === adsChoosingId);
    const route = adsWorkspace.routes.find(item => item.routing_id === routingId);
    if (!ad || !route) return;
    const members = (adsChoosingGroup || [ad.ad_id]).map(id => adsWorkspace.items.find(item => item.ad_id === id));
    if (members.some(item => !item)) return;
    if (adsChoosingGroup && (!ad.effective_instagram_media_id || !ad.instagram_permalink_url || members.some(item => item.effective_instagram_media_id !== ad.effective_instagram_media_id || item.instagram_permalink_url !== ad.instagram_permalink_url))) return;
    const confirmation = `Bind individual exact Meta Ad IDs:\n${members.map(item => `${item.ad_id}${item.mapped ? ' · replace/reactivate existing binding to source ' + item.routing_id : ''}`).join('\n')}\nSelected existing source ${routingId}: ${route.display_label || route.routing_topic || route.source_type}\nMedia: ${route.media_id || 'Unavailable'}\nPermalink: ${route.canonical_url || route.shortcode || 'Unavailable'}\nConfirm each listed individual binding?`;
    if (!confirm(confirmation)) return;
    adsMutationBusy = true;
    const completed = [];
    try {
        for (const member of members) {
        const response = await fetch(`/admin/instagram-ads/${encodeURIComponent(member.ad_id)}/bind`, {method:'POST',headers:{...getAuthHeaders(),'Content-Type':'application/json'},body:JSON.stringify({routing_id:routingId,change_binding:member.mapped})});
        if (!response.ok) throw Error('Binding failed');
        const result = await response.json();
        if (result.binding?.ad_id !== member.ad_id || result.binding?.routing_id !== routingId || !result.binding?.active) throw Error('Binding verification failed');
        completed.push(member.ad_id);
        }
        document.getElementById('adsMappingDialog').close();
        await loadAdsWorkspace();
    } catch (_) {
        adsChoosingGroup = null; adsChoosingId = null;
        document.getElementById('adsMappingResult').textContent = `Stopped. Confirmed IDs: ${completed.join(', ') || 'None'}. The last attempted ID may have changed; remaining IDs were not attempted. Refresh and verify individual bindings before retrying.`;
        await loadAdsWorkspace();
    }
    finally { adsMutationBusy = false; }
}
async function toggleAdsBinding(ad) {
    if (adsMutationBusy || !confirm(`${ad.binding_active ? 'Disable' : 'Enable'} binding for Ad ${ad.ad_id}?`)) return;
    adsMutationBusy = true;
    try {
        const response = await fetch(`/admin/instagram-ads/${encodeURIComponent(ad.ad_id)}/active`, {method:'PATCH',headers:{...getAuthHeaders(),'Content-Type':'application/json'},body:JSON.stringify({active:!ad.binding_active})});
        if (!response.ok) throw Error('Update failed');
        await loadAdsWorkspace();
    } catch (_) { alert('Could not update binding. Refresh Ads before retrying.'); }
    finally { adsMutationBusy = false; }
}
async function editAdsSource(ad) {
    if (!ad.mapping) { alert('This source is unavailable. Choose an existing active mapping.'); return; }
    if (ad.mapping.source_type === 'story') {
        switchTab('tab-instagram-stories', null);
        await loadTopicsAndMenus();
        const source = {...ad.mapping,story_media_id:ad.mapping.media_id,mapping_status:ad.mapping.active ? 'MAPPED' : 'DISABLED'};
        igStorySources = [...igStorySources.filter(item => item.story_media_id !== source.story_media_id),source];
        openInstagramStoryMapping(source.story_media_id);
        return;
    }
    setInstagramContentSubview('routing');
    await loadAdRoutes();
    editInstagramRoute(ad.routing_id);
}
</script>
'''
