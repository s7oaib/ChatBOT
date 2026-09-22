"""Customer-provided Instagram order request drafts (never confirmations)."""

import re
from urllib.parse import quote

from camellia.config import config


ORDER_DRAFT_INCOMPLETE = "ORDER_DRAFT_INCOMPLETE"
ORDER_DRAFT_READY = "ORDER_DRAFT_READY"

_HOW_TO_ORDER_RE = re.compile(
    r"\b(?:how\s+(?:do\s+i|can\s+i|to)\s+(?:place\s+)?(?:an?\s+)?order|"
    r"how\s+can\s+i\s+place\s+(?:an?\s+)?order|"
    r"i\s+(?:want|would\s+like)\s+to\s+(?:place\s+)?(?:an?\s+)?order|"
    r"what\s+are\s+the\s+next\s+steps\s+to\s+(?:order|place\s+(?:an?\s+)?order)|"
    r"how\s+(?:do\s+i|can\s+i)\s+(?:pre\s*[- ]?book|pre\s*[- ]?order))\b",
    re.I,
)
_QUANTITY_RE = re.compile(r"\b(\d+\s*(?:boxes?|packs?|pieces?|pcs)|\d+(?:\.\d+)?\s*(?:kg|g))\b", re.I)
_SLOT_RE = re.compile(
    r"\b(?:(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"\s+(?:morning|afternoon|lunch|dinner)|"
    r"(?:morning|afternoon|lunch|dinner)\s+(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.I,
)
_AREA_RE = re.compile(r"\b(?:delhi\s+ncr|gurgaon|gurugram|delhi|noida|ghaziabad|faridabad|kolkata)\b", re.I)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{8,}\d)(?!\w)")
_PIN_RE = re.compile(r"https?://[^\s]+(?:maps\.app\.goo\.gl|google\.[^/]+/maps|maps\.google\.[^/]+)[^\s]*", re.I)
_PRODUCT_RE = re.compile(
    r"\b(?:sondesh(?:\s+classic)?|jolbhora|kanchagolla|nolen\s+gur|"
    r"chicken\s+bento|bengali\s+bento|bento|subscription)\b",
    re.I,
)
_ORDER_REQUEST_RE = re.compile(
    r"\b(?:need|i\s+(?:want|would\s+like)|please\s+order)\b|"
    r"\b\d+\s+(?:boxes?|packs?|pieces?|pcs)\b",
    re.I,
)
_EDIT_FIELD_RE = re.compile(
    r"\bchange\s+(quantity|item|address|delivery area|area|slot|day|phone|receiver phone|location pin)\b",
    re.I,
)


def new_order_draft() -> dict:
    return {
        "status": ORDER_DRAFT_INCOMPLETE,
        "item": None,
        "quantity": None,
        "delivery_area": None,
        "delivery_address": None,
        "delivery_slot": None,
        "receiver_phone": None,
        "location_pin": None,
        "location_pin_required": False,
        "edit_field": None,
    }


def is_order_process_question(message: str) -> bool:
    return bool(_HOW_TO_ORDER_RE.search(str(message or "")))


def is_order_request(message: str, active_product_context=None) -> bool:
    text = str(message or "")
    has_product = bool(_PRODUCT_RE.search(text))
    has_quantity = bool(_QUANTITY_RE.search(text) or re.match(r"\s*\d+\s+(?=(?:chicken|bengali|sondesh|bento)\b)", text, re.I))
    context_known = str(active_product_context or "").casefold() in {
        "sondesh", "sondesh_general", "bento", "subscription",
    }
    return bool(
        (has_product and has_quantity)
        or (_ORDER_REQUEST_RE.search(text) and (_extract_customer_item(text) or context_known))
        or (has_quantity and _AREA_RE.search(text) and _extract_customer_item(text))
        or (context_known and has_quantity and re.search(r"\b(?:i\s+want|need|make\s+it)\b", text, re.I))
    )


def is_edit_request(message: str) -> bool:
    return bool(re.search(r"\bedit\s+(?:my\s+)?order\b", str(message or ""), re.I))


def is_price_or_payment(message: str) -> bool:
    return bool(re.search(r"\b(?:price|cost|rate|how\s+much|pay|payment|upi|cash\s+on\s+delivery|cod)\b", str(message or ""), re.I))


def is_unsupported_question(message: str) -> bool:
    return bool(re.search(
        r"\b(?:available|availability|in\s+stock|discount|offer|wedding|event|cater|catering|bulk|guests)\b",
        str(message or ""), re.I,
    ))


def update_order_draft(draft: dict, message: str, active_product_context=None) -> dict:
    """Extract only explicit customer fields, preserving all other draft values."""
    text = str(message or "").strip()
    if not text:
        return draft
    edit = draft.get("edit_field")
    requested_edit = _EDIT_FIELD_RE.search(text)
    if requested_edit and not any((
        _QUANTITY_RE.search(text), _SLOT_RE.search(text), _PHONE_RE.search(text),
        _PIN_RE.search(text), _PRODUCT_RE.search(text),
    )):
        target = requested_edit.group(1).casefold()
        draft["edit_field"] = {
            "area": "delivery_area", "delivery area": "delivery_area",
            "day": "delivery_slot", "slot": "delivery_slot",
            "phone": "receiver_phone", "receiver phone": "receiver_phone",
        }.get(target, target)
        draft["status"] = ORDER_DRAFT_INCOMPLETE
        return draft

    quantity_match = _QUANTITY_RE.search(text)
    updated_fields = set()
    if quantity_match:
        draft["quantity"] = " ".join(quantity_match.group(1).split())
        updated_fields.add("quantity")
    elif not draft.get("quantity"):
        leading_count = re.match(r"\s*(\d+)\s+(?=(?:chicken|bengali|sondesh|bento)\b)", text, re.I)
        if leading_count:
            draft["quantity"] = leading_count.group(1)
            updated_fields.add("quantity")

    product_match = _PRODUCT_RE.search(text)
    if product_match:
        draft["item"] = " ".join(product_match.group(0).split()).title()
        updated_fields.add("item")
    elif not is_order_process_question(text) and (not draft.get("item") or edit == "item"):
        extracted_item = _extract_customer_item(text)
        if extracted_item:
            draft["item"] = extracted_item
            updated_fields.add("item")
        elif _QUANTITY_RE.search(text):
            context_products = {
                "sondesh": "Sondesh", "sondesh_general": "Sondesh",
                "bento": "Bengali Bento", "subscription": "Subscription",
            }
            contextual_item = context_products.get(str(active_product_context or "").casefold())
            if contextual_item:
                draft["item"] = contextual_item
                updated_fields.add("item")

    slot_match = _SLOT_RE.search(text)
    if slot_match:
        draft["delivery_slot"] = " ".join(slot_match.group(0).split()).title()
        updated_fields.add("delivery_slot")

    area_match = _AREA_RE.search(text)
    if area_match:
        area = " ".join(area_match.group(0).split()).title()
        draft["delivery_area"] = "Delhi NCR" if area.casefold() == "delhi ncr" else area
        updated_fields.add("delivery_area")

    phone_match = _PHONE_RE.search(text)
    if phone_match and len(re.sub(r"\D", "", phone_match.group(0))) >= 10:
        draft["receiver_phone"] = phone_match.group(0).strip()
        updated_fields.add("receiver_phone")

    pin_match = _PIN_RE.search(text)
    if pin_match:
        draft["location_pin"] = pin_match.group(0).rstrip(".,)")
        updated_fields.add("location_pin")

    if _looks_like_address(text, area_match, phone_match, slot_match, quantity_match, product_match):
        address_text = _extract_address_text(text)
        if address_text:
            if address_text != draft.get("delivery_address") and not pin_match:
                draft["location_pin"] = None
            draft["delivery_address"] = address_text
            updated_fields.add("delivery_address")
        draft["location_pin_required"] = bool(re.search(r"\b(?:standalone|independent)\s+house\b", text, re.I))
        if not draft["location_pin_required"]:
            draft["location_pin"] = None

    if edit:
        field = {"area": "delivery_area", "day": "delivery_slot", "slot": "delivery_slot", "phone": "receiver_phone", "receiver phone": "receiver_phone", "location pin": "location_pin"}.get(edit, edit)
        if field in updated_fields:
            draft["edit_field"] = None

    draft["status"] = ORDER_DRAFT_READY if not missing_fields(draft) else ORDER_DRAFT_INCOMPLETE
    return draft


def _extract_customer_item(text: str) -> str | None:
    known = _PRODUCT_RE.search(text)
    if known:
        return " ".join(known.group(0).split()).title()
    candidate = str(text or "")
    candidate = re.sub(
        r"^\s*how\s+(?:do\s+i|can\s+i|to)\s+(?:place\s+)?(?:an?\s+)?order\s*",
        "", candidate, flags=re.I,
    )
    candidate = re.sub(r"^\s*(?:need|i\s+want|i\s+would\s+like|please\s+order)\s+", "", candidate, flags=re.I)
    candidate = _QUANTITY_RE.sub(" ", candidate)
    candidate = _SLOT_RE.sub(" ", candidate)
    candidate = _AREA_RE.sub(" ", candidate)
    candidate = _PHONE_RE.sub(" ", candidate)
    candidate = _PIN_RE.sub(" ", candidate)
    candidate = re.sub(r"\b(?:for|in|to|at)\b", " ", candidate, flags=re.I)
    candidate = re.sub(r"\s+", " ", candidate).strip(" ,.-")
    if (
        candidate.casefold() in {"a", "an", "order", "place an order", "place order"}
        or re.match(r"^(?:know|learn|see|ask|talk|speak|information|more)\b", candidate, re.I)
    ):
        return None
    return candidate or None


def _looks_like_address(text, area_match, phone_match, slot_match, quantity_match, product_match) -> bool:
    candidate = _extract_address_text(text)
    if not candidate or re.fullmatch(r"\d+", candidate):
        return False
    if re.search(r"\b(?:flat|apartment|tower|sector|phase|block|floor|road|street|lane|standalone\s+house|independent\s+house|dlf)\b", candidate, re.I):
        return True
    return bool(area_match and ("," in candidate or re.search(r"\d", candidate)))


def _extract_address_text(text: str) -> str:
    candidate = str(text or "")
    preserve_area = bool(re.search(
        r"\b(?:flat|apartment|tower|sector|phase|block|floor|road|street|lane|standalone\s+house|independent\s+house|dlf)\b",
        candidate, re.I,
    ))
    candidate = _PHONE_RE.sub(" ", candidate)
    candidate = _PIN_RE.sub(" ", candidate)
    candidate = _SLOT_RE.sub(" ", candidate)
    candidate = _QUANTITY_RE.sub(" ", candidate)
    candidate = _PRODUCT_RE.sub(" ", candidate)
    if not preserve_area:
        candidate = _AREA_RE.sub(" ", candidate)
    candidate = re.sub(r"\b(?:need|i\s+want|i\s+would\s+like|please|order|for|in|to|at|delivery|address)\b", " ", candidate, flags=re.I)
    candidate = re.sub(r"\s+", " ", candidate).strip(" ,.-")
    return candidate


def missing_fields(draft: dict) -> list[str]:
    missing = [field for field in ("item", "quantity", "delivery_area") if not draft.get(field)]
    if not draft.get("delivery_slot"):
        missing.append("delivery_slot")
    if not draft.get("delivery_address"):
        missing.append("delivery_address")
    if not draft.get("receiver_phone"):
        missing.append("receiver_phone")
    if draft.get("location_pin_required") and not draft.get("location_pin"):
        missing.append("location_pin")
    if draft.get("edit_field"):
        missing.append(draft["edit_field"])
    return missing


def instruction_message() -> str:
    return (
        "Please send your order in one simple sentence like this:\n\n"
        "Need [quantity] [item] for [day/slot] in [delivery area]\n\n"
        "Examples:\n\n"
        "Need 2 boxes Sondesh for Friday dinner in Gurgaon\n\n"
        "Sondesh Classic 250g to Gurgaon\n\n"
        "You can send it naturally in your own words too."
    )


def next_question(draft: dict) -> str:
    missing = missing_fields(draft)
    if draft.get("edit_field"):
        prompts = {
            "quantity": "What quantity would you like instead?",
            "item": "What item would you like instead?",
            "delivery_area": "Which delivery area should I change it to?",
            "delivery_slot": "What preferred delivery day and slot should I use?",
            "delivery_address": "What should I change the full delivery address to?",
            "receiver_phone": "What receiver phone number should I use?",
            "location_pin": "Please send the updated Google Maps location pin.",
        }
        return prompts.get(draft["edit_field"], "What would you like to change?")
    if "item" in missing and "quantity" in missing:
        return "Please share the item and quantity you'd like."
    if "item" in missing:
        return "Which item would you like?"
    if "quantity" in missing:
        return "What quantity would you like?"
    if "delivery_area" in missing:
        return "Which delivery area should I note?"
    if "delivery_slot" in missing:
        return "Please send your preferred delivery day and slot, such as Friday lunch or Friday dinner."
    details = []
    if "delivery_address" in missing:
        details.append("the full delivery address")
    if "receiver_phone" in missing:
        details.append("the receiver's phone number")
    if "location_pin" in missing:
        details.append("the Google Maps location pin for the standalone house")
    if details:
        return "Please send " + " and ".join(details) + "."
    return "Please share the missing order detail."


def request_summary(draft: dict) -> str:
    pin = draft.get("location_pin") or ("Not required" if not draft.get("location_pin_required") else "Needed")
    return (
        "ORDER REQUEST\n\n"
        f"Item: {draft.get('item') or 'Not supplied'}\n"
        f"Quantity: {draft.get('quantity') or 'Not supplied'}\n"
        f"Delivery area: {draft.get('delivery_area') or 'Not supplied'}\n"
        f"Delivery address: {draft.get('delivery_address') or 'Not supplied'}\n"
        f"Preferred slot: {draft.get('delivery_slot') or 'Not supplied'}\n"
        f"Receiver phone: {draft.get('receiver_phone') or 'Not supplied'}\n"
        f"Location pin: {pin}\n\n"
        "This is an order request only.\n"
        "Our kitchen team will confirm availability, delivery charges and the invoice."
    )


def whatsapp_draft_link(draft: dict) -> str | None:
    number = re.sub(r"\D", "", config.WHATSAPP_BUSINESS_NUMBER or "")
    if not number:
        return None
    pin = draft.get("location_pin")
    lines = [
        "Hi, I'd like to place an order with Camellia Artisanal.",
        "",
        "ORDER REQUEST",
        "",
        f"Item: {draft.get('item') or ''}",
        f"Quantity: {draft.get('quantity') or ''}",
        f"Delivery area: {draft.get('delivery_area') or ''}",
        f"Delivery address: {draft.get('delivery_address') or ''}",
        f"Preferred slot: {draft.get('delivery_slot') or ''}",
        f"Receiver phone: {draft.get('receiver_phone') or ''}",
    ]
    if pin:
        lines.append(f"Location pin: {pin}")
    lines.extend(["", "Please confirm availability, final delivery charges and invoice."])
    return f"https://wa.me/{number}?text={quote(chr(10).join(lines))}"
