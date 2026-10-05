"""Small, fail-closed conversational and derived-answer layer for Instagram."""

import json
import re
import time
from typing import Any, Dict, Optional, Tuple

from camellia.intent_guards import split_conversational_opener
from camellia import client_qa
from camellia.ai_responder import ai_responder


BENTO_SOURCE = "bento_details_client"
MIN_CONFIDENCE = 0.95


def system_reply(
    message: str, *, include_contextual_acknowledgement: bool = True,
) -> Optional[str]:
    text = re.sub(r"[\s.!?]+", " ", message.casefold()).strip()
    opener, remainder = split_conversational_opener(message)
    if opener and not remainder:
        return "Hi! Thanks for reaching out to Camellia.artisanal 😊 How can I help you today?"
    if re.fullmatch(r"(?:(?:okay|ok) )?(?:thanks|thank you|thank you so much|thx)", text):
        return "You're most welcome! 😊"
    if include_contextual_acknowledgement and re.fullmatch(
        r"okay|ok|sure|great|nice|got it|yes|no|yep|nope|👍|😊|🙏", text,
    ):
        return "Got it! 😊 How can I help you?"
    if re.fullmatch(r"bye|goodbye|see you|take care", text):
        return "Thank you for connecting with Camellia.artisanal. Have a lovely day!"
    return None


def _bento_prices() -> Dict[str, int]:
    entry = client_qa.effective_entry(BENTO_SOURCE)
    if not entry:
        return {}
    matches = re.findall(r"\b(Vegetarian|Chicken|Mangsho) Bento\s*[—-]\s*₹\s*(\d+)", entry.answer, re.I)
    prices = {name.casefold(): int(value) for name, value in matches}
    return prices if len(matches) == 3 and len(prices) == 3 else {}


def _names(text: str) -> Tuple[str, ...]:
    found = re.findall(r"\b(vegetarian|veg|chicken|mangsho)\b", text, re.I)
    return tuple(dict.fromkeys("vegetarian" if name.casefold() == "veg" else name.casefold() for name in found))


def _render(operation: str, names: Tuple[str, ...], prices: Dict[str, int]) -> Optional[str]:
    if operation == "cheapest" and prices:
        name = min(prices, key=prices.get)
        return f"The {name.title()} Bento is the lowest-priced option at ₹{prices[name]}."
    if operation == "chicken_selection" and "chicken" in prices:
        return f"The Chicken Bento is the chicken option at ₹{prices['chicken']}."
    if operation == "selection" and len(names) == 1 and names[0] in prices:
        return f"The {names[0].title()} Bento is ₹{prices[names[0]]}."
    if len(names) != 2 or any(name not in prices for name in names):
        return None
    first, second = names
    if operation == "difference":
        high, low = sorted(names, key=lambda name: prices[name], reverse=True)
        return f"The {high.title()} Bento is ₹{prices[high] - prices[low]} more than the {low.title()} Bento."
    if operation == "more_expensive":
        high, low = sorted(names, key=lambda name: prices[name], reverse=True)
        return f"The {high.title()} Bento is more expensive at ₹{prices[high]}, compared with ₹{prices[low]} for the {low.title()} Bento."
    if operation == "confirm_difference":
        return f"Yes. The {first.title()} Bento is ₹{prices[first] - prices[second]} more than the {second.title()} Bento." if prices[first] > prices[second] else None
    return None


def _operation(message: str, names: Tuple[str, ...], bento_context: bool) -> Optional[str]:
    text = message.casefold()
    if not bento_context or re.search(r"\b(discount|offer|bulk|wedding|cater|available|availability|stock|without|allerg\w*|gluten|onion|deliver\w*|ship\w*|customi[sz]\w*|subscription|gst)\b", text):
        return None
    if re.search(r"\b(cheapest|lowest[- ]priced|least expensive)\b", text):
        return "cheapest"
    if len(names) == 2:
        if re.search(r"\b(is|would)\b.*₹\s*\d+\s+more\b", text):
            return "confirm_difference"
        if re.search(r"\b(how much more|difference|price gap)\b", text):
            return "difference"
        if re.search(r"\b(which|what)\b.*\b(more expensive|costs more|pricier|higher price)\b", text):
            return "more_expensive"
    if names == ("chicken",) and re.search(r"\b(which one|which bento|what should i order)\b", text):
        return "chicken_selection"
    if len(names) == 1 and re.fullmatch(r"(?:the\s+)?(?:vegetarian|veg|chicken|mangsho)(?:\s+bento)?[.! ]*", text):
        return "selection"
    return None


def validate_model_draft(draft: Any, expected_answer: str, source_id: str = BENTO_SOURCE) -> bool:
    """A model may select a derivation, but cannot author unverified customer facts."""
    if not isinstance(draft, dict) or draft.get("decision") != "GROUNDED_DERIVED":
        return False
    source = client_qa.effective_entry(source_id)
    if source is None:
        return False
    if draft.get("answer") != expected_answer or draft.get("source_ids") != [source_id]:
        return False
    confidence = draft.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (float, int)) or confidence < MIN_CONFIDENCE:
        return False
    claims = draft.get("claims")
    approved_answer = source.answer
    return bool(isinstance(claims, list) and claims and all(
        isinstance(claim, dict) and claim.get("supported_by") == [source_id]
        and isinstance(claim.get("claim"), str)
        and (claim["claim"] in expected_answer or claim["claim"] in approved_answer)
        for claim in claims
    ))


def derive(message: str, facts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if re.search(r"\bsondesh\b", message, re.I) and re.search(r"\bgluten\b", message, re.I):
        source = client_qa.effective_entry("general_details")
        if source and "no gluten" in source.answer.casefold() and not re.search(r"\b(?:allerg\w*|coeliac|celiac|cross[- ]contamination|strict|severe)\b", message, re.I):
            return {"answer": "The client-approved Sondesh description says ‘no gluten.’",
                    "source_ids": ["general_details"], "confidence": 1.0,
                    "grounding": "approved_registry_quote", "model_called": False, "latency_ms": 0}
    prices = _bento_prices()
    if not prices:
        return None
    names = _names(message)
    bento_context = bool(re.search(r"\bbento\b", message, re.I) or facts.get("active_product_context") == "bento" or len(names) == 2)
    operation = _operation(message, names, bento_context)
    if not operation:
        return None
    answer = _render(operation, names, prices)
    if not answer:
        return None
    # The confirmation must also agree with the customer's stated arithmetic.
    if operation == "confirm_difference":
        claimed = re.search(r"₹\s*(\d+)", message)
        if not claimed or int(claimed.group(1)) != prices[names[0]] - prices[names[1]]:
            return None
    result = {"answer": answer, "source_ids": [BENTO_SOURCE], "confidence": 1.0,
              "grounding": "approved_registry_arithmetic", "model_called": False, "latency_ms": 0}
    # Deterministic math is sufficient. When Groq is enabled, its independent
    # structured assessment is accepted only if byte-identical to the
    # Python-rendered answer; an outage cannot alter that deterministic fact.
    if ai_responder.get_active_provider() == "groq":
        prompt = json.dumps({
            "task": "Return JSON only. Select GROUNDED_DERIVED only if the answer follows from the supplied approved source. Do not add any fact.",
            "question": message,
            "approved_sources": [
                {"source_id": entry.qa_id, "answer": entry.answer}
                for entry in client_qa.effective_registry()
            ],
            "expected_answer": answer,
            "schema": {"decision": "GROUNDED_DERIVED or HANDOFF", "answer": "string", "source_ids": [BENTO_SOURCE],
                       "claims": [{"claim": "string", "supported_by": [BENTO_SOURCE]}], "confidence": "number"},
        }, ensure_ascii=False)
        started = time.perf_counter()
        raw, ok, _reason = ai_responder._query_groq(prompt, response_format={"type": "json_object"}, max_tokens=400)
        result["model_called"] = True
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        try:
            draft = json.loads(raw) if ok else None
        except (TypeError, ValueError):
            draft = None
        if ok and draft is None:
            return None
        if draft is not None and not validate_model_draft(draft, answer):
            return None
        if draft is not None:
            result["confidence"] = float(draft["confidence"])
            result["grounding"] = "model_draft_verified_against_approved_arithmetic"
    return result
