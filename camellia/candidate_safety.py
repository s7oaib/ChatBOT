"""Eligibility and validation rules for AI-drafted knowledge candidates."""

import re
from typing import Tuple


_ACTION_OR_SESSION = re.compile(
    r"\b(?:speak|talk|chat)\s+(?:to|with)\s+(?:a\s+)?(?:human|person|agent)|"
    r"\b(?:need|want|get|find)\s+(?:a\s+|an\s+)?(?:human|person|agent)\b|"
    r"\b(?:human|agent|staff)\s+(?:please|help|support)|\bhandoff\b|\bescalat(?:e|ion)\b|"
    r"\bCAM-\d+\b|\b(?:cancel|change|update|track)\s+(?:my|the|this|your)?\s*order\b|"
    r"\b(?:my|your|this|the)\s+(?:current|existing|active)\s+(?:order|session|draft)\b|"
    r"\b(?:already|previously)\s+(?:chose|selected|saved|confirmed|discussed)\b",
    re.IGNORECASE,
)

_NON_KNOWLEDGE = re.compile(
    r"\b(?:made|created|shot|edited)\s+(?:a\s+)?(?:reel|post|video)\s+for\s+you\b|"
    r"\b(?:collab|collaboration|influencer|social media services?|promote your)\b|"
    r"\b(?:follow me|check (?:out )?my profile|dm me)\b",
    re.IGNORECASE,
)

_SECRET_OR_DEBUG = re.compile(
    r"\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|password|system prompt|"
    r"traceback|stack trace|debug\s*:|exception\s*:|authorization\s*: bearer)\b",
    re.IGNORECASE,
)


def normalize_question(value: str) -> str:
    """Stable comparison form used to deduplicate equivalent pending drafts."""
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))[:500]


def knowledge_key(name: str, user_message: str) -> str:
    topic = normalize_question(name) or "general"
    question = normalize_question(user_message)
    return f"{topic}:{question}"[:700]


def candidate_eligibility(user_message: str, response: str) -> Tuple[bool, str]:
    combined = f"{user_message or ''}\n{response or ''}"
    if _ACTION_OR_SESSION.search(combined):
        return False, "Action, handoff, order, or session-specific content is not reusable knowledge"
    if _NON_KNOWLEDGE.search(combined):
        return False, "Outreach or other non-knowledge content is not eligible"
    return True, ""


def validate_candidate_answer(response: str) -> Tuple[bool, str]:
    answer = (response or "").strip()
    if not answer:
        return False, "Draft answer cannot be empty"
    if len(answer) < 3:
        return False, "Draft answer is too short to be useful"
    if len(answer) > 8000:
        return False, "Draft answer is too long"
    if _SECRET_OR_DEBUG.search(answer):
        return False, "Draft answer appears to contain a secret or debug output"
    return True, ""
