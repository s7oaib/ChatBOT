"""Persistent, truth-safe context for Instagram post and ad entry points.

Runtime matching uses only identifiers Meta actually supplied: the ad ID, the
story/media ID on a reply, and — for a reel shared into the DM — the canonical
permalink Meta puts on the attachment itself. That last one was captured in
production on 2026-09-11: ``type: "ig_reel"`` carrying ``payload.url`` =
``https://www.instagram.com/reel/<shortcode>/``, an ordinary permalink the
existing URL normalizer already reads.

A route points at canonical knowledge/product identifiers; it never copies
business facts into the routing table, and never reads a caption to decide
what a customer meant.
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


def _media_list_url(client: httpx.Client, token: str, version: str) -> Optional[str]:
    """
    Where this token can list the account's own media, or None.

    The two token flavours do not share a host. An Instagram Login token
    ("IGAA...") addresses the account directly on graph.instagram.com and is
    rejected by graph.facebook.com outright — which is what this function used
    to ask unconditionally, so every lookup on such a deployment came back as
    "permission_or_account_unavailable" and looked like a missing permission
    rather than the wrong address. A page token still has to go the long way
    round, via the page's linked instagram_business_account.
    """
    if token.startswith("IGAA"):
        return f"https://graph.instagram.com/{version}/me/media"
    base = f"https://graph.facebook.com/{version}"
    page_id = (config.INSTAGRAM_PAGE_ID or "me").strip()
    account = client.get(
        f"{base}/{page_id}",
        params={"fields": "instagram_business_account", "access_token": token},
    )
    account_data = account.json() if account.status_code == 200 else {}
    ig_id = ((account_data.get("instagram_business_account") or {}).get("id"))
    return f"{base}/{ig_id}/media" if ig_id else None


def verify_media_by_id(media_id: str) -> Dict[str, Any]:
    """
    Fetches one media object directly by id and reports its identity.

    Diagnostic only, not part of any customer-facing matcher: a direct
    by-id fetch is a stronger claim than presence in a paginated
    ``/me/media`` listing (which caps at a few hundred items and can miss
    older or differently-scoped media), and is the right tool for deciding
    whether an id captured off a real webhook event genuinely belongs to a
    post the admin has configured — before any routing logic is built on
    top of that answer.
    """
    token = (config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    if not token:
        return {"status": "not_configured"}
    if not media_id or not media_id.isdigit():
        return {"status": "invalid_media_id"}
    version = config.WHATSAPP_API_VERSION
    host = "graph.instagram.com" if token.startswith("IGAA") else "graph.facebook.com"
    url = f"https://{host}/{version}/{media_id}"
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.get(url, params={
                "fields": "id,permalink,media_type,timestamp", "access_token": token,
            })
        if response.status_code != 200:
            return {"status": "not_found_or_unauthorized", "http_status": response.status_code}
        data = response.json()
        permalink = data.get("permalink")
        try:
            identity = normalize_instagram_url(permalink) if permalink else None
        except ValueError:
            identity = None
        return {
            "status": "verified",
            "media_id": str(data.get("id") or media_id),
            "media_type": data.get("media_type"),
            "timestamp": data.get("timestamp"),
            "canonical_url": (identity or {}).get("canonical_url"),
            "shortcode": (identity or {}).get("shortcode"),
        }
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        return {"status": "lookup_failed", "detail": type(exc).__name__}


def lookup_instagram_media(identity: Dict[str, str]) -> Dict[str, Any]:
    """Best-effort verification against the connected Instagram account.

    Graph permissions and token types vary. Failure is deliberately returned
    as status, never converted into guessed metadata.
    """
    token = (config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    if not token:
        return {"metadata_status": "not_configured"}
    version = config.WHATSAPP_API_VERSION
    fields = "id,caption,media_type,media_url,permalink,thumbnail_url"
    try:
        with httpx.Client(timeout=8.0) as client:
            url = _media_list_url(client, token, version)
            if not url:
                return {"metadata_status": "permission_or_account_unavailable"}
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


def shared_reel_identity(message: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    The canonical identity of a reel shared into the DM, or None.

    Reads exactly one field — ``payload.url`` on an attachment whose ``type``
    is ``ig_reel`` — and puts it through the same deterministic normalizer the
    admin form uses, so a shared reel and a configured reel are compared as
    the same canonical string.

    Everything else Meta sends alongside it is deliberately ignored:

    * ``payload.title`` is the caption. It is customer-visible marketing text,
      not an identifier, and matching on it would let a caption decide what
      the business is taken to have said.
    * ``payload.reel_video_id`` is a video id that has never been verified to
      equal the Graph media id an operator configures, so treating the two as
      interchangeable would be a guess.
    * ``ig_post`` attachments carry a lookaside CDN URL with no shortcode in
      it, so a shared post cannot be identified this way at all and is left
      unmatched rather than approximated.
    """
    attachments = (message or {}).get("attachments")
    if not isinstance(attachments, list):
        return None
    for attachment in attachments:
        if not isinstance(attachment, dict) or attachment.get("type") != "ig_reel":
            continue
        payload = attachment.get("payload")
        if not isinstance(payload, dict):
            continue
        try:
            identity = normalize_instagram_url(payload.get("url"))
        except (ValueError, TypeError):
            # A URL we cannot parse is not a reel we can identify. Silence
            # here is correct: the alternative is guessing.
            continue
        if identity["source_type"] == "reel":
            return identity
    return None


def shared_post_media_id(message: Dict[str, Any]) -> Optional[str]:
    """
    The messaging-attachment id of a post shared into the DM, or None.

    Reads exactly one field — ``payload.ig_post_media_id`` on an attachment
    whose ``type`` is ``ig_post`` — and nothing else. Unlike a reel, a shared
    post's ``payload.url`` is a lookaside CDN link carrying no shortcode, so
    there is no deterministic permalink to normalize here; a direct Graph
    fetch of a captured id has independently confirmed it is not resolvable
    against the account's own media graph (verified in production, not
    assumed), so this id is meaningful only once an admin has explicitly
    paired it to a route — see instagram_post_pairings. ``payload.title`` is
    the caption and is never read for routing.
    """
    attachments = (message or {}).get("attachments")
    if not isinstance(attachments, list):
        return None
    for attachment in attachments:
        if not isinstance(attachment, dict) or attachment.get("type") != "ig_post":
            continue
        payload = attachment.get("payload")
        if not isinstance(payload, dict):
            continue
        media_id = str(payload.get("ig_post_media_id") or "").strip()
        if media_id:
            return media_id
    return None


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


def route_for_inbound(
    *, ad_id: Optional[str], media_id: Optional[str], shortcode: Optional[str] = None,
    post_media_id: Optional[str] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Precedence, strongest claim first: ad ID, then a story reply's verified
    media ID, then a shared reel's shortcode, then a shared post's media ID —
    which is admin-paired rather than Meta-verified, so it is tried last of
    all four and only after route_for_inbound is called with a genuine
    ``post_media_id``, never guessed from one of the other fields.
    """
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
    if shortcode:
        route = db.find_instagram_source_route("shortcode", str(shortcode))
        if route:
            return decode_route(route), "shared_media_url"
    if post_media_id:
        # A paired post's id lives in the same media_id column a story reply
        # would occupy — it is still an exact Meta identifier once bound, just
        # one an admin verified rather than one Meta labelled as a reply.
        route = db.find_instagram_source_route("media_id", str(post_media_id))
        if route:
            return decode_route(route), "shared_post_media_id"
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
