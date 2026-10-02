"""Durable, owner-scoped Instagram Post/Reel inventory."""

from __future__ import annotations

import os
import uuid
from threading import Lock
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import httpx

from camellia.config import config
from camellia.instagram_routing import _graph_failure_category, _media_list_details, normalize_instagram_url


_FIELDS = "id,caption,media_type,media_url,permalink,thumbnail_url,timestamp,children{media_type,media_url,thumbnail_url}"
_GRAPH_HOSTS = {"graph.instagram.com", "graph.facebook.com"}
_SYNC_LOCK = Lock()


def _staging_demo_enabled() -> bool:
    return os.getenv("CAMELLIA_STAGING_DEMO_MEDIA", "").lower() in {"1", "true", "yes"} or os.getenv("RAILWAY_SERVICE_NAME", "") == "ChatBOT-Staging"


def seed_staging_media(db) -> int:
    """Create clearly-labelled fixture cards only in the staging service."""
    if not _staging_demo_enabled():
        return 0
    examples = [
        ("demo-gallery-post-001", "IMAGE", "https://images.unsplash.com/photo-1576610616656-d3aa5d1f4534?w=800", "Demo post — context only"),
        ("demo-gallery-reel-001", "VIDEO", "https://images.unsplash.com/photo-1601050690597-df0568f70950?w=800", "Demo reel — context only"),
        ("demo-gallery-carousel-001", "CAROUSEL_ALBUM", "https://images.unsplash.com/photo-1578985545062-69928b1d9587?w=800", "Demo carousel — context only"),
    ]
    for media_id, media_type, preview, caption in examples:
        db.upsert_instagram_media({"instagram_media_id": media_id, "media_type": media_type,
                                   "permalink": f"https://www.instagram.com/p/{media_id}/",
                                   "caption": caption, "thumbnail_url": preview,
                                   "published_at": "2026-09-22T00:00:00Z", "sync_status": "DEMO"})
    return len(examples)


def _resolve_current_account(client: httpx.Client, token: str, version: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Resolve the Graph-authenticated professional account without logging credentials."""
    auth_mode = config.instagram_auth_mode()
    if auth_mode == "instagram_login":
        host = "graph.instagram.com"
        media_url = f"https://{host}/{version}/me/media"
        identity_url = f"https://{host}/{version}/me"
    else:
        media_url, setup_failure = _media_list_details(client, token, version)
        if not media_url:
            return None, None, None, setup_failure or "linked_account_unavailable"
        parsed = urlsplit(media_url)
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.scheme != "https" or parsed.hostname not in _GRAPH_HOSTS or len(parts) < 3 or parts[-1] != "media":
            return None, None, None, "linked_account_unavailable"
        host = parsed.hostname
        graph_version, linked_id = parts[-3], parts[-2]
        if graph_version != version or not linked_id.isdigit():
            return None, None, None, "linked_account_unavailable"
        identity_url = f"https://{host}/{version}/{linked_id}"

    response = client.get(identity_url, params={"fields": "id,username", "access_token": token})
    if response.status_code != 200:
        return None, None, media_url, _graph_failure_category(response)
    try:
        identity = response.json()
    except (ValueError, TypeError):
        return None, None, media_url, "malformed_response"
    if not isinstance(identity, dict):
        return None, None, media_url, "malformed_response"
    account_id = str(identity.get("id") or "").strip()
    username = str(identity.get("username") or "").strip().lstrip("@")
    if not account_id.isdigit() or not username:
        return account_id or None, username or None, media_url, "identity_unavailable"
    return account_id, username, media_url, None


def _base_result(status: str, *, current_account_id=None, current_username=None,
                 diagnostic_permalink=None) -> Dict[str, Any]:
    return {
        "status": status,
        "state": status,
        "current_account_id": current_account_id,
        "current_username": current_username,
        "discovered": 0,
        "updated": 0,
        "unchanged": 0,
        "hidden_foreign": 0,
        "hidden_stale": 0,
        "failures": 0,
        "pagination_complete": False,
        "diagnostic_permalink": diagnostic_permalink,
    }


def _sync_instagram_media(db, diagnostic_permalink: Optional[str] = None) -> Dict[str, Any]:
    """Sync only the configured Instagram account and reconcile after full pagination."""
    target = None
    if diagnostic_permalink:
        try:
            target = normalize_instagram_url(diagnostic_permalink)["canonical_url"]
        except ValueError:
            result = _base_result("FAILED")
            result.update(failures=1, failure_category="invalid_permalink")
            return result

    token = (config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    if not token:
        seeded = seed_staging_media(db)
        result = _base_result("NOT_CONFIGURED")
        result.update(demo_seeded=seeded,
                      message="Instagram credentials are not configured; no Graph request was made.")
        return result

    # Instagram Login's authenticated /me id is not the webhook account guard
    # id. It must be explicitly bound; matching a username is not sufficient.
    expected_id = config.instagram_media_owner_id()
    expected_username = str(config.INSTAGRAM_EXPECTED_USERNAME or "").strip().lstrip("@").casefold()
    if not expected_id or not expected_username:
        result = _base_result("FAILED")
        result.update(failures=1, pages=0, pagination_complete=False,
                      failure_category="linked_account_not_configured")
        return result

    counts = {"discovered": 0, "updated": 0, "unchanged": 0,
              "hidden_foreign": 0, "hidden_stale": 0}
    diagnostic = {"canonical_permalink": target, "status": "NOT_RETURNED_BY_OWNED_MEDIA_PAGINATION"} if target else None
    current_id = current_username = None
    version = config.INSTAGRAM_API_VERSION
    generation = str(uuid.uuid4())
    pages = 0
    visited_pages = set()
    inventory_rows = {}
    try:
        with httpx.Client(timeout=12.0) as client:
            current_id, current_username, verified_media_url, identity_failure = _resolve_current_account(client, token, version)
            if identity_failure:
                result = _base_result("FAILED", current_account_id=current_id,
                                      current_username=current_username, diagnostic_permalink=diagnostic)
                result.update(failures=1, pages=0, pagination_complete=False,
                              failure_category=identity_failure)
                return result
            media_url = verified_media_url
            if not media_url:
                result = _base_result("FAILED", current_account_id=current_id,
                                      current_username=current_username, diagnostic_permalink=diagnostic)
                result.update(failures=1, pages=0, pagination_complete=False,
                              failure_category="linked_account_unavailable")
                return result
            if current_id != expected_id or current_username.casefold() != expected_username:
                result = _base_result("FAILED", current_account_id=current_id,
                                      current_username=current_username, diagnostic_permalink=diagnostic)
                result.update(failures=1, pages=0, pagination_complete=False,
                              failure_category="linked_account_mismatch")
                return result
            url = media_url
            params = {"fields": _FIELDS, "limit": 100, "access_token": token}
            expected_host = urlsplit(media_url).hostname
            if expected_host not in _GRAPH_HOSTS:
                result = _base_result("FAILED", current_account_id=current_id,
                                      current_username=current_username, diagnostic_permalink=diagnostic)
                result.update(failures=1, pages=0, pagination_complete=False,
                              failure_category="linked_account_unavailable")
                return result
            while url:
                if url in visited_pages:
                    raise ValueError("repeated pagination link")
                visited_pages.add(url)
                parsed_page = urlsplit(url)
                if (parsed_page.scheme != "https" or parsed_page.hostname != expected_host
                        or parsed_page.netloc.casefold() != expected_host):
                    raise ValueError("untrusted pagination host")
                response = client.get(url, params=params)
                params = None  # Meta's paging.next includes its cursor.
                if response.status_code != 200:
                    result = _base_result("FAILED", current_account_id=current_id,
                                          current_username=current_username, diagnostic_permalink=diagnostic)
                    result.update(counts, failures=1, pages=pages,
                                  failure_category=_graph_failure_category(response))
                    return result
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    result = _base_result("FAILED", current_account_id=current_id,
                                          current_username=current_username, diagnostic_permalink=diagnostic)
                    result.update(counts, failures=1, pages=pages, failure_category="malformed_response")
                    return result
                pages += 1
                for raw in payload["data"]:
                    if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
                        result = _base_result("FAILED", current_account_id=current_id,
                                              current_username=current_username, diagnostic_permalink=diagnostic)
                        result.update(counts, failures=1, pages=pages, failure_category="malformed_response")
                        return result
                    media_id = str(raw["id"]).strip()
                    permalink = raw.get("permalink")
                    try:
                        identity = normalize_instagram_url(permalink) if permalink else {}
                    except ValueError:
                        identity = {}
                    canonical_permalink = identity.get("canonical_url")
                    if diagnostic and canonical_permalink == target:
                        diagnostic = {"canonical_permalink": target, "status": "FOUND",
                                      "media_id": media_id, "media_type": raw.get("media_type")}
                    preview_url = raw.get("thumbnail_url") or raw.get("media_url")
                    values = {
                        "media_type": raw.get("media_type") or "IMAGE",
                        "permalink": canonical_permalink or permalink,
                        "shortcode": identity.get("shortcode"),
                        "caption": raw.get("caption"),
                        "media_url": raw.get("media_url"),
                        "thumbnail_url": preview_url,
                        "published_at": raw.get("timestamp"),
                        "owner_instagram_account_id": current_id,
                        "owner_username": current_username,
                        "last_sync_generation": generation,
                        "inventory_state": "CURRENT",
                        "sync_status": "SYNCED",
                    }
                    inventory_rows[media_id] = {"instagram_media_id": media_id, **values}
                paging = payload.get("paging")
                if paging is None:
                    paging = {}
                if not isinstance(paging, dict):
                    raise ValueError("malformed pagination")
                next_url = paging.get("next")
                if next_url is not None and (not isinstance(next_url, str) or not next_url.strip()):
                    raise ValueError("malformed pagination link")
                url = next_url.strip() if isinstance(next_url, str) else None

        for media_id, row in inventory_rows.items():
            before = db.get_instagram_media_by_meta_id(media_id)
            if before is None:
                counts["discovered"] += 1
            elif any(before.get(key) != value for key, value in row.items()
                     if key not in {"last_sync_generation", "sync_status"}):
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1
        counts.update(db.apply_instagram_media_inventory(inventory_rows.values(), current_id, generation))
        result = _base_result("COMPLETED", current_account_id=current_id,
                              current_username=current_username, diagnostic_permalink=diagnostic)
        result.update(counts, pagination_complete=True, pages=pages)
        return result
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        category = ("timeout" if isinstance(exc, httpx.TimeoutException) else
                    "network" if isinstance(exc, httpx.HTTPError) else
                    "paging_failure" if isinstance(exc, ValueError) and "pagination" in str(exc) else
                    "malformed_response")
        result = _base_result("FAILED", current_account_id=current_id,
                              current_username=current_username, diagnostic_permalink=diagnostic)
        result.update(counts, failures=1, pages=pages,
                      failure_category=category)
        return result


def sync_instagram_media(db, diagnostic_permalink: Optional[str] = None) -> Dict[str, Any]:
    """Serialize syncs so an older completed generation cannot hide newer rows."""
    with _SYNC_LOCK:
        return _sync_instagram_media(db, diagnostic_permalink)
