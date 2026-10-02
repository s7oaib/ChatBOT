"""Keep protected Response Library copy aligned with effective Approved Q&A."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from camellia import client_qa
from camellia.handoff import customer_facing_notice


# A binding keeps the protected template ID and triggers for compatibility;
# customer-facing business copy is sourced from the current Approved Q&A row.
APPROVED_QA_BINDINGS: Dict[str, Tuple[str, ...]] = {
    "alacarte_prebooking_steps": ("alacarte_prebook",),
    "bengali_home_table_details": ("home_table_details",),
    "bengali_home_table_payment": ("home_table_payment",),
    "bengali_home_table_pricing": ("home_table_price",),
    "bento_details": ("bento_details_client",),
    "chicken_kosha_price": ("chicken_kosha_price_client",),
    "combo_bento": ("bento_details_client",),
    "delivery_out_of_area": ("delivery_out_of_area_client",),
    "GUARD_payment_method": ("general_payment_client",),
    "how_to_order": ("order_prebook",),
    "minimum_sondesh_quantity": ("minimum_sondesh_quantity_client",),
    "order_next_steps": ("order_ready_handoff",),
    "pack_size_250g": ("sondesh_price_client",),
    "pricing_value_context": ("price_is_high",),
    "savoury_best_time_to_book": ("best_time_to_book",),
    "savoury_delivery_time": ("savoury_delivery_time",),
    "savoury_food_intro": ("general_details",),
    "sondesh_details": ("sondesh_details_client",),
    "sondesh_dispatch_time": ("sondesh_delivery_time",),
    "sondesh_price_detailed": ("sondesh_price_client",),
    "sondesh_unfamiliar": ("sondesh_unfamiliar_client",),
    "subscription_details": ("subscription_details_client",),
}

MENU_DELEGATION_TEMPLATE_IDS = frozenset({"weekly_menu_veg", "weekly_menu_nonveg", "aamar_mishti_collection", "signature_collection", "previous_menus"})

# These entries perform conversation management or offer a controlled human
# path. They do not answer a separate business fact and keep their current role.
SYSTEM_GUARD_TEMPLATE_IDS = frozenset({
    "bot_capabilities", "bot_identity", "contact_social", "greeting",
    "know_more", "name_introduction", "order_status_tracking", "product_overview",
    "recall_name", "recall_order_flavour", *MENU_DELEGATION_TEMPLATE_IDS,
})

# Business-looking entries without a current/effective Approved Q&A equivalent
# fail closed through the ordinary human handoff path.
HANDOFF_ONLY_TEMPLATE_IDS = frozenset({
    "aamar_mishti_collection", "artisanal_differentiation", "freshness_ingredients",
    "GUARD_allergen_nutfree_glutenfree", "GUARD_collection_differentiation",
    "GUARD_customization_flavour_mixing", "GUARD_dietary_suitability",
    "GUARD_halal_ingredients_oil", "GUARD_pickup_address_hours",
    "GUARD_shelf_life_storage", "GUARD_staff_names_manager_contact",
    "jolbhora", "jolbhora_talgash_details", "kanchagolla",
    "kolkata_expansion_info", "mahua_signature_special", "nolen_gur_aamar",
    "opening_hours", "packaging", "previous_menus", "product_recommendation",
    "savoury_crosssell_sondesh", "signature_collection", "sondesh_classic_rose",
    "sondesh_crosssell_savoury", "sondesh_mango_kesar", "sondesh_nolen_gur",
    "subscription_booking_steps", "sugar_free_options", "delivery_area", "delivery_time",
    "pack_size_500g", "pack_size_1kg",
})

# An approved copy can expire. In that case the trigger remains as a protected
# compatibility identity but is handled as unsupported and handed to a person.
HANDOFF_ONLY_TEMPLATE_IDS = (HANDOFF_ONLY_TEMPLATE_IDS | frozenset(APPROVED_QA_BINDINGS)) - MENU_DELEGATION_TEMPLATE_IDS


def effective_binding(template_id: str, entries=None) -> tuple[Optional[str], Optional[str]]:
    """Return the first active/effective QA ID and its exact customer answer."""
    for qa_id in APPROVED_QA_BINDINGS.get(str(template_id), ()):
        if entries is None:
            match = client_qa.resolve_effective_approved_qa_by_id(qa_id)
            entry = match.entry if match else None
        else:
            entry = entries.get(qa_id)
        if entry is not None:
            return qa_id, entry.answer
    return None, None


def runtime_template(template: Mapping[str, Any], entries=None) -> Optional[Dict[str, Any]]:
    """Ignore durable copy for authority-bound IDs, resolving lifecycle on every read."""
    template_id = str(template.get("id") or "")
    if template_id not in HANDOFF_ONLY_TEMPLATE_IDS and template_id not in MENU_DELEGATION_TEMPLATE_IDS:
        return None
    qa_id, answer = effective_binding(template_id, entries)
    result = dict(template)
    result.pop("responses", None)
    result["response"] = "" if template_id in MENU_DELEGATION_TEMPLATE_IDS else answer or customer_facing_notice()
    result["escalate"] = bool(template.get("escalate")) or (
        not bool(qa_id) and template_id not in MENU_DELEGATION_TEMPLATE_IDS)
    return result


def runtime_templates(templates: Iterable[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    """Read one effective lifecycle snapshot for this materialization, never cache it."""
    from camellia import approved_qa
    try:
        entries = {entry.qa_id: entry for entry in approved_qa.registry(approved_qa.overlays(strict=True))}
    except Exception:
        entries = {}
    return [runtime_template(template, entries) or dict(template) for template in templates]


def unsupported_pack_request(message: str) -> bool:
    """An old pack trigger cannot broaden the approved Sondesh availability."""
    text = str(message or "")
    for template_id, size in (("pack_size_500g", r"500\s*g"), ("pack_size_1kg", r"1\s*kg")):
        if (re.search(rf"\b{size}\b", text, re.I)
                and (re.fullmatch(rf"\s*{size}\s*[?!.]*", text, re.I)
                     or re.search(r"\b(?:sondesh|sandesh|mishti|pack)\b", text, re.I))
                and not effective_binding(template_id)[0]):
            return True
    return False


def aligned_responses() -> Dict[str, str]:
    """Return one current approved answer or a controlled handoff for each binding."""
    result = {}
    handoff_copy = customer_facing_notice()
    for template_id in APPROVED_QA_BINDINGS:
        _qa_id, answer = effective_binding(template_id)
        result[template_id] = answer or handoff_copy
    return result


def apply_approved_qa_alignment(database: Any, templates: Iterable[Mapping[str, Any]]) -> Dict[str, str]:
    """Version and align protected template copy without changing triggers or IDs."""
    by_id = {str(row.get("id") or ""): row for row in templates}
    changes = {}
    for template_id, response in aligned_responses().items():
        template = by_id.get(template_id)
        if not template:
            continue
        current = database.get_response_template_override(template_id)
        current_response = (current or {}).get("response")
        if current_response == response and (current or {}).get("status", "ACTIVE") == "ACTIVE":
            changes[template_id] = "UNCHANGED"
            continue
        default = str(template.get("response") or ((template.get("responses") or [""])[0]))
        saved = database.save_response_template_override(
            template_id, response, updated_by="approved_qa_alignment",
            status="ACTIVE", change_reason="align_to_effective_approved_qa",
            previous_response=default if current is None else None,
        )
        changes[template_id] = "ALIGNED" if saved else "FAILED"
    return changes


def matching_handoff_only_template(message: str, templates: Iterable[Mapping[str, Any]]) -> Optional[str]:
    """Find a non-authoritative business trigger without widening its wording."""
    normalized = " ".join(str(message or "").casefold().split())
    if not normalized:
        return None
    matches = []
    for template in templates:
        template_id = str(template.get("id") or "")
        if template_id not in HANDOFF_ONLY_TEMPLATE_IDS:
            continue
        for trigger in template.get("triggers") or []:
            trigger_text = " ".join(str(trigger or "").casefold().split())
            if trigger_text and trigger_text in normalized:
                matches.append((len(trigger_text), template_id))
    return max(matches)[1] if matches else None
