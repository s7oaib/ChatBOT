"""Persistent customer-facing menu image versions.

Images are files on the same durable volume as ``menu.json``; SQLite stores
only the small amount of metadata needed to make one version current.
"""
import os
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, Optional

from PIL import Image, ImageOps

from camellia.database import db
from camellia import weekly_flyer

# Two of the five original menus shipped with a real static image baked into
# the repo before this catalogue existed; kept here as pure fallback image
# paths, not menu facts — the name/category/description for every menu,
# built-in or not, now lives in menu_catalogue exclusively.
_BUILTIN_DEFAULT_IMAGES = {
    "aamar_mishti": "/images/AAMAR%20MISHTI.jpeg",
    "signature": "/images/Signature%20Sondesh.jpeg",
}
CANONICAL_ALIASES = {
    "collections.aamar_mishti": "aamar_mishti",
    "collections.signature_collection": "signature",
    "weekly.vegetarian": "weekly_vegetarian",
    "weekly.non_vegetarian": "weekly_non_vegetarian",
    "weekly.bengali_bento": "bento",
}
# A weekly menu card is one customer-facing asset even though the catalogue
# keeps vegetarian and non-vegetarian records separately.  Resolve the live
# card from their durable lifecycle rows rather than from a filename, list
# position, or the day-of-week built-in rotation.
WEEKLY_MENU_KEYS = ("weekly_vegetarian", "weekly_non_vegetarian")
ALLOWED_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
PREVIEW_MAX_EDGE = 640
PREVIEW_MAX_BYTES = 2 * 1024 * 1024


def _dir() -> str:
    path = os.path.join(weekly_flyer.uploads_dir(), "menu-images")
    os.makedirs(path, exist_ok=True)
    return path


def _key(menu_key: str) -> str:
    return CANONICAL_ALIASES.get(menu_key, menu_key)


def _known(menu_key: str) -> bool:
    """Whether menu_key is a real, registered menu — dynamic, never hardcoded."""
    return db.get_menu_catalogue_entry(_key(menu_key)) is not None


def remove_persisted_files(image_paths: list[str]) -> int:
    """Delete only files from this feature's private upload directory."""
    removed = 0
    root = os.path.realpath(_dir())
    for image_path in image_paths:
        raw = str(image_path or "").split("?", 1)[0]
        if not raw.startswith("/uploads/menu-images/"):
            continue
        candidate = os.path.realpath(os.path.join(root, os.path.basename(raw)))
        if not candidate.startswith(root + os.sep):
            continue
        try:
            os.remove(candidate)
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def validate_image(data: bytes, content_type: str) -> str:
    if not data:
        raise ValueError("Choose an image to upload.")
    mime = (content_type or "").split(";", 1)[0].lower()
    if mime not in ALLOWED_TYPES:
        raise ValueError("Only PNG, JPG/JPEG, and WEBP images are supported.")
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
    except Exception as exc:
        raise ValueError("The uploaded file is not a valid image.") from exc
    return ALLOWED_TYPES[mime]


def prepare_admin_preview(data: bytes, content_type: str) -> tuple[bytes, str, str]:
    """Validate and compact an admin-only preview without changing route data.

    Normal-sized images keep their original format. Larger or very high
    resolution uploads are reduced to a visual thumbnail so the durable
    preview store never keeps an unnecessarily large original.
    """
    extension = validate_image(data, content_type)
    mime = (content_type or "").split(";", 1)[0].lower()
    with Image.open(BytesIO(data)) as opened:
        image = ImageOps.exif_transpose(opened)
        if max(image.size) <= PREVIEW_MAX_EDGE and len(data) <= PREVIEW_MAX_BYTES:
            return data, mime, extension
        image.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.LANCZOS)
        if image.mode not in ("RGB", "L"):
            background = Image.new("RGB", image.size, "white")
            alpha = image.getchannel("A") if "A" in image.getbands() else None
            background.paste(image, mask=alpha)
            image = background
        elif image.mode == "L":
            image = image.convert("RGB")
        output = BytesIO()
        for quality in (88, 78, 68, 58):
            output.seek(0)
            output.truncate(0)
            image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
            if output.tell() <= PREVIEW_MAX_BYTES:
                break
        return output.getvalue(), "image/jpeg", "jpg"


def _fallback(menu_key: str) -> Optional[str]:
    static_default = _BUILTIN_DEFAULT_IMAGES.get(menu_key)
    if static_default:
        return static_default
    if menu_key in WEEKLY_MENU_KEYS:
        flyer = weekly_flyer.get()
        return flyer.get("url") if flyer else "/images/From%20Friday.jpeg"
    return None


def _published_sort_key(row: Dict[str, Any]) -> tuple:
    """Order current rows by authoritative publication time, then row id.

    SQLite timestamps written by this module are UTC-naive ISO strings.  The
    parser also accepts an explicit offset so old or hand-migrated rows do not
    fall back to lexical ordering accidentally.
    """
    raw = str(row.get("published_at") or "").strip()
    parsed = None
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
        except ValueError:
            parsed = None
    return (
        parsed or datetime.min.replace(tzinfo=timezone.utc),
        int(row.get("id") or 0),
        int(row.get("version") or 0),
    )


def current_weekly() -> Optional[Dict[str, Any]]:
    """Return the newest currently published weekly menu image row.

    Draft and archived rows are excluded by ``get_menu_image_current``.  A
    fresh database read on every call keeps publish/restore visible to the
    responder immediately and after a process restart.
    """
    from camellia import weekly_savoury
    if weekly_savoury.lifecycle_enabled():
        version = weekly_savoury.current_version()
        if not version:
            return None
        import json
        approval = json.loads(version["approval_json"])
        return {"id": version["id"], "image_path": approval.get("image_path"),
                "published_at": version["changed_at"], "version": version["id"],
                "menu_key": version["menu_key"]}
    rows = [
        row
        for key in WEEKLY_MENU_KEYS
        if _known(key)
        if (row := db.get_menu_image_current(key)) is not None
    ]
    return max(rows, key=_published_sort_key) if rows else None


def has_weekly_archive() -> bool:
    """Whether any managed weekly image has history but no current row."""
    from camellia import weekly_savoury
    if weekly_savoury.lifecycle_enabled():
        # An early future schedule must not hide a still-valid printed legacy
        # week. Once that week expires, never reuse its flyer as current.
        return not weekly_savoury.legacy_baseline_current()
    return any(_known(key) and db.has_menu_image_archive(key) for key in WEEKLY_MENU_KEYS)


def current_weekly_url(fallback: Optional[str] = None) -> Optional[str]:
    """Resolve the customer-facing weekly image from the shared lifecycle."""
    row = current_weekly()
    if row:
        return row.get("image_path")
    if has_weekly_archive():
        return None
    return fallback


def list_menus() -> list[Dict[str, Any]]:
    result = []
    for entry in db.list_menu_catalogue():
        key = entry["menu_id"]
        current = db.get_menu_image_current(key)
        draft = db.get_menu_image_draft(key)
        archived = db.has_menu_image_archive(key)
        result.append({
            "menu_key": key,
            "title": entry["name"],
            "category": entry["category"],
            "description": entry["description"],
            "active": bool(entry["active"]),
            "image_url": (current or {}).get("image_path") or (None if archived else _fallback(key)),
            "status": "Archived" if current is None and archived else ("Active" if current else "Active (built-in)"),
            "draft_status": "Draft ready" if draft else None,
            "version": (current or {}).get("version"),
            "published_at": (current or {}).get("published_at"),
            "draft": draft,
        })
    return result


def card(menu_key: str) -> Dict[str, Any]:
    key = _key(menu_key)
    if not _known(key):
        raise KeyError(menu_key)
    current = db.get_menu_image_current(key)
    draft = db.get_menu_image_draft(key)
    archived = db.has_menu_image_archive(key)
    image_url = (current or {}).get("image_path") or (None if archived else _fallback(key))
    status = "Archived" if current is None and archived else ("Published" if current else "Built-in")
    return {
        "menu_key": menu_key, "image_url": image_url, "image_status": status,
        "draft_status": "Draft ready" if draft else None,
        "image_version": (current or {}).get("version"),
        "published_at": (current or {}).get("published_at"), "image_draft": draft,
    }


def save_draft(menu_key: str, data: bytes, content_type: str, uploaded_by: str = "admin") -> Dict[str, Any]:
    menu_key = _key(menu_key)
    if not _known(menu_key):
        raise KeyError(menu_key)
    ext = validate_image(data, content_type)
    token = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    filename = f"{menu_key}-{token}.{ext}"
    path = os.path.join(_dir(), filename)
    with open(path, "wb") as handle:
        handle.write(data)
    row = db.save_menu_image_draft(menu_key, f"/uploads/menu-images/{filename}", uploaded_by)
    return row


def preview(menu_key: str) -> Dict[str, Any]:
    menu_key = _key(menu_key)
    if not _known(menu_key):
        raise KeyError(menu_key)
    current = db.get_menu_image_current(menu_key)
    current_url_value = (current or {}).get("image_path") or (None if current is not None else _fallback(menu_key))
    return {"menu_key": menu_key, "current": current_url_value, "draft": db.get_menu_image_draft(menu_key)}


def _requires_review(menu_key: str) -> bool:
    key = _key(menu_key)
    draft = db.get_menu_draft(key)
    with db.get_connection() as conn:
        current = conn.execute("SELECT 1 FROM menu_versions WHERE menu_key=? AND is_current=1", (key,)).fetchone()
    return bool(current or (draft and draft.get("review_json")))


def publish(menu_key: str) -> Dict[str, Any]:
    if _requires_review(menu_key):
        raise ValueError("Use Review Menu → Approve & Publish to publish facts and flyer together.")
    menu_key = _key(menu_key)
    if not _known(menu_key):
        raise KeyError(menu_key)
    row = db.publish_menu_image(menu_key)
    if not row:
        raise ValueError("Save a draft image before publishing.")
    return row


def archive(menu_key: str) -> Optional[Dict[str, Any]]:
    menu_key = _key(menu_key)
    from camellia import weekly_savoury
    if menu_key in weekly_savoury.LEGACY_WEEKLY_KEYS and weekly_savoury.lifecycle_enabled():
        raise ValueError("Restore the approved Weekly Savoury version to a draft instead of archiving its flyer.")
    row = db.archive_menu_image(menu_key)
    if row is not None:
        return row
    # A built-in image has no database row yet.  Record a tombstone so an
    # archive action really removes it from customer view and remains
    # restorable after a restart.
    if not _known(menu_key):
        return None
    return db.create_menu_image_archive(menu_key, _fallback(menu_key), "admin")


def history(menu_key: str) -> list[Dict[str, Any]]:
    menu_key = _key(menu_key)
    if not _known(menu_key):
        raise KeyError(menu_key)
    return db.list_menu_image_history(menu_key)


def restore(menu_key: str, version_id: int) -> Optional[Dict[str, Any]]:
    if _requires_review(menu_key):
        raise ValueError("Restore the approved menu version through Review Menu.")
    menu_key = _key(menu_key)
    if not _known(menu_key):
        raise KeyError(menu_key)
    return db.restore_menu_image(menu_key, version_id)


def current_url(menu_key: str, fallback: Optional[str] = None) -> Optional[str]:
    menu_key = _key(menu_key)
    from camellia import weekly_savoury
    if menu_key in weekly_savoury.LEGACY_WEEKLY_KEYS and weekly_savoury.lifecycle_enabled():
        current = current_weekly()
        if current:
            return current.get("image_path")
        if weekly_savoury.legacy_baseline_current():
            flyer = weekly_flyer.get()
            return flyer.get("url") if flyer and weekly_flyer.is_current(flyer) else fallback
        return None
    if not _known(menu_key):
        return None
    row = db.get_menu_image_current(menu_key)
    if row:
        return row.get("image_path")
    if db.has_menu_image_archive(menu_key):
        return None
    return fallback if fallback is not None else _fallback(menu_key)
