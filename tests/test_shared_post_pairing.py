"""
Shared-post routing via admin-verified pairing.

A shared post's webhook attachment carries only ``payload.ig_post_media_id``
(``payload.url`` is a lookaside CDN link with no shortcode) — captured for
real in production on 2026-09-11. Two independent Graph checks against that
exact id (a paginated /me/media listing, and a direct by-id fetch) both came
back unable to confirm it belongs to the account, so it cannot be trusted the
way a reel's permalink is. Routing a post is therefore only possible once an
admin deliberately pairs a captured id to a route during a short, explicitly
opened window.
"""
import os

os.environ.setdefault("DB_PATH", "test_camellia.db")

import time

import pytest

import camellia.app as app_module
from camellia.database import Database
from camellia.instagram_routing import route_for_inbound, shared_post_media_id
from camellia.session_memory import session_memory

POST_MEDIA_ID = "17930882778146779"
THIRD_PARTY_MEDIA_ID = "99999999999999999"


def _post_attachment(media_id, title="FOR THOSE WHO LOVE MISHTI."):
    return {"type": "ig_post", "payload": {
        "ig_post_media_id": media_id, "title": title,
        "url": "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1&signature=x",
    }}


@pytest.fixture(autouse=True)
def bypass_ad_gate(monkeypatch):
    monkeypatch.setattr(app_module.config, "BYPASS_AD_GATE", True)


@pytest.fixture
def post_route(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "pairing.db"))
    route = database.save_instagram_source_route({
        "source_type": "post",
        "canonical_url": "https://www.instagram.com/p/Dc2PmrQE3Y4/",
        "shortcode": "Dc2PmrQE3Y4",
        "media_id": None, "ad_id": None, "campaign_id": None,
        "routing_topic": "norem pak sondesh",
        "knowledge_scope": [],
        "escalation_policy": "human_if_unsupported",
        "priority_products": ["signature_collection"],
        "active": True,
    })
    import camellia.database as db_module
    monkeypatch.setattr(db_module, "db", database)
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr("camellia.handoff.db", database)
    return database, route["routing_id"]


# --- extraction --------------------------------------------------------

def test_shared_post_media_id_reads_only_the_ig_post_field():
    message = {"attachments": [_post_attachment(POST_MEDIA_ID)]}
    assert shared_post_media_id(message) == POST_MEDIA_ID


def test_caption_is_never_read_for_a_shared_post():
    message = {"attachments": [_post_attachment(
        POST_MEDIA_ID, title="Dc2PmrQE3Y4 norem pak sondesh",
    )]}
    # The captured id is what is returned regardless of what the caption says.
    assert shared_post_media_id(message) == POST_MEDIA_ID


def test_a_reel_attachment_yields_no_post_media_id():
    message = {"attachments": [{"type": "ig_reel", "payload": {
        "reel_video_id": "1", "title": "x", "url": "https://www.instagram.com/reel/AbC/",
    }}]}
    assert shared_post_media_id(message) is None


def test_missing_or_malformed_payload_yields_none():
    assert shared_post_media_id({"attachments": [{"type": "ig_post", "payload": {}}]}) is None
    assert shared_post_media_id({"attachments": "not-a-list"}) is None
    assert shared_post_media_id({}) is None


# --- an unconfigured/unpaired post never guesses ------------------------

def test_an_unpaired_post_does_not_match_and_no_pairing_is_open(post_route):
    database, routing_id = post_route
    route, method = route_for_inbound(ad_id=None, media_id=None, post_media_id=POST_MEDIA_ID)
    assert route is None
    assert method is None


def test_third_party_post_never_matches_even_during_an_open_pairing(post_route):
    """A pairing window binds the *next* shared post, not any post at all —
    but route_for_inbound itself only ever matches an id already bound."""
    database, routing_id = post_route
    database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    route, method = route_for_inbound(ad_id=None, media_id=None, post_media_id=THIRD_PARTY_MEDIA_ID)
    assert route is None  # nothing is bound yet — a pairing window alone matches nothing


# --- the pairing lifecycle -----------------------------------------------

def test_starting_a_pairing_creates_a_pending_row(post_route):
    database, routing_id = post_route
    pairing = database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    assert pairing["status"] == "pending"
    assert pairing["routing_id"] == routing_id
    assert database.get_active_post_pairing()["id"] == pairing["id"]


def test_starting_a_second_pairing_supersedes_the_first(post_route):
    database, routing_id = post_route
    first = database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    second = database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    active = database.get_active_post_pairing()
    assert active["id"] == second["id"]
    assert database.get_post_pairing_for_route(routing_id)["id"] == second["id"]
    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM instagram_post_pairings WHERE id = ?", (first["id"],)
        ).fetchone()
    assert row["status"] == "cancelled"


def test_an_expired_pairing_is_not_active(post_route):
    database, routing_id = post_route
    pairing = database.start_post_pairing(routing_id, ttl_seconds=0, started_by="admin")
    time.sleep(1.1)
    assert database.get_active_post_pairing() is None
    # And it cannot be bound after expiry either.
    assert database.bind_post_pairing(pairing["id"], POST_MEDIA_ID) is None


def test_cancelling_closes_the_pending_pairing(post_route):
    database, routing_id = post_route
    database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    assert database.cancel_post_pairing(routing_id) is True
    assert database.get_active_post_pairing() is None


def test_binding_writes_the_media_id_onto_the_route(post_route):
    database, routing_id = post_route
    pairing = database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    bound = database.bind_post_pairing(pairing["id"], POST_MEDIA_ID)
    assert bound["status"] == "bound"
    assert bound["bound_media_id"] == POST_MEDIA_ID
    route = database.get_instagram_source_route(routing_id)
    assert route["media_id"] == POST_MEDIA_ID


def test_after_binding_the_post_matches_through_the_normal_lookup(post_route):
    """Once bound, no special-case matcher is needed — the existing exact
    media_id lookup (the same one story replies use) finds it."""
    database, routing_id = post_route
    pairing = database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    database.bind_post_pairing(pairing["id"], POST_MEDIA_ID)
    route, method = route_for_inbound(ad_id=None, media_id=None, post_media_id=POST_MEDIA_ID)
    assert method == "shared_post_media_id"
    assert route["routing_topic"] == "norem pak sondesh"
    assert route["priority_products"] == ["signature_collection"]


# --- the webhook-facing behavior ------------------------------------------

def test_establishing_context_during_an_open_window_binds_and_sets_source(post_route):
    database, routing_id = post_route
    database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    session_memory.clear_session("uid_pair", "instagram")

    recognized = app_module._establish_shared_post_context("uid_pair", POST_MEDIA_ID)

    assert recognized is True
    facts = session_memory.get_facts("uid_pair", "instagram")
    assert facts["source_route"]["routing_id"] == routing_id
    assert facts["source_match_method"] == "shared_post_media_id"
    assert facts["last_topic"] is None
    assert facts["source_priority_cursor"] == 0
    # And the route itself is now permanently paired for future customers.
    assert database.get_instagram_source_route(routing_id)["media_id"] == POST_MEDIA_ID


def test_a_second_customer_sharing_the_same_post_matches_without_a_new_pairing(post_route):
    database, routing_id = post_route
    database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    app_module._establish_shared_post_context("uid_first", POST_MEDIA_ID)

    session_memory.clear_session("uid_second", "instagram")
    recognized = app_module._establish_shared_post_context("uid_second", POST_MEDIA_ID)
    assert recognized is True
    facts = session_memory.get_facts("uid_second", "instagram")
    assert facts["source_match_method"] == "shared_post_media_id"


def test_an_unconfigured_post_with_no_open_pairing_establishes_nothing(post_route):
    database, routing_id = post_route
    session_memory.clear_session("uid_none", "instagram")
    recognized = app_module._establish_shared_post_context("uid_none", THIRD_PARTY_MEDIA_ID)
    assert recognized is False
    assert session_memory.get_facts("uid_none", "instagram").get("source_route") is None


def test_a_different_post_shared_during_an_open_window_still_binds(post_route):
    """The window trusts whatever share arrives next, by design — this is
    exactly why starting it is a deliberate admin action with a short TTL,
    not something a customer can trigger."""
    database, routing_id = post_route
    database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    session_memory.clear_session("uid_any", "instagram")
    recognized = app_module._establish_shared_post_context("uid_any", THIRD_PARTY_MEDIA_ID)
    assert recognized is True
    assert database.get_instagram_source_route(routing_id)["media_id"] == THIRD_PARTY_MEDIA_ID


# --- requirement 7: no misleading "can't open" reply for a recognized post -

def test_recognized_post_suppresses_the_unreadable_attachment_reply(post_route, monkeypatch):
    database, routing_id = post_route
    database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    session_memory.clear_session("uid_reply", "instagram")

    replied = []
    monkeypatch.setattr(app_module, "_reply_to_unreadable_attachment", lambda uid, **_kwargs: replied.append(uid))

    app_module._handle_shared_attachment("uid_reply", None, POST_MEDIA_ID)
    assert replied == []


def test_unrecognized_post_uses_shared_media_handoff_not_attachment_limitation(post_route, monkeypatch):
    database, routing_id = post_route
    handed_off = []
    monkeypatch.setattr(app_module, "_handoff_unmapped_shared_media", lambda uid, **kwargs: handed_off.append((uid, kwargs)))

    app_module._handle_shared_attachment("uid_unrec", None, THIRD_PARTY_MEDIA_ID)
    assert handed_off == [("uid_unrec", {"source_type": "post", "media_id": THIRD_PARTY_MEDIA_ID, "shortcode": None})]


def test_unmapped_reel_uses_shared_media_handoff_not_attachment_limitation(post_route, monkeypatch):
    handed_off = []
    monkeypatch.setattr(app_module, "_handoff_unmapped_shared_media", lambda uid, **kwargs: handed_off.append((uid, kwargs)))
    app_module._handle_shared_attachment("uid_reel", {"shortcode": "unconfigured"}, None)
    assert handed_off == [("uid_reel", {"source_type": "reel", "media_id": None, "shortcode": "unconfigured"})]


@pytest.mark.parametrize("source_type,media_id,shortcode", [
    ("post", THIRD_PARTY_MEDIA_ID, None), ("reel", None, "unmapped-reel"),
])
def test_unmapped_identifiable_share_creates_one_waiting_handoff_notice(post_route, monkeypatch, source_type, media_id, shortcode):
    database, _routing_id = post_route
    monkeypatch.setattr(app_module.instagram_chatbot_control, "is_sender_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(app_module, "_cached_instagram_identity", lambda sender, *_a, **_k: {
        "customer_external_id": sender, "customer_username": None,
    })
    monkeypatch.setattr(app_module.escalator, "handle_escalation", lambda **_kwargs: None)
    sent = []
    monkeypatch.setattr(app_module, "_send_or_queue_instagram_reply", lambda uid, text, **kwargs: sent.append((uid, text, kwargs)))
    session_memory.clear_session("uid_handoff", "instagram")

    app_module._handoff_unmapped_shared_media(
        "uid_handoff", source_type=source_type, media_id=media_id, shortcode=shortcode,
    )

    handoff = database.get_active_handoff("instagram", "uid_handoff")
    assert handoff and handoff["status"] == "WAITING_FOR_HUMAN"
    assert len(sent) == 1
    assert "Manager of Kitchens" in sent[0][1]
    assert not any(phrase in sent[0][1].lower() for phrase in ("can't open", "cannot open", "can't access", "can't see"))

    app_module._handoff_unmapped_shared_media(
        "uid_handoff", source_type=source_type, media_id=media_id, shortcode=shortcode,
    )
    assert len(sent) == 1


def test_text_with_unmapped_shared_post_still_sends_only_the_handoff(post_route, monkeypatch):
    database, _routing_id = post_route
    monkeypatch.setattr(app_module.instagram_chatbot_control, "is_sender_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(app_module, "_cached_instagram_identity", lambda sender, *_a, **_k: {
        "customer_external_id": sender, "customer_username": None,
    })
    monkeypatch.setattr(app_module.escalator, "handle_escalation", lambda **_kwargs: None)
    monkeypatch.setattr(app_module.responder, "match_and_respond", lambda *_a, **_k: pytest.fail("unmapped share must bypass business templates"))
    sent = []
    monkeypatch.setattr(app_module, "_send_or_queue_instagram_reply", lambda uid, text, **kwargs: sent.append(text))
    session_memory.clear_session("uid_text_share", "instagram")

    app_module._process_instagram_event(
        "uid_text_share", "Tell me more", None,
        app_module.EntryPoint(source="shared_media"),
        shared_post_id=THIRD_PARTY_MEDIA_ID, shared_source_type="post",
    )

    handoff = database.get_active_handoff("instagram", "uid_text_share")
    assert handoff and handoff["status"] == "WAITING_FOR_HUMAN"
    assert len(sent) == 1 and "Manager of Kitchens" in sent[0]
    assert "can't open" not in sent[0].lower()


# --- follow-up precedence: same guarantee as the reel fix ------------------

def test_tell_me_more_after_a_paired_post_without_description_hands_off(post_route):
    from camellia.responder import responder
    database, routing_id = post_route
    database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    session_memory.clear_session("uid_tmore", "instagram")
    app_module._establish_shared_post_context("uid_tmore", POST_MEDIA_ID)

    text, matched, template_id, _images = responder.match_and_respond(
        "Tell me more", user_id="uid_tmore", channel="instagram",
    )
    facts = session_memory.get_facts("uid_tmore", "instagram")
    assert text
    assert matched is False
    assert template_id is None
    assert "manager of kitchens" in text.lower()
    assert facts["source_context_used"] is False


def test_learned_3_does_not_override_a_valid_post_source_context(post_route):
    """A stale last_topic must not survive the share, as with the reel fix."""
    from camellia.responder import responder
    database, routing_id = post_route
    database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    session_memory.clear_session("uid_stale", "instagram")
    facts = session_memory.get_facts("uid_stale", "instagram")
    facts["last_topic"] = "sondesh_general"
    app_module._establish_shared_post_context("uid_stale", POST_MEDIA_ID)
    assert session_memory.get_facts("uid_stale", "instagram")["last_topic"] is None

    text, matched, template_id, _images = responder.match_and_respond(
        "Tell me more", user_id="uid_stale", channel="instagram",
    )
    assert template_id != "learned_3"


def test_explicit_product_intent_overrides_the_post_priority(post_route):
    from camellia.responder import responder
    database, routing_id = post_route
    database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    session_memory.clear_session("uid_explicit", "instagram")
    app_module._establish_shared_post_context("uid_explicit", POST_MEDIA_ID)

    text, matched, template_id, _images = responder.match_and_respond(
        "What about Kanchagolla?", user_id="uid_explicit", channel="instagram",
    )
    used = bool(session_memory.get_facts("uid_explicit", "instagram").get("source_context_used"))
    assert used is False
    assert "signature" not in (template_id or "")


def test_delivery_intent_overrides_post_source_context(post_route):
    from camellia.responder import responder
    database, routing_id = post_route
    database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    session_memory.clear_session("uid_delivery", "instagram")
    app_module._establish_shared_post_context("uid_delivery", POST_MEDIA_ID)

    text, matched, template_id, _images = responder.match_and_respond(
        "Do you deliver to Delhi?", user_id="uid_delivery", channel="instagram",
    )
    used = bool(session_memory.get_facts("uid_delivery", "instagram").get("source_context_used"))
    assert used is False


# --- no order/CAM state is created by the share itself ---------------------

def test_a_paired_post_share_creates_no_order_or_handoff(post_route):
    database, routing_id = post_route
    database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    session_memory.clear_session("uid_noorder", "instagram")
    app_module._establish_shared_post_context("uid_noorder", POST_MEDIA_ID)

    facts = session_memory.get_facts("uid_noorder", "instagram")
    draft = facts.get("order_draft") or {}
    assert not draft.get("flavour")
    assert not facts.get("cart")
    assert not facts.get("handoff_ref")


# --- caption never used, even end to end -----------------------------------

def test_caption_never_decides_routing_end_to_end(post_route):
    database, routing_id = post_route
    database.start_post_pairing(routing_id, ttl_seconds=600, started_by="admin")
    session_memory.clear_session("uid_caption", "instagram")
    message = {"attachments": [_post_attachment(
        THIRD_PARTY_MEDIA_ID, title="norem pak sondesh Dc2PmrQE3Y4",
    )]}
    media_id = shared_post_media_id(message)
    app_module._establish_shared_post_context("uid_caption", media_id)
    # The caption named the configured topic, but the id is what was bound —
    # not something inferred from the caption text.
    assert database.get_instagram_source_route(routing_id)["media_id"] == THIRD_PARTY_MEDIA_ID


# --- admin endpoints ---------------------------------------------------

def test_pairing_endpoints_require_admin_auth():
    from fastapi.testclient import TestClient
    import camellia.database as db_module
    db_module.db.init_db()
    from camellia.app import app
    client = TestClient(app)
    assert client.post("/admin/instagram-routes/1/pairing/start").status_code in (401, 403, 503)
    assert client.get("/admin/instagram-routes/1/pairing").status_code in (401, 403, 503)
    assert client.post("/admin/instagram-routes/1/pairing/cancel").status_code in (401, 403, 503)
