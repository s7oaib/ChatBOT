"""Persistent, truth-safe context for Instagram post and ad entry points.

Instagram URLs are useful to an operator, but they are not present in the
messaging webhook.  Runtime matching therefore uses only identifiers that
Meta actually supplied (ad ID, or the story/media ID on a reply).  A route
points at canonical knowledge/product identifiers; it never copies business
facts into the routing table.
"""

import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

from camellia.config import config

from camellia.knowledge import CATALOGUE, SOURCE_MAP


_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SOURCE_TYPES = {"post", "reel", "ad"}
_ESCALATION_POLICIES = {"canonical", "human_if_unsupported", "always_human"}


def lookup_instagram_media(identity: Dict[str, str]) -> Dict[str, Any]:
    """Best-effort verification against the connected Instagram account.

    Graph permissions and token types vary. Failure is deliberately returned
    as status, never converted into guessed metadata.
    """
    token = (config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    if not token:
        return {"metadata_status": "not_configured"}
    version = config.WHATSAPP_API_VERSION
    page_id = (config.INSTAGRAM_PAGE_ID or "me").strip()
    base = f"https://graph.facebook.com/{version}"
    fields = "id,caption,media_type,media_url,permalink,thumbnail_url"
    try:
        with httpx.Client(timeout=8.0) as client:
            account = client.get(
                f"{base}/{page_id}",
                params={"fields": "instagram_business_account", "access_token": token},
            )
            account_data = account.json() if account.status_code == 200 else {}
            ig_id = ((account_data.get("instagram_business_account") or {}).get("id"))
            if not ig_id:
                return {"metadata_status": "permission_or_account_unavailable"}
            url = f"{base}/{ig_id}/media"
            params = {"fields": fields, "limit": 100, "access_token": token}
            for _ in range(3):
                response = client.get(url, params=params)
                if response.status_code != 200:
                    return {"metadata_status": "permission_or_account_unavailable"}
                payload = response.json()
                for media in payload.get("data") or []:
                    try:
                        media_identity = normalize_instagram_url(media.get("permalink"))
                    except ValueError:
                        continue
                    if media_identity["canonical_url"] == identity["canonical_url"]:
                        return {
                            "metadata_status": "verified_graph_api",
                            "verified_media_identifier": str(media.get("id") or "") or None,
                            "thumbnail_url": media.get("thumbnail_url") or media.get("media_url"),
                            "caption": media.get("caption"),
                        }
                next_url = ((payload.get("paging") or {}).get("next"))
                if not next_url:
                    break
                url, params = next_url, None
    except (httpx.HTTPError, ValueError, TypeError):
        return {"metadata_status": "lookup_failed"}
    return {"metadata_status": "not_found_on_connected_account"}


def normalize_instagram_url(raw_url: str) -> Dict[str, str]:
    """Return the canonical post/reel identity or raise ``ValueError``."""
    value = str(raw_url or "").strip()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("Enter a valid Instagram post or reel URL.") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in ("http", "https") or host not in ("instagram.com", "www.instagram.com"):
        raise ValueError("Only instagram.com post or reel links are supported.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0].lower() not in ("p", "reel"):
        raise ValueError("Use a specific Instagram /p/ post or /reel/ link, not a profile URL.")
    shortcode = parts[1]
    if not _SHORTCODE_RE.fullmatch(shortcode):
        raise ValueError("The Instagram post/reel shortcode is invalid.")
    source_type = "post" if parts[0].lower() == "p" else "reel"
    return {
        "canonical_url": f"https://www.instagram.com/{parts[0].lower()}/{shortcode}/",
        "shortcode": shortcode,
        "source_type": source_type,
    }


def product_catalogue() -> List[Dict[str, str]]:
    """Products/collections currently backed by canonical approved knowledge."""
    seen = set()
    items: List[Dict[str, str]] = []
    for record in CATALOGUE:
        if not record.knowledge_id.startswith("product:"):
            continue
        product_id = record.knowledge_id.split(":", 1)[1]
        if product_id in ("overview", "collection_difference") or product_id in seen:
            continue
        seen.add(product_id)
        items.append({
            "id": product_id,
            "label": product_id.replace("_", " ").title(),
            "knowledge_id": record.knowledge_id,
            "template_id": record.template_id,
        })
    return items


def product_ids() -> set[str]:
    return {item["id"] for item in product_catalogue()}


def validate_route_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize an admin route without accepting copied product facts."""
    raw_url = str(payload.get("canonical_url") or payload.get("url") or "").strip()
    normalized = normalize_instagram_url(raw_url) if raw_url else {}
    source_type = normalized.get("source_type") or str(payload.get("source_type") or "ad").strip().lower()
    if source_type not in _SOURCE_TYPES:
        raise ValueError("source_type must be post, reel, or ad.")

    priority = payload.get("priority_products", [])
    if priority is None:
        priority = []
    if not isinstance(priority, list) or any(not isinstance(item, str) for item in priority):
        raise ValueError("priority_products must be an ordered list of product IDs.")
    priority = [item.strip() for item in priority]
    if len(priority) != len(set(priority)):
        raise ValueError("A priority product can only be added once.")
    unknown_products = [item for item in priority if item not in product_ids()]
    if unknown_products:
        raise ValueError(f"Unknown priority product: {unknown_products[0]}")

    scope = payload.get("knowledge_scope", [])
    if scope is None:
        scope = []
    if not isinstance(scope, list) or any(not isinstance(item, str) for item in scope):
        raise ValueError("knowledge_scope must be a list of canonical knowledge domains.")
    scope = [item.strip() for item in scope]
    unknown_scope = [item for item in scope if item not in SOURCE_MAP]
    if unknown_scope:
        raise ValueError(f"Unknown knowledge scope: {unknown_scope[0]}")

    escalation = str(payload.get("escalation_policy") or "human_if_unsupported").strip()
    if escalation not in _ESCALATION_POLICIES:
        raise ValueError("Unknown escalation policy.")

    active = payload.get("active", True)
    if not isinstance(active, bool):
        raise ValueError("active must be true or false.")
    clean = {
        "source_type": source_type,
        "canonical_url": normalized.get("canonical_url"),
        "shortcode": normalized.get("shortcode"),
        "media_id": str(payload.get("media_id") or "").strip() or None,
        "ad_id": str(payload.get("ad_id") or "").strip() or None,
        "campaign_id": str(payload.get("campaign_id") or "").strip() or None,
        "routing_topic": str(payload.get("routing_topic") or "").strip(),
        "knowledge_scope": scope,
        "escalation_policy": escalation,
        "priority_products": priority,
        "active": active,
    }
    if not clean["canonical_url"] and not clean["media_id"] and not clean["ad_id"]:
        raise ValueError("Add an Instagram post/reel link, Media ID, or Ad ID.")
    if len(clean["routing_topic"]) > 160:
        raise ValueError("Routing topic must be 160 characters or fewer.")
    return clean


def decode_route(row: Dict[str, Any]) -> Dict[str, Any]:
    route = dict(row)
    for field in ("knowledge_scope", "priority_products"):
        value = route.get(field)
        try:
            route[field] = json.loads(value) if isinstance(value, str) else list(value or [])
        except (json.JSONDecodeError, TypeError):
            route[field] = []
    route["active"] = bool(route.get("active"))
    available = product_ids()
    route["unavailable_priority_products"] = [
        item for item in route["priority_products"] if item not in available
    ]
    return route


def route_for_inbound(*, ad_id: Optional[str], media_id: Optional[str]) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Specific Ad ID wins over media ID; old Ad-ID rows remain usable."""
    from camellia.database import db

    if ad_id:
        route = db.find_instagram_source_route("ad_id", str(ad_id))
        if route:
            return decode_route(route), "ad_id"
        legacy = db.get_ad_product_route(str(ad_id))
        if legacy:
            return {
                "routing_id": None,
                "source_type": "ad",
                "ad_id": str(ad_id),
                "routing_topic": legacy,
                "knowledge_scope": [],
                "escalation_policy": "human_if_unsupported",
                "priority_products": [legacy],
                "active": True,
                "legacy": True,
                "shortcode": None,
                "media_id": None,
            }, "legacy_ad_id"
    if media_id:
        route = db.find_instagram_source_route("media_id", str(media_id))
        if route:
            return decode_route(route), "media_id"
    return None, None


def route_template_id(product_id: str, *, price: bool = False) -> Optional[str]:
    legacy = {
        "sondesh": "sondesh_price_detailed" if price else "know_more",
        "savoury": "savoury_food_intro",
        "bento": "bento_details",
    }
    if product_id in legacy:
        return legacy[product_id]
    prefix = "price:" if price else "product:"
    wanted = prefix + str(product_id or "")
    for record in CATALOGUE:
        if record.knowledge_id == wanted:
            return record.template_id
    return None
