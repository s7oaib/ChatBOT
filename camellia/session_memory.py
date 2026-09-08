import re
from collections import OrderedDict
from typing import Dict, Any

class SessionMemoryManager:
    """
    Manages in-memory active chat sessions keyed by (user_id, channel).
    Extracts customer facts (Name) and maintains conversation turn history
    for multi-turn context and human escalation handoffs.
    """
    def __init__(self, max_history_turns: int = 5, max_sessions: int = 5000):
        self.max_history_turns = max_history_turns
        self.max_sessions = max_sessions
        # Structure: {(user_id, channel): {"facts": {"name": str}, "turns": [...]}}
        # Ordered so the least-recently-touched session can be evicted at capacity.
        self._sessions: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()

    def get_session(self, user_id: str, channel: str) -> Dict[str, Any]:
        import time
        current_time = time.time()
        key = (str(user_id), str(channel))
        if key not in self._sessions:
            self._sessions[key] = {
                "last_updated": current_time,
                "facts": {
                    "name": None,
                    "last_greeting_index": None,
                    "image_card_sent": False,
                    "awaiting_subscription_booking": False,
                    "savoury_crosssell_sent": False,
                    "sondesh_crosssell_sent": False,
                    "previous_menus_offered": False,
                    "slow_notice_sent": False,
                    "awaiting_menu_choice": False,
                    "awaiting_product_price": False,
                    "awaiting_area_confirmation": False,
                    "awaiting_instagram_contact": False,
                    "pending_instagram_escalation": None,
                    "clarify_sent": False,
                    "last_topic": None,
                    "cart": [],
                    "order_draft": {
                        "flavour": None,
                        "collection": None,
                        "size": None,
                        "quantity": 1
                    }
                },
                "turns": [] # List of {"role": "user"|"assistant", "content": str}
            }
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
        else:
            session = self._sessions[key]
            session.setdefault("facts", {}).setdefault("last_greeting_index", None)
            last_updated = session.get("last_updated", current_time)
            if current_time - last_updated > 86400:
                facts = session.get("facts", {})
                facts["image_card_sent"] = False
                facts["awaiting_subscription_booking"] = False
                facts["last_greeting_index"] = None
                facts["savoury_crosssell_sent"] = False
                facts["sondesh_crosssell_sent"] = False
                facts["slow_notice_sent"] = False
                facts["awaiting_menu_choice"] = False
                facts["awaiting_product_price"] = False
                facts["awaiting_area_confirmation"] = False
                facts["awaiting_instagram_contact"] = False
                facts["pending_instagram_escalation"] = None
                facts["clarify_sent"] = False
                facts["last_topic"] = None
                facts["cart"] = []
                facts["order_draft"] = {
                    "flavour": None,
                    "collection": None,
                    "size": None,
                    "quantity": 1
                }
            session["last_updated"] = current_time
            self._sessions.move_to_end(key)
        return self._sessions[key]

    def clear_session(self, user_id: str, channel: str) -> None:
        key = (str(user_id), str(channel))
        if key in self._sessions:
            del self._sessions[key]

    def get_facts(self, user_id: str, channel: str) -> Dict[str, Any]:
        """Returns the facts dictionary for a session."""
        return self.get_session(user_id, channel)["facts"]

    def clear_order_draft(self, user_id: str, channel: str) -> None:
        """Resets the active order draft and cart for a session."""
        session = self.get_session(user_id, channel)
        session["facts"]["cart"] = []
        session["facts"]["order_draft"] = {
            "flavour": None,
            "collection": None,
            "size": None,
            "quantity": 1
        }

    def extract_and_update_facts(self, user_id: str, channel: str, text: str) -> Dict[str, Any]:
        """Extracts customer attributes (Name) & order preferences (Flavour, Collection, Size, Quantity) from user message."""
        session = self.get_session(user_id, channel)
        facts = session["facts"]
        if "order_draft" not in facts:
            facts["order_draft"] = {"flavour": None, "collection": None, "size": None, "quantity": 1}
        order_draft = facts["order_draft"]
        clean_text = text.strip()
        text_lower = clean_text.lower()

        # 1. Extract Name
        extracted_name = None

        # "I am ..." is far more often a self-description than an introduction.
        # Treating "I am diabetic" as a name meant greeting a customer as
        # "Diabetic" and never answering what they disclosed, so these words are
        # never accepted as a name.
        self_descriptor_words = {
            "vegan", "vegetarian", "veggie", "nonveg", "eggless", "jain", "halal", "kosher",
            "diabetic", "diabetes", "allergic", "allergy", "intolerant", "lactose", "gluten",
            "celiac", "coeliac", "pregnant", "sick", "ill", "unwell", "fasting",
            "new", "old", "regular", "local", "back", "confused", "sorry", "angry", "upset",
            "waiting", "coming", "calling", "asking", "hungry", "vegetarians", "vegans",
        }

        ignored_name_words = {
            "a", "an", "the", "in", "at", "from", "near", "to", "around", "on", "into", "with", "by", "for",
            "located", "living", "based", "staying", "visiting", "asking", "looking", "ordering", "wanting", "having",
            "here", "there", "interested", "trying", "planning", "searching", "buying", "ready", "going",
            "wondering", "thinking", "sure", "ok", "okay", "fine", "good", "menu", "sondesh", "sandesh",
            "mishti", "price", "prices", "cost", "veg", "nonveg", "non-veg", "food", "delhi", "gurgaon",
            "noida", "order", "delivery", "cancel", "refund", "hi", "hello", "hey", "namaste", "yes", "no",
            "human", "manager", "agent", "bot", "help", "please", "thanks", "thank", "what", "whats", "what's",
            "who", "where", "how", "when", "why", "just", "also", "still", "only", "not", "so", "very"
        }

        name_patterns = [
            # "My name is John Doe" or "Name: Priya"
            r"\b(?:my\s+name\s+(?:is|'s)|my\s+names|call\s+me|name\s*[:=])\s+([A-Za-z]{2,25}(?:\s+[A-Za-z]{2,25})?)\b",
            # "Hi! I'm Priya Sen. What is..." or "I am Sourav" or "Myself Priya Sen" or "It's Ananya"
            r"(?:^|[.!?]\s*|\b(?:hi|hello|hey|namaste)\b[!,.]*\s*)(?:i\s+am|i'm|im|myself|this\s+is|it's|its)\s+([A-Za-z]{2,25}(?:\s+[A-Za-z]{2,25})?)\b",
        ]

        for pattern in name_patterns:
            match = re.search(pattern, clean_text, re.IGNORECASE)
            if match:
                raw_name = match.group(1).strip().strip(".,!?:;\"'")
                words = raw_name.split()
                if len(words) > 1 and words[1].lower() in ignored_name_words:
                    raw_name = words[0]

                first_word = raw_name.split()[0].lower() if raw_name else ""
                if (
                    raw_name
                    and first_word not in ignored_name_words
                    and first_word not in self_descriptor_words
                    and raw_name.lower() not in ignored_name_words
                    and not any(w.lower() in self_descriptor_words for w in raw_name.split())
                    and 2 <= len(raw_name) <= 30
                    and not any(char.isdigit() for char in raw_name)
                    and not any(term in raw_name.lower() for term in ["sondesh", "mishti", "menu", "price", "delivery", "bento", "box", "noida", "delhi", "gurgaon", "sector"])
                ):
                    extracted_name = raw_name.title()
                    break

        # Check conversational context: Was the bot just asking for the user's name?
        # (e.g. from recall_name 'What should I call you?' or awaiting_name flag)
        turns = session.get("turns", [])
        last_bot_turn = turns[-1]["content"] if (turns and turns[-1].get("role") == "assistant") else ""
        was_asked_name = (
            facts.get("awaiting_name", False)
            or "what should i call you" in last_bot_turn.lower()
            or "what's your name" in last_bot_turn.lower()
            or "what is your name" in last_bot_turn.lower()
        )

        if not extracted_name and was_asked_name:
            words = clean_text.split()
            if (
                1 <= len(words) <= 3
                and 2 <= len(clean_text) <= 35
                and not any(char.isdigit() for char in clean_text)
                and "?" not in clean_text
                and clean_text.lower() not in ignored_name_words
                and words[0].lower() not in ignored_name_words
                and not any(w.lower().strip(".,!?") in self_descriptor_words for w in words)
                and not any(term in text_lower for term in ["sondesh", "sandesh", "mishti", "menu", "price", "bento", "1kg", "500g", "250g", "box", "delivery", "order", "cancel", "refund"])
            ):
                extracted_name = clean_text.strip(".,!?:;\"'").title()

        if extracted_name:
            facts["name"] = extracted_name
            facts["awaiting_name"] = False
            facts["just_introduced_name"] = True

        # 2. Extract Order Attributes into Slots
        # Flavour
        for fl in ["jolbhora", "mahua", "mango", "kesar", "rose", "vanilla", "cinnamon", "classic", "nolen gur"]:
            if fl in text_lower:
                order_draft["flavour"] = fl
                break

        # Collection
        if "signature" in text_lower:
            order_draft["collection"] = "signature"
        elif "aamar mishti" in text_lower or "aamar_mishti" in text_lower or "aamar" in text_lower:
            order_draft["collection"] = "aamar_mishti"

        # Size
        if any(kw in text_lower for kw in ["1kg", "1 kg", "1000g", "36 pcs", "36pcs"]):
            order_draft["size"] = "1kg"
        elif any(kw in text_lower for kw in ["500g", "500 g", "18 pcs", "18pcs"]):
            order_draft["size"] = "500g"
        elif any(kw in text_lower for kw in ["250g", "250 g", "9 pcs", "9pcs"]):
            order_draft["size"] = "250g"

        # Quantity
        qty_match = re.search(r'\b(\d+)\s*(?:box|boxes|pack|packs|pcs)\b', text_lower)
        if qty_match:
            try:
                order_draft["quantity"] = max(1, int(qty_match.group(1)))
            except ValueError:
                pass

        return facts

    def has_prior_history(self, user_id: str, channel: str) -> bool:
        """
        Determines whether this user has any prior conversation history.
        Checks both in-memory session turns and the persistent SQLite database.
        Returns True if prior history exists, False for brand new / first-time users.
        """
        if not user_id:
            return False
        key = (str(user_id), str(channel))
        if key in self._sessions:
            turns = self._sessions[key].get("turns", [])
            if turns and len(turns) > 0:
                return True
        try:
            from camellia.database import db
            return db.get_conversations_count(user_id=str(user_id), channel=str(channel)) > 0
        except Exception:
            return False

    def add_turn(self, user_id: str, channel: str, user_message: str, assistant_response: str) -> None:
        """Appends user input and bot response to session history."""
        session = self.get_session(user_id, channel)
        turns = session["turns"]

        turns.append({"role": "user", "content": user_message})
        turns.append({"role": "assistant", "content": assistant_response})

        # Keep history bounded to max_history_turns * 2 (turns pair = user + assistant)
        if len(turns) > self.max_history_turns * 2:
            session["turns"] = turns[-(self.max_history_turns * 2):]

    def get_context_summary(self, user_id: str, channel: str) -> str:
        """Generates a text summary of active customer facts for the AI system prompt."""
        session = self.get_session(user_id, channel)
        facts = session["facts"]
        
        summary_lines = []
        if facts["name"]:
            summary_lines.append(f"Customer Name: {facts['name']}")

        if not summary_lines:
            return "No prior customer details recorded."
        return "\n".join(summary_lines)

    def get_handoff_brief(self, user_id: str, channel: str, escalation_reason: str = "Unmatched Query") -> Dict[str, Any]:
        """Generates a structured handoff brief for human staff when escalating."""
        session = self.get_session(user_id, channel)
        facts = session["facts"]
        turns = session["turns"]

        turn_history = []
        for t in turns:
            role = "Customer" if t["role"] == "user" else "Bot"
            turn_history.append(f"{role}: {t['content']}")

        return {
            "customer_name": facts["name"] or "Unknown",
            "recent_chat_history": turn_history,
            "escalation_reason": escalation_reason
        }

# Global Singleton Memory Manager
session_memory = SessionMemoryManager()
