import json
import time
import httpx
from typing import Tuple, Dict, Any, List, Optional
from camellia.config import config
from camellia.database import db
from camellia.monitoring import get_logger

log = get_logger("camellia.ai_responder")

class GeminiResponder:
    """
    Unified Cloud AI RAG & Template Refinement Engine.
    
    Supports:
    - OpenAI Reasoning & Chat Models (o3-mini, o1, gpt-4o, gpt-4o-mini)
    - Google Gemini Models (gemini-flash-latest, gemini-1.5-flash, gemini-1.5-pro)
    
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

    def _load_json(self, path: str) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error("Error loading %s: %s", path, e)
            return {}

    def reload_data(self):
        """Reload JSON context data and reset cooldowns."""
        self.menu_data = self._load_json(self.menu_path)
        self.qa_data = self._load_json(self.qa_path)
        self._gemini_cooldown_until = 0.0
        self._openai_cooldown_until = 0.0

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
                    if resp.status_code in (429, 503, 500, 502):
                        self._openai_cooldown_until = time.time() + 60.0
                    log.warning("OpenAI HTTP %s: %s", resp.status_code, resp.text[:200])
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
            self._openai_cooldown_until = time.time() + 60.0
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

        try:
            with httpx.Client(timeout=config.GEMINI_TIMEOUT) as client:
                resp = client.post(url, json=payload)
                if resp.status_code != 200:
                    if resp.status_code in (429, 503, 500, 502):
                        self._gemini_cooldown_until = time.time() + 60.0
                    log.warning("Gemini HTTP %s: %s", resp.status_code, resp.text[:200])
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

                return assistant_text, True, "gemini_rag"

        except Exception as e:
            self._gemini_cooldown_until = time.time() + 60.0
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

        context_str = self._build_context_string()
        user_ctx_block = f"\nACTIVE CUSTOMER PROFILE:\n{user_context}\n" if user_context else ""

        system_prompt = (
            "You are the warm, helpful customer support assistant for Camellia Artisanal, a boutique bakery & kitchen in Delhi NCR.\n\n"
            f"MATCHED SUPPORT TEMPLATE ({template_id}):\n{template_text}\n\n"
            "Using the matched template and context below, reply warmly and naturally to the customer's message. "
            "Preserve all structured monospace code-block tables (``` +---+---+ ```) exactly as provided in the template for crystal-clear readability on WhatsApp and mobile screens. "
            "Keep prices and details accurate, address the customer by name if known, and keep formatting clear and concise.\n\n"
            f"--- CONTEXT ---\n{context_str}\n"
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
            return resp_text, True

        return template_text, False

gemini_responder = GeminiResponder()
ai_responder = gemini_responder
