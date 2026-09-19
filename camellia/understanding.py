"""Structured language understanding without authority to answer or act."""

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from camellia import semantic_fallback
from camellia.semantic_fallback import normalize_menu_type, normalize_pack_size
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
    DIETARY_QUESTION = "dietary_question"
    DELIVERY_TIME_QUESTION = "delivery_time_question"
    SUBSCRIPTION_QUERY = "subscription_query"
    SUBSCRIPTION_BOOKING = "subscription_booking"
    SUBSCRIPTION_CHANGE = "subscription_change"
    # A question that is clearly about the business but names something no
    # approved knowledge covers — pets, washrooms, seating, whatever comes
    # next. Typed rather than silent, so it can be answered honestly.
    BUSINESS_POLICY_UNKNOWN = "unknown_business_query"
    ESCALATION = "escalation"
    UNKNOWN = "unknown"


STRUCTURED_NLU_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "camellia_structured_understanding",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "intents": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [intent.value for intent in Intent],
                    },
                },
                "entities": {
                    "type": "object",
                    "properties": {
                        "product": {"type": ["string", "null"]},
                        "location": {"type": ["string", "null"]},
                        "quantity": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "integer"},
                                {"type": "null"},
                            ],
                        },
                        "event": {"type": ["string", "null"]},
                        "menu_type": {"type": ["string", "null"]},
                        "pack_size": {"type": ["string", "null"]},
                        "topic": {"type": ["string", "null"]},
                        "subtopic": {"type": ["string", "null"]},
                    },
                    "required": [
                        "product", "location", "quantity", "event",
                        "menu_type", "pack_size", "topic", "subtopic",
                    ],
                    "additionalProperties": False,
                },
                "confidence": {"type": "number"},
                "requires_human": {"type": "boolean"},
                "knowledge_gap": {"type": "boolean"},
            },
            "required": [
                "intents", "entities", "confidence", "requires_human",
                "knowledge_gap",
            ],
            "additionalProperties": False,
        },
    },
}


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
    # Words the deterministic reader could not account for. Empty for a model
    # result, which is trusted to have read the whole message.
    unmapped_terms: List[str] = field(default_factory=list)


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

    _STRICT_FORMAT_FAILURE_COOLDOWN_SECONDS = 60.0

    _entity_keys = {
        "product", "location", "quantity", "event", "menu_type", "topic",
        "pack_size", "subtopic",
    }

    def __init__(self):
        self._strict_format_disabled_until = 0.0

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
        """Read meaning without a model, using concepts rather than phrasings.

        `semantic_fallback.read` owns the reading: concept families, entity
        extraction and clause-by-clause analysis, so a compound question keeps
        both halves and an unrecognised subject becomes a typed gap instead of
        disappearing. Confidence stays deliberately below the model's: this
        result is continuity, not certainty.
        """
        reading = semantic_fallback.read(user_message)
        intents = [
            intent for intent in reading["intents"]
            if intent in {member.value for member in Intent}
        ] or [Intent.UNKNOWN.value]
        entities = reading["entities"]
        return UnderstandingResult(
            intents=list(dict.fromkeys(intents)),
            entities=entities,
            confidence=0.75,
            requires_human=any(
                intent in intents
                for intent in (
                    Intent.EVENT_ORDER.value, Intent.BULK_ORDER.value, Intent.ESCALATION.value,
                )
            ),
            knowledge_gap=any(
                intent in intents
                for intent in (Intent.UNKNOWN.value, Intent.BUSINESS_POLICY_UNKNOWN.value)
            ),
            available=True,
            reason="deterministic_fallback",
            unmapped_terms=list(reading.get("unmapped") or []),
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
        if time.monotonic() < self._strict_format_disabled_until:
            return UnderstandingResult(reason="structured_nlu_format_cooldown")

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
            '"quantity":null,"event":null,"menu_type":null,"pack_size":null,"topic":null,'
            '"subtopic":null},"confidence":0.0,'
            '"requires_human":false,"knowledge_gap":false}\n'
            f"ACTIVE STATE: {json.dumps(active_state, ensure_ascii=False)}\n"
            f"RECENT CONVERSATION:\n{recent or '(none)'}\n"
            f"CUSTOMER MESSAGE: {user_message}"
        )
        raw, ok, reason = ai_responder._query_groq(
            prompt,
            is_refinement=True,
            model=config.GROQ_ROUTER_MODEL,
            response_format=STRUCTURED_NLU_RESPONSE_FORMAT,
            max_tokens=300,
            reasoning_effort="low",
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
                    response_format=STRUCTURED_NLU_RESPONSE_FORMAT,
                    max_tokens=300,
                    reasoning_effort="low",
                )
                reason = f"{reason}>{fallback_reason}" if not ok else "ok_fallback_model"
        if not ok:
            if reason == "groq_json_validate_failed":
                self._strict_format_disabled_until = (
                    time.monotonic() + self._STRICT_FORMAT_FAILURE_COOLDOWN_SECONDS
                )
                log.warning(
                    "Structured NLU strict output failed; using deterministic fallback "
                    "for %.0fs.",
                    self._STRICT_FORMAT_FAILURE_COOLDOWN_SECONDS,
                )
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

        # The model is asked for one of a few named values here, but nothing
        # enforces that at the API boundary: it can say "veg" where the
        # catalogue matches on "vegetarian". Normalizing at the boundary means
        # a raw model value can never silently fail every record and fall
        # through to a generic answer — the same vocabulary the offline
        # reader uses decides both paths.
        if "menu_type" in entities:
            normalized_menu_type = normalize_menu_type(entities["menu_type"])
            if normalized_menu_type:
                entities["menu_type"] = normalized_menu_type
            else:
                del entities["menu_type"]
        if "pack_size" in entities:
            normalized_pack_size = normalize_pack_size(entities["pack_size"])
            if normalized_pack_size:
                entities["pack_size"] = normalized_pack_size

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
