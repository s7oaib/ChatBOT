"""Customer-provided Instagram order request drafts (never confirmations)."""

import hashlib
from datetime import date
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
_QUANTITY_RE = re.compile(r"\b(\d+\s*(?:box(?:es)?|packs?|pieces?|pcs|portions?|servings?|plates?)|\d+(?:\.\d+)?\s*(?:kg|grams?|gm|g))\b", re.I)
_SLOT_RE = re.compile(
    r"\b(?:(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"\s+(?:morning|afternoon|lunch|dinner)|next\s+week|"
    r"(?:morning|afternoon|lunch|dinner)\s+(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.I,
)
_AREA_RE = re.compile(r"\b(?:delhi\s+ncr|gurgaon|gurugram|delhi|noida|ghaziabad|faridabad|kolkata)\b", re.I)
_DATE_RE = re.compile(
    r"\b(?:\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}(?:st|nd|rd|th)?"
    r"|\d{1,2}[/-]\d{1,2})(?:\s+\d{4})?\b", re.I,
)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{8,}\d)(?!\w)")
_PIN_RE = re.compile(r"https?://(?:maps\.app\.goo\.gl|(?:www\.)?google\.[^/]+/maps|maps\.google\.[^/]+)[^\s]*", re.I)
_PRODUCT_RE = re.compile(
    r"\b(?:sondesh(?:\s+classic)?|jolbhora|kanchagolla|nolen\s+gur|"
    r"chicken\s+bento|bengali\s+bento|bento|chicken\s+kosha|subscription)\b",
    re.I,
)
_ORDER_REQUEST_RE = re.compile(
    r"\b(?:need|i\s+(?:want|would\s+like)|i['’]d\s+like|please\s+order)\b|"
    r"\b\d+\s+(?:box(?:es)?|packs?|pieces?|pcs)\b",
    re.I,
)
_EDIT_FIELD_RE = re.compile(
    r"\bchange\s+(?:my\s+)?(quantity|item|address|delivery area|area|slot|day|receiver phone(?: number)?|phone(?: number)?|location pin)\b",
    re.I,
)


def _calendar_date(text: str) -> str | None:
    """Validate supplied dates without inferring availability or a booking."""
    months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
    for match in _DATE_RE.finditer(text):
        value = match.group(0)
        parts = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", value, flags=re.I)
        parts = re.split(r"[\s/-]+", parts.strip())
        try:
            if parts[0].isalpha():
                month, day = months.index(parts[0][:3].lower()) + 1, int(parts[1])
            else:
                day = int(parts[0])
                month = int(parts[1]) if parts[1].isdigit() else months.index(parts[1][:3].lower()) + 1
            date(int(parts[2]) if len(parts) > 2 else 2000, month, day)
        except (ValueError, IndexError):
            continue
        return " ".join(value.split()).title()
    return None


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
    if _structured_fields(text):
        return True
    if is_order_information_question(text):
        return False
    has_quantity = bool(_QUANTITY_RE.search(text) or re.match(r"\s*\d+\s+(?=(?:chicken|bengali|sondesh|bento)\b)", text, re.I))
    context_known = str(active_product_context or "").casefold() in {
        "sondesh", "sondesh_general", "bento", "subscription",
    }
    return bool(
        (has_quantity and _extract_customer_item(text))
        or (_ORDER_REQUEST_RE.search(text) and (_extract_customer_item(text) or context_known))
        or (context_known and has_quantity and re.search(r"\b(?:i\s+want|need|make\s+it)\b", text, re.I))
    )


def is_order_information_question(message: str) -> bool:
    """Keep business/product questions out of customer-provided draft data."""
    text = str(message or "").strip()
    if is_order_process_question(text):
        return False
    return bool(
        is_price_or_payment(text) or is_unsupported_question(text)
        or re.match(r"^(?:what|which|why|when|where|how|do you|does|is|are|can you|could you|tell me|i want to (?:know|see|learn))\b", text, re.I)
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
    supplied = _structured_fields(text)
    if not supplied and is_order_information_question(text):
        return draft
    text = re.sub(r"(?m)^\s*[1-4][.)]\s*", "", text)
    edit = draft.get("edit_field")
    requested_edit = _EDIT_FIELD_RE.search(text)
    if requested_edit and not text[requested_edit.end():].strip(" ?.!"):
        target = requested_edit.group(1).casefold()
        draft["edit_field"] = {
            "area": "delivery_area", "delivery area": "delivery_area", "address": "delivery_address",
            "day": "delivery_slot", "slot": "delivery_slot",
            "phone": "receiver_phone", "phone number": "receiver_phone", "receiver phone": "receiver_phone", "receiver phone number": "receiver_phone",
        }.get(target, target)
        draft["status"] = ORDER_DRAFT_INCOMPLETE
        return draft

    quantity_match = _QUANTITY_RE.search(text)
    updated_fields = set()
    if quantity_match:
        draft["quantity"] = " + ".join(" ".join(match.group(1).split()) for match in _QUANTITY_RE.finditer(text))
        updated_fields.add("quantity")
    elif draft.get("item") and (not edit or edit == "quantity") and re.fullmatch(r"\d{1,4}(?:\.\d+)?", text):
        draft["quantity"] = text
        updated_fields.add("quantity")
    elif _ORDER_REQUEST_RE.search(text) or _PRODUCT_RE.search(text):
        count_text = re.sub(r"^\s*(?:need|i\s+want|i\s+would\s+like|i['’]d\s+like|please\s+order)\s+", "", text, flags=re.I)
        leading_count = re.match(r"\s*(\d+)\s+(?=[a-z])", count_text, re.I)
        if leading_count:
            draft["quantity"] = leading_count.group(1)
            updated_fields.add("quantity")

    product_match = _PRODUCT_RE.search(text)
    if product_match or is_order_process_question(text) or (
        _ORDER_REQUEST_RE.search(text) or quantity_match or edit == "item"
    ):
        extracted_item = _extract_customer_item(text)
        if extracted_item:
            draft["item"] = extracted_item
            updated_fields.add("item")
        elif not draft.get("item") and quantity_match:
            context_products = {
                "sondesh": "Sondesh", "sondesh_general": "Sondesh",
                "bento": "Bengali Bento", "subscription": "Subscription",
            }
            contextual_item = context_products.get(str(active_product_context or "").casefold())
            if contextual_item:
                draft["item"] = contextual_item
                updated_fields.add("item")

    slot_matches = list(_SLOT_RE.finditer(text))
    correcting = bool(re.search(r"\b(?:actually|instead|not|change|make it)\b", text, re.I))
    slot_match = slot_matches[-1] if slot_matches and (correcting or len({m[0].casefold() for m in slot_matches}) == 1) else None
    if slot_match:
        draft["delivery_slot"] = " ".join(slot_match.group(0).split()).title()
        updated_fields.add("delivery_slot")
    elif (supplied_date := _calendar_date(text)):
        draft["delivery_slot"] = supplied_date
        updated_fields.add("delivery_slot")
    elif re.fullmatch(r"morning|afternoon|lunch|dinner", text, re.I):
        previous_day = re.fullmatch(r"today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday", str(draft.get("delivery_slot") or ""), re.I)
        draft["delivery_slot"] = ((previous_day.group(0) + " " if previous_day else "") + text).title()
        updated_fields.add("delivery_slot")
    elif re.fullmatch(r"(?:make it |change (?:slot|day) to )?(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next week)(?: please)?", text, re.I):
        draft["delivery_slot"] = re.sub(r"^(?:make it |change (?:slot|day) to )", "", text, flags=re.I).removesuffix(" please").title()
        updated_fields.add("delivery_slot")

    area_matches = list(_AREA_RE.finditer(text))
    area_match = area_matches[-1] if area_matches and (correcting or len({m[0].casefold() for m in area_matches}) == 1) else None
    if area_match:
        area = " ".join(area_match.group(0).split()).title()
        old_area = str(draft.get("delivery_area") or "").casefold()
        same_area = area.casefold() == old_area or {area.casefold(), old_area} <= {"gurgaon", "gurugram"}
        if old_area and not same_area:
            draft["delivery_address"] = None
            draft["location_pin"] = None
            draft["location_pin_required"] = False
        draft["delivery_area"] = "Delhi NCR" if area.casefold() == "delhi ncr" else area
        updated_fields.add("delivery_area")

    phone_matches = list(_PHONE_RE.finditer(text))
    phone_match = phone_matches[-1] if phone_matches and (correcting or len({re.sub(r"\D", "", m[0]) for m in phone_matches}) == 1) else None
    if phone_match and 10 <= len(re.sub(r"\D", "", phone_match.group(0))) <= 15:
        draft["receiver_phone"] = re.sub(r"[\s().-]", "", phone_match.group(0))
        updated_fields.add("receiver_phone")

    pin_match = _PIN_RE.search(text)
    if pin_match:
        draft["location_pin"] = pin_match.group(0).rstrip(".,)")
        updated_fields.add("location_pin")

    if not supplied and _looks_like_address(text, area_match, phone_match, slot_match, quantity_match, product_match):
        address_text = _extract_address_text(text)
        if address_text:
            if address_text != draft.get("delivery_address") and not pin_match:
                draft["location_pin"] = None
            draft["delivery_address"] = address_text
            updated_fields.add("delivery_address")
        draft["location_pin_required"] = bool(re.search(r"\b(?:standalone|independent)\s+house\b", text, re.I))
        if not draft["location_pin_required"]:
            draft["location_pin"] = None

    for field, value in supplied.items():
        if field == "item":
            item_value = _QUANTITY_RE.sub("", value).strip(" ,-")
            if not item_value:
                continue
            draft[field] = item_value
        elif field == "delivery_address":
            if _looks_like_address(value, _AREA_RE.search(value), None, None, None, None):
                draft[field] = value
            else:
                draft["delivery_area"] = value
        else:
            draft[field] = value
        updated_fields.add(field)

    if edit:
        field = {"area": "delivery_area", "day": "delivery_slot", "slot": "delivery_slot", "phone": "receiver_phone", "phone number": "receiver_phone", "receiver phone": "receiver_phone", "receiver phone number": "receiver_phone", "location pin": "location_pin"}.get(edit, edit)
        if field == "delivery_address" and "delivery_area" in updated_fields and "delivery_address" not in updated_fields:
            draft["delivery_address"] = None
            updated_fields.add("delivery_address")
        if field in updated_fields:
            draft["edit_field"] = None

    draft["status"] = ORDER_DRAFT_READY if not missing_fields(draft) else ORDER_DRAFT_INCOMPLETE
    return draft


def _structured_fields(text: str) -> dict:
    """Read explicit numbered/labelled customer fields without inventing values."""
    fields = {}
    labels = {
        "item": "item", "items": "item", "item & quantity": "item", "items & quantity": "item",
        "quantity": "quantity", "qty": "quantity",
        "address": "delivery_address", "delivery address": "delivery_address",
        "delivery address/area": "delivery_address", "area": "delivery_area", "delivery area": "delivery_area",
        "phone": "receiver_phone", "receiver phone": "receiver_phone", "phone number": "receiver_phone", "receiver phone number": "receiver_phone",
        "slot": "delivery_slot", "preferred slot": "delivery_slot", "day": "delivery_slot", "preferred day": "delivery_slot", "preferred day/slot": "delivery_slot",
    }
    numbered = {"1": "item", "2": "delivery_address", "3": "receiver_phone", "4": "delivery_slot"}
    for line in re.split(r"\n|;\s*", text):
        number = re.match(r"^\s*([1-4])[.)]\s*(.+)$", line)
        if number:
            field, value = numbered[number[1]], number[2].strip()
        else:
            label = re.match(r"^\s*([^:]+):\s*(.+)$", line)
            if not label or label[1].strip().casefold() not in labels:
                continue
            field, value = labels[label[1].strip().casefold()], label[2].strip()
        # A numbered line may also carry a label.
        prefix = re.match(r"^([^:]+):\s*(.+)$", value)
        if prefix and prefix[1].strip().casefold() in labels:
            value = prefix[2].strip()
        if value.casefold() not in {"not provided", "n/a", "none"}:
            fields[field] = value
    return fields


def _extract_customer_item(text: str) -> str | None:
    candidate = str(text or "")
    process_prefix = _HOW_TO_ORDER_RE.match(candidate.strip())
    if process_prefix:
        candidate = candidate.strip()[process_prefix.end():]
    candidate = re.sub(
        r"^\s*how\s+(?:do\s+i|can\s+i|to)\s+(?:place\s+)?(?:an?\s+)?order\s*",
        "", candidate, flags=re.I,
    )
    candidate = re.sub(r"^\s*(?:need|i\s+want|i\s+would\s+like|i['’]d\s+like|please\s+order)\s+(?:to\s+order\s+)?", "", candidate, flags=re.I)
    candidate = _QUANTITY_RE.sub(" ", candidate)
    candidate = _SLOT_RE.sub(" ", candidate)
    candidate = _DATE_RE.sub(" ", candidate)
    candidate = _PHONE_RE.sub(" ", candidate)
    candidate = _PIN_RE.sub(" ", candidate)
    # A volunteered address is a separate field, not part of an item name.
    candidate = re.split(r"\b(?:flat|apartment|tower|sector|phase|block|floor|road|street|lane|dlf|standalone house|independent house)\b", candidate, maxsplit=1, flags=re.I)[0]
    candidate = _AREA_RE.sub(" ", candidate)
    candidate = re.sub(r"\b(?:for|in|to|at|of|delivery|ph|phone)\b", " ", candidate, flags=re.I)
    candidate = re.sub(r"^\s*\d+\s+(?=[a-z])", "", candidate, flags=re.I)
    candidate = re.sub(r"\s+", " ", candidate).strip(" ,.-?!")
    if not candidate and _QUANTITY_RE.search(text) and (known_in_message := _PRODUCT_RE.search(text)):
        return known_in_message[0].title()
    if (
        not candidate
        or candidate.casefold() in {"a", "an", "order", "place an order", "place order", "next week"}
        or (process_prefix and re.fullmatch(r"(?:from\s+)?(?:(?:a|the|your)\s+)?menu", candidate, re.I))
        or re.match(r"^(?:know|learn|see|ask|talk|speak|information|more|actually|make it|change|not|use)\b", candidate, re.I)
    ):
        return None
    known = _PRODUCT_RE.fullmatch(candidate)
    return candidate.title() if known else candidate


def _looks_like_address(text, area_match, phone_match, slot_match, quantity_match, product_match) -> bool:
    candidate = _extract_address_text(text)
    if not candidate or re.fullmatch(r"\d+", candidate):
        return False
    if re.search(r"\b(?:flat|apartment|tower|sector|phase|block|floor|road|street|lane|standalone\s+house|independent\s+house|dlf)\b", candidate, re.I):
        return True
    return bool(area_match and "," in candidate)


def _extract_address_text(text: str) -> str:
    candidate = str(text or "")
    address_start = re.search(r"\b(?:flat|apartment|tower|sector|phase|block|floor|road|street|lane|dlf|standalone house|independent house)\b", candidate, re.I)
    if address_start:
        candidate = candidate[address_start.start():]
        boundaries = [match.start() for pattern in (_QUANTITY_RE, _SLOT_RE, _PHONE_RE, _PIN_RE) if (match := pattern.search(candidate))]
        if boundaries:
            candidate = candidate[:min(boundaries)]
    preserve_area = bool(re.search(
        r"\b(?:flat|apartment|tower|sector|phase|block|floor|road|street|lane|standalone\s+house|independent\s+house|dlf)\b",
        candidate, re.I,
    ))
    candidate = _PHONE_RE.sub(" ", candidate)
    candidate = _PIN_RE.sub(" ", candidate)
    candidate = _SLOT_RE.sub(" ", candidate)
    candidate = _QUANTITY_RE.sub(" ", candidate)
    if not address_start:
        candidate = _PRODUCT_RE.sub(" ", candidate)
    if not preserve_area:
        candidate = _AREA_RE.sub(" ", candidate)
    candidate = re.sub(r"\b(?:need|i\s+want|i\s+would\s+like|please|order|for|in|to|at|delivery|address|is|change|actually|instead|ph|phone)\b", " ", candidate, flags=re.I)
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


def has_meaningful_order_details(draft: dict) -> bool:
    """A named item is enough for a draft; a bare referential pronoun is not."""
    item = str(draft.get("item") or "").strip().casefold()
    return bool(item and item not in {"this", "that", "it", "one", "something"})


def instruction_message() -> str:
    return (
        "To place a pre-order, Kindly call / WhatsApp our kitchen at 8796551990 with following details\n\n"
        "1. Your choice of items & the quantity\n"
        "2. delivery address so that I can tell u Porter charges,\n"
        "3. phone number of person who will receive the package\n"
        "4. which slot - example : Friday / Saturday / Sunday - lunch or dinner or any day next week"
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
    item_quantity = " ".join((draft.get(field) or "Not provided") for field in ("quantity", "item"))
    if not draft.get("quantity") and not draft.get("item"):
        item_quantity = "Not provided"
    address = draft.get("delivery_address") or draft.get("delivery_area") or "Not provided"
    summary = (
        "ORDER DRAFT\n\n"
        f"1. Items & quantity: {item_quantity}\n"
        f"2. Delivery address/area: {address}\n"
        f"3. Receiver phone: {draft.get('receiver_phone') or 'Not provided'}\n"
        f"4. Preferred slot: {draft.get('delivery_slot') or 'Not provided'}"
    )
    if draft.get("location_pin"):
        summary += f"\nLocation pin: {draft['location_pin']}"
    return summary


def whatsapp_draft_link(draft: dict, *, reference: str | None = None) -> str:
    number = re.sub(r"\D", "", config.WHATSAPP_BUSINESS_NUMBER or "") or "918796551990"
    lines = [
        "Hi, I'd like to place a pre-order with Camellia Artisanal.",
        "",
        ((f"Camellia reference: {reference}\n\n" if reference else "") + request_summary(draft)),
        "",
        "Please help me complete any missing details and confirm this order.",
    ]
    return f"https://wa.me/{number}?text={quote(chr(10).join(lines))}"



def draft_fingerprint(draft: dict) -> str:
    """Hash only the normalized customer-visible fields, never internal state."""
    visible = " ".join(request_summary(draft).casefold().split())
    return hashlib.sha256(visible.encode("utf-8")).hexdigest()


def is_link_resend_request(message: str) -> bool:
    text = " ".join(str(message or "").casefold().split()).strip(" .?!")
    return bool(re.fullmatch(
        r"(?:send (?:the )?whatsapp link(?: again)?|where is (?:the )?whatsapp link|"
        r"whatsapp link please|send (?:the )?link(?: again)?|how do i continue on whatsapp|"
        r"(?:show|resend) (?:me )?(?:my|the) order(?: draft)?)", text,
    ))


def is_new_order_request(message: str) -> bool:
    text = " ".join(str(message or "").casefold().split()).strip(" .?!")
    return bool(re.fullmatch(
        r"(?:start (?:a )?new order|i want to place another order|new order please|i need a separate order)", text,
    ))


def is_whatsapp_referral_ack(message: str) -> bool:
    """Require an explicit past-tense send; weak acknowledgements are not proof."""
    text = " ".join(str(message or "").casefold().split()).strip(" .?!")
    return bool(re.fullmatch(
        r"(?:done[, ]*)?(?:i(?:['’]ve| have)?\s+)?(?:sent|messaged)\s+"
        r"(?:(?:it|(?:the |my )?order|you|your manager|the manager)\s+)?"
        r"(?:on|to|via)\s+whatsapp|"
        r"i(?:['’]ve| have)? sent (?:the |my )?order to (?:the |your )?manager", text,
    ))


def is_order_detail_message(message: str) -> bool:
    """Detect updates to a referred order without borrowing its snapshot."""
    if is_order_information_question(message):
        return False
    text = str(message or "")
    return bool(
        is_order_process_question(text) or is_order_request(text) or is_edit_request(text)
        or _EDIT_FIELD_RE.search(text) or _QUANTITY_RE.search(text) or _SLOT_RE.search(text) or _calendar_date(text)
        or _AREA_RE.search(text) or _PHONE_RE.search(text) or _PIN_RE.search(text)
        or _looks_like_address(text, _AREA_RE.search(text), None, None, None, None)
        or re.fullmatch(r"(?:make it |actually )?\d{1,4}|(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|lunch|dinner)", text.strip(), re.I)
    )


def present_draft(facts: dict, draft: dict, message: str) -> str:
    fingerprint = draft_fingerprint(draft)
    force = is_link_resend_request(message) or is_order_process_question(message) or is_new_order_request(message)
    if not force and facts.get("order_intake_last_presented_fingerprint") == fingerprint and facts.get("order_intake_whatsapp_link_sent_for_fingerprint") == fingerprint:
        return "Your order draft is unchanged. The details are already noted."
    prefix = instruction_message() + "\n\n" if is_order_process_question(message) or is_new_order_request(message) else ""
    response = (
        prefix + "I've prepared your order draft. Please continue on WhatsApp "
        "to send it to our team. Missing details are marked Not provided, "
        "and our manager can help complete them.\n\n"
        + request_summary(draft)
        + "\n\nOur manager will review availability, delivery charges, "
        "and the final invoice.\n\n[Edit Order]\nReply with the change you want."
        + f"\n\n[Continue on WhatsApp]\n{whatsapp_draft_link(draft)}"
    )
    facts["order_intake_last_presented_fingerprint"] = fingerprint
    facts["order_intake_whatsapp_link_sent_for_fingerprint"] = fingerprint
    return response
