"""Admin-only, read-only Marketing inventory. Local PR #58 aliases stay authoritative."""
import re
import time
import json
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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


def mapping_display(row):
    """Presentation from the already selected authority; never used for identity."""
    from camellia.client_qa import resolve_effective_approved_qa_by_id
    route = decode_route(row)
    approved = resolve_effective_approved_qa_by_id(route.get("approved_qa_id")) if route.get("response_mode") == "APPROVED_QA" else None
    route["display_label"] = route.get("routing_topic") or (approved.entry.question if approved else "Mapped source")
    route["display_description"] = route.get("source_description") or (approved.entry.answer[:500] if approved else "Approved Q&A unavailable" if route.get("response_mode") == "APPROVED_QA" else "No description added.")
    return route


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


def active_presentation_groups(rows):
    """Discovery only: exact identities never replace individual binding rows."""
    active = sorted((row for row in rows if row.get("active")), key=lambda row: row["ad_id"])
    media_urls, url_media = {}, {}
    for row in active:
        media = row.get("effective_instagram_media_id")
        url = _permalink(row.get("instagram_permalink_url"))
        if media and url:
            media_urls.setdefault(str(media), set()).add(url)
            url_media.setdefault(url, set()).add(str(media))
    groups = {}
    for row in active:
        media = str(row.get("effective_instagram_media_id") or "")
        url = _permalink(row.get("instagram_permalink_url"))
        if not media and url and len(url_media.get(url, ())) == 1:
            media = next(iter(url_media[url]))
        if media:
            key = ("media", media, url or "") if len(media_urls.get(media, ())) > 1 else ("media", media)
        elif url:
            key = ("permalink", url)
        elif row.get("creative_id"):
            key = ("creative", str(row["creative_id"]))
        else:
            key = ("ad", row["ad_id"])
        groups.setdefault(key, []).append(row["ad_id"])
    return [{"identity_kind": key[0], "identity": list(key[1:]), "member_ad_ids": members}
            for key, members in sorted(groups.items())]


def _delivery_read(client, url, token, params):
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
        raise AdsSyncError(_ERRORS.get(code, "ACCOUNT_OR_API_UNAVAILABLE"))
    return payload


def _delivery_pages(client, url, token, params, deadline):
    rows, cursors = [], set()
    for _ in range(100):
        if time.monotonic() > deadline:
            raise AdsSyncError("DELIVERY_TIMEOUT")
        payload = _delivery_read(client, url, token, params)
        if not isinstance(payload.get("data"), list):
            raise AdsSyncError("INVALID_RESPONSE")
        rows.extend(payload["data"])
        paging = payload.get("paging") or {}
        if not isinstance(paging, dict):
            raise AdsSyncError("INVALID_PAGINATION")
        next_url = paging.get("next")
        if not next_url:
            return rows
        if not isinstance(next_url, str):
            raise AdsSyncError("INVALID_PAGINATION")
        parsed = urlsplit(next_url)
        cursor = (parse_qs(parsed.query).get("after") or [None])[0]
        if parsed.scheme != "https" or parsed.netloc != "graph.facebook.com" or parsed.path != urlsplit(url).path or not cursor or cursor in cursors:
            raise AdsSyncError("INVALID_PAGINATION")
        cursors.add(cursor)
        params = {**params, "after": cursor}
    raise AdsSyncError("PAGINATION_LIMIT")


def _schedule_window(row, zone):
    """Intersect explicit ad and ad-set schedules; never derive a duration."""
    adset = row.get("adset") or {}
    if not isinstance(adset, dict):
        raise AdsSyncError("INVALID_SCHEDULE")

    def times(values):
        result = []
        for value in values:
            if value is None or value == "":
                continue
            try:
                stamp = datetime.fromisoformat(value)
                if stamp.tzinfo is None:
                    raise ValueError()
                result.append(stamp.astimezone(zone))
            except (TypeError, ValueError):
                raise AdsSyncError("INVALID_SCHEDULE") from None
        return result

    starts = times((row.get("ad_schedule_start_time"), adset.get("start_time")))
    ends = times((row.get("ad_schedule_end_time"), adset.get("end_time")))
    # Compare absolute instants, including repeated local hours at DST folds.
    start, end = max(starts, key=lambda stamp: stamp.timestamp(), default=None), min(ends, key=lambda stamp: stamp.timestamp(), default=None)
    if start and end and start.timestamp() >= end.timestamp():
        raise AdsSyncError("INVALID_SCHEDULE")
    return start, end


def delivery_evidence():
    """Read-only, fresh Meta status + today's reported impressions; never instant delivery."""
    token, account = config.META_MARKETING_ACCESS_TOKEN, ad_account_id()
    rule = "Current Meta ACTIVE status, within supplied ad/ad-set schedule [start, end), and positive ad-level impressions today (ad-account timezone). Reporting can lag; not instantaneous delivery."
    unavailable = {"status": "unavailable", "rule": rule, "ads": {}}
    if not token or not account:
        return {**unavailable, "error_category": "NOT_CONFIGURED"}
    try:
        base = f"https://graph.facebook.com/{config.INSTAGRAM_API_VERSION}/{account}"
        deadline = time.monotonic() + 120
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            account_data = _delivery_read(client, base, token, {"fields": "timezone_name"})
            zone = ZoneInfo(account_data["timezone_name"])
            day = datetime.now(zone).date().isoformat()
            statuses = _delivery_pages(client, base + "/ads", token,
                {"fields": "id,effective_status,ad_schedule_start_time,ad_schedule_end_time,adset{start_time,end_time}", "limit": 100}, deadline)
            active = set()
            schedules = {}
            seen = set()
            for row in statuses:
                if not isinstance(row, dict) or not re.fullmatch(r"[0-9]+", str(row.get("id") or "")) or row["id"] in seen or not isinstance(row.get("effective_status"), str):
                    raise AdsSyncError("INVALID_RESPONSE")
                seen.add(row["id"])
                schedules[row["id"]] = _schedule_window(row, zone)
                if row["effective_status"] == "ACTIVE":
                    active.add(row["id"])
            insights = _delivery_pages(client, base + "/insights", token,
                {"level": "ad", "fields": "ad_id,impressions,date_start,date_stop", "time_range": json.dumps({"since": day, "until": day}), "limit": 100}, deadline)
            evidence, seen = {}, set()
            for row in insights:
                if not isinstance(row, dict) or not re.fullmatch(r"[0-9]+", str(row.get("ad_id") or "")) or row["ad_id"] in seen or not re.fullmatch(r"[0-9]+", str(row.get("impressions") or "")) or row.get("date_start") != day or row.get("date_stop") != day:
                    raise AdsSyncError("INVALID_RESPONSE")
                seen.add(row["ad_id"])
                impressions = int(row["impressions"])
                if row["ad_id"] in active and impressions > 0:
                    evidence[row["ad_id"]] = {"impressions": impressions}
            checked = datetime.now(zone)
            if checked.date().isoformat() != day:
                raise AdsSyncError("REPORTING_DAY_CHANGED")
            schedule_diagnostic = {}
            for ad_id, (start, end) in schedules.items():
                reason = "NOT_STARTED" if start and checked.timestamp() < start.timestamp() else "ENDED" if end and checked.timestamp() >= end.timestamp() else "WITHIN_SCHEDULE"
                schedule_diagnostic[ad_id] = {"start_time": start.isoformat() if start else None,
                    "end_time": end.isoformat() if end else None, "eligibility": reason}
                if reason != "WITHIN_SCHEDULE":
                    evidence.pop(ad_id, None)
                elif ad_id in evidence and (start or end):
                    evidence[ad_id].update(start_time=start.isoformat() if start else None, end_time=end.isoformat() if end else None)
        return {"status": "available", "rule": rule, "ads": evidence, "reporting_date": day,
                "account_timezone": zone.key, "checked_at": checked.isoformat(), "raw_running": len(evidence),
                "schedule_source": "ad_schedule_start_time/ad_schedule_end_time intersected with adset.start_time/end_time",
                "schedules": schedule_diagnostic}
    except AdsSyncError as exc:
        return {**unavailable, "error_category": str(exc)}
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return {**unavailable, "error_category": "INVALID_RESPONSE"}
    except Exception:
        return {**unavailable, "error_category": "DELIVERY_READ_FAILED"}


def running_presentation_groups(rows, evidence):
    """Only complete, globally consistent media/permalink pairs allow multi-ID confirmation."""
    pairs, media_urls, url_media = {}, {}, {}
    for row in rows:
        media, url = str(row.get("effective_instagram_media_id") or ""), _permalink(row.get("instagram_permalink_url"))
        if media and url:
            media_urls.setdefault(media, set()).add(url)
            url_media.setdefault(url, set()).add(media)
        pairs[row["ad_id"]] = (media, url)
    groups = {}
    for row in sorted(rows, key=lambda row: row["ad_id"]):
        media, url = pairs[row["ad_id"]]
        verified = bool(media and url and len(media_urls[media]) == 1 and len(url_media[url]) == 1)
        key = ("exact", media, url) if verified else ("ad", row["ad_id"])
        group = groups.setdefault(key, {"identity": list(key[1:]), "member_ad_ids": [], "delivering_ad_ids": [], "verified_identity": verified})
        group["member_ad_ids"].append(row["ad_id"])
        if row["ad_id"] in evidence:
            group["delivering_ad_ids"].append(row["ad_id"])
    return [group for _, group in sorted(groups.items()) if group["delivering_ad_ids"]]


def ads_workspace(database, view="active"):
    routes = [mapping_display(row) for row in database.list_instagram_source_routes()]
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
    active_groups = active_presentation_groups(rows)
    delivery = delivery_evidence() if view == "running" else {"status": "not_requested", "ads": {}}
    running_groups = running_presentation_groups(rows, delivery["ads"])
    running_ids = {ad_id for group in running_groups for ad_id in group["member_ad_ids"]}
    delivery["unresolved_ad_ids"] = sorted(set(delivery["ads"]) - {row["ad_id"] for row in rows})
    for row in rows:
        row["delivery"] = delivery["ads"].get(row["ad_id"])
    counts = {"active": len(active_groups), "raw_active": sum(row["active"] for row in rows), "unmapped": sum(not row["mapped"] for row in rows),
              "mapped": sum(row["mapped"] for row in rows), "inactive": sum(not row["active"] and row["inventory_state"] != "OBSERVED" for row in rows), "all": len(rows)}
    counts["running"] = len(running_groups) if delivery["status"] == "available" else None
    if delivery["unresolved_ad_ids"]:
        counts["running"] = None
        delivery["resolved_group_count"] = len(running_groups)
    counts["raw_running"] = delivery.get("raw_running")
    filtered = [row for row in rows if view == "all" or (view == "active" and row["active"]) or
                (view == "running" and row["ad_id"] in running_ids) or
                (view == "inactive" and not row["active"] and row["inventory_state"] != "OBSERVED") or (view == "mapped" and row["mapped"]) or
                (view == "unmapped" and not row["mapped"])]
    return {"status": "success", "items": filtered, "active_groups": active_groups, "running_groups": running_groups, "delivery": delivery, "counts": counts, "sync": sync_status(database),
            "routes": [row for row in routes if row.get("active") and row.get("source_type") in {"post", "reel", "story"}]}
