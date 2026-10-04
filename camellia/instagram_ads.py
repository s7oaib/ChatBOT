"""Admin-only, read-only Marketing inventory. Local PR #58 aliases stay authoritative."""
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx

from camellia.config import config
from camellia.instagram_routing import decode_route, normalize_instagram_url

_BASE_FIELDS = "id,name,status,effective_status,created_time,updated_time,campaign_id,adset_id"
_CREATIVE_FIELDS = (
    "id,name,effective_instagram_media_id,instagram_permalink_url,thumbnail_url",
    "id,effective_instagram_media_id,instagram_permalink_url",
    "id,effective_instagram_media_id",
    "id",
    "",
)
_ERRORS = {190: "INVALID_OR_EXPIRED_TOKEN", 10: "PERMISSION_DENIED", 200: "PERMISSION_DENIED",
           294: "PERMISSION_DENIED", 4: "RATE_LIMITED", 17: "RATE_LIMITED", 613: "RATE_LIMITED"}


def ad_account_id():
    value = str(config.META_AD_ACCOUNT_ID or "").strip()
    if value.startswith("act_"):
        value = value[4:]
    return "act_" + value if re.fullmatch(r"[0-9]+", value) else ""


def sync_status(database):
    account = ad_account_id()
    result = database.get_instagram_ads_sync(account)
    return {**result, "configured": bool(config.META_MARKETING_ACCESS_TOKEN and account),
            "marketing_token_configured": bool(config.META_MARKETING_ACCESS_TOKEN),
            "ad_account_configured": bool(config.META_AD_ACCOUNT_ID),
            "ad_account_valid": bool(account), "api_version": config.INSTAGRAM_API_VERSION,
            "active_ad_count": sum(bool(row["active"]) for row in database.list_instagram_ads_inventory(account))}


class AdsSyncError(Exception):
    """Carries only a fixed safe category, never an upstream message/URL."""


def _text(value, token, limit=500):
    if value is None:
        return None
    value = str(value)[:limit]
    return value.replace(token, "[redacted]") if token else value


def _permalink(value):
    try:
        return normalize_instagram_url(str(value))["canonical_url"] if value else None
    except ValueError:
        return None


def _thumbnail(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or "access_token" in parse_qs(parsed.query):
        return None
    return value


def _fetch_inventory(token, account):
    url = f"https://graph.facebook.com/{config.INSTAGRAM_API_VERSION}/{account}/ads"
    deadline = time.monotonic() + 540
    with httpx.Client(timeout=20.0, follow_redirects=False) as client:
        # Restart the entire read on optional-field rejection. Never mix pages
        # from different field sets or publish a partial fallback snapshot.
        for level, creative_fields in enumerate(_CREATIVE_FIELDS):
            fields = _BASE_FIELDS + (",creative{" + creative_fields + "}" if creative_fields else "")
            params = {"fields": fields, "limit": 100}
            items, seen_cursors = {}, set()
            for _ in range(1000):
                if time.monotonic() > deadline:
                    raise AdsSyncError("SYNC_TIMEOUT")
                try:
                    response = client.get(url, params=params, headers={"Authorization": "Bearer " + token})
                    payload = response.json()
                except (httpx.HTTPError, ValueError):
                    raise AdsSyncError("NETWORK_OR_INVALID_RESPONSE") from None
                if not isinstance(payload, dict):
                    raise AdsSyncError("INVALID_RESPONSE")
                error = payload.get("error") or {}
                if response.status_code != 200 or error:
                    code = error.get("code") if isinstance(error, dict) else None
                    message = str(error.get("message", "")).lower() if isinstance(error, dict) else ""
                    unavailable = code == 100 and "field" in message and any(word in message for word in ("nonexisting", "non-existing", "unsupported", "does not exist", "unknown"))
                    if unavailable and level < len(_CREATIVE_FIELDS) - 1:
                        break
                    raise AdsSyncError(_ERRORS.get(code, "OPTIONAL_FIELDS_UNAVAILABLE" if unavailable else "ACCOUNT_OR_API_UNAVAILABLE"))
                data = payload.get("data")
                if not isinstance(data, list):
                    raise AdsSyncError("INVALID_RESPONSE")
                for row in data:
                    if not isinstance(row, dict) or not re.fullmatch(r"[0-9]+", str(row.get("id") or "")) or not isinstance(row.get("effective_status"), str):
                        raise AdsSyncError("INVALID_RESPONSE")
                    creative = row.get("creative") or {}
                    if not isinstance(creative, dict):
                        raise AdsSyncError("INVALID_RESPONSE")
                    ad_id = str(row["id"])
                    if ad_id in items:
                        raise AdsSyncError("INCONSISTENT_PAGINATION")
                    item = {field: _text(row.get(field), token) for field in _BASE_FIELDS.split(",") if field != "id"}
                    item.update(ad_id=ad_id, creative_id=_text(creative.get("id"), token),
                                effective_instagram_media_id=_text(creative.get("effective_instagram_media_id"), token),
                                instagram_permalink_url=_permalink(creative.get("instagram_permalink_url")),
                                thumbnail_url=_thumbnail(_text(creative.get("thumbnail_url"), token, 4096)))
                    items[ad_id] = item
                paging = payload.get("paging") or {}
                if not isinstance(paging, dict):
                    raise AdsSyncError("INVALID_PAGINATION")
                next_url = paging.get("next")
                if not next_url:
                    return list(items.values()), level > 0
                # Do not follow URLs or tokens supplied by Graph. Reconstruct
                # the same account endpoint using only the opaque next cursor.
                if not isinstance(next_url, str):
                    raise AdsSyncError("INVALID_PAGINATION")
                parsed = urlsplit(next_url)
                if parsed.scheme != "https" or parsed.netloc != "graph.facebook.com" or parsed.path != urlsplit(url).path:
                    raise AdsSyncError("INVALID_PAGINATION")
                cursor = (parse_qs(parsed.query).get("after") or [None])[0]
                if not cursor or cursor in seen_cursors:
                    raise AdsSyncError("INVALID_PAGINATION")
                seen_cursors.add(cursor)
                params = {"fields": fields, "limit": 100, "after": cursor}
            else:
                raise AdsSyncError("PAGINATION_LIMIT")
    raise AdsSyncError("OPTIONAL_FIELDS_UNAVAILABLE")


def sync_instagram_ads(database):
    account = ad_account_id()
    token = config.META_MARKETING_ACCESS_TOKEN
    if not token or not account:
        category = "NOT_CONFIGURED" if not token or not config.META_AD_ACCOUNT_ID else "INVALID_ACCOUNT_ID"
        database.record_instagram_ads_sync_failure(account, category)
        return {"status": "error", **sync_status(database)}
    if not database.acquire_instagram_ads_sync(account):
        return {"status": "busy", **sync_status(database)}
    try:
        started = time.monotonic()
        items, reduced = _fetch_inventory(token, account)
        if time.monotonic() - started > 540:
            raise AdsSyncError("SYNC_TIMEOUT")
        database.apply_instagram_ads_inventory(account, items, reduced=reduced)
    except AdsSyncError as exc:
        database.record_instagram_ads_sync_failure(account, str(exc))
        return {"status": "error", **sync_status(database)}
    except Exception:
        # Exception strings can contain credentials or SQL inputs. Never log
        # or return them. Rollback retains the last complete snapshot.
        database.record_instagram_ads_sync_failure(account, "LOCAL_SYNC_FAILED")
        return {"status": "error", **sync_status(database)}
    return {"status": "success", **sync_status(database)}


def exact_route_suggestion(item, routes):
    media_id = item.get("effective_instagram_media_id")
    permalink = _permalink(item.get("instagram_permalink_url"))
    shortcode = normalize_instagram_url(permalink)["shortcode"] if permalink else None
    matches = []
    for route in routes:
        if not route.get("active") or route.get("archived_at") or route.get("source_type") not in {"post", "reel", "story"}:
            continue
        media_match = bool(media_id and str(route.get("media_id") or "") == str(media_id))
        url_match = bool(permalink and (_permalink(route.get("canonical_url")) == permalink or route.get("shortcode") == shortcode))
        if media_match or url_match:
            matches.append(route)
    if len(matches) != 1:
        return None, "AMBIGUOUS" if matches else "NO_EXACT_MATCH"
    candidate = matches[0]
    # Conflicting exact identities must not recommend the media-ID match.
    if media_id and candidate.get("media_id") and str(candidate["media_id"]) != str(media_id):
        return None, "AMBIGUOUS"
    if permalink and candidate.get("canonical_url") and _permalink(candidate["canonical_url"]) != permalink:
        return None, "AMBIGUOUS"
    return int(candidate["routing_id"]), "EXACT_MATCH"


def ads_workspace(database, view="active"):
    routes = [decode_route(row) for row in database.list_instagram_source_routes()]
    route_by_id = {row["routing_id"]: row for row in routes}
    bindings = {row["ad_id"]: row for row in database.list_instagram_ad_bindings()}
    inventory = {row["ad_id"]: row for row in database.list_instagram_ads_inventory(ad_account_id())}
    observed = {row["ad_id"]: row for row in database.list_observed_instagram_ads()}
    rows = []
    for ad_id in sorted(inventory.keys() | observed.keys() | bindings.keys()):
        item = dict(inventory.get(ad_id) or {})
        binding = bindings.get(ad_id)
        route = route_by_id.get(binding["routing_id"]) if binding else None
        suggestion, identity_status = exact_route_suggestion(item, routes)
        source = _permalink(item.get("instagram_permalink_url"))
        item.update(ad_id=ad_id, name=item.get("name") or (observed.get(ad_id) or {}).get("display_title") or "Observed ad",
                    inventory_state="CURRENT" if ad_id in inventory else "OBSERVED",
                    active=bool(item.get("active")), effective_status=item.get("effective_status") or "UNKNOWN",
                    mapped=bool(binding), binding_status=database.instagram_ad_binding_status(ad_id),
                    binding_active=bool(binding and binding["active"]), routing_id=binding["routing_id"] if binding else None,
                    mapping=route, suggested_routing_id=suggestion, identity_status=identity_status,
                    source_type=normalize_instagram_url(source)["source_type"] if source else route.get("source_type", "unknown") if route else "unknown",
                    history=[{key: event.get(key) for key in ("routing_id", "action", "occurred_at")} for event in database.list_instagram_ad_binding_history(ad_id)])
        rows.append(item)
    counts = {"active": sum(row["active"] for row in rows), "unmapped": sum(not row["mapped"] for row in rows),
              "mapped": sum(row["mapped"] for row in rows), "inactive": sum(not row["active"] and row["inventory_state"] != "OBSERVED" for row in rows), "all": len(rows)}
    filtered = [row for row in rows if view == "all" or (view == "active" and row["active"]) or
                (view == "inactive" and not row["active"] and row["inventory_state"] != "OBSERVED") or (view == "mapped" and row["mapped"]) or
                (view == "unmapped" and not row["mapped"])]
    return {"status": "success", "items": filtered, "counts": counts, "sync": sync_status(database),
            "routes": [row for row in routes if row.get("active") and row.get("source_type") in {"post", "reel", "story"}]}
