"""Cheap whole-turn ownership checks; these rules never supply business facts."""
import re
from dataclasses import dataclass


_COLLABORATION_PROPOSAL_RE = re.compile(
    r"\b(?:collab(?:oration|orate|orating|orative)?|"
    r"partnership|partner(?:ing)?\s+with\s+(?:you|camellia)|work(?:ing)?\s+together|"
    r"brand\s+(?:collab(?:oration)?|partnership)|"
    r"feature(?:d|ing)?\s+(?:some\s+of\s+)?(?:my|our)\s+products?|"
    r"stock(?:ing)?\s+(?:some\s+of\s+)?(?:my|our)\s+products?|"
    r"carry(?:ing)?\s+(?:some\s+of\s+)?(?:my|our)\s+products?|supplier\s+proposal|vendor\s+proposal)\b", re.I,
)
SUBSCRIPTION_CHANGE_RE = re.compile(r"\b(change|changing|reschedul\w*|postpone|prepone|shift|skip|pause|cancel)\b", re.I)
CONFIRMED_BENTO_SUBSTITUTION_RE = re.compile(
    r"\bchholar\s+daa?l\b(?=.*\b(bento|aloo\s+dom)\b)|\b(bento|aloo\s+dom)\b(?=.*\bchholar\s+daa?l\b)", re.I,
)
_SUBSCRIPTION_RE = re.compile(r"\b(?:subscriptions?|meal\s+plan|everyday\s+meal)\b", re.I)
_SEATING_RE = re.compile(r"\b(?:places?|seats?|space|room|slots?|table\s+for|people|guests|pax)\b", re.I)
_REQUEST_RE = re.compile(r"\b(?:can|could|would|do|does|is|are|any|please|availability|table\s+for)\b", re.I)
_PRODUCT_RE = re.compile(r"\b(?:bento|chicken\s+kosha|home\s+table|sondesh)\b", re.I)


def normalize(message):
    return " ".join(re.findall(r"\w+", (message or "").casefold()))


def is_collaboration_proposal(message):
    return bool(_COLLABORATION_PROPOSAL_RE.search(message or ""))


def is_payment_method_request(message):
    text = normalize(message)
    return bool(re.search(
        r"\b(?:how\s+(?:(?:do|can)\s+i\s+|to\s+|2\s+)(?:make\s+(?:the\s+)?(?:a\s+)?payment|pay)|"
        r"can\s+i\s+pay|(?:what|which)\b.{0,35}\bpayment\s+(?:methods?|options?)|"
        r"(?:share|send|give)\b.{0,20}\bpayment\s+(?:details|methods?|options?)|"
        r"(?:do|can)\s+you\s+accept\b.{0,20}\b(?:upi|bank\s+transfer|cards?|cash))\b", text)
        or re.fullmatch(r"(?:pay|payment|upi|bank transfer|qr(?: code)?|gpay|paytm|phonepe|net banking|credit card|debit card|cash on delivery|cod)(?: please)?", text)
        or re.fullmatch(r"(?:(?:home table|subscription|bento|sondesh) (?:upi )?)?payment(?: methods?| options?| details)?(?: please)?", text)
    )


def has_price_intent(message):
    text = normalize(message)
    return bool(re.search(r"\b(?:prices?|pricing|pirce|cost|rates?|how much)\b", text)
                and not re.search(r"\bhow much time\b", text))


def is_details_request(message):
    text = normalize(message)
    return bool(re.search(r"\b(?:tell|explain|describe|details?|what is|what are|what do|whats|options?|know more|more about)\b", text)
                or re.search(r"\bwhat\b.{0,50}\bdo you (?:offer|provide|have)\b", text)
                or text in {"bento", "bengali bento", "subscription", "meal plan", "sondesh", "sandesh", "home table"})


def is_menu_browse_request(message):
    text = normalize(message)
    return bool(re.search(r"\bmenus?\b", text) and (
        re.search(r"(?:^|\bplease\s+|\b(?:can|could|would|will) you\s+|\b(?:hi|hello|hey)\s+)(?:show|send|share|give|bhejo|dikhao)\b", text)
        or re.search(r"\b(?:can|could|may) (?:i|we) (?:see|have|get)\b", text)
        or re.search(r"\b(?:i|we) (?:want|need|would like)\b", text)
        or re.search(r"\b(?:whats|what is|what s|what menu|which menu|what food|tell me)\b", text)
        or re.fullmatch(r"(?:(?:the|your|current|weekly|savoury|savory|veg|vegetarian|non veg|non vegetarian|this week s)\s+)*menus?(?: for (?:this|current) week)?(?: please| pls)?", text)
    ))


@dataclass(frozen=True)
class TurnAuthority:
    reason: str = ""
    primary_intent: str = ""

    @property
    def blocks_shortcut(self):
        return bool(self.reason)


def evaluate_turn_authority(message):
    """Detect requests a borrowed keyword cannot answer, before any shortcut.

    A block means seek exact approved coverage or hand off, never that a topic
    is permanently forbidden. Bare business nouns are deliberately insufficient.
    """
    text = normalize(message)
    if is_collaboration_proposal(message):
        return TurnAuthority("higher_priority_collaboration", "collaboration_proposal")
    if _REQUEST_RE.search(text):
        seating = _SEATING_RE.search(text) and re.search(r"\b(?:free|available|availability|reserve|reservation|hold|room|space|slots?)\b", text)
        product = _PRODUCT_RE.search(text) and re.search(r"\b(?:available|availability|in stock|reserve|reservation|hold)\b", text)
        availability = re.search(r"\bavailability\b", text) and re.search(r"\b(?:lunch|dinner|confirm|sunday|weekend)\b", text)
        meal = re.search(r"\b(?:lunch|dinner)\b", text) and (
            re.search(r"\b(?:book|reserve|hold)\b", text)
            or (re.search(r"\b(?:have|available|free|availability)\b", text)
                and re.search(r"\b(?:today|tomorrow|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", text))
        )
        if seating or product or availability or (meal and not is_menu_browse_request(message)):
            return TurnAuthority("unsupported_live_availability", "live_availability")
    if SUBSCRIPTION_CHANGE_RE.search(text) and (
        _SUBSCRIPTION_RE.search(text) or re.search(r"\b(?:skip|pause)\s+(?:next|this)\s+week\b", text)
    ):
        return TurnAuthority("subscription_action_requires_authority", "subscription_change")
    # Reuse the existing parser's amenity vocabulary and clause interpretation.
    from camellia import semantic_fallback
    if semantic_fallback._AMENITY_OVERRIDE_RE.search(text):
        return TurnAuthority("unsupported_business_request", "unknown_business_query")
    if re.search(r"\b(?:cater|catering|customi[sz]e)\b", text) and _REQUEST_RE.search(text):
        return TurnAuthority("unsupported_operational_request", "business_proposal")
    if re.search(r"\band\b.*\b(?:do|does|can|could|is|are|what|how|why|when)\b", text):
        reading = semantic_fallback.read(message)
        if reading["unknown_topics"] or "event_order" in reading["intents"]:
            return TurnAuthority("compound_unresolved_intent", "unknown_business_query")
        if len(set(reading["intents"]) - {"greeting", "unknown"}) > 1:
            return TurnAuthority("compound_requires_complete_authority", "compound_request")
    return TurnAuthority()


def can_candidate_own_turn(message, candidate_id):
    if evaluate_turn_authority(message).blocks_shortcut:
        return False
    if candidate_id in {"general_payment_client", "home_table_payment"}:
        return is_payment_method_request(message)
    if candidate_id in {"chicken_kosha_price_client", "bento_price_client", "subscription_price", "home_table_price"}:
        return has_price_intent(message)
    if candidate_id in {"bento_details_client", "subscription_details_client", "home_table_details"}:
        if candidate_id == "bento_details_client" and CONFIRMED_BENTO_SUBSTITUTION_RE.search(message):
            return True
        if candidate_id == "subscription_details_client" and re.fullmatch(r"(?:subscription|meal plan) (?:delivery|shipping) time", normalize(message)):
            return True
        return is_details_request(message) and not is_payment_method_request(message) and not has_price_intent(message)
    if candidate_id == "approved_menu":
        return is_menu_browse_request(message)
    return True


def exact_custom_coverage(message, row):
    """A substring/token-overlap match cannot authorize an unresolved whole turn."""
    return bool(row and row.get("source") == "client_admin" and row.get("status", "ACTIVE") == "ACTIVE"
                and normalize(message) in {normalize(value) for value in [row.get("question_pattern", ""), *(row.get("aliases") or [])]})
