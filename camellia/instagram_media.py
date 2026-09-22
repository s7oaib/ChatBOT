"""Durable Instagram Post/Reel inventory; never a customer-message matcher."""

from __future__ import annotations

import os
from typing import Any, Dict

import httpx

from camellia.config import config
from camellia.instagram_routing import _graph_failure_category, _media_list_details, normalize_instagram_url


_FIELDS = "id,caption,media_type,media_url,permalink,thumbnail_url,timestamp,children{media_type,media_url,thumbnail_url}"


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


def sync_instagram_media(db) -> Dict[str, Any]:
    """Page through owned Meta media, preserving all local route configuration."""
    token = (config.INSTAGRAM_ACCESS_TOKEN or "").strip()
    if not token:
        seeded = seed_staging_media(db)
        return {"status": "NOT_CONFIGURED", "state": "NOT_CONFIGURED", "discovered": 0, "updated": 0,
                "unchanged": 0, "failures": 0, "demo_seeded": seeded,
                "message": "Instagram credentials are not configured; no Graph request was made."}
    discovered = updated = unchanged = 0
    version = config.INSTAGRAM_API_VERSION
    try:
        with httpx.Client(timeout=12.0) as client:
            url, setup_failure = _media_list_details(client, token, version)
            if not url:
                return {"status": "FAILED", "state": "FAILED", "failures": 1,
                        "failure_category": setup_failure or "permission denied"}
            params = {"fields": _FIELDS, "limit": 100, "access_token": token}
            while url:
                response = client.get(url, params=params)
                params = None  # Meta's paging.next already includes its cursor.
                if response.status_code != 200:
                    return {"status": "FAILED", "state": "FAILED", "failures": 1,
                            "failure_category": _graph_failure_category(response)}
                payload = response.json()
                for raw in payload.get("data") or []:
                    media_id = str(raw.get("id") or "")
                    if not media_id:
                        continue
                    before = db.get_instagram_media_by_meta_id(media_id)
                    permalink = raw.get("permalink")
                    try:
                        identity = normalize_instagram_url(permalink) if permalink else {}
                    except ValueError:
                        identity = {}
                    db.upsert_instagram_media({"instagram_media_id": media_id, "media_type": raw.get("media_type") or "IMAGE",
                                               "permalink": identity.get("canonical_url") or permalink,
                                               "shortcode": identity.get("shortcode"), "caption": raw.get("caption"),
                                               "media_url": raw.get("media_url"), "thumbnail_url": raw.get("thumbnail_url") or raw.get("media_url"),
                                               "published_at": raw.get("timestamp"), "sync_status": "SYNCED"})
                    if before is None: discovered += 1
                    elif any(before.get(k) != v for k, v in {"caption": raw.get("caption"), "media_url": raw.get("media_url"), "thumbnail_url": raw.get("thumbnail_url") or raw.get("media_url"), "published_at": raw.get("timestamp")}.items()): updated += 1
                    else: unchanged += 1
                url = (payload.get("paging") or {}).get("next")
        return {"status": "COMPLETED", "state": "COMPLETED", "discovered": discovered, "updated": updated, "unchanged": unchanged, "failures": 0}
    except (httpx.HTTPError, ValueError, TypeError):
        return {"status": "FAILED", "state": "FAILED", "failures": 1, "failure_category": "network or response unavailable"}
