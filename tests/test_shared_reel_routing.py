"""
Source routing for a reel shared into the DM.

Built on the payload production actually sent on 2026-09-11, not on a guess:

    {"type": "ig_reel", "payload": {
        "reel_video_id": "18103484309205929",
        "title": "<the caption>",
        "url": "https://www.instagram.com/reel/DdAnO6MTok1/"}}

Only ``payload.url`` is a routing key. The caption is marketing text and the
video id has never been verified against the Graph media id an operator
configures, so both are treated as unusable here — and there are tests below
that fail if either ever starts being matched on.
"""
import os

os.environ.setdefault("DB_PATH", "test_camellia.db")

import pytest

import camellia.app as app_module
from camellia.database import Database
from camellia import instagram_routing
from camellia.instagram_routing import route_for_inbound, shared_reel_identity

CONFIGURED_SHORTCODE = "DdAnO6MTok1"
CONFIGURED_URL = f"https://www.instagram.com/reel/{CONFIGURED_SHORTCODE}/"
THIRD_PARTY_URL = "https://www.instagram.com/reel/DdHBVMzpRq5/"


def _reel_attachment(url, *, title="a caption that must never be matched on"):
    return {"type": "ig_reel", "payload": {
        "reel_video_id": "18103484309205929", "title": title, "url": url,
    }}


def _event(attachments):
    return {
        "sender": {"id": "sender_1"}, "recipient": {"id": "page_1"},
        "message": {"mid": "m_1", "attachments": attachments},
    }


@pytest.fixture
def routed_db(tmp_path, monkeypatch):
    """A database holding the configured Wedding / Event Sondesh reel route."""
    database = Database(str(tmp_path / "routes.db"))
    database.save_instagram_source_route({
        "source_type": "reel",
        "canonical_url": CONFIGURED_URL,
        "shortcode": CONFIGURED_SHORTCODE,
        "media_id": None, "ad_id": None, "campaign_id": None,
        "routing_topic": "Wedding / Event Sondesh",
        "knowledge_scope": [],
        "escalation_policy": "human_if_unsupported",
        "priority_products": ["jolbhora", "kanchagolla", "aamar_mishti"],
        "active": True,
    })
    import camellia.database as db_module
    monkeypatch.setattr(db_module, "db", database)
    return database


# --- the identifier itself -------------------------------------------------

def test_configured_reel_url_normalizes_to_the_configured_shortcode():
    identity = shared_reel_identity({"attachments": [_reel_attachment(CONFIGURED_URL)]})
    assert identity["shortcode"] == CONFIGURED_SHORTCODE
    assert identity["canonical_url"] == CONFIGURED_URL
    assert identity["source_type"] == "reel"


def test_tracking_parameters_on_a_shared_url_do_not_defeat_the_match():
    identity = shared_reel_identity({"attachments": [
        _reel_attachment(f"{CONFIGURED_URL}?igshid=abc&utm_source=ig_web_button_share_sheet")
    ]})
    assert identity["shortcode"] == CONFIGURED_SHORTCODE


def test_caption_is_never_used_as_a_matcher():
    """A caption naming the configured reel must not make an unrelated reel match."""
    identity = shared_reel_identity({"attachments": [
        _reel_attachment(THIRD_PARTY_URL, title=f"Wedding / Event Sondesh {CONFIGURED_URL}")
    ]})
    assert identity["shortcode"] == "DdHBVMzpRq5"


def test_reel_video_id_is_never_used_as_a_matcher(routed_db):
    """The configured route has no media id; a video id must not stand in for one."""
    route, method = route_for_inbound(ad_id=None, media_id="18103484309205929", shortcode=None)
    assert route is None
    assert method is None


def test_an_ig_post_attachment_is_not_matched(routed_db):
    """Shared posts carry a lookaside CDN url with no shortcode — unsupported."""
    identity = shared_reel_identity({"attachments": [{
        "type": "ig_post",
        "payload": {
            "ig_post_media_id": "17929864047401754",
            "title": "a post",
            "url": "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1&signature=x",
        },
    }]})
    assert identity is None


def test_unparseable_or_missing_urls_are_ignored():
    assert shared_reel_identity({"attachments": [_reel_attachment("not-a-url")]}) is None
    assert shared_reel_identity({"attachments": [{"type": "ig_reel", "payload": {}}]}) is None
    assert shared_reel_identity({"attachments": [{"type": "image", "payload": {"url": CONFIGURED_URL}}]}) is None
    assert shared_reel_identity({"attachments": "not-a-list"}) is None
    assert shared_reel_identity({}) is None


# --- resolving it to a configured route ------------------------------------

def test_configured_shared_reel_resolves_the_route(routed_db):
    route, method = route_for_inbound(ad_id=None, media_id=None, shortcode=CONFIGURED_SHORTCODE)
    assert method == "shared_media_url"
    assert route["routing_topic"] == "Wedding / Event Sondesh"
    assert route["shortcode"] == CONFIGURED_SHORTCODE
    assert route["source_type"] == "reel"
    assert route["routing_id"] is not None
    assert route["priority_products"] == ["jolbhora", "kanchagolla", "aamar_mishti"]


def test_unconfigured_third_party_reel_resolves_nothing(routed_db):
    route, method = route_for_inbound(ad_id=None, media_id=None, shortcode="DdHBVMzpRq5")
    assert route is None
    assert method is None


def test_an_inactive_route_is_not_matched(routed_db):
    rows = routed_db.list_instagram_source_routes()
    routed_db.set_instagram_source_route_active(rows[0]["routing_id"], False)
    route, method = route_for_inbound(ad_id=None, media_id=None, shortcode=CONFIGURED_SHORTCODE)
    assert route is None


def test_shortcode_lookup_is_still_allowlisted(routed_db):
    """The column name is interpolated, so the allowlist is load-bearing."""
    with pytest.raises(ValueError):
        routed_db.find_instagram_source_route("routing_topic", "Wedding / Event Sondesh")
    with pytest.raises(ValueError):
        routed_db.find_instagram_source_route("1=1; DROP TABLE instagram_source_routes--", "x")


# --- precedence: nothing existing gets weaker ------------------------------

def test_ad_id_still_wins_over_a_shared_reel(routed_db):
    routed_db.save_instagram_source_route({
        "source_type": "ad", "canonical_url": None, "shortcode": None,
        "media_id": None, "ad_id": "ad_999", "campaign_id": None,
        "routing_topic": "Ad campaign", "knowledge_scope": [],
        "escalation_policy": "human_if_unsupported",
        "priority_products": ["bento"], "active": True,
    })
    route, method = route_for_inbound(
        ad_id="ad_999", media_id=None, shortcode=CONFIGURED_SHORTCODE,
    )
    assert method == "ad_id"
    assert route["routing_topic"] == "Ad campaign"


def test_story_media_id_still_wins_over_a_shared_reel(routed_db):
    routed_db.save_instagram_source_route({
        "source_type": "post", "canonical_url": None, "shortcode": None,
        "media_id": "story_777", "ad_id": None, "campaign_id": None,
        "routing_topic": "Story topic", "knowledge_scope": [],
        "escalation_policy": "human_if_unsupported",
        "priority_products": ["mahua"], "active": True,
    })
    route, method = route_for_inbound(
        ad_id=None, media_id="story_777", shortcode=CONFIGURED_SHORTCODE,
    )
    assert method == "media_id"
    assert route["routing_topic"] == "Story topic"


def test_existing_callers_need_no_shortcode(routed_db):
    """route_for_inbound keeps working for callers that pass only the old two."""
    assert route_for_inbound(ad_id=None, media_id=None) == (None, None)


# --- the session hand-off to the next message ------------------------------

def test_shared_reel_sets_session_context_for_the_next_message(routed_db, monkeypatch):
    stored = {}
    monkeypatch.setattr(app_module.session_memory, "get_facts", lambda uid, ch: stored)
    monkeypatch.setattr(instagram_routing, "decode_route", instagram_routing.decode_route)

    identity = shared_reel_identity({"attachments": [_reel_attachment(CONFIGURED_URL)]})
    app_module._establish_shared_reel_context("sender_1", identity)

    assert stored["source_route"]["routing_topic"] == "Wedding / Event Sondesh"
    assert stored["source_route"]["shortcode"] == CONFIGURED_SHORTCODE
    assert stored["source_match_method"] == "shared_media_url"


def test_fresh_reel_then_tell_me_more_uses_source_context_and_dispatches(routed_db, monkeypatch):
    sender_id = "sender_reel_acceptance"
    identity = shared_reel_identity({"attachments": [_reel_attachment(CONFIGURED_URL)]})
    assert app_module._establish_shared_reel_context(sender_id, identity) is True

    logged = []
    sent = []
    monkeypatch.setattr(app_module.db, "log_conversation", lambda *args, **kwargs: logged.append((args, kwargs)) or 901)
    monkeypatch.setattr(app_module, "send_instagram_reply", lambda *args, **kwargs: sent.append((args, kwargs)))

    app_module._process_instagram_event(sender_id, "Tell me more", None)

    assert sent
    assert sent[0][1]["inbound_message_id"] == 901
    assert sent[0][0][1]
    trace = logged[0][1]
    assert trace["source_context_used"] is True
    assert trace["source_routing_id"] is not None
    assert trace["source_match_method"] == "shared_media_url"
    assert "can't open photos" not in sent[0][0][1].lower()


def test_unconfigured_reel_leaves_the_session_untouched(routed_db, monkeypatch):
    stored = {}
    monkeypatch.setattr(app_module.session_memory, "get_facts", lambda uid, ch: stored)

    identity = shared_reel_identity({"attachments": [_reel_attachment(THIRD_PARTY_URL)]})
    app_module._establish_shared_reel_context("sender_1", identity)

    assert stored == {}


def test_context_failure_never_breaks_the_attachment_reply(monkeypatch):
    replied = []
    monkeypatch.setattr(app_module, "_reply_to_unreadable_attachment", lambda uid, **_kwargs: replied.append(uid))
    monkeypatch.setattr(
        app_module, "route_for_inbound",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("database down")),
    )
    app_module._handle_shared_attachment("sender_1", {"shortcode": CONFIGURED_SHORTCODE})
    assert replied == ["sender_1"]


# --- dispatch keeps its existing shape -------------------------------------

class _Tasks:
    def __init__(self):
        self.queued = []

    def add_task(self, fn, *args, **kwargs):
        self.queued.append((fn.__name__, args))


@pytest.fixture
def answering_organic_dms(monkeypatch):
    """Production runs with BYPASS_AD_GATE on, so the bot answers organic DMs —
    and a shared reel is organic traffic by definition."""
    monkeypatch.setattr(app_module.config, "BYPASS_AD_GATE", True)


def test_dispatch_queues_the_reel_identity_without_changing_the_reply(answering_organic_dms):
    tasks = _Tasks()
    payload = {"object": "instagram", "entry": [{"messaging": [_event([_reel_attachment(CONFIGURED_URL)])]}]}
    result = app_module._dispatch_instagram_payload(payload, tasks)

    assert result["results"][0]["action"] == "queued_attachment_reply"
    assert result["results"][0]["shared_reel_shortcode"] == CONFIGURED_SHORTCODE
    name, args = tasks.queued[0]
    assert name == "_handle_shared_attachment"
    assert args[1]["shortcode"] == CONFIGURED_SHORTCODE


def test_dispatch_reports_no_shortcode_for_an_ordinary_photo(answering_organic_dms):
    tasks = _Tasks()
    payload = {"object": "instagram", "entry": [{"messaging": [
        _event([{"type": "image", "payload": {"url": "https://scontent.cdninstagram.com/v/t51/1.jpg"}}])
    ]}]}
    result = app_module._dispatch_instagram_payload(payload, tasks)

    assert result["results"][0]["action"] == "queued_attachment_reply"
    assert result["results"][0]["shared_reel_shortcode"] is None
    assert tasks.queued[0][1][1] is None


def test_a_typed_message_is_still_routed_normally(answering_organic_dms):
    """The reel change must not have moved ordinary text off its own path."""
    tasks = _Tasks()
    payload = {"object": "instagram", "entry": [{"messaging": [{
        "sender": {"id": "sender_2"}, "recipient": {"id": "page_1"},
        "message": {"mid": "m_2", "text": "What is the price of jolbhora?"},
    }]}]}
    result = app_module._dispatch_instagram_payload(payload, tasks)

    assert result["results"][0]["action"] == "queued"
    assert tasks.queued[0][0] == "_process_instagram_event"
