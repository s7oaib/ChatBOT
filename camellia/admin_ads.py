"""Classic Admin Ads workspace; all mutations reuse PR #58 endpoints."""
HTML = r'''
<div id="tab-ads" class="panel-card" data-group="ads" style="display:none;">
  <div class="admin-page-heading"><div><h2>Ads</h2><p>Map exact Meta Ad IDs to existing Instagram sources.</p></div>
    <button id="adsSyncButton" class="btn-secondary" type="button" onclick="syncAdsNow()">↻ Sync Now</button></div>
  <div id="adsSummary" class="gallery-toolbar" aria-label="Ads summary"></div>
  <div class="gallery-toolbar" role="group" aria-label="Ads filters">
    <button class="btn-secondary active" data-ads-filter="active" onclick="setAdsFilter('active')">Active</button>
    <button class="btn-secondary" data-ads-filter="unmapped" onclick="setAdsFilter('unmapped')">Unmapped</button>
    <button class="btn-secondary" data-ads-filter="mapped" onclick="setAdsFilter('mapped')">Mapped</button>
    <button class="btn-secondary" data-ads-filter="inactive" onclick="setAdsFilter('inactive')">Inactive</button>
    <button class="btn-secondary" data-ads-filter="all" onclick="setAdsFilter('all')">All</button>
  </div>
  <p id="adsSyncStatus" class="ig-media-muted" role="status"></p>
  <div id="adsList" class="instagram-gallery"></div>
  <dialog id="adsMappingDialog" style="max-width:850px;width:90%;border:1px solid var(--border);border-radius:12px;padding:24px;">
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
let adsFilter = 'active';
let adsRequestGeneration = 0;
let adsSyncBusy = false;
let adsMutationBusy = false;
let adsChoosingId = null;

function setAdsFilter(view) {
    adsFilter = view;
    document.querySelectorAll('[data-ads-filter]').forEach(button => button.classList.toggle('active', button.dataset.adsFilter === view));
    loadAdsWorkspace();
}
async function loadAdsWorkspace() {
    if (!adminAuthenticated) return;
    const generation = ++adsRequestGeneration;
    try {
        const response = await fetch(`/admin/instagram-ads?view=${encodeURIComponent(adsFilter)}`, {headers:getAuthHeaders(), cache:'no-store'});
        if (!response.ok) throw Error('Ads inventory unavailable.');
        const payload = await response.json();
        if (generation !== adsRequestGeneration) return;
        adsWorkspace = payload;
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
    const summary = [['active','Active'],['unmapped','Unmapped'],['mapped','Mapped'],['inactive','Inactive / Ended']];
    document.getElementById('adsSummary').innerHTML = summary.map(([key,label]) => `<span class="ig-media-muted">${label}: <b>${Number(adsWorkspace.counts[key] || 0)}</b></span>`).join('');
    const sync = adsWorkspace.sync || {};
    document.getElementById('adsSyncStatus').textContent = `Last successful sync: ${sync.last_success_at || 'Never'}${sync.error_category ? ' · Sync warning: ' + sync.error_category + ' · Last complete inventory retained.' : ''}${sync.optional_fields_reduced ? ' · Some creative fields are unavailable.' : ''}`;
    const list = document.getElementById('adsList');
    list.innerHTML = (adsWorkspace.items || []).map(ad => {
        const image = ad.thumbnail_url || ad.mapping?.preview_url;
        const preview = image ? `<img class="ig-media-thumb" loading="lazy" src="${escapeHtml(image)}" alt="Ad preview">` : '<div class="ig-media-placeholder">Preview unavailable</div>';
        const identity = ad.instagram_permalink_url || ad.effective_instagram_media_id || 'Not supplied by Meta';
        const suggestedRoute = adsWorkspace.routes.find(route => route.routing_id === ad.suggested_routing_id);
        const linked = suggestedRoute ? `Linked Instagram content: FOUND · Existing content mapping: ${suggestedRoute.routing_topic || suggestedRoute.source_type}` : `Linked Instagram content: ${ad.identity_status === 'AMBIGUOUS' ? 'Ambiguous exact identity' : 'Not linked'}`;
        const mapping = ad.mapping ? `${ad.mapping.routing_topic || 'Existing mapping'} · ${ad.mapping.source_description || ''}` : ad.mapped ? 'Mapping retained; source unavailable' : 'Not mapped';
        const action = ad.mapped ? '<button class="btn-secondary" data-ads-action="edit">Edit Mapping</button><button class="btn-secondary" data-ads-action="map">Change Mapping</button><button class="btn-secondary" data-ads-action="toggle">' + (ad.binding_active ? 'Disable Binding' : 'Enable Binding') + '</button><button class="btn-secondary" data-ads-action="history">History</button>' : '<button class="btn-primary" data-ads-action="map">Map Ad</button>';
        const suggested = ad.suggested_routing_id && !ad.mapped ? '<button class="btn-secondary" data-ads-action="suggestion">Use Existing Mapping</button>' : '';
        return `<article class="ig-media-card" data-ad-id="${escapeHtml(ad.ad_id)}"><div class="ig-media-preview">${preview}</div><div class="ig-media-main"><b>AD · ${escapeHtml(ad.name)}</b><div class="ig-media-summary">Meta Ad ID: ${escapeHtml(ad.ad_id)}</div><div class="ig-media-facts"><span>${ad.inventory_state === 'OBSERVED' ? 'OBSERVED' : ad.active ? 'ACTIVE' : 'INACTIVE'} · ${escapeHtml(ad.effective_status)}</span><span>${ad.mapped ? 'MAPPED ✓' : 'UNMAPPED'} · ${escapeHtml(ad.binding_status)}</span><span>Linked source: ${escapeHtml(ad.source_type || 'unknown')}</span></div><p style="overflow-wrap:anywhere;">${escapeHtml(identity)}</p><p>${escapeHtml(linked)}</p><p>Mapped to: ${escapeHtml(mapping)}${ad.mapped ? ` · Binding: ${ad.binding_active ? 'ON' : 'OFF'}` : ''}</p><small>Last synced: ${escapeHtml(ad.last_synced_at || 'Not yet synced')}</small></div><div class="ig-media-actions">${action}${suggested}</div></article>`;
    }).join('') || '<p class="ig-media-muted">No ads match this filter. Sync Now discovers ads before a customer sends a DM.</p>';
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
    adsChoosingId = ad.ad_id;
    const dialog = document.getElementById('adsMappingDialog');
    document.getElementById('adsMappingResult').textContent = '';
    document.getElementById('adsMappingIdentity').textContent = `Ad ${ad.ad_id} · ${ad.instagram_permalink_url || ad.effective_instagram_media_id || 'Meta has not supplied source identity'}`;
    const routes = adsWorkspace.routes.filter(route => !suggestedOnly || route.routing_id === ad.suggested_routing_id);
    const choices = document.getElementById('adsMappingChoices');
    choices.innerHTML = routes.map(route => `<article class="ig-media-card"><div class="ig-media-main">${route.preview_url ? `<img loading="lazy" src="${escapeHtml(route.preview_url)}" alt="Source preview" style="max-width:120px;max-height:120px;">` : ''}<b>${escapeHtml(route.routing_topic || route.source_type)}</b><p>${escapeHtml(route.source_description || 'No description')}</p><small style="overflow-wrap:anywhere;">${escapeHtml(route.source_type)} · ${escapeHtml(route.media_id || 'Media ID unavailable')}<br>${escapeHtml(route.canonical_url || route.shortcode || 'Permalink unavailable')}</small></div><button class="btn-primary" data-ads-route="${Number(route.routing_id)}">Use This Mapping</button></article>`).join('') || '<p>No active existing mappings are available.</p>';
    choices.querySelectorAll('[data-ads-route]').forEach(button => button.addEventListener('click', () => bindAdsMapping(Number(button.dataset.adsRoute))));
    dialog.showModal();
}
async function bindAdsMapping(routingId) {
    if (adsMutationBusy) return;
    const ad = adsWorkspace.items.find(item => item.ad_id === adsChoosingId);
    const route = adsWorkspace.routes.find(item => item.routing_id === routingId);
    if (!ad || !route || !confirm(`Bind exact Ad ${ad.ad_id} to ${route.canonical_url || route.media_id || route.shortcode}?`)) return;
    adsMutationBusy = true;
    try {
        const response = await fetch(`/admin/instagram-ads/${encodeURIComponent(ad.ad_id)}/bind`, {method:'POST',headers:{...getAuthHeaders(),'Content-Type':'application/json'},body:JSON.stringify({routing_id:routingId,change_binding:ad.mapped})});
        if (!response.ok) throw Error('Binding failed');
        const result = await response.json();
        if (result.binding?.ad_id !== ad.ad_id || result.binding?.routing_id !== routingId || !result.binding?.active) throw Error('Binding verification failed');
        document.getElementById('adsMappingDialog').close();
        await loadAdsWorkspace();
    } catch (_) { document.getElementById('adsMappingResult').textContent = 'Could not confirm binding. Refresh Ads before retrying.'; }
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
