"""Persistent customer-facing menu image versions.

Images are files on the same durable volume as ``menu.json``; SQLite stores
only the small amount of metadata needed to make one version current.
"""
import os
import time
import uuid
from io import BytesIO
from typing import Any, Dict, Optional

from PIL import Image

from camellia.config import config
from camellia.database import db
from camellia import weekly_flyer

MENU_IMAGE_DEFINITIONS = {
    "aamar_mishti": {"title": "Aamar Mishti Collection", "default": "/images/AAMAR%20MISHTI.jpeg"},
    "signature": {"title": "Signature Collection", "default": "/images/Signature%20Sondesh.jpeg"},
    "weekly_vegetarian": {"title": "Weekly Vegetarian", "default": None},
    "weekly_non_vegetarian": {"title": "Weekly Non-Vegetarian", "default": None},
    "bento": {"title": "Bento / Meal Menu", "default": None},
}
CANONICAL_ALIASES = {
    "collections.aamar_mishti": "aamar_mishti",
    "collections.signature_collection": "signature",
    "weekly.vegetarian": "weekly_vegetarian",
    "weekly.non_vegetarian": "weekly_non_vegetarian",
    "weekly.bengali_bento": "bento",
}
ALLOWED_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def _dir() -> str:
    path = os.path.join(weekly_flyer.uploads_dir(), "menu-images")
    os.makedirs(path, exist_ok=True)
    return path


def _key(menu_key: str) -> str:
    return CANONICAL_ALIASES.get(menu_key, menu_key)


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


def _fallback(menu_key: str) -> Optional[str]:
    definition = MENU_IMAGE_DEFINITIONS[menu_key]
    if definition["default"]:
        return definition["default"]
    flyer = weekly_flyer.get()
    return flyer.get("url") if flyer else "/images/From%20Friday.jpeg"


def list_menus() -> list[Dict[str, Any]]:
    result = []
    for key, definition in MENU_IMAGE_DEFINITIONS.items():
        current = db.get_menu_image_current(key)
        draft = db.get_menu_image_draft(key)
        archived = db.has_menu_image_archive(key)
        result.append({
            "menu_key": key,
            "title": definition["title"],
            "image_url": (current or {}).get("image_path") or (None if archived else _fallback(key)),
            "status": "Archived" if current is None and archived else ("Active" if current else "Active (built-in)"),
            "version": (current or {}).get("version"),
            "published_at": (current or {}).get("published_at"),
            "draft": draft,
        })
    return result


def card(menu_key: str) -> Dict[str, Any]:
    key = _key(menu_key)
    if key not in MENU_IMAGE_DEFINITIONS:
        raise KeyError(menu_key)
    current = db.get_menu_image_current(key)
    draft = db.get_menu_image_draft(key)
    archived = db.has_menu_image_archive(key)
    image_url = (current or {}).get("image_path") or (None if archived else _fallback(key))
    return {"menu_key": menu_key, "image_url": image_url, "image_status": "Archived" if current is None and archived else ("Published" if current else "Built-in"), "image_version": (current or {}).get("version"), "published_at": (current or {}).get("published_at"), "image_draft": draft}


def save_draft(menu_key: str, data: bytes, content_type: str, uploaded_by: str = "admin") -> Dict[str, Any]:
    menu_key = _key(menu_key)
    if menu_key not in MENU_IMAGE_DEFINITIONS:
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
    if menu_key not in MENU_IMAGE_DEFINITIONS:
        raise KeyError(menu_key)
    current = db.get_menu_image_current(menu_key)
    return {"menu_key": menu_key, "current": (current or {}).get("image_path") or (None if current is not None else _fallback(menu_key)), "draft": db.get_menu_image_draft(menu_key)}


def publish(menu_key: str) -> Dict[str, Any]:
    menu_key = _key(menu_key)
    row = db.publish_menu_image(menu_key)
    if not row:
        raise ValueError("Save a draft image before publishing.")
    return row


def archive(menu_key: str) -> Optional[Dict[str, Any]]:
    menu_key = _key(menu_key)
    row = db.archive_menu_image(menu_key)
    if row is not None:
        return row
    # A built-in image has no database row yet.  Record a tombstone so an
    # archive action really removes it from customer view and remains
    # restorable after a restart.
    if menu_key not in MENU_IMAGE_DEFINITIONS:
        return None
    return db.create_menu_image_archive(menu_key, MENU_IMAGE_DEFINITIONS[menu_key]["default"] or _fallback(menu_key), "admin")


def history(menu_key: str) -> list[Dict[str, Any]]:
    menu_key = _key(menu_key)
    return db.list_menu_image_history(menu_key)


def restore(menu_key: str, version_id: int) -> Optional[Dict[str, Any]]:
    menu_key = _key(menu_key)
    return db.restore_menu_image(menu_key, version_id)


def current_url(menu_key: str, fallback: Optional[str] = None) -> Optional[str]:
    menu_key = _key(menu_key)
    row = db.get_menu_image_current(menu_key)
    if row:
        return row.get("image_path")
    if db.has_menu_image_archive(menu_key):
        return None
    return fallback if fallback is not None else _fallback(menu_key)
