"""
The registry of every menu the Visual Menu Manager and Instagram routing
know about.

This is deliberately just a registry — name, category, customer-facing
description, active flag. The durable image/version machinery
(``camellia/menu_images.py``, backed by ``menu_image_versions``) already
works against any ``menu_id`` string; a new menu needs nothing more than a
row here to become real everywhere it is referenced, with no second upload
system and no hardcoded list in JavaScript.

Adding a menu here authorizes exactly one thing: that a name, category and
description may be shown to a customer alongside an image. It does not
authorize a price, ingredient, allergen, availability, or delivery claim —
those remain in canonical knowledge, entered through the existing approval
workflow, never inferred from a menu's name or its image.
"""
import re
from typing import Any, Dict, List, Optional

from camellia.database import db

CATEGORIES = [
    "Sondesh", "Weekly Vegetarian", "Weekly Non-Vegetarian",
    "Bento / Meal", "Subscription", "Event", "Other",
]


class MenuCatalogueError(ValueError):
    """A create/update request failed validation."""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "menu"


def list_menus(include_inactive: bool = True) -> List[Dict[str, Any]]:
    return db.list_menu_catalogue(include_inactive=include_inactive)


def get_menu(menu_id: str) -> Optional[Dict[str, Any]]:
    return db.get_menu_catalogue_entry(menu_id)


def create_menu(name: str, category: str, description: str, active: bool = True) -> Dict[str, Any]:
    name = (name or "").strip()
    description = (description or "").strip()
    if not name:
        raise MenuCatalogueError("Menu name is required.")
    if not description:
        raise MenuCatalogueError("Customer description is required.")
    category = category.strip() if (category or "").strip() in CATEGORIES else "Other"

    if db.find_menu_catalogue_entry_by_name(name):
        raise MenuCatalogueError(f"A menu named '{name}' already exists.")

    menu_id = _slugify(name)
    if db.get_menu_catalogue_entry(menu_id):
        # A different name that slugifies to the same id (e.g. punctuation-only
        # difference) — disambiguate rather than silently colliding into the
        # existing menu's image history.
        suffix = 2
        while db.get_menu_catalogue_entry(f"{menu_id}_{suffix}"):
            suffix += 1
        menu_id = f"{menu_id}_{suffix}"

    return db.create_menu_catalogue_entry(menu_id, name, category, description, active=active, is_builtin=False)


def update_menu(
    menu_id: str, *, name: Optional[str] = None, category: Optional[str] = None,
    description: Optional[str] = None, active: Optional[bool] = None,
) -> Dict[str, Any]:
    if not db.get_menu_catalogue_entry(menu_id):
        raise KeyError(menu_id)
    if name is not None:
        name = name.strip()
        if not name:
            raise MenuCatalogueError("Menu name is required.")
        existing = db.find_menu_catalogue_entry_by_name(name)
        if existing and existing["menu_id"] != menu_id:
            raise MenuCatalogueError(f"A menu named '{name}' already exists.")
    if category is not None and category not in CATEGORIES:
        category = "Other"
    updated = db.update_menu_catalogue_entry(
        menu_id, name=name, category=category, description=description, active=active,
    )
    return updated
