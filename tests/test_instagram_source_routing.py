import re
import urllib.parse
import uuid
from unittest.mock import patch

import pytest

from camellia import app
from camellia.config import config
from camellia.database import Database
from camellia.instagram_routing import (
    _preview_url_from_media,
    decode_route,
    lookup_instagram_media,
    normalize_instagram_url,
    product_catalogue,
    route_template_id,
    validate_route_payload,
)
from camellia.session_memory import session_memory
from camellia.responder import responder


def test_post_link_is_normalized_and_tracking_is_removed():
    value = normalize_instagram_url("https://www.instagram.com/reel/DdAnO6MTok1/?utm_source=ig_web_copy_link")
    assert value == {"canonical_url": "https://www.instagram.com/reel/DdAnO6MTok1/", "shortcode": "DdAnO6MTok1", "source_type": "reel"}


@pytest.mark.parametrize("url", [
    "https://example.com/reel/DdAnO6MTok1/",
    "https://www.instagram.com/camellia.artisanal/",
    "javascript:alert(1)",
])
def test_invalid_or_non_post_links_are_rejected(url):
    with pytest.raises(ValueError):
        normalize_instagram_url(url)


def test_product_picker_comes_from_canonical_catalogue():
    products = product_catalogue()
    assert products
    assert all(item["knowledge_id"] == "product:" + item["id"] for item in products)
    assert {"jolbhora", "bento"}.issubset({item["id"] for item in products})


def test_graph_lookup_verifies_media_without_inventing_metadata(monkeypatch):
    class Response:
        def __init__(self, payload): self.status_code, self._payload = 200, payload
        def json(self): return self._payload
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def get(self, url, params=None):
            if url.endswith("/page-1"):
                return Response({"instagram_business_account": {"id": "ig-1"}})
            return Response({"data": [{"id": "media-1", "media_type": "VIDEO", "permalink": "https://instagram.com/reel/DdAnO6MTok1/", "thumbnail_url": "https://cdn.example/thumb.jpg", "caption": "Verified caption"}]})
    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", "token")
    monkeypatch.setattr(config, "INSTAGRAM_AUTH_MODE", "legacy_page")
    monkeypatch.setattr(config, "INSTAGRAM_PAGE_ID", "page-1")
    monkeypatch.setattr("camellia.instagram_routing.httpx.Client", lambda *args, **kwargs: Client())
    result = lookup_instagram_media(normalize_instagram_url("https://instagram.com/reel/DdAnO6MTok1/"))
    assert result == {
        "metadata_status": "verified_graph_api",
        "verified_media_identifier": "media-1",
        "thumbnail_url": "https://cdn.example/thumb.jpg",
        "preview_url": "https://cdn.example/thumb.jpg",
        "media_type": "VIDEO",
        "graph_fields": {
            "id": True, "media_type": True, "media_url": False,
            "thumbnail_url": True, "permalink": True, "children": False,
        },
        "caption": "Verified caption",
    }


def test_priority_products_preserve_order_and_allow_empty():
    empty = validate_route_payload({"url": "https://instagram.com/p/ABC_123/", "priority_products": []})
    ordered = validate_route_payload({"url": "https://instagram.com/p/ABC_124/", "priority_products": ["jolbhora", "bento"]})
    assert empty["priority_products"] == []
    assert ordered["priority_products"] == ["jolbhora", "bento"]


def test_duplicate_unknown_and_malformed_products_are_rejected():
    base = {"url": "https://instagram.com/p/ABC_125/"}
    for priority in (["jolbhora", "jolbhora"], ["not-a-product"], "jolbhora"):
        with pytest.raises(ValueError):
            validate_route_payload({**base, "priority_products": priority})


def test_route_crud_disable_archive_and_unavailable_product_are_safe(tmp_path):
    database = Database(str(tmp_path / "routes.db"))
    clean = validate_route_payload({
        "url": "https://instagram.com/reel/ROUTE_1/?x=1",
        "media_id": "story-1", "ad_id": "ad-1", "routing_topic": "Jolbhora reel",
        "priority_products": ["jolbhora", "bento"], "knowledge_scope": ["products"],
    })
    saved = decode_route(database.save_instagram_source_route(clean))
    assert saved["priority_products"] == ["jolbhora", "bento"]
    assert database.find_instagram_source_route("ad_id", "ad-1")
    assert database.set_instagram_source_route_active(saved["routing_id"], False)
    assert database.find_instagram_source_route("ad_id", "ad-1") is None
    assert database.set_instagram_source_route_active(saved["routing_id"], True)
    with database.get_connection() as conn:
        conn.execute("UPDATE instagram_source_routes SET priority_products='[\"retired_product\"]' WHERE routing_id=?", (saved["routing_id"],))
        conn.commit()
    decoded = decode_route(database.list_instagram_source_routes()[0])
    assert decoded["unavailable_priority_products"] == ["retired_product"]
    assert database.archive_instagram_source_route(saved["routing_id"])
    assert database.list_instagram_source_routes() == []


def test_route_stores_ids_not_product_facts():
    route = validate_route_payload({"url": "https://instagram.com/p/IDSONLY/", "priority_products": ["jolbhora"]})
    assert route["priority_products"] == ["jolbhora"]
    assert "price" not in route and "availability" not in route


def test_every_catalogue_product_resolves_only_to_approved_template():
    assert all(route_template_id(item["id"]) == item["template_id"] for item in product_catalogue())


def test_ordered_priorities_handle_vague_then_what_else_without_overriding_explicit_intent():
    user_id = "route_priority_" + uuid.uuid4().hex
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {"priority_products": ["jolbhora", "bento"], "routing_topic": "reel", "active": True}
    common = {"user_id": user_id, "channel": "instagram", "referral_source": "ADS", "referral_ad_id": "route-ad"}
    # A bare greeting is conversational even when a source route is active;
    # use an actual referential follow-up to exercise ordered priorities.
    first = responder.match_and_respond("Tell me more", **common)
    second = responder.match_and_respond("what else", **common)
    explicit = responder.match_and_respond("what is the price of kanchagolla?", **common)
    assert first[2].startswith("source_jolbhora_")
    assert second[2].startswith("source_bento_")
    assert explicit[2] == "kanchagolla"
    assert facts["source_priority_cursor"] == 2


def test_admin_route_ui_has_ordering_crud_and_advanced_compatibility():
    html = app.get_admin_dashboard().body.decode()
    for expected in ("Instagram Link", "Priority Products", "+ Add Product", "moveIgPriority", "Remove", "Edit", "Disable", "Archive", "Advanced", "igAdId", "igMediaId", "igCampaignId"):
        assert expected in html


def test_carousel_preview_uses_first_suitable_child_image():
    assert _preview_url_from_media({
        "media_type": "CAROUSEL_ALBUM",
        "children": {"data": [
            {"media_type": "VIDEO", "thumbnail_url": "https://cdn.example/video.jpg"},
            {"media_type": "IMAGE", "media_url": "https://cdn.example/first-image.jpg"},
        ]},
    }) == "https://cdn.example/video.jpg"


def test_carousel_preview_falls_back_to_first_image_without_video_thumbnail():
    assert _preview_url_from_media({
        "media_type": "CAROUSEL_ALBUM",
        "children": {"data": [
            {"media_type": "VIDEO"},
            {"media_type": "IMAGE", "media_url": "https://cdn.example/first-image.jpg"},
        ]},
    }) == "https://cdn.example/first-image.jpg"


def test_webhook_audit_does_not_infer_url_or_shortcode():
    source = open(app.__file__, encoding="utf-8").read()
    assert 'route_for_inbound(\n            ad_id=trace["ad_id"], media_id=trace["media_id"]' in source
    assert "referral.shortcode" not in source


def test_general_handoff_is_one_callback_ack_and_minimal_cam():
    user_id = "route_general_" + uuid.uuid4().hex
    session_memory.clear_session(user_id, "instagram")
    with patch.object(app, "send_instagram_reply") as send, patch.object(app.escalator, "handle_escalation"), patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app._process_instagram_event(user_id, "I need an agent", "ad-source")
    assert send.call_count == 1
    notice = send.call_args.args[1]
    assert notice == "Of course. I’ve shared your message with our team and someone will get back to you shortly."
    assert "wa.me/" not in notice
