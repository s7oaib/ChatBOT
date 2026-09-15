"""
Guards for the /admin dashboard's shape and for its JavaScript surviving edits.

The existing dashboard tests grep the rendered HTML, which cannot see that the
JavaScript inside it does not parse — the documented failure mode where the
page renders perfectly and every button is dead because the whole <script>
aborted. Consolidating eight tabs into three reintroduced exactly that: a
removal left an orphan `}`, the HTML still looked right, and `switchTab` and
`verifyPin` were simply undefined. The balance check below is what would have
caught it without a browser.
"""
import re

import pytest
from fastapi.testclient import TestClient

import camellia.database as db_module
from camellia.app import app

db_module.db.init_db()
client = TestClient(app)


@pytest.fixture(scope="module")
def page():
    resp = client.get("/admin")
    assert resp.status_code == 200
    return resp.text


@pytest.fixture(scope="module")
def script(page):
    blocks = re.findall(r"<script>(.*?)</script>", page, re.DOTALL)
    assert blocks, "no inline script in the dashboard"
    return max(blocks, key=len)


def _strip_literals(js: str) -> str:
    """
    Removes comments and quoted strings so brackets inside them do not count.

    Deliberately crude — it only has to be good enough that a balanced file
    stays balanced, so that a genuine stray bracket stands out.
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    js = re.sub(r"(?m)//.*$", "", js)
    js = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", js)
    js = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', js)
    js = re.sub(r"`(?:\\.|[^`\\])*`", "``", js)
    return js


@pytest.mark.parametrize("opener,closer", [("{", "}"), ("(", ")"), ("[", "]")])
def test_dashboard_javascript_brackets_balance(script, opener, closer):
    """
    An unbalanced bracket kills the entire script, and every HTML-grepping test
    still passes while it does.
    """
    clean = _strip_literals(script)
    assert clean.count(opener) == clean.count(closer), (
        f"unbalanced {opener}{closer} in the dashboard script "
        f"({clean.count(opener)} vs {clean.count(closer)}) — the whole <script> "
        "will fail to parse and every button on the page will be dead"
    )


def test_the_dashboard_has_five_client_facing_tabs(page):
    """
    Eight tabs needed 1655px of nav on a 347px phone, so most of the portal was
    unreachable on the handset the kitchen actually uses. Menu Manager is its
    own tab rather than folded into Menu, so it does not disturb the existing
    Instagram Post & Ad Routing panel that already lives there.

    Customers and Answers were folded a second time, into AI Resolution &
    Answers, once every action either offered was confirmed reachable from
    there — see test_ai_resolution_tab_exists_and_reuses_existing_endpoints
    and the candidate/escalation/knowledge-gap regression tests below. Their
    old panels stay in the DOM behind the Advanced/Legacy disclosure, not in
    this main nav bar. Custom Q&A is the fifth: the client-managed reusable
    knowledge library, on the same learned_knowledge table.
    """
    nav = page[page.index('<div class="tabs-nav">'):page.index('</div>', page.index('<div class="tabs-nav">'))]
    buttons = re.findall(r'<button class="tab-btn[^"]*" onclick="switchTab\(', nav)
    assert len(buttons) == 5, f"expected 5 client-facing tabs, found {len(buttons)}"
    assert "Custom Q&amp;A" in nav


def test_menu_manager_sits_between_menu_and_ai_resolution(page):
    nav = page[page.index('<div class="tabs-nav">'):]
    nav = nav[:nav.index('</div>')]
    assert nav.index("tab-image") < nav.index("tab-menu-manager") < nav.index("tab-ai-resolution")
    assert "Menu Manager" in nav


def test_customers_and_answers_are_not_in_normal_client_navigation(page):
    """
    Their functions are not gone -- they are inside AI Resolution & Answers,
    reached through the exact same candidate/escalation/knowledge-gap/
    learned-knowledge endpoints. This only checks that a client opening the
    dashboard is no longer offered two extra buttons for workflows the
    unified tab already covers.
    """
    nav = page[page.index('<div class="tabs-nav">'):]
    nav = nav[:nav.index('</div>')]
    assert "💬 Customers" not in nav
    assert "✅ Answers <span" not in nav
    assert "🤖 AI Resolution &amp; Answers" in nav


def test_legacy_views_are_reachable_only_from_the_advanced_disclosure(page):
    """
    The old panels stay in the DOM for regression comparison during this
    transition, but only behind a collapsed, clearly-labelled disclosure --
    never as prominent client-facing navigation.
    """
    assert "Advanced / Legacy Views" in page
    assert "Open Legacy Customers View" in page
    assert "Open Legacy Answers View" in page
    assert "switchTab('tab-stats', null)" in page
    assert "switchTab('tab-candidates', null)" in page


def test_menu_manager_can_edit_preview_publish_and_restore(page):
    assert 'id="mmMenuList"' in page
    assert 'id="mmEditorItems"' in page
    assert 'id="mmPreviewText"' in page
    assert 'id="mmImageGrid"' in page
    assert "mmLoadMenuList()" in page
    assert "mmSaveDraft()" in page
    assert "mmPreview()" in page
    assert "mmPublish()" in page
    assert "mmRestoreVersion(" in page
    assert "mmReplaceImage(" in page
    assert "mmArchiveImage(" in page
    assert "mmHistoryImage(" in page


def test_ad_routing_panel_still_present_alongside_menu_manager(page):
    """Menu Manager must not have displaced the existing Post & Ad Routing UI."""
    assert 'id="tab-ad-routing"' in page
    assert "Instagram Post" in page or "Post &amp; Ad Routing" in page
    assert 'id="igPriorityProducts"' in page


def test_menu_dashboard_explains_how_to_hide_the_current_week(page):
    assert "Hide this week's menu from customers" in page
    assert "unpublishCurrentMenu" in page


def test_every_panel_belongs_to_a_tab(page):
    """A panel with no group is invisible for good — no tab can ever show it."""
    panels = re.findall(r'<div [^>]*class="panel-card"[^>]*>', page)
    panels += re.findall(r'<div class="panel-card"[^>]*>', page)
    panels = list(dict.fromkeys(panels))
    assert panels, "no panels found"

    ungrouped = [p for p in panels if "data-group=" not in p]
    assert not ungrouped, f"panels unreachable from any tab: {ungrouped}"

    groups = set(re.findall(r'data-group="(\w+)"', page))
    routed = set(re.findall(r"'tab-[\w-]+':\s*'(\w+)'", page))
    assert groups == routed, (
        f"panel groups {sorted(groups)} do not match the tabs that route to "
        f"them {sorted(routed)}"
    )


def test_each_tab_routes_to_a_group(page):
    """Every tab button must name a group switchTab knows about."""
    tab_ids = re.findall(r"switchTab\('([\w-]+)'", page)
    routed = dict(re.findall(r"'(tab-[\w-]+)':\s*'(\w+)'", page))
    for tab_id in tab_ids:
        assert tab_id in routed, f"tab button '{tab_id}' has no group in TAB_GROUPS"


def test_the_menu_tab_leads_with_the_weekly_job(page):
    """
    Panels in a group are stacked, so DOM order is reading order. Uploading the
    flyer is the recurring task and belongs above the alternatives to it.
    """
    order = re.findall(r'<div [^>]*id="(tab-[\w-]+)"[^>]*class="panel-card"|'
                       r'<div class="panel-card" id="(tab-[\w-]+)"', page)
    ids = [a or b for a, b in order]
    assert ids.index("tab-image") < ids.index("tab-paste"), (
        "the paste-text panel sits above the flyer upload"
    )
    assert ids.index("tab-candidates") < ids.index("tab-knowledge"), (
        "the hand-written form sits above the review queue"
    )


def test_menu_tab_has_simple_ad_routing_controls(page):
    assert 'id="tab-ad-routing"' in page
    assert 'id="igRouteUrl"' in page
    assert 'id="igProductPicker"' in page
    assert "saveInstagramRoute()" in page
    assert "loadAdRoutes()" in page
    assert "product_catalogue" in page
    assert "igProducts.map" in page


def test_stories_are_not_offered_or_shown_in_content_routing_tab(page):
    """Architectural requirement: Stories are managed only in the dedicated
    Instagram Stories tab. 📣 Instagram Content Routing must not offer
    'story' as a source type, must not carry Story-specific mapping
    controls, and must not render a duplicate Story listing -- Posts,
    Reels and Ads stay fully functional there."""
    section = page[page.index('id="tab-ad-routing"'):page.index('id="tab-instagram-stories"')]
    source_select = section[section.index('id="igSourceType"'):section.index('</select>', section.index('id="igSourceType"'))]
    assert '<option value="post">' in source_select
    assert '<option value="reel">' in source_select
    assert '<option value="ad">' in source_select
    assert 'value="story"' not in source_select
    assert 'igStoryMapForm' not in section
    assert 'igStoryMediaId' not in section
    assert 'igStorySourceList' not in section
    # The registry list in this tab must never render a Story card even if
    # a story-typed row somehow reaches it client-side.
    registry = page[page.index('function renderInstagramMediaRegistry'):page.index('async function loadAdRoutes')]
    assert "route.source_type !== 'story'" in registry
    # The dead fallback that used to let this form silently switch itself
    # into story mode (from a code path the dedicated tab has replaced)
    # must be gone, not just unreachable.
    assert 'useCapturedStory' not in page
    assert "igSourceType').value = 'story'" not in page


def test_instagram_stories_has_a_dedicated_top_level_tab_and_panel(page):
    assert "switchTab('tab-instagram-stories', this)" in page
    assert 'id="tab-instagram-stories"' in page
    assert 'data-group="instagram_stories"' in page
    assert "'tab-instagram-stories': 'instagram_stories'" in page
    # Opening the tab must trigger the active Meta sync (automatic Story
    # discovery), not just a passive reload of whatever the DB already has.
    assert "if (group === 'instagram_stories') { syncAndLoadInstagramStories(); }" in page
    assert "fetch('/admin/instagram-story-sources?include_expired=true'" in page
    assert "fetch('/admin/instagram-story-sources/sync'" in page


def test_instagram_stories_map_flow_uses_a_read_only_captured_id(page):
    section = page[page.index('id="tab-instagram-stories"'):page.index('<!-- MENU tab, panel 3 of 4')]
    assert 'id="igStoryMapForm"' in section
    assert 'id="igStoryMediaId"' in section and 'readonly' in section
    for field in ("igStoryTopicPicker", "igStoryPriorityPicker", "igStoryKnowledgeScope", "igStoryMenuMode", "igStoryMenuPicker", "igStoryEscalationPolicy", "igStoryActive"):
        assert f'id="{field}"' in section
    assert "media_id: mediaId" in page
    assert 'onclick="editInstagramRoute(' not in section


def test_instagram_story_menu_mode_offers_inherit_none_and_selected(page):
    """
    Defect 1 (P1): the picker's empty option used to be labelled "Inherit
    Topic Menu" but always submitted menu_mode='NONE' -- INHERIT was
    unreachable. A dedicated menu-mode select now carries all three states
    explicitly; tests/test_instagram_story_runtime.py proves the actual
    submit/edit-reload behavior by executing the real JS.
    """
    section = page[page.index('id="tab-instagram-stories"'):page.index('<!-- MENU tab, panel 3 of 4')]
    mode_select = section[section.index('id="igStoryMenuMode"'):section.index('</select>', section.index('id="igStoryMenuMode"'))]
    assert '<option value="INHERIT">Inherit Topic Menu</option>' in mode_select
    assert '<option value="NONE">No Menu</option>' in mode_select
    assert '<option value="SELECTED">Select Menu</option>' in mode_select
    assert 'id="igStoryMenuPickerWrap"' in section

    script = page[page.index('function onInstagramStoryMenuModeChange'):page.index('function renderInstagramStoryPriorityProducts')]
    assert "igStoryMenuMode" in script

    save = page[page.index('async function saveInstagramStoryMapping'):page.index('async function setInstagramStoryActive')]
    assert "menu_mode: menuMode" in save
    assert "menuMode === 'SELECTED' ? menuId : null" in save
    assert "menuMode === 'SELECTED' && !menuId" in save, "SELECTED without a chosen menu must be blocked before submit"

    edit = page[page.index('function openInstagramStoryMapping'):page.index('function cancelInstagramStoryMapping')]
    assert "source.menu_mode" in edit
    assert "onInstagramStoryMenuModeChange()" in edit


def test_instagram_story_refresh_does_not_clear_the_list_before_it_settles(page):
    """
    Defect 2 (P2): loadInstagramStories() used to overwrite the visible
    Story list with "Loading..." immediately, so a failed refresh replaced
    good data with an error. tests/test_instagram_story_runtime.py executes
    the real function against mocked fetch failures; this only locks the
    structural shape (a separate status element, list only replaced on
    success) that makes that behavior possible.
    """
    assert 'id="igStoryRefreshStatus"' in page
    load = page[page.index('async function loadInstagramStories'):page.index('function renderInstagramMediaRegistry')]
    assert "igStoriesEverLoaded" in load
    assert "setInstagramStoryRefreshStatus" in load
    assert "igStoryList" in load
    # The list must only be blanked to "Loading..." on the very first load
    # (gated by isFirstLoad), never unconditionally at the top of every
    # call the way the original defect did it.
    assert load.index("isFirstLoad") < load.index("Loading captured Stories")
    before_gate = load[:load.index("if (isFirstLoad)")]
    assert "list.innerHTML" not in before_gate
    assert "Could not refresh Stories. Showing last loaded data." in load


def test_instagram_stories_exposes_status_filters_and_safe_actions(page):
    section = page[page.index('id="tab-instagram-stories"'):page.index('<!-- MENU tab, panel 3 of 4')]
    render = page[page.index('function renderInstagramStories'):page.index('async function loadInstagramStories')]
    for label in ("All", "Unmapped", "Mapped", "Disabled", "Archived", "Map Story", "Edit Mapping", "Disable", "Enable", "Archive", "Restore"):
        assert label in section or label in render
    assert 'data-story-action="delete"' not in render
    assert "/permanent" not in render
    assert "setInstagramStoryActive" in render
    assert "archiveInstagramStory" in render
    assert "restoreInstagramStory" in render


def test_instagram_stories_refreshes_from_source_and_mapping_endpoints(page):
    section = page[page.index('async function loadInstagramStories'):page.index('function renderInstagramMediaRegistry')]
    assert "igStoryRefreshButton" in page
    assert "instagram-story-sources" in section
    assert "instagram-routes?include_archived=true" in section
    assert "populateInstagramStoryPickers" in section
    assert "renderInstagramStories()" in section


def test_instagram_story_tab_open_and_refresh_both_trigger_active_meta_sync(page):
    """Automatic discovery: opening the tab or pressing Refresh must call
    the active Meta sync endpoint, not just reload whatever is already in
    the database -- a customer reply must never be required to discover a
    Story that is currently live on the connected account."""
    sync_fn = page[page.index('async function syncAndLoadInstagramStories'):page.index('async function loadInstagramStories')]
    assert "/admin/instagram-story-sources/sync" in sync_fn
    assert "method: 'POST'" in sync_fn
    assert "await loadInstagramStories()" in sync_fn
    assert "document.getElementById('igStoryRefreshButton')?.addEventListener('click', syncAndLoadInstagramStories)" in page


def test_instagram_story_preview_metadata_never_exposes_temporary_url(page):
    section = page[page.index('function storyPreviewMetadata'):page.index('function storyMatches')]
    assert "new URL(String(value), window.location.origin)" in section
    assert "parsed.hostname" in section and "parsed.pathname" in section
    story_render = page[page.index('function renderInstagramStories'):page.index('async function loadInstagramStories')]
    assert "storyPreviewMetadata(source.temporary_url)" in story_render
    assert "temporary_url}" not in story_render


def test_the_fake_flyer_test_button_is_gone(page):
    """
    It drew a sample flyer on a canvas and posted it through the real upload
    endpoint, which replaces the photo customers are sent and — when the read
    succeeds — the dish list with invented prices. Not something to leave in a
    kitchen portal behind the word "test".
    """
    assert "testSampleFlyer" not in page
    assert "1-Click Test" not in page


def test_no_implementation_jargon_in_the_headings(page):
    """The kitchen does not need to know what OCR is to post a menu."""
    headings = re.findall(r"<h2>(.*?)</h2>", page, re.DOTALL)
    joined = " ".join(headings)
    for word in ("Vision OCR", "Self-Healing", "Bot Intelligence"):
        assert word not in joined, f"{word!r} still shown to the operator"


def test_the_status_bar_carries_no_hardcoded_lights(page):
    """
    "Server: Online" restated that the page had loaded, and "Photo OCR: Ready"
    was hardcoded — it said Ready with no key configured, with the quota spent,
    and with the model failing, which are exactly the conditions that made
    uploads silently stop extracting dishes. A light that cannot go red is
    worse than no light.
    """
    assert "Server: <b>Online</b>" not in page
    assert "Photo OCR: <b>Ready</b>" not in page


def test_the_reader_light_is_driven_by_health(page):
    """It has to read real state, and say so when it cannot read any."""
    assert 'id="ocrStatus"' in page and 'id="ocrDot"' in page
    assert "async function refreshReaderStatus" in page
    assert "fetch('/health')" in page

    reader = page[page.index("async function refreshReaderStatus"):]
    reader = reader[:reader.index("window.addEventListener")]
    # Each state the operator can actually be in.
    for phrase in ("not set up", "having trouble", "ready", "unknown"):
        assert phrase in reader, f"reader light cannot report {phrase!r}"
    assert "dot red" in reader, "the light has no failure colour"


def test_status_pills_wrap_as_units(page):
    """
    Without this the row stayed unbroken on a phone and the labels broke
    instead — "Active Menu Items:" split across three lines.
    """
    assert "white-space: nowrap" in page
    css = page[page.index(".status-items"):]
    css = css[:css.index("}") + 1]
    assert "flex-wrap: wrap" in css, "status pills cannot wrap onto a second row"


def test_the_dashboard_is_never_cached():
    """
    It shipped with no Cache-Control, no ETag and no Last-Modified, so a browser
    could keep serving the copy it already had. A deployed dashboard change then
    looks like a failed deploy — the server has the new page, the phone does not.
    """
    resp = client.get("/admin")
    cache = resp.headers.get("cache-control", "")
    assert "no-store" in cache, f"dashboard is cacheable: {cache!r}"


def test_instagram_routing_has_a_simple_client_first_default_view(page):
    """The normal route workflow is link -> topic -> save; technical fields stay disclosed."""
    section = page[page.index('id="tab-ad-routing"'):page.index('<!-- MENU tab, panel 3 of 4')]
    assert "Instagram Link" in section
    assert "Instagram Post / Reel Link" not in section
    assert ">Identify<" not in section
    assert "id=\"igTopicPicker\"" in section and "+ Add Topic" in section
    assert "Save Routing" in section
    assert "Preview Reply" in section
    assert "Preview Customer Response" not in section
    assert '<details id="igCustomize"' in section and '<details id="igCustomize" open' not in section
    assert '<details id="igAdvancedSettings"' in section and '<details id="igAdvancedSettings" open' not in section
    assert "Advanced settings are normally filled automatically" in section


def test_instagram_routing_keeps_client_friendly_cards_and_existing_actions(page):
    section = page[page.index('function renderInstagramMediaCard'):page.index('// Shared-post pairing')]
    assert "View Details" in section
    for action in ("Edit", "Disable", "Archive", "Status:", "Menu:", "Priority:"):
        assert action in section
    assert "/admin/instagram-routes/${id}/active" in page
    assert "/admin/instagram-routes/${id}" in page
    assert "/admin/instagram-routes/${routingId}/pairing/start" in page
    assert "Save Changes" in page


def test_instagram_routing_has_unified_connected_media_registry(page):
    section = page[page.index('function renderInstagramMediaCard'):page.index('// Shared-post pairing')]
    for marker in (
        "Connected Instagram media filters", "igMediaSearch", "Posts", "Reels", "Ads",
        "ig-media-card", "preview_url", "POST", "REEL", "Open Instagram",
        "/metadata/refresh", "Add Cover", "Change Cover",
        "Save Cover", "Cancel", "Uploading...", "/preview", "Remove Cover",
        "target=\"_blank\"",
    ):
        assert marker in page, marker
    assert "lookup_instagram_media" not in section[section.index("async function loadAdRoutes"):]


def test_instagram_refresh_capability_is_preserved_but_hidden_from_normal_ui(page):
    assert "Refresh Media Preview" not in page
    assert "function refreshInstagramRouteMetadata" in page
    assert "/admin/instagram-routes/${id}/metadata/refresh" in page


def test_instagram_cover_management_is_inline_and_admin_only(page):
    section = page[page.index('function renderInstagramMediaCard'):page.index('// Shared-post pairing')]
    for marker in (
        "openInstagramCoverModal", "previewInstagramCoverFile", "saveInstagramCover",
        "Cover updated", "Remove this cover image? Routing will not be affected.",
        "Admin-only visual metadata", "ig-cover-modal-preview", "MAX_INSTAGRAM_COVER_UPLOAD_BYTES",
    ):
        assert marker in page, marker
    assert "route.routing_id" in section
    assert "route.source_type === 'post' || route.source_type === 'reel'" in section
    assert "igMediaFilter" in page


def test_instagram_routing_delete_is_client_friendly_and_scoped(page):
    section = page[page.index('id="tab-ad-routing"'):page.index('<!-- MENU tab, panel 3 of 4')]
    assert page.count("openIgRemoveModal") >= 3, "active and archived cards need Delete"
    assert "Delete this Instagram routing connection?" in page
    assert "Topic:" in page and "Instagram content type:" in page and "Menu:" in page and "Status:" in page
    assert "This only removes this Instagram routing connection." in page
    assert "It will not delete the Instagram content, Topic, Menu, products, Custom Q&amp;A, or customer conversations." in page
    assert ">Cancel</button>" in page
    assert "Delete Routing" in page
    assert "confirmIgRemove()" in page
    assert "method: 'DELETE'" in page and "/admin/instagram-routes/${id}/permanent" in page
    assert "✓ Instagram routing deleted" in page
    assert "Remove Permanently" not in page


def test_topic_preview_exposes_reply_order_and_uses_the_preview_endpoint(page):
    section = page[page.index('id="tab-ad-routing"'):page.index('<!-- MENU tab, panel 3 of 4')]
    assert "Reply Order:" in section
    assert "⇅ Swap Order" in section
    assert "Description First" in page and "Menu First" in page
    assert "swapIgResponseOrder" in page
    assert "response_order: igResponseOrder" in page


def test_both_dashboard_routes_send_the_same_no_cache_headers():
    """/admin and /admin/dashboard are the same page; one must not be stale."""
    for path in ("/admin", "/admin/dashboard"):
        assert "no-store" in client.get(path).headers.get("cache-control", ""), path


def test_ai_resolution_tab_exists_and_reuses_existing_endpoints(page):
    """
    AI Resolution & Answers merges candidate_templates, escalations and
    knowledge_gaps into one queue. It must not introduce a parallel workflow:
    every action it offers has to call the same endpoints the older Answers
    and Customers tabs already use to approve/reject/edit/resolve.
    """
    assert 'id="tab-ai-resolution"' in page
    assert "AI Resolution &amp; Answers" in page
    assert "'tab-ai-resolution': 'ai_resolution'" in page

    script = page[page.index("async function loadAiResolution"):]
    assert "fetch('/admin/candidates'" in script
    assert "fetch('/admin/escalations?status=pending" in script
    assert "fetch('/admin/knowledge-gaps?status=OPEN" in script

    assert "fetch(`/admin/candidates/${id}/approve`" in page
    assert "fetch(`/admin/candidates/${id}`" in page
    assert "fetch(`/admin/candidates/${id}/reject`" in page
    assert "fetch(`/admin/knowledge-gaps/${gapId}/status`" in page
    assert "onclick=\"openResolveModal(" in script, "gap/escalation rows must reuse the existing resolve modal"


def test_triage_shadow_section_is_present_and_strictly_read_only(page):
    """
    Phase 3 slice 2: the triage model's shadow categorization has to be
    visible inside AI Resolution & Answers, but it must never gain an
    approve/reject/resolve action of its own -- a shadow row was never acted
    on by anything, and the dashboard must not become the first thing that
    does before an auto-resolve path exists.
    """
    assert 'id="triageShadowTableBody"' in page
    assert "Triage Shadow Mode" in page
    assert "measurement only" in page

    section = page[page.index("async function loadTriageShadow"):]
    section = section[:section.index("// CUSTOM Q&A:")]
    assert "fetch('/admin/triage-shadow" in section
    for verb in ("method: 'POST'", "method: 'PATCH'", "method: 'DELETE'", "confirm("):
        assert verb not in section, f"triage shadow section must stay read-only, found {verb!r}"


def test_custom_qa_tab_exists_and_reuses_the_same_learned_knowledge_endpoints(page):
    """
    Custom Q&A must not be a second knowledge system: every action it offers
    has to call /admin/custom-qa, which is backed by the same
    learned_knowledge table (and matcher) Teach & Resolve and candidate
    approval already use.
    """
    assert 'id="tab-custom-qa"' in page
    assert "Custom Q&amp;A" in page
    assert "'tab-custom-qa': 'custom_qa'" in page
    assert "Create and manage approved answers the chatbot can reuse" in page

    script = page[page.index("async function loadCustomQa"):]
    assert "fetch(`/admin/custom-qa?status=" in script
    assert "fetch('/admin/custom-qa" in page or "fetch(`/admin/custom-qa" in page
    assert "/admin/custom-qa/${id}/status" in page or "/admin/custom-qa/${item.id}" in page
    assert "/admin/custom-qa/export.csv" in page
    assert "/admin/custom-qa/csv-template" in page
    assert "/admin/custom-qa/import/dry-run" in page
    assert "/admin/custom-qa/import/confirm" in page
    assert "/admin/custom-qa/test" in page
    assert "/admin/custom-qa/preview" in page
    assert "/versions" in page and "/restore/" in page


def test_custom_qa_lifecycle_and_delete_confirmation_are_present(page):
    assert "Delete this Custom Q&amp;A permanently?" in page
    assert "This permanently removes this Q&amp;A and it will no longer be available to the chatbot." in page
    assert "cqaSetStatus" in page
    assert "customQaHistoryModal" in page
