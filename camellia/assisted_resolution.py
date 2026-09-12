"""Human-gated assisted resolution drafts for Instagram review."""
import re
from typing import Any, Dict, Optional

from camellia.config import config
from camellia.database import db
from camellia.monitoring import get_logger
from camellia.rag_fallback import rag_fallback_engine

log = get_logger("camellia.assisted_resolution")
ALLOWED_CATEGORIES = {"AUTO_RESOLVE", "AI_SUGGESTED", "TEACH_REQUIRED", "HUMAN_REQUIRED", "NON_ACTIONABLE", "CONTACT_DETAIL", "UNSUPPORTED"}


def business_risk_flags(message: str) -> list[str]:
    text = (message or "").lower()
    patterns = (
        ("price", r"\b(price|prices|cost|rate|rs\.?|inr)\b|₹"),
        ("discount", r"\b(discount|offer|cheaper)\b"),
        ("delivery_commitment", r"\b(deliver|delivery|courier|dispatch|arrive|free delivery)\b"),
        ("availability", r"\b(available|availability|in stock|sold out)\b"),
        ("event_date", r"\b(wedding|event|party|function|date|bulk|boxes)\b"),
        ("payment", r"\b(payment|pay|upi|cod|cash|invoice|billing)\b"),
        ("refund", r"\b(refund|return|cancel|cancellation)\b"),
        ("hiring", r"\b(job|hire|hiring|work|career|vacancy)\b"),
        ("allergen", r"\b(allerg|nut|gluten|dairy|ingredient)\w*\b"),
        ("custom_order", r"\b(custom|customise|customize|bespoke|special order)\b"),
    )
    return [name for name, pattern in patterns if re.search(pattern, text, re.IGNORECASE)]


def classify_message(message: str) -> str:
    text = " ".join((message or "").strip().split())
    lowered = text.lower()
    if not text or re.fullmatch(r"[\W_]*(?:🤣|😂|👍|👌|❤️|❤|🙏|😄|🙂)+[\W_]*", text):
        return "NON_ACTIONABLE"
    if re.search(r"(?:\+?\d[\d\s().-]{8,}\d|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", text):
        return "CONTACT_DETAIL"
    if re.search(r"\b(?:human|person|agent|representative|speak to|talk to|job|hiring|career)\b", lowered):
        return "HUMAN_REQUIRED"
    if re.search(r"\b(?:teach|remember|learn this|add this|update your answer)\b", lowered):
        return "TEACH_REQUIRED"
    return "AI_SUGGESTED"


def _approved_match(message: str) -> Dict[str, Any]:
    try:
        learned = db.search_learned_knowledge(message, count_usage=False)
    except TypeError:
        learned = db.search_learned_knowledge(message)
    if learned and learned.get("verified_answer"):
        return {"answer": learned["verified_answer"], "knowledge_id": f"learned_knowledge#{learned.get('id')}", "source": "learned_knowledge", "source_chunks": [learned["verified_answer"]], "confidence": float(learned.get("confidence") or 1.0)}
    try:
        result = rag_fallback_engine.retrieve(message, persist_candidate=False)
    except Exception as exc:
        log.warning("Approved-knowledge lookup failed: %s", exc)
        return {}
    if not result.proposed_response or not result.source_refs or not any(ref.startswith(("qa_templates.json#", "learned_knowledge#")) for ref in result.source_refs):
        return {}
    ref = result.source_refs[0]
    return {"answer": result.proposed_response, "knowledge_id": ref if ref.startswith("learned_knowledge#") else result.proposed_intent, "source": ref, "source_chunks": result.source_chunks or [result.proposed_response], "confidence": float(result.confidence_score or 0.0)}


def create_suggestion(*, user_id: str, channel: str, customer_message: str, escalation_id: Optional[int] = None, conversation_id: Optional[int] = None, deterministic_resolved: bool = False, active_protected_flow: bool = False, source_routed: bool = False, source_routing_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not config.LLM_ASSISTED_RESOLUTION_ENABLED or str(channel).lower() != "instagram":
        return None
    if deterministic_resolved or active_protected_flow or source_routed:
        return None
    category = classify_message(customer_message)
    risks = business_risk_flags(customer_message)
    match = _approved_match(customer_message)
    answer = str(match.get("answer") or "").strip()
    grounded = False
    details = "No approved knowledge found; human review required."
    if answer and match.get("source_chunks"):
        grounded, reason = rag_fallback_engine.grounding_report(answer, match["source_chunks"])
        details = "Matched approved knowledge with high confidence." if grounded else f"Approved source failed grounding validation: {reason}."
    if category in {"NON_ACTIONABLE", "CONTACT_DETAIL", "HUMAN_REQUIRED"}:
        answer = ""; grounded = False; details = "Human review is required for this message."
    elif not answer:
        category = "UNSUPPORTED"
    elif not risks and grounded:
        category = "AUTO_RESOLVE"
    else:
        category = "AI_SUGGESTED"
    if category not in ALLOWED_CATEGORIES:
        category = "UNSUPPORTED"
    sid = db.create_assisted_suggestion(escalation_id=escalation_id, conversation_id=conversation_id, user_id=user_id, channel=channel, customer_message=customer_message, triage_category=category, detected_intent=category.lower(), confidence=float(match.get("confidence") or 0.0), matched_knowledge_id=match.get("knowledge_id"), matched_knowledge_source=match.get("source"), grounding_status="Grounded" if grounded else "Not Grounded", grounding_details=details, suggested_answer=answer, original_suggested_answer=answer, risk_flags=risks, model="approved_knowledge" if match else config.GROQ_MODEL, source_routing_id=source_routing_id)
    return db.get_assisted_suggestion(sid)


def is_whatsapp_allowed(channel: str) -> bool:
    return str(channel or "").lower() != "whatsapp"
