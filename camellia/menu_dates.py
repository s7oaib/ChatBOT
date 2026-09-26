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


def parse_week_end_date(week_of: Optional[str], today: Optional[date] = None) -> Optional[date]:
    """
    The last day a menu labelled `week_of` is still current, or None when the
    text carries no readable date.

    Reads the range's END rather than its start, since that is what decides
    when the menu stops applying. A missing year resolves to whichever year
    puts the printed day nearest today — see _nearest_year.
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

    day = max(days)
    year_match = _YEAR_RE.search(text)
    if year_match:
        try:
            return date(int(year_match.group(1)), month_num, day)
        except ValueError:
            # A day the month does not have (31 September) means the read was
            # wrong; treat it as unreadable rather than inventing a nearby date.
            return None
    return _nearest_year(month_num, day, today or today_ist())


_ORDINAL = r"(?:st|nd|rd|th)?"
_MONTH_TOKEN = r"(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")"
_RANGE_SEPARATOR = r"\s*(?:-|–|—|to)\s*"
_DAY_FIRST_RANGE = re.compile(
    rf"\b(?P<start_day>\d{{1,2}}){_ORDINAL}\s+(?P<start_month>{_MONTH_TOKEN})"
    rf"(?:,?\s+(?P<start_year>20\d{{2}}))?{_RANGE_SEPARATOR}"
    rf"(?P<end_day>\d{{1,2}}){_ORDINAL}\s+(?P<end_month>{_MONTH_TOKEN})"
    rf"(?:,?\s+(?P<end_year>20\d{{2}}))?\b", re.IGNORECASE,
)
_MONTH_FIRST_RANGE = re.compile(
    rf"\b(?P<start_month>{_MONTH_TOKEN})\s+(?P<start_day>\d{{1,2}}){_ORDINAL}"
    rf"{_RANGE_SEPARATOR}(?:(?P<end_month>{_MONTH_TOKEN})\s+)?"
    rf"(?P<end_day>\d{{1,2}}){_ORDINAL}(?:,?\s+(?P<end_year>20\d{{2}}))?\b",
    re.IGNORECASE,
)
_NUMERIC_RANGE = re.compile(
    r"\b(?P<start_day>\d{1,2})/(?P<start_month>\d{1,2})(?:/(?P<start_year>20\d{2}))?"
    + _RANGE_SEPARATOR +
    r"(?P<end_day>\d{1,2})/(?P<end_month>\d{1,2})(?:/(?P<end_year>20\d{2}))?\b",
    re.IGNORECASE,
)


def parse_week_date_range(week_of: Optional[str], today: Optional[date] = None) -> Optional[Tuple[date, date]]:
    """Parse the complete printed range, or fail closed when either end is unclear.

    The existing end-only parser remains unchanged for legacy callers. Yearless
    ranges use the same bounded year choice, including December-to-January.
    A single explicit year belongs to the final date unless printed twice.
    """
    if not isinstance(week_of, str) or not week_of.strip():
        return None
    text = week_of.strip()
    match = next((found for pattern in (_NUMERIC_RANGE, _DAY_FIRST_RANGE, _MONTH_FIRST_RANGE)
                  if (found := pattern.search(text))), None)
    if not match:
        return None
    parts = match.groupdict()
    try:
        start_month = (int(parts["start_month"]) if parts["start_month"].isdigit()
                       else _MONTHS[parts["start_month"].lower()])
        end_token = parts.get("end_month") or parts["start_month"]
        end_month = int(end_token) if end_token.isdigit() else _MONTHS[end_token.lower()]
        start_day, end_day = int(parts["start_day"]), int(parts["end_day"])
        explicit_start = parts.get("start_year")
        explicit_end = parts.get("end_year")
        if explicit_end:
            end_year = int(explicit_end)
        elif explicit_start:
            end_year = int(explicit_start) + int((end_month, end_day) < (start_month, start_day))
        else:
            inferred = _nearest_year(end_month, end_day, today or today_ist())
            if inferred is None:
                return None
            end_year = inferred.year
        start_year = int(explicit_start) if explicit_start else end_year - int((start_month, start_day) > (end_month, end_day))
        start, end = date(start_year, start_month, start_day), date(end_year, end_month, end_day)
    except (KeyError, TypeError, ValueError):
        return None
    # A weekly flyer must describe a short, inclusive range. Anything longer
    # is more likely an OCR error than an authorisation to serve stale prices.
    return (start, end) if timedelta(days=0) <= end - start <= timedelta(days=14) else None


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
