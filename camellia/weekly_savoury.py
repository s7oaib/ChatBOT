"""Date-authoritative Weekly Savoury views over existing reviewed menu versions.

There is no second menu store: rows, flyer, approval and provenance remain in
menu_versions.approval_json. Only the explicit supersession decision is stored;
CURRENT/UPCOMING/EXPIRED are calculated in the kitchen's timezone on reads.
"""

import json
import re
from datetime import date, datetime, timedelta, timezone

from camellia.database import db
from camellia import menu_dates

WEEKLY_KEY = "weekly_vegetarian"
LEGACY_WEEKLY_KEYS = (WEEKLY_KEY, "weekly_non_vegetarian")
_IST = timezone(timedelta(hours=5, minutes=30))


def _parse_datetime(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _approval(version):
    try:
        return json.loads(version["approval_json"])
    except (TypeError, ValueError, KeyError):
        return {}


def lifecycle_versions(conn=None):
    if conn is None:
        with db.get_connection() as connection:
            return lifecycle_versions(connection)
    placeholders = ",".join("?" for _ in LEGACY_WEEKLY_KEYS)
    return [dict(row) for row in conn.execute(
        f"SELECT * FROM menu_versions WHERE menu_key IN ({placeholders}) "
        "AND approval_json IS NOT NULL AND valid_from IS NOT NULL ORDER BY id DESC",
        LEGACY_WEEKLY_KEYS,
    )]


def lifecycle_enabled(conn=None):
    return bool(lifecycle_versions(conn))


def version_status(version, today=None, now=None):
    if version.get("weekly_state") == "SUPERSEDED":
        return "SUPERSEDED"
    today = today or menu_dates.today_ist()
    try:
        start = date.fromisoformat(version["valid_from"])
        end = date.fromisoformat(version["valid_until"])
    except (KeyError, TypeError, ValueError):
        return "EXPIRED"
    if today < start:
        return "UPCOMING"
    if today > end:
        return "EXPIRED"
    deadline = _parse_datetime(version.get("fallback_until_at"))
    if deadline and (now or datetime.now(timezone.utc)) >= deadline:
        return "EXPIRED"
    return "CURRENT"


def _legacy_range(version, today):
    approval = _approval(version)
    label = (approval.get("menu_metadata") or {}).get("week_of") or ""
    parsed = menu_dates.parse_week_date_range(label, today=today)
    if parsed:
        return parsed
    uploaded = _parse_datetime(approval.get("approved_at") or version.get("changed_at"))
    if uploaded:
        return uploaded.astimezone(_IST).date(), (uploaded + timedelta(days=7)).astimezone(_IST).date()
    return None


def _legacy_current(conn, today, now):
    placeholders = ",".join("?" for _ in LEGACY_WEEKLY_KEYS)
    rows = [dict(row) for row in conn.execute(
        f"SELECT * FROM menu_versions WHERE menu_key IN ({placeholders}) "
        "AND approval_json IS NOT NULL AND valid_from IS NULL AND is_current=1 ORDER BY id DESC",
        LEGACY_WEEKLY_KEYS,
    )]
    for row in rows:
        dates = _legacy_range(row, today)
        if dates and dates[0] <= today <= dates[1]:
            uploaded = _parse_datetime(_approval(row).get("approved_at") or row.get("changed_at"))
            if uploaded and not menu_dates.parse_week_date_range(
                    (_approval(row).get("menu_metadata") or {}).get("week_of"), today=today):
                if now >= uploaded + timedelta(days=7):
                    continue
            return row
    return None


def current_version(today=None, now=None, conn=None):
    """At most one version may ground current-week answers; ambiguity fails shut."""
    if conn is None:
        with db.get_connection() as connection:
            return current_version(today=today, now=now, conn=connection)
    today = today or menu_dates.today_ist()
    now = now or datetime.now(timezone.utc)
    active = [row for row in lifecycle_versions(conn) if version_status(row, today, now) == "CURRENT"]
    if len(active) > 1:
        return None
    if active:
        return active[0]
    return _legacy_current(conn, today, now) if lifecycle_enabled(conn) else None


def overlap_versions(start, end, conn=None):
    if conn is None:
        with db.get_connection() as connection:
            return overlap_versions(start, end, connection)
    return [row for row in lifecycle_versions(conn)
            if row.get("weekly_state") != "SUPERSEDED"
            and row["valid_from"] <= end.isoformat() and row["valid_until"] >= start.isoformat()]


def display_range(start, end):
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)
    return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"


def _name(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _quantity(value):
    return re.sub(r"\s+", "", str(value or "").casefold())


def _approved_aliases(row):
    return {_name(alias) for alias in row.get("aliases", []) if _name(alias)}


def _match_previous(row, previous, used):
    name = _name(row.get("item"))
    quantity = _quantity(row.get("quantity"))
    exact = [(index, old) for index, old in enumerate(previous)
             if index not in used and _name(old.get("item")) == name and _quantity(old.get("quantity")) == quantity]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    names = [(index, old) for index, old in enumerate(previous) if index not in used and
             (name == _name(old.get("item")) or name in _approved_aliases(old))]
    return names[0] if len(names) == 1 else None


def _quantity_anomaly(old, new):
    pattern = r"^\s*(\d+(?:\.\d+)?)\s*(kg|g)\s*$"
    old_match, new_match = re.match(pattern, str(old or ""), re.I), re.match(pattern, str(new or ""), re.I)
    if not old_match or not new_match:
        return False
    old_grams = float(old_match[1]) * (1000 if old_match[2].lower() == "kg" else 1)
    new_grams = float(new_match[1]) * (1000 if new_match[2].lower() == "kg" else 1)
    return old_grams > 0 and (new_grams / old_grams >= 10 or new_grams / old_grams <= .1)


def compare_rows(rows, previous):
    """Exact canonical identity, then unique approved aliases; never fuzzy match."""
    changes = {"unchanged": [], "new": [], "removed": [], "price_changed": [],
               "quantity_changed": [], "suspicious_price": [], "suspicious_quantity": []}
    used = set()
    for row in rows:
        match = _match_previous(row, previous, used)
        if match is None:
            changes["new"].append(row)
            continue
        index, old = match
        used.add(index)
        if _quantity(old.get("quantity")) != _quantity(row.get("quantity")):
            change = {"before": old, "after": row}
            changes["quantity_changed"].append(change)
            if _quantity_anomaly(old.get("quantity"), row.get("quantity")):
                changes["suspicious_quantity"].append(change)
            continue  # Different variant: total-price ratio is not comparable.
        if old.get("price_inr") != row.get("price_inr"):
            change = {"before": old, "after": row}
            changes["price_changed"].append(change)
            try:
                ratio = float(row["price_inr"]) / float(old["price_inr"])
                if ratio >= 3 or ratio <= 1 / 3:
                    changes["suspicious_price"].append(change)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        else:
            changes["unchanged"].append(row)
    changes["removed"] = [old for index, old in enumerate(previous) if index not in used]
    return changes


def previous_rows(conn=None):
    if conn is None:
        with db.get_connection() as connection:
            return previous_rows(connection)
    version = current_version(conn=conn)
    if not version:
        versions = lifecycle_versions(conn)
        version = next((row for row in versions if row.get("weekly_state") != "SUPERSEDED"), None)
    if version:
        return _approval(version).get("rows", [])
    from camellia.config import config
    try:
        with open(config.MENU_JSON_PATH, encoding="utf-8") as stream:
            week = (json.load(stream).get("weekly_menus") or [{}])[-1]
    except (OSError, ValueError, TypeError):
        return []
    return [{"item": item.get("item") or "", "quantity": item.get("quantity") or "",
             "price_inr": item.get("price_inr"), "aliases": []}
            for section in ("vegetarian", "non_vegetarian") for item in (week.get(section) or [])]


def historical_item_match(message):
    """Identify an old weekly item only to withhold stale facts, never answer."""
    lowered = str(message or "").casefold()
    for version in lifecycle_versions():
        for row in _approval(version).get("rows", []):
            for name in [row.get("item"), *(row.get("aliases") or [])]:
                if name and re.search(r"(?<!\w)" + re.escape(name.casefold()) + r"(?!\w)", lowered):
                    return True
    return False


def legacy_baseline_current(today=None):
    """Transitional, dated menu.json week stays live until its printed end.

    An early scheduled reviewed version must not hide the still-valid legacy
    week. Undated seed/legacy content is never treated as current here.
    """
    from camellia.config import config
    try:
        with open(config.MENU_JSON_PATH, encoding="utf-8") as stream:
            weeks = json.load(stream).get("weekly_menus") or []
    except (OSError, ValueError, TypeError):
        return False
    if not weeks or not weeks[-1].get("is_published", True):
        return False
    dates = menu_dates.parse_week_date_range(weeks[-1].get("week_of"), today=today)
    today = today or menu_dates.today_ist()
    return bool(dates and dates[0] <= today <= dates[1])
