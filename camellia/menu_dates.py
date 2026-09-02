"""
Reads the date range printed on a weekly menu flyer well enough to know when
that menu has stopped being this week's.

The dates arrive as free text off an OCR pass — "24th July - 30th July, 2026",
"28th August - 3rd September, 2026", "19, 20, 21 JUNE" — so this parses the
handful of shapes the client's flyers actually use rather than pulling in a
general-purpose date library that would guess far more aggressively than is
safe here. Anything it cannot read confidently returns None, and a None date
never expires: wrongly hiding a perfectly good menu from every customer is a
worse failure than showing one a few days longer than intended.
"""
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

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


def today_ist() -> date:
    """Today in the kitchen's own timezone, not the server's."""
    return datetime.now(tz=_IST).date()


def parse_week_end_date(week_of: Optional[str], today: Optional[date] = None) -> Optional[date]:
    """
    The last day a menu labelled `week_of` is still current, or None when the
    text carries no readable date.

    Reads the range's END rather than its start, since that is what decides
    when the menu stops applying. A missing year is assumed to be the current
    one — the flyers are printed weekly, so the current year is right except
    across a New Year boundary.
    """
    if not week_of:
        return None
    text = str(week_of).strip()
    if not text:
        return None

    months = list(_MONTH_RE.finditer(text))
    if not months:
        # "Current Week (Friday Lunch - Thursday Dinner)" and bare service
        # windows land here: weekday names are not months, so nothing to expire.
        return None

    last_month = months[-1]
    month_num = _MONTHS[last_month.group(1).lower()]

    # Days belonging to the final month sit between the previous month name
    # and this one ("24th July - 30th July"), or immediately after it when the
    # flyer writes the month once at the end ("19, 20, 21 JUNE").
    span_start = months[-2].end() if len(months) > 1 else 0
    days = [int(m.group(1)) for m in _DAY_RE.finditer(text[span_start:last_month.start()])]
    if not days:
        days = [int(m.group(1)) for m in _DAY_RE.finditer(text[last_month.end():])]

    days = [d for d in days if 1 <= d <= 31]
    if not days:
        return None

    year_match = _YEAR_RE.search(text)
    year = int(year_match.group(1)) if year_match else (today or today_ist()).year

    try:
        return date(year, month_num, max(days))
    except ValueError:
        # A day the month does not have (31 September) means the read was
        # wrong; treat it as unreadable rather than inventing a nearby date.
        return None


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
