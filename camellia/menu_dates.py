"""
Reads the date range printed on a weekly menu flyer well enough to know when
that menu has stopped being this week's.

The dates arrive as free text off an OCR pass — "24th July - 30th July, 2026",
"28th August - 3rd September, 2026", "19, 20, 21 JUNE" — so this parses the
handful of shapes the client's flyers actually use rather than pulling in a
general-purpose date library that would guess far more aggressively than is
safe here. Anything it cannot read confidently returns None. Such a menu stays
in history but is not eligible for current-menu selection until an operator
provides a valid date.
"""
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from camellia.config import config

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# Longest first so "sept" is not truncated to "sep", and "june" not to "jun".
_MONTH_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b", re.IGNORECASE
)
# A four-digit year cannot match here: \d{1,2} needs a word boundary after it,
# and "2026" has another digit where that boundary would have to be.
_DAY_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")

_IST = timezone(timedelta(hours=5, minutes=30))


def business_today(now: Optional[datetime] = None, timezone_name: Optional[str] = None) -> date:
    """Return today's date in the configured business timezone."""
    now = now or datetime.now(tz=timezone.utc)
    timezone_name = timezone_name or getattr(config, "BUSINESS_TIMEZONE", None) or os.getenv(
        "BUSINESS_TIMEZONE", "Asia/Kolkata"
    )
    try:
        local_zone = ZoneInfo(timezone_name)
    except Exception:
        # Asia/Kolkata is the safe project default even if a bad deployment
        # setting is supplied; never fall back to the host's UTC date.
        local_zone = _IST
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(local_zone).date()


def today_ist() -> date:
    """Backward-compatible name for the configured kitchen business date."""
    return business_today()


# How far ahead of today a yearless flyer date may plausibly sit. The flyers
# cover one week and are uploaded around the week they describe, so a fortnight
# absorbs an early upload while still rejecting a date most of a year out.
_FUTURE_GRACE = timedelta(days=14)


def _nearest_year(month: int, day: int, today: date) -> Optional[date]:
    """
    Resolves a yearless printed date to the most recent plausible year.

    Assuming the current year resurrected last year's menu every New Year: a
    flyer reading "24th July - 30th July" became 30 July *2027* once the
    calendar rolled over, sat months in the future, and so never expired — the
    stale July menu went back to being served to customers on 1 January.

    Picking the numerically nearest year is not enough either. By March, next
    July is closer than last July, and the same dead menu revives. What
    actually rules a candidate out is being implausibly far ahead: a weekly
    flyer's last day is never months away. So take the latest year that is not
    beyond the grace window, which keeps a genuine December-to-January span
    live while sending every stale menu to the past where it expires.
    """
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            # 29 February outside a leap year — that candidate simply does not
            # exist. Only give up if no year yields a real date.
            continue
    if not candidates:
        return None
    plausible = [d for d in candidates if d <= today + _FUTURE_GRACE]
    # year - 1 is always in the past, so `plausible` is never empty; the
    # fallback only guards a future caller widening the candidate list.
    return max(plausible) if plausible else min(candidates)


def _date_from_value(value: Any) -> Optional[date]:
    """Parse a structured menu date without guessing from free-form text."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_week_date_range(week_of: Optional[str], today: Optional[date] = None) -> Optional[Tuple[date, date]]:
    """Parse the existing human-readable ``week_of`` range into dates.

    Weekday/service labels are intentionally ignored. A range is valid only
    when a month and at least one real calendar day are present.
    """
    if not week_of:
        return None
    text = str(week_of).strip()
    if not text:
        return None
    months = list(_MONTH_RE.finditer(text))
    if not months:
        return None
    reference = today or business_today()
    explicit_year = _YEAR_RE.search(text)

    def resolve(month_num: int, day_num: int) -> Optional[date]:
        if not 1 <= day_num <= 31:
            return None
        if explicit_year:
            try:
                return date(int(explicit_year.group(1)), month_num, day_num)
            except ValueError:
                return None
        return _nearest_year(month_num, day_num, reference)

    first_month = months[0]
    last_month = months[-1]
    if len(months) > 1:
        first_days = [int(m.group(1)) for m in _DAY_RE.finditer(text[:first_month.start()])]
        middle_days = [int(m.group(1)) for m in _DAY_RE.finditer(text[first_month.end():last_month.start()])]
        last_days = [int(m.group(1)) for m in _DAY_RE.finditer(text[last_month.end():])]
        if not first_days or not middle_days:
            return None
        start = resolve(_MONTHS[first_month.group(1).lower()], min(first_days))
        end = resolve(_MONTHS[last_month.group(1).lower()], max(middle_days or last_days))
        if not start or not end:
            return None
        # A yearless December-to-January range resolves each endpoint nearest
        # the reference date; the end is the following year in that case.
        if not explicit_year and end < start:
            try:
                end = date(end.year + 1, end.month, end.day)
            except ValueError:
                return None
        return (start, end) if start <= end else None

    month = _MONTHS[first_month.group(1).lower()]
    before = [int(m.group(1)) for m in _DAY_RE.finditer(text[:first_month.start()])]
    after = [int(m.group(1)) for m in _DAY_RE.finditer(text[first_month.end():])]
    days = before or after
    if not days:
        return None
    dates = [resolve(month, day) for day in days]
    if any(item is None for item in dates):
        return None
    start = min(dates)
    end = max(dates)
    return start, end


def parse_menu_date_range(menu: Optional[Dict[str, Any]], today: Optional[date] = None) -> Optional[Tuple[date, date]]:
    """Resolve a menu's configured inclusive date range.

    Structured fields take precedence. A single structured date is a
    one-day menu. The legacy ``week_of`` string is the only supported fallback;
    weekday/service-window labels never create a date.
    """
    menu = menu or {}
    structured_keys = ("start_date", "end_date", "valid_from", "valid_until", "menu_date", "date")
    has_structured = any(key in menu and menu.get(key) not in (None, "") for key in structured_keys)
    if has_structured:
        raw_start = menu.get("start_date") or menu.get("valid_from") or menu.get("menu_date") or menu.get("date")
        raw_end = menu.get("end_date") or menu.get("valid_until") or menu.get("menu_date") or menu.get("date")
        start = _date_from_value(raw_start)
        end = _date_from_value(raw_end)
        if (raw_start and not start) or (raw_end and not end):
            return None
        if not start and not end:
            return None
        start = start or end
        end = end or start
        return (start, end) if start <= end else None
    return parse_week_date_range(menu.get("week_of"), today=today)


def menu_is_active(menu: Optional[Dict[str, Any]], today: Optional[date] = None) -> bool:
    """Whether a published menu is active for the local business date."""
    menu = menu or {}
    if menu.get("is_published", True) is False:
        return False
    date_range = parse_menu_date_range(menu, today=today)
    if not date_range:
        return False
    local_date = today or business_today()
    start, end = date_range
    return start <= local_date <= end


def resolve_current_menu(
    weekly_menus: Optional[List[Dict[str, Any]]], today: Optional[date] = None
) -> Dict[str, Any]:
    """Return the deterministic date-active menu and safe validation state.

    If ranges overlap, later entries win because the existing write/publish
    flow treats the later entry as the more recently authoritative version.
    Expired/future/undated entries remain in the returned source list and are
    never deleted by resolution.
    """
    local_date = today or business_today()
    menus = list(weekly_menus or [])
    active: List[Tuple[int, Dict[str, Any], Tuple[date, date]]] = []
    invalid_dates: List[int] = []
    for index, menu in enumerate(menus):
        date_range = parse_menu_date_range(menu, today=local_date)
        if not date_range:
            invalid_dates.append(index)
            continue
        if menu_is_active(menu, today=local_date):
            active.append((index, menu, date_range))
    if active:
        index, menu, date_range = active[-1]
        return {
            "menu": menu,
            "index": index,
            "date_range": date_range,
            "today": local_date,
            "status": "active",
            "invalid_date_indexes": invalid_dates,
        }
    return {
        "menu": None,
        "index": None,
        "date_range": None,
        "today": local_date,
        "status": "no_menu" if not menus else ("invalid_date" if len(invalid_dates) == len(menus) else "no_active_range"),
        "invalid_date_indexes": invalid_dates,
    }


def parse_week_end_date(week_of: Optional[str], today: Optional[date] = None) -> Optional[date]:
    """
    The last day a menu labelled `week_of` is still current, or None when the
    text carries no readable date.

    Reads the range's END rather than its start, since that is what decides
    when the menu stops applying. A missing year resolves to whichever year
    puts the printed day nearest today — see _nearest_year.
    """
    parsed = parse_week_date_range(week_of, today=today)
    return parsed[1] if parsed else None


def is_expired(week_of: Optional[str], today: Optional[date] = None) -> bool:
    """
    True only when the menu's final day is definitively in the past.

    Unreadable dates are never expired, deliberately — see the module note.
    The end day itself still counts as current, so a menu running "28 Aug -
    3 Sep" is live all through the 3rd.
    """
    end = parse_week_end_date(week_of, today=today)
    if end is None:
        return False
    return end < (today or today_ist())


def prune_expired_weeks(
    weekly_menus: List[Dict[str, Any]], today: Optional[date] = None
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Drops menus whose date has passed, returning the kept list and the labels
    removed.

    The newest entry is always kept even when it has expired: it is the slot
    the next upload writes into, and emptying the array would leave the bot
    with no menu structure at all. An expired newest entry is handled at reply
    time instead, where the customer is told the menu is being updated rather
    than shown stale dishes.
    """
    if len(weekly_menus) <= 1:
        return weekly_menus, []

    kept: List[Dict[str, Any]] = []
    removed: List[str] = []
    for week in weekly_menus[:-1]:
        if is_expired(week.get("week_of"), today=today):
            removed.append(str(week.get("week_of") or "untitled week"))
        else:
            kept.append(week)
    kept.append(weekly_menus[-1])
    return kept, removed
