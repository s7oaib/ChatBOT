"""Small, deterministic conversation-state resolver.

This module only resolves meaning that is already present in the current
message or in an immediately active bot question.  It never selects customer
facing copy and never invents a business fact; the responder remains the sole
authority for approved answers.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


_FILLER = {
    "a", "an", "the", "please", "pls", "yes", "yeah", "yep", "sure", "okay",
    "ok", "one", "that", "this", "it", "me", "my", "want", "would", "like",
    "send", "share", "show", "give", "can", "you", "could", "again", "same",
}

_ASSET_WORDS = re.compile(
    r"\b(chart|rate\s*card|image|photo|flyer|card|asset)\b|"
    r"\b(resend|re-?send|send|share|forward)\b.*\b(it|that|again|chart|menu|image|card)\b",
    re.I,
)
_CHART_WORDS = re.compile(r"\b(chart|rate\s*card|price\s*chart|pricing\s*chart)\b", re.I)
_MENU_WORDS = re.compile(r"\b(menu|flyer|menu\s*card)\b", re.I)
_RESEND_WORDS = re.compile(r"\b(resend|again|re-?send|send\s+(?:it|that))\b", re.I)

_SONDESH = re.compile(r"\b(sondesh|sandesh|mishti|mithai|sweet(?:s)?|sweets)\b", re.I)
_MEALS = re.compile(r"\b(bengali\s+meals?|meals?|meal|food|khana|lunch|dinner|savoury|savory|bento|combo)\b", re.I)
_SUBSCRIPTION = re.compile(r"\b(subscription|subscriptions|meal\s+plan|tiffin|everyday\s+meal)\b", re.I)
_DELIVERY = re.compile(r"\b(deliver(?:y|ies)?|courier|ship(?:ping)?)\b", re.I)
_PRICE = re.compile(r"\b(price|prices|pricing|cost|rate|how\s+much|daam|kitna)\b", re.I)
_FOLLOWUP = re.compile(
    r"^(?:and\s+)?(?:how\s+much|price|prices|cost|rate|where|when|what\s+about|"
    r"delivery|deliver(?:y)?|veg|non\s*veg|chicken|mangsho|menu|chart|again|"
    r"share\s+(?:it|that)|send\s+(?:it|that)|same|tomorrow|noida|gurgaon)\s*[?.!]*$",
    re.I,
)
_CORRECTION = re.compile(r"\b(?:not|no,?\s+i\s+meant|actually|i\s+meant)\b", re.I)


@dataclass
class ContextResolution:
    kind: Optional[str] = None
    entity: Optional[str] = None
    asset: Optional[str] = None
    corrected_text: Optional[str] = None
    confidence: float = 0.0
    options: List[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return bool(self.kind)


def _clean(text: str) -> str:
    return " ".join(str(text or "").lower().replace("’", "'").split()).strip(" .?!,")


def explicit_entity(text: str) -> Optional[str]:
    value = _clean(text)
    if re.search(r"\b(?:veg|vegetarian|niramish|non\s*veg|chicken|mangsho|fish|mutton)\b", value) and re.search(r"\bmenu\b", value):
        return "BENGALI_MEAL"
    if _SUBSCRIPTION.search(value):
        return "SUBSCRIPTION"
    if _SONDESH.search(value):
        return "SONDESH"
    if _MEALS.search(value):
        return "BENGALI_MEAL"
    if _DELIVERY.search(value):
        return "DELIVERY"
    return None


def _selection(text: str, options: List[str]) -> Optional[str]:
    value = _clean(text)
    if not options:
        return None
    # Explicit corrections are allowed to replace an earlier selection.
    if "sondesh" in value or "mishti" in value or "sweet" in value:
        return "SONDESH" if "SONDESH" in options else None
    if "signature" in value or re.search(r"\b(?:option\s*)?2\b|collection\s*2|second", value):
        if "SIGNATURE" in options:
            return "SIGNATURE"
    if "aamar" in value or re.search(r"\b(?:option\s*)?1\b|collection\s*1|first", value):
        if "AAMAR_MISHTI" in options:
            return "AAMAR_MISHTI"
    if any(word in value for word in ("meal", "meals", "food", "khana", "bengali")):
        return "BENGALI_MEAL" if "BENGALI_MEAL" in options else None
    if any(word in value for word in ("subscription", "plan", "tiffin")):
        return "SUBSCRIPTION" if "SUBSCRIPTION" in options else None
    menu = "NON_VEGETARIAN" if re.search(r"\b(non\s*veg|chicken|mangsho|fish|mutton)\b", value) else None
    if menu is None and re.search(r"\b(veg|vegetarian|niramish)\b", value):
        menu = "VEGETARIAN"
    if menu and menu in options:
        return menu
    ordinal = re.search(r"\b(?:option\s*)?(first|1|one|second|2|two|third|3|three|last)\b", value)
    if ordinal:
        word = ordinal.group(1)
        index = {"first": 0, "1": 0, "one": 0, "second": 1, "2": 1, "two": 1,
                 "third": 2, "3": 2, "three": 2, "last": len(options) - 1}.get(word)
        if index is not None and 0 <= index < len(options):
            return options[index]
    return None


def resolve(text: str, state: Dict[str, Any]) -> ContextResolution:
    """Resolve only active, unambiguous context; otherwise return unresolved."""
    value = _clean(text)
    if not value:
        return ContextResolution()

    current = explicit_entity(value)
    pending = state.get("pending_clarification")
    options = [str(item) for item in (state.get("clarification_options") or state.get("presented_options") or [])]

    # A current explicit subject outranks every stale state and source route.
    if current:
        if pending == "product_price" and current in {"SONDESH", "BENGALI_MEAL"}:
            return ContextResolution("clarification_selection", current, confidence=1.0, options=options)
        if pending == "asset_request" and current in {"SONDESH", "BENGALI_MEAL", "SUBSCRIPTION"}:
            return ContextResolution("topic_switch", current, confidence=1.0)
        if _CORRECTION.search(value) and current:
            return ContextResolution("correction", current, corrected_text=value, confidence=1.0)

    selected = _selection(value, options)
    if pending and selected:
        return ContextResolution("clarification_selection", selected, confidence=1.0, options=options)

    # Asset requests are first-class.  The active topic is used only when the
    # requested asset has one clearly relevant owner.
    menu_selection = re.search(r"\b(?:veg|vegetarian|niramish|non\s*veg|chicken|mangsho|fish|mutton)\b", value)
    if _ASSET_WORDS.search(value) and not (menu_selection and _MENU_WORDS.search(value)):
        asset = "chart" if _CHART_WORDS.search(value) else "menu" if _MENU_WORDS.search(value) else "active"
        if _MENU_WORDS.search(value) and re.search(r"\bnot\b[^.?!]*\bchart\b", value):
            asset = "menu"
        topic = state.get("active_topic") or state.get("last_topic")
        if topic or state.get("last_answer_id") or state.get("active_asset"):
            return ContextResolution("asset_request", str(topic or ""), asset=asset, confidence=0.95)
        return ContextResolution("asset_ambiguous", asset=asset, confidence=0.0)

    # Short follow-ups are eligible only with a recent topic and are never
    # borrowed from an old source route when the message names a new subject.
    topic = state.get("active_topic") or state.get("last_topic")
    if topic and (_FOLLOWUP.fullmatch(value) or len(value.split()) <= 3):
        if _PRICE.search(value):
            return ContextResolution("followup_price", str(topic), confidence=0.95)
        if _DELIVERY.search(value) or value in {"where", "when", "tomorrow"}:
            return ContextResolution("followup_delivery", str(topic), confidence=0.9)
        if value in {"veg", "vegetarian", "non veg", "non-veg", "chicken", "mangsho", "fish"}:
            return ContextResolution("followup_option", str(topic), entity=value, confidence=0.95)
        if _RESEND_WORDS.search(value):
            return ContextResolution("asset_request", str(topic), asset="active", confidence=0.9)

    return ContextResolution()


def pending_for_response(template_id: Optional[str], response: str, images: Any) -> Dict[str, Any]:
    """Derive structured next-turn state from the bot's own approved reply."""
    text = _clean(response)
    pending: Optional[str] = None
    options: List[str] = []
    clarification_intent: Optional[str] = None
    if template_id == "price_clarify_product":
        pending, options = "product_price", ["SONDESH", "BENGALI_MEAL"]
        clarification_intent = "PRICING"
    elif template_id == "context_meal_price_followup":
        pending, options = "menu_choice", ["VEGETARIAN", "NON_VEGETARIAN"]
    elif template_id == "delivery_area" and "which area" in text:
        pending, options = "delivery_area", []
    elif template_id == "subscription_details":
        pending, options = "subscription_booking", ["SUBSCRIPTION"]
    elif template_id == "sondesh_price_detailed" and re.search(r"collection|option\s*[12]", text):
        pending, options = "sondesh_collection", ["AAMAR_MISHTI", "SIGNATURE"]
    elif template_id in {"know_more", "product_overview"} and any(x in text for x in ("sondesh", "meals")):
        pending, options = "product_topic", ["SONDESH", "BENGALI_MEAL"]
    if not pending and isinstance(response, str) and "option 1" in text and "option 2" in text:
        pending, options = "product_topic", ["SONDESH", "BENGALI_MEAL"]
    return {
        "pending_clarification": pending,
        "clarification_options": options,
        "clarification_intent": clarification_intent,
    }
