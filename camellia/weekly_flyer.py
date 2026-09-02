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
from typing import Any, Dict, Optional

from camellia.config import config
from camellia.monitoring import get_logger

log = get_logger("camellia.weekly_flyer")

# Deliberately generic — no dish names or prices. The vision model's extraction
# has not been reviewed by a human at the point this caption is sent, and a
# wrong price reaching a customer is worse than no price. The accurate weekly
# menu is already sent as text elsewhere (weekly_menu_veg / weekly_menu_nonveg),
# built straight from menu.json's structured items rather than copied here.
# Rotated so a customer asking twice in one session does not see identical text.
CAPTIONS = (
    "*This Week's Menu* — freshly cooked in small batches, updated just for you! 🌸",
    "*This Week's Menu* — hot off the kitchen, ready when you are! 🌸",
    "*This Week's Menu* — fresh ingredients, cooked daily, just for this week. 🌸",
    "*This Week's Menu* — a little taste of home, made fresh this week! 🌸",
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


def save(image_bytes: bytes, mime_type: str) -> Dict[str, Any]:
    """
    Persists an uploaded photo as the current weekly menu card.

    Overwrites whatever was there before: there is exactly one current weekly
    menu, and keeping old uploads around risks an outdated one being served by
    a stale reference. The filename does not change between uploads, so no
    caching layer can be left pointing at a photo that no longer exists.
    """
    ext = _EXT_BY_MIME.get((mime_type or "").lower(), "jpeg")
    directory = uploads_dir()
    os.makedirs(directory, exist_ok=True)
    filename = f"weekly_flyer.{ext}"

    with open(os.path.join(directory, filename), "wb") as f:
        f.write(image_bytes)

    state = {
        "url": f"/uploads/{filename}",
        "caption": random.choice(CAPTIONS),
        "uploaded_at": time.time(),
    }
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning("Could not persist weekly flyer state: %s", e)
    return state


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
