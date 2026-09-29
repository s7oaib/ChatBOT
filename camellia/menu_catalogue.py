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
import json
import re
from typing import Any, Dict, List, Optional

from camellia.database import db

class MenuCatalogueError(ValueError):
    """A create/update request failed validation."""


class MenuCatalogueInUseError(MenuCatalogueError):
    """A menu still has active customer-facing routing references."""

    def __init__(self, menu_id: str, references: List[Dict[str, Any]]):
        self.menu_id = menu_id
        self.references = references
        labels = []
        for reference in references:
            if reference["type"] == "instagram_route":
                labels.append(
                    f"Instagram {reference.get('source_type') or 'content'} route #{reference['routing_id']}"
                )
            else:
                labels.append(f"Instagram Topic '{reference.get('name') or reference['topic_id']}'")
        super().__init__(
            f"Cannot permanently delete '{menu_id}' because it is used by: "
            + ", ".join(labels)
            + ". Remove or disable those active references first."
        )


_MANAGER_KEYS = {
    "aamar_mishti": "collections.aamar_mishti",
    "signature": "collections.signature_collection",
    "weekly_vegetarian": "weekly.vegetarian",
    "weekly_non_vegetarian": "weekly.non_vegetarian",
    "bento": "weekly.bengali_bento",
}

# Customer-message routing metadata is deliberately separate from the Admin
# category and from Meta's source-specific Instagram Topics.
CUSTOMER_TOPICS = (
    "SONDESH", "SAVOURY", "BENTO_MEAL", "GIFT_EVENT",
    "WEEKLY_SAVOURY", "SUBSCRIPTION", "HOME_TABLE",
)


def clean_customer_topic(value: Optional[str]) -> Optional[str]:
    topic = str(value or "").strip().upper()
    if topic and topic not in CUSTOMER_TOPICS:
        raise MenuCatalogueError("Choose an approved customer message topic.")
    return topic or None


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "menu"


def clean_category_name(name: str) -> str:
    """Normalize an operator label without accepting control characters."""
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        raise MenuCatalogueError("Category name is required.")
    if len(cleaned) > 100:
        raise MenuCatalogueError("Category name must be 100 characters or fewer.")
    if any(ord(char) < 32 for char in cleaned):
        raise MenuCatalogueError("Category name contains invalid characters.")
    return cleaned


def list_categories(include_inactive: bool = False) -> List[Dict[str, Any]]:
    return db.list_menu_categories(include_inactive=include_inactive)


def create_category(name: str) -> Dict[str, Any]:
    try:
        return db.create_menu_category(clean_category_name(name))
    except ValueError as exc:
        raise MenuCatalogueError(str(exc)) from exc


def update_category(category_id: int, name: str) -> Dict[str, Any]:
    try:
        category = db.rename_menu_category(category_id, clean_category_name(name))
    except ValueError as exc:
        raise MenuCatalogueError(str(exc)) from exc
    if not category:
        raise KeyError(category_id)
    return category


def category_usage(category_id: int) -> Dict[str, Any]:
    usage = db.menu_category_usage(category_id)
    if not usage:
        raise KeyError(category_id)
    return usage


def delete_category(category_id: int, replacement_category_id: Optional[int] = None) -> Dict[str, Any]:
    try:
        return db.delete_menu_category(category_id, replacement_category_id)
    except ValueError as exc:
        raise MenuCatalogueError(str(exc)) from exc


def list_menus(include_inactive: bool = True) -> List[Dict[str, Any]]:
    return db.list_menu_catalogue(include_inactive=include_inactive)


def get_menu(menu_id: str) -> Optional[Dict[str, Any]]:
    return db.get_menu_catalogue_entry(menu_id)


def workspace_summary(menus: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Admin-only, zero-write projection. Never used to select customer facts.

    Keep the routing registry unchanged: Weekly Savoury is a workspace over
    the existing timeline, not a catalogue record to insert or reseed.
    """
    from camellia import menu_dates, weekly_savoury
    from camellia.config import config

    try:
        with open(config.MENU_JSON_PATH, encoding="utf-8") as stream:
            baseline = json.load(stream)
    except (OSError, ValueError):
        baseline = {}
    review_labels = {"EXTRACTING": "Extracting", "NEEDS_REVIEW": "Needs review",
                     "READY_TO_APPROVE": "Ready to approve", "FAILED": "Needs review"}
    with db.get_connection() as conn:
        versions = [dict(row) for row in conn.execute(
            "SELECT * FROM menu_versions WHERE approval_json IS NOT NULL ORDER BY id DESC")]
        drafts = {row["menu_key"]: json.loads(row["review_json"]) for row in conn.execute(
            "SELECT menu_key,review_json FROM menu_drafts WHERE review_json IS NOT NULL")}
        current_weekly = weekly_savoury.current_version(conn=conn)
        reference_counts = {}
        for row in conn.execute("""SELECT menu_id, COUNT(*) AS n FROM instagram_source_routes
                WHERE active=1 AND archived_at IS NULL GROUP BY menu_id
                UNION ALL SELECT default_menu_id, COUNT(*) AS n FROM instagram_topics
                WHERE active=1 GROUP BY default_menu_id"""):
            reference_counts[row[0]] = reference_counts.get(row[0], 0) + row[1]

    def project(menu, related, selected, baseline_standing=False):
        key = menu["menu_id"]
        draft = drafts.get(key) or {}
        status = draft.get("status")
        # Unlike customer relevance, operator action also includes inactive menus.
        unresolved = bool(status and status not in {"PUBLISHED", "REJECTED"})
        approval = json.loads(selected["approval_json"]) if selected else {}
        latest = related[0] if related else None
        latest_approval = json.loads(latest["approval_json"]) if latest else {}
        start = selected.get("valid_from") if selected else None
        end = selected.get("valid_until") if selected else None
        lifecycle = weekly_savoury.version_status(selected) if start and end else None
        published = bool(selected or baseline_standing)
        validity = weekly_savoury.display_range(start, end) if start and end else (
            "Standing" if published else "Not published")
        reference_count = reference_counts.get(key, 0)
        return {**menu, "workspace": "menu", "catalogue_state": "ACTIVE" if menu["active"] else "INACTIVE",
                "lifecycle_status": lifecycle, "valid_from": start, "valid_until": end,
                "display_status": "INACTIVE" if not menu["active"] else (lifecycle or (
                    "ACTIVE" if published else "Not published")), "validity_label": validity,
                "pending_review_status": status if unresolved else None,
                "review_required": unresolved, "review_label": review_labels.get(status, "Needs review") if unresolved else "—",
                "current_version": selected["id"] if selected else None,
                "last_approved_version": latest["id"] if latest else None,
                "last_approved_date": latest_approval.get("approved_at") or (latest.get("changed_at") if latest else None),
                "source_type": approval.get("source_type") or ("existing" if baseline_standing else None),
                "reference_count": reference_count,
                "actions": {"edit": True, "toggle_active": True, "delete": not reference_count}}

    rows = []
    for menu in menus:
        key = menu["menu_id"]
        if key in weekly_savoury.LEGACY_WEEKLY_KEYS:
            continue
        related = [version for version in versions if version["menu_key"] == key]
        selected = next((version for version in related if version["is_current"]), None)
        standing = bool(menu["is_builtin"] and (
            (key == "aamar_mishti" and (baseline.get("collections") or {}).get("aamar_mishti")) or
            (key == "signature" and (baseline.get("collections") or {}).get("signature_collection")) or
            (key == "bento" and (baseline.get("order_types") or {}).get("bengali_bento"))))
        rows.append(project(menu, related, selected, standing))

    related = [version for version in versions if version["menu_key"] in weekly_savoury.LEGACY_WEEKLY_KEYS]
    upcoming = sorted((version for version in related if version.get("valid_from") and
                       weekly_savoury.version_status(version) == "UPCOMING"), key=lambda version: version["valid_from"])
    selected = current_weekly or (upcoming[0] if upcoming else (related[0] if related else None))
    weekly = project({"menu_id": weekly_savoury.WEEKLY_KEY, "name": "Weekly Savoury",
                      "category": "Weekly Savoury", "description": "Review this week's menu or prepare the next one.",
                      "active": True, "is_builtin": True, "image_url": None, "image_status": None}, related, selected)
    # The older non-vegetarian review slot is the same operator workspace.
    pending = [drafts[key] for key in weekly_savoury.LEGACY_WEEKLY_KEYS if key in drafts and
               drafts[key].get("status") not in {None, "PUBLISHED", "REJECTED"}]
    if pending:
        status = next((draft["status"] for draft in pending if draft["status"] != "READY_TO_APPROVE"), pending[0]["status"])
        weekly.update(pending_review_status=status, review_required=True,
                      review_label=review_labels.get(status, "Needs review"))
    if not current_weekly and weekly_savoury.legacy_baseline_current():
        week = baseline["weekly_menus"][-1]
        start, end = menu_dates.parse_week_date_range(week.get("week_of"))
        weekly.update(lifecycle_status="CURRENT", display_status="CURRENT", valid_from=start.isoformat(),
                      valid_until=end.isoformat(), validity_label=weekly_savoury.display_range(start, end),
                      current_version=None, source_type="existing")
    if current_weekly:
        weekly["image_url"] = json.loads(current_weekly["approval_json"]).get("image_path")
        weekly["lifecycle_status"] = weekly["display_status"] = "CURRENT"
    elif weekly["lifecycle_status"] != "CURRENT":
        weekly["image_url"] = None
        weekly["current_version"] = None
    weekly.update(menu_id="__weekly_savoury__", workspace="weekly", catalogue_state=None,
                  actions={"edit": False, "toggle_active": False, "delete": False})
    rows.insert(0, weekly)
    return {"workspace_rows": rows, "review_count": sum(row["review_required"] for row in rows)}


def create_menu(name: str, category: str, description: str, active: bool = True,
                customer_topic: Optional[str] = None) -> Dict[str, Any]:
    name = (name or "").strip()
    description = (description or "").strip()
    if not name:
        raise MenuCatalogueError("Menu name is required.")
    if not description:
        raise MenuCatalogueError("Customer description is required.")
    selected_category = db.find_menu_category_by_name((category or "").strip())
    category = selected_category["name"] if selected_category and selected_category["active"] else "Other"

    if db.find_menu_catalogue_entry_by_name(name):
        raise MenuCatalogueError(f"A menu named '{name}' already exists.")

    menu_id = _slugify(name)
    if db.get_menu_catalogue_entry(menu_id) or db.is_menu_deleted(menu_id):
        # A different name that slugifies to the same id (e.g. punctuation-only
        # difference) — disambiguate rather than silently colliding into the
        # existing menu's image history.
        suffix = 2
        while db.get_menu_catalogue_entry(f"{menu_id}_{suffix}"):
            suffix += 1
        menu_id = f"{menu_id}_{suffix}"

    return db.create_menu_catalogue_entry(
        menu_id, name, category, description, active=active, is_builtin=False,
        customer_topic=clean_customer_topic(customer_topic),
    )


def permanently_delete_menu(menu_id: str, deleted_by: str = "admin") -> Dict[str, Any]:
    """Irreversibly remove a catalogue menu and its local assets/history."""
    # The Menu Manager gallery uses its legacy dotted keys while the Menu
    # Catalogue uses durable catalogue ids. Accept both at this boundary so
    # the destructive action cannot silently target the wrong record.
    menu_id = next(
        (catalogue_id for catalogue_id, manager_key in _MANAGER_KEYS.items() if manager_key == menu_id),
        menu_id,
    )
    if not db.get_menu_catalogue_entry(menu_id):
        raise KeyError(menu_id)
    related_key = _MANAGER_KEYS.get(menu_id)
    result = db.permanently_delete_menu(
        menu_id,
        related_keys=[related_key] if related_key else None,
        deleted_by=deleted_by,
    )
    if not result or not result.get("deleted"):
        raise MenuCatalogueInUseError(menu_id, (result or {}).get("references", []))

    # SQLite owns the metadata; the image module owns the durable upload path.
    from camellia import menu_images
    result["image_files_removed"] = menu_images.remove_persisted_files(result.pop("image_paths", []))
    return result


def update_menu(
    menu_id: str, *, name: Optional[str] = None, category: Optional[str] = None,
    description: Optional[str] = None, active: Optional[bool] = None,
    customer_topic: Optional[str] = None, update_topic: bool = False,
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
    if description is not None:
        description = description.strip()
        if not description:
            raise MenuCatalogueError("Customer description is required.")
    if category is not None:
        selected_category = db.find_menu_category_by_name(category.strip())
        category = selected_category["name"] if selected_category and selected_category["active"] else "Other"
    updated = db.update_menu_catalogue_entry(
        menu_id, name=name, category=category, description=description, active=active,
        customer_topic=clean_customer_topic(customer_topic) if update_topic else None,
        update_topic=update_topic,
    )
    return updated


def move_menu_in_topic(menu_id: str, position: int) -> List[Dict[str, Any]]:
    try:
        result = db.move_menu_in_topic(menu_id, position)
    except ValueError as exc:
        raise MenuCatalogueError(str(exc)) from exc
    if result is None:
        raise KeyError(menu_id)
    return result


def eligible_topic_menus(topic: str) -> List[Dict[str, Any]]:
    """Images only, after the existing review/current/pending authority gates.

    No menu description or extracted facts are returned to the responder.
    Unlinked/inactive menus and drafts are never candidates.
    """
    from camellia import menu_images, menu_review

    selected = []
    approved = {version["menu_key"] for version in menu_review.customer_versions()}
    blocked = menu_review.customer_blocked_keys()
    for menu in db.list_menu_catalogue(include_inactive=False):
        if menu.get("customer_topic") != topic or menu["menu_id"] in blocked:
            continue
        # The two known standing collection assets have canonical approval in
        # the baseline. All operator-added menus require a reviewed version.
        if menu["menu_id"] not in approved and menu["menu_id"] not in {"aamar_mishti", "signature"}:
            continue
        image_url = menu_images.current_url(menu["menu_id"], None)
        if image_url:
            selected.append({"menu_id": menu["menu_id"], "name": menu["name"],
                             "image_url": image_url, "display_order": menu["display_order"]})
    return sorted(selected, key=lambda item: (item["display_order"], item["menu_id"]))
