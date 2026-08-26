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
CONFIG_ERROR_HTTP_STATUSES = (400, 401, 403, 404)
CONFIG_ERROR_COOLDOWN_SECONDS = 600.0


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


def _should_refine(template_text: str) -> bool:
    """False for templates whose exact wording is load-bearing."""
    return not _PRICE_OR_TABLE_RE.search(template_text or "")

class GeminiResponder:
    """
    Unified Cloud AI RAG & Template Refinement Engine.
    
    Supports:
    - OpenAI Reasoning & Chat Models (o3-mini, o1, gpt-4o, gpt-4o-mini)
    - Google Gemini Models (gemini-3.5-flash and other currently served models)
    
    Provides fast, context-grounded customer support answers based strictly on
    menu.json + qa_templates.json.
    Enforces strict refusal ('OUT_OF_CONTEXT') for out-of-syllabus or unlisted queries,
    triggering human escalation.
    """

    def __init__(self, menu_path: str = None, qa_path: str = None):
        self.menu_path = menu_path or config.MENU_JSON_PATH
        self.qa_path = qa_path or config.QA_TEMPLATES_PATH
        self.menu_data = self._load_json(self.menu_path)
        self.qa_data = self._load_json(self.qa_path)
        self._gemini_cooldown_until: float = 0.0
        self._openai_cooldown_until: float = 0.0
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
                ("gemini", self._gemini_cooldown_until),
                ("openai", self._openai_cooldown_until),
            )
            if until > now
        }
        return {
            "provider": provider,
            "enabled": bool(config.is_ai_enabled),
            "model": config.GEMINI_MODEL,
            "last_call_ok": self._last_call_ok,
            "last_call": self._last_call_detail,
            # Unknown until a call is made, so absence of a failure is not success.
            "working": bool(self._last_call_ok) if self._last_call_ok is not None else None,
            # Seconds remaining on each provider's cooldown. A long pause here
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
        self._gemini_cooldown_until = 0.0
        self._openai_cooldown_until = 0.0
        # Cached refinements are rewordings of the templates that just changed.
        # Keeping them would serve the old menu's wording after a menu update.
        self._refinement_cache.clear()

    def get_active_provider(self) -> str:
        """Determines active AI provider: 'gemini', 'openai', or 'none'."""
        if not config.is_ai_enabled:
            return "none"
        now = time.time()
        gemini_available = bool(config.GEMINI_API_KEY) and (now > self._gemini_cooldown_until)
        openai_available = bool(config.OPENAI_API_KEY) and (now > self._openai_cooldown_until)

        if config.AI_PROVIDER == "openai" and openai_available:
            return "openai"
        if config.AI_PROVIDER == "gemini" and gemini_available:
            return "gemini"
        # Auto / default mode: prioritize Gemini Flash
        if gemini_available:
            return "gemini"
        if openai_available:
            return "openai"
        return "none"

    def _build_context_string(self) -> str:
        """Assembles a compact, high-density context string from menu.json and qa_templates.json."""
        context_parts = []
        
        # Business Details & Coverage
        context_parts.append("BUSINESS NAME: Camellia Artisanal")
        context_parts.append("TAGLINE: Bengal-Rooted Artisanal Sweets & Food")
        context_parts.append("DELIVERY AREA: Delhi NCR (Delhi, Gurgaon, Noida, Ghaziabad, Faridabad). We do NOT deliver outside Delhi NCR.")
        context_parts.append("ORDER METHOD: Direct message reply or WhatsApp message to our team.")

        # QA Syllabus / Templates
        templates = self.qa_data.get("templates", [])
        if templates:
            context_parts.append("\n--- FREQUENTLY ASKED QUESTIONS & POLICIES ---")
            for tmpl in templates:
                resp = tmpl.get("response")
                if not resp:
                    continue
                title = tmpl.get("id", "").replace("_", " ").title()
                context_parts.append(f"• {title}: {resp}")

        # Menu Details
        if self.menu_data:
            context_parts.append("\n--- MENU & PRODUCTS ---")
            collections = self.menu_data.get("collections", {})
            if collections:
                context_parts.append(f"Sondesh Collections, Flavours & Pricing: {json.dumps(collections, ensure_ascii=False)}")

            weekly = self.menu_data.get("weekly_menus", [])
            if weekly:
                context_parts.append(f"Weekly Menus & Bento Combos: {json.dumps(weekly, ensure_ascii=False)}")

            delivery = self.menu_data.get("delivery", {})
            if delivery:
                context_parts.append(f"Delivery & Packaging: {json.dumps(delivery, ensure_ascii=False)}")

        # Learned Knowledge & Verified Resolutions from Staff
        try:
            learned_items = db.get_learned_knowledge(limit=50)
            if learned_items:
                context_parts.append("\n--- VERIFIED KNOWLEDGE & POLICIES (LEARNED FROM STAFF) ---")
                for lk in learned_items:
                    topic = lk.get("topic", "")
                    ans = lk.get("verified_answer", "")
                    context_parts.append(f"• {topic}: {ans}")
        except Exception as e:
            log.debug("Could not fetch learned knowledge: %s", e)

        return "\n".join(context_parts)

    def _query_openai(self, prompt_text: str, is_refinement: bool = False, template_id: str = "") -> Tuple[str, bool, str]:
        """
        Queries OpenAI Chat Completions API with support for reasoning models (o3-mini, o1)
        and standard models (gpt-4o, gpt-4o-mini).
        """
        if not config.OPENAI_API_KEY:
            return "", False, "openai_no_key"

        model_name = config.OPENAI_MODEL.lower()
        is_reasoning_model = any(model_name.startswith(prefix) for prefix in ("o1", "o3", "o4"))

        messages = [
            {"role": "user", "content": prompt_text}
        ]

        payload: Dict[str, Any] = {
            "model": config.OPENAI_MODEL,
            "messages": messages,
        }

        if is_reasoning_model:
            payload["max_completion_tokens"] = 1024
            if config.OPENAI_REASONING_EFFORT:
                payload["reasoning_effort"] = config.OPENAI_REASONING_EFFORT
        else:
            payload["max_tokens"] = 1024
            payload["temperature"] = 0.2 if is_refinement else 0.1

        headers = {
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }

        url = "https://api.openai.com/v1/chat/completions"

        try:
            with httpx.Client(timeout=config.OPENAI_TIMEOUT) as client:
                resp = client.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    cooldown = _cooldown_for_status(resp.status_code)
                    if cooldown:
                        self._openai_cooldown_until = time.time() + cooldown
                        if resp.status_code in CONFIG_ERROR_HTTP_STATUSES:
                            log.error(
                                "OpenAI rejected the request as misconfigured (HTTP %s, model %r). "
                                "Pausing OpenAI calls for %.0fs — check OPENAI_API_KEY and OPENAI_MODEL.",
                                resp.status_code, config.OPENAI_MODEL, cooldown,
                            )
                    log.warning("OpenAI HTTP %s: %s", resp.status_code, resp.text[:200])
                    self._record_call(False, f"HTTP {resp.status_code}: {resp.text[:120]}")
                    return "", False, f"openai_http_{resp.status_code}"

                data = resp.json()
                choices = data.get("choices", [])
                if not choices:
                    return "", False, "openai_no_choices"

                assistant_text = choices[0].get("message", {}).get("content", "").strip()

                if "OUT_OF_CONTEXT" in assistant_text.upper() or not assistant_text:
                    fallback = self.qa_data.get("escalation_triggers", {}).get(
                        "fallback_response",
                        "Thanks for reaching out! I can only help with questions about Camellia Artisanal menu, prices, and delivery. Let me connect you with our team. 🙏"
                    )
                    return fallback, False, "out_of_context"

                return assistant_text, True, "openai_rag"

        except Exception as e:
            self._openai_cooldown_until = time.time() + TRANSIENT_COOLDOWN_SECONDS
            log.warning("OpenAI API call failed (%s): %s", config.OPENAI_MODEL, e)
            fallback = self.qa_data.get("escalation_triggers", {}).get(
                "fallback_response",
                "Thanks for reaching out! Let me connect you with our team — they'll get back to you shortly. 🙏"
            )
            return fallback, False, f"openai_exception_{type(e).__name__}"

    def _query_gemini(self, prompt_text: str, is_refinement: bool = False, template_id: str = "") -> Tuple[str, bool, str]:
        """Queries Google Gemini generateContent REST endpoint."""
        if not config.GEMINI_API_KEY:
            return "", False, "gemini_no_key"

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"

        payload = {
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": {
                "temperature": 0.2 if is_refinement else 0.1,
                "maxOutputTokens": 2048
            }
        }

        # Refinement is optional polish on an answer we already hold, so it gets
        # a short budget; answering an unmatched question is the only path worth
        # making the customer wait for.
        timeout = config.GEMINI_REFINE_TIMEOUT if is_refinement else config.GEMINI_TIMEOUT

        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=payload)
                if resp.status_code != 200:
                    cooldown = _cooldown_for_status(resp.status_code)
                    if cooldown:
                        self._gemini_cooldown_until = time.time() + cooldown
                        if resp.status_code in CONFIG_ERROR_HTTP_STATUSES:
                            log.error(
                                "Gemini rejected the request as misconfigured (HTTP %s, model %r). "
                                "Pausing Gemini calls for %.0fs — check GEMINI_API_KEY and GEMINI_MODEL.",
                                resp.status_code, config.GEMINI_MODEL, cooldown,
                            )
                    log.warning("Gemini HTTP %s: %s", resp.status_code, resp.text[:200])
                    self._record_call(False, f"HTTP {resp.status_code}: {resp.text[:120]}")
                    return "", False, f"gemini_http_{resp.status_code}"

                data = resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    return "", False, "gemini_no_candidates"

                finish_reason = candidates[0].get("finishReason", "")
                if is_refinement and finish_reason == "MAX_TOKENS":
                    log.warning("Gemini refinement hit MAX_TOKENS for %s. Falling back to template.", template_id)
                    return "", False, "gemini_max_tokens"

                assistant_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()

                if "OUT_OF_CONTEXT" in assistant_text.upper() or not assistant_text:
                    fallback = self.qa_data.get("escalation_triggers", {}).get(
                        "fallback_response",
                        "Thanks for reaching out! I can only help with questions about Camellia Artisanal menu, prices, and delivery. Let me connect you with our team. 🙏"
                    )
                    return fallback, False, "out_of_context"

                self._record_call(True, "ok")
                return assistant_text, True, "gemini_rag"

        except Exception as e:
            self._gemini_cooldown_until = time.time() + TRANSIENT_COOLDOWN_SECONDS
            self._record_call(False, f"{type(e).__name__}: {str(e)[:120]}")
            log.warning("Gemini API call failed (%s): %s", config.GEMINI_MODEL, e)
            fallback = self.qa_data.get("escalation_triggers", {}).get(
                "fallback_response",
                "Thanks for reaching out! Let me connect you with our team — they'll get back to you shortly. 🙏"
            )
            return fallback, False, f"gemini_exception_{type(e).__name__}"

    def generate_response(self, user_message: str, user_context: str = None, history_turns: list = None) -> Tuple[str, bool, str]:
        """
        Queries the active AI provider (OpenAI o3-mini or Google Gemini) with strict RAG context grounding.
        
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
            "6. When displaying prices, menus, or flavour options, format them clearly with clean bullet points or structured monospace code-block tables (``` +---+---+ ```) for mobile & WhatsApp readability.\n\n"
            f"--- KNOWLEDGE CONTEXT ---\n{context_str}\n"
            f"{user_ctx_block}"
        )

        prompt_text = f"{system_prompt}\n\nCustomer Message: {user_message}"
        if history_turns:
            history_str = "\n".join([f"{t['role'].capitalize()}: {t['content']}" for t in history_turns[-4:]])
            prompt_text = f"{system_prompt}\n\nRecent Chat History:\n{history_str}\n\nCustomer Message: {user_message}"

        if provider == "openai":
            return self._query_openai(prompt_text, is_refinement=False)
        else:
            return self._query_gemini(prompt_text, is_refinement=False)

    def refine_template_response(
        self,
        template_id: str,
        template_text: str,
        user_message: str,
        user_context: str = None,
        history_turns: list = None
    ) -> Tuple[str, bool]:
        """
        Uses active AI provider to think through and format a natural, personalized response
        based on a matched QA template and active customer context.
        Falls back to template_text if AI is disabled, offline, or times out.
        """
        provider = self.get_active_provider()
        if provider == "none":
            return template_text, False

        # Templates carrying prices or monospace tables are sent as written.
        if not _should_refine(template_text):
            return template_text, False

        # Only cache the single-turn form. With history in the prompt the reply
        # depends on the whole conversation, so a key that ignored it would
        # return another customer's answer; a key that included it would
        # essentially never hit. responder.py's hot path passes no history.
        cache_key = None
        if not history_turns:
            normalised = " ".join((user_message or "").lower().split())
            cache_key = (template_id, normalised, user_context or "")
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
            "Keep prices and details accurate, address the customer by name if known, and keep formatting clear and concise.\n"
            f"{user_ctx_block}"
        )

        prompt_text = f"{system_prompt}\n\nCustomer Message: {user_message}"
        if history_turns:
            history_str = "\n".join([f"{t['role'].capitalize()}: {t['content']}" for t in history_turns[-4:]])
            prompt_text = f"{system_prompt}\n\nRecent Chat History:\n{history_str}\n\nCustomer Message: {user_message}"

        if provider == "openai":
            resp_text, ok, _ = self._query_openai(prompt_text, is_refinement=True, template_id=template_id)
        else:
            resp_text, ok, _ = self._query_gemini(prompt_text, is_refinement=True, template_id=template_id)

        if ok and resp_text:
            # Only successful refinements are cached. A failure is transient —
            # caching the template as if it were a refinement would keep the
            # bot un-refined long after the provider recovered.
            if cache_key is not None:
                self._refinement_cache[cache_key] = resp_text
                while len(self._refinement_cache) > _REFINEMENT_CACHE_MAX:
                    self._refinement_cache.popitem(last=False)
            return resp_text, True

        return template_text, False

gemini_responder = GeminiResponder()
ai_responder = gemini_responder
