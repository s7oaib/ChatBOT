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


def test_the_dashboard_has_three_tabs(page):
    """
    Eight tabs needed 1655px of nav on a 347px phone, so most of the portal was
    unreachable on the handset the kitchen actually uses.
    """
    buttons = re.findall(r'<button class="tab-btn[^"]*" onclick="switchTab\(', page)
    assert len(buttons) == 3, f"expected 3 tabs, found {len(buttons)}"


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


def test_both_dashboard_routes_send_the_same_no_cache_headers():
    """/admin and /admin/dashboard are the same page; one must not be stale."""
    for path in ("/admin", "/admin/dashboard"):
        assert "no-store" in client.get(path).headers.get("cache-control", ""), path
