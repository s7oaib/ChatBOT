"""
Permanent removal and archive/restore for Instagram Content Routing.

Four distinct actions, four distinct meanings:
* Disable  — active=0, route and history both survive, re-enable any time.
* Archive  — archived_at set, drops out of active matching, recoverable
  from the Archived tab.
* Restore  — archived_at cleared on the same row; no duplicate is created.
* Remove   — the row is gone. Not recoverable from the admin UI. Scope is
  the route (and its own pairing attempts) only — Topics, Menus, product
  catalogue, canonical knowledge, conversations and CAM handoffs are never
  reachable from a route's removal.
"""
import os

os.environ.setdefault("DB_PATH", "test_camellia.db")

import pytest
from fastapi.testclient import TestClient

import camellia.app as app_module
import camellia.database as db_module
from camellia.database import Database
from camellia.instagram_routing import route_for_inbound

ROUTE_A = {
    "source_type": "reel",
    "canonical_url": "https://www.instagram.com/reel/DdAnO6MTok1/",
    "shortcode": "DdAnO6MTok1",
    "media_id": None, "ad_id": None, "campaign_id": None,
    "routing_topic": "Wedding / Event Sondesh",
    "knowledge_scope": [], "escalation_policy": "human_if_unsupported",
    "priority_products": ["jolbhora", "kanchagolla"], "active": True,
}
ROUTE_B = {
    "source_type": "post",
    "canonical_url": "https://www.instagram.com/p/Dc2PmrQE3Y4/",
    "shortcode": "Dc2PmrQE3Y4",
    "media_id": None, "ad_id": None, "campaign_id": None,
    "routing_topic": "norem pak sondesh",
    "knowledge_scope": [], "escalation_policy": "human_if_unsupported",
    "priority_products": ["signature_collection"], "active": True,
}


@pytest.fixture
def two_routes(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "routes.db"))
    a = database.save_instagram_source_route(dict(ROUTE_A))
    b = database.save_instagram_source_route(dict(ROUTE_B))
    monkeypatch.setattr(db_module, "db", database)
    monkeypatch.setattr(app_module, "db", database)
    return database, a["routing_id"], b["routing_id"]


@pytest.fixture
def admin_client(two_routes):
    db_module.db.init_db()
    client = TestClient(app_module.app)
    headers = {"X-Admin-Key": app_module.config.ADMIN_API_KEY} if app_module.config.ADMIN_API_KEY else {}
    return client, headers


# --- Remove: scope and permanence ------------------------------------------

def test_removing_route_a_deletes_only_route_a(two_routes):
    database, id_a, id_b = two_routes
    assert database.delete_instagram_source_route(id_a, removed_by="tester") is True
    assert database.get_instagram_source_route(id_a) is None
    remaining = database.get_instagram_source_route(id_b)
    assert remaining is not None
    assert remaining["routing_topic"] == "norem pak sondesh"
    assert remaining["priority_products"] == ROUTE_B["priority_products"] or "signature_collection" in remaining["priority_products"]


def test_removed_route_no_longer_matches_inbound_traffic(two_routes):
    database, id_a, id_b = two_routes
    database.delete_instagram_source_route(id_a, removed_by="tester")
    route, method = route_for_inbound(ad_id=None, media_id=None, shortcode="DdAnO6MTok1")
    assert route is None


def test_other_route_still_matches_after_removal(two_routes):
    database, id_a, id_b = two_routes
    database.delete_instagram_source_route(id_a, removed_by="tester")
    route, method = route_for_inbound(ad_id=None, media_id=None, shortcode="Dc2PmrQE3Y4")
    assert route is not None
    assert route["routing_topic"] == "norem pak sondesh"


def test_missing_routing_id_returns_false_not_an_exception(two_routes):
    database, id_a, id_b = two_routes
    assert database.delete_instagram_source_route(999999, removed_by="tester") is False


def test_removal_deletes_only_that_routes_own_pairing_attempts(two_routes):
    database, id_a, id_b = two_routes
    pairing_a = database.start_post_pairing(id_a, ttl_seconds=600, started_by="admin")
    database.start_post_pairing(id_b, ttl_seconds=600, started_by="admin")
    database.delete_instagram_source_route(id_a, removed_by="tester")
    with database.get_connection() as conn:
        rows = conn.execute("SELECT routing_id FROM instagram_post_pairings").fetchall()
    remaining_routing_ids = {row["routing_id"] for row in rows}
    assert id_a not in remaining_routing_ids
    assert id_b in remaining_routing_ids


def test_removal_writes_an_audit_row(two_routes):
    database, id_a, id_b = two_routes
    database.delete_instagram_source_route(id_a, removed_by="admin_key_123")
    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM instagram_route_removals WHERE routing_id = ?", (id_a,)
        ).fetchone()
    assert row is not None
    assert row["shortcode"] == "DdAnO6MTok1"
    assert row["routing_topic"] == "Wedding / Event Sondesh"
    assert row["removed_by"] == "admin_key_123"


# --- Disable / Archive / Remove stay distinct -------------------------------

def test_disable_is_not_remove(two_routes):
    database, id_a, id_b = two_routes
    database.set_instagram_source_route_active(id_a, False)
    route = database.get_instagram_source_route(id_a)
    assert route is not None
    assert route["active"] == 0
    assert route["archived_at"] is None


def test_archive_is_not_remove(two_routes):
    database, id_a, id_b = two_routes
    database.archive_instagram_source_route(id_a)
    route = database.get_instagram_source_route(id_a)
    assert route is not None  # still exists
    assert route["archived_at"] is not None


def test_archived_route_does_not_match_inbound_traffic(two_routes):
    database, id_a, id_b = two_routes
    database.archive_instagram_source_route(id_a)
    route, method = route_for_inbound(ad_id=None, media_id=None, shortcode="DdAnO6MTok1")
    assert route is None


def test_archive_removes_from_active_list(two_routes):
    database, id_a, id_b = two_routes
    database.archive_instagram_source_route(id_a)
    active_ids = {r["routing_id"] for r in database.list_instagram_source_routes()}
    assert id_a not in active_ids
    assert id_b in active_ids


def test_archived_route_appears_in_include_archived_listing(two_routes):
    database, id_a, id_b = two_routes
    database.archive_instagram_source_route(id_a)
    all_ids = {r["routing_id"] for r in database.list_instagram_source_routes(include_archived=True)}
    assert id_a in all_ids and id_b in all_ids


# --- Restore -----------------------------------------------------------

def test_restore_returns_route_to_active_matching(two_routes):
    database, id_a, id_b = two_routes
    database.archive_instagram_source_route(id_a)
    assert database.restore_instagram_source_route(id_a) is True
    route, method = route_for_inbound(ad_id=None, media_id=None, shortcode="DdAnO6MTok1")
    assert route is not None
    assert route["routing_topic"] == "Wedding / Event Sondesh"


def test_restore_creates_no_duplicate_row(two_routes):
    database, id_a, id_b = two_routes
    database.archive_instagram_source_route(id_a)
    database.restore_instagram_source_route(id_a)
    all_rows = database.list_instagram_source_routes(include_archived=True)
    matching = [r for r in all_rows if r["shortcode"] == "DdAnO6MTok1"]
    assert len(matching) == 1
    assert matching[0]["routing_id"] == id_a


def test_restore_preserves_topic_and_priority_products(two_routes):
    database, id_a, id_b = two_routes
    database.archive_instagram_source_route(id_a)
    database.restore_instagram_source_route(id_a)
    route = database.get_instagram_source_route(id_a)
    assert route["routing_topic"] == "Wedding / Event Sondesh"
    import json
    assert json.loads(route["priority_products"]) == ["jolbhora", "kanchagolla"]


def test_restore_of_a_never_archived_route_fails_safely(two_routes):
    database, id_a, id_b = two_routes
    assert database.restore_instagram_source_route(id_a) is False


def test_restore_does_not_resurrect_a_disabled_routes_active_flag(two_routes):
    """A route disabled before archiving comes back disabled, not live."""
    database, id_a, id_b = two_routes
    database.set_instagram_source_route_active(id_a, False)
    database.archive_instagram_source_route(id_a)
    database.restore_instagram_source_route(id_a)
    route = database.get_instagram_source_route(id_a)
    assert route["active"] == 0


# --- HTTP endpoints: auth and shape -----------------------------------

def test_permanent_delete_requires_admin_auth(admin_client, two_routes):
    client, headers = admin_client
    _database, id_a, _id_b = two_routes
    resp = client.delete(f"/admin/instagram-routes/{id_a}/permanent")
    assert resp.status_code in (401, 403, 503)


def test_restore_endpoint_requires_admin_auth(admin_client, two_routes):
    client, headers = admin_client
    _database, id_a, _id_b = two_routes
    resp = client.post(f"/admin/instagram-routes/{id_a}/restore")
    assert resp.status_code in (401, 403, 503)


def test_permanent_delete_endpoint_removes_and_returns_shape(admin_client, two_routes):
    client, headers = admin_client
    _database, id_a, _id_b = two_routes
    if not headers:
        pytest.skip("ADMIN_API_KEY not configured for this test run")
    resp = client.delete(f"/admin/instagram-routes/{id_a}/permanent", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["routing_id"] == id_a
    assert body["removed"] is True


def test_permanent_delete_missing_route_is_a_clean_404(admin_client):
    client, headers = admin_client
    if not headers:
        pytest.skip("ADMIN_API_KEY not configured for this test run")
    resp = client.delete("/admin/instagram-routes/999999/permanent", headers=headers)
    assert resp.status_code == 404


def test_archived_query_param_lists_only_archived_routes(admin_client, two_routes):
    client, headers = admin_client
    database, id_a, id_b = two_routes
    if not headers:
        pytest.skip("ADMIN_API_KEY not configured for this test run")
    database.archive_instagram_source_route(id_a)
    resp = client.get("/admin/instagram-routes?include_archived=true", headers=headers)
    assert resp.status_code == 200
    routing_ids = {r["routing_id"] for r in resp.json()["routes"]}
    assert routing_ids == {id_a}


def test_default_listing_excludes_archived_routes(admin_client, two_routes):
    client, headers = admin_client
    database, id_a, id_b = two_routes
    if not headers:
        pytest.skip("ADMIN_API_KEY not configured for this test run")
    database.archive_instagram_source_route(id_a)
    resp = client.get("/admin/instagram-routes", headers=headers)
    routing_ids = {r["routing_id"] for r in resp.json()["routes"]}
    assert id_a not in routing_ids
    assert id_b in routing_ids
