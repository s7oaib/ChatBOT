"""
Runtime (not just string-match) tests for the two QA-found Instagram Story
admin-tab defects:

  Defect 1 (P1): the Story menu picker's empty option was labelled "Inherit
  Topic Menu" but always submitted menu_mode='NONE' -- INHERIT was
  unreachable. Fixed by adding an explicit igStoryMenuMode select
  (INHERIT/NONE/SELECTED) that the JS now reads and preserves.

  Defect 2 (P2): loadInstagramStories() replaced the visible Story list with
  "Loading..." before the request settled, so a failed refresh wiped out
  previously-good data. Fixed by only replacing the list after a successful
  response, with a separate non-destructive status banner.

These execute the actual JavaScript shipped in the rendered /admin page (via
Node.js), not a reimplementation of it, so a regression in the real script
fails here the same way it would in a browser. DOM/fetch are stubbed with a
generic proxy-backed fake element and a URL+method-matched fetch mock; no
other page behavior is exercised.
"""
import json
import re
import shutil
import subprocess
import textwrap

import pytest
from fastapi.testclient import TestClient

from camellia.app import app

client = TestClient(app)

NODE = shutil.which("node")


def _extract_admin_script() -> str:
    resp = client.get("/admin")
    assert resp.status_code == 200
    page = resp.text
    blocks = re.findall(r"<script>(.*?)</script>", page, re.DOTALL)
    assert blocks, "no inline script in the dashboard"
    return max(blocks, key=len)


_HARNESS_PRELUDE = r"""
'use strict';

// -- minimal DOM shim --------------------------------------------------
function makeFakeElement(id) {
    const state = {
        value: '', checked: false, innerHTML: '', innerText: '', textContent: '',
        disabled: false, className: '', style: {}, dataset: {}, options: [], selectedOptions: [],
    };
    const noop = new Set(['addEventListener', 'removeEventListener', 'removeChild',
        'scrollIntoView', 'click', 'focus', 'blur', 'remove']);
    const target = { __id: id };
    return new Proxy(target, {
        get(t, prop) {
            if (prop === 'classList') return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
            if (prop === 'querySelectorAll') return () => [];
            if (prop === 'querySelector') return () => null;
            // A real appendChild grows innerHTML/textContent; the tests
            // assert on the banner's actual rendered text, so this has to
            // genuinely append rather than swallow the node.
            if (prop === 'appendChild') return (node) => {
                const text = (node && (node.textContent || '')) || '';
                state.innerHTML = (state.innerHTML || '') + text;
                state.textContent = (state.textContent || '') + text;
                return node;
            };
            if (noop.has(prop)) return () => {};
            if (prop in state) return state[prop];
            return t[prop];
        },
        set(t, prop, value) {
            state[prop] = value;
            t[prop] = value;
            return true;
        },
    });
}

const _elements = new Map();
global.document = {
    cookie: '',
    getElementById(id) {
        if (!_elements.has(id)) _elements.set(id, makeFakeElement(id));
        return _elements.get(id);
    },
    createElement(tag) { return makeFakeElement('created:' + tag); },
    createTextNode(text) { return { nodeType: 3, textContent: text }; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    addEventListener() {},
    body: makeFakeElement('body'),
};
global.window = { addEventListener() {}, location: { origin: 'http://localhost' } };
global.confirm = () => true;
global.alert = () => {};

// -- fetch mock: matched by (url substring, method), FIFO within a match --
const fetchLog = [];
const fetchHandlers = [];
function queueFetch(urlPart, method, handler) {
    fetchHandlers.push({ urlPart, method: (method || 'GET').toUpperCase(), handler });
}
function pendingFetchCount() {
    return fetchHandlers.length;
}
global.fetch = (url, opts) => {
    const method = ((opts && opts.method) || 'GET').toUpperCase();
    fetchLog.push({ url: String(url), method, body: opts && opts.body ? JSON.parse(opts.body) : null });
    const idx = fetchHandlers.findIndex(h => String(url).includes(h.urlPart) && h.method === method);
    if (idx === -1) {
        return Promise.resolve({ ok: false, status: 404, json: async () => ({ detail: 'unhandled fetch: ' + method + ' ' + url }) });
    }
    const [entry] = fetchHandlers.splice(idx, 1);
    return entry.handler();
};

function okResponse(body) { return Promise.resolve({ ok: true, status: 200, json: async () => body }); }
function errorResponse(detail, status) { return Promise.resolve({ ok: false, status: status || 500, json: async () => ({ detail }) }); }
function rejectedFetch() { return Promise.reject(new Error('network error')); }

function deferred() {
    let resolve;
    const promise = new Promise(r => { resolve = r; });
    return { promise, resolve };
}
"""


def _run_node(scenario_js: str) -> dict:
    if NODE is None:
        pytest.skip("node executable not found on PATH")
    script = _extract_admin_script()
    full = _HARNESS_PRELUDE + "\n" + script + "\n" + scenario_js
    proc = subprocess.run(
        [NODE, "--input-type=commonjs"],
        input=full.encode("utf-8"),
        capture_output=True,
        timeout=30,
    )
    proc.stdout = proc.stdout.decode("utf-8", errors="replace")
    proc.stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise AssertionError(f"node scenario failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    # The scenario prints exactly one JSON line prefixed with RESULT::
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT::"):
            return json.loads(line[len("RESULT::"):])
    raise AssertionError(f"no RESULT:: line in node output:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


def _seed_common_pickers():
    return textwrap.dedent("""
        igTopics = [{ topic_id: 't1', name: 'Topic One', priority_products: [], default_menu_id: null }];
        igMenus = [{ menu_id: 'm1', name: 'Menu One' }, { menu_id: 'm2', name: 'Menu Two' }];
        adminAuthenticated = true;
    """)


# =====================================================================
# Defect 1 -- P1: Story menu-mode inheritance
# =====================================================================

class TestStoryMenuModeSubmission:
    def _save_scenario(self, media_id, topic_id, menu_mode, menu_id_value):
        return _seed_common_pickers() + textwrap.dedent(f"""
            document.getElementById('igStoryMediaId').value = {json.dumps(media_id)};
            document.getElementById('igStoryTopicPicker').value = {json.dumps(topic_id)};
            document.getElementById('igStoryMenuMode').value = {json.dumps(menu_mode)};
            document.getElementById('igStoryMenuPicker').value = {json.dumps(menu_id_value)};
            queueFetch('/admin/instagram-routes', 'POST', () => okResponse({{ status: 'success', routing_id: 55 }}));
            // saveInstagramStoryMapping() calls loadInstagramStories() on success;
            // let those three GETs resolve harmlessly so the promise settles.
            queueFetch('/admin/instagram-story-sources', 'GET', () => okResponse({{ story_sources: [] }}));
            queueFetch('/admin/instagram-routes?include_archived=true', 'GET', () => okResponse({{ routes: [] }}));
            queueFetch('/admin/instagram-routes', 'GET', () => okResponse({{ routes: [] }}));

            saveInstagramStoryMapping().then(() => {{
                const call = fetchLog.find(c => c.url.includes('/admin/instagram-routes') && c.method === 'POST');
                console.log('RESULT::' + JSON.stringify({{
                    postCalled: !!call,
                    body: call ? call.body : null,
                    resultText: document.getElementById('igStoryResult').innerText,
                }}));
            }});
        """)

    def test_inherit_submits_menu_mode_inherit(self):
        result = _run_node(self._save_scenario("story-1", "t1", "INHERIT", ""))
        assert result["postCalled"] is True
        assert result["body"]["menu_mode"] == "INHERIT"
        assert result["body"]["menu_id"] is None

    def test_none_submits_menu_mode_none(self):
        result = _run_node(self._save_scenario("story-1", "t1", "NONE", ""))
        assert result["postCalled"] is True
        assert result["body"]["menu_mode"] == "NONE"
        assert result["body"]["menu_id"] is None

    def test_selected_submits_menu_mode_selected_with_menu_id(self):
        result = _run_node(self._save_scenario("story-1", "t1", "SELECTED", "m2"))
        assert result["postCalled"] is True
        assert result["body"]["menu_mode"] == "SELECTED"
        assert result["body"]["menu_id"] == "m2"

    def test_selected_without_a_menu_cannot_be_submitted(self):
        result = _run_node(self._save_scenario("story-1", "t1", "SELECTED", ""))
        assert result["postCalled"] is False
        assert "menu" in result["resultText"].lower()


class TestStoryMenuModeEditPreservation:
    def _edit_scenario(self, source_menu_mode, source_menu_id):
        return _seed_common_pickers() + textwrap.dedent(f"""
            igStorySources = [{{
                story_media_id: 'story-1', routing_id: 101, mapping_status: 'MAPPED',
                topic_id: 't1', routing_topic: 'Topic One',
                menu_mode: {json.dumps(source_menu_mode)}, menu_id: {json.dumps(source_menu_id)},
                priority_products: [], knowledge_scope: [], active: true,
            }}];
            openInstagramStoryMapping('story-1');
            console.log('RESULT::' + JSON.stringify({{
                menuMode: document.getElementById('igStoryMenuMode').value,
                menuId: document.getElementById('igStoryMenuPicker').value,
                pickerWrapDisplay: document.getElementById('igStoryMenuPickerWrap').style.display,
            }}));
        """)

    def test_editing_an_inherit_route_preserves_inherit(self):
        result = _run_node(self._edit_scenario("INHERIT", None))
        assert result["menuMode"] == "INHERIT"
        assert result["pickerWrapDisplay"] != "block"

    def test_editing_a_none_route_preserves_none(self):
        result = _run_node(self._edit_scenario("NONE", None))
        assert result["menuMode"] == "NONE"
        assert result["pickerWrapDisplay"] != "block"

    def test_editing_a_selected_route_preserves_selected_menu(self):
        result = _run_node(self._edit_scenario("SELECTED", "m2"))
        assert result["menuMode"] == "SELECTED"
        assert result["menuId"] == "m2"
        assert result["pickerWrapDisplay"] == "block"

    def test_menu_mode_select_offers_inherit_as_a_real_option(self):
        """Requirement 1: INHERIT is selectable -- proven by successfully
        assigning it and reading it back, not just by string presence."""
        result = _run_node(_seed_common_pickers() + textwrap.dedent("""
            document.getElementById('igStoryMenuMode').value = 'INHERIT';
            console.log('RESULT::' + JSON.stringify({
                value: document.getElementById('igStoryMenuMode').value,
            }));
        """))
        assert result["value"] == "INHERIT"


# =====================================================================
# Defect 2 -- P2: refresh must not clear a good Story list on failure
# =====================================================================

def _seed_loaded_story_data():
    return textwrap.dedent("""
        queueFetch('/admin/instagram-story-sources', 'GET', () => okResponse({
            story_sources: [{ story_media_id: 'story-1', routing_id: 101, first_seen: '2026-01-01', last_seen: '2026-01-02', story_status: 'ACTIVE' }],
        }));
        queueFetch('/admin/instagram-routes?include_archived=true', 'GET', () => okResponse({ routes: [] }));
        queueFetch('/admin/instagram-routes', 'GET', () => okResponse({
            routes: [{ routing_id: 101, source_type: 'story', menu_mode: 'INHERIT', menu_id: null, topic_id: 't1', routing_topic: 'Topic One', mapping_status: 'MAPPED', active: true, priority_products: [], knowledge_scope: [] }],
            product_catalogue: [], knowledge_scopes: [],
        }));
    """)


class TestStoryRefreshResilience:
    def test_successful_first_load_renders_data(self):
        result = _run_node(_seed_common_pickers() + _seed_loaded_story_data() + textwrap.dedent("""
            loadInstagramStories().then(() => {
                console.log('RESULT::' + JSON.stringify({
                    sourcesCount: igStorySources.length,
                    listHtml: document.getElementById('igStoryList').innerHTML,
                    everLoaded: igStoriesEverLoaded,
                }));
            });
        """))
        assert result["sourcesCount"] == 1
        assert "story-1" in result["listHtml"] or "Topic One" in result["listHtml"]
        assert result["everLoaded"] is True

    def test_refresh_preserves_current_list_while_request_is_pending(self):
        result = _run_node(_seed_common_pickers() + _seed_loaded_story_data() + textwrap.dedent("""
            (async () => {
                await loadInstagramStories();
                const listBefore = document.getElementById('igStoryList').innerHTML;

                const d1 = deferred(); const d2 = deferred(); const d3 = deferred();
                queueFetch('/admin/instagram-story-sources', 'GET', () => d1.promise);
                queueFetch('/admin/instagram-routes?include_archived=true', 'GET', () => d2.promise);
                queueFetch('/admin/instagram-routes', 'GET', () => d3.promise);

                const inFlight = loadInstagramStories();
                // Give pending microtasks a tick without resolving the fetches.
                await new Promise(r => setTimeout(r, 10));
                const listDuringRefresh = document.getElementById('igStoryList').innerHTML;
                const refreshBannerDuringRefresh = document.getElementById('igStoryRefreshStatus').innerHTML;
                const buttonDisabledDuringRefresh = document.getElementById('igStoryRefreshButton').disabled;

                d1.resolve({ ok: true, status: 200, json: async () => ({ story_sources: [{ story_media_id: 'story-1', routing_id: 101, first_seen: '2026-01-01', last_seen: '2026-01-02', story_status: 'ACTIVE' }] }) });
                d2.resolve({ ok: true, status: 200, json: async () => ({ routes: [] }) });
                d3.resolve({ ok: true, status: 200, json: async () => ({ routes: [{ routing_id: 101, source_type: 'story', menu_mode: 'INHERIT', menu_id: null, topic_id: 't1', routing_topic: 'Topic One', mapping_status: 'MAPPED', active: true, priority_products: [], knowledge_scope: [] }], product_catalogue: [], knowledge_scopes: [] }) });
                await inFlight;

                console.log('RESULT::' + JSON.stringify({
                    listUnchangedDuringRefresh: listDuringRefresh === listBefore,
                    listWasNotLoadingPlaceholder: !listDuringRefresh.toLowerCase().includes('loading captured'),
                    bannerShownDuringRefresh: refreshBannerDuringRefresh.length > 0,
                    buttonDisabledDuringRefresh: buttonDisabledDuringRefresh,
                    buttonReenabledAfter: document.getElementById('igStoryRefreshButton').disabled === false,
                }));
            })();
        """))
        assert result["listUnchangedDuringRefresh"] is True
        assert result["listWasNotLoadingPlaceholder"] is True
        assert result["bannerShownDuringRefresh"] is True
        assert result["buttonDisabledDuringRefresh"] is True
        assert result["buttonReenabledAfter"] is True

    def test_successful_refresh_replaces_list_with_fresh_data(self):
        result = _run_node(_seed_common_pickers() + _seed_loaded_story_data() + textwrap.dedent("""
            (async () => {
                await loadInstagramStories();
                queueFetch('/admin/instagram-story-sources', 'GET', () => okResponse({ story_sources: [{ story_media_id: 'story-2', routing_id: 202, first_seen: '2026-02-01', last_seen: '2026-02-02', story_status: 'ACTIVE' }] }));
                queueFetch('/admin/instagram-routes?include_archived=true', 'GET', () => okResponse({ routes: [] }));
                queueFetch('/admin/instagram-routes', 'GET', () => okResponse({ routes: [{ routing_id: 202, source_type: 'story', menu_mode: 'NONE', menu_id: null, topic_id: 't1', routing_topic: 'Topic Two', mapping_status: 'MAPPED', active: true, priority_products: [], knowledge_scope: [] }], product_catalogue: [], knowledge_scopes: [] }));
                await loadInstagramStories();
                console.log('RESULT::' + JSON.stringify({
                    sourcesCount: igStorySources.length,
                    mediaId: igStorySources[0].story_media_id,
                }));
            })();
        """))
        assert result["sourcesCount"] == 1
        assert result["mediaId"] == "story-2"

    def test_failed_refresh_retains_previous_data_and_shows_separate_error(self):
        result = _run_node(_seed_common_pickers() + _seed_loaded_story_data() + textwrap.dedent("""
            (async () => {
                await loadInstagramStories();
                const sourcesBefore = JSON.parse(JSON.stringify(igStorySources));
                const listBefore = document.getElementById('igStoryList').innerHTML;

                queueFetch('/admin/instagram-story-sources', 'GET', () => errorResponse('boom'));
                queueFetch('/admin/instagram-routes?include_archived=true', 'GET', () => okResponse({ routes: [] }));
                queueFetch('/admin/instagram-routes', 'GET', () => okResponse({ routes: [] }));
                await loadInstagramStories();

                console.log('RESULT::' + JSON.stringify({
                    sourcesUnchanged: JSON.stringify(igStorySources) === JSON.stringify(sourcesBefore),
                    listUnchanged: document.getElementById('igStoryList').innerHTML === listBefore,
                    bannerText: document.getElementById('igStoryRefreshStatus').innerHTML,
                    bannerIsErrorClass: document.getElementById('igStoryRefreshStatus').className.includes('error'),
                }));
            })();
        """))
        assert result["sourcesUnchanged"] is True
        assert result["listUnchanged"] is True
        assert "could not refresh" in result["bannerText"].lower()
        assert "showing last loaded data" in result["bannerText"].lower()
        assert result["bannerIsErrorClass"] is True

    def test_failed_refresh_via_rejected_fetch_also_retains_previous_data(self):
        """A thrown/rejected fetch (real network failure), not just a non-ok response."""
        result = _run_node(_seed_common_pickers() + _seed_loaded_story_data() + textwrap.dedent("""
            (async () => {
                await loadInstagramStories();
                const sourcesBefore = JSON.parse(JSON.stringify(igStorySources));

                queueFetch('/admin/instagram-story-sources', 'GET', () => rejectedFetch());
                queueFetch('/admin/instagram-routes?include_archived=true', 'GET', () => okResponse({ routes: [] }));
                queueFetch('/admin/instagram-routes', 'GET', () => okResponse({ routes: [] }));
                await loadInstagramStories();

                console.log('RESULT::' + JSON.stringify({
                    sourcesUnchanged: JSON.stringify(igStorySources) === JSON.stringify(sourcesBefore),
                    bannerText: document.getElementById('igStoryRefreshStatus').innerHTML,
                }));
            })();
        """))
        assert result["sourcesUnchanged"] is True
        assert "could not refresh" in result["bannerText"].lower()

    def test_retry_after_failure_recovers(self):
        result = _run_node(_seed_common_pickers() + _seed_loaded_story_data() + textwrap.dedent("""
            (async () => {
                await loadInstagramStories();

                queueFetch('/admin/instagram-story-sources', 'GET', () => errorResponse('boom'));
                queueFetch('/admin/instagram-routes?include_archived=true', 'GET', () => okResponse({ routes: [] }));
                queueFetch('/admin/instagram-routes', 'GET', () => okResponse({ routes: [] }));
                await loadInstagramStories();  // fails

                queueFetch('/admin/instagram-story-sources', 'GET', () => okResponse({ story_sources: [{ story_media_id: 'story-3', routing_id: 303, first_seen: '2026-03-01', last_seen: '2026-03-02', story_status: 'ACTIVE' }] }));
                queueFetch('/admin/instagram-routes?include_archived=true', 'GET', () => okResponse({ routes: [] }));
                queueFetch('/admin/instagram-routes', 'GET', () => okResponse({ routes: [{ routing_id: 303, source_type: 'story', menu_mode: 'INHERIT', menu_id: null, topic_id: 't1', routing_topic: 'Topic Three', mapping_status: 'MAPPED', active: true, priority_products: [], knowledge_scope: [] }], product_catalogue: [], knowledge_scopes: [] }));
                await loadInstagramStories();  // retry succeeds

                console.log('RESULT::' + JSON.stringify({
                    sourcesCount: igStorySources.length,
                    mediaId: igStorySources[0].story_media_id,
                    bannerCleared: document.getElementById('igStoryRefreshStatus').innerHTML === '' ,
                }));
            })();
        """))
        assert result["sourcesCount"] == 1
        assert result["mediaId"] == "story-3"
        assert result["bannerCleared"] is True

    def test_search_and_filter_state_survive_a_failed_refresh(self):
        result = _run_node(_seed_common_pickers() + _seed_loaded_story_data() + textwrap.dedent("""
            (async () => {
                await loadInstagramStories();
                document.getElementById('igStorySearch').value = 'story-1';
                igStoryFilter = 'MAPPED';

                queueFetch('/admin/instagram-story-sources', 'GET', () => errorResponse('boom'));
                queueFetch('/admin/instagram-routes?include_archived=true', 'GET', () => okResponse({ routes: [] }));
                queueFetch('/admin/instagram-routes', 'GET', () => okResponse({ routes: [] }));
                await loadInstagramStories();

                console.log('RESULT::' + JSON.stringify({
                    searchValue: document.getElementById('igStorySearch').value,
                    filterValue: igStoryFilter,
                }));
            })();
        """))
        assert result["searchValue"] == "story-1"
        assert result["filterValue"] == "MAPPED"


class TestStoryDeleteActions:
    def test_delete_requires_confirmation_and_uses_local_soft_delete_endpoint(self):
        result = _run_node(_seed_common_pickers() + textwrap.dedent("""
            adminAuthenticated = true;
            igStorySources = [{ story_media_id: 'story-delete', mapping_status: 'UNMAPPED', last_seen: '2026-01-01' }];
            let confirmation = '';
            global.confirm = message => { confirmation = message; return true; };
            queueFetch('/admin/instagram-story-sources/story-delete', 'DELETE', () => okResponse({ deleted: true }));
            queueFetch('/admin/instagram-story-sources', 'GET', () => okResponse({ story_sources: [] }));
            queueFetch('/admin/instagram-routes?include_archived=true', 'GET', () => okResponse({ routes: [] }));
            queueFetch('/admin/instagram-routes', 'GET', () => okResponse({ routes: [] }));

            deleteInstagramStory('story-delete').then(() => {
                const call = fetchLog.find(item => item.method === 'DELETE');
                console.log('RESULT::' + JSON.stringify({
                    confirmation,
                    deleteUrl: call && call.url,
                    deleteMethod: call && call.method,
                    successText: document.getElementById('igStoryResult').innerText,
                }));
            });
        """))
        assert result["deleteMethod"] == "DELETE"
        assert result["deleteUrl"].endswith("/admin/instagram-story-sources/story-delete")
        assert "Delete this Story from Camellia Admin?" in result["confirmation"]
        assert "does not delete the Story from Instagram" in result["confirmation"]
        assert "not deleted" in result["successText"]

    def test_deleted_filter_requests_history_without_changing_default_load(self):
        result = _run_node(_seed_common_pickers() + textwrap.dedent("""
            adminAuthenticated = true;
            queueFetch('/admin/instagram-story-sources?include_deleted=true', 'GET', () => okResponse({ story_sources: [{ story_media_id: 'story-deleted', admin_deleted: 1, deleted_at: '2026-01-02', last_seen: '2026-01-02' }] }));
            queueFetch('/admin/instagram-routes?include_archived=true', 'GET', () => okResponse({ routes: [] }));
            queueFetch('/admin/instagram-routes', 'GET', () => okResponse({ routes: [] }));
            setInstagramStoryFilter('DELETED');
            setTimeout(() => {
                console.log('RESULT::' + JSON.stringify({
                    historyUrl: fetchLog[0] && fetchLog[0].url,
                    listHtml: document.getElementById('igStoryList').innerHTML,
                }));
            }, 10);
        """))
        assert "include_deleted=true" in result["historyUrl"]
        assert "story-deleted" in result["listHtml"]
