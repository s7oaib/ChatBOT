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
_SOURCE_TYPES = {"post", "reel", "ad", "story"}
_ESCALATION_POLICIES = {"canonical", "human_if_unsupported", "always_human"}
_MENU_MODES = {"NONE", "SELECTED", "INHERIT"}


def _graph_failure_category(response: httpx.Response, *, media_lookup: bool = False) -> str:
    """Map Graph failures to a small safe diagnostic vocabulary."""
    try:
        error = response.json().get("error") or {}
    except (ValueError, TypeError):
        error = {}
    code = error.get("code")
    message = str(error.get("message") or "").lower()
    if code == 190 or "invalid oauth" in message or "application has been deleted" in message:
        return "invalid/expired token"
    if code in {10, 200, 294} or "permission" in message or "access" in message and "denied" in message:
        return "permission denied"
    if code == 100:
        return "unsupported media ID" if media_lookup else "permission denied"
    if "field" in message and ("unsupported" in message or "invalid" in message):
        return "field unavailable"
    if response.status_code in {404, 405}:
        return "Graph API endpoint mismatch"
    return "other"


def _media_field_presence(media: Dict[str, Any]) -> Dict[str, bool]:
    return {
        field: bool(media.get(field))
        for field in ("id", "media_type", "media_url", "thumbnail_url", "permalink", "children")
    }


def _media_list_details(client: httpx.Client, token: str, version: str) -> tuple[Optional[str], Optional[str]]:
    """Return the owned-media endpoint and a sanitized setup failure, if any."""
    if config.instagram_auth_mode() == "instagram_login":
        return f"https://graph.instagram.com/{version}/me/media", None
    base = f"https://graph.facebook.com/{version}"
    page_id = (config.INSTAGRAM_PAGE_ID or "me").strip()
    account = client.get(
        f"{base}/{page_id}",
        params={"fields": "instagram_business_account", "access_token": token},
    )
    if account.status_code != 200:
        return None, _graph_failure_category(account)
    try:
        ig_id = ((account.json().get("instagram_business_account") or {}).get("id"))
    except (ValueError, TypeError):
        ig_id = None
    return (f"{base}/{ig_id}/media", None) if ig_id else (None, "permission denied")


def _media_list_url(client: httpx.Client, token: str, version: str) -> Optional[str]:
    """
    Where this token can list the account's own media, or None.

    The two explicitly configured auth modes do not share a host. Instagram
    Login addresses the account directly on graph.instagram.com. A legacy Page
    token goes through graph.facebook.com and the linked business account.
    """
    return _media_list_details(client, token, version)[0]


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
    version = config.INSTAGRAM_API_VERSION
    host = (
        "graph.instagram.com"
        if config.instagram_auth_mode() == "instagram_login"
        else "graph.facebook.com"
    )
    url = f"https://{host}/{version}/{media_id}"
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.get(url, params={
                "fields": "id,permalink,media_type,media_url,thumbnail_url,children{media_type,media_url,thumbnail_url},timestamp",
                "access_token": token,
            })
        if response.status_code != 200:
            return {
                "status": "not_found_or_unauthorized",
                "http_status": response.status_code,
                "failure_category": _graph_failure_category(response, media_lookup=True),
            }
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
            "preview_url": _preview_url_from_media(data),
            "graph_fields": _media_field_presence(data),
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
    version = config.INSTAGRAM_API_VERSION
    fields = "id,caption,media_type,media_url,permalink,thumbnail_url,children{media_type,media_url,thumbnail_url}"
    try:
        with httpx.Client(timeout=8.0) as client:
            url, setup_failure = _media_list_details(client, token, version)
            if not url:
                return {"metadata_status": "permission_or_account_unavailable", "failure_category": setup_failure or "other"}
            params = {"fields": fields, "limit": 100, "access_token": token}
            for _ in range(3):
                response = client.get(url, params=params)
                if response.status_code != 200:
                    return {
                        "metadata_status": "permission_or_account_unavailable",
                        "failure_category": _graph_failure_category(response),
                    }
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
                            "preview_url": _preview_url_from_media(media),
                            "media_type": media.get("media_type"),
                            "graph_fields": _media_field_presence(media),
                            "caption": media.get("caption"),
                        }
                next_url = ((payload.get("paging") or {}).get("next"))
                if not next_url:
                    break
                url, params = next_url, None
    except (httpx.HTTPError, ValueError, TypeError):
        return {"metadata_status": "lookup_failed"}
    return {
        "metadata_status": "not_found_on_connected_account",
        "failure_category": "media belongs to another account",
    }


def _preview_url_from_media(media: Dict[str, Any]) -> Optional[str]:
    """Choose a stable-looking admin preview without using captions or routing data."""
    media_type = str(media.get("media_type") or "").upper()
    if media_type in {"VIDEO", "REEL"}:
        return media.get("thumbnail_url") or media.get("media_url")
    if media_type == "CAROUSEL_ALBUM":
        for child in (media.get("children") or {}).get("data", []) if isinstance(media.get("children"), dict) else []:
            if not isinstance(child, dict):
                continue
            child_type = str(child.get("media_type") or "").upper()
            preview = child.get("thumbnail_url") if child_type in {"VIDEO", "REEL"} else child.get("media_url")
            if preview:
                return preview
    return media.get("thumbnail_url") or media.get("media_url")


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
    """
    Normalize an admin route without accepting copied product facts.

    A `topic_id` is the simplified client-facing path: pick a Topic and the
    route's topic text, priority products, and knowledge scope all default
    from it. Those defaults only fill in gaps — an explicit, non-empty
    priority_products/knowledge_scope in the payload always wins, matching
    "the client may override it if needed." The Topic itself is never read
    again at matching or reply time; its fields are copied onto the route
    once, here, so the route stays the single thing the matcher and
    responder act on.
    """
    from camellia.database import db

    raw_url = str(payload.get("canonical_url") or payload.get("url") or "").strip()
    requested_source_type = str(payload.get("source_type") or "").strip().lower()
    if requested_source_type == "story" and raw_url:
        raise ValueError("Story routes use the Meta Story media ID, not an Instagram URL.")
    normalized = normalize_instagram_url(raw_url) if raw_url else {}
    source_type = normalized.get("source_type") or str(payload.get("source_type") or "ad").strip().lower()
    if source_type not in _SOURCE_TYPES:
        raise ValueError("source_type must be post, reel, ad, or story.")

    topic_id = str(payload.get("topic_id") or "").strip() or None
    topic = db.get_instagram_topic(topic_id) if topic_id else None
    if topic_id and not topic:
        raise ValueError("Unknown topic.")

    raw_menu_id = str(payload.get("menu_id") or "").strip()
    requested_menu_mode = str(payload.get("menu_mode") or "").strip().upper()
    if raw_menu_id:
        menu_mode = "SELECTED"
        menu_id = raw_menu_id
    elif requested_menu_mode:
        if requested_menu_mode not in _MENU_MODES:
            raise ValueError("menu_mode must be NONE, SELECTED, or INHERIT.")
        menu_mode = requested_menu_mode
        menu_id = None
    elif "menu_id" in payload:
        # The current admin form always sends menu_id. An empty value is an
        # intentional NONE selection, not permission to inherit the Topic's
        # default menu.
        menu_mode = "NONE"
        menu_id = None
    else:
        # Older API clients did not send a menu field. Preserve their existing
        # Topic-default behavior until they explicitly save the route again.
        menu_mode = "INHERIT"
        menu_id = topic.get("default_menu_id") or None if topic else None
    if menu_mode == "INHERIT" and topic:
        menu_id = topic.get("default_menu_id") or None
    if menu_mode == "SELECTED" and not menu_id:
        raise ValueError("A selected menu is required when menu_mode is SELECTED.")
    selected_menu = db.get_menu_catalogue_entry(menu_id) if menu_id else None
    if menu_id and not selected_menu:
        raise ValueError("Unknown menu.")
    if selected_menu and not bool(selected_menu.get("active")):
        raise ValueError("That menu is inactive. Choose an active menu or No menu.")

    priority = payload.get("priority_products", [])
    if priority is None:
        priority = []
    if not isinstance(priority, list) or any(not isinstance(item, str) for item in priority):
        raise ValueError("priority_products must be an ordered list of product IDs.")
    priority = [item.strip() for item in priority]
    if not priority and topic:
        priority = list(json.loads(topic["priority_products"]) or [])
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
    if not scope and topic:
        scope = list(json.loads(topic["knowledge_scope"]) or [])
    unknown_scope = [item for item in scope if item not in SOURCE_MAP]
    if unknown_scope:
        raise ValueError(f"Unknown knowledge scope: {unknown_scope[0]}")

    escalation = str(payload.get("escalation_policy") or "human_if_unsupported").strip()
    if escalation not in _ESCALATION_POLICIES:
        raise ValueError("Unknown escalation policy.")

    active = payload.get("active", True)
    if not isinstance(active, bool):
        raise ValueError("active must be true or false.")

    # routing_topic stays the display/back-compat field every existing part
    # of the code already reads (the "About {topic}:" prefix, admin lists,
    # audit rows). With a Topic selected it always mirrors the Topic's name,
    # so the two can never drift apart under the same route.
    routing_topic = (
        topic["name"] if topic else str(payload.get("routing_topic") or "").strip()
    )
    source_description = str(payload.get("source_description") or "").strip()
    if len(source_description) > 2000:
        raise ValueError("Description must be 2,000 characters or fewer.")

    preview_url = str(payload.get("preview_url") or "").strip() or None
    if preview_url and (len(preview_url) > 2000 or not preview_url.lower().startswith(("http://", "https://"))):
        raise ValueError("preview_url must be an http(s) URL.")
    media_type = str(payload.get("media_type") or "").strip() or None
    if media_type and len(media_type) > 80:
        raise ValueError("media_type is too long.")

    media_id = str(payload.get("story_media_id") or payload.get("media_id") or "").strip() or None
    if source_type == "story":
        if normalized:
            raise ValueError("Story routes use the Meta Story media ID, not an Instagram URL.")
        if not media_id:
            raise ValueError("A Story route requires a Story media ID.")
    clean = {
        "source_type": source_type,
        "canonical_url": normalized.get("canonical_url"),
        "shortcode": normalized.get("shortcode") or (str(payload.get("shortcode") or "").strip() or None if media_id else None),
        "media_id": media_id,
        "ad_id": str(payload.get("ad_id") or "").strip() or None,
        "campaign_id": str(payload.get("campaign_id") or "").strip() or None,
        "topic_id": topic_id,
        "menu_id": menu_id,
        "menu_mode": menu_mode,
        "preview_url": str(payload.get("preview_url") or "").strip() or None,
        "media_type": str(payload.get("media_type") or "").strip() or None,
        "routing_topic": routing_topic,
        "source_description": source_description,
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
    route["menu_mode"] = str(route.get("menu_mode") or "").strip().upper()
    if route["menu_mode"] not in _MENU_MODES:
        route["menu_mode"] = "SELECTED" if route.get("menu_id") else "INHERIT"
    available = product_ids()
    route["unavailable_priority_products"] = [
        item for item in route["priority_products"] if item not in available
    ]
    return route


def decode_topic(row: Dict[str, Any]) -> Dict[str, Any]:
    """Same JSON-string-to-list unpacking decode_route does, for a Topic row —
    without it, priority_products/knowledge_scope cross the API as an encoded
    string rather than an array."""
    topic = dict(row)
    for field in ("priority_products", "knowledge_scope"):
        value = topic.get(field)
        try:
            topic[field] = json.loads(value) if isinstance(value, str) else list(value or [])
        except (json.JSONDecodeError, TypeError):
            topic[field] = []
    topic["response_order"] = topic.get("response_order") if topic.get("response_order") in RESPONSE_ORDERS else "description_first"
    topic["active"] = bool(topic.get("active"))
    return topic


def route_for_inbound(
    *, ad_id: Optional[str], media_id: Optional[str], shortcode: Optional[str] = None,
    post_media_id: Optional[str] = None, source_type: Optional[str] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    For explicitly typed source events, the deterministic source identifier is
    checked first (Story media ID, Reel shortcode, or paired Post media ID).
    Legacy callers retain the historical ad-first precedence.
    """
    from camellia.database import db

    # A Story reply carries the strongest source evidence: the exact Meta
    # Story media id.  Keep ad referral matching unchanged for all other
    # events, but do not let an accompanying ad referral steal a configured
    # Story mapping.
    if media_id and source_type == "story_reply":
        route = db.find_instagram_source_route("media_id", str(media_id), source_type="story")
        if route:
            return decode_route(route), "story_media_id"
    if source_type == "reel" and shortcode:
        route = db.find_instagram_source_route("shortcode", str(shortcode), source_type="reel")
        if route:
            return decode_route(route), "shared_media_url"
    if source_type == "post" and post_media_id:
        route = db.find_instagram_source_route("media_id", str(post_media_id), source_type="post")
        if route:
            return decode_route(route), "shared_post_media_id"
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
        route = db.find_instagram_source_route(
            "media_id", str(media_id), source_type="story" if source_type == "story_reply" else None,
        )
        if route:
            return decode_route(route), "media_id"
    if shortcode:
        route = db.find_instagram_source_route(
            "shortcode", str(shortcode), source_type="reel" if source_type == "reel" else None,
        )
        if route:
            return decode_route(route), "shared_media_url"
    if post_media_id:
        # A paired post's id lives in the same media_id column a story reply
        # would occupy — it is still an exact Meta identifier once bound, just
        # one an admin verified rather than one Meta labelled as a reply.
        route = db.find_instagram_source_route(
            "media_id", str(post_media_id), source_type="post" if source_type == "post" else None,
        )
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


class InstagramTopicError(ValueError):
    """A topic create/update request failed validation."""


RESPONSE_ORDERS = {"description_first", "menu_first"}


def _slugify_topic(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "topic"


def create_topic(
    name: str, description: str, default_menu_id: Optional[str] = None,
    priority_products: Optional[List[str]] = None, knowledge_scope: Optional[List[str]] = None,
    active: bool = True, response_order: str = "description_first",
) -> Dict[str, Any]:
    from camellia.database import db

    name = (name or "").strip()
    description = (description or "").strip()
    if not name:
        raise InstagramTopicError("Topic name is required.")
    if not description:
        raise InstagramTopicError("Description is required.")
    response_order = str(response_order or "description_first").strip().lower()
    if response_order not in RESPONSE_ORDERS:
        raise InstagramTopicError("Reply order must be Description First or Menu First.")
    if db.find_instagram_topic_by_name(name):
        raise InstagramTopicError(f"A topic named '{name}' already exists.")

    priority_products = [str(p).strip() for p in (priority_products or []) if str(p).strip()]
    unknown = [p for p in priority_products if p not in product_ids()]
    if unknown:
        raise InstagramTopicError(f"Unknown priority product: {unknown[0]}")
    knowledge_scope = [str(s).strip() for s in (knowledge_scope or []) if str(s).strip()]
    unknown_scope = [s for s in knowledge_scope if s not in SOURCE_MAP]
    if unknown_scope:
        raise InstagramTopicError(f"Unknown knowledge scope: {unknown_scope[0]}")

    default_menu = db.get_menu_catalogue_entry(default_menu_id) if default_menu_id else None
    if default_menu_id and not default_menu:
        raise InstagramTopicError("Unknown default menu.")
    if default_menu and not bool(default_menu.get("active")):
        raise InstagramTopicError("The default menu must be active.")

    topic_id = _slugify_topic(name)
    if db.get_instagram_topic(topic_id):
        suffix = 2
        while db.get_instagram_topic(f"{topic_id}_{suffix}"):
            suffix += 1
        topic_id = f"{topic_id}_{suffix}"

    return db.create_instagram_topic(
        topic_id, name, description, default_menu_id or None,
        priority_products, knowledge_scope, active=active, response_order=response_order,
    )


def update_topic(topic_id: str, **fields: Any) -> Dict[str, Any]:
    from camellia.database import db

    if not db.get_instagram_topic(topic_id):
        raise KeyError(topic_id)
    if "name" in fields and fields["name"] is not None:
        name = fields["name"].strip()
        if not name:
            raise InstagramTopicError("Topic name is required.")
        existing = db.find_instagram_topic_by_name(name)
        if existing and existing["topic_id"] != topic_id:
            raise InstagramTopicError(f"A topic named '{name}' already exists.")
        fields["name"] = name
    default_menu_id = fields.get("default_menu_id")
    default_menu = db.get_menu_catalogue_entry(default_menu_id) if default_menu_id else None
    if default_menu_id and not default_menu:
        raise InstagramTopicError("Unknown default menu.")
    if default_menu and not bool(default_menu.get("active")):
        raise InstagramTopicError("The default menu must be active.")
    if "response_order" in fields:
        response_order = str(fields["response_order"] or "description_first").strip().lower()
        if response_order not in RESPONSE_ORDERS:
            raise InstagramTopicError("Reply order must be Description First or Menu First.")
        fields["response_order"] = response_order
    updated = db.update_instagram_topic(topic_id, **fields)
    if not updated:
        raise KeyError(topic_id)
    return updated


def migrate_legacy_routing_topics() -> int:
    """
    Backfills `topic_id` on routes that predate the Topic catalogue.

    Idempotent and safe to run on every startup: a route already carrying a
    topic_id is left untouched, and a route with no routing_topic text has
    nothing to migrate. Matching is by exact topic name only — never a guess
    from priority products or a shortcode — so a route that finds no
    matching Topic simply gets one created from its existing text.

    Uses set_instagram_source_route_topic() rather than
    save_instagram_source_route(), specifically so an archived route stays
    archived: the general save path unconditionally clears archived_at on
    update, which is correct for an admin editing a route on purpose but
    would be a real data-integrity bug here — a startup migration must never
    un-archive anything.
    """
    from camellia.database import db

    migrated = 0
    for row in db.list_instagram_source_routes(include_archived=True):
        if row.get("topic_id"):
            continue
        name = (row.get("routing_topic") or "").strip()
        if not name:
            continue
        topic = db.find_instagram_topic_by_name(name)
        if not topic:
            priority = json.loads(row.get("priority_products") or "[]")
            scope = json.loads(row.get("knowledge_scope") or "[]")
            topic = db.create_instagram_topic(
                _unique_topic_id(db, name), name, "", None, priority, scope, active=True,
            )
        db.set_instagram_source_route_topic(row["routing_id"], topic["topic_id"])
        migrated += 1
    return migrated


def _unique_topic_id(db_module, name: str) -> str:
    topic_id = _slugify_topic(name)
    if not db_module.get_instagram_topic(topic_id):
        return topic_id
    suffix = 2
    while db_module.get_instagram_topic(f"{topic_id}_{suffix}"):
        suffix += 1
    return f"{topic_id}_{suffix}"
