"""
Stores the operator's uploaded weekly menu photo so DMs can show the actual
current flyer instead of one of the two fixed images baked into the repo
(Till Thursday.jpeg / From Friday.jpeg).

Saved next to menu.json rather than under images/, because images/ ships
inside the Docker image and is not on the durable volume — anything written
there is lost on the next deploy exactly like an unmounted menu.json would be.
menu.json's directory is already the durable one (see camellia/bootstrap.py),
so putting uploads there means one volume covers both.
"""
import json
import os
import random
import time
import uuid
from typing import Any, Dict, Optional

from camellia.config import config
from camellia.monitoring import get_logger
from camellia import menu_dates

log = get_logger("camellia.weekly_flyer")

# Deliberately generic — no dish names or prices. The vision model's extraction
# has not been reviewed by a human at the point this caption is sent, and a
# wrong price reaching a customer is worse than no price. The accurate weekly
# menu is already sent as text elsewhere (weekly_menu_veg / weekly_menu_nonveg),
# built straight from menu.json's structured items rather than copied here.
# Rotated so a customer asking twice in one session does not see identical text.
CAPTIONS = (
    "*This Week's Menu* — freshly cooked in small batches, updated just for you!",
    "*This Week's Menu* — hot off the kitchen, ready when you are!",
    "*This Week's Menu* — fresh ingredients, cooked daily, just for this week.",
    "*This Week's Menu* — a little taste of home, made fresh this week!",
)

_EXT_BY_MIME = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/png": "png",
    "image/webp": "webp",
}


def uploads_dir() -> str:
    """
    The durable directory uploaded flyers live in.

    Resolved from MENU_JSON_PATH rather than hardcoded, so it lands on the
    mounted volume automatically wherever that has been pointed — the same
    contract camellia/bootstrap.py relies on for menu.json itself.
    """
    return os.path.join(os.path.dirname(os.path.abspath(config.MENU_JSON_PATH)), "uploads")


def _state_path() -> str:
    return os.path.join(uploads_dir(), "weekly_flyer.json")


# How long a superseded flyer stays available. The client asked for two weeks:
# long enough that a customer who saw last week's menu can still be shown what
# they are referring to, short enough that nothing genuinely stale accumulates.
ARCHIVE_RETENTION_DAYS = 14


def archive_dir() -> str:
    """Where superseded flyers live, alongside the current one on the volume."""
    return os.path.join(uploads_dir(), "archive")


def _archive_state_path() -> str:
    return os.path.join(uploads_dir(), "flyer_archive.json")


def _read_archive() -> list:
    try:
        with open(_archive_state_path(), "r", encoding="utf-8") as f:
            entries = json.load(f)
        return entries if isinstance(entries, list) else []
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return []


def _write_archive(entries: list) -> None:
    try:
        with open(_archive_state_path(), "w", encoding="utf-8") as f:
            json.dump(entries, f)
    except Exception as e:
        log.warning("Could not persist flyer archive: %s", e)


def prune_archive(now: Optional[float] = None) -> list:
    """
    Drops archived flyers past the retention window, deleting their files too,
    and returns the labels removed.

    Retention is measured from each flyer's own upload time rather than from
    the printed week, because the printed date is optional — a flyer the OCR
    could not read a date off would otherwise never age out.
    """
    now = time.time() if now is None else now
    cutoff = now - (ARCHIVE_RETENTION_DAYS * 86400)
    kept, removed = [], []
    for entry in _read_archive():
        if float(entry.get("uploaded_at") or 0) >= cutoff:
            kept.append(entry)
            continue
        removed.append(str(entry.get("week_of") or "undated menu"))
        filename = os.path.basename(entry.get("url", ""))
        if filename:
            try:
                os.remove(os.path.join(archive_dir(), filename))
            except OSError:
                pass
    if removed:
        _write_archive(kept)
        log.info("Pruned %d expired archived flyer(s): %s", len(removed), ", ".join(removed))
    return removed


def list_archive(include_current: bool = False) -> list:
    """
    Archived flyers, newest first, with anything past retention already gone.

    The current flyer is excluded by default: "previous menus" means the ones
    that are no longer being served, and including the live one would show a
    customer the same photo twice.
    """
    prune_archive()
    entries = sorted(_read_archive(), key=lambda e: float(e.get("uploaded_at") or 0), reverse=True)
    entries = [e for e in entries if os.path.exists(
        os.path.join(archive_dir(), os.path.basename(e.get("url", "")))
    )]
    if include_current:
        return entries
    current_id = (get() or {}).get("id")
    return [e for e in entries if e.get("id") != current_id]


def save(
    image_bytes: bytes,
    mime_type: str,
    week_of: Optional[str] = None,
    service_window: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Persists an uploaded photo as the current weekly menu card.

    Overwrites whatever was there before: there is exactly one current weekly
    menu, and keeping old uploads around risks an outdated one being served by
    a stale reference. The filename does not change between uploads, so no
    caching layer can be left pointing at a photo that no longer exists.

    week_of, when the vision OCR could read one off the flyer, is folded into
    the caption so the date customers see updates in lockstep with the photo
    instead of the two silently drifting apart. It stays out of the caption
    entirely when absent — a blank date slot reads as broken, so this falls
    back to the same generic rotation used before dates were extracted at all.
    """
    ext = _EXT_BY_MIME.get((mime_type or "").lower(), "jpeg")
    directory = uploads_dir()
    os.makedirs(directory, exist_ok=True)
    filename = f"weekly_flyer.{ext}"

    with open(os.path.join(directory, filename), "wb") as f:
        f.write(image_bytes)

    # Only a genuinely readable date goes in the caption. The seed placeholder
    # is the literal string "Current Week (Friday Lunch - Thursday Dinner)",
    # which rendered as "*This Week's Menu* (Current Week (Friday Lunch -
    # Thursday Dinner))" — nested brackets and no date. If it does not parse as
    # a date it is not one, and the generic caption reads better than a label.
    week_of = (week_of or "").strip()
    has_real_date = menu_dates.parse_week_end_date(week_of) is not None
    caption = (
        f"*This Week's Menu* ({week_of}) — freshly cooked in small batches, updated just for you!"
        if has_real_date else random.choice(CAPTIONS)
    )
    uploaded_at = time.time()
    # A per-upload id, not the timestamp: two uploads in the same second would
    # collide on a second-resolution name, and the archive would silently keep
    # one photo for two weeks. It also lets the archive say which entry is the
    # live one without comparing floats for equality.
    flyer_id = f"{int(uploaded_at * 1000)}-{uuid.uuid4().hex[:8]}"
    state = {
        "id": flyer_id,
        "url": f"/uploads/{filename}",
        "caption": caption,
        "week_of": week_of or None,
        "service_window": (service_window or "").strip() or None,
        "uploaded_at": uploaded_at,
    }
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning("Could not persist weekly flyer state: %s", e)

    # The archived copy needs its own filename: the current flyer deliberately
    # reuses one so no cached reference can point at a deleted photo, but that
    # is exactly why it cannot double as the archive — the next upload would
    # overwrite the very history this keeps.
    try:
        os.makedirs(archive_dir(), exist_ok=True)
        archived_name = f"flyer-{flyer_id}.{ext}"
        with open(os.path.join(archive_dir(), archived_name), "wb") as f:
            f.write(image_bytes)
        entries = [e for e in _read_archive() if e.get("id") != flyer_id]
        entries.append({**state, "url": f"/uploads/archive/{archived_name}"})
        _write_archive(entries)
        prune_archive(now=uploaded_at)
    except Exception as e:
        # An archive failure must never cost the operator this week's upload.
        log.warning("Could not archive weekly flyer: %s", e)
    return state


# How long an undated flyer is treated as current. The printed date is the
# authority whenever it can be read; this is the fallback for a flyer whose
# date the vision model could not make out, which since the upload/OCR split
# reaches customers rather than being discarded. One week matches the kitchen's
# own cycle: long enough that this week's upload stays live all week, short
# enough that a forgotten one stops being shown.
UNDATED_FLYER_LIFETIME_DAYS = 7


def is_current(state: Optional[Dict[str, Any]], now: Optional[float] = None) -> bool:
    """
    Whether an uploaded flyer should still be sent to customers.

    A readable printed date decides it outright. Without one, the upload time
    stands in — the operator posted it when it was current, so it is current
    for a week from then. Judged on the flyer's own date rather than
    menu.json's, because the two now move independently: a failed text
    extraction leaves menu.json on last week while the photo is this week's,
    and letting the stale one veto the fresh one would put the operator back
    where an upload appeared to do nothing at all.
    """
    if not state:
        return False
    week_of = state.get("week_of")
    if menu_dates.parse_week_end_date(week_of) is not None:
        return not menu_dates.is_expired(week_of)
    now = time.time() if now is None else now
    uploaded_at = float(state.get("uploaded_at") or 0)
    return (now - uploaded_at) <= UNDATED_FLYER_LIFETIME_DAYS * 86400


def get() -> Optional[Dict[str, Any]]:
    """
    The uploaded flyer's URL and caption, or None if nothing was ever uploaded
    or the file behind a saved record has since gone missing.
    """
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            state = json.load(f)
        filename = os.path.basename(state.get("url", ""))
        if filename and os.path.exists(os.path.join(uploads_dir(), filename)):
            return state
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


def clear() -> None:
    """
    Removes the uploaded flyer and its state, reverting to the two baked-in
    weekly images. Called from Reset to Baseline, so that action fully means
    what it says rather than resetting dish text while leaving a stale photo.
    """
    try:
        if os.path.exists(_state_path()):
            os.remove(_state_path())
    except OSError as e:
        log.warning("Could not clear weekly flyer state: %s", e)

    # Reset to Baseline means baseline: leaving a browsable archive of past
    # menus behind would be the same stale-photo problem one level down.
    for entry in _read_archive():
        filename = os.path.basename(entry.get("url", ""))
        if filename:
            try:
                os.remove(os.path.join(archive_dir(), filename))
            except OSError:
                pass
    try:
        if os.path.exists(_archive_state_path()):
            os.remove(_archive_state_path())
    except OSError as e:
        log.warning("Could not clear flyer archive: %s", e)
