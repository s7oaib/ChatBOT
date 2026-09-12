"""Small, idempotent customer-copy remediations from the content audit.

These edits deliberately use the versioned Response Library override store.  The
protected template triggers and routing metadata remain untouched, and an
operator's existing edit is never overwritten silently.
"""

from __future__ import annotations

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


def canonical_override_responses(menu_data: Mapping[str, Any]) -> Dict[str, str]:
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
    return {
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
    desired = canonical_override_responses(menu_data)
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
