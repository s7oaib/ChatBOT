"""
The AI Resolution triage classifier, running in shadow.

Every message that reaches this module has already failed deterministic and
canonical resolution — the existing candidate_templates/escalations/
knowledge_gaps pipeline in responder.py has already decided what to do about
it. This asks a model to categorize the same message into one of seven review
buckets, purely so an admin can compare "what the pipeline actually did"
against "what a triage model would have called it" before any auto-send path
exists.

It never writes a reply and never creates or changes a row a customer's
experience depends on — it only ever writes to triage_shadow, which nothing
in the reply path reads. Same shape and the same two safety rules as
camellia.router.ShadowRouter, which this was modelled on:

  - It runs off the reply path, in a small bounded thread pool. A customer
    must never wait on a call whose result is discarded.
  - Every failure is swallowed. A shadow measurement that can break a live
    reply is not a shadow.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from camellia.config import config
from camellia.database import db
from camellia.monitoring import get_logger

log = get_logger("camellia.triage")

CATEGORIES = (
    "AUTO_RESOLVE",
    "AI_SUGGESTED",
    "TEACH_REQUIRED",
    "HUMAN_REQUIRED",
    "NON_ACTIONABLE",
    "CONTACT_DETAIL",
    "UNSUPPORTED",
)

_CATEGORY_GUIDE = """
AUTO_RESOLVE - a verified answer already covers this exactly and fully; would be safe to send with no human involved.
AI_SUGGESTED - the bot can draft a plausible answer, but a person should check it before it is trusted.
TEACH_REQUIRED - nobody has ever taught the bot an answer for this; it is a genuine knowledge gap.
HUMAN_REQUIRED - the customer needs a person regardless of whether the knowledge exists: an order issue, a complaint, an explicit request to talk to someone, or anything sensitive or session-specific.
NON_ACTIONABLE - no substantive reply is needed: greetings, thanks, small talk, emoji, or promotional/influencer spam.
CONTACT_DETAIL - the message is a phone number, address, or other contact info being shared, not a question.
UNSUPPORTED - the request is entirely outside what this business does; not a gap worth teaching, just not something it offers.
""".strip()

_SHADOW_WORKERS = 2
_SHADOW_QUEUE_LIMIT = 64


class TriageClassifier:
    """Categorizes a message into one of seven review buckets, in shadow."""

    def __init__(self):
        self._pool: Optional[ThreadPoolExecutor] = None
        self._pending = 0
        self._lock = threading.Lock()

    # -- the classification --------------------------------------------

    def classify(self, user_message: str) -> Dict[str, Any]:
        """
        Asks the model to categorize one message. Never raises — a provider
        failure or a malformed reply comes back as `ok: False`, not an
        exception.
        """
        from camellia.ai_responder import ai_responder

        if not user_message or not user_message.strip():
            return {"ok": False, "reason": "empty_message"}
        if ai_responder.get_active_provider() == "none":
            return {"ok": False, "reason": "provider_unavailable"}

        prompt = (
            "You triage one customer message for Camellia Artisanal, a Bengali "
            "sweets and meals kitchen in Gurgaon, into exactly one review "
            "category. This is a measurement only — nothing you say is sent "
            "to the customer.\n\n"
            f"--- CATEGORIES ---\n{_CATEGORY_GUIDE}\n---\n\n"
            "Reply with ONLY a JSON object of the shape "
            '{"category": "<one of the seven names above>", '
            '"confidence": <a number from 0 to 1>, '
            '"reasoning": "<one short sentence>"}\n\n'
            f"Customer message: {user_message}"
        )

        raw, ok, reason = ai_responder._query_groq(
            prompt,
            is_refinement=True,
            model=config.LLM_TRIAGE_MODEL,
            response_format={"type": "json_object"},
        )
        if not ok:
            return {"ok": False, "reason": f"provider_error:{reason}"}

        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "reason": "unparseable_response"}

        category = str(parsed.get("category", "")).strip().upper()
        if category not in CATEGORIES:
            return {"ok": False, "reason": f"invalid_category:{category[:40]}"}

        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        reasoning = str(parsed.get("reasoning", "") or "")[:300]

        return {"ok": True, "category": category, "confidence": confidence, "reasoning": reasoning}

    def _grounding_for(self, user_message: str) -> Dict[str, Any]:
        """
        Looks for an existing approved answer and checks whether it is
        actually grounded for this message, reusing the same anti-
        hallucination gate (`rag_fallback.grounding_report`) the live serving
        path already trusts — not a new grounding rule invented for this.
        """
        from camellia.rag_fallback import rag_fallback_engine

        match = db.search_learned_knowledge(user_message)
        if not match:
            return {
                "matched_knowledge_id": None,
                "matched_knowledge_source": None,
                "matched_knowledge_preview": None,
                "grounded": False,
                "grounding_reason": "no_matched_knowledge",
            }

        answer = match.get("verified_answer") or ""
        grounded, reason = rag_fallback_engine.grounding_report(answer, [answer], strict=True)
        return {
            "matched_knowledge_id": match.get("id"),
            "matched_knowledge_source": match.get("source"),
            "matched_knowledge_preview": answer[:200],
            "grounded": grounded,
            "grounding_reason": reason,
        }

    # -- shadow recording -----------------------------------------------

    def _pool_or_none(self) -> Optional[ThreadPoolExecutor]:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=_SHADOW_WORKERS, thread_name_prefix="triage-shadow"
            )
        return self._pool

    def record_shadow(
        self,
        user_message: str,
        channel: str,
        user_id: str,
        was_matched: bool,
        template_id: Optional[str],
    ) -> None:
        """
        Fires the triage classification in the background. Returns
        immediately. Never raises — the customer's reply has already been
        decided by the time this is called.
        """
        if not config.LLM_TRIAGE_ENABLED:
            return
        try:
            with self._lock:
                if self._pending >= _SHADOW_QUEUE_LIMIT:
                    log.warning("Triage shadow queue is full; skipping this message.")
                    return
                self._pending += 1

            pool = self._pool_or_none()
            pool.submit(self._run_triage, user_message, channel, user_id, was_matched, template_id)
        except Exception as e:
            with self._lock:
                self._pending = max(0, self._pending - 1)
            log.warning("Could not queue a triage shadow classification: %s", e)

    def _run_triage(
        self,
        user_message: str,
        channel: str,
        user_id: str,
        was_matched: bool,
        template_id: Optional[str],
    ) -> None:
        started = time.perf_counter()
        result: Dict[str, Any] = {"ok": False, "reason": "unrun"}
        grounding: Dict[str, Any] = {}
        try:
            result = self.classify(user_message)
            grounding = self._grounding_for(user_message)
        except Exception as e:
            result = {"ok": False, "reason": f"exception:{type(e).__name__}"}
            log.warning("Triage shadow classification failed: %s", e)
        finally:
            with self._lock:
                self._pending = max(0, self._pending - 1)

        category = result.get("category") if result.get("ok") else None
        confidence = result.get("confidence") if result.get("ok") else None
        would_auto_resolve = bool(
            category == "AUTO_RESOLVE"
            and confidence is not None
            and confidence >= config.LLM_TRIAGE_CONFIDENCE_THRESHOLD
            and grounding.get("grounded")
        )

        try:
            db.record_triage_shadow(
                channel=channel,
                user_id=user_id,
                user_message=user_message,
                category=category,
                confidence=confidence,
                reasoning=result.get("reasoning"),
                ok=bool(result.get("ok")),
                detail=result.get("reason", "ok"),
                matched_knowledge_id=grounding.get("matched_knowledge_id"),
                matched_knowledge_source=grounding.get("matched_knowledge_source"),
                matched_knowledge_preview=grounding.get("matched_knowledge_preview"),
                grounded=bool(grounding.get("grounded", False)),
                grounding_reason=grounding.get("grounding_reason"),
                would_auto_resolve=would_auto_resolve,
                was_matched=was_matched,
                template_id=template_id,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                model=config.LLM_TRIAGE_MODEL,
            )
        except Exception as e:
            log.warning("Could not record a triage shadow classification: %s", e)


triage_classifier = TriageClassifier()
