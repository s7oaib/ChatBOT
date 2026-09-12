"""Deterministic Auto Resolve eligibility and simulation.

This module is intentionally separate from the LLM prompt path.  A model may
suggest an answer, but only these server-side gates can classify it as a
simulation candidate.  The production default records decisions and continues
through the human-gated Assisted Resolution workflow; it never sends here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, Optional

from camellia.config import config
from camellia.database import db
from camellia.monitoring import get_logger

log = get_logger("camellia.auto_resolve")

# Safe categories are explicit.  AUTO_RESOLVE is the category emitted by the
# existing grounded FAQ path; the descriptive aliases make the contract clear
# for future category controls without trusting free-form model output.
SAFE_CATEGORY_WHITELIST = {
    "AUTO_RESOLVE",
    "GENERAL_FAQ",
    "PRODUCT_DESCRIPTION",
    "MENU_EXPLANATION",
    "GENERAL_BRAND_INFORMATION",
}

HARD_RISK_FLAGS = {
    "price", "discount", "delivery_commitment", "availability", "payment",
    "refund", "order_change", "event_date", "hiring", "allergen",
    "custom_order", "quantity", "complaint", "escalation", "human",
    "contact_detail",
}


def _normalise_categories(raw: Optional[str] = None) -> set[str]:
    value = config.LLM_AUTO_RESOLVE_ALLOWED_CATEGORIES if raw is None else raw
    return {item.strip().upper() for item in str(value or "").split(",") if item.strip()}


def _idempotency_key(suggestion: Dict[str, Any]) -> str:
    sid = suggestion.get("id") or suggestion.get("assisted_suggestion_id")
    if sid:
        return f"assisted:{sid}"
    source = "|".join(str(suggestion.get(key) or "") for key in ("channel", "user_id", "customer_message"))
    return "message:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


def _knowledge_state(suggestion: Dict[str, Any]) -> tuple[bool, list[str]]:
    knowledge_id = str(suggestion.get("matched_knowledge_id") or "")
    if not knowledge_id:
        return False, ["approved_knowledge_missing"]
    if knowledge_id.startswith("learned_knowledge#"):
        try:
            row = db.get_custom_qa(int(knowledge_id.rsplit("#", 1)[1]))
        except (TypeError, ValueError):
            row = None
        if not row:
            return False, ["approved_knowledge_missing"]
        status = str(row.get("status") or "ACTIVE").upper()
        reasons: list[str] = []
        if status != "ACTIVE":
            reasons.append(f"knowledge_{status.lower()}")
        if not str(row.get("verified_answer") or "").strip():
            reasons.append("approved_knowledge_missing")
        # Repeated rejection/edit issues make the answer ineligible without
        # disabling the Q&A itself; the dashboard surfaces the same signal.
        problems = db.knowledge_resolution_problems(knowledge_id)
        if problems.get("repeated_rejections") or problems.get("major_edits"):
            reasons.append("knowledge_review_recommended")
        return not reasons, reasons
    # Canonical qa_templates references are approved by construction, but the
    # source and grounding checks below still apply.
    return True, []


def evaluate_eligibility(
    suggestion: Optional[Dict[str, Any]],
    *,
    deterministic_resolved: bool = False,
    protected_order_flow: bool = False,
    source_routed: bool = False,
    category_controls: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """Return a complete, explainable eligibility decision.

    Every failed gate is retained in ``eligibility_reasons`` so operators can
    distinguish an ordinary low-confidence fallback from a safety exclusion.
    """
    item = suggestion or {}
    reasons: list[str] = []
    channel = str(item.get("channel") or "").lower()
    if channel != "instagram":
        reasons.append("instagram_only")
    if protected_order_flow or item.get("protected_order_flow"):
        reasons.append("protected_order_flow")
    if source_routed or item.get("source_route_present") or item.get("source_routing_id"):
        reasons.append("source_route_precedence")
    if deterministic_resolved:
        reasons.append("deterministic_resolved")
    if item.get("cam_interference"):
        reasons.append("cam_handoff_conflict")

    category = str(item.get("triage_category") or item.get("category") or "").upper()
    allowed_categories = _normalise_categories()
    # An empty allowlist means simulation observes every safe category.  The
    # actual-send path has a separate persisted category control and remains
    # off while LLM_AUTO_RESOLVE_ENABLED=false.
    category_enabled = (category_controls or {}).get(category, False) if category_controls is not None else (category in allowed_categories if allowed_categories else True)
    if category not in SAFE_CATEGORY_WHITELIST:
        reasons.append("category_not_whitelisted")
    if not category_enabled:
        reasons.append("category_control_disabled")

    grounding = str(item.get("grounding_status") or "").strip().lower()
    if grounding != "grounded":
        reasons.append("grounding_failed")
    try:
        confidence = float(item.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
        reasons.append("confidence_missing")
    if confidence < float(config.LLM_AUTO_RESOLVE_MIN_CONFIDENCE):
        reasons.append("confidence_below_threshold")

    risks = {str(flag).strip().lower() for flag in (item.get("risk_flags") or []) if str(flag).strip()}
    if risks & HARD_RISK_FLAGS:
        reasons.append("risk_flags_present")
    if item.get("ambiguous") or item.get("ambiguity"):
        reasons.append("ambiguous_knowledge")
    if item.get("duplicate_knowledge"):
        reasons.append("duplicate_knowledge")
    knowledge_ok, knowledge_reasons = _knowledge_state(item)
    if not knowledge_ok:
        reasons.extend(knowledge_reasons)

    # The global kill switch is checked even in simulation so a future pilot
    # can stop actual sends without a deploy.  Simulation remains useful while
    # the global actual switch is off, hence this is reported separately.
    eligible = not reasons
    would_auto_resolve = bool(eligible)
    return {
        "eligibility": "ELIGIBLE" if eligible else "NOT_ELIGIBLE",
        "eligible": eligible,
        "would_auto_resolve": would_auto_resolve,
        "eligibility_reasons": reasons or ["all_gates_passed"],
        "category": category,
        "confidence": confidence,
        "grounding_status": item.get("grounding_status") or "Not Grounded",
        "risk_flags": sorted(risks),
        "matched_knowledge_id": item.get("matched_knowledge_id"),
    }


def simulate_auto_resolve(
    suggestion: Optional[Dict[str, Any]],
    *,
    deterministic_resolved: bool = False,
    protected_order_flow: bool = False,
    source_routed: bool = False,
) -> Optional[Dict[str, Any]]:
    """Record one simulation decision; never send a customer-facing reply."""
    if not config.LLM_AUTO_RESOLVE_SIMULATION_ENABLED:
        return None
    item = suggestion or {}
    decision = evaluate_eligibility(
        item,
        deterministic_resolved=deterministic_resolved,
        protected_order_flow=protected_order_flow,
        source_routed=source_routed,
    )
    fields = {
        "assisted_suggestion_id": item.get("id"),
        "conversation_id": item.get("conversation_id"),
        "user_id": item.get("user_id") or "",
        "channel": item.get("channel") or "instagram",
        "customer_message": item.get("customer_message") or "",
        "matched_knowledge_id": decision.get("matched_knowledge_id"),
        "category": decision.get("category"),
        "confidence": decision.get("confidence"),
        "grounding_status": decision.get("grounding_status"),
        "risk_flags": decision.get("risk_flags") or [],
        "eligibility": decision.get("eligibility"),
        "eligibility_reasons": decision.get("eligibility_reasons") or [],
        "simulated": True,
        "would_auto_resolve": decision.get("would_auto_resolve", False),
        "actually_sent": False,
        "response_text": item.get("suggested_answer") or "",
        "idempotency_key": _idempotency_key(item),
    }
    try:
        return db.record_auto_resolve_decision(**fields)
    except Exception:
        # Simulation is observability; it must never make the customer path
        # fail closed because a metrics write had a transient DB problem.
        log.exception("Auto Resolve simulation decision could not be recorded")
        return None
