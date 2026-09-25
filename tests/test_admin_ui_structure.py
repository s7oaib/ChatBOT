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
import inspect

import pytest
from fastapi.testclient import TestClient

import camellia.database as db_module
from camellia import instagram_routing
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


def test_the_dashboard_has_a_coherent_sidebar_navigation(page):
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
    nav = page[page.index('<aside class="admin-sidebar"'):page.index('</aside>', page.index('<aside class="admin-sidebar"'))]
    for label in ("Dashboard", "Menu", "Instagram Stories", "Posts &amp; Reels", "Handoffs", "QA Templates", "Analytics", "Candidates", "Settings / Health"):
        assert label in nav
    assert "handoffSidebarBadge" in nav


def test_menu_manager_sits_between_menu_and_ai_resolution(page):
    assert 'id="tab-image"' in page
    assert 'id="tab-menu-manager"' in page
    assert "Manage catalogue" in page


def test_customers_and_answers_are_not_in_normal_client_navigation(page):
    """
    Their functions are not gone -- they are inside AI Resolution & Answers,
    reached through the exact same candidate/escalation/knowledge-gap/
    learned-knowledge endpoints. This only checks that a client opening the
    dashboard is no longer offered two extra buttons for workflows the
    unified tab already covers.
    """
    nav = page[page.index('<aside class="admin-sidebar"'):page.index('</aside>', page.index('<aside class="admin-sidebar"'))]
    assert "Customers" not in nav
    assert "Answers" not in nav
    assert "Candidates" in nav


def test_legacy_views_are_removed_from_normal_ui_and_old_bookmarks_redirect(page):
    """The modern AI Resolution workspace owns the migrated legacy actions."""
    assert "Advanced / Legacy Views" not in page
    assert "Open Legacy Customers View" not in page
    assert "Open Legacy Answers View" not in page
    assert "/admin/legacy" in {route.path for route in app.routes}
    assert "tab-ai-resolution" in page


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
    assert "Save &amp; Publish" in page
    assert "Draft ready" in page
    assert "mmPublishImage(" in page
    assert "/image/publish" in page
    assert "mmArchiveImage(" in page
    assert "mmHistoryImage(" in page


def test_ad_routing_panel_still_present_alongside_menu_manager(page):
    """Routing remains available, now as a Posts & Reels subview."""
    assert 'id="tab-ad-routing"' in page
    assert 'data-group="instagram_content"' in page[page.index('id="tab-ad-routing"'):]
    assert "Posts &amp; Reels Mapping" in page
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
    """Stories are managed only in the dedicated Stories tab."""
    section = page[page.index('id="tab-ad-routing"'):page.index('id="tab-instagram-stories"')]
    source_select = section[section.index('id="igSourceType"'):section.index('</select>', section.index('id="igSourceType"'))]
    assert '<option value="post">' in source_select
    assert '<option value="reel">' in source_select
    assert '<option value="ad">' in source_select
    assert 'value="story"' not in source_select
    assert 'igStoryMapForm' not in section
    assert 'igStoryMediaId' not in section
    assert 'igStorySourceList' not in section
    registry = page[page.index('function renderInstagramMediaRegistry'):page.index('async function loadAdRoutes')]
    assert "route.source_type !== 'story'" in registry
    assert 'useCapturedStory' not in page
    assert "igSourceType').value = 'story'" not in page


def test_failed_story_capability_probe_and_automatic_listing_are_removed(page):
    assert not any(route.path == "/admin/instagram-story-capability" for route in app.routes)
    source = inspect.getsource(instagram_routing)
    assert "def probe_instagram_story_capability" not in source
    assert "/stories" not in source


def test_instagram_stories_has_a_dedicated_top_level_tab_and_panel(page):
    assert "switchTab('tab-instagram-stories', this)" in page
    assert 'id="tab-instagram-stories"' in page
    assert 'data-group="instagram_stories"' in page
    assert "'tab-instagram-stories': 'instagram_stories'" in page
    assert "if (group === 'instagram_stories') { loadInstagramStories(); }" in page
    assert "fetch('/admin/instagram-story-sources'" in page
    assert "New Stories appear here after Instagram sends a Story interaction/reply event." in page


def test_instagram_stories_map_flow_uses_a_read_only_captured_id(page):
    section = page[page.index('id="tab-instagram-stories"'):page.index('<!-- MENU tab, panel 3 of 4')]
    assert 'id="igStoryMapForm"' in section
    assert 'id="igStoryMediaId"' in section and 'readonly' in section
    for field in ("igStoryTopicPicker", "igStoryPriorityPicker", "igStoryKnowledgeScope", "igStoryMenuPicker", "igStoryEscalationPolicy", "igStoryActive"):
        assert f'id="{field}"' in section
    assert 'id="igStoryAdvancedSettings"' in section
    assert '<details id="igStoryAdvancedSettings" open' not in section
    assert "media_id: mediaId" in page
    assert 'onclick="editInstagramRoute(' not in section


def test_instagram_story_menu_choice_offers_inherit_none_and_active_menus(page):
    """
    New mappings default to no asset; legacy inherited links remain supported.
    """
    section = page[page.index('id="tab-instagram-stories"'):page.index('<!-- MENU tab, panel 3 of 4')]
    menu_select = section[section.index('id="igStoryMenuPicker"'):section.index('</select>', section.index('id="igStoryMenuPicker"'))]
    assert '<option value="__none__">Choose menu or image</option>' in menu_select
    assert 'Existing Topic-linked menu' in page
    assert "Menu / Image" in section
    assert 'id="igStoryMenuSelected"' in section
    assert "openMenuModal('igStoryMenuPicker')" in section

    save = page[page.index('async function saveInstagramStoryMapping'):page.index('async function setInstagramStoryActive')]
    assert "menu_mode: menuChoice.menu_mode" in save
    assert "menu_id: menuChoice.menu_id" in save
    assert "updateMenuChoiceWarning" in save

    edit = page[page.index('function openInstagramStoryMapping'):page.index('function cancelInstagramStoryMapping')]
    assert "menuChoiceForRoute(source)" in edit
    assert "setMenuChoiceFromRoute" in edit


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
    toolbar = section[section.index('aria-label="Instagram Story filters"'):section.index('id="igStoryMapForm"')]
    filters = re.findall(r'data-story-filter="([^"]+)"', toolbar)
    assert filters == ["all", "UNMAPPED", "MAPPED", "DISABLED"]
    for label in ("All", "Unmapped", "Mapped", "Disabled", "Map Story", "Edit Mapping", "Delete", "Delete Mapping"):
        assert label in section or label in render
    for retired in ("ACTIVE", "EXPIRED", "REMOVED", "DELETED"):
        assert f'data-story-filter="{retired}"' not in toolbar
    assert 'data-story-filter="ARCHIVED"' not in section
    assert 'data-story-action="archive"' not in render
    assert 'data-story-action="delete-mapping"' not in render
    assert 'data-story-action="restore-source"' not in render
    assert 'Historical Story' not in page
    assert "lifecycleLabel" not in render
    assert 'Permanently remove this Story from Camellia?' in page
    assert 'This removes the local Story record and its mapping.' in page
    assert '/admin/instagram-story-sources/${encodeURIComponent(mediaId)}' in page
    assert '/permanent' not in render
    drawer = page[page.index('function openContentDetails'):page.index('async function saveInstagramMediaRoute')]
    assert "removeInstagramStoryMapping" in drawer
    assert 'id="igStoryActive"' in section


def test_instagram_story_delete_is_local_and_permanent(page):
    section = page[page.index('id="tab-instagram-stories"'):page.index('<!-- MENU tab, panel 3 of 4')]
    assert 'data-story-filter="HISTORY"' not in section
    assert "include_deleted=true" not in page
    assert "does not delete anything from Instagram" in page
    assert "restoreInstagramStorySource" not in page
    delete_fn = page[page.index('async function deleteInstagramStory'):page.index('function renderInstagramStories')]
    assert "igStorySources = igStorySources.filter" in delete_fn
    assert "renderInstagramStories()" in delete_fn
    assert "permanently removed" in delete_fn


def test_instagram_story_operational_filters_are_current_only_and_auto_refresh_is_safe(page):
    story_matches = page[page.index('function storyMatches'):page.index('function setInstagramStoryFilter')]
    assert "igStoryFilter === 'HISTORY'" not in story_matches
    assert "EXPIRED" not in story_matches

    refresh = page[page.index('function instagramStoriesTabIsVisible'):page.index('function renderInstagramMediaRegistry')]
    assert "document.visibilityState !== 'hidden'" in refresh
    assert "setInterval" in refresh
    assert "15000" in refresh
    assert "loadInstagramStories()" in refresh
    assert "graph.instagram.com" not in refresh
    assert "graph.facebook.com" not in refresh
    assert "access_token" not in refresh
    assert "document.addEventListener('visibilitychange', syncInstagramStoryAutoRefresh)" in page


def test_instagram_stories_refreshes_from_source_and_mapping_endpoints(page):
    section = page[page.index('async function loadInstagramStories'):page.index('function renderInstagramMediaRegistry')]
    assert "igStoryRefreshButton" in page
    assert "instagram-story-sources" in section
    assert "instagram-routes?include_archived=true" not in section
    assert "populateInstagramStoryPickers" in section
    assert "renderInstagramStories()" in section


def test_instagram_stories_default_empty_state_is_current_only(page):
    render = page[page.index('function renderInstagramStories'):page.index('function setInstagramStoryRefreshStatus')]
    assert "No Instagram Stories" in render
    assert "Current Stories will appear here when they are detected." in render
    assert "History" not in render
    assert "Historical Stories" not in render


def test_instagram_story_preview_metadata_never_exposes_temporary_url(page):
    section = page[page.index('function storyPreviewMetadata'):page.index('function storyMatches')]
    assert "new URL(String(value), window.location.origin)" in section
    assert "parsed.hostname" in section and "parsed.pathname" in section
    story_render = page[page.index('function renderInstagramStories'):page.index('async function loadInstagramStories')]
    assert "storyPreviewMetadata(source.temporary_url)" not in story_render
    assert "Meta Story ID:" not in story_render
    assert "data-story-media-id" in story_render
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
    assert "Save Mapping" in section
    assert "Menu / Image" in section
    assert 'id="igMenuSelected"' in section
    assert "openMenuModal('igMenuPicker')" in section
    assert "Preview Reply" in section
    assert "Preview Customer Response" not in section
    assert '<details id="igAdvancedSettings"' in section and '<details id="igAdvancedSettings" open' not in section
    assert "Advanced settings are normally filled automatically" in section


def test_topic_selectors_explain_empty_catalogues_and_refresh_together(page):
    """A fresh database must be configurable, and a new Topic reaches Stories too."""
    assert 'id="igTopicEmptyHelp"' in page
    assert "No active Topics are configured yet." in page
    assert 'id="igStoryTopicEmptyHelp"' in page
    assert "openTopicModal('', 'igTopicPicker')" in page
    assert "openTopicModal('', 'igStoryTopicPicker')" in page
    load_topics = page[page.index("async function loadTopicsAndMenus"):page.index("function onIgTopicChange")]
    assert "activeTopics = igTopics.filter" in load_topics
    assert "populateInstagramStoryPickers();" in load_topics
    story_pickers = page[page.index("function populateInstagramStoryPickers"):page.index("function onInstagramStoryTopicChange")]
    assert "activeTopics = igTopics.filter" in story_pickers
    assert "igStoryTopicEmptyHelp" in story_pickers


def test_topic_and_menu_edit_actions_are_clear_and_metadata_only(page):
    story_section = page[page.index('id="tab-instagram-stories"'):page.index('<!-- MENU tab, panel 3 of 4')]
    assert 'id="igStoryEditTopicButton"' not in story_section
    assert 'openManageTopics(\'igStoryTopicPicker\')' in story_section
    assert 'id="igEditTopicButton"' not in page
    assert 'id="manageTopicsModal"' in page
    assert 'id="topicModalTitle">+ Add Topic</h3>' in page
    assert 'id="topicDescription"' not in page

    catalogue = page[page.index('id="tab-menu-manager"'):page.index('<!-- CUSTOMERS tab')]
    assert "openMenuEditModal" in page
    assert 'id="menuModalTitle"' in page
    assert 'id="menuModalEditingId"' in page
    assert 'id="menuMetadataHelp"' in page
    assert "Replace → Preview → Save &amp; Publish" in page
    assert "method: 'PATCH'" in page
    assert "body: JSON.stringify({ name, category:" in page
    assert "Menu Catalogue" in catalogue


def test_instagram_routing_keeps_client_friendly_cards_and_existing_actions(page):
    section = page[page.index('function renderInstagramMediaCard'):page.index('// Shared-post pairing')]
    assert "Edit Mapping" in section
    for action in ("Edit Mapping", "Context: On", "Delete Mapping", "Status:", "Menu:"):
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


def test_admin_redesign_has_dashboard_gallery_and_drawer(page):
    assert 'id="tab-dashboard"' in page
    assert 'id="tab-instagram-content"' in page
    assert 'id="igContentGallery"' in page
    assert 'data-content-filter="post"' in page
    assert 'data-content-filter="reel"' in page
    assert 'id="igContentTableView"' not in page
    assert 'id="contentDetailsDrawer"' in page
    assert 'Open full details' in page
    assert "function openContentDetails" in page


def test_handoff_notifications_are_global_and_persistent(page):
    assert 'id="handoffBell"' in page
    assert 'id="handoffSidebarBadge"' in page
    assert 'id="handoffPopover"' in page
    assert "fetch('/admin/handoffs?limit=100&include_resumed=true'" in page
    assert "/admin/handoffs/${encodeURIComponent(ref)}/seen" in page
    assert "showHandoffToast" in page
    assert "WAITING_FOR_HUMAN" in page
    assert "Open handoff" in page
    assert "Read-only history" in page


def test_handoff_identity_and_current_menu_actions_are_wired(page):
    assert "${escapeHtml(customer)}" in page
    assert "Instagram ID: ${escapeHtml(item.customer_external_id" in page
    assert "function handoffCustomerIdentity" in page
    assert "function showGroup(group, selectedTabId = null)" in page
    assert "panel.id === selectedTabId" in page
    assert "showGroup(group, group === 'menu' ? tabId : null)" in page
    assert "onclick=\"switchTab('tab-active', null)\"" in page
    assert "fetch('/admin/menu/active'" in page
    assert "No published menu is currently serving customers" in page
    assert "Could not load the current menu" in page
    assert "← Back to Menu" in page


def test_story_cards_open_the_shared_details_drawer(page):
    assert 'class="instagram-gallery"' in page
    assert 'data-story-media-id' in page
    assert "openContentDetails('story', source)" in page
    assert "Exact Meta ID" in page


def test_second_pass_layout_and_drilldown_navigation_are_explicit(page):
    assert "grid-template-columns: minmax(220px, 248px) minmax(0, 1fr)" in page
    assert "← Back to Menu" in page
    assert "Menu Catalogue" in page
    assert "Current Menu" in page
    assert "menuCurrentStatus" in page
    assert "No published menu is currently serving customers" in page


def test_posts_reels_is_a_single_gallery_without_routing_subview(page):
    section = page[page.index('id="tab-instagram-content"'):page.index('id="tab-handoffs"')]
    assert "data-content-subview=\"gallery\"" not in section
    assert "data-content-subview=\"routing\"" not in section
    assert "data-content-subview=\"list\"" not in section
    assert "Exact Meta ID" in page
    assert "Instagram Content Routing" not in page[page.index('<aside class="admin-sidebar"'):page.index('</aside>')]


def test_qa_page_states_latest_accepted_lane_and_provenance_model(page):
    for marker in (
        "Authoritative approved QA model", "EXACT_APPROVED", "GROUNDED_APPROVED",
        "INSUFFICIENT_KNOWLEDGE", "Intent overrides stale state", "Home Table",
        "pre-booking", "payment", "Paraphrases/aliases", "source/provenance",
        "active/inactive status", "LLM cannot rewrite approved answers",
    ):
        assert marker in page


def test_health_page_uses_safe_current_schema_and_refresh_states(page):
    assert "fetch('/health', { cache: 'no-store' })" in page
    assert "settingsHealthRefresh" in page
    assert "settingsHealthIndicators" in page
    for marker in ("Durable storage", "Instagram delivery", "Webhook signatures", "Auth readiness", "Failure counters"):
        assert marker in page
    settings = page[page.index('id="tab-settings"'):page.index('<!-- Customers and Answers')]
    assert "mfa_health" not in settings.lower()
