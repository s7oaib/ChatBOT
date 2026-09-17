"""Opt-in synchronization of the connected Instagram account's active Stories.

The service uses Meta's documented owned-Instagram-User ``/stories`` edge
only.  It discovers identity from Meta's numeric media ID, never from a
caption, URL guess, image, or nearest timestamp.  Sync writes inventory only;
it never sends a customer message or creates a mapping.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from camellia.config import config
from camellia.database import db as default_db


STORY_FIELDS = "id,media_type,media_url,thumbnail_url,permalink,timestamp"
SYNC_SOURCE = "META_STORY_SYNC"
_OWNER_PREFIX = "story-sync-"


def _safe_url(value: Any) -> Optional[str]:
    """Keep only non-sensitive URL identity for admin diagnostics."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    return f"{parsed.scheme.lower()}://{parsed.hostname}{parsed.path}"[:2000]


def _failure_category(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error") or {} if isinstance(payload, dict) else {}
    except (ValueError, TypeError):
        error = {}
    code = error.get("code")
    message = str(error.get("message") or "").lower()
    if code == 190 or "oauth" in message or "access token" in message:
        return "invalid_or_expired_token"
    if code in {10, 200, 294} or "permission" in message or "access denied" in message:
        return "permission_denied"
    if response.status_code == 429:
        return "rate_limited"
    if response.status_code >= 500:
        return "meta_server_error"
    if response.status_code in {404, 405}:
        return "endpoint_unavailable"
    return "meta_request_failed"


def _safe_error(response: httpx.Response) -> Dict[str, Any]:
    """Return diagnostics without copying Meta tokens or raw response text."""
    return {
        "category": _failure_category(response),
        "http_status": int(response.status_code),
    }


def _resolve_stories_endpoint(
    client: httpx.Client, token: str, version: str,
) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """Resolve the connected professional account and its owned Stories edge."""
    if token.startswith("IGAA"):
        base = f"https://graph.instagram.com/{version}"
        profile = client.get(
            f"{base}/me", params={"fields": "id", "access_token": token}
        )
        if profile.status_code != 200:
            return None, None, _safe_error(profile)
        try:
            payload = profile.json()
            account_id = str(payload.get("id") or "").strip() if isinstance(payload, dict) else ""
        except (ValueError, TypeError):
            account_id = ""
        return (
            f"{base}/me/stories", account_id or None,
            None if account_id else {"category": "malformed_account_response"},
        )

    base = f"https://graph.facebook.com/{version}"
    page_id = (config.INSTAGRAM_PAGE_ID or "").strip()
    if not page_id:
        return None, None, {"category": "instagram_page_not_configured"}
    page = client.get(
        f"{base}/{page_id}",
        params={"fields": "instagram_business_account", "access_token": token},
    )
    if page.status_code != 200:
        return None, None, _safe_error(page)
    try:
        payload = page.json()
        account = payload.get("instagram_business_account") if isinstance(payload, dict) else None
        account_id = str((account or {}).get("id") or "").strip() if isinstance(account, dict) else ""
    except (ValueError, TypeError):
        account_id = ""
    if not account_id:
        return None, None, {"category": "instagram_business_account_unavailable"}
    return f"{base}/{account_id}/stories", account_id, None


def _normalise_story(item: Any, account_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    story_id = str(item.get("id") or "").strip()
    if not story_id or len(story_id) > 256:
        return None
    published_at = str(item.get("timestamp") or "").strip() or None
    return {
        "story_media_id": story_id,
        "media_type": str(item.get("media_type") or "").strip() or None,
        "temporary_url": _safe_url(item.get("thumbnail_url") or item.get("media_url")),
        "permalink": _safe_url(item.get("permalink")),
        "published_at": published_at,
        "event_metadata": {
            "source": SYNC_SOURCE,
            "account_id": account_id,
        },
    }


def sync_instagram_stories(database=None) -> Dict[str, Any]:
    """Synchronize active Stories once, returning sanitized operational counts."""
    database = database or default_db
    token = (config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    base_result = {
        "status": "failed", "fetched": 0, "created": 0, "updated": 0,
        "unchanged": 0, "expired": 0, "failed": 0,
    }
    if not token:
        return {**base_result, "failure": {"category": "instagram_token_not_configured"}}

    owner = _OWNER_PREFIX + secrets.token_hex(8)
    if not database.try_acquire_instagram_story_sync_lock(owner):
        return {**base_result, "status": "busy", "failure": {"category": "sync_already_running"}}

    try:
        version = config.WHATSAPP_API_VERSION
        with httpx.Client(timeout=10.0) as client:
            endpoint, account_id, failure = _resolve_stories_endpoint(client, token, version)
            if failure or not endpoint or not account_id:
                return {**base_result, "failure": failure or {"category": "account_unavailable"}}
            raw_stories = []
            params: Optional[Dict[str, Any]] = {
                "fields": STORY_FIELDS, "limit": 100, "access_token": token,
            }
            next_url: Optional[str] = endpoint
            while next_url and len(raw_stories) < 1000:
                response = client.get(next_url, params=params)
                if response.status_code != 200:
                    return {**base_result, "account_id": account_id, "failure": _safe_error(response)}
                try:
                    payload = response.json()
                    page_stories = payload.get("data") if isinstance(payload, dict) else None
                except (ValueError, TypeError):
                    return {**base_result, "account_id": account_id, "failure": {"category": "malformed_meta_response"}}
                if not isinstance(page_stories, list):
                    return {**base_result, "account_id": account_id, "failure": {"category": "malformed_meta_response"}}
                raw_stories.extend(page_stories)
                paging = payload.get("paging") if isinstance(payload, dict) else None
                next_url = paging.get("next") if isinstance(paging, dict) else None
                # Meta's paging URL carries its own cursor and credentials;
                # never append or log a second token when following it.
                params = None

        active_ids = []
        malformed_item = False
        result = {**base_result, "status": "success", "account_id": account_id}
        for raw_story in raw_stories:
            story = _normalise_story(raw_story, account_id)
            if not story:
                result["failed"] += 1
                malformed_item = True
                continue
            story_id = story["story_media_id"]
            active_ids.append(story_id)
            existing = database.get_instagram_story_source(story_id)
            database.upsert_instagram_story_sync(story_id, **{key: story[key] for key in (
                "media_type", "temporary_url", "permalink", "published_at", "event_metadata"
            )})
            if not existing:
                result["created"] += 1
                continue
            comparable = ("media_type", "temporary_url", "permalink", "published_at", "story_status", "source")
            changed = any(existing.get(key) != ("ACTIVE" if key == "story_status" else SYNC_SOURCE if key == "source" else story.get(key)) for key in comparable)
            result["updated" if changed else "unchanged"] += 1

        if malformed_item:
            # A partial/malformed page must not be treated as a complete
            # active set, or it could incorrectly expire unrelated Stories.
            result["status"] = "partial"
            result["failure"] = {"category": "malformed_story_item"}
        else:
            cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
            result["expired"] = database.expire_missing_instagram_synced_stories(
                active_ids, expired_before=cutoff,
            )
        result["fetched"] = len(raw_stories)
        return result
    except (httpx.HTTPError, ValueError, TypeError):
        return {**base_result, "failure": {"category": "sync_request_failed"}}
    finally:
        database.release_instagram_story_sync_lock(owner)


async def story_sync_loop(stop_event: asyncio.Event, database=None) -> None:
    """Optional lifespan task; disabled unless explicitly configured."""
    database = database or default_db
    while not stop_event.is_set():
        await asyncio.to_thread(sync_instagram_stories, database)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.INSTAGRAM_STORY_SYNC_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
