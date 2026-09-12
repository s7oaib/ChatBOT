"""Small, idempotent customer-copy remediations from the content audit.

These edits deliberately use the versioned Response Library override store.  The
protected template triggers and routing metadata remain untouched, and an
operator's existing edit is never overwritten silently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional


ACTOR = "content_integrity_audit_2026-09-13"
REASON = "approved_canonical_content_correction"

# Preserved in version history when the seed was already upgraded before the
# durable override migration ran. This is audit evidence only and is never
# returned to a customer.
LEGACY_RESPONSES = {
    "sugar_free_options": (
        "At Camellia Artisanal, all our Sondesh are handcrafted without artificial "
        "chemical sweeteners or corn syrups:\n\n• *Authentic Nolen Gur Norom Pak*: "
        "Made with 100% natural, unrefined date palm jaggery (Nolen Gur). It has a "
        "lower glycemic index and a mild, natural sweetness compared to refined white "
        "sugar sweets.\n• *Mild Sweetness Profile*: Because we make fresh small batches "
        "with pure dairy / A2 cow milk chhena, our Sondesh is noticeably light and "
        "subtly sweetened, never cloying.\n• *Diabetic Guidance*: While naturally "
        "sweetened with pure Nolen Gur or minimal unrefined cane sugar, it is not "
        "certified medical zero-calorie sugar-free. We recommend moderation or "
        "consulting with a physician for strict diabetic diets.\n\nWould you like "
        "to try our subtly sweetened Nolen Gur Norom Pak or Classic Sondesh?"
    ),
}


def _week(menu_data: Mapping[str, Any]) -> Mapping[str, Any]:
    weeks = menu_data.get("weekly_menus") or []
    return weeks[-1] if weeks else {}


def _weekly_text(title: str, emoji: str, entries: Iterable[Mapping[str, Any]], closing: str) -> str:
    lines = [f"*Camellia Artisanal — This Week's {title}* {emoji}",
             "_Current Week (Friday Lunch – Thursday Dinner)_\n"]
    for item in entries:
        name = item.get("item", "")
        quantity = item.get("quantity", "")
        price = item.get("price_inr", "")
        description = item.get("description", "")
        suffix = f" ({quantity})" if quantity else ""
        detail = f" • _{description}_" if description else ""
        lines.append(f"• *{name}*{suffix} — *₹{price}*{detail}")
    lines.extend([
        "\n📦 *Packaging*: 100% plastic-free, eco-friendly cornstarch boxes.",
        "🚚 *Delivery*: Across Delhi NCR from Sushant Arcade, Gurgaon (Courier on actuals).",
        f"\n{closing}",
    ])
    return "\n".join(lines)


CONFIRMED_COPY_TEMPLATES = (
    "jolbhora", "jolbhora_talgash_details", "sondesh_price_detailed",
    "aamar_mishti_collection", "signature_collection",
    "pack_size_250g", "pack_size_500g", "pack_size_1kg",
)


def canonical_override_responses(
    menu_data: Mapping[str, Any], templates: Optional[Iterable[Mapping[str, Any]]] = None
) -> Dict[str, str]:
    """Build the four safe corrections from the current canonical menu file."""
    week = _week(menu_data)
    vegetarian = _weekly_text(
        "Vegetarian Menu", "🌿", week.get("vegetarian", []),
        "Which vegetarian dishes would you like to order? 🙏",
    )
    nonvegetarian = _weekly_text(
        "Non-Vegetarian Menu", "🍗", week.get("non_vegetarian", []),
        "Which non-vegetarian dishes would you like to order? 🙏",
    )
    bentos = week.get("bengali_bento", []) or week.get("combos", [])
    bento_lines = ["*Camellia Artisanal — Bengali Bento Meals & Combos* 🍱", ""]
    for bento in bentos:
        includes = ", ".join(bento.get("includes", []))
        suffix = f" (Includes: {includes})" if includes else ""
        bento_lines.append(f"• *{bento.get('name', '')}* — *₹{bento.get('price_inr', '')}*{suffix}")
    bento_lines.extend([
        "\n📦 *Packaging*: 100% plastic-free, eco-friendly cornstarch boxes.",
        "🚚 *Delivery*: Across Delhi NCR from Sushant Arcade, Gurgaon.",
        "\nWhich Bento meal would you like to order?",
    ])
    bento_text = "\n".join(bento_lines)
    result = {
        "weekly_menu_veg": vegetarian,
        "weekly_menu_nonveg": nonvegetarian,
        "combo_bento": bento_text,
        "bento_details": bento_text,
        "sugar_free_options": (
            "Our Sondesh is made in small batches without artificial chemical sweeteners "
            "or corn syrups. We do not offer a certified sugar-free product. If you have "
            "a dietary or medical requirement, please tell us before ordering so our team "
            "can check the ingredients with you."
        ),
    }
    # These templates already contain the client-confirmed Jolbhora matrix.
    # Materialising them as managed overrides makes the Response Library and
    # its version history the same authoritative copy used by production.
    if templates is not None:
        for template in templates:
            template_id = str(template.get("id") or "")
            if template_id in CONFIRMED_COPY_TEMPLATES:
                result[template_id] = template.get("response") or ((template.get("responses") or [""])[0])
    return result


def apply_safe_content_remediations(
    database: Any,
    menu_data: Mapping[str, Any],
    templates: Iterable[Mapping[str, Any]],
    logger: Optional[Any] = None,
) -> Dict[str, str]:
    """Apply missing/legacy overrides, preserving any distinct operator edit.

    Returns a per-template action map (``created``, ``updated``, ``skipped``).
    The function is safe to call on every startup and in isolated test databases.
    """
    defaults = {str(t.get("id")): (t.get("response") or ((t.get("responses") or [""])[0]))
                for t in templates if t.get("id")}
    desired = canonical_override_responses(menu_data, templates)
    actions: Dict[str, str] = {}
    unsafe_health_terms = (
        "glycemic", "diabetic-friendly", "sugar-safe", "healthier for diabetes",
        "suitable for diabetics",
    )
    for template_id, response in desired.items():
        current = database.get_response_template_override(template_id)
        default = defaults.get(template_id, "")
        if current and str(current.get("status") or "ACTIVE").upper() == "ACTIVE":
            if (current.get("response") or "").strip() == response.strip():
                actions[template_id] = "unchanged"
                continue
            # Never overwrite a client-authored copy.  The audit can be rerun
            # after confirmation without destroying a deliberate edit.
            current_copy = (current.get("response") or "").strip()
            unsafe_existing = template_id == "sugar_free_options" and any(
                term in current_copy.lower() for term in unsafe_health_terms
            )
            managed_existing = str(current.get("updated_by") or "") == ACTOR
            if current_copy != default.strip() and not unsafe_existing and not managed_existing:
                actions[template_id] = "skipped"
                continue
            ok = database.save_response_template_override(
                template_id, response, ACTOR, change_reason=REASON,
                previous_response=LEGACY_RESPONSES.get(template_id, default),
            )
            actions[template_id] = "updated" if ok else "failed"
            continue
        ok = database.save_response_template_override(
            template_id, response, ACTOR, change_reason=REASON,
            previous_response=LEGACY_RESPONSES.get(template_id, default),
        )
        actions[template_id] = "created" if ok else "failed"
    if logger:
        for template_id, action in actions.items():
            if action in {"created", "updated", "skipped"}:
                logger.info("Content-integrity remediation %s: %s", template_id, action)
    return actions


def apply_confirmed_menu_facts(menu_path: str, logger: Optional[Any] = None) -> bool:
    """Merge only the newly confirmed facts into the durable menu file.

    Weekly customer edits are left untouched.  The merge is intentionally
    narrow: Jolbhora pricing/pieces, Bento tiers, and the confirmation record.
    """
    path = Path(menu_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    changed = False
    collections = data.get("collections") or {}
    targets = {
        "aamar_mishti": {"250g": (6, 750), "500g": (12, 1400), "1kg": (24, 2500)},
        "signature_collection": {"250g": (6, 900), "500g": (12, 1600), "1kg": (24, 2900)},
    }
    for collection_id, packs in targets.items():
        for product in (collections.get(collection_id, {}).get("products") or []):
            if str(product.get("item") or "").strip().lower() != "jolbhora":
                continue
            prices = product.setdefault("prices", [])
            by_pack = {str(row.get("pack_size")): row for row in prices}
            for pack, (pieces, price) in packs.items():
                row = by_pack.get(pack)
                if row is None:
                    prices.append({"pack_size": pack, "pieces": pieces, "price_inr": price})
                    changed = True
                else:
                    if row.get("pieces") != pieces or row.get("price_inr") != price:
                        row.update({"pieces": pieces, "price_inr": price})
                        changed = True
    confirmed_bentos = [
        {"name": "Veg Bento", "price_inr": 400, "includes": ["6 Luchi", "Chholar Daal", "Tomato Aamsottwo Chatni", "1 Classic Aamar Sondesh"]},
        {"name": "Non-Veg Bento", "price_inr": 700, "includes": ["6 Luchi", "Mangsho (Mutton Kosha)", "Tomato Aamsottwo Chatni", "1 Classic Aamar Sondesh"]},
    ]
    weeks = data.get("weekly_menus") or []
    if weeks and weeks[-1].get("bengali_bento") != confirmed_bentos:
        weeks[-1]["bengali_bento"] = confirmed_bentos
        changed = True
    order_types = data.setdefault("order_types", {})
    bento_type = order_types.setdefault("bengali_bento", {})
    if bento_type.get("tiers") != [
        {"tier_name": "Vegetarian Bento", "price_inr": 400, "includes": confirmed_bentos[0]["includes"]},
        {"tier_name": "Non-Vegetarian Bento", "price_inr": 700, "includes": confirmed_bentos[1]["includes"]},
    ]:
        bento_type["tiers"] = [
            {"tier_name": "Vegetarian Bento", "price_inr": 400, "includes": confirmed_bentos[0]["includes"]},
            {"tier_name": "Non-Vegetarian Bento", "price_inr": 700, "includes": confirmed_bentos[1]["includes"]},
        ]
        bento_type.pop("status", None)
        changed = True
    meta = data.setdefault("meta", {})
    facts = meta.setdefault("client_confirmed_facts", {})
    if facts.get("confirmed_at") != "2026-09-13":
        facts.update({"confirmed_at": "2026-09-13", "jolbhora_prices_inr": {
            "aamar_mishti": {"250g": 750, "500g": 1400, "1kg": 2500},
            "signature": {"250g": 900, "500g": 1600, "1kg": 2900},
        }, "jolbhora_pieces": {"250g": 6, "500g": 12, "1kg": 24}})
        changed = True
    if changed:
        try:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            if logger:
                logger.info("Applied client-confirmed Jolbhora/Bento facts to %s", path)
            return True
        except Exception:
            return False
    return False
