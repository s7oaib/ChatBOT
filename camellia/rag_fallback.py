import json
import re
from dataclasses import dataclass, field
from typing import Dict, Any, List, Tuple, Optional
from camellia.config import config
from camellia.database import db
from camellia.monitoring import get_logger
from camellia.trigger_index import trigger_index

log = get_logger("camellia.rag_fallback")

# Configured Confidence Thresholds (Section 8)
HIGH_CONFIDENCE_THRESHOLD = 0.85
MEDIUM_CONFIDENCE_THRESHOLD = 0.60

# Customer-Facing Safety Rule (Section 4):
# The following business facts must NEVER be auto-published without explicit template approval.
FORBIDDEN_FACT_KEYWORDS = {
    "price", "prices", "cost", "rs", "inr", "rupees", "per kg", "250g", "500g", "1kg",
    "ingredient", "ingredients", "allergen", "allergens", "allergy", "allergies", "nut", "nuts", "gluten", "dairy",
    "shelf life", "expiry", "storage", "refrigeration", "fridge", "last", "spoil",
    "delivery area", "delivery time", "delivery fee", "porter", "noida", "gurgaon", "delhi", "faridabad", "ghaziabad",
    "payment", "upi", "cod", "qr", "gpay", "paytm", "phonepe",
    "somdeb", "guru", "owner", "founder", "chef", "manager", "staff", "contact", "email", "phone",
    "customization", "flavour mixing", "assorted", "subscription", "policy", "policies"
}

RAG_STOP_WORDS = {
    "i", "am", "im", "my", "is", "are", "do", "does", "did", "you", "u", "r",
    "a", "an", "the", "to", "for", "of", "in", "on", "at", "by", "from", "with",
    "and", "or", "we", "can", "your", "this", "that", "these", "those", "what", "how",
    "when", "where", "why", "who", "which", "many", "much", "me", "us", "it", "its",
    "show", "tell", "send", "share", "about", "some", "any", "all", "than", "other",
    "please", "pls", "give", "let", "use", "be", "has", "had", "know", "offer"
}

@dataclass
class RAGResult:
    retrieved_sources: List[str] = field(default_factory=list)
    source_chunks: List[str] = field(default_factory=list)
    similarity_score: float = 0.0
    confidence_score: float = 0.0
    proposed_intent: str = "unknown_intent"
    proposed_response: str = ""
    auto_response: str = ""
    source_refs: List[str] = field(default_factory=list)
    has_conflict: bool = False
    contains_unapproved_facts: bool = False
    candidate_id: Optional[int] = None
    approval_status: str = "PENDING_APPROVAL"

class RAGFallbackEngine:
    """
    Isolated RAG Fallback & Knowledge Discovery Engine.
    
    Operates ONLY when no approved qa_templates.json match exists.
    Grounded strictly on approved Camellia Artisanal source documents.
    Enforces candidate creation (status: PENDING_APPROVAL) and human approval gates.
    """

    def __init__(self, qa_path: str = None, menu_path: str = None):
        self.qa_path = qa_path or config.QA_TEMPLATES_PATH
        self.menu_path = menu_path or config.MENU_JSON_PATH
        self.qa_data = self._load_json(self.qa_path)
        self.menu_data = self._load_json(self.menu_path)

    def _load_json(self, path: str) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error("RAG error loading %s: %s", path, e)
            return {}

    def reload_data(self):
        """Reload source documents."""
        self.qa_data = self._load_json(self.qa_path)
        self.menu_data = self._load_json(self.menu_path)

    def contains_business_facts(self, text: str) -> bool:
        """Checks if text mentions sensitive business facts subject to Section 4 safety rules."""
        lowered = text.lower()
        if re.search(r'₹|\brs\.?\s*\d+|\b\d+\s*rupees', lowered):
            return True
        for kw in FORBIDDEN_FACT_KEYWORDS:
            if re.search(r'\b' + re.escape(kw) + r'\b', lowered):
                return True
        return False

    def retrieve(self, user_message: str) -> RAGResult:
        """
        Retrieves relevant context chunks strictly from approved source documents,
        calculates similarity/confidence scores, checks for conflicting sources,
        and creates a candidate template with status 'PENDING_APPROVAL'.
        """
        result = RAGResult()
        if not user_message or not user_message.strip():
            return result

        lowered_text = user_message.lower().strip()
        user_words = set(re.findall(r'\b\w+\b', lowered_text)) - RAG_STOP_WORDS

        # Staff-approved answers are the highest-priority learned source.
        learned = db.search_learned_knowledge(user_message)
        if learned and learned.get("verified_answer"):
            result.retrieved_sources = [f"learned_knowledge#{learned.get('id')}"]
            result.source_chunks = [learned["verified_answer"]]
            result.similarity_score = 1.0
            # `or 1.0` would silently upgrade an explicit confidence:0 (staff
            # marking an entry as not yet trustworthy) to full confidence,
            # since 0 is falsy. Only a genuinely missing value should default.
            stored_confidence = learned.get("confidence")
            result.confidence_score = float(stored_confidence) if stored_confidence is not None else 1.0
            result.proposed_intent = f"learned_{learned.get('id')}"
            result.proposed_response = learned["verified_answer"]
            result.auto_response = learned["verified_answer"]
            result.source_refs = list(result.retrieved_sources)
            result.approval_status = "APPROVED"
            return result

        # Search approved templates in qa_templates.json for candidate match
        templates = self.qa_data.get("templates", [])
        candidates = []

        for tmpl in templates:
            tmpl_id = tmpl.get("id", "")
            triggers = tmpl.get("triggers", [])
            source_ref = tmpl.get("source_ref", f"qa_templates.json#{tmpl_id}")
            response_text = tmpl.get("response") or ""
            # Greeting variation is selected by responder.py; RAG must not invent a variation.
            if not response_text and tmpl_id != "greeting":
                responses = tmpl.get("responses", [])
                response_text = responses[0] if len(responses) == 1 else ""
            if tmpl.get("escalate") is True or tmpl_id.startswith("GUARD_") or not response_text:
                continue

            best_match_count = 0
            best_trigger_score = 0.0
            for trigger in triggers:
                trigger_pattern, trigger_words = trigger_index(trigger.lower().strip())
                t_words = trigger_words - RAG_STOP_WORDS
                matches = t_words.intersection(user_words)
                if matches and len(matches) > best_match_count:
                    best_match_count = len(matches)
                if trigger_pattern.search(lowered_text) and t_words:
                    best_trigger_score = 1.0
                elif t_words:
                    best_trigger_score = max(best_trigger_score, len(matches) / len(t_words))

            if best_match_count > 0:
                query_coverage = best_match_count / max(len(user_words), 1)
                score = min((best_trigger_score * 0.8) + (query_coverage * 0.2), 1.0)
                candidates.append({
                    "tmpl_id": tmpl_id,
                    "response": response_text,
                    "source_ref": source_ref,
                    "score": score,
                    "match_count": best_match_count
                })

        if not candidates:
            result.confidence_score = 0.0
            return result

        # Sort candidates by score
        candidates.sort(key=lambda x: x["score"], reverse=True)
        top = candidates[0]

        # Check for conflicting top sources (Section 10 Scenario I)
        if len(candidates) > 1:
            second = candidates[1]
            if top["score"] > 0.5 and second["score"] > 0.5 and top["response"] != second["response"]:
                # Difference in score is very small -> conflict!
                if abs(top["score"] - second["score"]) < 0.15:
                    result.has_conflict = True
                    result.confidence_score = 0.40
                    result.retrieved_sources = [top["source_ref"], second["source_ref"]]
                    result.source_chunks = [top["response"], second["response"]]
                    log.warning("RAG retrieved conflicting sources for '%s': %s vs %s", user_message, top["source_ref"], second["source_ref"])
                    return result

        result.retrieved_sources = [top["source_ref"]]
        result.source_chunks = [top["response"]]
        result.similarity_score = round(top["score"], 2)

        # Map to confidence score
        if top["score"] >= 0.70:
            result.confidence_score = 0.90
        elif top["score"] >= 0.40:
            result.confidence_score = 0.70
        else:
            result.confidence_score = 0.45

        result.proposed_intent = top["tmpl_id"]
        result.proposed_response = top["response"]
        result.source_refs = [top["source_ref"]]
        result.contains_unapproved_facts = self.contains_business_facts(result.proposed_response)

        # Only a high-confidence response already present in an approved template
        # may be sent automatically — and only when it does not itself state a
        # guarded business fact (price, delivery area, allergen info, staff
        # contact, etc.), since RAG's own word-overlap match to a template is not
        # the same guarantee as the deterministic matcher's stricter scoring.
        # contains_unapproved_facts was previously computed and never checked
        # here, so a template response mentioning a forbidden-fact keyword could
        # be auto-sent despite the rule stated in this class's own docstring.
        if (
            result.confidence_score >= HIGH_CONFIDENCE_THRESHOLD
            and top["source_ref"].startswith("qa_templates.json#")
            and not result.contains_unapproved_facts
        ):
            result.auto_response = result.proposed_response
            result.approval_status = "APPROVED_SOURCE"

        # High / Medium confidence -> Create Candidate Template with PENDING_APPROVAL status (Section 3)
        if result.confidence_score >= MEDIUM_CONFIDENCE_THRESHOLD and not result.auto_response:
            try:
                candidate_id = db.save_candidate_template(
                    name=result.proposed_intent,
                    response=result.proposed_response,
                    user_message=user_message,
                    source_refs=result.source_refs,
                    confidence=result.confidence_score,
                    created_by="rag_fallback"
                )
                result.candidate_id = candidate_id
                result.approval_status = "PENDING_APPROVAL"
                log.info("RAG Candidate Template #%d created for '%s' (status: PENDING_APPROVAL, confidence: %.2f)", candidate_id, user_message, result.confidence_score)
            except Exception as e:
                log.error("Failed to save RAG candidate template: %s", e)

        return result

rag_fallback_engine = RAGFallbackEngine()
