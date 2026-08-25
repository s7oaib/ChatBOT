import difflib
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Dict, Any, Tuple, Optional, List
from camellia.config import config
from camellia.monitoring import get_logger, latency_monitor
from camellia.gemini_responder import gemini_responder
from camellia.session_memory import session_memory

from camellia.order_calculator import order_calculator
from camellia.rag_fallback import rag_fallback_engine
from camellia.database import db

log = get_logger("camellia.responder")

# Conversational intents used to branch the unmatched / escalation reply.
# A single generic "we've notified our team" notice is wrong for a customer who
# is only saying thanks, is complaining, or is trying to hand us a large order.
CLOSING_ONLY_RE = re.compile(
    r'^(thanks?|thank you|thank u|thnx|thnks|thx|tysm|ty|dhanyabad|dhonnobad|shukriya|'
    r'bye+|goodbye|good bye|see you|see ya|tc|take care|good night|gn|'
    r'thanks a lot|thank you so much|thanks so much|many thanks)'
    r'[\s!.,\U0001F300-\U0001FAFF☀-➿]*$',
    re.IGNORECASE
)

ACK_ONLY_RE = re.compile(
    r'^(ok+|okay|okey|k|kk|hmm+|hm|sure|fine|alright|all right|right|got it|gotcha|'
    r'noted|understood|cool|nice|great|good|super|acha|accha|theek hai|thik hai)'
    r'[\s!.,\U0001F300-\U0001FAFF☀-➿]*$',
    re.IGNORECASE
)

COMPLAINT_RE = re.compile(
    r'\b(was bad|were bad|not good|no good|not fresh|wasn.?t fresh|stale|spoil(ed|t)?|rotten|'
    r'smell(ed|s|y|ing)?|worst|terrible|horrible|awful|disgusting|pathetic|'
    r'disappoint(ed|ing)?|unhappy|upset|angry|furious|'
    r'poor quality|bad quality|bad experience|bad taste|tasted bad|'
    r'damaged|broken|melted|leak(ed|ing)?|crushed|'
    r'missing item|item missing|wrong item|wrong order|wrong flavour|wrong flavor|'
    r'late delivery|delivered late|came late|arrived late|very late|'
    r'never (arrived|came|received|delivered)|not (received|delivered)|'
    r'didn.?t (arrive|come|receive|deliver)|did not (arrive|come|receive)|'
    r'waste of money|bakwas|kharab|ganda)\b',
    re.IGNORECASE
)

BULK_QTY_RE = re.compile(
    r'\b(\d{2,})\s*(box|boxes|pcs|pieces|packs?|plates?|kgs?|kilos?|orders?|hampers?|people|guests|pax)\b',
    re.IGNORECASE
)

BULK_CONTEXT_RE = re.compile(
    r'\b(corporate|office|company|firm|bulk|wholesale|reseller|distributor|'
    r'event|wedding|shaadi|sangeet|reception|party|conference|seminar|'
    r'diwali|deepavali|puja|pujo|durga puja|holi|eid|christmas|new year|'
    r'gift hamper|gift hampers|hampers|client gift|employee gift|festive gifting)\b',
    re.IGNORECASE
)

ORDER_INTENT_RE = re.compile(
    r'\b(place (the |my |this |an )?order|order (it|this|now|please)|'
    r'confirm (the |my |this )?order|book (it|the order|my order|this)|'
    r'i.?ll take|ill take|i want (it|this|that)|want to buy|'
    r'let.?s order|lets order|go ahead|proceed|check ?out|'
    r'buy (it|this|now)|finalise|finalize)\b',
    re.IGNORECASE
)

# Words that carry no information about what the customer wants. Used to tell a
# genuinely vague message ("ok tell me") from a short but specific one
# ("do you serve bhapa pitha") — the first is worth a clarifying question, the
# second names something real and belongs with a human if we cannot answer it.
VAGUE_FILLER_WORDS = {
    "a", "an", "the", "is", "it", "this", "that", "these", "those", "one", "ones",
    "i", "im", "me", "my", "we", "us", "you", "your", "u", "r", "are", "am", "be",
    "do", "does", "did", "can", "could", "would", "will", "shall", "should", "may",
    "what", "whats", "how", "when", "where", "which", "who", "why", "any", "anything",
    "some", "something", "else", "other", "more", "much", "many", "about", "for",
    "of", "in", "on", "at", "to", "from", "with", "and", "or", "but", "so", "if",
    "ok", "okay", "okey", "hmm", "hm", "well", "just", "really", "please", "pls",
    "tell", "say", "know", "want", "need", "like", "get", "give", "show", "send",
    "there", "here", "yes", "no", "yeah", "yep", "nope", "sure", "fine", "good",
    "bad", "nice", "great", "hi", "hello", "hey", "namaste", "thanks", "thank",
}

# --- Context carry-over for bare follow-ups -------------------------------
# A customer who just got a product answer often replies with a bare
# "how much?" or "is it veg?" with no product words at all. Without tracking
# what was just discussed, these fall straight to the human handover even
# though the answer is obvious from the previous turn.
BARE_PRICE_RE = re.compile(
    r'^(how much|hw much|howmuch|price|prices|cost|rate|kitna|kitne|kitne ka|'
    r'what.?s the price|whats the price|what is the price|price\??)[\s\?\.\!]*$',
    re.IGNORECASE
)

BARE_VEG_RE = re.compile(
    r'^(is it veg|is this veg|is that veg|is it vegetarian|is this vegetarian|'
    r'veg or non.?veg|veg or nonveg)[\s\?\.\!]*$',
    re.IGNORECASE
)

# One-word answers to "Veg or Non-Veg?". Non-veg is matched first everywhere it
# is used, because "non veg" also contains "veg".
BARE_NONVEG_CHOICE_RE = re.compile(
    r'^[\s\U0001F300-\U0001FAFF]*(non.?veg|nonveg|non.?vegetarian|'
    r'chicken|mutton|fish|prawn)( menu)?[\s\?\.\!]*$',
    re.IGNORECASE
)

BARE_VEG_CHOICE_RE = re.compile(
    r'^[\s\U0001F300-\U0001FAFF]*(veg|vegetarian|niramish)( menu)?[\s\?\.\!]*$',
    re.IGNORECASE
)

# Maps a matched template id to the product topic it just discussed, so the
# next bare follow-up can be resolved against it.
TOPIC_MAP = {
    "aamar_mishti_collection": "aamar_mishti",
    "pack_size_250g": "aamar_mishti",
    "pack_size_500g": "aamar_mishti",
    "pack_size_1kg": "aamar_mishti",
    "nolen_gur_aamar": "aamar_mishti",
    "sondesh_classic_rose": "aamar_mishti",
    "sondesh_mango_kesar": "aamar_mishti",
    "sondesh_nolen_gur": "aamar_mishti",
    "kanchagolla": "aamar_mishti",
    "jolbhora": "aamar_mishti",
    "jolbhora_talgash_details": "aamar_mishti",
    "signature_collection": "signature",
    "mahua_signature_special": "signature",
    "sondesh_price_detailed": "sondesh_general",
    "product_overview": "sondesh_general",
    "know_more": "sondesh_general",
    "sugar_free_options": "sondesh_general",
    "combo_bento": "savoury",
    "weekly_menu_veg": "savoury",
    "weekly_menu_nonveg": "savoury",
    "savoury_food_intro": "savoury",
    "subscription_details": "savoury",
    "alacarte_prebooking_steps": "savoury",
}

# Rewording each topic maps onto an existing template's own trigger phrases,
# so the normal matcher answers with a template that was already reviewed —
# this only steers matching, it never invents new copy.
# "savoury" is deliberately absent: meal pricing is per-dish across two weekly
# menus, and every bento-worded hint lands on the bento_details guard, which is
# withheld pending the client resolving a pricing conflict. Asking which menu
# they want is handled separately instead of steering the matcher.
TOPIC_PRICE_HINT = {
    "aamar_mishti": "aamar mishti price",
    "signature": "signature collection price",
    "sondesh_general": "sondesh price",
}



@dataclass
class BotResponse:
    text: str
    was_matched: bool = True
    template_id: Optional[str] = None
    image_url: Optional[Any] = None

    def __str__(self):
        return self.text

class DeterministicResponder:
    """
    Hybrid Auto-Responder Engine for Camellia Artisanal.
    Combines 100% deterministic regex boundary matching, menu photo cards,
    conversational session memory, and Google Gemini Flash AI RAG fallback.
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
            log.error("Error loading %s: %s", path, e)
            return {}

    def reload_data(self):
        """Reload JSON files to support live menu updates."""
        self.qa_data = self._load_json(self.qa_path)
        self.menu_data = self._load_json(self.menu_path)
        gemini_responder.reload_data()

    def reload_templates(self):
        """Alias for reload_data."""
        self.reload_data()

    def check_explicit_escalation_triggers(self, text: str) -> Optional[str]:
        """Checks if text contains explicit escalation keywords."""
        triggers = self.qa_data.get("escalation_triggers", {}).get("keywords", [])
        lowered = text.lower()
        for kw in triggers:
            if re.search(r'\b' + re.escape(kw.lower()) + r'\b', lowered):
                return f"Trigger keyword matched: '{kw}'"
        return None

    def _generic_fallback(self) -> str:
        """The scripted 'we've handed this to a human' notice from qa_templates.json."""
        return self.qa_data.get("escalation_triggers", {}).get(
            "fallback_response",
            "Thank you for getting in touch! 🌸 I've notified our team (+91 87965 51990) regarding "
            "your request and they'll connect with you directly on WhatsApp/call shortly.\n\n"
            "In the meantime, feel free to ask me about:\n"
            "• 🌸 Our fresh Sondesh, Kanchagolla & Jolbhora (Aamar Mishti & Signature Collections)\n"
            "• 🍱 This week's fresh Bengali Lunch & Dinner Bento menu\n"
            "• 🚚 Delivery details across Delhi NCR\n\n"
            "Or reply 'menu' to see our complete price list!"
        )

    def classify_unmatched_intent(self, text: str) -> str:
        """
        Classifies a message we could not answer from a template, so the handover
        notice can be worded for what the customer is actually doing.
        Returns one of: 'complaint', 'bulk', 'order_intent', 'unknown'.
        """
        if not text:
            return "unknown"

        if COMPLAINT_RE.search(text):
            return "complaint"

        qty_match = BULK_QTY_RE.search(text)
        big_qty = qty_match and int(qty_match.group(1)) >= 20
        if big_qty or (BULK_CONTEXT_RE.search(text) and re.search(r'\b(order|book|quote|need|want|buy|gift)\w*\b', text, re.IGNORECASE)):
            return "bulk"

        if ORDER_INTENT_RE.search(text):
            return "order_intent"

        return "unknown"

    def build_handover_response(self, user_message: str) -> Tuple[str, str]:
        """
        Builds the reply for a message that is being handed to a human.
        Every branch here escalates, so all of them may say the team was notified.

        Returns: (response_text, intent_tag)
        """
        intent = self.classify_unmatched_intent(user_message or "")

        if intent == "complaint":
            return (
                "I'm really sorry that happened — that is not the experience we want you to have. 🙏\n\n"
                "I've flagged this to our team (+91 87965 51990) along with this chat, and they will "
                "reach out to you personally to put it right.\n\n"
                "If you can share your order date and what went wrong, it helps us fix it faster.",
                intent,
            )

        if intent == "bulk":
            return (
                "Wonderful — we would love to make this for you! 🌸\n\n"
                "Bulk, corporate and festive orders are quoted personally so we can work out pricing, "
                "packaging and delivery dates around your date.\n\n"
                "I've passed this to our team (+91 87965 51990) and they will connect with you shortly. "
                "To speed things up, do share:\n"
                "• Approximate quantity\n"
                "• Delivery date\n"
                "• Delhi NCR area, or if you'd prefer kitchen pickup",
                intent,
            )

        if intent == "order_intent":
            return (
                "Lovely — let's get this order placed! 🌸\n\n"
                "Our team (+91 87965 51990) has your chat and will confirm on WhatsApp/call shortly.\n\n"
                "To make it quick, please share:\n"
                "1. Item & flavour\n"
                "2. Pack size (250g / 500g / 1kg)\n"
                "3. Delivery address & phone number\n"
                "4. Preferred delivery date\n\n"
                "Payment is UPI / bank transfer once the order is confirmed.",
                intent,
            )

        return self._generic_fallback(), intent

    def _should_send_slow_notice(self, user_id: str, channel: str, response_text: str, template_id: Optional[str]) -> bool:
        """
        Decides whether this reply should carry the slow-reply notice.

        Sent at most once per conversation, and only when the customer actually
        waited — a fast reply gets no apology, and a blocked/empty reply is never
        turned into a message that would not otherwise have been sent.
        """
        if not config.is_slow_reply_notice_active:
            return False
        if template_id == "ad_gate_blocked" or not (response_text or "").strip():
            return False
        if not latency_monitor.is_degraded():
            return False
        return not session_memory.get_facts(user_id, channel).get("slow_notice_sent")

    def match_and_respond(
        self,
        user_message: str,
        user_id: str = None,
        channel: str = None,
        referral_source: str = None,
        referral_ad_id: str = None
    ) -> Tuple[str, bool, Optional[str], Optional[Any]]:
        """
        Matches incoming message against qa_templates.json & menu.json.

        Wraps the matching itself so every reply is timed. When the bot has gone
        slow, the customer is told once and given the manager's direct line —
        they can switch to a human or keep talking to the bot.
        """
        user_id = user_id or "default_user"
        channel = channel or "default_channel"

        started = time.perf_counter()
        try:
            response_text, was_matched, template_id, image_url = self._resolve_response(
                user_message, user_id, channel, referral_source, referral_ad_id
            )
        finally:
            latency_monitor.record((time.perf_counter() - started) * 1000.0)

        if self._should_send_slow_notice(user_id, channel, response_text, template_id):
            session_memory.get_facts(user_id, channel)["slow_notice_sent"] = True
            log.info("Slow-reply notice sent to %s on %s.", user_id, channel)
            response_text = f"{config.SLOW_REPLY_NOTICE_TEXT}\n\n{response_text}"

        return response_text, was_matched, template_id, image_url

    def _resolve_response(
        self,
        user_message: str,
        user_id: str = None,
        channel: str = None,
        referral_source: str = None,
        referral_ad_id: str = None
    ) -> Tuple[str, bool, Optional[str], Optional[Any]]:
        """
        Matches incoming message against qa_templates.json & menu.json.
        Updates session memory and falls back to Gemini Flash RAG.

        Returns:
            (response_text: str, was_matched: bool, template_id: Optional[str], image_url: str | List[str] | None)
        """
        user_id = user_id or "default_user"
        channel = channel or "default_channel"

        # 0. Instagram Ad-Gate Verification (Deliver strictly to Ad clickers unless bypassed)
        if channel == "instagram" and not config.BYPASS_AD_GATE and referral_source != "ADS":
            log.info("Ad-Gate Block: Organic Instagram message from user %s blocked.", user_id)
            return "", False, "ad_gate_blocked", None

        if not user_message or not user_message.strip():
            return self._generic_fallback(), False, None, None

        lowered_text = user_message.lower().strip()

        # Normalise elongated / misspelled greetings ("hii", "heyyy", "helo") so they
        # still reach the greeting template instead of falling through to escalation.
        lowered_text = re.sub(r'^(?:h+i+|h+a+i+)\b', 'hi', lowered_text)
        lowered_text = re.sub(r'^(?:h+e+y+)\b', 'hey', lowered_text)
        lowered_text = re.sub(r'^(?:h+e+l+o+|h+e+l+l+o+|h+l+o+)\b', 'hello', lowered_text)
        lowered_text = re.sub(r'^(?:namaskar|nomoshkar|nomoskar)\b', 'namaste', lowered_text)

        # 0.5 Spam / External Link / Unreadable Gibberish Detection
        if re.search(r'https?://|www\.|\.xyz|\.top|\.click\b', lowered_text):
            fallback = self._generic_fallback()
            session_memory.add_turn(user_id, channel, user_message, fallback)
            return fallback, False, None, None

        if not re.search(r'[a-zA-Z\u0980-\u09FF]', user_message):
            fallback = self._generic_fallback()
            session_memory.add_turn(user_id, channel, user_message, fallback)
            return fallback, False, None, None

        user_words = set(re.findall(r'\b\w+\b', lowered_text))
        
        STOP_WORDS = {
            "i", "am", "im", "my", "is", "are", "do", "does", "did", "you", "u", "r", "a", "an", "the", "to", "for",
            "of", "in", "on", "at", "by", "from", "with", "and", "or", "we", "can", "your", "this", "that", "these", "those",
            "what", "how", "when", "where", "why", "who", "which", "many", "much", "me", "us", "it", "its",
            "show", "tell", "send", "share", "about", "some", "any", "all", "than", "other",
            "please", "pls", "give", "let", "use", "be", "has", "had", "know"
        }

        # 1. Update & Extract Session Memory Facts (Name, Order Draft)
        facts = session_memory.extract_and_update_facts(user_id, channel, user_message)

        # Check if user just provided their name
        intro_greeting_prefix = ""
        if facts.get("just_introduced_name"):
            facts["just_introduced_name"] = False
            user_name = facts.get("name")
            name_words = set(user_name.lower().split()) if user_name else set()
            intro_noise = {
                "hi", "hello", "hey", "namaste", "nomoshkar", "im", "i", "am", "my", "name",
                "names", "is", "this", "call", "me", "myself", "it", "its", "it's", "here"
            }
            substantive_tokens = [w for w in user_words if w not in STOP_WORDS and w not in intro_noise and w not in name_words]
            has_substantive_inquiry = (
                len(substantive_tokens) >= 2
                or "?" in user_message
                or any(kw in lowered_text for kw in [
                    "difference", "collection", "sondesh", "menu", "price", "cost", "deliver",
                    "delivery", "bento", "box", "order", "what", "how", "where", "can", "tell",
                    "halal", "oil", "payment", "cod", "upi", "gift", "gifting", "jolbhora",
                    "sugar", "diabetic", "flavour", "flavors", "sweet", "sweets", "curry", "meal",
                    "allergy", "allergies", "nut", "pickup", "tracking"
                ])
            )

            if not has_substantive_inquiry:
                if user_name:
                    response_text = f"Nice to meet you, {user_name}! 👋 Welcome to Camellia Artisanal. Ask us about our Sondesh flavours & prices, weekly menu, or delivery — happy to help!"
                else:
                    response_text = "Nice to meet you! 👋 Welcome to Camellia Artisanal. Ask us about our Sondesh flavours & prices, weekly menu, or delivery — happy to help!"
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, "name_introduction", None
            else:
                if user_name:
                    intro_greeting_prefix = f"Nice to meet you, {user_name}! 👋\n\n"
                else:
                    intro_greeting_prefix = "Nice to meet you! 👋\n\n"

        # 1.5 Pure sign-offs and acknowledgements. These carry no question, so handing
        # them to a human — and telling the customer we did — is both noise and untrue.
        if not intro_greeting_prefix:
            if CLOSING_ONLY_RE.match(lowered_text):
                response_text = (
                    "Thank you so much! 🌸 Lovely chatting with you.\n\n"
                    "Whenever you'd like Sondesh or this week's Bengali meals, just message here — "
                    "our kitchen is open 10:00 AM – 7:30 PM every day. Take care!"
                )
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, "conversation_closing", None

            if ACK_ONLY_RE.match(lowered_text):
                response_text = (
                    "Got it! 😊 What would you like to do next?\n\n"
                    "• 🌸 Sondesh flavours & prices\n"
                    "• 🍱 This week's Bengali meal menu\n"
                    "• 🚚 Delivery details for your area\n"
                    "• 🛒 Place an order"
                )
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, "acknowledgement", None

        # 2. Check explicit escalation triggers first (refund, complaint, etc.)
        escalation_reason = self.check_explicit_escalation_triggers(user_message)
        if escalation_reason:
            fallback, _intent = self.build_handover_response(user_message)
            if intro_greeting_prefix:
                fallback = intro_greeting_prefix + fallback
            session_memory.add_turn(user_id, channel, user_message, fallback)
            return fallback, False, "explicit_escalation", None

        # 2.5 Check if accumulated order_draft slots form a complete order receipt
        order_draft = facts.get("order_draft", {})
        existing_cart = facts.get("cart", [])
        has_explicit_order_action = any(verb in lowered_text for verb in [
            "want to order", "i want to order", "please order", "place order", "i would like to order",
            "book an order", "confirm order", "need to order", "chahiye", "bhejo",
            "order 1", "order 2", "order 3", "order 4", "order 5"
        ])
        has_quantity_order_signal = bool(
            re.search(r'\b(order|deliver|send|buy|add|pack)\w*\b', lowered_text)
            and re.search(r'\b\d+\b', lowered_text)
        )
        is_general_menu_query = (not has_explicit_order_action and not has_quantity_order_signal) and any(phrase in lowered_text for phrase in [
            "full menu", "send menu", "price list", "show menu", "what is your menu",
            "how much is sondesh", "sondesh price", "how much sondesh", "sondesh cost", "cost of sondesh", "price of sondesh",
            "tell me about your product", "about your product", "about your products", "what products", "your products", "product", "products",
            "how can you assist", "how can you help", "what can you do", "help me", "assist me",
            "bangalore", "bengaluru", "mumbai", "pune", "kolkata", "hyderabad", "chennai", "jaipur", "ahmedabad", "chandigarh", "lucknow",
            "courier to", "deliver to bangalore", "deliver to mumbai", "deliver to pune", "deliver to kolkata", "out of ncr", "outside ncr",
            "allergy", "allergies", "allergic", "nut allergy", "nut-free", "nut free", "nut allergens",
            "order status", "track my order", "where is my order", "paid but no confirmation", "paid 2 hours ago", "paid 1 hour ago", "order tracking",
            "kitchen address", "pickup address", "self pickup", "pick it up from kitchen", "kitchen timings",
            "how to pay", "pay", "payment", "upi", "qr", "qr code", "gpay", "paytm", "phonepe", "cod", "cash on delivery",
            "shelf life", "how long", "storage", "refrigeration", "refrigerate", "fridge", "spoil", "expiry",
            "how to order", "how do i order", "how can i order", "how to place", "place an order", "ordering process",
            "dispatch time", "how fast", "best time to order", "best time to book", "pre-order", "pre order",
            "subscription", "subscriptions", "what sweets", "sweet boxes", "cook meals", "bengali meals",
            "packaging", "eco friendly", "eco-friendly", "bento", "bento details", "bento meal", "bento box", "bento combo", "bento combos", "what combos", "what is my name"
        ])
        if not is_general_menu_query:
            calc_result = order_calculator.parse_and_calculate(user_message, existing_draft=order_draft, existing_cart=existing_cart)
            is_order_intent = (
                calc_result and (
                    calc_result.get("has_new_items") or
                    any(kw in lowered_text for kw in ["confirm", "bill", "receipt", "total", "cart", "summary", "place order", "place this order", "finalize", "checkout"])
                )
            )
            if is_order_intent and calc_result.get("formatted_receipt"):
                if calc_result.get("items"):
                    facts["cart"] = calc_result["items"]
                response_text = calc_result["formatted_receipt"]
                if intro_greeting_prefix:
                    response_text = intro_greeting_prefix + response_text
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, "order_draft_receipt", None
        
        # Check Self-Healing Learned Knowledge Base (Saved from Staff Escalation Resolutions)
        try:
            from camellia.database import db
            learned_items = db.get_learned_knowledge(limit=100)
            for item in learned_items:
                pattern = item.get("question_pattern", "").lower().strip()
                ans = item.get("verified_answer", "").strip()
                if not pattern or not ans:
                    continue
                p_words = set(re.findall(r'\b\w+\b', pattern))
                sub_p_words = p_words - STOP_WORDS
                sub_u_words = user_words - STOP_WORDS
                if pattern in lowered_text or (sub_p_words and sub_p_words.issubset(sub_u_words)):
                    if intro_greeting_prefix:
                        ans = intro_greeting_prefix + ans
                    session_memory.add_turn(user_id, channel, user_message, ans)
                    return ans, True, f"learned_{item.get('id')}", None
        except Exception:
            pass

        # 3. Clean Conversational Preamble to isolate Business Intent
        cleaned_business_text = re.sub(
            r'^(hi|hello|hey|good morning|good afternoon|good evening|namaste|nomoshkar|greetings|hie|hii+)[,\s!.-]*', '', lowered_text
        ).strip()
        cleaned_business_text = re.sub(
            r'^(can you tell me|could you please tell me|i want to know|i would like to know|please let me know|please tell me|can i ask|i am looking for|im looking for|looking for|tell me)[,\s!.-]*', '', cleaned_business_text
        ).strip()
        cleaned_business_text = re.sub(
            r'[,\s!.-]*(thanks|thank you|pls|please|hi|hello|hey)$', '', cleaned_business_text
        ).strip()

        cleaned_words = set(re.findall(r'\b\w+\b', cleaned_business_text)) if cleaned_business_text else set()

        # 3.5 Resolve bare follow-ups ("how much?", "is it veg?") against the
        # last product topic discussed this session, instead of letting a
        # message with no product words of its own fall through to a human
        # handover the customer didn't need.
        last_topic = facts.get("last_topic")
        stripped_for_bare_check = user_message.strip()
        if last_topic and BARE_VEG_RE.match(stripped_for_bare_check):
            if last_topic == "savoury":
                response_text = (
                    "Both! 🌸 Our weekly Bengali meals include vegetarian dishes (Aloor Dom, Dhokar Dalna, "
                    "Basonti Polao) and non-vegetarian dishes (Bhetki Paturi, Chingri Malaikari, Robibarer Mangsho).\n\n"
                    "Would you like to see the Veg or Non-Veg menu?"
                )
            else:
                response_text = (
                    "Yes, always! 🌸 Every Sondesh we make — Aamar Mishti and Signature Collection alike — is "
                    "100% vegetarian, made from milk, sugar & natural flavourings only. No eggs, no meat extracts."
                )
            if intro_greeting_prefix:
                response_text = intro_greeting_prefix + response_text
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "context_veg_followup", None

        # Meals are priced per dish across two weekly menus, and bento tier
        # pricing is withheld by the bento_details guard pending the client
        # resolving a conflict — so ask which menu they want rather than
        # steering a bare "how much?" into a price we must not quote.
        if last_topic == "savoury" and BARE_PRICE_RE.match(stripped_for_bare_check):
            response_text = (
                "Our Bengali meals are priced per dish, and the menu changes every week 🍱\n\n"
                "Would you like this week's *Veg* or *Non-Veg* menu? I'll send it across with prices."
            )
            if intro_greeting_prefix:
                response_text = intro_greeting_prefix + response_text
            facts["awaiting_menu_choice"] = True
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "context_meal_price_followup", None

        # A one-word "veg" / "non veg" is only an answer to that question — on its
        # own it is too thin to match a template, so resolve it here. Non-veg is
        # tested first because "non veg" also contains "veg".
        if facts.get("awaiting_menu_choice") or last_topic == "savoury":
            is_nonveg = BARE_NONVEG_CHOICE_RE.match(stripped_for_bare_check)
            is_veg = (not is_nonveg) and BARE_VEG_CHOICE_RE.match(stripped_for_bare_check)
            if is_nonveg or is_veg:
                facts["awaiting_menu_choice"] = False
                hint = "non veg menu" if is_nonveg else "veg menu"
                lowered_text = f"{lowered_text} {hint}".strip()
                cleaned_business_text = f"{cleaned_business_text} {hint}".strip()
                cleaned_words = set(re.findall(r'\b\w+\b', cleaned_business_text))

        if last_topic and BARE_PRICE_RE.match(stripped_for_bare_check) and last_topic in TOPIC_PRICE_HINT:
            hint = TOPIC_PRICE_HINT[last_topic]
            lowered_text = f"{lowered_text} {hint}".strip()
            cleaned_business_text = f"{cleaned_business_text} {hint}".strip()
            cleaned_words = set(re.findall(r'\b\w+\b', cleaned_business_text))

        # Check if user message contains substantive business inquiry
        has_business_query = bool(
            (cleaned_words - STOP_WORDS) or
            any(kw in lowered_text for kw in [
                "sondesh", "menu", "price", "cost", "deliver", "delivery", "bento", "box", "order",
                "halal", "oil", "payment", "cod", "upi", "gift", "gifting", "jolbhora", "sugar",
                "flavour", "flavors", "sweet", "sweets", "curry", "meal", "allergy", "allergies",
                "nut", "pickup", "tracking", "subscription", "subscriptions", "alacarte", "dispatch",
                "product", "products", "recommend", "recommendation", "options"
            ])
        )

        templates = self.qa_data.get("templates", [])

        # Priority Tier Definitions
        TIER_1_GUARDS = {
            "GUARD_collection_differentiation", "GUARD_customization_flavour_mixing",
            "GUARD_halal_ingredients_oil", "GUARD_payment_method",
            "GUARD_allergen_nutfree_glutenfree", "GUARD_pickup_address_hours",
            "GUARD_shelf_life_storage", "GUARD_staff_names_manager_contact",
            "GUARD_dietary_suitability",
            "bento_details"
        }

        TIER_2_SPECIFIC_INTENTS = {
            "sondesh_price_detailed", "sondesh_classic_rose", "sondesh_mango_kesar",
            "sondesh_nolen_gur", "nolen_gur_aamar", "jolbhora", "kanchagolla", "mahua_signature_special",
            "aamar_mishti_collection", "signature_collection", "pack_size_250g",
            "pack_size_500g", "pack_size_1kg", "weekly_menu_veg", "weekly_menu_nonveg",
            "combo_bento", "delivery_area", "delivery_out_of_area", "delivery_time",
            "packaging", "contact_social", "how_to_order", "freshness_ingredients",
            "order_status_tracking", "jolbhora_talgash_details", "sugar_free_options",
            "recall_order_flavour", "savoury_food_intro", "bento_details",
            "subscription_details", "subscription_booking_steps", "alacarte_prebooking_steps",
            "sondesh_dispatch_time", "savoury_crosssell_sondesh", "sondesh_crosssell_savoury",
            "savoury_best_time_to_book", "savoury_delivery_time", "kolkata_expansion_info",
            "order_next_steps", "sondesh_price_general", "recall_name", "name_introduction"
        }

        TIER_3_GENERIC_INFO = {
            "product_overview", "know_more", "bot_capabilities", "bot_identity",
            "artisanal_differentiation"
        }

        TIER_4_GREETING_INTRO = {
            "greeting"
        }

        best_template = None
        best_composite_score = -1
        best_score = 0
        runner_up_template = None
        runner_up_composite = -1

        sorted_templates = sorted(templates, key=lambda t: 1 if t.get("id") in TIER_4_GREETING_INTRO else 0)

        for tmpl in sorted_templates:
            tmpl_id = tmpl.get("id", "")
            is_guard = tmpl_id.startswith("GUARD_")
            triggers = tmpl.get("triggers", [])
            max_trigger_score = 0
            
            for trigger in triggers:
                trigger_lower = trigger.lower().strip()
                trigger_words = set(re.findall(r'\b\w+\b', trigger_lower))
                t_score = 0
                
                # Check 1: Exact full word boundary regex match against lowered_text OR cleaned_business_text
                pattern = r'\b' + re.escape(trigger_lower) + r'\b'
                if re.search(pattern, lowered_text) or (cleaned_business_text and re.search(pattern, cleaned_business_text)):
                    t_score = len(trigger_words) * 10 + 10
                else:
                    # Check 2: Substantive (non-stopword) token intersection
                    substantive_trigger_words = trigger_words - STOP_WORDS
                    substantive_user_words = user_words - STOP_WORDS
                    substantive_cleaned_words = cleaned_words - STOP_WORDS
                    
                    matching_substantive = substantive_trigger_words.intersection(substantive_user_words)
                    if substantive_cleaned_words:
                        matching_substantive = matching_substantive.union(substantive_trigger_words.intersection(substantive_cleaned_words))
                    
                    if matching_substantive:
                        coverage = len(matching_substantive) / max(len(substantive_trigger_words), 1)
                        user_coverage = len(matching_substantive) / max(len(substantive_user_words), 1)
                        if len(substantive_trigger_words) == 1:
                            if len(substantive_user_words) <= 2 or user_coverage >= 0.35:
                                t_score = len(matching_substantive) * 6 + int(coverage * 6)
                        else:
                            if len(matching_substantive) >= 2 or coverage >= 0.6:
                                t_score = len(matching_substantive) * 6 + int(coverage * 6)
                    else:
                        # Check 3: Fuzzy word similarity match
                        fuzzy_matches = 0
                        matched_t_words = set()
                        eval_words = substantive_cleaned_words if substantive_cleaned_words else substantive_user_words
                        for u_word in eval_words:
                            if len(u_word) < 3:
                                continue
                            for t_word in substantive_trigger_words:
                                if len(t_word) < 3 or t_word in matched_t_words:
                                    continue
                                ratio = difflib.SequenceMatcher(None, u_word, t_word).ratio()
                                threshold = 0.83 if min(len(u_word), len(t_word)) == 3 else 0.80
                                if ratio >= threshold:
                                    fuzzy_matches += 1
                                    matched_t_words.add(t_word)
                                    break
                        if fuzzy_matches > 0:
                            coverage = fuzzy_matches / max(len(substantive_trigger_words), 1)
                            if len(substantive_trigger_words) == 1 or fuzzy_matches >= 2 or coverage >= 0.6:
                                t_score = fuzzy_matches * 5 + int(coverage * 5)

                if t_score > max_trigger_score:
                    max_trigger_score = t_score

            score = max_trigger_score

            # Guard Anchor Enforcement
            if is_guard:
                guard_anchors = {
                    "GUARD_collection_differentiation": {"difference", "differ", "differs", "compare"},
                    "GUARD_customization_flavour_mixing": {"mix", "mixing", "custom", "customize", "customise", "customized", "customised", "assorted"},
                    "GUARD_halal_ingredients_oil": {"halal", "jhatka", "oil", "medium", "mustard", "ghee"},
                    "GUARD_payment_method": {"pay", "payment", "paying", "cod", "upi", "qr", "gpay", "paytm", "phonepe", "banking", "card", "cash"},
                    "GUARD_allergen_nutfree_glutenfree": {"allergy", "allergies", "allergic", "allergen", "allergens", "peanut", "peanuts", "cashew", "cashews", "pistachio", "pistachios", "gluten", "contamination", "nut", "nuts"},
                    # Anchors are substring-matched, so these stay phrases: a bare
                    # "address" would catch "my address is ...", which is a customer
                    # giving their delivery address, not asking for the kitchen's.
                    "GUARD_pickup_address_hours": {"pickup", "pick", "collect", "kitchen", "your address", "the address", "located", "location"},
                    "GUARD_shelf_life_storage": {"shelf", "storage", "refrigeration", "refrigerate", "unrefrigerated", "fridge", "expiry", "spoil", "heat", "store", "last", "room"},
                    "GUARD_staff_names_manager_contact": {"owner", "founder", "chef", "somdeb", "manager", "staff", "guru", "email"}
                }
                anchors = guard_anchors.get(tmpl_id, set())
                if anchors and not any(a in lowered_text for a in anchors):
                    score = 0

            if tmpl_id == "GUARD_shelf_life_storage" and "jolbhora" in lowered_text:
                score = 0

            # Suppress greeting/name intro when user message contains substantive business inquiry and another specific template matched
            if has_business_query and tmpl_id in TIER_4_GREETING_INTRO and best_composite_score > 0:
                score = 0

            if intro_greeting_prefix and tmpl_id == "name_introduction":
                score = 0

            # Specific Domain Boosts
            if score > 0:
                if tmpl_id == "GUARD_halal_ingredients_oil" and re.search(r'\b(halal|jhatka|cooking oil|mustard oil|what oil|which oil|which cooking oil|what cooking oil|oil do you cook|cooking medium|what cooking medium)\b', lowered_text):
                    score += 100
                elif tmpl_id == "GUARD_allergen_nutfree_glutenfree" and (re.search(r'\b(nut allergy|nuts allergy|severe allergy|allergic to nuts|peanut allergy|cashew allergy|pistachio allergy|gluten allergy|cross contamination|allergen|allergens|nut allergen|contain nut|contain nuts)\b', lowered_text) or (re.search(r'\b(allergy|allergic|allergies)\b', lowered_text) and re.search(r'\b(nut|nuts|severe|peanut|cashew|pistachio|gluten)\b', lowered_text))):
                    score += 100
                elif tmpl_id == "GUARD_payment_method" and re.search(r'\b(how to pay|how do i pay|how can i pay|payment method|payment methods|payment options|pay via|pay with|pay by|cod|cash on delivery|upi qr|qr code|gpay|paytm|phonepe|net banking|credit card|debit card)\b', lowered_text):
                    score += 100
                elif tmpl_id == "GUARD_shelf_life_storage" and re.search(r'\b(shelf life|how long (can i store|does it last|will it last|to keep)|how many days will it last|refrigeration|refrigerate|fridge|storage guidelines|how to store)\b', lowered_text):
                    if "jolbhora" in lowered_text:
                        score = 0
                    else:
                        score += 100
                elif tmpl_id == "GUARD_staff_names_manager_contact" and re.search(r'\b(who is the owner|who is the founder|who is the chef|who is the cook|who runs the kitchen now|who is somdeb|chef name|owner name|founder name|staff names|kitchen manager|who is the manager|manager name|manager email|email address|email id|your email)\b', lowered_text):
                    score += 100
                elif tmpl_id == "GUARD_collection_differentiation" and re.search(r'\b(difference between collection|collection 1 vs collection 2|collection 1 and collection 2 difference|how do the collections differ|compare collection|how are the collections different)\b', lowered_text):
                    score += 100
                elif tmpl_id == "GUARD_customization_flavour_mixing" and re.search(r'\b(mix (rose and classic|classic and rose|flavours|flavors|two flavours|two flavors|different flavours|different flavors)|custom assorted box|customize the (box|flavours|flavors)|customise the (box|flavours|flavors)|customize flavours|customise flavours|customize flavors|customise flavors|mix flavours in|mix flavors in|mix flavours|mix flavors)\b', lowered_text):
                    score += 100
                elif tmpl_id == "delivery_out_of_area" and (
                    re.search(r'\b(deliver(y|ing)?|courier|ship(ping)?|send|order|to|in)\b.*\b(bangalore|bengaluru|mumbai|kolkata|pune|hyderabad|chennai|jaipur|ahmedabad|chandigarh|lucknow)\b', lowered_text) or
                    re.search(r'\b(bangalore|bengaluru|mumbai|kolkata|pune|hyderabad|chennai|jaipur|ahmedabad|chandigarh|lucknow)\b.*\b(deliver(y|ing)?|courier|ship(ping)?|send|order)\b', lowered_text) or
                    any(c in lowered_text for c in ["bangalore", "bengaluru", "outside delhi", "outside ncr", "other cities", "other city", "out of delhi", "out of ncr", "out of state", "interstate"])
                ):
                    score += 50
                elif tmpl_id == "combo_bento" and any(b in lowered_text for b in ["bento", "combo"]):
                    score += 100
                elif tmpl_id == "packaging" and any(p in lowered_text for p in ["packaging", "package", "packeging", "box material", "eco friendly", "eco-friendly"]):
                    score += 100
                elif tmpl_id == "sondesh_nolen_gur" and re.search(r'\b(nolen gur|nolen gur sondesh)\b', lowered_text) and not any(c in lowered_text for c in ["collection 1", "collection1", "aamar"]):
                    score += 150
                elif tmpl_id == "nolen_gur_aamar" and re.search(r'\b(nolen gur)\b', lowered_text) and any(c in lowered_text for c in ["collection 1", "collection1", "aamar"]):
                    score += 150
                elif tmpl_id == "kanchagolla" and re.search(r'\b(kanchagolla|kancha golla)\b', lowered_text):
                    score += 150
                elif tmpl_id == "pack_size_250g" and re.search(r'\b(250g|250 g)\b', lowered_text) and any(w in lowered_text for w in ["how many", "pieces", "pcs", "box", "pack", "quantity"]):
                    score += 150
                elif tmpl_id == "subscription_booking_steps" and facts.get("awaiting_subscription_booking") and re.search(r'\b(how (do i|to|can i) (order|book)|how to get started|book|booking steps|next steps|how do i start)\b', lowered_text):
                    score += 120
                elif tmpl_id == "subscription_booking_steps" and re.search(r'\b(how to book subscription|book subscription|start subscription|subscribe|how do i book subscription|booking steps for subscription|next steps for subscription|supscription)\b', lowered_text):
                    score += 100
                elif tmpl_id == "subscription_details" and re.search(r'\b(subscription|subscriptions|meal plan|everyday meal|meal subscription|5 meals package|5 meals)\b', lowered_text):
                    score += 80
                elif tmpl_id == "bento_details" and re.search(r'\b(bento|bengali bento|bento box|bento details|bento tiers|bento prices|bento pricing|bento meal|bintow)\b', lowered_text):
                    score += 80
                elif tmpl_id == "savoury_food_intro" and re.search(r'\b(lunch menu|dinner options|savoury food|bengali meals|lunch options|dinner menu|savoury menu|bengali lunch|bengali dinner|3 ways to order|savoury dishes|savoury ordering)\b', lowered_text):
                    score += 80
                elif tmpl_id == "alacarte_prebooking_steps" and re.search(r'\b(how to order from menu|how to order item from menu|pre-book|prebook|pre book|pre-order savoury|preorder savoury|how to order alacarte|order alacarte|order à la carte|pre-order ala carte)\b', lowered_text):
                    score += 80
                elif tmpl_id == "sondesh_dispatch_time" and re.search(r'\b(how long to deliver|dispatch time|how fast can you deliver|dispatch timing|prep time|how long to dispatch|sondesh dispatch time|how fast can you dispatch)\b', lowered_text):
                    score += 80
                elif tmpl_id == "sondesh_price_detailed" and re.search(r'\b(sondesh price|price of sondesh|cost of sondesh|how much is sondesh|price list|sondesh cost|sondesh rate|how many pieces)\b', lowered_text):
                    has_specific_flavour = any(fl in lowered_text for fl in ["nolen gur", "kanchagolla", "250g", "500g", "1kg", "jolbhora", "mahua", "kesar", "rose", "mango", "vanilla", "cinnamon"])
                    if not has_specific_flavour:
                        score += 70
                elif tmpl_id == "savoury_best_time_to_book" and re.search(r'\b(best time to order|when should i book|when to book|best time to book|when should i order|how early to book|how far in advance|when to place order|best time to place order)\b', lowered_text):
                    score += 80
                elif tmpl_id == "savoury_delivery_time" and re.search(r'\b(savoury delivery time|delivery time for food|how long for food|when will my meal arrive|food dispatch time|meal delivery time|how long for lunch|how long for dinner|food delivery time|when will meal arrive|delvery time)\b', lowered_text):
                    score += 80
                elif tmpl_id == "order_status_tracking" and re.search(r'\b(order status|where is my order|ordr status|where is my ordr|ordr)\b', lowered_text):
                    score += 80
                elif tmpl_id == "kolkata_expansion_info" and re.search(r'\b(kolkata|moving to kolkata|leaving delhi|expansion to kolkata|who runs the kitchen now|kolkata kitchen|relocating to kolkata)\b', lowered_text):
                    score += 80
                elif tmpl_id == "sondesh_crosssell_savoury" and re.search(r'\b(sondesh crosssell|also savoury|also meals|do you have food too|bengali meals|also cook meals|food menu too|do you also cook meals|cook bengali meals)\b', lowered_text):
                    score += 80
                elif tmpl_id == "how_to_order" and re.search(r'\b(how to order|how do i order|how can i order|how to place an order|how do i place an order|i want to place an order|how to buy)\b', lowered_text):
                    score += 80

            # Calculate Tier Multiplier
            if tmpl_id in TIER_1_GUARDS:
                tier_multiplier = 10000
            elif tmpl_id in TIER_2_SPECIFIC_INTENTS:
                tier_multiplier = 1000
            elif tmpl_id in TIER_3_GENERIC_INFO:
                tier_multiplier = 100
            else: # TIER_4_GREETING_INTRO
                tier_multiplier = 1

            composite_score = (score * tier_multiplier) if score >= 4 else 0

            if composite_score > best_composite_score:
                runner_up_composite = best_composite_score
                runner_up_template = best_template
                best_composite_score = composite_score
                best_template = tmpl
                best_score = score
            elif composite_score > runner_up_composite:
                runner_up_composite = composite_score
                runner_up_template = tmpl

        # Require a minimum matching threshold (score >= 4) in hybrid/deterministic mode
        if best_template is not None:
            log.debug(
                "MATCH_SCORE msg=%r best=%s score=%s runner_up=%s",
                user_message, best_template.get("id"), best_score,
                runner_up_template.get("id") if runner_up_template else None,
            )

        if best_template and best_score >= 4:
            tmpl_id = best_template.get("id", "")
            facts = session_memory.get_facts(user_id, channel)
            if tmpl_id == "greeting":
                responses = best_template.get("responses", [])
                last_index = facts.get("last_greeting_index")
                available_indices = [
                    index for index in range(len(responses)) if index != last_index
                ]
                if available_indices:
                    selected_index = random.choice(available_indices)
                    raw_response = responses[selected_index]
                    facts["last_greeting_index"] = selected_index
                else:
                    raw_response = None
            else:
                raw_response = best_template.get("response")

            # Hard Code-Level Safety Checks (Rules 3 & 4):
            # 1. Any template marked escalate: true triggers escalation.
            # 2. If a template has response: null (or empty string), it must NEVER be auto-sent — force escalation.
            is_null_or_empty_response = (raw_response is None) or (isinstance(raw_response, str) and not raw_response.strip())
            force_escalate = (best_template.get("escalate") is True) or is_null_or_empty_response

            if force_escalate:
                escalate_image = None
                if is_null_or_empty_response:
                    # Nothing approved to say — use the intent-aware handover notice.
                    fallback, _intent = self.build_handover_response(user_message)
                else:
                    # The template is escalation-flagged but has purpose-written copy
                    # (e.g. order_next_steps). Send that copy rather than the generic
                    # notice; the caller still escalates because was_matched is False.
                    fallback, escalate_image = self._format_dynamic_response(
                        tmpl_id, raw_response, user_id=user_id, channel=channel, user_message=user_message
                    )
                    if tmpl_id == "order_next_steps":
                        session_memory.clear_order_draft(user_id, channel)
                if intro_greeting_prefix:
                    fallback = intro_greeting_prefix + fallback
                session_memory.add_turn(user_id, channel, user_message, fallback)
                return fallback, False, tmpl_id, escalate_image

            response_text, image_url = self._format_dynamic_response(
                tmpl_id, raw_response or "", user_id=user_id, channel=channel, user_message=user_message
            )
            
            # Dynamic Name Personalization & Order Draft reset on order confirmation
            if tmpl_id == "name_introduction":
                facts["awaiting_name"] = False
                if facts.get("name"):
                    response_text = f"Nice to meet you, {facts['name']}! 👋 Welcome to Camellia Artisanal. Ask us about our Sondesh flavours & prices, weekly menu, or delivery — happy to help!"
                else:
                    response_text = "Nice to meet you! 👋 Welcome to Camellia Artisanal. Ask us about our Sondesh flavours & prices, weekly menu, or delivery — happy to help!"
            elif tmpl_id == "recall_name":
                if facts.get("name"):
                    response_text = f"Yes! Your name is {facts['name']}. 😊 How can I assist you today with our Sondesh or weekly menu?"
                    facts["awaiting_name"] = False
                else:
                    response_text = "You haven't told me your name yet! What should I call you?"
                    facts["awaiting_name"] = True
            elif tmpl_id == "subscription_details":
                facts["awaiting_subscription_booking"] = True
            elif tmpl_id == "subscription_booking_steps":
                facts["awaiting_subscription_booking"] = False
            elif tmpl_id == "order_next_steps":
                session_memory.clear_order_draft(user_id, channel)

            # First-time customer welcoming line for savoury food intro
            if tmpl_id == "savoury_food_intro" and not session_memory.has_prior_history(user_id, channel):
                first_time_nudge = "\n\nIf this is your first Camellia meal and the menu feels unfamiliar, I'll be happy to help you choose!"
                response_text = response_text.rstrip() + first_time_nudge

            # One-time savoury cross-sell nudge logic
            savoury_templates = {
                "savoury_food_intro", "weekly_menu_veg", "weekly_menu_nonveg",
                "combo_bento", "alacarte_prebooking_steps",
                "subscription_details", "subscription_booking_steps"
            }
            if tmpl_id in savoury_templates and not facts.get("savoury_crosssell_sent"):
                facts["savoury_crosssell_sent"] = True
                crosssell_nudge = "\n\n*P.S.* We also handcraft Bengali mishti — mild sweet, subtle natural flavours. Say *Sondesh* for prices."
                response_text = response_text.rstrip() + crosssell_nudge
            elif tmpl_id == "savoury_crosssell_sondesh":
                facts["savoury_crosssell_sent"] = True

            # One-time sondesh cross-sell nudge logic (reverse direction: sondesh -> savoury)
            sondesh_templates = {
                "sondesh_price_detailed", "sondesh_price_general",
                "classic_collection_details", "signature_collection_details",
                "aamar_mishti_details", "signature_sondesh_details",
                "jolbhora_talgash_details", "shelf_life_storage"
            }
            if tmpl_id in sondesh_templates and not facts.get("sondesh_crosssell_sent"):
                facts["sondesh_crosssell_sent"] = True
                crosssell_nudge = "\n\n*P.S.* We also cook fresh Bengali meals for Delhi NCR — say *Meals* for this week's menu."
                response_text = response_text.rstrip() + crosssell_nudge
            elif tmpl_id == "sondesh_crosssell_savoury":
                facts["sondesh_crosssell_sent"] = True

            if tmpl_id != "greeting" and config.is_ai_enabled and gemini_responder.get_active_provider() != "none":
                try:
                    user_ctx = f"Customer Name: {facts.get('name', 'Unknown')}"
                    refined_text, is_refined = gemini_responder.refine_template_response(tmpl_id, response_text, user_message, user_context=user_ctx)
                    if is_refined and refined_text:
                        response_text = refined_text
                except Exception as e:
                    log.warning("AI template refinement failed: %s", e)

            if intro_greeting_prefix and not response_text.startswith("Nice to meet you"):
                if response_text.startswith("Hi! "):
                    response_text = response_text[4:]
                elif response_text.startswith("Hello! "):
                    response_text = response_text[7:]
                response_text = intro_greeting_prefix + response_text

            if tmpl_id in TOPIC_MAP:
                facts["last_topic"] = TOPIC_MAP[tmpl_id]

            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, tmpl_id, image_url

        # Check Active AI Provider (Gemini / OpenAI RAG) when enabled
        fallback, _intent = self.build_handover_response(user_message)
        if intro_greeting_prefix:
            fallback = intro_greeting_prefix + fallback

        # Retrieve from approved QA/learned sources before any generative provider.
        # High-confidence approved retrieval may answer; lower-confidence retrieval
        # is logged for review and continues to the safe human fallback.
        rag_res = None
        try:
            rag_res = rag_fallback_engine.retrieve(user_message)
            if rag_res.auto_response:
                response_text, image_url = self._format_dynamic_response(
                    rag_res.proposed_intent,
                    rag_res.auto_response,
                    user_id=user_id,
                    channel=channel,
                    user_message=user_message,
                )
                db.log_rag_audit(
                    user_message=user_message,
                    matched_template=None,
                    rag_invoked=True,
                    retrieved_sources=rag_res.retrieved_sources,
                    confidence_score=rag_res.confidence_score,
                    proposed_template=rag_res.proposed_intent,
                    proposed_response=rag_res.proposed_response,
                    approval_status=rag_res.approval_status,
                    final_customer_response=response_text,
                )
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, rag_res.proposed_intent, image_url
        except Exception as e:
            log.error("RAG retrieval error: %s", e)

        # Generative providers may suggest candidate answers for review only.
        if config.is_ai_enabled and gemini_responder.get_active_provider() != "none":
            try:
                user_ctx = f"Customer Name: {facts.get('name', 'Unknown')}"
                ai_resp, ok, reason = gemini_responder.generate_response(user_message, user_context=user_ctx)
                if ok and ai_resp:
                    # Controlled Architecture: Save AI generated response as candidate template for admin review (PENDING_APPROVAL).
                    # DO NOT return AI generated text directly to the customer.
                    try:
                        db.save_candidate_template(
                            name="gemini_rag_candidate",
                            response=ai_resp,
                            user_message=user_message,
                            source_refs=["gemini_responder"],
                            confidence=0.85,
                            created_by="gemini_responder"
                        )
                    except Exception as ce:
                        log.warning("Could not save Gemini candidate template: %s", ce)
            except Exception as e:
                log.warning("AI RAG generation failed: %s", e)

        # Fallback to Controlled Isolated RAG Engine when no approved answer matched.
        try:
            if rag_res is None:
                rag_res = rag_fallback_engine.retrieve(user_message)
            db.log_rag_audit(
                user_message=user_message,
                matched_template=None,
                rag_invoked=True,
                retrieved_sources=rag_res.retrieved_sources,
                confidence_score=rag_res.confidence_score,
                proposed_template=rag_res.proposed_intent if rag_res.confidence_score > 0 else None,
                proposed_response=rag_res.proposed_response if rag_res.confidence_score > 0 else None,
                approval_status=rag_res.approval_status if rag_res.confidence_score > 0 else "FAILED",
                final_customer_response=fallback
            )
        except Exception as e:
            log.error("RAG fallback processing error: %s", e)

        # Nothing matched. If the message was simply too vague to place ("india",
        # "is it good", "anything else"), asking what they meant beats claiming we
        # notified staff — which also would not have been true. Only once: a
        # customer who is still unclear after being asked gets a real human.
        if (
            _intent == "unknown"
            and not facts.get("clarify_sent")
            and self._is_vague_enough_to_clarify(user_message)
        ):
            facts["clarify_sent"] = True
            clarifier = (
                "Happy to help — I just want to point you the right way 🌸\n\n"
                "Which of these is closest to what you're after?\n"
                "• 🌸 Sondesh flavours & prices\n"
                "• 🍱 This week's Bengali meals\n"
                "• 🚚 Delivery to your area\n"
                "• 🛍️ Placing an order\n\n"
                "Or tell me in a few more words and I'll take it from there."
            )
            if intro_greeting_prefix:
                clarifier = intro_greeting_prefix + clarifier
            log.info("Asked %s to clarify a vague message instead of escalating.", user_id)
            session_memory.add_turn(user_id, channel, user_message, clarifier)
            return clarifier, True, "clarify_request", None

        session_memory.add_turn(user_id, channel, user_message, fallback)
        return fallback, False, None, None

    @staticmethod
    def _is_vague_enough_to_clarify(user_message: str) -> bool:
        """
        True only when a message carries almost no content of its own, so asking
        what they meant is genuinely useful.

        A short message is not the same as a vague one: "Do you serve steamed
        bhapa pitha?" is brief but perfectly specific, and it names something we
        could not answer — that is a real gap for a human, not something to
        answer with a menu of options the customer has to re-read.
        """
        if not user_message:
            return False
        # Anything naming a concrete problem goes straight to a human.
        if re.search(
            r"\b(refund|cancel|complaint|urgent|wrong|missing|late|address|invoice|"
            r"gst|bill|receipt|custom|bulk|wedding|corporate|allerg\w*)\b",
            user_message, re.IGNORECASE
        ):
            return False

        words = [w.lower() for w in re.findall(r"[a-zA-Zঀ-৿]+", user_message)]
        if not words:
            return False
        substantive = [w for w in words if w not in VAGUE_FILLER_WORDS]
        # At most one content word means there is genuinely nothing to go on.
        # Two or more names something definite — "bhapa pitha", "capital france" —
        # and an out-of-scope or unanswerable specific belongs with a human, per
        # the out-of-syllabus rule, rather than being redirected to a menu.
        return len(substantive) <= 1

    def _detect_relevant_images(self, user_message: str) -> Optional[Any]:
        """Attaches relevant menu photo cards when user inquires about products or menus in AI-First mode."""
        lowered = user_message.lower().strip()
        img_till_thursday = "/images/Till%20Thursday.jpeg"
        img_from_friday = "/images/From%20Friday.jpeg"
        img_signature_sondesh = "/images/Signature%20Sondesh.jpeg"
        img_aamar_mishti = "/images/AAMAR%20MISHTI.jpeg"

        import datetime, zoneinfo
        try:
            ist = zoneinfo.ZoneInfo("Asia/Kolkata")
        except Exception:
            ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        today_weekday = datetime.datetime.now(tz=ist).weekday()
        current_weekly_img = img_from_friday if today_weekday in [4, 5, 6] else img_till_thursday

        if any(w in lowered for w in ["collection 1", "collection1", "1st collection", "first collection", "aamar mishti", "col 1", "option 1"]):
            return [img_aamar_mishti]
        elif any(w in lowered for w in ["collection 2", "collection2", "2nd collection", "second collection", "signature", "desi a2", "col 2", "option 2", "mahua"]):
            return [img_signature_sondesh]
        elif any(w in lowered for w in ["product", "all products", "what do you have", "what do you sell", "overview", "know more", "full menu", "both collections", "difference", "custom", "customize", "customise", "assorted", "mix"]):
            return [img_aamar_mishti, img_signature_sondesh, current_weekly_img]
        elif any(w in lowered for w in ["sondesh", "mishti", "sweet", "sweets", "kanchagolla", "jolbhora", "nolen gur", "250g", "500g", "1kg", "rose", "classic", "mango", "kesar", "price"]):
            return [img_aamar_mishti, img_signature_sondesh]
        elif any(w in lowered for w in ["menu", "veg", "non veg", "non-veg", "bento", "combo", "meal", "meals", "lunch", "dinner", "halal", "oil", "ghee", "savoury"]):
            return current_weekly_img
        return None

    def _format_dynamic_response(
        self, template_id: str, default_response: str, user_id: str = None, channel: str = None, user_message: str = ""
    ) -> Tuple[str, Optional[Any]]:
        """Attaches the exact photo card(s) a template should send alongside its text whenever requested."""
        img_till_thursday = "/images/Till%20Thursday.jpeg"
        img_from_friday = "/images/From%20Friday.jpeg"
        img_signature_sondesh = "/images/Signature%20Sondesh.jpeg"
        img_aamar_mishti = "/images/AAMAR%20MISHTI.jpeg"

        import datetime, zoneinfo
        try:
            ist = zoneinfo.ZoneInfo("Asia/Kolkata")
        except Exception:
            # zoneinfo available Python 3.9+; fallback to fixed +5:30 offset
            ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        today_weekday = datetime.datetime.now(tz=ist).weekday()  # 0=Mon … 6=Sun
        current_weekly_img = img_from_friday if today_weekday in [4, 5, 6] else img_till_thursday

        target_images = None
        formatted_text = default_response
        lowered_msg = (user_message or "").lower().strip()

        # "I want to know more" & Product Overview -> both collection cards + this week's meal menu
        if template_id in ["know_more", "product_overview"]:
            target_images = [img_aamar_mishti, img_signature_sondesh, current_weekly_img]

        # Collection 1 specific queries — send ONLY Collection 1 photo card
        elif template_id in ["aamar_mishti_collection", "nolen_gur_aamar"]:
            target_images = [img_aamar_mishti]

        # Collection 2 specific queries — send ONLY Collection 2 photo card
        elif template_id in ["signature_collection", "mahua_signature_special"]:
            target_images = [img_signature_sondesh]

        # Classic & Rose Sondesh — dedicated formatting based on query specificity
        elif template_id == "sondesh_classic_rose":
            has_rose = "rose" in lowered_msg
            has_classic = "classic" in lowered_msg
            has_col1 = any(w in lowered_msg for w in ["collection 1", "collection1", "aamar mishti", "1st collection", "first collection", "col 1", "option 1"])
            has_col2 = any(w in lowered_msg for w in ["collection 2", "collection2", "signature", "desi a2", "2nd collection", "second collection", "col 2", "option 2"])

            if has_col1 and not has_col2:
                target_images = [img_aamar_mishti]
                if has_rose and not has_classic:
                    formatted_text = "*Collection 1 — Aamar Mishti (Rose Sondesh)* 🌸\nFresh artisanal chhana infused with fragrant organic rose petals, made with pure dairy milk in our kitchen:\n\n```\n+-----------------------+-------+-------+-------+\n| Pack Size             | 250g  | 500g  |  1kg  |\n+-----------------------+-------+-------+-------+\n| Piece Count           | 9 pcs | 18 pcs| 36 pcs|\n| Price (INR)           | ₹450  | ₹800  | ₹1500 |\n+-----------------------+-------+-------+-------+\n```\n\nWhich pack size (250g, 500g, or 1kg) would you like to order?"
                elif has_classic and not has_rose:
                    formatted_text = "*Collection 1 — Aamar Mishti (Classic Sondesh)* 🌸\nFresh artisanal chhana and delicate aromatic cardamom, made with pure dairy milk in our kitchen:\n\n```\n+-----------------------+-------+-------+-------+\n| Pack Size             | 250g  | 500g  |  1kg  |\n+-----------------------+-------+-------+-------+\n| Piece Count           | 9 pcs | 18 pcs| 36 pcs|\n| Price (INR)           | ₹450  | ₹800  | ₹1500 |\n+-----------------------+-------+-------+-------+\n```\n\nWhich pack size (250g, 500g, or 1kg) would you like to order?"
                else:
                    formatted_text = "*Collection 1 — Aamar Mishti (Classic & Rose Sondesh)* 🌸\nMade with pure dairy milk in our kitchen:\n\n```\n+-----------------------+-------+-------+-------+\n| Item / Flavour        | 250g  | 500g  |  1kg  |\n+-----------------------+-------+-------+-------+\n| Classic / Rose        | ₹450  | ₹800  | ₹1500 |\n+-----------------------+-------+-------+-------+\n* Box count: 9 pcs (250g) | 18 pcs (500g) | 36 pcs (1kg)\n```\n\nWhich flavour and pack size would you like to order?"

            elif has_col2 and not has_col1:
                target_images = [img_signature_sondesh]
                if has_rose and not has_classic:
                    formatted_text = "*Collection 2 — Signature A2 (Rose Sondesh)* 🌸\n100% handcrafted with pure desi A2 cow milk and fragrant organic rose petals:\n\n```\n+-----------------------+-------+-------+-------+\n| Pack Size             | 250g  | 500g  |  1kg  |\n+-----------------------+-------+-------+-------+\n| Piece Count           | 9 pcs | 18 pcs| 36 pcs|\n| Price (INR)           | ₹600  | ₹1100 | ₹2000 |\n+-----------------------+-------+-------+-------+\n```\n\nWhich pack size (250g, 500g, or 1kg) would you like to order?"
                elif has_classic and not has_rose:
                    formatted_text = "*Collection 2 — Signature A2 (Classic Sondesh)* 🌸\n100% handcrafted with pure desi A2 cow milk and delicate aromatic cardamom:\n\n```\n+-----------------------+-------+-------+-------+\n| Pack Size             | 250g  | 500g  |  1kg  |\n+-----------------------+-------+-------+-------+\n| Piece Count           | 9 pcs | 18 pcs| 36 pcs|\n| Price (INR)           | ₹600  | ₹1100 | ₹2000 |\n+-----------------------+-------+-------+-------+\n```\n\nWhich pack size (250g, 500g, or 1kg) would you like to order?"
                else:
                    formatted_text = "*Collection 2 — Signature A2 (Classic & Rose Sondesh)* 🌸\n100% handcrafted with pure desi A2 cow milk:\n\n```\n+-----------------------+-------+-------+-------+\n| Item / Flavour        | 250g  | 500g  |  1kg  |\n+-----------------------+-------+-------+-------+\n| Classic / Rose        | ₹600  | ₹1100 | ₹2000 |\n+-----------------------+-------+-------+-------+\n* Box count: 9 pcs (250g) | 18 pcs (500g) | 36 pcs (1kg)\n```\n\nWhich flavour and pack size would you like to order?"

            elif has_rose and not has_classic:
                target_images = [img_aamar_mishti, img_signature_sondesh]
                formatted_text = "Our handcrafted *Rose Sondesh* 🌸 is made with fresh artisanal chhana infused with fragrant organic rose petals. Available in both collections:\n\n*Rose Sondesh Price Breakdown:*\n```\n+-------------------------------------+-------+-------+-------+\n| Collection                          | 250g  | 500g  |  1kg  |\n+-------------------------------------+-------+-------+-------+\n| Collection 1 — Aamar Mishti (Dairy) | ₹450  | ₹800  | ₹1500 |\n| Collection 2 — Signature (Desi A2)  | ₹600  | ₹1100 | ₹2000 |\n+-------------------------------------+-------+-------+-------+\n* Box count: 9 pcs (250g) | 18 pcs (500g) | 36 pcs (1kg)\n```\n\n• *Collection 1 (Aamar Mishti)*: Daily indulgence, good dairy milk, partially mechanised, made in-house at our kitchen.\n• *Collection 2 (Signature A2)*: Special occasions & gifting — made with pure desi A2 cow milk (not industrially processed), completely handcrafted end to end.\n\nWhich collection and pack size would you like to order (250g, 500g, or 1kg)?"

            elif has_classic and not has_rose:
                target_images = [img_aamar_mishti, img_signature_sondesh]
                formatted_text = "Our traditional *Classic Sondesh* 🌸 is made with fresh artisanal chhana and delicate aromatic cardamom. Available in both collections:\n\n*Classic Sondesh Price Breakdown:*\n```\n+-------------------------------------+-------+-------+-------+\n| Collection                          | 250g  | 500g  |  1kg  |\n+-------------------------------------+-------+-------+-------+\n| Collection 1 — Aamar Mishti (Dairy) | ₹450  | ₹800  | ₹1500 |\n| Collection 2 — Signature (Desi A2)  | ₹600  | ₹1100 | ₹2000 |\n+-------------------------------------+-------+-------+-------+\n* Box count: 9 pcs (250g) | 18 pcs (500g) | 36 pcs (1kg)\n```\n\n• *Collection 1 (Aamar Mishti)*: Daily indulgence, good dairy milk, partially mechanised, made in-house at our kitchen.\n• *Collection 2 (Signature A2)*: Special occasions & gifting — made with pure desi A2 cow milk (not industrially processed), completely handcrafted end to end.\n\nWhich collection and pack size would you like to order (250g, 500g, or 1kg)?"

            else:
                target_images = [img_aamar_mishti, img_signature_sondesh]

        # Other Sondesh price / flavour queries
        elif template_id in [
            "sondesh_price_general", "sondesh_price_detailed",
            "sondesh_mango_kesar", "sondesh_nolen_gur",
            "jolbhora", "kanchagolla", "pack_size_250g", "pack_size_500g", "pack_size_1kg",
            "jolbhora_talgash_details", "sugar_free_options", "allergen_nut_safety"
        ]:
            if any(w in lowered_msg for w in ["collection 1", "collection1", "aamar mishti", "1st collection", "first collection", "col 1", "option 1"]):
                target_images = [img_aamar_mishti]
            elif any(w in lowered_msg for w in ["collection 2", "collection2", "signature", "desi a2", "2nd collection", "second collection", "col 2", "option 2"]):
                target_images = [img_signature_sondesh]
            else:
                target_images = [img_aamar_mishti, img_signature_sondesh]

        # Savoury food intro & Sondesh->Savoury cross-sell — attach current weekly menu image card alongside text
        elif template_id in ["savoury_food_intro", "sondesh_crosssell_savoury"]:
            target_images = current_weekly_img
            formatted_text = default_response

        # Weekly menu & bento photo card queries
        elif template_id in ["weekly_menu_veg", "weekly_menu_nonveg", "combo_bento"]:
            target_images = current_weekly_img
            weekly_menus = self.menu_data.get("weekly_menus", [])
            # Select appropriate rotation based on day of week if multiple rotations exist
            if today_weekday in [4, 5, 6] and len(weekly_menus) > 1:
                current_week = weekly_menus[-1]
            elif weekly_menus:
                current_week = weekly_menus[0]
            else:
                current_week = {}

            week_name = current_week.get("week_of", "Current Rotation")
            service_window = current_week.get("service_window", "Friday Lunch – Thursday Dinner")

            if template_id == "weekly_menu_veg":
                veg_list = current_week.get("vegetarian", [])
                if veg_list:
                    lines = [
                        f"🌸 *Camellia Artisanal — This Week's Vegetarian Menu* 🌿",
                        f"_{week_name} ({service_window})_\n"
                    ]
                    for itm in veg_list:
                        name = itm.get("item", "")
                        desc = itm.get("description", "")
                        qty = itm.get("quantity", "")
                        price = itm.get("price_inr", "")
                        desc_str = f" • _{desc}_" if desc else ""
                        qty_str = f" ({qty})" if qty else ""
                        lines.append(f"• *{name}*{qty_str} — *₹{price}*{desc_str}")
                    
                    lines.append("\n📦 *Packaging*: 100% plastic-free, eco-friendly cornstarch boxes.")
                    lines.append("🚚 *Delivery*: Across Delhi NCR from Sushant Arcade, Gurgaon (Courier on actuals).")
                    lines.append("\nWhich vegetarian dishes would you like to order? 🙏")
                    formatted_text = "\n".join(lines)
                else:
                    formatted_text = default_response

            elif template_id == "weekly_menu_nonveg":
                nonveg_list = current_week.get("non_vegetarian", [])
                if nonveg_list:
                    lines = [
                        f"🌸 *Camellia Artisanal — This Week's Non-Vegetarian Menu* 🍗",
                        f"_{week_name} ({service_window})_\n"
                    ]
                    for itm in nonveg_list:
                        name = itm.get("item", "")
                        desc = itm.get("description", "")
                        qty = itm.get("quantity", "")
                        price = itm.get("price_inr", "")
                        desc_str = f" • _{desc}_" if desc else ""
                        qty_str = f" ({qty})" if qty else ""
                        lines.append(f"• *{name}*{qty_str} — *₹{price}*{desc_str}")
                    
                    lines.append("\n📦 *Packaging*: 100% plastic-free, eco-friendly cornstarch boxes.")
                    lines.append("🚚 *Delivery*: Across Delhi NCR from Sushant Arcade, Gurgaon (Courier on actuals).")
                    lines.append("\nWhich non-vegetarian dishes would you like to order? 🙏")
                    formatted_text = "\n".join(lines)
                else:
                    formatted_text = default_response

            elif template_id == "combo_bento":
                bento_list = current_week.get("bengali_bento", []) or current_week.get("combos", [])
                if bento_list:
                    lines = [
                        f"🌸 *Camellia Artisanal — Bengali Bento Meals & Combos* 🍱",
                        f"_{week_name} ({service_window})_\n"
                    ]
                    for b in bento_list:
                        name = b.get("name", "")
                        price = b.get("price_inr", "")
                        includes = ", ".join(b.get("includes", []))
                        inc_str = f" (Includes: {includes})" if includes else ""
                        lines.append(f"• *{name}* — *₹{price}*{inc_str}")
                    
                    lines.append("\n📦 *Packaging*: 100% plastic-free, eco-friendly cornstarch boxes.")
                    lines.append("🚚 *Delivery*: Across Delhi NCR from Sushant Arcade, Gurgaon.")
                    lines.append("\nWhich Bento meal would you like to order? 🌸")
                    formatted_text = "\n".join(lines)
                else:
                    formatted_text = default_response

        elif template_id == "recall_order_flavour":
            session = session_memory.get_session(user_id or "default_user", channel or "default_channel")
            flv = session.get("facts", {}).get("order_draft", {}).get("flavour")
            if flv:
                formatted_text = f"Yes! We already have your selected flavour (*{flv.title()}*) saved in your active session — no need to repeat it at all! 🌸 Which pack size (250g, 500g, or 1kg) and delivery address would you like to proceed with?"
            else:
                formatted_text = "I don't have a flavour saved yet. Which flavour (Classic, Rose, Nolen Gur, Mango, Kesar, Jolbhora) would you like? 🌸"

        elif template_id == "artisanal_differentiation":
            target_images = [img_aamar_mishti, img_signature_sondesh]

        if target_images:
            return formatted_text, target_images

        return formatted_text, None

    def respond(
        self,
        user_message: str,
        user_id: str = None,
        channel: str = None,
        referral_source: str = None,
        referral_ad_id: str = None
    ) -> BotResponse:
        """Convenience wrapper returning a structured BotResponse object with .text attribute."""
        text, was_matched, tmpl_id, image_url = self.match_and_respond(
            user_message, user_id, channel, referral_source, referral_ad_id
        )
        return BotResponse(text=text, was_matched=was_matched, template_id=tmpl_id, image_url=image_url)

responder = DeterministicResponder()
