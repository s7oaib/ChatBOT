import hashlib
import json
import re
import time
import httpx
from collections import OrderedDict
from typing import Tuple, Dict, Any, List, Optional
from camellia.config import config
from camellia.database import db
from camellia.monitoring import get_logger

log = get_logger("camellia.ai_responder")

# Provider failures split into two kinds, and they deserve very different
# cooldowns.
#
# Transient: the provider is up but busy or briefly broken. Worth retrying
# within the minute.
TRANSIENT_HTTP_STATUSES = (429, 500, 502, 503)
TRANSIENT_COOLDOWN_SECONDS = 60.0
# Configuration: a bad key, a revoked key, or a model name the provider does
# not serve. Retrying changes nothing — the deployment has to change. These
# were previously left out of the cooldown entirely, so the provider kept
# reporting itself available and every single customer message paid a full
# failed round trip to the API before falling back to the template. Measured
# at ~145ms per message against a live invalid key.
#
# 413 belongs here too: on a TPM-capped plan (Groq free tier allows only
# ~6-8k tokens per minute) a prompt that exceeds the limit can never succeed,
# no matter how many times it is retried. Without the cooldown every unmatched
# customer message paid a full ~13k-token round trip to a guaranteed 413.
CONFIG_ERROR_HTTP_STATUSES = (400, 401, 403, 404, 413)
CONFIG_ERROR_COOLDOWN_SECONDS = 600.0

# Char ceiling for the full-RAG context. Groq's tokenizer ran ~3.07 chars/token
# over this corpus (41,288 chars → 13,454 tokens, rejected as HTTP 413), so
# 16,000 chars ≈ 5.2k tokens leaves headroom under even the tightest free-tier
# per-minute limit (~6k TPM) once the system prompt and customer message are
# added. Pinned by test_rag_prompt_stays_under_provider_token_budget.
_RAG_CONTEXT_CHAR_BUDGET = 16000


# A 429 on the free tier is a burst, not a fault: the minute's token budget is
# spent and the next minute is fine. Parking the provider for the full 60s on
# the first one drops every customer in that window back to unrefined
# templates. A rejection is also cheap — the server refuses immediately rather
# than holding the connection — so one short retry usually lands.
#
# The retry must never widen the reply budget. Refinement is capped below
# SLOW_REPLY_THRESHOLD_MS on purpose, so overrunning it would tell customers
# the business is running slow to save a cosmetic rewording. Every retry is
# therefore checked against the deadline of the attempt it belongs to, and
# skipped when there is not enough left for a real second try.
_RETRY_BACKOFF_SECONDS = 0.25
_MIN_RETRY_ATTEMPT_SECONDS = 0.4


def _retry_after_seconds(resp: Any) -> Optional[float]:
    """The provider's own Retry-After hint, when it sends a usable one."""
    try:
        raw = resp.headers.get("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        # Retry-After may also be an HTTP date; no cheap parse, so ignore it
        # and fall back to the fixed backoff.
        return None


def _cooldown_for_status(status_code: int) -> float:
    """Seconds to stop calling a provider after an HTTP failure. 0 = don't."""
    if status_code in CONFIG_ERROR_HTTP_STATUSES:
        return CONFIG_ERROR_COOLDOWN_SECONDS
    if status_code in TRANSIENT_HTTP_STATUSES:
        return TRANSIENT_COOLDOWN_SECONDS
    return 0.0


# --- Refinement gating -----------------------------------------------------
# Refinement is cosmetic: it rewords copy a human already approved. It is not
# worth a network round trip on a reply that states a price or draws a table.
#
# Two reasons. Accuracy first — a model asked to reword "₹600 / 250g" can
# reformat the number, reflow the monospace table, or drop a row, and that
# lands in front of a paying customer as a wrong price. Speed second — these
# are the longest templates in the file, so they are the slowest and most
# expensive ones to send.
_PRICE_OR_TABLE_RE = re.compile(r'₹|```|\bRs\.?\s*\d|\bINR\b', re.IGNORECASE)

# Repeat traffic on this bot is heavy ("sondesh price", "do you deliver to
# noida"), and refining the same template for the same question produces the
# same text. Caching it turns most refinements into a dict lookup instead of a
# call to the provider.
_REFINEMENT_CACHE_MAX = 512

# How much of the conversation the refiner is shown. Without it the model
# cannot tell an answer from a fresh question: the bot asks "Which area are you
# in?", the customer replies "gurgaon", that re-matches delivery_area, and the
# model — seeing only the template and the word "gurgaon" — restates the
# template verbatim and asks the same question a second time. Four turns is
# enough to cover the ask/answer pair that causes this, and the per-line cut
# keeps a long menu reply from crowding out the rest under Groq's TPM ceiling.
_HISTORY_TURNS_IN_PROMPT = 4
_HISTORY_LINE_CHARS = 300


def _should_refine(template_text: str) -> bool:
    """False for templates whose exact wording is load-bearing."""
    return not _PRICE_OR_TABLE_RE.search(template_text or "")

class AIResponder:
    """
    Groq-backed RAG & Template Refinement Engine.

    Uses Groq's OpenAI-compatible chat completions endpoint (model:
    openai/gpt-oss-20b by default) for fast, context-grounded customer
    support answers based strictly on menu.json + qa_templates.json.
    Enforces strict refusal ('OUT_OF_CONTEXT') for out-of-syllabus or unlisted
    queries, triggering human escalation.
    """

    def __init__(self, menu_path: str = None, qa_path: str = None):
        self.menu_path = menu_path or config.MENU_JSON_PATH
        self.qa_path = qa_path or config.QA_TEMPLATES_PATH
        self.menu_data = self._load_json(self.menu_path)
        self.qa_data = self._load_json(self.qa_path)
        self._groq_cooldown_until: float = 0.0
        # Outcome of the most recent provider call. Holding a key is not the
        # same as the key working — a bad key or an unserved model name answers
        # instantly with an HTTP 400/404 and refines nothing. Those now trip a
        # long cooldown so the bot stops paying for the round trip, and this is
        # what /health reports so the failure is visible rather than silent.
        self._last_call_ok: Optional[bool] = None
        self._last_call_detail: str = "no call yet"
        # (template_id, normalised message, customer context) -> refined text.
        # The customer context is part of the key on purpose: a refinement can
        # address the customer by name, so a key without it would serve one
        # customer's name to the next one asking the same question.
        self._refinement_cache: "OrderedDict[tuple, str]" = OrderedDict()
        self._refinement_hits: int = 0
        self._refinement_misses: int = 0

    def _record_call(self, ok: bool, detail: str) -> None:
        self._last_call_ok = ok
        self._last_call_detail = detail

    def status(self) -> Dict[str, Any]:
        """Reports whether the AI layer is configured AND actually working."""
        provider = self.get_active_provider()
        now = time.time()
        paused = {
            name: round(until - now, 1)
            for name, until in (
                ("groq", self._groq_cooldown_until),
            )
            if until > now
        }
        return {
            "provider": provider,
            "enabled": bool(config.is_ai_enabled),
            "model": config.GROQ_MODEL,
            "last_call_ok": self._last_call_ok,
            "last_call": self._last_call_detail,
            # Unknown until a call is made, so absence of a failure is not success.
            "working": bool(self._last_call_ok) if self._last_call_ok is not None else None,
            # Seconds remaining on the provider's cooldown. A long pause here
            # means a config error (bad key / wrong model), not load.
            "paused_seconds": paused,
            "refinement_cache": {
                "size": len(self._refinement_cache),
                "hits": self._refinement_hits,
                "misses": self._refinement_misses,
            },
        }

    def _load_json(self, path: str) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error("Error loading %s: %s", path, e)
            return {}

    def reload_data(self):
        """Reload JSON context data, reset cooldowns, drop cached refinements."""
        self.menu_data = self._load_json(self.menu_path)
        self.qa_data = self._load_json(self.qa_path)
        self._groq_cooldown_until = 0.0
        # Cached refinements are rewordings of the templates that just changed.
        # Keeping them would serve the old menu's wording after a menu update.
        self._refinement_cache.clear()

    def get_active_provider(self) -> str:
        """Determines active AI provider: 'groq' or 'none'."""
        if not config.is_ai_enabled:
            return "none"
        now = time.time()
        if bool(config.GROQ_API_KEY) and (now > self._groq_cooldown_until):
            return "groq"
        return "none"

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """Collapses whitespace and cuts at a sentence or word boundary."""
        text = " ".join((text or "").split())
        if len(text) <= limit:
            return text
        cut = text[:limit]
        for sep in (". ", "! ", "? "):
            idx = cut.rfind(sep)
            if idx > limit * 0.5:
                return cut[: idx + 1].strip()
        return cut.rsplit(" ", 1)[0].rstrip(",;:") + "…"

    def _build_context_string(self) -> str:
        """
        Assembles a compact, high-density context string from menu.json and qa_templates.json.

        Deliberately truncated: the only consumer of `generate_response`'s output
        is a PENDING_APPROVAL candidate a human reviews before it ever ships, so
        the model drafts from summaries rather than the full corpus. The full
        text of every template plus the complete menu JSON measured ~41k chars
        (~13.4k tokens), which is over Groq's per-minute token ceiling on
        free-tier plans — every call returned HTTP 413. The budget below keeps
        the whole request under ~5k tokens. `tests/test_bot.py` pins this so a
        growing menu or template set cannot silently push it back over.
        """
        context_parts = []
        char_budget = _RAG_CONTEXT_CHAR_BUDGET

        # Business Details & Coverage
        context_parts.append("BUSINESS NAME: Camellia Artisanal")
        context_parts.append("TAGLINE: Bengal-Rooted Artisanal Sweets & Food")
        context_parts.append("DELIVERY AREA: Delhi NCR (Delhi, Gurgaon, Noida, Ghaziabad, Faridabad). We do NOT deliver outside Delhi NCR.")
        context_parts.append("ORDER METHOD: Direct message reply or WhatsApp message to our team.")

        # QA Syllabus / Templates — id plus the opening of each response. The
        # id tells the model which policy it is drafting against; the opening
        # carries the substance (prices usually lead).
        templates = self.qa_data.get("templates", [])
        if templates:
            context_parts.append("\n--- FREQUENTLY ASKED QUESTIONS & POLICIES (summarised) ---")
            for tmpl in templates:
                resp = tmpl.get("response")
                if not resp:
                    continue
                title = tmpl.get("id", "").replace("_", " ").title()
                context_parts.append(f"• {title}: {self._truncate(resp, 110)}")

        # Menu Details
        if self.menu_data:
            context_parts.append("\n--- MENU & PRODUCTS ---")
            collections = self.menu_data.get("collections", {})
            if collections:
                context_parts.append(f"Sondesh Collections, Flavours & Pricing: {json.dumps(collections, ensure_ascii=False)}")

            weekly = self.menu_data.get("weekly_menus", [])
            if weekly:
                # Item names and prices only — the full JSON with descriptions
                # measured 5.8k chars and is the least load-bearing detail for
                # drafting a candidate answer.
                for week in weekly:
                    lines = []
                    for key in ("vegetarian", "non_vegetarian", "bengali_bento", "combos"):
                        for itm in week.get(key) or []:
                            name = itm.get("item") or itm.get("name") or ""
                            price = itm.get("price_inr", "")
                            if name:
                                lines.append(f"{name} ₹{price}")
                    if lines:
                        week_name = week.get("week_of", "Current Rotation")
                        context_parts.append(f"{week_name}: " + "; ".join(lines))

            delivery = self.menu_data.get("delivery", {})
            if delivery:
                context_parts.append(f"Delivery & Packaging: {json.dumps(delivery, ensure_ascii=False)}")

        # Learned Knowledge & Verified Resolutions from Staff
        try:
            learned_items = db.get_learned_knowledge(limit=30)
            if learned_items:
                context_parts.append("\n--- VERIFIED KNOWLEDGE & POLICIES (LEARNED FROM STAFF) ---")
                for lk in learned_items:
                    topic = lk.get("topic", "")
                    ans = self._truncate(lk.get("verified_answer", ""), 150)
                    context_parts.append(f"• {topic}: {ans}")
        except Exception as e:
            log.debug("Could not fetch learned knowledge: %s", e)

        context = "\n".join(context_parts)

        # Defensive trim. The per-part budgets above hold for today's data, but
        # the menu and template files are live-editable — if a future edit
        # pushes the assembled context over budget, cut it here rather than
        # guaranteeing an HTTP 413 on every unmatched question again.
        if len(context) > char_budget:
            log.error(
                "RAG context is %d chars, over the %d-char budget (%s). "
                "Truncating — trim the QA/menu data or raise the budget.",
                len(context), char_budget, "Groq TPM limit",
            )
            context = context[:char_budget]
        return context

    def _query_groq(
        self,
        prompt_text: str,
        is_refinement: bool = False,
        template_id: str = "",
        model: Optional[str] = None,
    ) -> Tuple[str, bool, str]:
        """
        Queries Groq's OpenAI-compatible chat completions endpoint.

        `model` overrides GROQ_MODEL for callers whose job is not writing a
        reply — the router runs on every message and needs its own token
        budget, since rate limits on Groq are per model.
        """
        if not config.GROQ_API_KEY:
            return "", False, "groq_no_key"

        payload: Dict[str, Any] = {
            "model": model or config.GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt_text}],
            "temperature": 0.2 if is_refinement else 0.1,
            "max_tokens": 1024 if is_refinement else 2048,
        }

        headers = {
            "Authorization": f"Bearer {config.GROQ_API_KEY}",
            "Content-Type": "application/json",
        }

        # Rate limits on Groq are per model, so a router running on its own
        # model must not park the model that writes replies. A router 429 also
        # must not make /health report the AI layer as broken, since the part
        # customers depend on is still fine.
        on_side_model = bool(model) and model != config.GROQ_MODEL

        url = "https://api.groq.com/openai/v1/chat/completions"

        # Refinement is optional polish on an answer we already hold, so it gets
        # a short budget; answering an unmatched question is the only path worth
        # making the customer wait for.
        timeout = config.GROQ_REFINE_TIMEOUT if is_refinement else config.GROQ_TIMEOUT

        deadline = time.monotonic() + timeout

        try:
            with httpx.Client(timeout=timeout) as client:
                while True:
                    resp = client.post(url, json=payload, headers=headers)
                    if resp.status_code == 200:
                        break

                    # One retry, and only for a transient refusal that leaves
                    # room inside this call's own budget.
                    if resp.status_code in TRANSIENT_HTTP_STATUSES:
                        backoff = _retry_after_seconds(resp) or _RETRY_BACKOFF_SECONDS
                        remaining = deadline - time.monotonic()
                        if remaining > backoff + _MIN_RETRY_ATTEMPT_SECONDS:
                            log.info(
                                "Groq HTTP %s — retrying once in %.2fs (%.2fs of budget left).",
                                resp.status_code, backoff, remaining,
                            )
                            time.sleep(backoff)
                            # Whatever is left is all the second attempt gets,
                            # so a slow retry cannot push past the deadline.
                            client.timeout = httpx.Timeout(max(0.1, deadline - time.monotonic()))
                            resp = client.post(url, json=payload, headers=headers)
                            if resp.status_code == 200:
                                break

                    cooldown = _cooldown_for_status(resp.status_code)
                    if cooldown and not on_side_model:
                        self._groq_cooldown_until = time.time() + cooldown
                        if resp.status_code in CONFIG_ERROR_HTTP_STATUSES:
                            log.error(
                                "Groq rejected the request as misconfigured (HTTP %s, model %r). "
                                "Pausing Groq calls for %.0fs — check GROQ_API_KEY and GROQ_MODEL.",
                                resp.status_code, config.GROQ_MODEL, cooldown,
                            )
                    log.warning("Groq HTTP %s (%s): %s", resp.status_code, payload["model"], resp.text[:200])
                    if not on_side_model:
                        self._record_call(False, f"HTTP {resp.status_code}: {resp.text[:120]}")
                    return "", False, f"groq_http_{resp.status_code}"

                data = resp.json()
                choices = data.get("choices", [])
                if not choices:
                    return "", False, "groq_no_choices"

                assistant_text = choices[0].get("message", {}).get("content", "").strip()

                if "OUT_OF_CONTEXT" in assistant_text.upper() or not assistant_text:
                    fallback = self.qa_data.get("escalation_triggers", {}).get(
                        "fallback_response",
                        "Thanks for reaching out! I can only help with questions about Camellia Artisanal menu, prices, and delivery. Let me connect you with our team. 🙏"
                    )
                    return fallback, False, "out_of_context"

                if not on_side_model:
                    self._record_call(True, "ok")
                return assistant_text, True, "groq_rag"

        except Exception as e:
            if not on_side_model:
                self._groq_cooldown_until = time.time() + TRANSIENT_COOLDOWN_SECONDS
                self._record_call(False, f"{type(e).__name__}: {str(e)[:120]}")
            log.warning("Groq API call failed (%s): %s", config.GROQ_MODEL, e)
            fallback = self.qa_data.get("escalation_triggers", {}).get(
                "fallback_response",
                "Thanks for reaching out! Let me connect you with our team — they'll get back to you shortly. 🙏"
            )
            return fallback, False, f"groq_exception_{type(e).__name__}"

    def generate_response(self, user_message: str, user_context: str = None, history_turns: list = None) -> Tuple[str, bool, str]:
        """
        Queries Groq with strict RAG context grounding.

        Returns:
            (response_text: str, is_context_grounded: bool, reason: str)
        """
        # 1. Fast-Path: Check Self-Healing Learned Knowledge base
        try:
            learned_match = db.search_learned_knowledge(user_message)
            if learned_match:
                ans = learned_match.get("verified_answer", "").strip()
                if ans:
                    log.info("Answered query via Learned Knowledge (%s): %s", learned_match.get("topic"), user_message[:60])
                    return ans, True, "learned_knowledge"
        except Exception as e:
            log.debug("Error searching learned knowledge in AI responder: %s", e)

        provider = self.get_active_provider()
        if provider == "none":
            return "", False, "ai_disabled_or_no_key"

        context_str = self._build_context_string()
        user_ctx_block = f"\nACTIVE CUSTOMER PROFILE:\n{user_context}\n" if user_context else ""

        system_prompt = (
            "You are the warm, delightful, and highly knowledgeable AI virtual assistant for Camellia Artisanal — a premier Bengal-rooted boutique kitchen in Sushant Lok, Gurgaon (delivering fresh across Delhi NCR).\n\n"
            "Your personality is warm, elegant, polite, and deeply knowledgeable about artisanal sweets and authentic Bengali home cooking (like an expert concierge).\n\n"
            "GUIDELINES:\n"
            "1. Answer customer questions naturally, conversationally, and clearly based on the menu, prices, and policies in the context below.\n"
            "2. When discussing our sweets, highlight their craftsmanship: authentic pure dairy chhana, date-palm Nolen Gur from Bengal, hand-kneaded fresh to order, and A2 desi cow milk.\n"
            "3. If a customer asks for recommendations, gifting ideas, or dietary advice, provide thoughtful, appetizing suggestions based on our actual collections (Aamar Mishti & Signature A2).\n"
            "4. If a customer asks general friendly or conversational questions, respond warmly and guide them back to exploring our fresh sweets or weekly meals.\n"
            "5. If a customer asks to order, summarize their request and encourage them to confirm their items, delivery address, and preferred time.\n"
            "6. When displaying prices, menus, or flavour options, format them clearly with clean bullet points or structured monospace code-block tables (``` +---+---+ ```) for mobile & WhatsApp readability.\n"
            "7. Never use flower emoji of any kind. The client has asked for them to be removed from everything their customers see, and a draft carrying one still has to be cleaned by hand before it can be approved.\n\n"
            f"--- KNOWLEDGE CONTEXT ---\n{context_str}\n"
            f"{user_ctx_block}"
        )

        prompt_text = f"{system_prompt}\n\nCustomer Message: {user_message}"
        if history_turns:
            history_str = "\n".join([f"{t['role'].capitalize()}: {t['content']}" for t in history_turns[-4:]])
            prompt_text = f"{system_prompt}\n\nRecent Chat History:\n{history_str}\n\nCustomer Message: {user_message}"

        return self._query_groq(prompt_text, is_refinement=False)

    def answer_from_retrieved_context(
        self,
        user_message: str,
        context_chunks: List[str],
        user_context: str = None,
        history_turns: list = None,
    ) -> Tuple[str, bool, str]:
        """
        Answers a question the deterministic matcher missed, using ONLY the
        approved excerpts retrieval already selected.

        Different from `generate_response`, which drafts against a summary of
        the whole corpus for a human to review later. This one is allowed to
        reach the customer, so it is given a much narrower brief: a handful of
        approved passages and an instruction to refuse rather than fill gaps.
        The refusal path is the existing OUT_OF_CONTEXT contract in
        `_query_groq`, which returns the escalation copy with ok=False.
        """
        if not context_chunks:
            return "", False, "no_context"

        provider = self.get_active_provider()
        if provider == "none":
            return "", False, "ai_disabled_or_no_key"

        excerpts = "\n\n".join(
            f"[APPROVED EXCERPT {i + 1}]\n{chunk}"
            for i, chunk in enumerate(context_chunks)
        )
        excerpts = excerpts[:_RAG_CONTEXT_CHAR_BUDGET]

        history_str = ""
        if history_turns:
            history_str = "\n".join(
                f"{t.get('role', '').capitalize()}: {self._truncate(t.get('content', ''), _HISTORY_LINE_CHARS)}"
                for t in history_turns[-_HISTORY_TURNS_IN_PROMPT:]
            )

        user_ctx_block = f"\nACTIVE CUSTOMER PROFILE:\n{user_context}\n" if user_context else ""

        system_prompt = (
            "You are the warm, polite customer support assistant for Camellia Artisanal, "
            "a Bengal-rooted artisanal kitchen in Sushant Lok, Gurgaon.\n\n"
            "Answer the customer's message using ONLY the approved excerpts below.\n\n"
            "HARD RULES:\n"
            "- Never state a price, product name, flavour, delivery area, delivery time, "
            "ingredient, allergen, storage instruction, discount or policy that is not written "
            "in the excerpts. Do not estimate, round, convert or infer any of these.\n"
            "- Do not introduce any number that does not appear in the excerpts.\n"
            "- If the excerpts do not contain what the customer asked for, reply with exactly "
            "OUT_OF_CONTEXT and nothing else. Refusing is always better than guessing.\n"
            "- Do not invent an apology for something that did not happen, and do not promise "
            "a callback, refund or timeline.\n"
            "- Keep it short, warm and natural. Reuse the excerpts' own wording for facts.\n"
            "- Never use flower emoji. The client has asked for them to be removed everywhere.\n\n"
            f"--- APPROVED EXCERPTS ---\n{excerpts}\n"
            f"{user_ctx_block}"
        )

        prompt_text = f"{system_prompt}\n\nCustomer Message: {user_message}"
        if history_str:
            prompt_text = (
                f"{system_prompt}\n\nRecent Chat History:\n{history_str}\n\n"
                f"Customer Message: {user_message}"
            )

        return self._query_groq(prompt_text, is_refinement=False)

    def refine_template_response(
        self,
        template_id: str,
        template_text: str,
        user_message: str,
        user_context: str = None,
        history_turns: list = None
    ) -> Tuple[str, bool]:
        """
        Uses Groq to think through and format a natural, personalized response
        based on a matched QA template and active customer context.
        Falls back to template_text if AI is disabled, offline, or times out.
        """
        provider = self.get_active_provider()
        if provider == "none":
            return template_text, False

        # Templates carrying prices or monospace tables are sent as written.
        if not _should_refine(template_text):
            return template_text, False

        # The conversation so far, exactly as the prompt will carry it. Built
        # before the cache key because the key hashes this same string: the
        # reply depends on what was already said, so two customers may share a
        # cached refinement only when their recent history matches too.
        history_str = ""
        if history_turns:
            history_str = "\n".join(
                f"{t.get('role', '').capitalize()}: {self._truncate(t.get('content', ''), _HISTORY_LINE_CHARS)}"
                for t in history_turns[-_HISTORY_TURNS_IN_PROMPT:]
            )

        # Caching survives the move to history-aware refinement because most
        # customers walk the same funnel ("do you deliver" -> "gurgaon"), so the
        # history string repeats across them. Hashing keeps the key small.
        # The customer context stays in the key on its own: a refinement can
        # address the customer by name, and a key without it would serve one
        # customer's name to the next one asking the same question.
        history_digest = (
            hashlib.blake2s(history_str.encode("utf-8"), digest_size=8).hexdigest()
            if history_str else ""
        )
        normalised = " ".join((user_message or "").lower().split())
        cache_key = (template_id, normalised, user_context or "", history_digest)
        cached = self._refinement_cache.get(cache_key)
        if cached is not None:
            self._refinement_cache.move_to_end(cache_key)
            self._refinement_hits += 1
            return cached, True
        self._refinement_misses += 1

        # Deliberately without the full knowledge base. Refinement rewords an
        # answer the matcher has already chosen, and that answer is right here in
        # template_text — the rest of the corpus adds nothing to the task. Sending
        # it cost ~10,000 tokens on every single reply, which on the live service
        # meant a read timeout at ten seconds, long enough to trip the slow-reply
        # notice at customers before falling back to this same template anyway.
        # `generate_response` still gets the full context, because answering an
        # unmatched question genuinely needs it.
        user_ctx_block = f"\nACTIVE CUSTOMER PROFILE:\n{user_context}\n" if user_context else ""

        system_prompt = (
            "You are the warm, helpful customer support assistant for Camellia Artisanal, a boutique bakery & kitchen in Delhi NCR.\n\n"
            f"MATCHED SUPPORT TEMPLATE ({template_id}):\n{template_text}\n\n"
            "Reply warmly and naturally to the customer's message using ONLY the matched template above. "
            "Do not add facts, prices or claims that are not in it. "
            "Preserve all structured monospace code-block tables (``` +---+---+ ```) exactly as provided in the template for crystal-clear readability on WhatsApp and mobile screens. "
            "Keep prices and details accurate, address the customer by name if known, and keep formatting clear and concise.\n\n"
            "ANSWER THE CURRENT TURN:\n"
            "- Read the chat history and reply to what the customer just said, not to the template's topic in general.\n"
            "- If your previous reply asked a question and this message answers it, acknowledge their answer directly and never ask that question again.\n"
            "- Never repeat a reply you have already sent. When the template's facts still apply, give only the part that answers this message, and say it more briefly the second time.\n"
            "- Then move the conversation forward with the natural next step.\n"
            f"{user_ctx_block}"
        )

        prompt_text = f"{system_prompt}\n\nCustomer Message: {user_message}"
        if history_str:
            prompt_text = f"{system_prompt}\n\nRecent Chat History:\n{history_str}\n\nCustomer Message: {user_message}"

        resp_text, ok, _ = self._query_groq(prompt_text, is_refinement=True, template_id=template_id)

        if ok and resp_text:
            # Only successful refinements are cached. A failure is transient —
            # caching the template as if it were a refinement would keep the
            # bot un-refined long after the provider recovered.
            self._refinement_cache[cache_key] = resp_text
            while len(self._refinement_cache) > _REFINEMENT_CACHE_MAX:
                self._refinement_cache.popitem(last=False)
            return resp_text, True

        return template_text, False

ai_responder = AIResponder()
