"""Menu Manager: the client's own admin control over what the chatbot sells.

Two kinds of menu, one write path each, no LLM in either:

* Collections (Aamar Mishti, Signature) — a flat list of flavour rows,
  stored in ``data/menu.json``'s ``collections`` block (previously unused by
  any responder code) and rendered deterministically into the exact
  qa_templates.json answer the chatbot already sends verbatim
  (``aamar_mishti_collection`` / ``signature_collection``). Publishing writes
  both files and calls ``responder.reload_data()`` — the same hot-reload
  already used by the weekly-menu and Q&A-template admin endpoints, so there
  is one source of truth (menu.json) and one reload path, not a second
  database that can drift from what customers see.

* Weekly menu (vegetarian / non_vegetarian / bengali_bento) — already lives
  in ``data/menu.json``'s ``weekly_menus[-1]`` and is already read live by
  ``responder.py`` at reply time; this module adds draft/publish/version
  history on top of the existing validate-and-reload endpoint instead of
  replacing it.

Every publish is deterministic Python string/JSON manipulation — never a
model call — so the admin can create or remove a flavour, but nothing here
can invent one on its own.
"""

import copy
import json
from typing import Any, Dict, List, Optional

from camellia.config import config
from camellia.database import db
from camellia.monitoring import get_logger

log = get_logger("camellia.menu_manager")

PACK_SIZES = ["250g", "500g", "1kg"]

# One row per manageable menu: where it lives in menu.json, and — for
# collections only — which qa_templates.json template's approved response
# is regenerated from it on publish.
MANAGEABLE_MENUS: Dict[str, Dict[str, Any]] = {
    "collections.aamar_mishti": {
        "label": "Aamar Mishti Collection",
        "kind": "collection",
        "collection_key": "aamar_mishti",
        "template_id": "aamar_mishti_collection",
    },
    "collections.signature_collection": {
        "label": "Signature Collection",
        "kind": "collection",
        "collection_key": "signature_collection",
        "template_id": "signature_collection",
    },
    "weekly.vegetarian": {
        "label": "Current Weekly Menu — Vegetarian",
        "kind": "weekly",
        "weekly_field": "vegetarian",
    },
    "weekly.non_vegetarian": {
        "label": "Current Weekly Menu — Non-Vegetarian",
        "kind": "weekly",
        "weekly_field": "non_vegetarian",
    },
    "weekly.bengali_bento": {
        "label": "Bento / Meal Menu",
        "kind": "weekly",
        "weekly_field": "bengali_bento",
    },
}

_CATALOGUE_KEYS = {
    "collections.aamar_mishti": "aamar_mishti",
    "collections.signature_collection": "signature",
    "weekly.vegetarian": "weekly_vegetarian",
    "weekly.non_vegetarian": "weekly_non_vegetarian",
    "weekly.bengali_bento": "bento",
}

# Seed data for the two collections: menu.json's own "collections" block
# predates this feature and was never actually read by the chatbot (the
# approved reply was a hand-written string in qa_templates.json), so the
# very first time each collection is opened here it is seeded from that
# already-approved copy rather than from the disconnected legacy shape —
# otherwise the client's first edit would silently discard real flavours.
_SEED_ITEMS: Dict[str, List[Dict[str, Any]]] = {
    "aamar_mishti": [
        {"flavour": "Classic", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 450, "500g": 800, "1kg": 1500}},
        {"flavour": "Rose", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 450, "500g": 800, "1kg": 1500}},
        {"flavour": "Mango", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 550, "500g": 1000, "1kg": 1800}},
        {"flavour": "Kesar", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 550, "500g": 1000, "1kg": 1800}},
        {"flavour": "Ada", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 550, "500g": 1000, "1kg": 1800}},
        {"flavour": "Nolen Gur", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 600, "500g": 1100, "1kg": 2000}},
        {"flavour": "Jolbhora", "active": True, "note": "Liquid", "pieces": {"250g": 6, "500g": 12, "1kg": 24},
         "prices": {"250g": 750, "500g": 1400, "1kg": 2500}},
        {"flavour": "Kanchagolla", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 450, "500g": 800, "1kg": 1500}},
    ],
    "signature_collection": [
        {"flavour": "Classic", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 600, "500g": 1100, "1kg": 2000}},
        {"flavour": "Rose", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 600, "500g": 1100, "1kg": 2000}},
        {"flavour": "Mango", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 650, "500g": 1200, "1kg": 2200}},
        {"flavour": "Real Vanilla", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 650, "500g": 1200, "1kg": 2200}},
        {"flavour": "Dalchini", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 650, "500g": 1200, "1kg": 2200}},
        {"flavour": "Kesar", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 650, "500g": 1200, "1kg": 2200}},
        {"flavour": "Nolen Gur", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 700, "500g": 1300, "1kg": 2500}},
        {"flavour": "Chocolate", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 700, "500g": 1300, "1kg": 2500}},
        {"flavour": "Mahua", "active": True, "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 900, "500g": 1600, "1kg": 2900}},
        {"flavour": "Kanchagolla", "active": True, "note": "Nolen Gur", "pieces": {"250g": 9, "500g": 18, "1kg": 36},
         "prices": {"250g": 700, "500g": 1300, "1kg": 2500}},
        {"flavour": "Jolbhora", "active": True, "note": "Nolen Gur, liquid", "pieces": {"250g": 6, "500g": 12, "1kg": 24},
         "prices": {"250g": 900, "500g": 1600, "1kg": 2900}},
    ],
}

_COLLECTION_INTRO = {
    "aamar_mishti": (
        "Collection 1 — Aamar Mishti (আমার মিষ্টি)\n"
        "Indulgence for your daily sweet cravings at home or work. Made using "
        "pure dairy milk in our kitchen.\n\n*Price Breakdown Table:*"
    ),
    "signature_collection": (
        "Collection 2. Signature collection — mishti for special occasions, "
        "gifting & when you are in the mood to give yourself a treat. Made "
        "using desi A2 cow milk that has not been industrially processed. "
        "The mishti is completely hand crafted end to end. Sharing the menu "
        "for signature collection below."
    ),
}

_COLLECTION_OUTRO = {
    "aamar_mishti": "\nWhich item and pack size would you like to order?",
    "signature_collection": (
        "\nPorter charges are on actuals.\n\nPlease feel free to ask anything "
        "before confirming — I'll be happy to help."
    ),
}


class MenuValidationError(ValueError):
    """A publish/draft body failed structural validation."""


def _menu_entry(menu_key: str) -> Dict[str, Any]:
    entry = MANAGEABLE_MENUS.get(menu_key)
    if not entry or not db.get_menu_catalogue_entry(_CATALOGUE_KEYS.get(menu_key, menu_key)):
        raise KeyError(menu_key)
    return entry


def list_menus() -> List[Dict[str, Any]]:
    """Registry + live item counts + whether a draft is waiting, for the tab's landing view."""
    out = []
    for menu_key, entry in MANAGEABLE_MENUS.items():
        if not db.get_menu_catalogue_entry(_CATALOGUE_KEYS.get(menu_key, menu_key)):
            continue
        items = get_published_items(menu_key)
        draft = db.get_menu_draft(menu_key)
        out.append({
            "menu_key": menu_key,
            "label": entry["label"],
            "kind": entry["kind"],
            "item_count": len(items),
            "active_item_count": sum(1 for i in items if i.get("active", True)) if entry["kind"] == "collection" else len(items),
            "has_draft": draft is not None,
        })
    return out


def _read_menu_json() -> Dict[str, Any]:
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_menu_json(data: Dict[str, Any]) -> None:
    with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_published_items(menu_key: str) -> List[Dict[str, Any]]:
    """The live, customer-facing item list for this menu right now."""
    entry = _menu_entry(menu_key)
    menu_data = _read_menu_json()
    if entry["kind"] == "collection":
        collection_key = entry["collection_key"]
        collection = (menu_data.get("collections") or {}).get(collection_key) or {}
        items = collection.get("products")
        # menu.json's pre-existing "products" (item + nested flavours) is the
        # legacy shape no responder code ever actually read — only a flat
        # list of {"flavour": ..., "prices": {...}} rows written by this
        # module counts as "already managed here". Anything else means this
        # collection has never been opened in Menu Manager yet, so it is
        # seeded from the copy customers are actually sent today.
        if not items or not all(isinstance(i, dict) and "flavour" in i for i in items):
            return copy.deepcopy(_SEED_ITEMS[collection_key])
        return items
    field = entry["weekly_field"]
    weekly = menu_data.get("weekly_menus") or []
    if not weekly:
        return []
    return weekly[-1].get(field) or []


def get_draft_items(menu_key: str) -> Optional[List[Dict[str, Any]]]:
    row = db.get_menu_draft(menu_key)
    if not row:
        return None
    return json.loads(row["draft_json"])


def get_menu(menu_key: str) -> Dict[str, Any]:
    _menu_entry(menu_key)  # raises KeyError for an unknown key
    return {
        "menu_key": menu_key,
        "label": MANAGEABLE_MENUS[menu_key]["label"],
        "kind": MANAGEABLE_MENUS[menu_key]["kind"],
        "published": get_published_items(menu_key),
        "draft": get_draft_items(menu_key),
    }


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def validate_collection_items(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise MenuValidationError("A collection must have at least one item.")
    cleaned: List[Dict[str, Any]] = []
    seen_flavours = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise MenuValidationError(f"Item {index + 1} is not a valid record.")
        flavour = _clean_str(entry.get("flavour"))
        if not flavour:
            raise MenuValidationError(f"Item {index + 1} has no flavour/product name.")
        key = flavour.lower()
        if key in seen_flavours:
            raise MenuValidationError(f"'{flavour}' appears more than once in this collection.")
        seen_flavours.add(key)

        prices_raw = entry.get("prices") or {}
        pieces_raw = entry.get("pieces") or {}
        if not isinstance(prices_raw, dict) or not prices_raw:
            raise MenuValidationError(f"'{flavour}' has no prices.")
        prices: Dict[str, int] = {}
        pieces: Dict[str, int] = {}
        for pack_size, price in prices_raw.items():
            pack_size = _clean_str(pack_size)
            if not pack_size:
                raise MenuValidationError(f"'{flavour}' has a price with no pack size.")
            try:
                price_val = int(float(price))
            except (TypeError, ValueError):
                raise MenuValidationError(f"'{flavour}' ({pack_size}) has an unreadable price.")
            if price_val <= 0:
                raise MenuValidationError(f"'{flavour}' ({pack_size}) is priced at {price_val}.")
            prices[pack_size] = price_val
            piece_val = pieces_raw.get(pack_size)
            if piece_val is not None:
                try:
                    piece_count = int(piece_val)
                except (TypeError, ValueError):
                    raise MenuValidationError(f"'{flavour}' ({pack_size}) has an unreadable piece count.")
                if piece_count <= 0:
                    raise MenuValidationError(f"'{flavour}' ({pack_size}) has an invalid piece count.")
                pieces[pack_size] = piece_count
        item: Dict[str, Any] = {
            "flavour": flavour,
            "active": bool(entry.get("active", True)),
            "prices": prices,
        }
        if pieces:
            item["pieces"] = pieces
        note = _clean_str(entry.get("note"))
        if note:
            item["note"] = note
        cleaned.append(item)
    if not any(i["active"] for i in cleaned):
        raise MenuValidationError("A collection must have at least one active item.")
    return cleaned


def validate_weekly_items(raw: Any, field: str) -> List[Dict[str, Any]]:
    """Same rule the existing /admin/update-menu endpoint already enforces —
    reused rather than re-implemented so the two write paths cannot drift."""
    from camellia.app import _reviewed_menu_items  # local import: avoids a
    # module import cycle (app imports menu_manager for its new endpoints).
    return _reviewed_menu_items(raw, field)


def validate_items(menu_key: str, raw: Any) -> List[Dict[str, Any]]:
    entry = _menu_entry(menu_key)
    if entry["kind"] == "collection":
        return validate_collection_items(raw)
    try:
        return validate_weekly_items(raw, entry["weekly_field"])
    except Exception as exc:  # HTTPException from _reviewed_menu_items
        raise MenuValidationError(getattr(exc, "detail", str(exc)))


def _price_cell(item: Dict[str, Any], pack_size: str) -> str:
    price = item["prices"].get(pack_size)
    return f"₹{price}" if price is not None else "—"


def render_collection_text(collection_key: str, items: List[Dict[str, Any]]) -> str:
    """The exact chatbot reply for this collection, built from structured
    data instead of hand-typed — this is what makes an admin edit and the
    customer-visible answer the same fact instead of two copies of it."""
    active_items = [i for i in items if i.get("active", True)]
    rows = []
    name_col_width = max([len("Item / Flavour")] + [len(i["flavour"]) + (len(f" ({i['note']})") if i.get("note") else 0) for i in active_items])
    header = f"| {'Item / Flavour'.ljust(name_col_width)} | 250g  | 500g  |  1kg  |"
    border = "+" + "-" * (name_col_width + 2) + "+-------+-------+-------+"
    rows.append(border)
    rows.append(header)
    rows.append(border)
    for item in active_items:
        label = item["flavour"] + (f" ({item['note']})" if item.get("note") else "")
        cells = " | ".join(_price_cell(item, size).ljust(5) for size in PACK_SIZES)
        rows.append(f"| {label.ljust(name_col_width)} | {cells} |")
    rows.append(border)
    table = "\n".join(rows)

    piece_notes = []
    seen_piece_shapes = set()
    for item in active_items:
        pieces = item.get("pieces")
        if not pieces:
            continue
        shape = tuple(pieces.get(size) for size in PACK_SIZES)
        if shape in seen_piece_shapes:
            continue
        seen_piece_shapes.add(shape)
        parts = " | ".join(f"{size}({pieces[size]}pc)" for size in PACK_SIZES if pieces.get(size))
        if parts:
            piece_notes.append(f"* {item['flavour']}: {parts}" if len(seen_piece_shapes) > 1 else f"* {parts}")

    intro = _COLLECTION_INTRO.get(collection_key, "")
    outro = _COLLECTION_OUTRO.get(collection_key, "").strip()
    body = "\n".join(["```", table, *piece_notes, "```"])
    return "\n\n".join(part for part in (intro, body, outro) if part)


def preview(menu_key: str) -> Dict[str, Any]:
    """Exactly what publishing would show — draft if present, else published."""
    entry = _menu_entry(menu_key)
    items = get_draft_items(menu_key)
    used = "draft"
    if items is None:
        items = get_published_items(menu_key)
        used = "published"
    if entry["kind"] == "collection":
        text = render_collection_text(entry["collection_key"], items)
    else:
        text = "\n".join(
            f"- {i['item']}: ₹{i['price_inr']}" + (f" ({i['quantity']})" if i.get("quantity") else "")
            for i in items
        )
    return {"menu_key": menu_key, "source": used, "items": items, "customer_facing_text": text}


def save_draft(menu_key: str, raw_items: Any, updated_by: Optional[str] = None) -> Dict[str, Any]:
    items = validate_items(menu_key, raw_items)
    db.save_menu_draft(menu_key, json.dumps(items, ensure_ascii=False), updated_by)
    return {"menu_key": menu_key, "draft": items}


def discard_draft(menu_key: str) -> None:
    _menu_entry(menu_key)
    db.clear_menu_draft(menu_key)


def publish(menu_key: str, raw_items: Any = None, changed_by: Optional[str] = None) -> Dict[str, Any]:
    """Validates, writes to the authoritative source, hot-reloads the live
    responder, records a version, and clears the draft. `raw_items` lets a
    caller publish an explicit body directly (used by restore); omitted, the
    current draft is published."""
    entry = _menu_entry(menu_key)
    if raw_items is None:
        raw_items = get_draft_items(menu_key)
        if raw_items is None:
            raise MenuValidationError("There is no draft to publish for this menu.")
    items = validate_items(menu_key, raw_items)

    menu_data = _read_menu_json()
    if entry["kind"] == "collection":
        collection_key = entry["collection_key"]
        menu_data.setdefault("collections", {}).setdefault(collection_key, {})["products"] = items
        _write_menu_json(menu_data)
        # The chatbot's approved reply for this collection is regenerated
        # from the same data just written — one write, one fact, no drift.
        _publish_collection_template(entry["template_id"], collection_key, items)
    else:
        weekly = menu_data.get("weekly_menus")
        if not weekly:
            raise MenuValidationError("No weekly_menus block exists in menu.json to publish into.")
        weekly[-1][entry["weekly_field"]] = items
        weekly[-1]["is_published"] = True
        _write_menu_json(menu_data)

    from camellia.responder import responder
    responder.reload_data()

    version_id = db.record_menu_version(
        menu_key, json.dumps(items, ensure_ascii=False), action="publish", changed_by=changed_by,
    )
    db.clear_menu_draft(menu_key)
    log.info("Published menu %s (version %s) by %s", menu_key, version_id, changed_by or "admin")
    return {"menu_key": menu_key, "version_id": version_id, "items": items}


def _publish_collection_template(template_id: str, collection_key: str, items: List[Dict[str, Any]]) -> None:
    with open(config.QA_TEMPLATES_PATH, "r", encoding="utf-8") as f:
        qa_data = json.load(f)
    text = render_collection_text(collection_key, items)
    found = False
    for template in qa_data.get("templates", []):
        if template.get("id") == template_id:
            template["response"] = text
            found = True
            break
    if not found:
        raise MenuValidationError(f"No approved template '{template_id}' exists to publish into.")
    with open(config.QA_TEMPLATES_PATH, "w", encoding="utf-8") as f:
        json.dump(qa_data, f, indent=2, ensure_ascii=False)


def list_versions(menu_key: str, limit: int = 20) -> List[Dict[str, Any]]:
    _menu_entry(menu_key)
    rows = db.get_menu_versions(menu_key, limit=limit)
    for row in rows:
        row["items"] = json.loads(row["snapshot_json"])
    return rows


def restore_version(menu_key: str, version_id: int, changed_by: Optional[str] = None) -> Dict[str, Any]:
    _menu_entry(menu_key)
    version = db.get_menu_version(version_id)
    if not version or version["menu_key"] != menu_key:
        raise KeyError(version_id)
    items = json.loads(version["snapshot_json"])
    result = publish(menu_key, raw_items=items, changed_by=changed_by)
    # The publish above already recorded a "publish" version; relabel it as
    # a restore so history shows what actually happened.
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE menu_versions SET action = 'restore' WHERE id = ?",
            (result["version_id"],),
        )
        conn.commit()
    return result
