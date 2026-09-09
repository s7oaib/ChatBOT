"""Structured language understanding without authority to answer or act."""

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from camellia.config import config
from camellia.monitoring import get_logger

log = get_logger("camellia.understanding")


class Intent(str, Enum):
    GREETING = "greeting"
    MENU_REQUEST = "menu_request"
    PRODUCT_INFORMATION = "product_information"
    PRODUCT_PRICE = "product_price"
    DELIVERY_QUERY = "delivery_query"
    BULK_ORDER = "bulk_order"
    EVENT_ORDER = "event_order"
    STORAGE_QUESTION = "storage_question"
    ORDER_PROCESS = "order_process"
    INGREDIENTS_QUESTION = "ingredients_question"
    ALLERGEN_QUESTION = "allergen_question"
    PACKAGING_QUESTION = "packaging_question"
    PAYMENT_QUESTION = "payment_question"
    CONTACT_QUESTION = "contact_question"
    AVAILABILITY_QUERY = "availability_query"
    DISCOUNT_REQUEST = "discount_request"
    PACK_SIZE_QUESTION = "pack_size_question"
    DELIVERY_TIME_QUESTION = "delivery_time_question"
    SUBSCRIPTION_QUERY = "subscription_query"
    SUBSCRIPTION_BOOKING = "subscription_booking"
    SUBSCRIPTION_CHANGE = "subscription_change"
    ESCALATION = "escalation"
    UNKNOWN = "unknown"


class BusinessAction(str, Enum):
    SEND_RESPONSE = "SEND_RESPONSE"
    SEND_MENU = "SEND_MENU"
    SEND_PRODUCT_IMAGE = "SEND_PRODUCT_IMAGE"
    ASK_DELIVERY_AREA = "ASK_DELIVERY_AREA"
    CONFIRM_DELIVERY_AREA = "CONFIRM_DELIVERY_AREA"
    SEND_PRICE_SEQUENCE = "SEND_PRICE_SEQUENCE"
    ESCALATE_TO_TEAM = "ESCALATE_TO_TEAM"


@dataclass
class UnderstandingResult:
    intents: List[str] = field(default_factory=list)
    entities: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    requires_human: bool = False
    knowledge_gap: bool = False
    available: bool = False
    reason: str = "not_run"


def _json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1).strip() if fenced else text.strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(candidate[start:end + 1])
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


class UnderstandingEngine:
    """Uses Groq for meaning only. Returned prose never reaches customers."""

    _entity_keys = {
        "product", "location", "quantity", "event", "menu_type", "topic", "pack_size",
    }

    @staticmethod
    def should_analyze(user_message: str) -> bool:
        """Every substantive request uses NLU; pure social turns stay deterministic."""
        text = " ".join((user_message or "").lower().split()).strip(" .?!,")
        if not text:
            return False
        return not bool(re.fullmatch(
            r"(?:hi+|hello+|hey+|namaste|nomoshkar|good (?:morning|afternoon|evening)|"
            r"thanks|thank you|ok(?:ay)?|bye|goodbye|good night)(?: there)?",
            text,
        ))

    @staticmethod
    def deterministic_fallback(user_message: str) -> UnderstandingResult:
        """Conservative continuity when Groq is unavailable or rate-limited."""
        text = user_message.lower()
        intents: List[str] = []
        if re.search(r"\b(menu|what can i order|what do you have)\b", text):
            intents.append(Intent.MENU_REQUEST.value)
        elif re.search(r"\b(show|send|share)\b.*\b(products?|sweets?|mishti|mithai)\b", text):
            intents.append(Intent.MENU_REQUEST.value)
        if re.search(
            r"\b(price|cost|rate|how much|kitna|kitne|kitne ka|kitne paise|daam|"
            r"what does it cost)\b",
            text,
        ):
            intents.append(Intent.PRODUCT_PRICE.value)
        elif re.search(r"\b(jolbhora|sondesh|sweet|sweets|mishti|mithai|aamar mishti)\b", text):
            intents.append(Intent.PRODUCT_INFORMATION.value)
        elif re.search(
            r"\b(products?|items?)\b.*\b(available|offer|have)\b|\bwhat products?\b|"
            r"\b(sell|selling|offer|make|makes|prepare|speciality|specialty)\b[^.?!]*"
            r"\b(sweets?|mishti|mithai|sondesh|items?|products?)\b|"
            r"\b(sweets?|mishti|mithai)\b[^.?!]*\b(make|makes|prepare|sell|offer|speciality)\b",
            text,
        ):
            intents.append(Intent.PRODUCT_INFORMATION.value)
        if re.search(r"\b(deliver|delivery|courier|ship)\b", text):
            intents.append(Intent.DELIVERY_QUERY.value)
        if re.search(r"\b(event|wedding|party|function)\b", text):
            intents.append(Intent.EVENT_ORDER.value)
        elif re.search(r"\b(bulk|corporate|festive)\b", text):
            intents.append(Intent.BULK_ORDER.value)
        if re.search(
            r"\b(discount|discounted|percent off|% off|coupon|promo|"
            r"free delivery|complimentary delivery|free shipping)\b",
            text,
        ):
            intents.append(Intent.DISCOUNT_REQUEST.value)
        if re.search(
            r"\b(storage|store|storing|keep|keeping|shelf life|refrigerat\w*|"
            r"fridge|freezer|expiry)\b",
            text,
        ):
            intents.append(Intent.STORAGE_QUESTION.value)
        if re.search(
            r"\b(how (?:do|can|would|should) i (?:place an |make an )?order|"
            r"how to (?:place an |make an )?order|ordering process|"
            r"procedure for (?:placing|making) an order|"
            r"(?:go about|walk me through) (?:buying|ordering|placing)|"
            r"how (?:do|can) i (?:buy|purchase)|buying from you)\b",
            text,
        ):
            intents = [value for value in intents if value != Intent.MENU_REQUEST.value]
            intents.append(Intent.ORDER_PROCESS.value)
        if re.search(r"\b(ingredient|ingredients|made of|made from|contain|contains)\b", text):
            intents.append(Intent.INGREDIENTS_QUESTION.value)
        if re.search(r"\b(allergen|allergy|allergic|almonds?|peanuts?|cashews?|pistachios?|gluten|nut|nuts)\b", text):
            intents = [value for value in intents if value != Intent.INGREDIENTS_QUESTION.value]
            intents.append(Intent.ALLERGEN_QUESTION.value)
        if re.search(
            # "boxes" alone is a quantity noun ("50 boxes for a wedding"), so
            # it only reads as packaging next to a packaging property.
            r"\b(packaging|packed|packing|package|food-safe|plastic-free|"
            r"cornstarch|eco.?friendly|environmentally friendly|gift wrap\w*)\b",
            text,
        ):
            intents.append(Intent.PACKAGING_QUESTION.value)
        if re.search(r"\b(subscription|subscriptions|supscription|meal plan|everyday meal|meal subscription)\b", text):
            subscription_intent = Intent.SUBSCRIPTION_QUERY.value
            if re.search(r"\b(change|reschedul\w*|postpone|prepone|shift|move|skip)\b", text):
                subscription_intent = Intent.SUBSCRIPTION_CHANGE.value
            elif re.search(r"\b(book|booking|subscribe|sign me up|start|next steps?)\b", text):
                subscription_intent = Intent.SUBSCRIPTION_BOOKING.value
            intents = [
                value for value in intents
                if value not in (Intent.MENU_REQUEST.value, Intent.PRODUCT_INFORMATION.value)
            ]
            intents.append(subscription_intent)
        if re.search(r"\b(250\s?gm?s?|500\s?gm?s?|1\s?kgs?|one kg)\b", text) and re.search(
            r"\b(how many|pieces|pcs|box|pack|quantity|contains?)\b", text,
        ):
            intents = [value for value in intents if value != Intent.PRODUCT_INFORMATION.value]
            intents.append(Intent.PACK_SIZE_QUESTION.value)
        if re.search(r"\b(payment|pay|upi|cod|cash on delivery|gpay|paytm|phonepe)\b", text):
            intents.append(Intent.PAYMENT_QUESTION.value)
            if re.search(r"\b(cod|cash on delivery)\b", text):
                intents = [value for value in intents if value != Intent.DELIVERY_QUERY.value]
        # Naming a person is not the same as asking how to reach one: "can I
        # hire your chef for an evening" is a request the business has no
        # approved answer for, and handing back the manager's contact card
        # answers a question the customer did not ask.
        if re.search(r"\b(contact|phone number|email|speak to|talk to|get in touch)\b", text) or (
            re.search(r"\b(manager|owner|founder|chef|cook)\b", text)
            and re.search(r"\b(who|name|contact|number|phone|email|reach)\b", text)
        ):
            intents.append(Intent.CONTACT_QUESTION.value)
        if re.search(r"\b(in stock|out of stock|available today|availability)\b", text):
            intents.append(Intent.AVAILABILITY_QUERY.value)

        entities: Dict[str, Any] = {}
        for product in ("jolbhora", "aamar mishti", "kanchagolla", "nolen gur", "mahua", "bento", "sondesh"):
            if product in text:
                entities["product"] = product.title()
                break
        location = re.search(
            r"\b(?:deliver(?:y)?\s+(?:to|in)|ship\s+to|courier\s+to)\s+([a-z][a-z ]{1,40}?)(?:[?.!,]|\s+and\b|$)",
            text,
        )
        if location:
            entities["location"] = " ".join(location.group(1).split()).title()
        quantity = re.search(r"\b(\d+)\s*(?:boxes|box|packs|pack|pieces|pcs)\b", text)
        if quantity:
            entities["quantity"] = int(quantity.group(1))
        if re.search(r"\b(non[ -]?veg|non vegetarian)\b", text):
            entities["menu_type"] = "non_vegetarian"
        elif re.search(r"\b(veg|vegetarian)\b", text):
            entities["menu_type"] = "vegetarian"
        if re.search(r"\b(event|wedding|party|function)\b", text):
            entities["event"] = next(
                word for word in ("wedding", "event", "party", "function") if word in text
            )
        pack_size = re.search(r"\b(250\s?gm?s?|500\s?gm?s?|1\s?kgs?)\b", text)
        if pack_size:
            entities["pack_size"] = pack_size.group(1).replace(" ", "")
        if "valet" in text or re.search(r"\bparking\b", text):
            entities["topic"] = "valet parking" if "valet" in text else "parking"

        if (
            Intent.MENU_REQUEST.value in intents
            and Intent.PRODUCT_INFORMATION.value in intents
            and re.search(r"\b(sweets?|mishti|mithai)\b", text)
        ):
            intents = [value for value in intents if value != Intent.MENU_REQUEST.value]

        # A named product is an entity inside these questions, not a second
        # request for a product overview.
        if any(value in intents for value in (
            Intent.STORAGE_QUESTION.value,
            Intent.INGREDIENTS_QUESTION.value,
            Intent.ALLERGEN_QUESTION.value,
            Intent.PACKAGING_QUESTION.value,
            Intent.PAYMENT_QUESTION.value,
        )):
            intents = [value for value in intents if value != Intent.PRODUCT_INFORMATION.value]

        if not intents:
            intents = [Intent.UNKNOWN.value]
        return UnderstandingResult(
            intents=list(dict.fromkeys(intents)),
            entities=entities,
            confidence=0.75,
            requires_human=any(i in intents for i in (Intent.EVENT_ORDER.value, Intent.BULK_ORDER.value)),
            knowledge_gap=Intent.UNKNOWN.value in intents,
            available=True,
            reason="deterministic_fallback",
        )

    @staticmethod
    def _another_model_could_survive(reason: str) -> bool:
        """Only capacity and provider-side errors are worth a second model.

        A malformed body or a schema violation is about the answer, not the
        model's availability, and repeating the call would be a retry loop
        wearing a different name.
        """
        return (
            reason in {"groq_no_choices"}
            or reason.startswith("groq_exception_")
            or (
                reason.startswith("groq_http_")
                and reason.split("_")[-1] in {"402", "413", "429", "500", "502", "503", "504"}
            )
        )

    def analyze(
        self,
        user_message: str,
        history_turns: Optional[List[Dict[str, Any]]] = None,
        state: Optional[Dict[str, Any]] = None,
    ) -> UnderstandingResult:
        if not config.STRUCTURED_NLU_ENABLED or not user_message.strip():
            return UnderstandingResult(reason="disabled_or_empty")

        from camellia.ai_responder import ai_responder

        if ai_responder.get_active_provider() == "none":
            return UnderstandingResult(reason="provider_unavailable")

        allowed = ", ".join(intent.value for intent in Intent)
        recent = "\n".join(
            f"{turn.get('role', '')}: {' '.join(str(turn.get('content', '')).split())[:180]}"
            for turn in (history_turns or [])[-4:]
        )
        active_state = {
            key: value for key, value in (state or {}).items()
            if key.startswith("awaiting_") and value
        }
        prompt = (
            "Classify one Camellia Artisanal customer message. Understand meaning only; "
            "do not answer it and do not supply business facts.\n"
            f"Allowed intents: {allowed}.\n"
            "Return every meaningful intent. Drop greeting when another intent exists. "
            "Use unknown only when no listed intent fits. Extract only text stated by the customer.\n"
            "When the intent is unknown, name the subject of the question in topic "
            "(for example \"washrooms\", \"wheelchair access\", \"pet policy\").\n"
            "Return exactly one JSON object with this schema:\n"
            '{"intents":["menu_request"],"entities":{"product":null,"location":null,'
            '"quantity":null,"event":null,"menu_type":null,"pack_size":null,"topic":null},"confidence":0.0,'
            '"requires_human":false,"knowledge_gap":false}\n'
            f"ACTIVE STATE: {json.dumps(active_state, ensure_ascii=False)}\n"
            f"RECENT CONVERSATION:\n{recent or '(none)'}\n"
            f"CUSTOMER MESSAGE: {user_message}"
        )
        raw, ok, reason = ai_responder._query_groq(
            prompt,
            is_refinement=True,
            model=config.GROQ_ROUTER_MODEL,
            response_format={"type": "json_object"},
            max_tokens=300,
        )
        if not ok and self._another_model_could_survive(reason):
            fallback_model = config.STRUCTURED_NLU_FALLBACK_MODEL
            if fallback_model and fallback_model != config.GROQ_ROUTER_MODEL:
                log.info(
                    "Understanding model %s returned %s; trying the configured "
                    "fallback model %s once.",
                    config.GROQ_ROUTER_MODEL, reason, fallback_model,
                )
                raw, ok, fallback_reason = ai_responder._query_groq(
                    prompt,
                    is_refinement=True,
                    model=fallback_model,
                    response_format={"type": "json_object"},
                    max_tokens=300,
                )
                reason = f"{reason}>{fallback_reason}" if not ok else "ok_fallback_model"
        if not ok:
            return UnderstandingResult(reason=reason)
        parsed = _json_object(raw)
        if not parsed:
            return UnderstandingResult(reason="invalid_json")
        if (
            not isinstance(parsed.get("intents"), list)
            or not isinstance(parsed.get("entities"), dict)
            or isinstance(parsed.get("confidence"), bool)
            or not isinstance(parsed.get("confidence"), (int, float))
            or not isinstance(parsed.get("requires_human"), bool)
            or not isinstance(parsed.get("knowledge_gap"), bool)
        ):
            return UnderstandingResult(reason="invalid_schema")

        valid_intents: List[str] = []
        allowed_intents = {intent.value for intent in Intent}
        for value in parsed.get("intents") or []:
            normalized = str(value).strip().lower()
            if normalized in allowed_intents and normalized not in valid_intents:
                valid_intents.append(normalized)
        if len(valid_intents) > 1 and Intent.GREETING.value in valid_intents:
            valid_intents.remove(Intent.GREETING.value)
        if len(valid_intents) > 1 and Intent.UNKNOWN.value in valid_intents:
            valid_intents.remove(Intent.UNKNOWN.value)
        if any(value in valid_intents for value in (
            Intent.STORAGE_QUESTION.value,
            Intent.INGREDIENTS_QUESTION.value,
            Intent.ALLERGEN_QUESTION.value,
            Intent.PACKAGING_QUESTION.value,
            Intent.PAYMENT_QUESTION.value,
        )):
            valid_intents = [
                value for value in valid_intents
                if value != Intent.PRODUCT_INFORMATION.value
            ]
        if (
            Intent.PAYMENT_QUESTION.value in valid_intents
            and re.search(r"\b(cod|cash on delivery)\b", user_message, re.IGNORECASE)
        ):
            valid_intents = [
                value for value in valid_intents
                if value != Intent.DELIVERY_QUERY.value
            ]
        if not valid_intents:
            valid_intents = [Intent.UNKNOWN.value]

        entities: Dict[str, Any] = {}
        raw_entities = parsed["entities"]
        for key in self._entity_keys:
            value = raw_entities.get(key)
            if isinstance(value, str) and value.strip():
                entities[key] = value.strip()[:120]
            elif key == "quantity" and isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 100000:
                entities[key] = value

        try:
            confidence = min(1.0, max(0.0, float(parsed.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        gap = parsed["knowledge_gap"]
        return UnderstandingResult(
            intents=valid_intents,
            entities=entities,
            confidence=confidence,
            requires_human=bool(parsed.get("requires_human")),
            knowledge_gap=gap,
            available=True,
            reason="ok",
        )


understanding_engine = UnderstandingEngine()
