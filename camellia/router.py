"""
The LLM router, running in shadow.

The deterministic matcher needs a customer to phrase a question the way one of
1,097 trigger phrases expects. "Hi can I get the menu?" nearly missed for that
reason. The router replaces the guessing of phrasings with understanding: it is
shown the customer's message and the catalogue of approved answers, and picks
which one fits.

It never writes the reply. It returns a template id, and the approved text for
that id ships verbatim — the client requires their copy be sent as written, so
the model does the understanding and never touches the wording.

In this phase it is not connected to replies at all. It picks in silence beside
the keyword matcher, both picks are recorded, and the disagreements are the
measurement that decides whether it is trusted with live traffic. Two rules make
that safe to leave running:

  - It runs off the reply path, in a small bounded thread pool. A customer must
    never wait on a call whose result is discarded.
  - Every failure is swallowed. A shadow measurement that can break a live reply
    is not a shadow.
"""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from camellia.config import config
from camellia.database import db
from camellia.monitoring import get_logger

log = get_logger("camellia.router")

# One line per template: the id, then enough of its answer to tell the model
# what that id actually covers. Built from the templates themselves rather than
# hand-written, so a new or edited template cannot silently fall out of the
# catalogue the way a stale hand-maintained list would.
_DESCRIPTION_CHARS = 90

# Two workers is enough to absorb bursts without letting a slow provider pile up
# unbounded threads behind it.
_SHADOW_WORKERS = 2
_SHADOW_QUEUE_LIMIT = 64


def _first_sentence(text: str, limit: int = _DESCRIPTION_CHARS) -> str:
    """A compact gist of an answer, for the catalogue line."""
    flat = " ".join((text or "").split())
    flat = re.sub(r'[*_`]', '', flat)
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    for sep in (". ", "! ", "? ", " — ", ", "):
        idx = cut.rfind(sep)
        if idx > limit * 0.5:
            return cut[:idx].strip()
    return cut.rsplit(" ", 1)[0] + "…"


class ShadowRouter:
    """Picks a template id for a message, and records that pick for review."""

    def __init__(self):
        self._pool: Optional[ThreadPoolExecutor] = None
        self._pending = 0
        self._lock = threading.Lock()
        self._catalog_cache: Optional[Tuple[int, str, List[str]]] = None

    # -- catalogue ---------------------------------------------------------

    def build_catalog(self, templates: List[Dict[str, Any]]) -> Tuple[str, List[str]]:
        """
        Returns (catalogue text, valid ids).

        Escalation templates and GUARDs are deliberately included: the router
        needs to be able to say "this belongs to the guarded answer", and a
        catalogue that hides them would push those messages onto a wrong id.
        """
        cached = self._catalog_cache
        if cached and cached[0] == len(templates):
            return cached[1], cached[2]

        lines: List[str] = []
        ids: List[str] = []
        for tmpl in templates:
            tmpl_id = tmpl.get("id")
            if not tmpl_id:
                continue
            answer = tmpl.get("response") or ""
            if not answer:
                responses = tmpl.get("responses") or []
                answer = responses[0] if responses else ""
            # A derived gist describes what an answer *says*, which for most
            # templates is enough to route by. For a handful it is actively
            # misleading — "Here are the menus from the past couple of weeks"
            # reads like the answer to "can I get the menu?" when it is only
            # for someone asking for an *earlier* week. `router_hint` overrides
            # the gist for those, and is written only where it is needed rather
            # than for all 58, which would rot the moment a template changed.
            gist = tmpl.get("router_hint") or _first_sentence(answer) or tmpl_id.replace("_", " ")
            lines.append(f"{tmpl_id}: {gist}")
            ids.append(tmpl_id)

        catalog = "\n".join(lines)
        self._catalog_cache = (len(templates), catalog, ids)
        return catalog, ids

    # -- the pick ----------------------------------------------------------

    def classify(
        self,
        user_message: str,
        templates: List[Dict[str, Any]],
        history_turns: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Optional[str], str]:
        """
        Asks the model which approved answer fits. Returns (template_id, detail).

        `None` is a real answer, not a failure: nothing in the catalogue
        covering the question is exactly what the escalation path is for, and a
        router that always picks something would route those to whatever fits
        worst.
        """
        # Imported here rather than at module scope: ai_responder imports
        # database, and a top-level cycle through this module would bite at
        # import time rather than at first use.
        from camellia.ai_responder import ai_responder

        if not user_message or not user_message.strip():
            return None, "empty_message"
        if ai_responder.get_active_provider() == "none":
            return None, "provider_unavailable"

        catalog, valid_ids = self.build_catalog(templates)
        if not catalog:
            return None, "empty_catalog"

        history_block = ""
        if history_turns:
            recent = history_turns[-4:]
            history_block = "\nRECENT CONVERSATION:\n" + "\n".join(
                f"{t.get('role', '')}: {' '.join(str(t.get('content', '')).split())[:200]}"
                for t in recent
            ) + "\n"

        prompt = (
            "You route customer messages for Camellia Artisanal, a Bengali sweets "
            "and meals kitchen in Gurgaon, to a pre-approved answer.\n\n"
            "Below is every answer available, as `id: what it covers`.\n\n"
            f"--- AVAILABLE ANSWERS ---\n{catalog}\n---\n"
            f"{history_block}\n"
            "Reply with ONLY the id of the single answer that best fits the "
            "customer's message.\n"
            "If the conversation shows the customer is replying to a question the "
            "assistant just asked, pick the answer that fits their reply in that "
            "context.\n"
            "If no answer genuinely covers the message, reply with exactly NONE. "
            "Choosing NONE is correct and expected — a wrong answer is worse than "
            "handing the customer to a human.\n"
            "No explanation, no punctuation, just the id or NONE.\n\n"
            f"Customer message: {user_message}"
        )

        raw, ok, reason = ai_responder._query_groq(prompt, is_refinement=True)
        if not ok:
            return None, f"provider_error:{reason}"

        # The model is asked for a bare id; accept a little untidiness around it
        # rather than throwing away an otherwise correct pick.
        candidate = (raw or "").strip().strip("`\"'. \n").split()[0] if (raw or "").strip() else ""
        if candidate.upper() == "NONE":
            return None, "router_says_none"
        if candidate in valid_ids:
            return candidate, "ok"
        # A hallucinated id is a real failure mode worth seeing in the data
        # rather than quietly coercing into the nearest real one.
        return None, f"invalid_id:{candidate[:40]}"

    # -- shadow recording --------------------------------------------------

    def _pool_or_none(self) -> Optional[ThreadPoolExecutor]:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=_SHADOW_WORKERS, thread_name_prefix="router-shadow"
            )
        return self._pool

    def record_shadow(
        self,
        user_message: str,
        channel: str,
        keyword_pick: Optional[str],
        templates: List[Dict[str, Any]],
        history_turns: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """
        Fires the comparison in the background. Returns immediately.

        Never raises. The customer's reply has already been decided by the time
        this is called, and nothing here may put that at risk.
        """
        if not config.ROUTER_SHADOW_ENABLED:
            return
        try:
            with self._lock:
                if self._pending >= _SHADOW_QUEUE_LIMIT:
                    log.warning("Router shadow queue is full; skipping this message.")
                    return
                self._pending += 1

            pool = self._pool_or_none()
            pool.submit(
                self._run_comparison, user_message, channel, keyword_pick,
                templates, history_turns,
            )
        except Exception as e:
            with self._lock:
                self._pending = max(0, self._pending - 1)
            log.warning("Could not queue a router shadow comparison: %s", e)

    def _run_comparison(
        self,
        user_message: str,
        channel: str,
        keyword_pick: Optional[str],
        templates: List[Dict[str, Any]],
        history_turns: Optional[List[Dict[str, Any]]],
    ) -> None:
        started = time.perf_counter()
        router_pick, detail = None, "unrun"
        try:
            router_pick, detail = self.classify(user_message, templates, history_turns)
        except Exception as e:
            detail = f"exception:{type(e).__name__}"
            log.warning("Router shadow comparison failed: %s", e)
        finally:
            with self._lock:
                self._pending = max(0, self._pending - 1)

        try:
            db.record_router_shadow(
                channel=channel,
                user_message=user_message,
                keyword_pick=keyword_pick,
                router_pick=router_pick,
                detail=detail,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except Exception as e:
            log.warning("Could not record a router shadow comparison: %s", e)


shadow_router = ShadowRouter()
