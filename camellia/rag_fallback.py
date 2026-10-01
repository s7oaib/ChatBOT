import hashlib
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
            if path == self.menu_path:
                from camellia.menu_review import canonical_menu
                return canonical_menu(path)
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

    def grounding_report(
        self, answer: str, context_chunks: List[str], strict: bool = True,
    ) -> Tuple[bool, str]:
        """
        True when a generated answer states no number the approved excerpts
        did not already state.

        Numbers are where a fabricated answer does real damage on this bot:
        a price, a pack size, a piece count, a delivery window, a phone
        number. Prose can be judged loosely, but "₹550 for 250g" is either in
        the approved source or it is invented, and there is no third case. So
        every digit group in the answer must appear in the context; anything
        else is discarded and the customer goes to a human instead.

        Separators are stripped before comparing so "₹1,500" in the answer
        still matches "₹1500" in the source.
        """
        if not answer:
            return False, "empty_answer"

        def digit_groups(text: str) -> set:
            collapsed = re.sub(r'(?<=\d)[,\s](?=\d)', '', text or "")
            return set(re.findall(r'\d+', collapsed))

        answer_numbers = digit_groups(answer)
        context_numbers = digit_groups(" ".join(context_chunks or []))
        if not answer_numbers.issubset(context_numbers):
            return False, "unsupported_number"

        # Numbers catch fabricated prices and times, but a model can still add
        # a harmful qualitative claim: "free delivery", "nut-free", "available
        # nationwide". Protected claims must be present in the source wording.
        normalise = lambda value: " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))
        answer_text = normalise(answer)
        context_text = normalise(" ".join(context_chunks or []))
        protected_claims = (
            "free delivery", "complimentary delivery", "discount", "cash on delivery",
            "same day", "next day", "nationwide", "all india", "outside delhi ncr",
            "nut free", "gluten free", "sugar free", "dairy free", "vegan", "halal",
            "no allergens", "refund", "return policy", "in stock", "out of stock",
        )
        for claim in protected_claims:
            if claim in answer_text and claim not in context_text:
                return False, f"unsupported_claim:{claim}"

        # Claim categories whose presence is itself meaningful. A generated
        # reply may paraphrase approved prose, but it may not introduce a new
        # ingredient, locality, policy, contact address, product, or packaging
        # property that the selected knowledge never mentioned.
        protected_terms = {
            "location": {
                "delhi", "new delhi", "gurgaon", "gurugram", "noida",
                "greater noida", "ghaziabad", "faridabad", "kolkata",
                "bangalore", "bengaluru", "mumbai", "pune", "hyderabad",
                "chennai", "jaipur", "ahmedabad", "chandigarh", "lucknow",
            },
            "ingredient": {
                "almond", "almonds", "cashew", "cashews", "pistachio",
                "pistachios", "peanut", "peanuts", "gluten", "dairy",
                "milk", "chhana", "sugar", "jaggery", "nolen gur", "ghee",
                "mustard oil", "egg", "eggs",
            },
            "storage": {
                "refrigerate", "refrigerated", "refrigeration", "fridge",
                "room temperature", "shelf life", "expiry", "freeze", "frozen",
            },
            "availability": {
                "available today", "currently available", "sold out",
                "in stock", "out of stock", "limited stock",
            },
            "packaging": {
                "plastic free", "cornstarch", "eco friendly", "food safe",
                "vacuum packed", "gift wrapped", "gift wrapping",
            },
        }
        for category, terms in protected_terms.items():
            for term in terms:
                if term in answer_text and term not in context_text:
                    return False, f"unsupported_{category}:{term}"

        answer_emails = set(re.findall(
            r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", (answer or "").lower()
        ))
        context_emails = set(re.findall(
            r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", " ".join(context_chunks or []).lower()
        ))
        if not answer_emails.issubset(context_emails):
            return False, "unsupported_contact"

        # Product names are drawn from the menu rather than maintained in a
        # second allowlist. Only names actually mentioned by the generated
        # answer need to be present in the retrieved excerpts.
        product_names = set()
        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"item", "flavour", "name"} and isinstance(child, str):
                        normalized = normalise(child)
                        if len(normalized) >= 4:
                            product_names.add(normalized)
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(self.menu_data)
        for product in product_names:
            if product in answer_text and product not in context_text:
                return False, f"unsupported_product:{product}"

        # Everything above names a category of claim in advance, which cannot
        # cover the claim nobody thought of: "we prepare Jolbhora fresh every
        # morning" contains no protected keyword and no number. So the last
        # check is coverage — every word in the answer that carries meaning of
        # its own has to be present in the approved excerpts. Paraphrasing is
        # allowed; introducing a subject the sources never mentioned is not.
        # Refinement rewrites one already-approved template into conversational
        # wording, so it is checked for new claims but not for full coverage.
        # Generation from retrieval has no such anchor and is checked strictly.
        if strict:
            unsupported = self._unsupported_content_word(answer, context_chunks)
            if unsupported:
                return False, f"unsupported_statement:{unsupported}"

        return True, "grounded"

    # Words that assert nothing on their own, so an answer may use them freely.
    _CONVERSATIONAL_VOCABULARY = frozenset({
        "hello", "hallo", "namaste", "thanks", "thank", "please", "happy",
        "help", "would", "like", "love", "sure", "great", "here", "there",
        "this", "that", "these", "those", "your", "yours", "ours", "with",
        "from", "about", "also", "just", "when", "what", "which", "them",
        "they", "have", "having", "been", "will", "shall", "should", "could",
        "cannot", "does", "doesn", "don", "isn", "aren", "let", "tell",
        "know", "need", "want", "were", "was", "are", "and", "the", "for",
        "you", "our", "we", "us", "it", "is", "to", "of", "in", "on", "at",
        "any", "all", "some", "more", "than", "then", "them", "their",
        "message", "chat", "whatsapp", "team", "kitchen", "camellia",
        "artisanal", "hi", "hey", "yes", "no", "not", "can", "may", "much",
        "many", "how", "why", "who", "where", "well", "good", "sorry",
        "welcome", "again", "back", "each", "every", "both", "very", "still",
        "same", "other", "another", "next", "please", "share", "ask", "asked",
        "confirm", "confirmed", "let's", "lets", "get", "got", "give", "made",
        # Discourse words that frame an answer without adding a claim to it:
        # "here are the pricing details" says nothing the approved copy did
        # not already say. Words that name a property of the food, a time, a
        # place or a policy are deliberately absent.
        "details", "detail", "information", "info", "pricing", "list",
        "options", "option", "below", "above", "following", "these", "those",
        "note", "happy", "glad", "choose", "choice", "prefer", "would",
        "like", "want", "know", "see", "send", "share", "reply", "ask",
        "hello", "thanks", "welcome", "sure", "yes", "please",
        # Question words ask, they do not assert.
        "where", "when", "which", "what", "who", "whom", "whose", "why",
        # Structural/enumeration words caught rejecting real live-Groq
        # paraphrases of already-approved coverage lists ("this includes
        # Delhi, Gurgaon...", "charges are applied"): the word itself names
        # no fact. Each specific place, price or policy an enumeration
        # actually lists is still checked on its own — a location, a number,
        # a protected claim — so loosening these cannot let a new one in.
        "product", "products", "including", "include", "includes", "included",
        "covers", "covered", "covering", "applied", "apply", "applies",
        "reach", "reaches", "reached", "order", "orders", "ordering",
    })

    def _unsupported_content_word(
        self, answer: str, context_chunks: List[str],
    ) -> Optional[str]:
        def words(text: str) -> set:
            return {
                token for token in re.findall(r"[a-z]+", (text or "").lower())
                if len(token) >= 4
            }

        context_words = words(" ".join(context_chunks or []))
        for word in sorted(words(answer) - self._CONVERSATIONAL_VOCABULARY):
            if word in context_words:
                continue
            stem = word[:-1] if word.endswith("s") else word
            stem = stem[:-1] if stem.endswith("e") else stem
            if len(stem) >= 4 and any(
                candidate.startswith(stem) or stem.startswith(candidate[:len(stem)])
                for candidate in context_words
                if len(candidate) >= 4
            ):
                continue
            return word
        return None

    def is_grounded_in_context(
        self, answer: str, context_chunks: List[str], strict: bool = False,
    ) -> bool:
        return self.grounding_report(answer, context_chunks, strict=strict)[0]

    def menu_grounding_report(self, answer: str, approved_sentences: List[str]) -> Tuple[bool, str]:
        """Conservative menu gate: preserve the product/quantity/price binding.

        A bag-of-numbers check alone permits swapping two products' prices.
        Menu answers need no creative generation: exact approved statements
        are the deterministic fallback as well as the generation allowlist.
        """
        normalize = lambda value: re.sub(r"\s+", " ", value.strip().rstrip(".!"))
        allowed = {normalize(s) for s in approved_sentences}
        valid = normalize(answer) in allowed
        return valid, "approved_menu_exact_facts" if valid else "unsupported_menu_claim"

    def retrieve_approved_menu(self, user_message: str) -> RAGResult:
        from camellia.menu_review import customer_versions as current_versions, menu_question_blocked
        from camellia import weekly_savoury
        result = RAGResult()
        if menu_question_blocked(user_message):
            result.proposed_intent = "approved_menu"
            result.contains_unapproved_facts = True
            return result
        message = weekly_savoury._name(user_message)
        weekly_request = bool(re.search(r"\b(?:this week|weekly|savoury)\b", message)) and not bool(
            re.search(r"\b(?:aamar mishti|signature collection|bento)\b", message))
        weekly_fact_request = weekly_request or bool(re.search(r"\b(?:menu|price|cost|much)\b", message)
                                                  and weekly_savoury.historical_item_match(message))
        if weekly_fact_request and re.search(r"\b(?:menu|price|cost|much|week)\b", message) and weekly_savoury.lifecycle_enabled() and not weekly_savoury.current_version():
            result.proposed_intent = "approved_menu"
            result.contains_unapproved_facts = True
            return result
        matches = []
        matched_labels = {}
        for version in current_versions():
            approval = json.loads(version["approval_json"])
            for row in approval["rows"]:
                names = [row["item"], *(row.get("aliases") or [])]
                labels = [weekly_savoury.price_name_match(message, name) for name in names if name]
                labels = [label for label in labels if label]
                if labels:
                    matches.append((version, row))
                    matched_labels[id(row)] = labels
        if not matches:
            for version in current_versions():
                for row in json.loads(version["approval_json"])["rows"]:
                    labels = [weekly_savoury.price_name_match(message, name, typo=True)
                              for name in [row["item"], *(row.get("aliases") or [])] if name]
                    labels = [label for label in labels if label]
                    if labels:
                        matches.append((version, row))
                        matched_labels[id(row)] = labels
            if len(matches) > 1:
                result.proposed_intent = "approved_menu"
                result.has_conflict = True
                return result
        if weekly_request and any(v["menu_key"] in weekly_savoury.LEGACY_WEEKLY_KEYS for v, _ in matches):
            matches = [(v, r) for v, r in matches if v["menu_key"] in weekly_savoury.LEGACY_WEEKLY_KEYS]
        if not matches and weekly_savoury.lifecycle_enabled() and re.search(r"\b(?:price|cost|much|menu)\b", message) and weekly_savoury.historical_item_match(message):
            result.proposed_intent = "approved_menu"
            result.contains_unapproved_facts = True
            return result
        if not matches:
            return result
        # Never choose an arbitrary menu/pack when the identity is ambiguous.
        quantities = re.findall(r"\b\d+(?:\.\d+)?\s*(?:kg|g|pcs|pieces|box|pack)\b", message)
        if quantities:
            matches = [(v, r) for v, r in matches if re.sub(r"\s", "", r["quantity"].lower()) in
                       {re.sub(r"\s", "", q) for q in quantities}]
        result.proposed_intent = "approved_menu"
        result.approval_status = "APPROVED_MENU"
        if not matches and re.search(r"\b(?:aamar mishti|signature collection)\b", message):
            menu_id = "aamar_mishti" if "aamar mishti" in message else "signature"
            if re.search(r"\b(?:menu|price|prices|cost|flavours?|items?)\b", message):
                rows = [r for v in current_versions() if v["menu_key"] == menu_id
                        for r in json.loads(v["approval_json"])["rows"]]
                if rows:
                    sentences = [f"{r['item']}{(' ' + r['quantity']) if r['quantity'] else ''} is ₹{r['price_inr']}." for r in rows]
                    result.source_chunks = sentences
                    result.source_refs = result.retrieved_sources = [f"approved_menu:{menu_id}:version:{v['id']}"
                        for v in current_versions() if v["menu_key"] == menu_id]
                    result.proposed_response = result.auto_response = "Current approved menu:\n" + "\n".join(sentences)
                    result.confidence_score = result.similarity_score = 1.0
                    return result
                return result
        if not matches:
            result.proposed_intent = "unknown_intent"
            return result
        if len(matches) > 1 and len({version["menu_key"] for version, _ in matches}) == 1 and not quantities:
            menu_key = matches[0][0]["menu_key"]
            rows = [row for version, row in matches if version["menu_key"] == menu_key]
            version_ids = sorted({version["id"] for version, _ in matches})
            sentences = [f"{row['item']}{(' ' + row['quantity']) if row['quantity'] else ''} is ₹{row['price_inr']}." for row in rows]
            result.source_chunks = sentences
            result.source_refs = result.retrieved_sources = [f"approved_menu:{menu_key}:version:{item_id}" for item_id in version_ids]
            words = set(re.findall(r"\w+", re.sub(re.escape(matches[0][1]["item"].casefold()), "", message)))
            allowed = {"how", "much", "is", "the", "what", "does", "cost", "price", "for", "please", "pls", "of", "tell", "me"}
            if weekly_request:
                allowed |= {"this", "week", "weekly", "savoury", "menu", "current"}
            if words <= allowed and words & {"price", "cost", "much"}:
                result.proposed_response = result.auto_response = "Current approved prices:\n" + "\n".join(sentences)
                result.confidence_score = result.similarity_score = 1.0
            return result
        if len(matches) != 1:
            result.has_conflict = True
            return result
        version, row = matches[0]
        ref = f"approved_menu:{version['menu_key']}:version:{version['id']}:item:{row['item']}"
        result.source_refs = result.retrieved_sources = [ref]
        remaining = message
        for name in matched_labels[id(row)]:
            remaining = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", "", remaining)
        if row["quantity"]:
            remaining = re.sub(re.escape(row["quantity"].casefold()), "", remaining)
        words = set(re.findall(r"\w+", remaining))
        allowed = {"how", "much", "is", "the", "what", "does", "cost", "price", "for", "please", "pls", "of", "tell", "me"}
        if weekly_request and version["menu_key"] in weekly_savoury.LEGACY_WEEKLY_KEYS:
            allowed |= {"this", "week", "weekly", "savoury", "menu", "current"}
        if not words <= allowed or not words & {"price", "cost", "much"}:
            result.contains_unapproved_facts = True
            return result
        quantity = f" {row['quantity']}" if row["quantity"] else ""
        answer = f"{row['item']}{quantity} is ₹{row['price_inr']}."
        result.source_chunks = [answer]
        result.proposed_response = answer
        if self.menu_grounding_report(answer, result.source_chunks)[0]:
            result.auto_response = answer
            result.confidence_score = result.similarity_score = 1.0
        return result

    def retrieve(self, user_message: str, *, persist_candidate: bool = True) -> RAGResult:
        """
        Retrieves relevant context chunks strictly from approved source documents,
        calculates similarity/confidence scores, checks for conflicting sources,
        and (when ``persist_candidate`` is true) creates a candidate template
        with status 'PENDING_APPROVAL'.
        """
        result = self.retrieve_approved_menu(user_message)
        if result.proposed_intent == "approved_menu":
            return result
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
        from camellia.template_retirement import customer_templates
        templates = customer_templates(self.qa_data.get("templates", []))
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
                    message_hash = hashlib.sha256(str(user_message or "").encode("utf-8")).hexdigest()[:12]
                    log.warning(
                        "RAG retrieved conflicting sources for message_hash=%s: %s vs %s",
                        message_hash, top["source_ref"], second["source_ref"],
                    )
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
        if persist_candidate and result.confidence_score >= MEDIUM_CONFIDENCE_THRESHOLD and not result.auto_response:
            try:
                candidate_id = db.save_candidate_template(
                    name=result.proposed_intent,
                    response=result.proposed_response,
                    user_message=user_message,
                    source_refs=result.source_refs,
                    confidence=result.confidence_score,
                    created_by="rag_fallback"
                )
                if candidate_id is not None:
                    result.candidate_id = candidate_id
                    result.approval_status = "PENDING_APPROVAL"
                    message_hash = hashlib.sha256(str(user_message or "").encode("utf-8")).hexdigest()[:12]
                    log.info(
                        "RAG Candidate Template #%d created for message_hash=%s "
                        "(status: PENDING_APPROVAL, confidence: %.2f)",
                        candidate_id, message_hash, result.confidence_score,
                    )
            except Exception as e:
                log.error("Failed to save RAG candidate template: %s", e)

        return result

rag_fallback_engine = RAGFallbackEngine()
