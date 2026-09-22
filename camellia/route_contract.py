"""Customer-facing decision lanes for the canonical router.

This module deliberately classifies an already-selected response; it never
chooses business facts.  Facts and approved copy remain owned by the existing
knowledge/template layers, while this contract gives every reply one safe,
auditable outcome.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional


EXACT_APPROVED = "EXACT_APPROVED"
GROUNDED_APPROVED = "GROUNDED_APPROVED"
INSUFFICIENT_KNOWLEDGE = "INSUFFICIENT_KNOWLEDGE"
SENSITIVE_BUSINESS_FACT = "SENSITIVE_BUSINESS_FACT"
SYSTEM_CONVERSATION = "SYSTEM_CONVERSATION"
GROUNDED_DERIVED = "GROUNDED_DERIVED"
HUMAN_HANDOFF = "HUMAN_HANDOFF"
SOURCE_DESCRIPTION = "SOURCE_DESCRIPTION"

_HANDOFF_TEMPLATES = frozenset({
    None,
    "explicit_escalation",
    "groq_structured_event_escalation",
    "instagram_support_handoff",
    "instagram_whatsapp_handoff",
})


@dataclass(frozen=True)
class RouteTrace:
    """Safe metadata describing why the selected lane was allowed."""

    lane: str
    intent: Optional[str]
    approved_source_ids: tuple[str, ...]
    state_influenced: bool
    source_context_influenced: bool
    handoff_reason: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "decision_lane": self.lane,
            "intent": self.intent,
            "approved_source_ids": list(self.approved_source_ids),
            "state_influenced": self.state_influenced,
            "source_context_influenced": self.source_context_influenced,
            "handoff_reason": self.handoff_reason,
        }


def classify_lane(
    *,
    was_matched: bool,
    template_id: Optional[str],
    facts: Dict[str, Any],
) -> str:
    """Map the existing response metadata to the authoritative decision lane."""
    if facts.get("last_route_source") == HUMAN_HANDOFF:
        return HUMAN_HANDOFF
    if not was_matched and facts.get("last_route_source") == EXACT_APPROVED:
        return HUMAN_HANDOFF
    if was_matched and facts.get("last_route_source") in {
        SYSTEM_CONVERSATION, EXACT_APPROVED, GROUNDED_DERIVED, SOURCE_DESCRIPTION,
    }:
        return facts["last_route_source"]
    if (
        not was_matched
        or template_id in _HANDOFF_TEMPLATES
        or facts.get("knowledge_sufficient") is False
    ):
        return INSUFFICIENT_KNOWLEDGE

    grounding = str(facts.get("last_grounding") or "")
    if grounding == "pass" or grounding.startswith("grounded"):
        return GROUNDED_APPROVED
    return EXACT_APPROVED


def build_trace(
    *,
    lane: str,
    facts: Dict[str, Any],
) -> RouteTrace:
    """Build a trace from privacy-safe session metadata only."""
    intents = facts.get("last_intents") or []
    intent = str(intents[0]) if intents else None
    source_ids = tuple(str(value) for value in (facts.get("last_knowledge_ids") or []) if value)
    route_source = str(facts.get("last_route_source") or "")
    state_influenced = route_source in {"state_handler", "canonical_order_handoff"}
    source_influenced = bool(facts.get("source_context_used"))
    return RouteTrace(
        lane=lane,
        intent=intent,
        approved_source_ids=source_ids,
        state_influenced=state_influenced,
        source_context_influenced=source_influenced,
        handoff_reason=(facts.get("last_handoff_reason") if lane == INSUFFICIENT_KNOWLEDGE else None),
    )
