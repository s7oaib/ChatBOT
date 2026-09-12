"""Eligibility and validation rules for AI-drafted knowledge candidates."""

import re
from typing import List, Tuple


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


# Custom Q&A (client-authored learned_knowledge), a distinct concern from the
# above: an authorized client is allowed to state a real price or a real
# delivery promise — that is the whole point of the feature — so this is a
# confirmation prompt, not a block. Only _SECRET_OR_DEBUG above still blocks
# outright, via validate_candidate_answer, which every save still runs.
_BUSINESS_COMMITMENT_CHECKS = (
    ("price", re.compile(r"₹|\brs\.?\s?\d|\bINR\b|\bprice\b|\bpriced?\b|\bcost(?:s|ing)?\b", re.IGNORECASE)),
    ("discount", re.compile(r"\bdiscount\b|\d{1,3}\s?%\s?off\b|\boffer\b", re.IGNORECASE)),
    ("free_delivery", re.compile(r"\bfree\s+delivery\b|\bno\s+delivery\s+charge", re.IGNORECASE)),
    ("delivery_promise", re.compile(r"\bwe\s+deliver\b|\bdelivery\s+(?:within|in)\b|\bship(?:ping)?\s+to\b", re.IGNORECASE)),
    ("same_day_guarantee", re.compile(r"\bsame[\s-]?day\b|\bnext[\s-]?day\b|\bwithin\s+\d+\s*(?:hours?|hrs?)\b|\btomorrow\b", re.IGNORECASE)),
    ("availability", re.compile(r"\bin\s+stock\b|\bavailable\b|\bsold\s+out\b|\bout\s+of\s+stock\b", re.IGNORECASE)),
    ("event_date", re.compile(r"\b(?:on|by|before)\s+\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", re.IGNORECASE)),
    ("hiring", re.compile(r"\bhiring\b|\bwe(?:'|')?re\s+hiring\b|\bjob\s+opening\b|\bvacan(?:cy|cies)\b|\bapply\s+(?:now|here|today)\b", re.IGNORECASE)),
    ("custom_order", re.compile(r"\bcustom[\s-]?order\b|\bcustomi[sz]ed?\s+order\b|\bbespoke\b|\bpersonali[sz]ed\s+(?:order|cake|hamper)\b", re.IGNORECASE)),
    ("final_invoice", re.compile(r"\bfinal\s+(?:invoice|bill|amount|price)\b|\btotal\s+(?:invoice|amount)\s+(?:is|will\s+be)\b", re.IGNORECASE)),
    ("allergen", re.compile(r"\ballerg\w*|\bcontains?\s+(?:nuts?|dairy|gluten|egg)\b|\bnut[\s-]?free\b|\bvegan\b", re.IGNORECASE)),
)


def detect_business_commitment_warnings(text: str) -> List[str]:
    """
    Flags a Custom Q&A answer that states a price, promise, or other business
    commitment the client should double-check before publishing — a warning
    that requires an explicit confirm, not a block. Secrets/credentials stay
    genuinely blocked by validate_candidate_answer, run alongside this.
    """
    body = text or ""
    return [label for label, pattern in _BUSINESS_COMMITMENT_CHECKS if pattern.search(body)]
