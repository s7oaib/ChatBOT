"""A shared carousel resolves through its synced child ids, without pairing.

Production 2026-10-14: the "Experience kolkata" carousel (Graph id
18283067548294124) arrived in the DM as ig_post_media_id 17914035549495134,
which Graph resolves to an IMAGE created one second before the carousel:
the carousel's first child.
"""
import pytest

import camellia.app as app_module
from camellia.database import Database
from camellia.instagram_media import sync_instagram_media
from camellia.instagram_routing import route_for_inbound
from camellia.session_memory import session_memory
from test_instagram_media_inventory import BASE, MEDIA_URL, FakeClient, FakeResponse, OWNER_ID, OWNER_USERNAME, _install_graph, _media

CAROUSEL_ID = "18283067548294124"
FIRST_CHILD_ID = "17914035549495134"
SECOND_CHILD_ID = "17914035549495199"


def _carousel(media_id=CAROUSEL_ID, children=(FIRST_CHILD_ID, SECOND_CHILD_ID)):
    return {**_media(media_id, "https://www.instagram.com/p/Deb2fHMkwK8/"), "media_type": "CAROUSEL_ALBUM",
            "children": {"data": [{"id": child, "media_type": "IMAGE"} for child in children]}}


@pytest.fixture
def database(tmp_path, monkeypatch):
    store = Database(str(tmp_path / "carousel.db"))
    import camellia.database as db_module
    for target in (db_module, app_module):
        monkeypatch.setattr(target, "db", store)
    monkeypatch.setattr("camellia.handoff.db", store)
    return store


def _route(store, media_id=CAROUSEL_ID, shortcode="Deb2fHMkwK8", active=True):
    return store.save_instagram_source_route({
        "source_type": "post", "canonical_url": f"https://www.instagram.com/p/{shortcode}/",
        "shortcode": shortcode, "media_id": media_id, "ad_id": None, "campaign_id": None,
        "routing_topic": "Experience kolkata", "source_description": "Experience Kolkata description.",
        "knowledge_scope": [], "escalation_policy": "human_if_unsupported", "priority_products": [],
        "active": active,
    })


def test_sync_records_children_and_a_shared_child_id_matches_the_carousel(database, monkeypatch):
    client = _install_graph(monkeypatch, {MEDIA_URL: FakeResponse({"data": [_carousel()]})})
    route = _route(database)
    assert route_for_inbound(ad_id=None, media_id=None, post_media_id=FIRST_CHILD_ID, source_type="post")[0] is None

    assert sync_instagram_media(database)["status"] == "COMPLETED"

    assert "children{id," in client.calls[1][1]["params"]["fields"]
    for child in (FIRST_CHILD_ID, SECOND_CHILD_ID):
        matched, method = route_for_inbound(ad_id=None, media_id=None, post_media_id=child, source_type="post")
        assert matched["routing_id"] == route["routing_id"] and method == "shared_post_media_id"
    # The route keeps its synced Graph id; nothing was paired.
    assert database.get_instagram_source_route(route["routing_id"])["media_id"] == CAROUSEL_ID
    assert database.get_active_post_pairing() is None


def test_shared_child_sets_source_context_end_to_end(database, monkeypatch):
    _install_graph(monkeypatch, {MEDIA_URL: FakeResponse({"data": [_carousel()]})})
    route = _route(database)
    sync_instagram_media(database)
    sender = "carousel-share-sender"
    session_memory.clear_session(sender, "instagram")

    assert app_module._establish_shared_post_context(sender, FIRST_CHILD_ID) is True

    facts = session_memory.get_facts(sender, "instagram")
    assert facts["source_route"]["routing_id"] == route["routing_id"]
    assert database.get_active_handoff("instagram", sender) is None


def test_a_later_sync_without_the_carousel_forgets_its_children(database, monkeypatch):
    _install_graph(monkeypatch, {MEDIA_URL: FakeResponse({"data": [_carousel()]})})
    _route(database)
    sync_instagram_media(database)
    _install_graph(monkeypatch, {MEDIA_URL: FakeResponse({"data": [_media("other-post", "https://www.instagram.com/p/OTHER1/")]})})

    assert sync_instagram_media(database)["status"] == "COMPLETED"

    assert database.find_shared_post_route(FIRST_CHILD_ID) is None


def test_a_failed_sync_keeps_the_last_complete_child_map(database, monkeypatch):
    _install_graph(monkeypatch, {MEDIA_URL: FakeResponse({"data": [_carousel()]})})
    route = _route(database)
    sync_instagram_media(database)
    next_page = f"{BASE}/me/media?after=broken"
    client = FakeClient({f"{BASE}/me": FakeResponse({"id": OWNER_ID, "username": OWNER_USERNAME}),
                         MEDIA_URL: FakeResponse({"data": [], "paging": {"next": next_page}}),
                         next_page: FakeResponse({}, status_code=503)})
    monkeypatch.setattr("camellia.instagram_media.httpx.Client", lambda **_kwargs: client)

    assert sync_instagram_media(database)["status"] == "FAILED"

    assert database.find_shared_post_route(FIRST_CHILD_ID)["routing_id"] == route["routing_id"]


def test_child_of_a_disabled_route_or_ambiguous_child_fails_closed(database, monkeypatch):
    _install_graph(monkeypatch, {MEDIA_URL: FakeResponse({"data": [_carousel()]})})
    route = _route(database)
    sync_instagram_media(database)
    # Another route already holds the same id as a paired alias: ambiguous.
    other = _route(database, media_id="unrelated-graph-id", shortcode="OTHER2")
    pairing = database.start_post_pairing(other["routing_id"], ttl_seconds=600, started_by="admin")
    database.bind_post_pairing(pairing["id"], FIRST_CHILD_ID)
    assert database.find_shared_post_route(FIRST_CHILD_ID) is None
    # A disabled carousel route never matches through its children.
    with database.get_connection() as conn:
        conn.execute("UPDATE instagram_source_routes SET active=0 WHERE routing_id=?", (route["routing_id"],))
        conn.commit()
    assert database.find_shared_post_route(SECOND_CHILD_ID) is None


# --- an ad tap delivered as a share of the ad's own creative -----------------
# Production 2026-10-14 (CAM-32946): a customer tapped a MISHTI ad; Meta sent
# an ig_post share of the creative 17919714294242234 and no referral. The ads
# sync already knows that creative id for every MISHTI ad bound to Reel 91.

AD_CREATIVE_ID = "17919714294242234"


def _reel(store, shortcode="Deak-MkTacu", media_id="17927869947185763"):
    return store.save_instagram_source_route({
        "source_type": "reel", "canonical_url": f"https://www.instagram.com/reel/{shortcode}/",
        "shortcode": shortcode, "media_id": media_id, "ad_id": None, "campaign_id": None,
        "routing_topic": "sondesh delivery delhi NCR", "source_description": "Sondesh description.",
        "knowledge_scope": [], "escalation_policy": "human_if_unsupported", "priority_products": [], "active": True,
    })


def _ads(store, *ad_ids, creative=AD_CREATIVE_ID):
    store.apply_instagram_ads_inventory("act_1", [
        {"ad_id": ad_id, "name": "MISHTI", "effective_status": "ACTIVE", "effective_instagram_media_id": creative}
        for ad_id in ad_ids])


def test_shared_ad_creative_inherits_the_ads_binding(database):
    reel = _reel(database)
    _ads(database, "ad-050540", "ad-630540")
    for ad_id in ("ad-050540", "ad-630540"):
        database.bind_instagram_ad(ad_id, reel["routing_id"], bound_by="test")

    matched, method = route_for_inbound(ad_id=None, media_id=None, post_media_id=AD_CREATIVE_ID, source_type="post")

    assert matched["routing_id"] == reel["routing_id"] and method == "shared_post_media_id"


def test_shared_ad_creative_fails_closed_when_disabled_or_split_across_routes(database):
    reel = _reel(database)
    other = _reel(database, shortcode="OTHERREEL", media_id="other-reel-media")
    _ads(database, "ad-a", "ad-b")
    database.bind_instagram_ad("ad-a", reel["routing_id"], bound_by="test")
    database.bind_instagram_ad("ad-b", other["routing_id"], bound_by="test")
    assert database.find_shared_post_route(AD_CREATIVE_ID) is None
    database.set_instagram_ad_binding_active("ad-b", False)
    assert database.find_shared_post_route(AD_CREATIVE_ID)["routing_id"] == reel["routing_id"]
    database.set_instagram_ad_binding_active("ad-a", False)
    assert database.find_shared_post_route(AD_CREATIVE_ID) is None


def test_a_matched_shared_post_is_never_finalized_as_unmapped(database, monkeypatch):
    from unittest.mock import patch
    monkeypatch.setattr(app_module.config, "BYPASS_AD_GATE", True)
    reel = _reel(database)
    _ads(database, "ad-050540")
    database.bind_instagram_ad("ad-050540", reel["routing_id"], bound_by="test")
    sender = "ad-creative-sender"
    session_memory.clear_session(sender, "instagram")
    database.record_pending_instagram_media("post-event-1", sender, AD_CREATIVE_ID, grace_seconds=0)

    with patch.object(app_module, "_send_or_queue_instagram_reply", return_value={"status": "sent"}), \
         patch.object(app_module.escalator, "handle_escalation") as escalate:
        app_module._handle_shared_attachment(sender, None, AD_CREATIVE_ID)
        app_module._finalize_expired_unmapped_posts()

    assert database.get_pending_instagram_media_status(sender, AD_CREATIVE_ID) == "CONSUMED"
    assert session_memory.get_facts(sender, "instagram")["source_route"]["routing_id"] == reel["routing_id"]
    assert database.get_active_handoff("instagram", sender) is None
    assert not escalate.called


def test_cancel_also_revokes_a_wrongly_bound_pairing_alias(database):
    route = _route(database)
    pairing = database.start_post_pairing(route["routing_id"], ttl_seconds=600, started_by="admin")
    database.bind_post_pairing(pairing["id"], AD_CREATIVE_ID)
    assert database.find_shared_post_route(AD_CREATIVE_ID)["routing_id"] == route["routing_id"]

    assert database.cancel_post_pairing(route["routing_id"]) is True

    assert database.find_shared_post_route(AD_CREATIVE_ID) is None
    assert database.get_instagram_source_route(route["routing_id"])["media_id"] == CAROUSEL_ID
