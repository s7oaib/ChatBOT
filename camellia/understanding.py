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
    knowledge_gap: Optional[str] = None
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

    _entity_keys = {"product", "location", "quantity", "event", "menu_type"}

    @staticmethod
    def should_analyze(user_message: str) -> bool:
        """Use structured NLU where one-winner routing is known to lose meaning."""
        text = user_message.lower()
        if (
            re.search(r"\b(order|buy|add|purchase|send|deliver)\b", text)
            and re.search(r"\b\d+\b", text)
            and re.search(r"\b(box|pack|bento|sondesh|mishti|sweet|jolbhora|kanchagolla)\b", text)
        ):
            return False
        if (
            re.search(r"\b(storage|store|shelf life|refrigerat\w*|fridge|expiry)\b", text)
            and not re.search(r"\b(menu|price|cost|deliver|delivery|courier|ship)\b", text)
        ):
            return False
        if re.search(r"\b(event|wedding|party|corporate|festive|function)\b", text):
            return True
        concepts = sum(bool(pattern.search(text)) for pattern in (
            re.compile(r"\b(menu|what can i order|what do you have)\b"),
            re.compile(r"\b(price|cost|rate|how much|jolbhora|sondesh|sweet|mishti)\b"),
            re.compile(r"\b(deliver|delivery|courier|ship)\b"),
            re.compile(r"\b(storage|store|shelf life|refrigerat|fridge|expiry)\b"),
        ))
        return concepts >= 2 and bool(re.search(r"\b(and|also|aur)\b|\?.*\?", text))

    @staticmethod
    def deterministic_fallback(user_message: str) -> UnderstandingResult:
        """Conservative continuity when Groq is unavailable or rate-limited."""
        text = user_message.lower()
        intents: List[str] = []
        if re.search(r"\b(menu|what can i order|what do you have)\b", text):
            intents.append(Intent.MENU_REQUEST.value)
        if re.search(r"\b(price|cost|rate|how much)\b", text):
            intents.append(Intent.PRODUCT_PRICE.value)
        elif re.search(r"\b(jolbhora|sondesh|sweet|sweets|mishti|mithai|aamar mishti)\b", text):
            intents.append(Intent.PRODUCT_INFORMATION.value)
        if re.search(r"\b(deliver|delivery|courier|ship)\b", text):
            intents.append(Intent.DELIVERY_QUERY.value)
        if re.search(r"\b(event|wedding|party|function)\b", text):
            intents.append(Intent.EVENT_ORDER.value)
        elif re.search(r"\b(bulk|corporate|festive)\b", text):
            intents.append(Intent.BULK_ORDER.value)
        if re.search(r"\b(storage|store|shelf life|refrigerat\w*|fridge|expiry)\b", text):
            intents.append(Intent.STORAGE_QUESTION.value)

        entities: Dict[str, Any] = {}
        for product in ("jolbhora", "aamar mishti", "kanchagolla", "nolen gur", "sondesh"):
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

        if not intents:
            intents = [Intent.UNKNOWN.value]
        return UnderstandingResult(
            intents=list(dict.fromkeys(intents)),
            entities=entities,
            confidence=0.75,
            requires_human=any(i in intents for i in (Intent.EVENT_ORDER.value, Intent.BULK_ORDER.value)),
            knowledge_gap=None,
            available=True,
            reason="deterministic_fallback",
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
            "Return exactly one JSON object with this schema:\n"
            '{"intents":["menu_request"],"entities":{"product":null,"location":null,'
            '"quantity":null,"event":null,"menu_type":null},"confidence":0.0,'
            '"requires_human":false,"knowledge_gap":null}\n'
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
        if not ok:
            return UnderstandingResult(reason=reason)
        parsed = _json_object(raw)
        if not parsed:
            return UnderstandingResult(reason="invalid_json")

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
        if not valid_intents:
            valid_intents = [Intent.UNKNOWN.value]

        entities: Dict[str, Any] = {}
        raw_entities = parsed.get("entities")
        if isinstance(raw_entities, dict):
            for key in self._entity_keys:
                value = raw_entities.get(key)
                if isinstance(value, str) and value.strip():
                    entities[key] = value.strip()[:120]
                elif key == "quantity" and isinstance(value, int) and 0 < value <= 100000:
                    entities[key] = value

        try:
            confidence = min(1.0, max(0.0, float(parsed.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        gap = parsed.get("knowledge_gap")
        gap = str(gap).strip()[:200] if gap else None
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
