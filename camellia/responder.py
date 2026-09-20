import difflib
import hashlib
import json
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List
from camellia.config import config
from camellia.ad_routing import product_for_ad
from camellia.instagram_routing import route_template_id
from camellia.monitoring import get_logger, latency_monitor
from camellia.ai_responder import ai_responder
from camellia.session_memory import session_memory

from camellia.order_calculator import order_calculator
from camellia.rag_fallback import MEDIUM_CONFIDENCE_THRESHOLD, rag_fallback_engine
from camellia.text_rules import FLOWER_RE, strip_flower_emoji
from camellia.router import shadow_router
from camellia.triage import triage_classifier
from camellia.database import db
from camellia.trigger_index import trigger_index
from camellia import weekly_flyer
from camellia import menu_dates
from camellia import client_qa
from camellia.menu_images import current_url as current_menu_image
from camellia import semantic_fallback
from camellia.understanding import BusinessAction, Intent, understanding_engine
from camellia.knowledge import HUMAN_ONLY_INTENTS, approved_knowledge
from camellia.route_contract import build_trace, classify_lane
from camellia.content_integrity import (
    ALACARTE_SONDESH_ADDON_COPY,
    quarantine_template_response,
    quarantine_unconfirmed_claims,
    runtime_quarantine_enabled,
    is_known_legacy_subscription_response,
    subscription_response_fingerprint,
)

log = get_logger("camellia.responder")


def _safe_message_hash(message: str) -> str:
    return hashlib.sha256(str(message or "").encode("utf-8")).hexdigest()[:12]


def _safe_user_hash(user_id: str) -> str:
    return hashlib.sha256(str(user_id or "").encode("utf-8")).hexdigest()[:12]


def compose_topic_response(
    topic_title: str = "", topic_description: str = "", menu_text: str = "",
    response_order: str = "description_first", menu_mode: str = "SELECTED",
) -> str:
    """Compose the shared Topic response used by preview and live Instagram.

    Existing Topics and malformed values safely retain the historical
    description-first order.  The menu text is the already-approved response;
    this helper only changes the order of those existing customer-facing parts.
    """
    title = str(topic_title or "").strip()
    description = str(topic_description or "").strip()
    menu = str(menu_text or "").strip()
    if str(menu_mode or "").strip().upper() == "NONE":
        return description
    order = str(response_order or "").lower()
    ordered = [menu, description] if order == "menu_first" else [description, menu]
    parts = [title] + [part for part in ordered if part]
    return "\n\n".join(part for part in parts if part)


def _structured_nlu_enabled_for(user_id: str, channel: str) -> bool:
    """Stable rollout cohort: a customer never flips paths between messages."""
    if not config.STRUCTURED_NLU_ENABLED:
        return False
    percent = config.STRUCTURED_NLU_ROLLOUT_PERCENT
    if percent >= 100:
        return True
    if percent <= 0:
        return False
    digest = hashlib.sha256(f"{channel}:{user_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 100 < percent


def _understanding_failed_closed(reason: str, model_result_usable: bool) -> bool:
    """True when understanding did not produce a usable, trusted meaning.

    Every provider failure shape counts, not a hand-listed few: a rate limit,
    an HTTP error, a connection exception, an empty choice list, malformed
    JSON, a schema violation, and a low-confidence answer all mean the same
    thing — the meaning of the message was never established. A second
    LLM router guessing a template from the same message is exactly the
    aggressive fallback this must prevent, so it is switched off for the
    rest of the turn. Structured NLU being switched off by configuration is
    not a failure: that customer is on the legacy path by design.
    """
    return not model_result_usable and reason != "disabled_or_empty"

# --- Repeat-reply guard ----------------------------------------------------
# A one-word answer to the bot's own closing question usually re-matches the
# template that asked it: the bot asks "Which area are you in?", the customer
# says "gurgaon", and "gurgaon" is itself a delivery_area trigger. Refinement
# normally rewrites the second reply into an actual answer, but it needs the
# provider — and on a config error or a 413 the provider is parked for ten
# minutes, during which every follow-up resends the template verbatim, question
# and all. This guard is the deterministic floor under that: it costs nothing,
# needs no network, and holds while the AI layer is down.
#
# It deliberately asserts no facts. Confirming "yes, we deliver to Gurgaon"
# would need to know the area is covered, and a wrong guess about coverage is
# exactly the kind of error the whole template architecture exists to prevent.
# So the repeat is replaced with an acknowledgement that points back at what
# was already said, plus the menu of next steps the bot can actually action.
# Sent instead of the weekly menu once its printed dates have passed and no
# new flyer has been uploaded yet. Deliberately carries no dishes, no prices
# and no photo: last week's prices are the ones a customer would order
# against, and a wrong price is a real cost to the kitchen. Points at the
# Sondesh collections, which are a separate, undated range and still correct.
_WEEKLY_MENU_BEING_UPDATED = (
    "Our kitchen is finalising this week's menu right now — I'd rather not "
    "send you last week's by mistake.\n\n"
    "It'll be ready shortly! In the meantime, say *Sondesh* to see our sweet "
    "collections."
)

_DUPLICATE_REPLY_NOTICE = (
    "Got it — thanks! I've shared those details just above.\n\n"
    "Anything else I can help with — *Sondesh* prices, this week's *Meals* menu, "
    "or *placing an order*?"
)


# Chholar Dal for Aloo Dom in the Vegetarian Bento is the one swap the client
# has confirmed in writing, so it is answerable rather than escalatable. Kept
# deliberately narrow: it needs the dish named AND a bento or Aloo Dom in the
# same message, so "can I swap things around?" still goes to a human.
#
# Flavour mixing is NOT here and must not be added — that was never confirmed,
# and GUARD_customization_flavour_mixing exists to keep it with a human.
_CONFIRMED_BENTO_SUBSTITUTION_RE = re.compile(
    r"\bchholar\s+daa?l\b(?=.*\b(bento|aloo\s+dom)\b)"
    r"|\b(bento|aloo\s+dom)\b(?=.*\bchholar\s+daa?l\b)",
    re.IGNORECASE,
)

_UNSUPPORTED_CUSTOMIZATION_RE = re.compile(
    r"\b(?:can|could|would|do)\s+you\s+(?:make|prepare|do|customi[sz]e|modify)\b.*"
    r"\b(?:without|no|remove|replace|substitute|change)\b|"
    r"\b(?:make|prepare|customi[sz]e|modify)\b.*\b(?:without|no|remove|replace|substitute)\b",
    re.IGNORECASE,
)
_HIGH_RISK_UNSUPPORTED_RE = re.compile(
    r"\b(?:severe|serious|strict)\s+(?:milk|dairy|food)?\s*allerg\w*\b|"
    r"\b(?:milk|dairy)\s+allerg\w*\b|\b(?:medical|life-threatening)\s+(?:allerg\w*|requirement)\b",
    re.IGNORECASE,
)


def _bento_grounded_comparison(question: str, approved_response: str) -> Optional[str]:
    """Answer only comparisons whose numbers are present in approved Bento copy."""
    prices = {
        match.group(1).lower(): int(match.group(2))
        for match in re.finditer(
            r"\b(Vegetarian|Chicken|Mangsho)\s+Bento\s*[—-]\s*₹\s*(\d+)",
            approved_response,
            re.IGNORECASE,
        )
    }
    if len(prices) != 3:
        return None
    text = " ".join(question.lower().split())
    if "cheapest" in text or "lowest price" in text:
        name, price = min(prices.items(), key=lambda item: item[1])
        return f"The cheapest Bento is the {name.title()} Bento at ₹{price}."
    difference = re.search(
        r"how much more is (?:the\s+)?(vegetarian|chicken|mangsho)\s+bento\s+than\s+"
        r"(?:the\s+)?(vegetarian|chicken|mangsho)(?:\s+bento)?",
        text,
    )
    if difference and difference.group(1) != difference.group(2):
        higher, lower = difference.groups()
        return (
            f"The {higher.title()} Bento is ₹{abs(prices[higher] - prices[lower])} more "
            f"than the {lower.title()} Bento."
        )
    return None


# The client-QA registry is the authoritative copy source, while the public
# route/template IDs remain stable for existing admin/audit consumers.
_CLIENT_QA_TEMPLATE_IDS = {
    "home_table_details": "bengali_home_table_details",
    "home_table_price": "bengali_home_table_pricing",
    "home_table_payment": "bengali_home_table_payment",
    "general_price": "savoury_food_intro",
    "general_details": "savoury_food_intro",
    "alacarte_prebook": "alacarte_prebooking_steps",
    "subscription_price": "subscription_details",
    "subscription_details_client": "subscription_details",
    "price_is_high": "pricing_value_context",
    "bento_details_client": "bento_details",
    "bento_price_client": "bento_details",
    "best_time_to_book": "savoury_best_time_to_book",
    "savoury_delivery_time": "savoury_delivery_time",
    "chicken_kosha_price_client": "chicken_kosha_price",
    "sondesh_delivery_time": "sondesh_dispatch_time",
    "sondesh_price_client": "sondesh_price_detailed",
    "sondesh_details_client": "sondesh_details",
    "sondesh_unfamiliar_client": "sondesh_unfamiliar",
    "delivery_out_of_area_client": "delivery_out_of_area",
    "order_prebook": "alacarte_prebooking_steps",
    "general_payment_client": "GUARD_payment_method",
    "minimum_sondesh_quantity_client": "minimum_sondesh_quantity",
}

# Existing deterministic routes carry additional state, media, dynamic-menu,
# handoff, and source-evidence semantics.  They must claim these messages
# before the compact client-QA registry is consulted.
_CANONICAL_ROUTE_HINT_RE = re.compile(
    r"\b(?:this|that|it|above|reel|post|video|item|one|"
    r"bengali\s+home\s+table|home\s+table|home\s+lunch|"
    r"subscription|meal\s+plan|bento|sondesh|sandesh|mishti|mithai|"
    r"aamar|signature|jolbhora|kanchagolla|mangsho|chicken\s+kosha|"
    r"price|cost|rate|how\s+much|payment|pay|delivery|deliver|"
    r"pre\s*[- ]?book|pre\s*[- ]?order|book|order|confirm|finali[sz]e|"
    r"reserve|best\s+time)\b",
    re.IGNORECASE,
)


# The flower strip lives in text_rules so database.py can use it too — the
# reply path is not the only place text reaches the client's people.
_FLOWER_RE = FLOWER_RE
_strip_flower_emoji = strip_flower_emoji


# This is a deliberately narrow cross-sell. It is offered only after a
# customer asks about individual meals/menu ordering, never as a generic
# footer on food, delivery, Bento, or subscription answers.
ALACARTE_SONDESH_ADDON_TEMPLATES = frozenset({
    "alacarte_prebooking_steps", "know_more", "weekly_menu_veg",
    "weekly_menu_nonveg", "savoury_food_intro", "how_to_order",
})
_ALACARTE_ADDON_MARKER_RE = re.compile(
    r"\b(?:à\s*la\s*carte|a\s*la\s*carte|alacarte|individual\s+(?:dish|dishes|item|items)|"
    r"from\s+the\s+menu|menu|food|meal|lunch|dinner|savoury|savory)\b",
    re.IGNORECASE,
)
_ALACARTE_ADDON_EXCLUSION_RE = re.compile(
    r"\b(?:bengali\s+home\s+table|home\s+table|subscription|sondesh|sandesh|mishti|"
    r"payment|pay|delivery|deliver|courier|human|person|manager|support|story)\b",
    re.IGNORECASE,
)
_ALACARTE_DIRECT_QUERY_RE = re.compile(
    r"\b(?:à\s*la\s*carte|a\s*la\s*carte|alacarte)\b|"
    r"\bindividual\s+(?:dish|dishes|item|items)\b|"
    r"\bwhat\s+(?:food|meals?)\s+do\s+you\s+have\b",
    re.IGNORECASE,
)
_ALACARTE_ADDON_DECLINE_RE = re.compile(
    r"^(?:no(?:\s+thanks?)?|nope|not\s+interested|not\s+now|skip(?:\s+it)?|"
    r"(?:i|we)\s+(?:don['’]t|do\s+not)\s+want(?:\s+(?:it|that))?|no\s+sweets?)$",
    re.IGNORECASE,
)


def _quarantine_image_claims(images: Any) -> Any:
    """Apply content-integrity quarantine to card captions as well as text."""
    if isinstance(images, list):
        return [_quarantine_image_claims(item) for item in images]
    if isinstance(images, dict):
        result = dict(images)
        if result.get("caption"):
            result["caption"] = quarantine_unconfirmed_claims(str(result["caption"]))
            if not result["caption"]:
                result.pop("caption", None)
        return result
    return images


def _normalise_reply(text: str) -> str:
    """Whitespace- and case-insensitive form used to spot an exact repeat."""
    return " ".join((text or "").split()).lower()


# The guard has to stay narrow. A customer working through several areas —
# "deliver to Dwarka?", "Indirapuram?", "CR Park?" — asks a genuinely new
# question each time and deserves the coverage answer each time, even though
# the template behind it never changes. Answering those with "I've shared that
# above" is a worse bug than the one being fixed. So the guard fires only on
# the shape that actually caused it: the bot asked something, and the customer
# sent a short reply that answers rather than asks.
_QUESTION_OPENER_RE = re.compile(
    r'^(do|does|did|is|are|was|were|can|could|will|would|should|shall|have|has|'
    r'what|where|when|why|how|which|who|any)\b',
    re.IGNORECASE,
)
_SHORT_ANSWER_MAX_WORDS = 3

# Confirming delivery is a business promise, so this is intentionally an
# allowlist. Unknown localities fail closed to the approved out-of-area reply;
# they are never guessed from an LLM or a broad geographic heuristic.
NCR_SERVICEABLE_AREAS = (
    "delhi", "new delhi", "gurgaon", "gurugram", "noida", "greater noida",
    "ghaziabad", "faridabad", "dwarka", "indirapuram", "vaishali",
    "kaushambi", "raj nagar extension", "sector 15 faridabad", "neelam bagh",
    "ballabhgarh", "cr park",
    "chittaranjan park", "lajpat nagar", "chatra marg", "chhatra marg",
    "north campus", "delhi university", "du campus", "karol bagh",
    "saket", "vasant kunj", "hauz khas", "greater kailash", "gk",
    "kalkaji", "malviya nagar", "rohini", "pitampura", "mayur vihar",
    "janakpuri", "rajouri garden", "paschim vihar", "nehru place",
    "connaught place", "chanakyapuri", "civil lines", "model town",
    "ashok vihar", "burari", "shalimar bagh", "vikaspuri", "cybercity",
    "cyber hub", "sector 45", "sector 62",
)


def _bot_asked_a_question(reply: str) -> bool:
    """True when the bot's own last reply ended by asking something."""
    return (reply or "").rstrip().endswith("?")


def _looks_like_a_short_answer(message: str) -> bool:
    """True for a brief reply that answers rather than asks — 'gurgaon'."""
    text = " ".join((message or "").split())
    if not text or text.endswith("?") or _QUESTION_OPENER_RE.match(text):
        return False
    return len(text.split()) <= _SHORT_ANSWER_MAX_WORDS


def _looks_like_an_area_answer(message: str) -> bool:
    """True for a short location reply, not a new product or delivery question."""
    text = " ".join((message or "").split())
    lowered = text.lower()
    if not text or text.endswith("?") or _QUESTION_OPENER_RE.match(text):
        return False
    if len(text.split()) > 12:
        return False
    return not any(word in lowered for word in (
        "price", "menu", "sondesh", "mishti", "order", "refund", "complaint",
        "delivery", "deliver", "courier", "shipping", "hours", "time",
    ))


def _is_serviceable_ncr_area(message: str) -> bool:
    """True only when a customer-provided location is on the NCR allowlist."""
    lowered = " ".join((message or "").lower().split())
    return any(area in lowered for area in NCR_SERVICEABLE_AREAS)


# Comparing against only the previous reply is not enough: it lets the bot
# oscillate template -> notice -> template as a customer repeats themselves,
# because by the third turn the template is no longer the *last* thing said.
# Three replies covers that without muting a template the customer legitimately
# asks for again much later in the conversation.
_DUPLICATE_LOOKBACK_REPLIES = 3


def _recent_bot_replies(user_id: str, channel: str, limit: int = _DUPLICATE_LOOKBACK_REPLIES) -> List[str]:
    """The bot's most recent replies, newest first."""
    turns = session_memory.get_session(user_id, channel).get("turns", [])
    replies = [t.get("content", "") for t in reversed(turns) if t.get("role") == "assistant"]
    return replies[:limit]

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

ORDER_READY_RE = re.compile(
    r"^\s*(?:i\s+want\s+to\s+order|want\s+to\s+order|"
    r"i'?m\s+ready\s+to\s+place\s+the\s+order|ready\s+to\s+place\s+the\s+order|"
    r"let'?s\s+confirm|finali[sz]e\s+it|book\s+this|can\s+we\s+lock\s+this\s+in|"
    r"go\s+ahead\s+and\s+book\s+it|order\s+now|i\s+wnt\s+2\s+ordr\s+pls|i\s+want\s+two\s+boxes|"
    r"yes\s+place\s+my\s+order|final\s+selection|confirm\s+my\s+order|"
    r"place\s+the\s+order)\s*[?.!]*$",
    re.IGNORECASE,
)

# A longer purchase request can contain an active Story/Post route and still
# be unambiguous.  Keep this narrower than ``ORDER_INTENT_RE`` so informational
# questions such as "what's the best time to book?" remain normal Q&A, while a
# request for help booking/ordering reaches the manual purchase handoff first.
PURCHASE_ASSISTANCE_RE = re.compile(
    r"(?:\b(?:need|want|looking\s+for)\s+(?:some\s+)?(?:help|assistance)\b.*"
    r"\b(?:book|booking|order|ordering|purchase|buy)\b|"
    r"\bhelp(?:\s+me)?\s+(?:to\s+)?(?:book|booking|order|ordering|purchase|buy)\b|"
    r"\b(?:book|booking|order|ordering|purchase|buy)\b.*"
    r"\b(?:need|want)\s+(?:some\s+)?(?:help|assistance)\b)",
    re.IGNORECASE,
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

# A price question that names nothing else at all. Answering it means asking
# which product, so it states no business fact and is safe to route the same
# way whether or not the language model is reachable. A message that names a
# flavour, a pack size or a time ("real vanilla price", "how much time for
# meals") is deliberately not a match: those have their own answers.
BARE_PRICE_QUESTION_RE = re.compile(
    r"(?:price|prices|cost|rate|how much|kitna|kitne|kitne ka|kitna padega|"
    r"kitne paise|daam|iska price|what does it cost|what is the price|"
    r"what.?s the price)",
    re.IGNORECASE,
)

# These turns are conversational openers, not references to a previously
# shared Instagram object.  Keep this deliberately narrow: a greeting may
# bypass source-context resolution, but a greeting carrying a real request
# (for example, "hi, what is the price of Jolbhora?") must continue through
# the normal intent/product precedence below.
GREETING_ONLY_RE = re.compile(
    r'^(?:(?:hi+|hello+|hey+)(?:\s+there)?|good\s+(?:morning|afternoon|evening)|namaste|'
    r'nomoshkar|nomoskar|greetings)[\W_]*$',
    re.IGNORECASE,
)

# A greeting may be a prefix to a real request ("hi tell me more"), not the
# whole intent.  Keep the opener grammar deterministic and deliberately
# conservative so only a leading conversational opener is removed.  The
# remainder is then resolved by the normal product/order/source precedence.
CONVERSATIONAL_OPENER_RE = re.compile(
    r"^(?P<opener>hi+|hello+|hey+|good\s+(?:morning|afternoon|evening)|"
    r"namaste|nomoshkar|nomoskar|greetings)(?:\s+there)?"
    r"(?:[\s,!?;:.-]+(?P<remainder>.*))?$",
    re.IGNORECASE,
)


def split_conversational_opener(text: str) -> Tuple[Optional[str], str]:
    """Return ``(canonical opener, semantic remainder)`` for a leading opener.

    The opener is not treated as business intent.  For opener-only messages
    the remainder is empty, preserving the existing greeting behavior.  The
    function intentionally does not fuzzy-match arbitrary words so a product
    or topic name at the start of a message cannot be stripped accidentally.
    """
    normalized = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not normalized:
        return None, ""
    match = CONVERSATIONAL_OPENER_RE.fullmatch(normalized)
    if not match:
        return None, normalized
    opener = re.sub(r"\s+", " ", match.group("opener").lower()).strip()
    remainder = (match.group("remainder") or "").strip()
    if opener.startswith("hi"):
        opener = "hi"
    elif opener.startswith("hello"):
        opener = "hello"
    elif opener.startswith("hey"):
        opener = "hey"
    return opener, remainder


# Generic detail wording must not outrank a named subject.  This guard is
# intentionally limited to the wrapper phrases that previously reached the
# semantic router as ``know_more`` before the deterministic product/topic
# matcher had a chance to see the entity.
GENERIC_DETAIL_WRAPPER_RE = re.compile(
    r"\b(?:tell\s+me\s+more|more\s+details?|i\s+(?:want|would\s+like)\s+to\s+know\s+more|"
    r"can\s+you\s+explain(?:\s+more)?)\b",
    re.IGNORECASE,
)

EXPLICIT_SUBJECT_RE = re.compile(
    r"\b(?:sondesh|mishti|mithai|sweet(?:s)?|jolbhora|kanchagolla|nolen\s+gur|mahua|"
    r"aamar|signature|bento|meal(?:s)?|savoury|savory|curry|veg|non[ -]?veg|"
    r"weekly\s+(?:veg(?:etarian)?|non[ -]?veg(?:etarian)?)?\s*menu(?:s)?|"
    r"(?:bengali|kolkata)\s+home\s+table|home\s+table|the\s+event)\b",
    re.IGNORECASE,
)

# A shared Instagram attachment gives the next message a subject, but only
# when the customer's wording is actually referential.  Keep this as a small
# deterministic intent layer rather than a growing list of whole sentences:
# punctuation, casing, apostrophes and one-character typos are normalized,
# then the terms are combined into an intent.
_SOURCE_REFERENTIAL_TERMS = frozenset({
    "this", "that", "it", "above", "reel", "post", "video", "item", "one",
})
_SOURCE_DETAIL_TERMS = frozenset({
    "tell", "more", "details", "detail", "about", "explain", "what", "options",
    "flavour", "flavours", "flavor", "flavors", "interested",
})
_SOURCE_PRICE_TERMS = frozenset({"price", "prices", "cost", "rate", "much"})
_SOURCE_AVAILABILITY_TERMS = frozenset({"available", "availability"})
_SOURCE_ACTION_TERMS = frozenset({"want", "need", "like", "get", "order", "buy", "looks", "good"})
_SOURCE_CONTEXT_FILLER_TERMS = frozenset({
    "i", "im", "am", "me", "the", "a", "is", "are", "do", "you", "can", "how",
    "what", "there", "please", "pls", "send", "give", "more", "about", "tell",
})
_SOURCE_COMMON_TYPOS = {
    "thsi": "this", "taht": "that", "tihs": "this", "oen": "one",
    "ths": "this", "abov": "above", "mor": "more", "moore": "more",
    "mroe": "more", "tlel": "tell", "detials": "details",
    "availble": "available", "avialble": "available",
}

_SOURCE_EXPLICIT_SUBJECT_TERMS = frozenset({
    "menu", "menus", "week", "weekly", "jolbhora", "kanchagolla", "sondesh",
    "mahua", "bento", "meal", "meals", "savoury", "savory",
})


def _one_edit_or_exact(left: str, right: str) -> bool:
    """Allow a single insertion/deletion/substitution/transposition typo."""
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1 or min(len(left), len(right)) < 4:
        return False
    if len(left) == len(right):
        mismatches = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        if len(mismatches) == 1:
            return True
        if len(mismatches) == 2:
            first, second = mismatches
            return second == first + 1 and left[first] == right[second] and left[second] == right[first]
        return False
    # One insertion/deletion: walk once and permit one skipped character.
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    i = j = 0
    skipped = False
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
            continue
        if skipped:
            return False
        skipped = True
        j += 1
    return True


def _source_term_set(tokens: List[str], vocabulary: frozenset[str]) -> set[str]:
    return {
        candidate for candidate in vocabulary
        if any(
            _SOURCE_COMMON_TYPOS.get(token, token) == candidate
            or (len(candidate) >= 5 and _one_edit_or_exact(token, candidate))
            for token in tokens
        )
    }


def source_referential_intent(text: str) -> Optional[str]:
    """Classify a source-context follow-up without inventing a subject.

    Returns ``price``, ``availability``, ``order``, ``details`` or ``None``.
    Detail/price/availability shorthand is intentionally accepted without an
    explicit pronoun because it is a normal continuation immediately after a
    shared attachment.  Action words such as ``want`` or ``buy`` require a
    referential term (``this``, ``the reel``, ``above``, etc.) so an unrelated
    first message cannot borrow a nonexistent source.
    """
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    normalized = normalized.replace("’", "'").replace("‘", "'")
    normalized = re.sub(r"\bi\s*['’]?\s*m\b", "im", normalized)
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    tokens = normalized.split()
    if not tokens:
        return None

    referential = _source_term_set(tokens, _SOURCE_REFERENTIAL_TERMS)
    details = _source_term_set(tokens, _SOURCE_DETAIL_TERMS)
    prices = _source_term_set(tokens, _SOURCE_PRICE_TERMS)
    availability = _source_term_set(tokens, _SOURCE_AVAILABILITY_TERMS)
    actions = _source_term_set(tokens, _SOURCE_ACTION_TERMS)
    has_reference = bool(referential)
    canonical_tokens = [_SOURCE_COMMON_TYPOS.get(token, token) for token in tokens]
    # "this one" / "that one" / "above one" are covered by the individual
    # terms, as are natural shorthand forms such as "about the reel".
    if prices and (has_reference or prices & {"price", "prices", "cost", "rate", "much"}):
        return "price"
    if availability and (has_reference or availability):
        return "availability"
    # A real subject in the same utterance wins over the generic ``this``
    # pronoun.  For example, "hey show me this week's menu" is a fresh menu
    # request, not an order for the previously shared reel.
    if actions and has_reference and not (
        set(canonical_tokens) & _SOURCE_EXPLICIT_SUBJECT_TERMS
    ):
        return "order"
    context_only_detail = all(
        token in _SOURCE_CONTEXT_FILLER_TERMS or token in _SOURCE_DETAIL_TERMS
        for token in canonical_tokens
    )
    if details and (has_reference or context_only_detail):
        return "details"
    if has_reference and not (set(canonical_tokens) & _SOURCE_EXPLICIT_SUBJECT_TERMS):
        # "This one please" / "the above one" carry a clear selection even
        # when the customer omits an explicit verb.
        return "order"
    return None

# Words that carry no subject, so they cannot anchor an answer to a question.
RELEVANCE_STOP_WORDS = frozenset({
    "the", "and", "but", "for", "you", "your", "yours", "our", "ours", "are",
    "can", "could", "would", "will", "shall", "should", "may", "any", "all",
    "some", "have", "has", "had", "does", "did", "want", "need", "like",
    "please", "pls", "tell", "give", "get", "got", "know", "let", "say",
    "there", "here", "this", "that", "these", "those", "what", "whats",
    "when", "where", "which", "who", "why", "how", "much", "many", "with",
    "from", "into", "about", "also", "just", "then", "than", "hai", "hain",
    "kya", "aap", "mujhe", "karo", "kar", "abhi", "yes", "not", "its",
})

# Topics with no approved answer anywhere in the business knowledge. They are
# listed because near-miss matching is otherwise convincing: "parking" is one
# edit from the "packing" trigger on the packaging answer, so a customer
# asking where to park was told about cornstarch boxes.
OUT_OF_SCOPE_TOPIC_RE = re.compile(
    rf"\b(?:{semantic_fallback.AMENITY_TOPIC_PATTERN})\b", re.IGNORECASE,
)

# Delivery-time questions, not delivery-*area* ones — "how long", never
# "do you deliver to X". Kept separate from BARE_PRICE_RE's exact-match
# shape because a time question is asked in far more varied wording
# ("when will my lunch arrive", "how fast is delivery") and still needs
# disambiguating against product context, not treated as a single fixed phrase.
TIME_INTENT_RE = re.compile(
    r'\b(how (long|much time|soon|fast)|when will|what time|eta|dispatch time|'
    r'delivery time|delivery hours|how fast)\b',
    re.IGNORECASE
)

# These two intent shapes are deliberately handled before the typed/semantic
# router. They are complete questions, but carry no product noun for the
# router to use: ``when should I book?`` is booking advice, not the generic
# order-steps template, and ``how long will delivery take?`` is the approved
# savoury delivery answer, not the broader Sondesh delivery template.
SAVOURY_BOOKING_TIME_RE = re.compile(
    r'\b(?:best time to book|when should i book|when to book|best time to order|'
    r'when should i order|how early (?:should i )?(?:book|order)|'
    r'how far in advance(?: should i (?:book|order))?|when to place order|'
    r'best time to place order|advance booking)\b',
    re.IGNORECASE,
)
GENERIC_SAVOURY_DELIVERY_TIME_RE = re.compile(
    r'\b(?:how long(?: will| does)? (?:the )?delivery take|'
    r'what(?: is|\'s)? (?:the )?delivery time|'
    r'how soon can you deliver)\b',
    re.IGNORECASE,
)
OPENING_HOURS_RE = re.compile(
    r'\b(?:opening hours?|opening times?|when are you open|when do you open|'
    r'what time (?:do you|does the kitchen) open|what time are you open|'
    r'kitchen hours?|kitchen opening hours?|kitchen timings?)\b',
    re.IGNORECASE,
)
SUBSCRIPTION_DELIVERY_TIME_RE = re.compile(
    r'(?:\bsubscription\b.*\b(?:delivery|deliver|arrive|time)\b|'
    r'\b(?:delivery|deliver|arrive|time)\b.*\bsubscription\b)',
    re.IGNORECASE,
)

MANAGER_CONTACT_QUERY_RE = re.compile(
    r"(?:\bwho\s+can\s+i\s+speak\s+to\s+directly\b|"
    r"\bhow\s+can\s+i\s+contact\s+(?:your|the)\s+manager\b|"
    r"\bwho\s+(?:do|should)\s+i\s+contact\b|"
    r"\bmanager(?:'s)?\s+(?:contact|email|details?)\b)",
    re.IGNORECASE,
)

# A food-context word anywhere in a time question means the client's 45-minute
# meals figure applies, not the 15/30/60-minute Sondesh dispatch numbers.
FOOD_CONTEXT_WORDS = (
    "food", "meal", "meals", "lunch", "dinner", "bento", "savoury", "savory",
    "curry", "curries", "dish", "dishes", "à la carte", "a la carte", "alacarte",
    "subscription",
)

# A Sondesh-context word means the 15/30/60-minute numbers do apply.
SONDESH_CONTEXT_WORDS = (
    "sondesh", "mishti", "kanchagolla", "jolbhora", "sweet", "sweets", "mithai",
)

# "Kolkata" alone, or a wording that names the business specifically
# ("relocating", "shutting down"), is an unambiguous ask for the
# reassurance answer. Everything else needs a relocation-shaped verb next
# to a word naming what the customer is actually worried will change —
# without that pairing, "who is the founder" or "is the kitchen open"
# would wrongly pull in the Kolkata answer instead of their own template.
KOLKATA_UNAMBIGUOUS_RE = re.compile(r'\b(kolkata|relocat\w*|shutting down|shut down)\b', re.IGNORECASE)
HOME_TABLE_RE = re.compile(
    r'\b(?:bengal(?:i)?\s+home\s+(?:lunch\s+)?tables?|'
    r'bengal(?:i)?\s+home\s+lunch|bengal(?:i)?\s+home\s+tabel(?:\s+kolkatta?)?|'
    r'kolkata\s+home\s+tables?|'
    r'home\s+table|home\s+tabel\s+pris(?:e|s)\s+kolkatta)\b',
    re.IGNORECASE,
)
HOME_TABLE_PAYMENT_RE = re.compile(
    r'\b(?:how\s+to\s+pay|how\s+do\s+i\s+pay|payment|pay)\b', re.IGNORECASE,
)
HOME_TABLE_DETAILED_PRICING_RE = re.compile(
    r'\b(?:detailed\s+pricing|price\s+per\s+person|pricing|prices|price|pris[es]|cost|rate|'
    r'how\s+much|per\s+person|per\s+head|'
    r'veg(?:etarian)?|non[ -]?veg(?:etarian)?)\b', re.IGNORECASE,
)
HOME_TABLE_BARE_PRICE_RE = re.compile(
    r'^\s*(?:what(?:\s+is|\'s)?\s+the\s+price|price|prices|cost|rate|how\s+much)\s*[?.!]*\s*$',
    re.IGNORECASE,
)
HOME_TABLE_BOOKING_RE = re.compile(
    r"\b(?:book|booking|reserve|reservation)\b",
    re.IGNORECASE,
)
PAYMENT_QUERY_RE = re.compile(
    r'\b(?:how\s+to\s+pay|how\s+do\s+i\s+pay|how\s+can\s+i\s+pay|'
    r'payment(?:\s+details?|\s+method(?:s)?|\s+options?)?|pay(?:ment)?\s+details?|'
    r'bank\s+transfer|upi|qr(?:\s+code)?|gpay|paytm|phonepe|net\s+banking|'
    r'credit\s+card|debit\s+card|cash\s+on\s+delivery|\bcod\b)\b',
    re.IGNORECASE,
)
KOLKATA_MOVE_VERB_RE = re.compile(r'\b(mov(e|ing)|leav(e|ing)|leaves|clos(e|ing))\b', re.IGNORECASE)
KOLKATA_SUBJECT_RE = re.compile(
    r'\b(kitchen|gurgaon|founder|founders|recipes|recipe|quality|standards|team|somdeb)\b',
    re.IGNORECASE,
)

# Asking about shipping beyond Delhi NCR without naming a specific city —
# "pan india", "internationally", "abroad", "another state" — has no city
# keyword for delivery_out_of_area's own triggers to catch, so it fell to
# delivery_area's generic "which area are you in?" instead of the approved
# refusal. Deliberately excludes anything already handled as an NCR-zone
# question ("outside Delhi NCR" itself resolves to delivery_area).
OUT_OF_AREA_GENERIC_RE = re.compile(
    r'\b(pan[\s-]?india|all over india|all of india|across india|'
    r'internationally|international shipping|abroad|overseas|'
    r'outside india|another state|other states|worldwide|world[\s-]?wide|'
    r'outside the country|ship (it |this )?internationally)\b',
    re.IGNORECASE,
)
NON_NCR_DESTINATION_RE = re.compile(
    r"\b(?:bangalore|bengaluru|mumbai|pune|kolkata|hyderabad|chennai|"
    r"jaipur|ahmedabad|chandigarh|lucknow)\b",
    re.IGNORECASE,
)
DELIVERY_ACTION_RE = re.compile(
    r"\b(?:deliver(?:y|ing)?|ship(?:ping)?|courier|send|order)\b",
    re.IGNORECASE,
)

# These are narrow, approved-area/contact questions. They run before the
# general NLU path because a model may otherwise classify them as unsupported
# stock availability or generic contact requests instead of using the exact
# approved QA template.
SUBSCRIPTION_AREA_QUERY_RE = re.compile(
    r"(?=.*\b(?:subscription|subscriptions|meal plan|meal package|everyday meal|"
    r"meal subscription|tiffin)\b)"
    r"(?=.*\b(?:available|availability|deliver|delivery|offered|get|take)\b)"
    r"(?=.*\b(?:delhi|new delhi|gurgaon|noida|greater noida|ghaziabad|faridabad|"
    r"dwarka|ncr|mumbai|pune|bangalore|bengaluru|kolkata|hyderabad|chennai|"
    r"jaipur|ahmedabad|chandigarh|lucknow)\b)",
    re.IGNORECASE,
)
SUBSCRIPTION_CANONICAL_QUERY_RE = re.compile(
    r"(?=.*\b(?:subscr(?:iption|ption)|meal\s+plan|meal\s+package|everyday\s+meal|"
    r"meal\s+subscription|daily\s+meals|monthly\s+meal)\b)"
    r"(?=.*\b(?:price|pricing|pric|cost|rate|more|about|details?|options?|"
    r"what\s+subscriptions?)\b)",
    re.IGNORECASE,
)
PICKUP_CONTACT_QUERY_RE = re.compile(
    r"\b(?:where is (?:your|the) kitchen|kitchen address|pickup directions|"
    r"directions for pickup|number .*\bpickup\b|call .*\bpickup\b)\b",
    re.IGNORECASE,
)
MEAL_PREORDER_QUERY_RE = re.compile(
    r"(?:"
    r"(?=.*\b(?:pre[- ]?order|pre[- ]?book|prebook)\b)"
    r"(?=.*\b(?:meal|food|savoury|savory|lunch|dinner|menu|a la carte|à la carte|alacarte)\b)"
    r"|"
    r"(?=.*\bbook(?:ing)?\b)"
    r"(?=.*\b(?:meal|lunch|dinner|menu|a la carte|à la carte|alacarte)\b)"
    r"|"
    r"(?=.*\bhow\s+(?:do\s+i|can\s+i|to)\s+order\b)"
    r"(?=.*\bmenu\b)"
    r")",
    re.IGNORECASE,
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

# The subscription message ends with "Would you like to book?", so the reply
# to it is usually a bare "yes", "sign me up" or "I'll take option 2" — none
# of which carry a subscription word for trigger scoring to work with.
SUBSCRIPTION_BOOKING_INTENT_RE = re.compile(
    r'^(yes|yep|yeah|ya|sure|ok|okay|yes please|go ahead|please do)[\s\?\.\!]*$'
    r'|\b(book|booking|subscribe|sign me up|count me in|i.?ll take|i will take|'
    r'i want (option|the veg|the chicken|this|that)|lets do|let.s do|'
    # "how do i pay" is deliberately absent: that question has its own
    # approved answer (UPI / bank transfer), and step 2 here only says "Pay".
    r'how do i (start|proceed)|what.s next|whats next|next step)\b',
    re.IGNORECASE
)

# "Option 2" also names Collection 2 of the Sondesh menu, which is what the
# matcher answered with while a customer was choosing a subscription plan.
SUBSCRIPTION_OPTION_RE = re.compile(
    r'\boption\s*(1|2|3|one|two|three)\b',
    re.IGNORECASE
)

# The 5-hour notice line lives at the end of the booking steps, so a
# date-change question is answered with the client's own wording.
SUBSCRIPTION_DATE_CHANGE_RE = re.compile(
    r'\b(change|changing|reschedul\w*|postpone|prepone|shift|move|swap|skip)\b'
    r'[^.?!]*\b(date|dates|day|days|delivery|deliveries|slot|schedule|booking)\b'
    r'|\b(date|dates|delivery|slot|schedule)\b[^.?!]*\b(change|changed|reschedul\w*|'
    r'postpone|prepone|shift|move)\b',
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
    "sondesh_details": "sondesh_general",
    "sondesh_unfamiliar": "sondesh_general",
    "product_overview": "sondesh_general",
    "know_more": "sondesh_general",
    "sugar_free_options": "sondesh_general",
    "combo_bento": "savoury",
    "bento_details": "bento",
    "weekly_menu_veg": "savoury",
    "weekly_menu_nonveg": "savoury",
    "savoury_food_intro": "savoury",
    "subscription_details": "subscription",
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
    conversational session memory, and a Groq-backed RAG fallback.
    """

    def __init__(self, qa_path: str = None, menu_path: str = None):
        self.qa_path = qa_path or config.QA_TEMPLATES_PATH
        self.menu_path = menu_path or config.MENU_JSON_PATH
        self.qa_data = self._load_json(self.qa_path)
        # Keep the trust source separate from the writable/admin-managed QA
        # file.  Durable content may be preserved for operators, but it can
        # never make itself an approved canonical response.
        canonical_path = Path(__file__).resolve().parent.parent / "data" / "qa_templates.json"
        self._canonical_qa_data = self._load_json(str(canonical_path))
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
        ai_responder.reload_data()

    def reload_templates(self):
        """Alias for reload_data."""
        self.reload_data()

    def _has_response_bank(self, template_id: str) -> bool:
        """
        True for templates that rotate between several approved wordings.

        Those already vary themselves, so the repeat guard must leave them
        alone rather than swap an approved greeting for its notice.
        """
        for tmpl in self.qa_data.get("templates", []):
            if tmpl.get("id") == template_id:
                return bool(tmpl.get("responses"))
        return False

    def check_explicit_escalation_triggers(self, text: str) -> Optional[str]:
        """
        Checks if text contains explicit escalation keywords.

        Runs before template matching, so a keyword here outranks every answer
        the bot has. "instead of" is on that list because a substitution the
        client has not ruled on must go to a human rather than be guessed at.
        """
        lowered = text.lower()

        # One substitution is not a guess: the client's confirmed bento copy
        # answers it outright — "Also available with Chholar Dal (Pooja style)
        # instead of Aloo Dom." Without this exemption the single substitution
        # we are allowed to answer is handed to a human alongside all the ones
        # we are not, purely because it contains the words "instead of".
        if _CONFIRMED_BENTO_SUBSTITUTION_RE.search(lowered):
            return None

        triggers = self.qa_data.get("escalation_triggers", {}).get("keywords", [])
        for kw in triggers:
            if re.search(r'\b' + re.escape(kw.lower()) + r'\b', lowered):
                return f"Trigger keyword matched: '{kw}'"
        return None

    def _generic_fallback(self) -> str:
        """Safe first message; the Instagram caller appends the CAM link separately."""
        return (
            "Our team can assist you personally on WhatsApp. "
            "Please continue your enquiry using the link below."
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

    def _conversation_request(self, message, user_id, channel, structured_owns_price=False):
        """Small conversation repairs, before broad keyword matching.

        Compose only the supported menu + delivery-coverage pair. Never split
        arbitrary conjunctions: a flavour name or an order can contain 'and'.

        Complaints, cancellations, "speak to a human", staff-verified answers
        and bare social turns are handled here for every message, with or
        without an active slot: a customer cancelling an order must never be
        answered with ordering instructions because no flow happened to be
        open. `structured_owns_price` hands the bare price clarification to
        the intent/entity path, which records the same route with its
        knowledge id and sufficiency instead of answering blind.
        """
        text = " ".join(message.lower().split()).strip(" .?!")
        # Learned knowledge is deliberately not consulted here. Canonical
        # customer intent must be resolved before a broad ACTIVE Custom Q&A
        # row gets a chance to answer it; the learned fallback runs only after
        # canonical guards/templates have declined the message.
        if (self.classify_unmatched_intent(message) == "complaint"
                or re.search(r"\b(cancel (?:my |the )?order|order is missing|(?:speak|talk) to (?:a |an )?(?:human|person))\b", text)):
            reply, _ = self.build_handover_response(message)
            facts = session_memory.get_facts(user_id, channel)
            facts["last_intents"] = [Intent.ESCALATION.value]
            facts["last_entities"] = {}
            facts["last_actions"] = [BusinessAction.ESCALATE_TO_TEAM.value, BusinessAction.SEND_RESPONSE.value]
            facts["knowledge_sufficient"] = False
            session_memory.add_turn(user_id, channel, message, reply)
            return reply, False, "explicit_escalation", None
        if (re.search(r"https?://|www\.", text)
                or self.check_explicit_escalation_triggers(message)
                or self.classify_unmatched_intent(message) != "unknown"
                or re.search(r"\b(allerg\w*|payment|refund|cancel|custom|without|instead|not|nahi)\b", text)):
            return None
        facts = session_memory.get_facts(user_id, channel)

        def finish(reply, template_id, images=None):
            session_memory.add_turn(user_id, channel, message, reply)
            return reply, True, template_id, images

        # These client-approved questions have distinctive intent that broad
        # compatibility triggers can otherwise misread (for example, a
        # chicken price as the generic Sondesh price, or a dispatch question
        # as a delivery-area query). Reuse the canonical records; do not add a
        # second copy of their answer here.
        special_template_ids = []
        if re.search(r"\b(?:price is high|prices are high|too expensive|quite expensive|why is it so expensive)\b", text):
            special_template_ids.append("pricing_value_context")
        if re.search(r"\b(?:chicken kosha price|price of chicken kosha|what is the price of chicken kosha|how much is chicken kosha)\b", text):
            special_template_ids.append("chicken_kosha_price")
        if re.search(r"\b(?:how much time do you take to deliver sondesh|how long do you take to deliver sondesh)\b", text):
            special_template_ids.append("sondesh_dispatch_time")
        if re.search(r"\b(?:tell me more about sondesh|tell me about sondesh|what is sondesh|sondesh details)\b", text):
            special_template_ids.append("sondesh_details")
        if re.search(r"\b(?:sondesh unfamiliar|sondesh is unfamiliar|new to sondesh|never tried sondesh|what is this sondesh)\b", text):
            special_template_ids.append("sondesh_unfamiliar")
        if re.search(r"\b(?:why is the minimum order 9 pieces|minimum order 9 pieces|minimum order quantity 9|why minimum 250g sondesh|multiples of 9 pieces)\b", text):
            special_template_ids.append("minimum_sondesh_quantity")
        if text in {"share weekly menu", "show weekly menu", "current weekly menu"}:
            special_template_ids.append("savoury_food_intro")
        if special_template_ids:
            template_id = special_template_ids[0]
            template = self._template_by_id(template_id) or {}
            response = template.get("response") or self._generic_fallback()
            response, images = self._format_dynamic_response(
                template_id, response, user_id=user_id, channel=channel, user_message=message,
            )
            response, images = self._apply_crosssell_nudges(
                template_id, response, images, session_memory.get_facts(user_id, channel),
            )
            return finish(response, template_id, images)

        if ACK_ONLY_RE.fullmatch(text):
            return finish("Got it!", "acknowledgement")
        if CLOSING_ONLY_RE.fullmatch(text):
            return finish("You're welcome! Message us whenever you need anything.", "conversation_closing")

        price_question = BARE_PRICE_QUESTION_RE.fullmatch(text)
        if price_question and not facts.get("last_topic") and not structured_owns_price:
            # The client Q&A flow uses a bare Instagram "what's the price?"
            # after the conversation has already entered through a Story/Post
            # route as a request for the approved food overview and current
            # menu.  Keep the no-context path cautious, but do not force a
            # customer who arrived through an established route into a
            # product-choice clarification after that route has been
            # superseded by an explicit subject.
            if channel == "instagram" and isinstance(facts.get("source_route"), dict):
                template_id = "savoury_food_intro"
                template = self._template_by_id(template_id) or {}
                response, images = self._format_dynamic_response(
                    template_id, template.get("response") or self._generic_fallback(),
                    user_id=user_id, channel=channel, user_message=message,
                )
                facts["last_topic"] = "savoury"
                facts["source_context_used"] = False
                return finish(response, template_id, images)
            facts["awaiting_area_confirmation"] = False
            facts["awaiting_product_price"] = True
            return finish("Would you like prices for Sondesh or Bengali Meals?", "price_clarify_product")
        if facts.get("awaiting_product_price"):
            if text in {"meals", "meal", "food", "khana", "bengali meals"}:
                facts["awaiting_product_price"] = False
                facts["last_topic"] = "savoury"
                facts["awaiting_menu_choice"] = True
                return finish("Would you like the vegetarian or non-vegetarian menu with prices?", "context_meal_price_followup")
            if text in {"sondesh", "sweets", "mishti", "mithai"}:
                facts["awaiting_product_price"] = False
                facts["last_topic"] = "sondesh_general"
                tmpl = self._template_by_id("sondesh_price_detailed")
                if tmpl.get("response") and not tmpl.get("escalate"):
                    reply, images = self._format_dynamic_response(tmpl["id"], tmpl["response"], user_id=user_id, channel=channel, user_message="sondesh price")
                    return finish(reply, tmpl["id"], images)
            facts["awaiting_product_price"] = False

        return None

    # Replies that carry no business fact: they ask a question, acknowledge, or
    # hand over to a person. They need a route, not a knowledge source.
    _CONVERSATIONAL_ROUTES = frozenset({
        "greeting", "acknowledgement", "conversation_closing", "clarify_request",
        "price_clarify_product", "context_meal_price_followup", "name_introduction",
        "recall_name", "bot_identity", "bot_capabilities", "explicit_escalation",
        "groq_structured_event_escalation", "ad_gate_blocked", "product_recommendation",
        "alacarte_sondesh_addon",
    })

    def _record_answer_sufficiency(
        self, facts: Dict[str, Any], template_id: Optional[str], was_matched: bool,
    ) -> None:
        """Give every factual answer a traceable source and a sufficiency call.

        The intent/entity path fills these in as it resolves knowledge. Answers
        that came from the deterministic compatibility handlers had a route but
        no recorded decision, which meant a customer-facing business fact could
        be sent with nothing saying where it came from. Conversational replies
        are exempt by design: "Sondesh or Meals?" asserts nothing.
        """
        if facts.get("last_knowledge_ids"):
            return
        # An upstream layer that already found something insufficient is not
        # overruled by whatever answered afterwards.
        recorded = facts.get("last_knowledge_sufficiency") or {}
        if recorded and not all(recorded.values()):
            return

        if not was_matched or not template_id:
            intents = facts.get("last_intents") or [Intent.UNKNOWN.value]
            facts["last_knowledge_sufficiency"] = {intent: False for intent in intents}
            facts["knowledge_sufficient"] = False
            facts.setdefault("last_grounding", None)
            return

        if template_id in self._CONVERSATIONAL_ROUTES or template_id.startswith("ad_"):
            return

        canonical_id = approved_knowledge.knowledge_id_for_template(template_id)
        template = self._template_by_id(template_id) or {}
        facts["last_knowledge_ids"] = [canonical_id or f"approved:{template_id}"]
        facts["last_knowledge_source"] = "canonical" if canonical_id else "approved_template"
        facts["last_source_ref"] = (
            template.get("source_ref") or f"qa_templates.json#{template_id}"
        )
        # Only an intent this very answer is the approved knowledge for may be
        # recorded as satisfied: an allergen question answered with a product
        # card is not a satisfied allergen question.
        intents = [
            intent for intent in (facts.get("last_intents") or [])
            if intent not in {Intent.UNKNOWN.value, Intent.BUSINESS_POLICY_UNKNOWN.value}
            and approved_knowledge.knowledge_id_for_intent(intent, template_id)
        ]
        keys = intents or [canonical_id.split(":")[0] if canonical_id else "compatibility_route"]
        facts["last_knowledge_sufficiency"] = {key: True for key in keys}
        facts["knowledge_sufficient"] = True
        if not facts.get("last_grounding"):
            facts["last_grounding"] = "deterministic_approved"

    def _apply_crosssell_nudges(
        self,
        template_id: str,
        response_text: str,
        image_url: Any,
        facts: Dict[str, Any],
        user_message: str = "",
    ) -> Tuple[str, Any]:
        """Apply the approved Sondesh add-on without broad food-flow spam."""
        # The expired-menu fallback ("kitchen is finalising this week's menu")
        # explicitly promises no photo and no price a customer could order
        # against — the nudge's own Sondesh card would break that promise.
        menu_is_being_updated = response_text.strip() == _WEEKLY_MENU_BEING_UPDATED.strip()
        lowered_message = " ".join((user_message or "").lower().split())
        alacarte_context = (
            template_id in ALACARTE_SONDESH_ADDON_TEMPLATES
            and bool(_ALACARTE_ADDON_MARKER_RE.search(lowered_message))
            and not _ALACARTE_ADDON_EXCLUSION_RE.search(lowered_message)
        )
        if (
            alacarte_context
            and not facts.get("alacarte_sondesh_addon_sent")
            and not facts.get("alacarte_sondesh_addon_declined")
            and not menu_is_being_updated
        ):
            facts["alacarte_sondesh_addon_sent"] = True
            facts["savoury_crosssell_sent"] = True
            response_text = response_text.rstrip() + "\n\n" + ALACARTE_SONDESH_ADDON_COPY
            image_url = self._append_sondesh_crosssell_images(image_url)
        elif template_id == "savoury_crosssell_sondesh":
            facts["savoury_crosssell_sent"] = True
            image_url = self._append_sondesh_crosssell_images(image_url)

        sondesh_templates = {
            "sondesh_price_detailed", "sondesh_price_general",
            "sondesh_details", "sondesh_unfamiliar",
            "classic_collection_details", "signature_collection_details",
            "aamar_mishti_details", "signature_sondesh_details",
            "jolbhora_talgash_details", "shelf_life_storage",
        }
        if template_id in sondesh_templates and not facts.get("sondesh_crosssell_sent"):
            facts["sondesh_crosssell_sent"] = True
            response_text = response_text.rstrip() + (
                '\n\n*P.S.* We also cook fresh Bengali meals for Delhi NCR — '
                "say *Meals* for this week's menu."
            )
        elif template_id == "sondesh_crosssell_savoury":
            facts["sondesh_crosssell_sent"] = True
        return response_text, image_url

    def _answer_addresses_the_question(
        self, user_message: str, template: Dict[str, Any],
    ) -> bool:
        """True when the selected answer is on the subject the customer raised.

        The test is lexical and deliberately simple: at least one content word
        of the question has to appear in the answer's own approved text or in
        the phrases it was written for. A question made entirely of content
        words we have never seen ("do you have washrooms", "is there wheelchair
        access", "can I bring pets") has nothing to anchor to and becomes a
        knowledge gap instead of the nearest-sounding approved answer.

        Conversational templates carry no subject of their own, so they are
        exempt: a greeting or an acknowledgement is a reply to the shape of
        the turn, not to a topic.
        """
        subject_free = {
            "greeting", "name_introduction", "recall_name", "acknowledgement",
            "conversation_closing", "bot_identity", "bot_capabilities",
            "clarify_request", "price_clarify_product", "product_recommendation",
            "recall_order_flavour", "order_status_tracking", "know_more",
        }
        template_id = template.get("id", "")
        if template_id in subject_free:
            return True

        content_words = {
            word for word in re.findall(r"\b[a-z]{3,}\b", user_message.lower())
            if word not in RELEVANCE_STOP_WORDS
        }
        if not content_words:
            return True

        haystack = " ".join([
            template_id.replace("_", " "),
            " ".join(str(trigger) for trigger in template.get("triggers", [])),
            str(template.get("response") or ""),
            " ".join(str(item) for item in template.get("responses", []) or []),
        ]).lower()
        haystack_words = set(re.findall(r"\b[a-z]{3,}\b", haystack))

        for word in content_words:
            if word in haystack_words:
                return True
            stem = word[:-1] if word.endswith("s") else word
            if len(stem) >= 4 and any(
                candidate.startswith(stem) or stem.startswith(candidate[:5])
                and len(candidate) >= 5 for candidate in haystack_words
            ):
                return True
        return False

    def _semantic_fallback_template(
        self,
        user_message: str,
        templates: List[Dict[str, Any]],
        user_id: str,
        channel: str,
    ) -> Optional[Dict[str, Any]]:
        """Return an approved low-risk template selected by the LLM router.

        This is deliberately a fallback, not a replacement for the keyword
        matcher. The router only interprets the customer's intent; the reply
        still comes verbatim from qa_templates.json. GUARD and escalation
        templates remain deterministic-only because their facts or handoff
        rules need an explicit anchor in the customer's message.
        """
        if not config.SEMANTIC_ROUTER_ENABLED:
            return None

        # Prefer the typed contract. It captures entities and multiple intents;
        # unlike the legacy router it cannot return an arbitrary template name.
        facts = session_memory.get_facts(user_id, channel)
        history = session_memory.get_session(user_id, channel).get("turns", [])
        understood = (
            understanding_engine.analyze(user_message, history_turns=history, state=facts)
            if _structured_nlu_enabled_for(user_id, channel)
            else None
        )
        if understood and understood.available and understood.confidence >= 0.65:
            facts["last_intents"] = understood.intents
            facts["last_entities"] = understood.entities
            template_map = {
                Intent.GREETING.value: "greeting",
                Intent.MENU_REQUEST.value: "know_more",
                Intent.PRODUCT_INFORMATION.value: "product_overview",
                Intent.PRODUCT_PRICE.value: "sondesh_price_detailed",
                Intent.DELIVERY_QUERY.value: "delivery_area",
            }
            if len(understood.intents) == 1:
                template_id = template_map.get(understood.intents[0])
                template = next((t for t in templates if t.get("id") == template_id), None)
                if template and template.get("response") and not template.get("escalate"):
                    facts["last_actions"] = [BusinessAction.SEND_RESPONSE.value]
                    facts["knowledge_sufficient"] = True
                    return template
            # A valid UNKNOWN/human result is evidence, not provider failure.
            # Do not let the legacy one-ID router coerce it into a nearby answer.
            facts["knowledge_sufficient"] = False
            return None

        try:
            template_id, detail = shadow_router.classify(user_message, templates, history)
        except Exception as exc:
            log.warning("Semantic router failed for %s: %s", channel, exc)
            return None

        if not template_id:
            log.info(
                "Semantic router did not route message_hash=%s message_length=%d (%s).",
                _safe_message_hash(user_message), len(user_message or ""), detail,
            )
            return None

        template = next((t for t in templates if t.get("id") == template_id), None)
        if not template or template_id.startswith("GUARD_") or template.get("escalate") is True:
            log.warning(
                "Semantic router proposed restricted template %r for message_hash=%s; falling back.",
                template_id, _safe_message_hash(user_message),
            )
            return None

        if not (template.get("response") or template.get("responses")):
            return None

        log.info(
            "Semantic router matched message_hash=%s to approved template %s.",
            _safe_message_hash(user_message), template_id,
        )
        return template

    def _structured_nlu_response(
        self,
        user_message: str,
        user_id: str,
        channel: str,
        intro_greeting_prefix: str = "",
    ) -> Optional[Tuple[str, bool, str, Optional[Any]]]:
        """Resolve every substantive request through intent/entity knowledge."""
        if not _structured_nlu_enabled_for(user_id, channel) or not understanding_engine.should_analyze(user_message):
            return None

        facts = session_memory.get_facts(user_id, channel)
        history = session_memory.get_session(user_id, channel).get("turns", [])
        result = understanding_engine.analyze(user_message, history_turns=history, state=facts)
        original_nlu_reason = result.reason
        model_result_available = result.available and result.confidence >= 0.65
        llm_called = result.reason not in {"provider_unavailable", "disabled_or_empty"}
        fallback_used = False
        if (
            not result.available
            or result.confidence < 0.65
            or result.intents == [Intent.UNKNOWN.value]
        ):
            deterministic = understanding_engine.deterministic_fallback(user_message)
            if not result.available or result.confidence < 0.65 or deterministic.intents != [Intent.UNKNOWN.value]:
                result = deterministic
                fallback_used = True

        # During a provider outage, keep the mature deterministic conversation
        # handlers as the compatibility layer for single, known intents. They
        # contain established slot filling and narrow approved-answer guards
        # (subscriptions, carts, delivery-time disambiguation, and so on) that
        # the canonical catalogue is absorbing incrementally. Compound asks and
        # explicit gaps stay here because the old fuzzy matcher cannot handle
        # them safely. The NLU attempt still happened first and is audited.
        fallback_intents = list(dict.fromkeys(result.intents))
        explicit_gap = result.requires_human or any(
            value in fallback_intents for value in (
                Intent.EVENT_ORDER.value,
                Intent.BULK_ORDER.value,
                Intent.DISCOUNT_REQUEST.value,
                Intent.AVAILABILITY_QUERY.value,
            )
        )
        parking_gap = "parking" in str(result.entities.get("topic") or "").lower()
        # A message naming one of the enumerated off-domain amenities (the
        # same list OUT_OF_SCOPE_TOPIC_RE refuses on the deterministic path)
        # must not defer to legacy trigger matching just because it also
        # contains a real business word ("is the delivery van wheelchair
        # accessible?" contains "delivery"). The old fuzzy matcher has no
        # notion of the topic already being ruled out and would re-score the
        # same word from scratch. This is deliberately narrower than "every
        # unknown_business_query": a topic the reader simply does not
        # recognize YET (a real gap in its concept vocabulary, not a ruled-
        # out amenity) must keep deferring to legacy, which is exactly the
        # compatibility floor this migration has not finished retiring.
        amenity_gap = bool(OUT_OF_SCOPE_TOPIC_RE.search(user_message.lower()))
        # A question that narrows a domain to one thing — gift wrapping, same-day
        # delivery, cancellation — has to be resolved here, because only the
        # knowledge layer can tell whether the approved copy covers that corner
        # of the domain. Letting it fall through would answer "do you gift wrap"
        # with the food-safe packaging copy.
        subtopic_gap = bool(result.entities.get("subtopic"))
        central_compound = (
            Intent.DELIVERY_QUERY.value in fallback_intents
            and any(value in fallback_intents for value in (
                Intent.MENU_REQUEST.value,
                Intent.PRODUCT_PRICE.value,
            ))
        )
        message_lower = user_message.lower().strip()

        # The deterministic reader is a semantic reader now, not a phrase list,
        # so what decides whether it may answer is the same thing that decides
        # for the model: does its reading resolve, in full, to approved
        # knowledge? If every intent it found has a record, the answer is the
        # catalogue's, not a trigger's. If any part does not resolve, the older
        # deterministic handlers still get their turn.
        location_for_trial = str(result.entities.get("location", "")).strip()
        trial = approved_knowledge.resolve(
            result,
            self.qa_data.get("templates", []),
            serviceable_location=(
                _is_serviceable_ncr_area(location_for_trial) if location_for_trial else None
            ),
        )
        # The reading may answer when every part of it resolves, and also when
        # part of it resolves and the rest is an honest gap — that is partial
        # fulfilment, and dropping to the old matcher would answer the
        # supported half while quietly losing the unsupported one.
        canonical_single = bool(
            trial.items
            and (trial.fully_sufficient or trial.partially_sufficient)
            and not result.requires_human
            and fallback_intents
            # A partly-read message stays with the older handlers: they may
            # know the flavour, collection or pack size this reader missed.
            and not result.unmapped_terms
        )
        # Two things the later deterministic handlers own outright: a price
        # question inside a live product topic, and any "how long / when will"
        # question, whose 15-minute and 45-minute answers depend on a context
        # this reader does not model.
        if canonical_single and TIME_INTENT_RE.search(message_lower):
            canonical_single = False
        # "rose 250g" is an order line, not a question about Rose Sondesh: a
        # flavour beside a pack size is the customer filling slots, which the
        # order draft owns.
        if (
            canonical_single
            and result.entities.get("product")
            and result.entities.get("pack_size")
            and not re.search(r"\b(what|which|how|do|does|is|are|can|tell)\b", message_lower)
        ):
            canonical_single = False
        if (
            canonical_single
            and Intent.PRODUCT_PRICE.value in fallback_intents
            and not result.entities.get("product")
            and facts.get("last_topic")
        ):
            canonical_single = False

        # A validated "I do not know what this is" is not the same as an
        # unsupported request. The deterministic templates still hold answers
        # the canonical catalogue has not absorbed yet — subscriptions, weekly
        # menus, bento tiers — so an unknown meaning continues into them
        # instead of escalating a question the bot can actually answer. It is
        # safe because the topics we must never guess at (parking and the rest)
        # are refused by name further down, whichever path is running.
        unknown_meaning = (
            fallback_intents == [Intent.UNKNOWN.value]
            and not parking_gap
            and not explicit_gap
            and not subtopic_gap
        )
        if unknown_meaning or (
            not model_result_available
            and not central_compound
            and not canonical_single
            and not explicit_gap
            and not parking_gap
            and not subtopic_gap
            and not amenity_gap
        ):
            facts["last_intents"] = fallback_intents
            facts["last_entities"] = dict(result.entities)
            facts["last_nlu_confidence"] = result.confidence
            facts["last_nlu_reason"] = original_nlu_reason
            facts["structured_nlu_provider_failed"] = _understanding_failed_closed(
                original_nlu_reason, model_result_available,
            )
            facts["last_llm_called"] = llm_called
            facts["last_fallback_used"] = True
            facts["last_grounding"] = "compatibility_handler"
            return None

        intents = list(dict.fromkeys(result.intents))
        facts["last_route_source"] = (
            "deterministic_semantic_fallback" if fallback_used else "model_nlu"
        )
        facts["last_intents"] = intents
        facts["last_entities"] = dict(result.entities)
        facts["last_nlu_confidence"] = result.confidence
        facts["last_nlu_reason"] = original_nlu_reason
        facts["structured_nlu_provider_failed"] = _understanding_failed_closed(
            original_nlu_reason, model_result_available,
        )
        location = str(result.entities.get("location", "")).strip()
        serviceable = _is_serviceable_ncr_area(location) if location else None
        resolution = approved_knowledge.resolve(
            result,
            self.qa_data.get("templates", []),
            serviceable_location=serviceable,
        )
        replies: List[str] = []
        approved_facts: List[str] = []
        images: List[Any] = []
        actions: List[str] = []
        used_templates: List[str] = []
        knowledge_ids: List[str] = []
        for item in resolution.items:
            rendered, rendered_images = self._format_dynamic_response(
                item.template_id or item.knowledge_id,
                item.approved_response,
                user_id=user_id,
                channel=channel,
                user_message=user_message,
            )
            # The once-per-session cross-sell belongs to the answer, not to the
            # layer that chose it, so it is applied here too.
            if item.template_id:
                rendered, rendered_images = self._apply_crosssell_nudges(
                    item.template_id, rendered, rendered_images, facts, user_message,
                )
            replies.append(rendered)
            approved_facts.append(rendered)
            knowledge_ids.append(item.knowledge_id)
            if item.template_id:
                used_templates.append(item.template_id)
                if item.template_id in TOPIC_MAP:
                    facts["last_topic"] = TOPIC_MAP[item.template_id]
            actions.extend(item.actions)
            if rendered_images:
                images.extend(rendered_images if isinstance(rendered_images, list) else [rendered_images])
                actions.append(BusinessAction.SEND_PRODUCT_IMAGE.value)

            if (
                item.template_id in {"know_more", "product_overview", "weekly_menu_veg", "weekly_menu_nonveg"}
                and not facts.get("previous_menus_offered")
            ):
                try:
                    if weekly_flyer.list_archive():
                        facts["previous_menus_offered"] = True
                        replies[-1] = replies[-1].rstrip() + (
                            "\n\n_Want an earlier week? Say *Previous menus* — "
                            "we keep the last two._"
                        )
                        approved_facts[-1] = replies[-1]
                except Exception as exc:
                    log.warning("Could not check flyer archive for structured nudge: %s", exc)

        if Intent.PRODUCT_PRICE.value in intents and not result.entities.get("product"):
            facts["awaiting_product_price"] = True
        if Intent.DELIVERY_QUERY.value in intents:
            facts["awaiting_area_confirmation"] = not bool(location)
        # The subscription business facts are canonical knowledge; where the
        # customer is in the booking conversation stays deterministic state,
        # and it is set the same way whichever path sent the plans.
        if "subscription_details" in used_templates:
            facts["awaiting_subscription_booking"] = True
            facts["subscription_context"] = True
        if "subscription_booking_steps" in used_templates:
            facts["awaiting_subscription_booking"] = False
            facts["subscription_context"] = True

        missing = list(dict.fromkeys(resolution.missing_intents))
        if result.requires_human and not missing:
            missing = list(intents)
        if missing:
            handoff, _ = self.build_handover_response(user_message)
            if Intent.EVENT_ORDER.value in missing and self.classify_unmatched_intent(user_message) == "unknown":
                handoff, _ = self.build_handover_response("bulk order " + user_message)
            replies.append(handoff)
            actions.extend([BusinessAction.ESCALATE_TO_TEAM.value, BusinessAction.SEND_RESPONSE.value])
            try:
                db.record_knowledge_gap(
                    user_message=user_message,
                    intents=missing,
                    entities=result.entities,
                    reason="unsupported_or_insufficient_knowledge",
                )
            except Exception as exc:
                log.warning("Could not record structured knowledge gap: %s", exc)

        knowledge_sufficient = resolution.fully_sufficient and not result.requires_human
        facts["knowledge_sufficient"] = knowledge_sufficient
        facts["last_knowledge_sufficiency"] = dict(resolution.sufficient_by_intent)
        facts["last_knowledge_ids"] = knowledge_ids
        facts["last_actions"] = list(dict.fromkeys(actions + [BusinessAction.SEND_RESPONSE.value]))
        facts["last_llm_called"] = llm_called
        facts["last_fallback_used"] = fallback_used

        response_text = "\n\n".join(dict.fromkeys(reply.strip() for reply in replies if reply.strip()))
        grounding = "deterministic_approved"
        # Prices and partial-knowledge handoffs stay byte-for-byte deterministic.
        # Other complete answers may be phrased from only the selected facts.
        keep_exact = any(template_id in {"weekly_menu_veg", "weekly_menu_nonveg"} for template_id in used_templates)
        if knowledge_sufficient and response_text and "₹" not in response_text and "```" not in response_text and not keep_exact:
            generated, ok, grounding_reason = ai_responder.compose_verified_response(
                user_message,
                intents,
                result.entities,
                approved_facts,
                user_context=session_memory.get_context_summary(user_id, channel),
                history_turns=history,
            )
            facts["last_llm_called"] = True
            if ok:
                response_text = generated
                grounding = "pass"
            else:
                grounding = f"fallback:{grounding_reason}"
        facts["last_grounding"] = grounding

        # The same guard the deterministic path applies: a short follow-up that
        # re-matches the answer just given gets an acknowledgement, not the
        # identical message twice.
        recent_replies = _recent_bot_replies(user_id, channel)
        if (
            _looks_like_a_short_answer(user_message)
            and _bot_asked_a_question(recent_replies[0] if recent_replies else "")
            and _normalise_reply(response_text) in {
                _normalise_reply(reply) for reply in recent_replies
            }
        ):
            log.info("Suppressed a duplicate structured reply for %s", channel)
            response_text = _DUPLICATE_REPLY_NOTICE
            images = []

        if intro_greeting_prefix:
            response_text = intro_greeting_prefix + response_text
        response_text = _strip_flower_emoji(response_text)
        deduped_images: List[Any] = []
        image_keys = set()
        for item in images:
            key = json.dumps(item, sort_keys=True, ensure_ascii=False) if isinstance(item, dict) else str(item)
            if key not in image_keys:
                image_keys.add(key)
                deduped_images.append(item)
        image_value: Optional[Any] = deduped_images or None
        if image_value and len(image_value) == 1 and not isinstance(image_value[0], dict):
            # One card from a single template keeps the list shape the
            # deterministic path returns, so adapters and tests see the same
            # thing whichever layer answered. A captioned card is already a
            # dict and stays wrapped.
            image_value = [image_value[0]]
        session_memory.add_turn(user_id, channel, user_message, response_text)

        if Intent.EVENT_ORDER.value in intents and missing:
            template_id = "groq_structured_event_escalation"
        elif not used_templates and all(
            intent in {Intent.UNKNOWN.value, Intent.BUSINESS_POLICY_UNKNOWN.value}
            for intent in intents
        ):
            # Nothing was answered, so there is no route to name. The typed
            # gap lives in last_intents and the audit row.
            template_id = None
        elif len(used_templates) == 1 and not missing:
            template_id = used_templates[0]
        elif set(intents) == {Intent.MENU_REQUEST.value, Intent.DELIVERY_QUERY.value} and not missing:
            template_id = "delivery_area"
        elif intents == [Intent.ESCALATION.value]:
            template_id = "explicit_escalation"
        elif intents:
            template_id = "compound_" + "_".join(intents)
        else:
            template_id = None
        if len(used_templates) > 1:
            log.info(
                "STRUCTURED_ROUTE message_hash=%s intents=%s knowledge=%s actions=%s sufficient=%s",
                _safe_message_hash(user_message), intents, knowledge_ids, facts["last_actions"], knowledge_sufficient,
            )
        return response_text, knowledge_sufficient, template_id, image_value

    def _match_and_respond_raw(
        self,
        user_message: str,
        user_id: str = None,
        channel: str = None,
        referral_source: str = None,
        referral_ad_id: str = None,
        referral_ad_title: str = None,
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
                user_message, user_id, channel, referral_source, referral_ad_id, referral_ad_title
            )
        finally:
            latency_monitor.record((time.perf_counter() - started) * 1000.0)

        # The reply is already decided. Showing the same message to the router
        # now costs the customer nothing — it is queued to a background worker
        # and its answer is only ever written to a table. Skipped for the
        # ad-gate block, which is not a customer message we answered at all.
        if config.ROUTER_SHADOW_ENABLED and template_id != "ad_gate_blocked":
            shadow_router.record_shadow(
                user_message=user_message,
                channel=channel,
                keyword_pick=template_id,
                templates=self.qa_data.get("templates", []),
                history_turns=session_memory.get_session(user_id, channel).get("turns", []),
            )

        # AI Resolution & Answers, Phase 3 slice 2: the triage model only
        # gets a message that deterministic/canonical resolution already gave
        # up on — was_matched is False here in exactly the cases that reach
        # the candidate/escalation/knowledge-gap machinery further down this
        # same fallback tail. Instagram-only and behind its own flag, on top
        # Instagram-only, so a future channel integration cannot accidentally
        # turn this assisted path into a WhatsApp automation surface.
        if (
            config.LLM_TRIAGE_ENABLED
            and channel == "instagram"
            and not was_matched
            and template_id != "ad_gate_blocked"
        ):
            triage_classifier.record_shadow(
                user_message=user_message,
                channel=channel,
                user_id=user_id,
                was_matched=was_matched,
                template_id=template_id,
            )

        if self._should_send_slow_notice(user_id, channel, response_text, template_id):
            session_memory.get_facts(user_id, channel)["slow_notice_sent"] = True
            log.info("Slow-reply notice sent to user_hash=%s on %s.", _safe_user_hash(user_id), channel)
            if was_matched:
                response_text = f"{config.SLOW_REPLY_NOTICE_TEXT}\n\n{response_text}"
            else:
                response_text = self._generic_fallback()

        try:
            audit_facts = session_memory.get_facts(user_id, channel)
            self._record_answer_sufficiency(audit_facts, template_id, was_matched)
            decision_lane = classify_lane(
                was_matched=was_matched,
                template_id=template_id,
                facts=audit_facts,
            )
            audit_facts["decision_lane"] = decision_lane
            audit_facts["route_trace"] = build_trace(
                lane=decision_lane, facts=audit_facts,
            ).as_dict()
            # Anything that answered before understanding ran did so from
            # conversation state. Recording which layer decided lets the
            # benchmark measure how much wording-driven routing is left.
            if not audit_facts.get("last_route_source"):
                audit_facts["last_route_source"] = (
                    "state_handler" if was_matched else "escalation"
                )
            actions = list(audit_facts.get("last_actions") or [])
            if response_text:
                actions.append(BusinessAction.SEND_RESPONSE.value)
            if image_url:
                actions.append(BusinessAction.SEND_PRODUCT_IMAGE.value)
            if not was_matched:
                actions.append(BusinessAction.ESCALATE_TO_TEAM.value)
            if template_id == "delivery_area":
                actions.append(
                    BusinessAction.ASK_DELIVERY_AREA.value
                    if audit_facts.get("awaiting_area_confirmation")
                    else BusinessAction.CONFIRM_DELIVERY_AREA.value
                )
            if template_id and any(token in template_id for token in ("price", "jolbhora", "kanchagolla")):
                actions.append(BusinessAction.SEND_PRICE_SEQUENCE.value)
            actions = list(dict.fromkeys(actions))
            audit_facts["last_actions"] = actions
            escalation_status = (
                "required" if BusinessAction.ESCALATE_TO_TEAM.value in actions or not was_matched
                else "not_required"
            )
            audit_facts["last_escalation_status"] = escalation_status
            knowledge_gap = (
                audit_facts.get("knowledge_sufficient") is False
                or (not was_matched and template_id is None)
            )
            db.log_message_route(
                user_id=user_id,
                channel=channel,
                user_message=user_message,
                route=template_id,
                intents=audit_facts.get("last_intents") or [],
                entities=audit_facts.get("last_entities") or {},
                confidence=audit_facts.get("last_nlu_confidence"),
                knowledge_ids=audit_facts.get("last_knowledge_ids") or [],
                actions=actions,
                llm_called=bool(audit_facts.get("last_llm_called")),
                fallback_used=bool(audit_facts.get("last_fallback_used")),
                grounding_result=audit_facts.get("last_grounding"),
                knowledge_gap=knowledge_gap,
                escalation_status=escalation_status,
                route_source=audit_facts.get("last_route_source"),
                decision_lane=decision_lane,
                state_context_used=bool(audit_facts["route_trace"].get("state_influenced")),
                source_context_influenced=bool(
                    audit_facts["route_trace"].get("source_context_influenced")
                ),
                handoff_reason=audit_facts["route_trace"].get("handoff_reason"),
            )
            log.info(
                "MESSAGE_ROUTE route=%s source=%s intents=%s entities=%s knowledge=%s actions=%s "
                "llm=%s fallback=%s grounding=%s gap=%s escalation=%s",
                template_id,
                audit_facts.get("last_route_source"),
                audit_facts.get("last_intents") or [],
                audit_facts.get("last_entities") or {},
                audit_facts.get("last_knowledge_ids") or [],
                actions,
                bool(audit_facts.get("last_llm_called")),
                bool(audit_facts.get("last_fallback_used")),
                audit_facts.get("last_grounding"),
                knowledge_gap,
                escalation_status,
            )
        except Exception as exc:
            log.warning("Could not record message route audit: %s", exc)

        return _strip_flower_emoji(response_text), was_matched, template_id, _strip_flower_emoji(image_url)

    def _resolve_response(
        self,
        user_message: str,
        user_id: str = None,
        channel: str = None,
        referral_source: str = None,
        referral_ad_id: str = None,
        referral_ad_title: str = None,
    ) -> Tuple[str, bool, Optional[str], Optional[Any]]:
        """
        Matches incoming message against qa_templates.json & menu.json.
        Updates session memory and falls back to Groq RAG.

        Returns:
            (response_text: str, was_matched: bool, template_id: Optional[str], image_url: str | List[str] | None)
        """
        user_id = user_id or "default_user"
        channel = channel or "default_channel"

        # 0. Instagram Ad-Gate Verification (Deliver strictly to Ad clickers unless bypassed)
        if channel == "instagram" and not config.BYPASS_AD_GATE and referral_source != "ADS":
            log.info(
                "Ad-Gate Block: Organic Instagram message from user_hash=%s blocked.",
                _safe_user_hash(user_id),
            )
            return "", False, "ad_gate_blocked", None

        if not user_message or not user_message.strip():
            return self._generic_fallback(), False, None, None

        lowered_text = user_message.lower().strip()
        opener_name, semantic_remainder = split_conversational_opener(
            lowered_text
        )
        facts = session_memory.get_facts(user_id, channel)
        facts.update({
            "last_intents": [],
            "last_entities": {},
            "last_actions": [],
            "last_knowledge_ids": [],
            "last_knowledge_sufficiency": {},
            "last_nlu_confidence": None,
            "last_nlu_reason": None,
            "last_llm_called": False,
            "last_fallback_used": False,
            "last_grounding": None,
            "decision_lane": None,
            "route_trace": {},
            "last_handoff_reason": None,
            "structured_nlu_provider_failed": False,
            "last_route_source": None,
        })
        # Each turn gets a fresh source-use audit value.  The source route
        # itself remains available for a later genuine follow-up; only this
        # turn's decision is reset here.
        facts["source_context_used"] = False
        facts["source_referential_intent"] = None

        # Client-approved QA is the hard deterministic fast path.  The
        # matcher may understand a paraphrase, but the response itself is
        # loaded from the source block and never sent through Groq, RAG,
        # response-library overrides, or cross-sell rewriting.
        # The registry matcher is deliberately broad, so safety and explicit
        # intent gates must run before it.  These exclusions preserve the
        # authoritative source copy without letting a product FAQ swallow a
        # grounded calculation, a fail-closed safety handoff, a purchase
        # handoff, or an explicit out-of-area delivery question.
        bento_template_for_precedence = self._template_by_id("bento_details") or {}
        source_route = facts.get("source_route") if isinstance(facts.get("source_route"), dict) else None
        source_route_active = bool(
            source_route
            and source_route.get("active", True)
            and not facts.get("source_context_superseded")
        )
        explicit_client_subject = bool(re.search(
            r"\b(?:sondesh|sandesh|mishti|mithai|bengali\s+bento|bento|"
            r"chicken\s+kosha|subscription|meal\s+plan|home\s+table|"
            r"aamar|signature|jolbhora|kanchagolla|mangsho)\b",
            lowered_text,
            re.I,
        ))
        source_topic_text = " ".join(
            str(source_route.get(key) or "")
            for key in ("routing_topic", "topic_name", "name")
        ) if source_route else ""
        # A non-Home-Table Story/Reel is authoritative for a vague follow-up
        # such as "what is this" or "what's the price".  An explicit named
        # subject is a new customer intent and must be allowed to select its
        # own canonical FAQ, even when stale source state is present.
        source_context_blocks_client_qa = bool(
            source_route_active
            and not HOME_TABLE_RE.search(source_topic_text)
            and not explicit_client_subject
        )
        client_qa_excluded = bool(
            _UNSUPPORTED_CUSTOMIZATION_RE.search(lowered_text)
            or _HIGH_RISK_UNSUPPORTED_RE.search(lowered_text)
            or _bento_grounded_comparison(
                user_message,
                str(bento_template_for_precedence.get("response") or ""),
            )
            or (
                re.search(r"\b(?:sondesh|sandesh|mishti|sweets?)\b", lowered_text)
                and DELIVERY_ACTION_RE.search(lowered_text)
                and NON_NCR_DESTINATION_RE.search(lowered_text)
            )
            or (
                channel == "instagram"
                and PURCHASE_ASSISTANCE_RE.search(lowered_text)
            )
            or (
                semantic_fallback._ESCALATION_INTENT_RE.search(lowered_text)
                and not semantic_fallback._IDENTITY_QUESTION_RE.search(lowered_text)
            )
            or source_context_blocks_client_qa
            or facts.get("source_route")
            or facts.get("ad_product")
            or facts.get("active_product_context")
            or _CANONICAL_ROUTE_HINT_RE.search(lowered_text)
        )
        client_match = (
            None
            if channel != "instagram" or client_qa_excluded
            else client_qa.match(user_message, facts)
        )
        if client_match:
            entry = client_match.entry
            asset_urls: List[str] = []
            asset_missing: List[str] = []
            asset_map = {
                "sondesh_addon_images": [
                    "/images/sondesh_addon_01.jpeg",
                    "/images/sondesh_addon_02.jpeg",
                    "/images/sondesh_addon_03.jpeg",
                ],
                "subscription_rate_chart": [
                    "/images/Everyday%20Meal%20Subscription%20Rate%20Chart.jpeg",
                ],
                "aamar_menu": ["/images/AAMAR%20MISHTI.jpeg"],
                "signature_menu": ["/images/Signature%20Sondesh.jpeg"],
                "weekly_menu": [current_menu_image("weekly_vegetarian", "/images/From%20Friday.jpeg")],
            }
            for asset_key in client_match.asset_keys:
                values = [value for value in asset_map.get(asset_key, []) if value]
                if not values:
                    asset_missing.append(asset_key)
                else:
                    asset_urls.extend(values)
            facts.update({
                "last_route_source": "client_approved_qa",
                "last_intents": [entry.qa_id],
                "last_entities": {"client_qa_id": entry.qa_id},
                "last_knowledge_ids": [f"client_qa:{entry.qa_id}"],
                "last_actions": [BusinessAction.SEND_RESPONSE.value],
                "knowledge_sufficient": not bool(asset_missing),
                "last_grounding": "asset-missing" if asset_missing else "exact_approved",
                "last_llm_called": False,
                "last_fallback_used": False,
                "client_qa_id": entry.qa_id,
                "client_qa_match_kind": client_match.match_kind,
                "client_qa_asset_status": "ASSET-MISSING" if asset_missing else "available",
                "client_qa_missing_assets": asset_missing,
            })
            client_topic = TOPIC_MAP.get(_CLIENT_QA_TEMPLATE_IDS.get(entry.qa_id, ""))
            if client_topic:
                facts["last_topic"] = client_topic
            if entry.qa_id in {"bento_details_client", "bento_price_client"}:
                facts["active_product_context"] = "bento"
            if entry.context == "home_table" and (
                facts.get("source_route")
                or facts.get("last_topic") == "bengali_home_table"
                or facts.get("home_table_context")
            ):
                facts["source_context_used"] = True
            if entry.context == "home_table":
                facts["home_table_context"] = True
                facts["last_topic"] = "bengali_home_table"
                source_route = facts.get("source_route")
                if isinstance(source_route, dict):
                    source_topic = " ".join(
                        str(source_route.get(key) or "")
                        for key in ("routing_topic", "topic_name", "name")
                    )
                    facts["source_context_used"] = bool(
                        re.search(r"bengali\s+home\s+table|home\s+table|kolkata\s+home", source_topic, re.I)
                        and not re.search(r"\b(?:bengali\s+home\s+table|home\s+table|kolkata\s+home)\b", lowered_text, re.I)
                    )
            if asset_missing:
                handoff, _ = self.build_handover_response(user_message)
                facts["last_actions"] = [
                    BusinessAction.ESCALATE_TO_TEAM.value,
                    BusinessAction.SEND_RESPONSE.value,
                ]
                session_memory.add_turn(user_id, channel, user_message, handoff)
                return handoff, False, "client_approved_asset_missing", None
            template_id = _CLIENT_QA_TEMPLATE_IDS.get(entry.qa_id)
            response_text = entry.answer
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, template_id or f"client_approved_{entry.qa_id}", asset_urls or None

        # A decline closes only this active add-on flow. It is session-scoped,
        # so a later conversation can offer the add-on again.
        if (
            facts.get("alacarte_sondesh_addon_sent")
            and _ALACARTE_ADDON_DECLINE_RE.fullmatch(lowered_text.strip(" .!?"))
        ):
            facts["alacarte_sondesh_addon_declined"] = True

        # A new, explicit business subject must be able to interrupt a stale
        # conversational slot before any slot handler runs. Without this
        # boundary a persisted contact, menu, price, or area confirmation can
        # consume a substantive request (for example, ``hi tell me more
        # about Kanchagolla``) as though it were an answer to an older
        # yes/no prompt. Bare confirmations remain untouched and continue
        # through their normal flow.
        explicit_subject_match = EXPLICIT_SUBJECT_RE.search(
            semantic_remainder or user_message
        )
        explicit_context_switch = bool(
            SUBSCRIPTION_DELIVERY_TIME_RE.search(lowered_text)
            or SUBSCRIPTION_CANONICAL_QUERY_RE.search(lowered_text)
            or SUBSCRIPTION_AREA_QUERY_RE.search(lowered_text)
            or MANAGER_CONTACT_QUERY_RE.search(lowered_text)
            or (
                TIME_INTENT_RE.search(lowered_text)
                and any(word in lowered_text for word in SONDESH_CONTEXT_WORDS)
            )
        )
        normalized_slot_answer = re.sub(
            r"[?!.,]+$", "", " ".join((semantic_remainder or user_message).lower().split())
        ).strip()
        is_relevant_slot_answer = (
            (
                facts.get("awaiting_area_confirmation")
                and _looks_like_an_area_answer(user_message)
                and not (
                    explicit_subject_match
                    and GENERIC_DETAIL_WRAPPER_RE.search(semantic_remainder or user_message)
                )
            )
            or (
                facts.get("awaiting_product_price")
                and normalized_slot_answer in {"meals", "meal", "sondesh", "sweets", "mishti", "mithai"}
            )
            or (
                facts.get("awaiting_menu_choice")
                and normalized_slot_answer in {
                    "veg", "vegetarian", "niramish", "veg menu",
                    "non veg", "non-veg", "nonveg", "non vegetarian", "non-vegetarian",
                    "non veg menu", "non-veg menu", "nonveg menu",
                }
            )
            or (
                (facts.get("awaiting_subscription_booking") or facts.get("subscription_context"))
                and bool(
                    SUBSCRIPTION_OPTION_RE.search(normalized_slot_answer)
                    or SUBSCRIPTION_BOOKING_INTENT_RE.search(normalized_slot_answer)
                    or SUBSCRIPTION_DATE_CHANGE_RE.search(normalized_slot_answer)
                )
            )
        )
        if (explicit_subject_match or explicit_context_switch) and not is_relevant_slot_answer:
            # The named product/topic is a deliberate subject switch. Keep
            # the source route for audit/history, but stop it from answering
            # the next vague message as though the old subject were current.
            facts["source_context_superseded"] = True
            interrupted_flags = []
            for flow_flag in (
                "awaiting_area_confirmation", "awaiting_product_price",
                "awaiting_menu_choice", "awaiting_subscription_booking",
                "subscription_context", "awaiting_instagram_contact",
                "awaiting_instagram_contact_confirmation", "awaiting_name",
                "home_table_context",
            ):
                if facts.get(flow_flag):
                    facts[flow_flag] = False
                    interrupted_flags.append(flow_flag)
            if facts.get("pending_instagram_escalation") is not None:
                facts["pending_instagram_escalation"] = None
                interrupted_flags.append("pending_instagram_escalation")
            if interrupted_flags:
                facts["last_flow_interrupted"] = interrupted_flags
                log.info(
                    "INTENT_INTERRUPT subject=%r flags=%s",
                    explicit_subject_match.group(0) if explicit_subject_match else "canonical_context",
                    interrupted_flags,
                )

        ad_product = product_for_ad(referral_ad_id, referral_ad_title) or facts.get("ad_product")
        if ad_product:
            facts["ad_product"] = ad_product

        # An explicit à la carte question is a new subject, even when the
        # session still carries a previous Story/Post topic. Resolve it before
        # source-topic handling so stale campaign context cannot answer the
        # customer's new menu question.
        if (
            _ALACARTE_DIRECT_QUERY_RE.search(lowered_text)
            and not MEAL_PREORDER_QUERY_RE.search(lowered_text)
            and not _ALACARTE_ADDON_EXCLUSION_RE.search(lowered_text)
        ):
            facts["source_context_superseded"] = True
            facts["home_table_context"] = False
            facts["last_route_source"] = "canonical_alacarte"
            template = self._template_by_id("know_more") or {}
            response_text, images = self._format_dynamic_response(
                "know_more", template.get("response") or self._generic_fallback(),
                user_id=user_id, channel=channel, user_message=user_message,
            )
            facts["last_topic"] = "savoury"
            response_text, images = self._apply_crosssell_nudges(
                "know_more", response_text, images, facts, user_message,
            )
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "know_more", images

        # Bengali Home Table is a live Kolkata campaign and must be resolved
        # before the broad ``kolkata_expansion_info`` matcher.  The campaign has
        # four separate deterministic trigger paths; the ambiguous bare-price
        # follow-up reuses the contextual introduction rather than guessing a
        # numeric answer that the supplied golden text does not specify.
        has_home_table_context = bool(facts.get("home_table_context"))
        has_home_table_subject = bool(HOME_TABLE_RE.search(lowered_text))
        if has_home_table_subject or has_home_table_context:
            if HOME_TABLE_BOOKING_RE.search(lowered_text):
                order_template = self._template_by_id("order_next_steps") or {}
                order_response = order_template.get("response") or self._generic_fallback()
                facts["last_route_source"] = "canonical_order_handoff"
                facts["last_topic"] = "order"
                session_memory.add_turn(user_id, channel, user_message, order_response)
                return order_response, False, "order_next_steps", None
            if HOME_TABLE_PAYMENT_RE.search(lowered_text):
                home_table_template_id = "bengali_home_table_payment"
            elif has_home_table_context and HOME_TABLE_BARE_PRICE_RE.fullmatch(lowered_text):
                home_table_template_id = "bengali_home_table_details"
            elif HOME_TABLE_DETAILED_PRICING_RE.search(lowered_text):
                home_table_template_id = "bengali_home_table_pricing"
            else:
                home_table_template_id = "bengali_home_table_details"
            home_table_template = self._template_by_id(home_table_template_id)
            home_table_response = home_table_template.get("response") or self._generic_fallback()
            facts["home_table_context"] = True
            facts["last_topic"] = "bengali_home_table"
            facts["active_product_context"] = None
            facts["source_context_used"] = False
            facts["last_route_source"] = "canonical_home_table"
            session_memory.add_turn(user_id, channel, user_message, home_table_response)
            return home_table_response, True, home_table_template_id, None

        # A direct human request always outranks an active order slot.  Without
        # this guard, "I need an agent" after a receipt could be treated as a
        # vague answer to the order flow and never reach the CAM handoff.
        if (
            channel == "instagram"
            and semantic_fallback._ESCALATION_INTENT_RE.search(lowered_text)
            and not semantic_fallback._IDENTITY_QUESTION_RE.search(lowered_text)
            and not MANAGER_CONTACT_QUERY_RE.search(lowered_text)
        ):
            fallback, _intent = self.build_handover_response(user_message)
            facts["last_route_source"] = "explicit_rule"
            facts["last_intents"] = [Intent.ESCALATION.value]
            facts["last_actions"] = [
                BusinessAction.ESCALATE_TO_TEAM.value,
                BusinessAction.SEND_RESPONSE.value,
            ]
            facts["knowledge_sufficient"] = False
            session_memory.add_turn(user_id, channel, user_message, fallback)
            return fallback, False, "explicit_escalation", None

        # Ready-to-order is a deterministic, fully-anchored intent (see
        # ORDER_READY_RE) and must be recognized here, before the generic
        # this/it source-reference clarify gate below. That gate's bare word
        # match on "this"/"it"/"that" would otherwise swallow phrases like
        # "book this" or "finalise it" into a false "which item?" prompt,
        # since ORDER_READY_RE would never get a chance to run. Checked
        # after safety/escalation, ahead of everything else.
        if ORDER_READY_RE.fullmatch(lowered_text):
            order_template = self._template_by_id("order_next_steps") or {}
            order_response = order_template.get("response") or self._generic_fallback()
            facts["last_route_source"] = "canonical_order_handoff"
            facts["last_topic"] = "order"
            session_memory.add_turn(user_id, channel, user_message, order_response)
            return order_response, False, "order_next_steps", None

        # A purchase request with an explicit need for help is also a complete
        # handoff intent, even when a Story/Post route is active.  Source-only
        # routes must not turn "I am ready to place my order and need help
        # booking" into an unrelated topic description.
        if channel == "instagram" and PURCHASE_ASSISTANCE_RE.search(lowered_text):
            order_template = self._template_by_id("order_next_steps") or {}
            order_response = order_template.get("response") or self._generic_fallback()
            facts["last_route_source"] = "canonical_order_handoff"
            facts["last_topic"] = "order"
            facts["source_context_used"] = False
            session_memory.add_turn(user_id, channel, user_message, order_response)
            return order_response, False, "order_next_steps", None

        # Safety-sensitive and customisation requests need their own approved
        # fact, not the nearest product/menu answer.  Keep the one confirmed
        # Bento substitution above the gate; every other unsupported variant
        # becomes a durable knowledge gap and human handoff.
        unsupported_reason = None
        if (
            _UNSUPPORTED_CUSTOMIZATION_RE.search(lowered_text)
            and not _CONFIRMED_BENTO_SUBSTITUTION_RE.search(lowered_text)
        ):
            unsupported_reason = "unsupported_customisation"
        elif _HIGH_RISK_UNSUPPORTED_RE.search(lowered_text):
            unsupported_reason = "unsupported_sensitive_allergy_or_medical_requirement"
        if unsupported_reason:
            facts["last_route_source"] = "explicit_rule"
            facts["last_handoff_reason"] = unsupported_reason
            facts["last_intents"] = [Intent.BUSINESS_POLICY_UNKNOWN.value]
            facts["last_knowledge_sufficiency"] = {
                Intent.BUSINESS_POLICY_UNKNOWN.value: False,
            }
            facts["knowledge_sufficient"] = False
            facts["last_actions"] = [
                BusinessAction.ESCALATE_TO_TEAM.value,
                BusinessAction.SEND_RESPONSE.value,
            ]
            fallback, _intent = self.build_handover_response(user_message)
            try:
                db.record_knowledge_gap(
                    user_message=user_message,
                    intents=[Intent.BUSINESS_POLICY_UNKNOWN.value],
                    entities={"reason": unsupported_reason},
                    reason=unsupported_reason,
                )
            except Exception as exc:
                log.warning("Could not record sensitive knowledge gap: %s", exc)
            session_memory.add_turn(user_id, channel, user_message, fallback)
            return fallback, False, None, None

        # Simple arithmetic/comparison is allowed only when every number is
        # parsed from the approved Bento response itself.
        bento_template = self._template_by_id("bento_details") or {}
        grounded_bento = _bento_grounded_comparison(
            user_message, str(bento_template.get("response") or "")
        )
        if grounded_bento:
            facts["last_route_source"] = "grounded_approved"
            facts["last_grounding"] = "grounded_approved"
            facts["last_knowledge_ids"] = ["approved:bento_details"]
            facts["last_knowledge_sufficiency"] = {
                Intent.PRODUCT_PRICE.value: True,
            }
            facts["knowledge_sufficient"] = True
            facts["last_intents"] = [Intent.PRODUCT_PRICE.value]
            facts["last_source_ref"] = "qa_templates.json#bento_details"
            session_memory.add_turn(user_id, channel, user_message, grounded_bento)
            return grounded_bento, True, "bento_grounded_comparison", None

        # A Sondesh delivery question naming a non-NCR city is a complete
        # approved out-of-area answer. Resolve it before compound NLU can
        # combine a generic product overview with a delivery-area prompt.
        if (
            re.search(r"\b(?:sondesh|sandesh|mishti|sweets?)\b", lowered_text)
            and DELIVERY_ACTION_RE.search(lowered_text)
            and NON_NCR_DESTINATION_RE.search(lowered_text)
        ):
            template = self._template_by_id("delivery_out_of_area") or {}
            response_text = template.get("response") or self._generic_fallback()
            facts["last_route_source"] = "canonical_delivery_out_of_area"
            facts["last_topic"] = "delivery"
            facts["last_knowledge_ids"] = ["approved:delivery_out_of_area"]
            facts["last_knowledge_sufficiency"] = {
                Intent.DELIVERY_QUERY.value: True,
            }
            facts["knowledge_sufficient"] = True
            facts["last_intents"] = [Intent.DELIVERY_QUERY.value]
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "delivery_out_of_area", None

        # A referral is a preference only for genuinely vague openings. It
        # runs before conversational shorthand (such as a bare "price"), so
        # a meal-ad click never receives the sweet-product price prompt.
        # A bare greeting/acknowledgement/sign-off must never consume an
        # active reel/post/ad route.  We intentionally bypass source
        # resolution rather than clearing the route, so "hi" followed by
        # "tell me more" can still use the shared content.
        greeting_text = re.sub(r'^(?:h+i+|h+a+i+)\b', 'hi', lowered_text)
        greeting_text = re.sub(r'^(?:h+e+y+)\b', 'hey', greeting_text)
        greeting_text = re.sub(r'^(?:h+e+l+o+|h+e+l+l+o+|h+l+o+)\b', 'hello', greeting_text)
        greeting_text = re.sub(r'^(?:namaskar|nomoshkar|nomoskar)\b', 'namaste', greeting_text)
        conversational_opener = bool(
            (opener_name and not semantic_remainder)
            or GREETING_ONLY_RE.fullmatch(greeting_text)
            or CLOSING_ONLY_RE.fullmatch(greeting_text)
            or ACK_ONLY_RE.fullmatch(greeting_text)
        )
        facts["greeting_intent"] = bool(GREETING_ONLY_RE.fullmatch(greeting_text))
        facts["conversational_opener"] = opener_name
        facts["semantic_remainder"] = semantic_remainder

        legacy_vague_opening = re.fullmatch(
            r"(?:hi+|hello+|hey+|namaste|menu|show menu|send menu|tell me more|interested|"
            r"price|prices|how much|cost|rate|available|is this available|what about this|"
            r"can i order this|i saw this|what is this|what else(?: do you have)?)[?!,. ]*",
            lowered_text,
        )
        explicit_product = re.search(
            r"\b(sondesh|mishti|mithai|sweet|jolbhora|kanchagolla|nolen gur|mahua|"
            r"aamar|signature|bento|meal|savoury|savory|curry|veg|non[ -]?veg)\b",
            lowered_text,
        )
        source_route = facts.get("source_route") if isinstance(facts.get("source_route"), dict) else None
        if explicit_product and (source_route or ad_product):
            # A named product/topic is a new subject. Keep the prior route in
            # the conversation evidence, but do not let a later pronoun jump
            # back to that older reel/post/ad after this explicit change.
            facts["source_context_superseded"] = True
        # A configured ad may carry a canonical product mapping without a
        # row in instagram_source_routes; that existing ad context is still a
        # valid subject for a follow-up. Reel/post context requires the active
        # source route established by the attachment.
        source_context_available = bool(
            (
                source_route.get("active", True)
                if source_route is not None
                else (channel == "instagram" and ad_product)
            )
        ) and not facts.get("source_context_superseded")
        detected_source_intent = (
            None
            if conversational_opener
            else source_referential_intent(semantic_remainder or user_message)
        )
        source_intent = detected_source_intent if source_context_available else None
        facts["source_referential_intent"] = source_intent
        # This exact client-approved wording asks for the general Camellia
        # food overview. Resolve it before the unavailable-source clarifier so
        # a stale Story route cannot turn it into a request to identify a post.
        if (
            channel == "instagram"
            and " ".join(user_message.lower().split()).strip(" .?!")
            == "can you tell me more about this"
        ):
            template_id = "savoury_food_intro"
            template = self._template_by_id(template_id) or {}
            response_text, images = self._format_dynamic_response(
                template_id,
                template.get("response") or self._generic_fallback(),
                user_id=user_id,
                channel=channel,
                user_message=user_message,
            )
            facts["last_topic"] = "savoury"
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, template_id, images
        if (
            channel == "instagram"
            and re.search(r"\b(?:tell|talk)\s+me\s+about\s+(?:the\s+)?bengali\s+bento\b", lowered_text)
        ):
            template_id = "bento_details"
            template = self._template_by_id(template_id) or {}
            response_text, images = self._format_dynamic_response(
                template_id,
                template.get("response") or self._generic_fallback(),
                user_id=user_id,
                channel=channel,
                user_message=user_message,
            )
            facts["last_topic"] = "bento"
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, template_id, images
        has_explicit_source_reference = bool(re.search(
            r"\b(this|that|it|above|reel|post|video|item|one)\b", lowered_text,
        ))
        if (
            channel == "instagram"
            and detected_source_intent
            and (
                has_explicit_source_reference
                or detected_source_intent in {"details", "order"}
            )
            and not source_context_available
            and not explicit_product
            # "now THAT you're moving to Kolkata" is a conjunction, not a
            # reference to a post/reel/product — has_explicit_source_reference
            # matches the bare word "that" with no grammatical awareness.
            # An unambiguous Kolkata-expansion question has its own approved
            # canonical answer further below and must never be diverted here.
            and not KOLKATA_UNAMBIGUOUS_RE.search(lowered_text)
        ):
            facts["clarify_sent"] = True
            clarification = (
                "Which item are you interested in? Please tell me the sweet, meal, "
                "or post/reel you mean and I'll share the right details."
            )
            session_memory.add_turn(user_id, channel, user_message, clarification)
            return clarification, True, "clarify_request", None
        # Keep the historical shorthand behavior, while allowing ordinary
        # human phrasing such as "this one please" or "price for this".
        # A pack-size follow-up is still about the source product even after
        # the first Story/Reel/Post answer established ``last_topic``.
        source_size_followup = bool(re.search(r"\b(?:250|500|1000|1)\s*(?:g|kg)\b", lowered_text))
        vague_opening = bool(
            source_intent
            or source_size_followup
            or (legacy_vague_opening and not conversational_opener)
        )
        source_topic = (
            db.get_instagram_topic(source_route["topic_id"])
            if source_route and source_route.get("topic_id") else None
        )
        priorities = [
            product for product in (source_route or {}).get("priority_products", [])
            if route_template_id(product)
        ]
        active_flow = any(facts.get(flag) for flag in (
            "awaiting_subscription_booking", "awaiting_menu_choice", "awaiting_product_price",
            "awaiting_area_confirmation",
        ))
        source_product = priorities[0] if priorities else ad_product
        route_menu_mode = str((source_route or {}).get("menu_mode") or "INHERIT").strip().upper()
        menu_attached = route_menu_mode != "NONE"
        what_else = bool(re.fullmatch(r"what else(?: do you have)?[?!,. ]*", lowered_text))
        source_context_authoritative = bool(
            source_route and not facts.get("source_context_superseded")
        )

        # A mapped Bengali Home Table Story is a canonical campaign source,
        # not a generic topic-only route.  Its ambiguous follow-ups still
        # have approved campaign answers: "tell me more about this" maps to
        # the introduction and "What's the price?" maps to the Home Table
        # pricing response.  Without this bridge, a route with no priority
        # product falls through to the generic topic overview and never
        # reaches the campaign's exact pricing template.
        source_topic_name = str((source_route or {}).get("routing_topic") or "")
        if (
            channel == "instagram"
            and source_context_available
            and source_route
            and source_intent in {"details", "price"}
            and not explicit_product
            and HOME_TABLE_RE.search(source_topic_name)
        ):
            home_table_template_id = (
                "bengali_home_table_pricing"
                if source_intent == "price"
                else "bengali_home_table_details"
            )
            home_table_template = self._template_by_id(home_table_template_id)
            home_table_response = home_table_template.get("response") or self._generic_fallback()
            facts["home_table_context"] = True
            facts["source_context_used"] = True
            facts["last_route_source"] = "source_home_table"
            facts["last_topic"] = "bengali_home_table"
            facts["active_product_context"] = None
            session_memory.add_turn(user_id, channel, user_message, home_table_response)
            return home_table_response, True, home_table_template_id, None

        can_use_source = not active_flow and (
            not facts.get("last_topic")
            or what_else
            or source_size_followup
            or source_context_authoritative
        )
        if channel == "instagram" and source_context_available and source_product and menu_attached and vague_opening and not explicit_product and can_use_source:
            cursor = int(facts.get("source_priority_cursor") or 0)
            if what_else and priorities:
                source_product = priorities[min(cursor, len(priorities) - 1)]
            wants_price = source_intent == "price" or bool(re.search(r"price|cost|rate|how much", lowered_text))
            template_id = route_template_id(source_product, price=wants_price) or route_template_id(source_product)
            template = self._template_by_id(template_id)
            if template and template.get("response") and not template.get("escalate"):
                response_text, images = self._format_dynamic_response(template_id, template["response"], user_id=user_id, channel=channel, user_message=user_message)
                # The Topic's own admin-authored description frames the
                # approved product answer that follows it — safe because it
                # is text an operator wrote, not anything inferred from the
                # topic name or generated here.
                if source_topic and source_topic.get("description"):
                    response_text = compose_topic_response(
                        source_topic.get("name") or source_route.get("routing_topic"),
                        source_topic.get("description"), response_text,
                        source_topic.get("response_order") or "description_first",
                        route_menu_mode,
                    )
                facts["source_context_used"] = True
                facts["source_priority_cursor"] = min(cursor + 1, len(priorities)) if priorities else 1
                # Keep the canonical product topic available for a later
                # bare follow-up while the exact source route remains the
                # authoritative context for referential questions.
                facts["last_topic"] = TOPIC_MAP.get(template_id, source_product)
                facts["active_product_context"] = source_product
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, f"source_{source_product}_{template_id}", images

        # A topic-only route may frame a vague opening, but the body still
        # comes from the approved overview template. The admin label is not
        # allowed to supply a price, promise, availability, or other fact.
        if channel == "instagram" and source_context_available and source_route and vague_opening and not explicit_product and can_use_source and menu_attached:
            template = self._template_by_id("know_more")
            if template and template.get("response"):
                response_text, images = self._format_dynamic_response(
                    "know_more", template["response"], user_id=user_id, channel=channel,
                    user_message=user_message,
                )
                topic = str(source_route.get("routing_topic") or "").strip()
                if source_topic and source_topic.get("description"):
                    # The same helper is used by the admin Preview Reply, so
                    # a Topic's saved order cannot drift between preview/live.
                    response_text = compose_topic_response(
                        topic, source_topic.get("description"), response_text,
                        source_topic.get("response_order") or "description_first",
                        route_menu_mode,
                    )
                elif topic:
                    response_text = f"About {topic}:\n\n{response_text}"
                # The Topic's linked menu image, when one is published, rides
                # alongside the overview cards already in `images` — never in
                # place of them, and never inferred from anything but the
                # admin's own default_menu_id choice on the Topic.
                linked_menu_id = (source_route or {}).get("menu_id") or (source_topic or {}).get("default_menu_id")
                if linked_menu_id:
                    linked_image = current_menu_image(linked_menu_id, None)
                    if linked_image:
                        if isinstance(images, list):
                            if linked_image not in images:
                                images = images + [linked_image]
                        elif images:
                            images = [images, linked_image] if images != linked_image else images
                        else:
                            images = [linked_image]
                facts["source_context_used"] = True
                if source_product:
                    facts["last_topic"] = TOPIC_MAP.get(route_template_id(source_product) or "", source_product)
                    facts["active_product_context"] = source_product
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, "source_topic_know_more", images

        # An explicit NONE route may publish its admin-authored description,
        # but must not inherit a Topic menu, priority product, or overview
        # template. With no description, the normal safe fallback continues.
        if channel == "instagram" and source_context_available and source_route and route_menu_mode == "NONE" and not explicit_product and can_use_source:
            description = (source_topic or {}).get("description") or ""
            if description:
                response_text = compose_topic_response(
                    "", description, "", "description_first", "NONE",
                )
                facts["source_context_used"] = True
                if source_product:
                    facts["last_topic"] = TOPIC_MAP.get(route_template_id(source_product) or "", source_product)
                    facts["active_product_context"] = source_product
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, "source_topic_description_only", None

        # An open flow is an answer the bot itself asked for: "which area are
        # you in", "Sondesh or Meals", "Option 1, 2 or 3". Those turns are
        # short, context-only replies whose meaning lives in the state, not in
        # the words, so the deterministic slot handlers below finish them and
        # generic understanding stays out of the way.
        active_conversation_flow = any(facts.get(key) for key in (
            "awaiting_area_confirmation", "awaiting_product_price",
            "awaiting_menu_choice", "awaiting_subscription_booking",
            "subscription_context",
            "awaiting_instagram_contact", "awaiting_instagram_contact_confirmation",
            "pending_instagram_escalation",
        )) or bool(facts.get("cart")) or sum(
            # An order already under way is a slot the customer is answering:
            # "250g" or "Collection 1" completes a receipt rather than asking
            # for a product page. One lone slot is not yet an order — a single
            # product mention fills one on its own.
            1 for slot in ("flavour", "collection", "pack_size", "quantity")
            if (facts.get("order_draft") or {}).get(slot)
        ) >= 2

        # Slot continuations and safety routing run before understanding. The
        # bare price clarification is the one branch the structured path owns
        # when it is eligible, so the audit records which knowledge answered.
        structured_owns_price = bool(
            _structured_nlu_enabled_for(user_id, channel)
            and understanding_engine.should_analyze(user_message)
            and not active_conversation_flow
            and not (
                channel == "instagram"
                and isinstance(facts.get("source_route"), dict)
                and BARE_PRICE_QUESTION_RE.fullmatch(" ".join(user_message.lower().split()).strip(" .?!"))
            )
        )
        if config.CONVERSATION_IMPROVEMENTS_ENABLED:
            conversational = self._conversation_request(
                user_message, user_id, channel,
                structured_owns_price=structured_owns_price,
            )
            if conversational is not None:
                return conversational

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

        # A location sent immediately after the delivery template's area
        # question is an answer to that question, even when it contains words
        # such as "Delhi" that would otherwise match delivery_area again.
        if facts.get("awaiting_area_confirmation"):
            if _looks_like_an_area_answer(user_message):
                area = " ".join(user_message.split())
                if _is_serviceable_ncr_area(area):
                    # Keep the question active for a customer comparing two
                    # localities in succession (for example Chatra Marg, then
                    # Lajpat Nagar).
                    facts["awaiting_area_confirmation"] = True
                    response_text = (
                        f"Yes, we deliver to {area} — courier charges on actuals "
                        "from Sushant Lok, Gurgaon."
                    )
                    template_id = "delivery_area_confirmation"
                else:
                    # A location we cannot positively identify as NCR must not
                    # receive a delivery promise. Reuse the client-approved
                    # outside-Delhi-NCR message and close the area flow.
                    facts["awaiting_area_confirmation"] = False
                    out_of_area = next(
                        (template for template in self.qa_data.get("templates", [])
                         if template.get("id") == "delivery_out_of_area"),
                        {},
                    )
                    response_text = out_of_area.get("response") or self._generic_fallback()
                    template_id = "delivery_out_of_area"
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, template_id, None
            # A new substantive message ends the area-comparison flow and is
            # processed by the usual matcher below.
            facts["awaiting_area_confirmation"] = False

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
                    "Thank you so much! Lovely chatting with you.\n\n"
                    "Whenever you'd like Sondesh or this week's Bengali meals, just message here — "
                    "our kitchen is open 10:00 AM – 7:30 PM every day. Take care!"
                )
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, "conversation_closing", None

            if ACK_ONLY_RE.match(lowered_text):
                response_text = (
                    "Got it! 😊 What would you like to do next?\n\n"
                    "• Sondesh flavours & prices\n"
                    "• 🍱 This week's Bengali meal menu\n"
                    "• 🚚 Delivery details for your area\n"
                    "• 🛒 Place an order"
                )
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, "acknowledgement", None

        # Structured NLU handles compound and event requests before broad
        # escalation keywords. Complaints/refunds are not eligible here and
        # continue into the deterministic guard below. An open slot is
        # excluded: its own handler owns the customer's answer.
        semantic_resolution_text = semantic_remainder or user_message
        explicit_subject_in_detail_wrapper = bool(
            EXPLICIT_SUBJECT_RE.search(semantic_resolution_text)
            and GENERIC_DETAIL_WRAPPER_RE.search(semantic_resolution_text)
        )

        # This client-approved wording is the general food pre-order question,
        # not a subscription follow-up. Resolve it before stale subscription
        # state can route it to the subscription booking steps.
        if (
            channel == "instagram"
            and " ".join(user_message.lower().split()).strip(" .?!")
            == "what are the next steps to pre book"
        ):
            template = self._template_by_id("alacarte_prebooking_steps") or {}
            response_text = template.get("response") or self._generic_fallback()
            response_text, images = self._apply_crosssell_nudges(
                "alacarte_prebooking_steps", response_text, None, facts,
            )
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "alacarte_prebooking_steps", images

        # Keep the client-confirmed subscription area, pickup contact, and
        # savoury pre-order answers deterministic when the wording is explicit.
        # These are approved template replies, not new facts or model guesses.
        if SUBSCRIPTION_CANONICAL_QUERY_RE.search(lowered_text):
            template = self._template_by_id("subscription_details") or {}
            response_text, images = self._format_dynamic_response(
                "subscription_details", template.get("response") or self._generic_fallback(),
                user_id=user_id, channel=channel, user_message=user_message,
            )
            facts["awaiting_subscription_booking"] = True
            facts["subscription_context"] = True
            facts["last_topic"] = "subscription"
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "subscription_details", images

        if SUBSCRIPTION_AREA_QUERY_RE.search(lowered_text):
            template = self._template_by_id("subscription_details") or {}
            response_text = template.get("response") or self._generic_fallback()
            facts["awaiting_subscription_booking"] = True
            facts["subscription_context"] = True
            facts["last_topic"] = "subscription"
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "subscription_details", None

        if PICKUP_CONTACT_QUERY_RE.search(lowered_text):
            template = self._template_by_id("GUARD_pickup_address_hours") or {}
            response_text = template.get("response") or self._generic_fallback()
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "GUARD_pickup_address_hours", None

        if MEAL_PREORDER_QUERY_RE.search(lowered_text):
            template = self._template_by_id("alacarte_prebooking_steps") or {}
            response_text = template.get("response") or self._generic_fallback()
            response_text, images = self._apply_crosssell_nudges(
                "alacarte_prebooking_steps", response_text, None, facts, user_message,
            )
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "alacarte_prebooking_steps", images

        # The approved subscription answer gives the current plan and delivery
        # charge policy, but does not promise a delivery duration. Keep an
        # explicit subscription-delivery question on that canonical route
        # instead of borrowing the savoury 45-minute answer.
        if (
            not active_conversation_flow
            and SUBSCRIPTION_DELIVERY_TIME_RE.search(lowered_text)
        ):
            template = self._template_by_id("subscription_details") or {}
            response_text, images = self._format_dynamic_response(
                "subscription_details", template.get("response") or self._generic_fallback(),
                user_id=user_id, channel=channel, user_message=user_message,
            )
            facts["awaiting_subscription_booking"] = True
            facts["subscription_context"] = True
            facts["last_topic"] = "subscription"
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "subscription_details", images

        # Context-free booking advice has a complete approved answer of its
        # own. Keep subscription/Home Table/event flows above or outside this
        # shortcut, and leave explicit product questions to their own routes.
        if (
            not active_conversation_flow
            and not facts.get("subscription_context")
            and SAVOURY_BOOKING_TIME_RE.search(lowered_text)
            and not re.search(r"\b(?:subscription|home table|event|wedding|corporate)\b", lowered_text)
        ):
            template = self._template_by_id("savoury_best_time_to_book") or {}
            response_text, images = self._format_dynamic_response(
                "savoury_best_time_to_book", template.get("response") or self._generic_fallback(),
                user_id=user_id, channel=channel, user_message=user_message,
            )
            response_text, images = self._apply_crosssell_nudges(
                "savoury_best_time_to_book", response_text, images, facts, user_message,
            )
            if intro_greeting_prefix:
                response_text = intro_greeting_prefix + response_text
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "savoury_best_time_to_book", images

        # A generic delivery-time question has no product noun for the normal
        # scorer to distinguish. Treat it as savoury only when it is not
        # explicitly about Sondesh, a subscription, or an event.
        if (
            not active_conversation_flow
            and not facts.get("subscription_context")
            and (GENERIC_SAVOURY_DELIVERY_TIME_RE.search(lowered_text) or (
                TIME_INTENT_RE.search(lowered_text)
                and re.search(r"\b(?:delivery|deliver|arrive)\b", lowered_text)
            ))
            and not any(word in lowered_text for word in SONDESH_CONTEXT_WORDS)
            and not re.search(r"\b(?:subscription|home table|event|wedding|corporate)\b", lowered_text)
        ):
            template = self._template_by_id("savoury_delivery_time") or {}
            response_text, images = self._format_dynamic_response(
                "savoury_delivery_time", template.get("response") or self._generic_fallback(),
                user_id=user_id, channel=channel, user_message=user_message,
            )
            response_text, images = self._apply_crosssell_nudges(
                "savoury_delivery_time", response_text, images, facts, user_message,
            )
            if intro_greeting_prefix:
                response_text = intro_greeting_prefix + response_text
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "savoury_delivery_time", images

        # Opening-hours questions are a deterministic part of the approved
        # delivery-time answer. Resolve them before model NLU can classify the
        # wording as an unsupported business question.
        if (
            not active_conversation_flow
            and not facts.get("subscription_context")
            and OPENING_HOURS_RE.search(lowered_text)
        ):
            template = self._template_by_id("opening_hours") or {}
            response_text, images = self._format_dynamic_response(
                "opening_hours", template.get("response") or self._generic_fallback(),
                user_id=user_id, channel=channel, user_message=user_message,
            )
            if intro_greeting_prefix:
                response_text = intro_greeting_prefix + response_text
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "opening_hours", images

        if (
            not active_conversation_flow
            and not explicit_subject_in_detail_wrapper
            and not ORDER_READY_RE.fullmatch(lowered_text)
        ):
            structured = self._structured_nlu_response(
                user_message, user_id, channel, intro_greeting_prefix,
            )
            if structured is not None:
                return structured

        # Topics the business has no approved answer for must never be
        # answered by the nearest-sounding template. "Do you have valet
        # parking?" is one edit away from the "packing" trigger on the
        # packaging answer, and fuzzy matching read it as a typo. Naming these
        # topics here keeps the refusal true of the deterministic path as
        # well, not only of the intent/entity path.
        if OUT_OF_SCOPE_TOPIC_RE.search(lowered_text):
            facts["last_route_source"] = "explicit_rule"
            facts["last_intents"] = [Intent.BUSINESS_POLICY_UNKNOWN.value]
            facts["last_knowledge_sufficiency"] = {Intent.UNKNOWN.value: False}
            facts["knowledge_sufficient"] = False
            facts["last_actions"] = [
                BusinessAction.ESCALATE_TO_TEAM.value, BusinessAction.SEND_RESPONSE.value,
            ]
            fallback, _intent = self.build_handover_response(user_message)
            try:
                db.record_knowledge_gap(
                    user_message=user_message,
                    intents=[Intent.UNKNOWN.value],
                    entities={"topic": OUT_OF_SCOPE_TOPIC_RE.search(lowered_text).group(0)},
                    reason="topic_outside_approved_knowledge",
                )
            except Exception as exc:
                log.warning("Could not record out-of-scope knowledge gap: %s", exc)
            if intro_greeting_prefix:
                fallback = intro_greeting_prefix + fallback
            session_memory.add_turn(user_id, channel, user_message, fallback)
            return fallback, False, None, None

        # 2. Check explicit escalation triggers first (refund, complaint, etc.)
        escalation_reason = self.check_explicit_escalation_triggers(user_message)
        if escalation_reason:
            facts["last_route_source"] = "explicit_rule"
            fallback, _intent = self.build_handover_response(user_message)
            if intro_greeting_prefix:
                fallback = intro_greeting_prefix + fallback
            session_memory.add_turn(user_id, channel, user_message, fallback)
            return fallback, False, "explicit_escalation", None

        # Payment facts are a high-risk canonical answer. Resolve short forms
        # such as "bank transfer" and "payment details" before fuzzy scoring
        # can turn them into a knowledge gap or a nearby product answer, while
        # allowing complaints and other explicit escalations above to win.
        if PAYMENT_QUERY_RE.search(lowered_text):
            payment_template = self._template_by_id("GUARD_payment_method")
            payment_response = payment_template.get("response") or self._generic_fallback()
            facts["last_topic"] = "payment"
            facts["last_route_source"] = "canonical_payment"
            facts["source_context_used"] = False
            session_memory.add_turn(user_id, channel, user_message, payment_response)
            return payment_response, True, "GUARD_payment_method", None

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

        # Ready-to-order phrases are resolved earlier now (right after the
        # escalation guard), before the this/it source-reference clarify
        # gate gets a chance to hijack them.

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
        active_product = facts.get("active_product_context")
        followup_text = cleaned_business_text.strip() or user_message.strip()
        if active_product and not explicit_product and re.search(
            r"\b(?:250|500|1000)\s*(?:g|kg)\b|\b(?:price|cost|rate|how much)\b",
            followup_text,
        ):
            wants_price = bool(re.search(r"price|cost|rate|how much", lowered_text))
            active_template_id = route_template_id(str(active_product), price=wants_price)
            active_template = self._template_by_id(active_template_id) if active_template_id else None
            if active_template and active_template.get("response"):
                response_text, images = self._format_dynamic_response(
                    active_template_id, active_template["response"], user_id=user_id,
                    channel=channel, user_message=user_message,
                )
                facts["source_context_used"] = False
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, active_template_id, images
        last_topic = facts.get("last_topic")
        # The greeting-stripped text, so "Hi, what is the price?" is treated as
        # the same bare price question as "what is the price?" — a customer who
        # says hello first is not asking a different question.
        stripped_for_bare_check = followup_text
        if last_topic and BARE_VEG_RE.match(stripped_for_bare_check):
            if last_topic == "savoury":
                response_text = (
                    "Both! Our weekly Bengali meals include vegetarian dishes (Aloor Dom, Dhokar Dalna, "
                    "Basonti Polao) and non-vegetarian dishes (Bhetki Paturi, Chingri Malaikari, Robibarer Mangsho).\n\n"
                    "Would you like to see the Veg or Non-Veg menu?"
                )
            else:
                response_text = (
                    "Yes, always! Every Sondesh we make — Aamar Mishti and Signature Collection alike — is "
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

        if last_topic == "bento" and BARE_PRICE_RE.match(stripped_for_bare_check):
            template = self._template_by_id("bento_details") or {}
            response_text, images = self._format_dynamic_response(
                "bento_details", template.get("response") or self._generic_fallback(),
                user_id=user_id, channel=channel, user_message=user_message,
            )
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "bento_details", images

        # A bare "what's the price?" with no product topic established yet is
        # genuinely ambiguous — Sondesh and Bengali meals are priced and sold
        # completely differently (per box vs per dish), and this is often the
        # very first message on an Instagram ad reply, where we cannot tell
        # from the referral payload which product the ad was even for. Ask
        # rather than default to Sondesh, which was answering meal-ad
        # customers with the wrong price sheet entirely.
        if not last_topic and BARE_PRICE_RE.match(stripped_for_bare_check):
            response_text = (
                "Happy to share prices! Are you asking about our *Sondesh* "
                "(handcrafted Bengali sweets) or our *Bengali Meals* (fresh "
                "lunch/dinner bento)?"
            )
            if intro_greeting_prefix:
                response_text = intro_greeting_prefix + response_text
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, "price_clarify_product", None

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

        # A customer who has just been sent the subscription plans is answering
        # its closing "Would you like to book?" — with a bare "yes", "sign me
        # up", or "I'll take option 2". None of those carry a subscription
        # word, so trigger scoring sent them to the Sondesh Signature
        # Collection ("option 2" is Collection 2 there) or to the generic
        # pre-ordering blurb. Context answers them from the subscription
        # templates the customer is actually looking at.
        # The date-change question usually arrives after the steps have been
        # sent and the awaiting flag has been cleared, so the subscription
        # context outlives it.
        if facts.get("awaiting_subscription_booking") or facts.get("subscription_context"):
            choosing_a_plan = bool(facts.get("awaiting_subscription_booking"))
            asks_about_an_option = choosing_a_plan and bool(SUBSCRIPTION_OPTION_RE.search(lowered_text))
            wants_to_book = choosing_a_plan and bool(SUBSCRIPTION_BOOKING_INTENT_RE.search(lowered_text))
            asks_about_subscription_price = bool(BARE_PRICE_RE.match(stripped_for_bare_check))
            changes_a_date = bool(SUBSCRIPTION_DATE_CHANGE_RE.search(lowered_text)) and not any(
                w in lowered_text for w in SONDESH_CONTEXT_WORDS
            )
            follow_up_id = None
            if changes_a_date or wants_to_book:
                # The booking steps carry the 5-hour date-change notice.
                follow_up_id = "subscription_booking_steps"
            elif asks_about_subscription_price:
                # A subscription customer asking the bare follow-up "price?"
                # must receive the subscription answer and its exact chart, not
                # the generic savoury per-dish clarification.
                follow_up_id = "subscription_details"
            elif asks_about_an_option:
                # What each option contains is in the plans message itself.
                follow_up_id = "subscription_details"

            if follow_up_id:
                tmpl = self._template_by_id(follow_up_id)
                if tmpl and tmpl.get("response"):
                    response_text, images = self._format_dynamic_response(
                        follow_up_id, tmpl["response"], user_id=user_id,
                        channel=channel, user_message=user_message,
                    )
                    facts["subscription_context"] = True
                    if follow_up_id == "subscription_booking_steps":
                        facts["awaiting_subscription_booking"] = False
                    if intro_greeting_prefix:
                        response_text = intro_greeting_prefix + response_text
                    session_memory.add_turn(user_id, channel, user_message, response_text)
                    return response_text, True, follow_up_id, images

        if last_topic and BARE_PRICE_RE.match(stripped_for_bare_check) and last_topic in TOPIC_PRICE_HINT:
            hint = TOPIC_PRICE_HINT[last_topic]
            lowered_text = f"{lowered_text} {hint}".strip()
            cleaned_business_text = f"{cleaned_business_text} {hint}".strip()
            cleaned_words = set(re.findall(r'\b\w+\b', cleaned_business_text))

        # "How long / how soon / what time" delivery questions were matching
        # on raw keyword overlap, which meant a food-context question could
        # still land on delivery_time or sondesh_dispatch_time — both of
        # which quote the 15/30/60-minute Sondesh dispatch numbers — simply
        # because those two templates carry the broadest set of generic
        # "how long / how soon" triggers. "When will my lunch arrive" and
        # "how long does bento take" were both answered with Sondesh timing.
        # This runs before trigger scoring, so context decides the template
        # instead of whichever one happens to share the most words.
        # "How long can I store the sondesh?" also matches TIME_INTENT_RE
        # ("how long") and SONDESH_CONTEXT_WORDS ("sondesh"), which would
        # steal it from the shelf-life guard and answer a storage question
        # with a dispatch-speed one. Storage words take this out of scope
        # entirely — that question already has its own, more specific match.
        is_storage_question = re.search(
            r'\b(store|storage|refrigerat|fridge|shelf life|room temperature|unrefrigerated|last (in|outside)|spoil)\b',
            lowered_text,
        )
        if TIME_INTENT_RE.search(lowered_text) and not is_storage_question and not any(
            w in lowered_text for w in ("out of delhi", "outside delhi", "outside ncr", "other city", "other cities")
        ):
            if any(w in lowered_text for w in FOOD_CONTEXT_WORDS):
                tmpl = self._template_by_id("savoury_delivery_time")
                if tmpl and tmpl.get("response"):
                    response_text = tmpl["response"]
                    images = None
                    if intro_greeting_prefix:
                        response_text = intro_greeting_prefix + response_text
                    session_memory.add_turn(user_id, channel, user_message, response_text)
                    return response_text, True, "savoury_delivery_time", images
            elif any(w in lowered_text for w in SONDESH_CONTEXT_WORDS):
                tmpl = self._template_by_id("sondesh_dispatch_time")
                if tmpl and tmpl.get("response"):
                    response_text = tmpl["response"]
                    if intro_greeting_prefix:
                        response_text = intro_greeting_prefix + response_text
                    session_memory.add_turn(user_id, channel, user_message, response_text)
                    return response_text, True, "sondesh_dispatch_time", None

        # The Kolkata reassurance question arrives in far more shapes than a
        # trigger list can enumerate — "is gurgaon kitchen closing", "will
        # the recipes change after you move", "who runs the kitchen after
        # you leave" — and several of those share words with unrelated
        # guards: "kitchen" with the pickup-address guard, "gurgaon" with
        # the delivery-area answer, "founder" with the staff-contact guard.
        # Whichever of those matched on raw overlap, so a customer worried
        # about the move got an address or a name instead of the answer
        # that actually addresses moving.
        if KOLKATA_UNAMBIGUOUS_RE.search(lowered_text) or (
            KOLKATA_MOVE_VERB_RE.search(lowered_text) and KOLKATA_SUBJECT_RE.search(lowered_text)
        ):
            tmpl = self._template_by_id("kolkata_expansion_info")
            if tmpl and tmpl.get("response"):
                response_text = tmpl["response"]
                if intro_greeting_prefix:
                    response_text = intro_greeting_prefix + response_text
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, "kolkata_expansion_info", None

        if OUT_OF_AREA_GENERIC_RE.search(lowered_text):
            tmpl = self._template_by_id("delivery_out_of_area")
            if tmpl and tmpl.get("response"):
                response_text = tmpl["response"]
                if intro_greeting_prefix:
                    response_text = intro_greeting_prefix + response_text
                session_memory.add_turn(user_id, channel, user_message, response_text)
                return response_text, True, "delivery_out_of_area", None

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

        # True when something real survives stripping the greeting off the
        # front — "hey, I need sweets for an event" carries a request, while
        # "hello there" is still only a hello. Without this the greeting
        # template answers the hello and silently drops the request.
        greeting_carries_a_request = bool(
            {w for w in cleaned_words if w not in VAGUE_FILLER_WORDS} - STOP_WORDS
        )

        templates = self._templates_with_overrides()

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
            "previous_menus",
            "subscription_details", "subscription_booking_steps", "alacarte_prebooking_steps",
            "sondesh_dispatch_time", "savoury_crosssell_sondesh", "sondesh_crosssell_savoury",
            "savoury_best_time_to_book", "savoury_delivery_time", "kolkata_expansion_info",
            "order_next_steps", "sondesh_price_general", "recall_name", "name_introduction",
            "pricing_value_context", "chicken_kosha_price",
            "minimum_sondesh_quantity"
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
                pattern, trigger_words = trigger_index(trigger_lower)
                t_score = 0

                # Check 1: Exact full word boundary regex match against lowered_text OR cleaned_business_text
                if pattern.search(lowered_text) or (cleaned_business_text and pattern.search(cleaned_business_text)):
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
                    # "Who can I speak to?" is the commonest way a customer asks
                    # for a human, and none of the name-shaped anchors caught
                    # it, so the question fell through to escalation instead of
                    # being answered with the manager's number.
                    "GUARD_staff_names_manager_contact": {
                        "owner", "founder", "chef", "somdeb", "manager", "staff", "guru", "email",
                        "speak to", "talk to", "contact person", "point of contact",
                        "who do i contact", "who should i contact", "connect me",
                    }
                }
                anchors = guard_anchors.get(tmpl_id, set())
                if anchors and not any(a in lowered_text for a in anchors):
                    score = 0

            if tmpl_id == "GUARD_shelf_life_storage" and "jolbhora" in lowered_text:
                score = 0

            # Every trigger on the cross-sell templates assumes the *other*
            # product is already being discussed ("do you have sweets too",
            # "any sweets with meal"). Without that context they hijack plain
            # first-contact questions: "What sweets do you have?" was being
            # answered with "We ALSO make Bengali mishti", which reads as a
            # non-sequitur to someone who never mentioned meals.
            if tmpl_id == "savoury_crosssell_sondesh" and last_topic != "savoury":
                score = 0
            if tmpl_id == "sondesh_crosssell_savoury" and last_topic not in (
                "aamar_mishti", "signature", "sondesh_general"
            ):
                score = 0

            # Suppress greeting/name intro whenever the message carries a real
            # business inquiry, even when nothing else matched it. "Hey, I need
            # sweets for an event" is a greeting *and* a request; answering only
            # the greeting drops the request on the floor, and the customer has
            # to ask twice. With the greeting out of the way an unanswered
            # request falls through to retrieval and then to a human, which is
            # what an event enquiry needed in the first place.
            if tmpl_id in TIER_4_GREETING_INTRO and has_business_query and greeting_carries_a_request:
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
                    re.search(r'\b(bangalore|bengaluru|mumbai|kolkata|pune|hyderabad|chennai|jaipur|ahmedabad|chandigarh|lucknow)\b.*\b(deliver(y|ing)?|courier|ship(ping)?|send|order)\b', lowered_text)
                ):
                    # Above every flavour and collection boost, deliberately.
                    # "Can you deliver 2 boxes of Nolen Gur Sondesh to
                    # Bangalore?" names a flavour and a destination we cannot
                    # ship to, and the flavour table scored 150 against this
                    # answer's 50 — so the customer was quoted a price grid and
                    # never told the sondesh cannot leave Delhi NCR. Naming a
                    # product does not make an unservable address servable, and
                    # a price for an order that cannot happen is the worse
                    # failure of the two.
                    score += 200
                elif tmpl_id == "delivery_out_of_area" and any(
                    c in lowered_text for c in [
                        "bangalore", "bengaluru", "outside delhi", "outside ncr",
                        "other cities", "other city", "out of delhi", "out of ncr",
                        "out of state", "interstate",
                    ]
                ):
                    # A bare mention with no delivery ask stays a weak nudge, so
                    # "do you use Bangalore vendors" does not become a shipping
                    # refusal.
                    score += 50
                elif tmpl_id == "combo_bento" and any(b in lowered_text for b in ["bento", "combo"]):
                    score += 100
                elif tmpl_id == "packaging" and any(p in lowered_text for p in ["packaging", "package", "packeging", "box material", "eco friendly", "eco-friendly"]):
                    score += 100
                elif tmpl_id == "sondesh_nolen_gur" and re.search(r'\b(nolen gur|nolen gur sondesh)\b', lowered_text) and not any(c in lowered_text for c in ["collection 1", "collection1", "aamar"]):
                    score += 150
                elif tmpl_id == "nolen_gur_aamar" and re.search(r'\b(nolen gur)\b', lowered_text) and any(c in lowered_text for c in ["collection 1", "collection1", "aamar"]):
                    score += 150
                elif tmpl_id == "jolbhora" and re.search(r'\bjol\s?bhora\b', lowered_text):
                    # A product-specific price question must beat the broad
                    # Sondesh price template, whose generic "price" triggers
                    # otherwise hide the approved Jolbhora price table.
                    score += 150
                elif tmpl_id == "kanchagolla" and re.search(r'\b(kanchagolla|kancha golla)\b', lowered_text):
                    score += 150
                elif tmpl_id == "pack_size_250g" and re.search(r'\b(250g|250 g)\b', lowered_text) and any(w in lowered_text for w in ["how many", "pieces", "pcs", "box", "pack", "quantity"]):
                    score += 150
                elif tmpl_id == "subscription_booking_steps" and facts.get("awaiting_subscription_booking") and re.search(r'\b(how (do i|to|can i) (order|book)|how to get started|book|booking steps|next steps|how do i start)\b', lowered_text):
                    score += 120
                # The article was the whole difference: "how do i book the
                # subscription" missed every alternative here, so it fell to
                # subscription_details, which boosts on the bare word
                # "subscription" and re-sent the price list instead of the
                # steps the customer had just asked for.
                elif tmpl_id == "subscription_booking_steps" and re.search(r'\b(how to book (the )?subscription|book (the )?subscription|start (the )?subscription|subscribe|how do i book (the )?subscription|booking steps for (the )?subscription|next steps for (the )?subscription|supscription)\b', lowered_text):
                    score += 100
                elif tmpl_id == "subscription_details" and re.search(r'\b(subscription|subscriptions|meal plan|everyday meal|meal subscription|5 meals package|5 meals)\b', lowered_text):
                    score += 80
                # "this week's menu" shares most of its words with "last
                # week's menu", so trigger overlap alone would hand a
                # request for the CURRENT menu to the archive. A present-tense
                # marker rules previous_menus out before anything else scores it.
                elif tmpl_id == "previous_menus" and re.search(r"\b(this\s+week|current|today|tonight|now|latest|new)\b", lowered_text):
                    score -= 500
                elif tmpl_id == "previous_menus" and re.search(r"\b(previous|last\s+week'?s?|old|past|earlier|archived?)\b", lowered_text) and re.search(r"\bmenus?\b", lowered_text):
                    score += 160
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
                # "menu for today" must resolve to the menu, not delivery_time.
                # Its trigger "get it today" leaves "today" as the only
                # substantive word, which the matcher's short-message rule
                # happily scores — and the Tier-2 multiplier outranks the
                # Tier-3 menu template on "today" alone.
                elif tmpl_id == "know_more" and re.search(r'\bmenu\b', lowered_text) and re.search(r"\b(today|today's|todays)\b", lowered_text):
                    score += 200

            # ``weekly menu`` is an explicit subject even when wrapped in a
            # generic detail request.  It is not duplicated in every menu
            # template's trigger list, so seed a deterministic score here to
            # keep it ahead of the generic ``know_more`` fallback.
            if tmpl_id == "savoury_food_intro" and re.search(
                r"\b(?:weekly|this\s+week(?:'s)?|current|today(?:'s)?)\s+menu\b",
                lowered_text,
            ):
                score = max(score, 140)

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
                "MATCH_SCORE message_hash=%s best=%s score=%s runner_up=%s",
                _safe_message_hash(user_message), best_template.get("id"), best_score,
                runner_up_template.get("id") if runner_up_template else None,
            )

        # Compound-intent tripwire. One message can ask two different things
        # ("menu for today and do you deliver to sector 90") and only the
        # winning template gets answered today. A full multi-intent resolver
        # is not justified yet — 925 logged messages contained zero cross-topic
        # compounds — so gather the evidence first: log whenever a
        # compound-patterned message left a meaningful runner-up behind. If
        # real traffic starts filling the logs, build the clause-splitting
        # resolver; if the log stays quiet, the complexity stays unbuilt.
        if (
            runner_up_template is not None
            and runner_up_composite >= 400
            and best_template is not None
            and runner_up_template.get("id") != best_template.get("id")
            and re.search(r'\b(and|also)\b|\?.*\?', lowered_text, re.IGNORECASE)
        ):
            log.info(
                "COMPOUND_INTENT_CANDIDATE message_hash=%s answered=%s runner_up=%s (score %s)",
                _safe_message_hash(user_message),
                best_template.get("id"),
                runner_up_template.get("id"),
                runner_up_composite,
            )

        # NLP fallback for wording the deterministic trigger list cannot
        # confidently recognize. It returns an approved template only, so menu
        # prices, policies and copy remain the same source of truth. Explicit
        # guards and escalation rules are intentionally excluded above.
        if (
            (not best_template or best_score < 4)
            and not facts.get("structured_nlu_provider_failed")
        ):
            semantic_template = self._semantic_fallback_template(
                user_message, templates, user_id, channel,
            )
            if semantic_template is not None:
                best_template = semantic_template
                best_score = 4
                facts["last_route_source"] = "semantic_router"

        # An approved answer may only be sent when it is actually about what
        # the customer asked. Word similarity is evidence of a typo, never of
        # relevance: "parking" scored against the "packing" trigger, and the
        # same shape would have answered a washroom, pet or wheelchair
        # question with packaging, delivery or storage copy. This is generic —
        # it needs no list of the topics we do not cover.
        if best_template and not self._answer_addresses_the_question(user_message, best_template):
            log.info(
                "RELEVANCE_REJECT message_hash=%s template=%s score=%s",
                _safe_message_hash(user_message), best_template.get("id"), best_score,
            )
            best_template = None
            best_score = 0

        if best_template and best_score >= 4:
            tmpl_id = best_template.get("id", "")
            facts = session_memory.get_facts(user_id, channel)
            if not facts.get("last_route_source"):
                facts["last_route_source"] = "legacy_trigger"
            if tmpl_id == "greeting":
                responses = best_template.get("responses", [])
                # Instagram's Response Library preview and live customer
                # reply must resolve to the same approved copy.  Keep the
                # long-standing WhatsApp/unit-test greeting rotation, but use
                # the canonical first response for Instagram.  An active
                # Response Library override already materializes as a
                # one-item response bank and therefore wins on both channels.
                if channel == "instagram" and responses:
                    raw_response = responses[0]
                    facts["last_greeting_index"] = 0
                else:
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
                facts["subscription_context"] = True
            elif tmpl_id == "subscription_booking_steps":
                facts["awaiting_subscription_booking"] = False
                facts["subscription_context"] = True
            elif tmpl_id == "order_next_steps":
                session_memory.clear_order_draft(user_id, channel)

            # First-time customer welcoming line for savoury food intro
            if tmpl_id == "savoury_food_intro" and not session_memory.has_prior_history(user_id, channel):
                first_time_nudge = "\n\nIf this is your first Camellia meal and the menu feels unfamiliar, I'll be happy to help you choose!"
                response_text = response_text.rstrip() + first_time_nudge

            response_text, image_url = self._apply_crosssell_nudges(
                tmpl_id, response_text, image_url, facts, user_message,
            )

            if tmpl_id not in {"greeting", "sondesh_price_detailed", "previous_menus"} and config.is_ai_enabled and ai_responder.get_active_provider() != "none":
                try:
                    user_ctx = f"Customer Name: {facts.get('name', 'Unknown')}"
                    # The turns so far, without this message — add_turn runs
                    # below, after the reply is built. Refinement needs them:
                    # a bare "gurgaon" answering "Which area are you in?"
                    # re-matches delivery_area, and a refiner that cannot see
                    # the question it is answering just restates the template
                    # and asks again.
                    history_turns = session_memory.get_session(user_id, channel).get("turns", [])
                    refined_text, is_refined = ai_responder.refine_template_response(tmpl_id, response_text, user_message, user_context=user_ctx, history_turns=history_turns)
                    if is_refined and refined_text:
                        response_text = refined_text
                except Exception as e:
                    log.warning("AI template refinement failed: %s", e)

            # Refinement is allowed to rephrase the approved primary answer,
            # but it must not discard the client-requested add-on pitch that
            # was attached to it above.
            if (
                facts.get("alacarte_sondesh_addon_sent")
                and ALACARTE_SONDESH_ADDON_COPY not in response_text
            ):
                response_text = response_text.rstrip() + "\n\n" + ALACARTE_SONDESH_ADDON_COPY

            # Appended AFTER refinement on purpose. The refinement cache is
            # keyed by the customer's wording, so a suffix added before it
            # gets baked into that cache and replayed on a later ask whose
            # archive is empty - offering menus that are no longer there.
            #
            # A customer who just got this week's menu is the one most likely
            # to want the previous one ("what was that dish last week?"), so
            # the offer goes here rather than sitting undiscoverable behind a
            # phrase nobody guesses. Only when an archive actually exists, and
            # once per session — the same restraint as the cross-sell nudges.
            # previous_menus is excluded from refinement above: its text is
            # generated from the live archive, not template copy, so refining
            # it rewords real state — and the refinement cache is keyed by the
            # customer's words, so the same question later replays an earlier
            # answer with the photo cards stripped off.
            menu_templates = {
                "know_more", "product_overview", "weekly_menu_veg", "weekly_menu_nonveg",
            }
            if tmpl_id in menu_templates and not facts.get("previous_menus_offered"):
                try:
                    if weekly_flyer.list_archive():
                        facts["previous_menus_offered"] = True
                        response_text = response_text.rstrip() + (
                            "\n\n_Want an earlier week? Say *Previous menus* — "
                            "we keep the last two._"
                        )
                except Exception as e:
                    # Never let an archive read cost the customer their menu.
                    log.warning("Could not check flyer archive for nudge: %s", e)


            if intro_greeting_prefix and not response_text.startswith("Nice to meet you"):
                if response_text.startswith("Hi! "):
                    response_text = response_text[4:]
                elif response_text.startswith("Hello! "):
                    response_text = response_text[7:]
                response_text = intro_greeting_prefix + response_text

            if tmpl_id == "delivery_area":
                facts["awaiting_area_confirmation"] = "Which area are you in?" in response_text

            if tmpl_id in TOPIC_MAP:
                facts["last_topic"] = TOPIC_MAP[tmpl_id]
            if tmpl_id in {"jolbhora", "kanchagolla"}:
                facts["active_product_context"] = tmpl_id

            # Last line of defence against sending the customer the identical
            # reply twice. Reached only when refinement did not already vary the
            # wording — because the AI layer is paused, or because it returned
            # the template unchanged. The images go too: re-sending the same
            # menu cards under a "shared above" note would contradict it.
            # Templates carrying a rotating `responses` bank (greeting) vary
            # themselves from copy the client approved; substituting the notice
            # would take a reply out of that bank.
            recent_replies = _recent_bot_replies(user_id, channel)
            if (
                not self._has_response_bank(tmpl_id)
                and _looks_like_a_short_answer(user_message)
                and _bot_asked_a_question(recent_replies[0] if recent_replies else "")
                and _normalise_reply(response_text) in {_normalise_reply(r) for r in recent_replies}
            ):
                log.info("Suppressed a duplicate %s reply for %s", tmpl_id, channel)
                response_text = _DUPLICATE_REPLY_NOTICE
                image_url = None

            response_text = _strip_flower_emoji(response_text)
            image_url = _strip_flower_emoji(image_url)
            session_memory.add_turn(user_id, channel, user_message, response_text)
            return response_text, True, tmpl_id, image_url

        # Canonical resolution has had first refusal. Only now consult the
        # ACTIVE learned-knowledge store for a genuine uncovered question.
        # The database accessor enforces the ACTIVE-only customer-serving
        # boundary; ARCHIVED and DISABLED rows never reach this path.
        try:
            learned_match = db.search_learned_knowledge(user_message)
            learned_answer = (learned_match or {}).get("verified_answer", "").strip()
            if learned_answer:
                if intro_greeting_prefix:
                    learned_answer = intro_greeting_prefix + learned_answer
                session_memory.add_turn(user_id, channel, user_message, learned_answer)
                return learned_answer, True, f"learned_{learned_match.get('id')}", None
        except Exception:
            pass

        # Check the active AI provider (Groq RAG) when enabled
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

        # Groq answers from the approved excerpts retrieval just selected — the
        # understanding layer for the thousands of phrasings no trigger list can
        # enumerate. It is not free rein: it only runs when retrieval actually
        # found approved material, it is shown nothing but that material, and
        # anything it writes that states a number the sources did not state is
        # thrown away in favour of a human. Every answer is still logged as a
        # candidate so staff can promote the good ones into approved templates.
        # A complaint, a bulk/event enquiry or a live order intent is not a
        # comprehension problem — those already have their own handover copy and
        # genuinely need a person, so the model does not get to answer them with
        # whatever generic passage retrieval happened to surface.
        # Retrieval scoring says how well a passage matched, not whether it is
        # about the question at all: an outdoor-seating question can retrieve
        # packaging copy with a confident score. So the excerpts must address
        # the subject the customer raised, and no intent may be answered here
        # that the canonical layer already knows it cannot support.
        retrieval_is_relevant = bool(
            rag_res is not None
            and rag_res.source_chunks
            and self._answer_addresses_the_question(
                user_message, {"id": "rag", "response": "\n".join(rag_res.source_chunks)},
            )
        )
        unsupported_intents = [
            intent for intent in (facts.get("last_intents") or [])
            if intent in HUMAN_ONLY_INTENTS and intent != Intent.UNKNOWN.value
        ]
        if not retrieval_is_relevant and rag_res is not None and rag_res.source_chunks:
            log.info(
                "RAG_RELEVANCE_REJECT message_hash=%s sources=%s",
                _safe_message_hash(user_message), rag_res.retrieved_sources,
            )
        if (
            config.is_ai_enabled
            and ai_responder.get_active_provider() != "none"
            and _intent == "unknown"
            and rag_res is not None
            and rag_res.source_chunks
            and rag_res.confidence_score >= MEDIUM_CONFIDENCE_THRESHOLD
            and not rag_res.has_conflict
            and retrieval_is_relevant
            and not unsupported_intents
        ):
            try:
                user_ctx = f"Customer Name: {facts.get('name', 'Unknown')}"
                history = session_memory.get_session(user_id, channel).get("turns", [])
                ai_resp, ok, reason = ai_responder.answer_from_retrieved_context(
                    user_message,
                    rag_res.source_chunks,
                    user_context=user_ctx,
                    history_turns=history,
                )
                if ok and ai_resp:
                    grounded, grounding_reason = rag_fallback_engine.grounding_report(
                        ai_resp, rag_res.source_chunks,
                    )
                    facts["last_grounding"] = "pass" if grounded else f"rejected:{grounding_reason}"
                    if grounded:
                        try:
                            db.save_candidate_template(
                                name=f"groq_grounded_{rag_res.proposed_intent}",
                                response=ai_resp,
                                user_message=user_message,
                                source_refs=rag_res.source_refs or ["ai_responder"],
                                confidence=rag_res.confidence_score,
                                created_by="ai_responder",
                            )
                        except Exception as ce:
                            log.warning("Could not save the grounded Groq candidate template: %s", ce)
                        if intro_greeting_prefix:
                            ai_resp = intro_greeting_prefix + ai_resp
                        db.log_rag_audit(
                            user_message=user_message,
                            matched_template=None,
                            rag_invoked=True,
                            retrieved_sources=rag_res.retrieved_sources,
                            confidence_score=rag_res.confidence_score,
                            proposed_template=rag_res.proposed_intent,
                            proposed_response=ai_resp,
                            approval_status="GROQ_GROUNDED",
                            final_customer_response=ai_resp,
                        )
                        session_memory.add_turn(user_id, channel, user_message, ai_resp)
                        return ai_resp, True, "groq_grounded_answer", None
                    log.warning(
                        "Discarded a Groq answer for message_hash=%s: it stated a number "
                        "the approved sources did not. Escalating instead.",
                        _safe_message_hash(user_message),
                    )
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
                "Happy to help — I just want to point you the right way\n\n"
                "Which of these is closest to what you're after?\n"
                "• Sondesh flavours & prices\n"
                "• 🍱 This week's Bengali meals\n"
                "• 🚚 Delivery to your area\n"
                "• 🛍️ Placing an order\n\n"
                "Or tell me in a few more words and I'll take it from there."
            )
            if intro_greeting_prefix:
                clarifier = intro_greeting_prefix + clarifier
            log.info(
                "Asked user_hash=%s to clarify a vague message instead of escalating.",
                _safe_user_hash(user_id),
            )
            session_memory.add_turn(user_id, channel, user_message, clarifier)
            return clarifier, True, "clarify_request", None

        session_memory.add_turn(user_id, channel, user_message, fallback)
        try:
            db.record_knowledge_gap(
                user_message=user_message,
                intents=facts.get("last_intents") or [_intent or "unknown"],
                entities=facts.get("last_entities") or {},
                reason="unsupported_or_insufficient_knowledge",
            )
        except Exception as exc:
            log.warning("Could not record unmatched knowledge gap: %s", exc)
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

    def _template_by_id(self, template_id: str) -> Dict[str, Any]:
        """Return protected template logic plus an optional managed copy override."""
        template = next(
            (t for t in self.qa_data.get("templates", []) if t.get("id") == template_id),
            {},
        )
        if not template:
            return {}
        try:
            override = db.get_response_template_override(template_id)
        except Exception as exc:
            log.warning("Could not load response-library override for %s: %s", template_id, exc)
            override = None
        if not override or str(override.get("status") or "ACTIVE").upper() != "ACTIVE":
            return template
        if (
            template_id == "subscription_details"
            and is_known_legacy_subscription_response(override.get("response"))
        ):
            # A known stale durable object must not win over the immutable
            # repository subscription copy while startup remediation catches
            # up. Distinct operator edits continue through the normal
            # Response Library path unchanged.
            canonical = dict(template)
            canonical_response = self._canonical_template_response(template_id)
            if canonical.get("responses"):
                canonical["responses"] = [canonical_response]
            else:
                canonical["response"] = canonical_response
            log.warning(
                "SUBSCRIPTION_STALE_OVERRIDE_BYPASS template_id=%s sha256=%s",
                template_id,
                subscription_response_fingerprint(override.get("response")),
            )
            return canonical
        result = dict(template)
        if result.get("responses"):
            result["responses"] = [override.get("response") or ""]
        else:
            result["response"] = override.get("response") or ""
        return result

    def _canonical_template_response(self, template_id: str) -> str:
        """Return the immutable repository seed copy, excluding durable edits."""
        template = next(
            (t for t in self._canonical_qa_data.get("templates", []) if t.get("id") == template_id),
            {},
        )
        return str(template.get("response") or ((template.get("responses") or [""])[0]))

    def _templates_with_overrides(self) -> List[Dict[str, Any]]:
        """Materialize runtime templates with managed copy overrides applied."""
        try:
            overrides = db.list_response_template_overrides()
        except Exception as exc:
            log.warning("Could not load response-library overrides: %s", exc)
            overrides = {}
        result = []
        for template in self.qa_data.get("templates", []):
            template_id = template.get("id")
            if not template_id:
                continue
            override = overrides.get(str(template_id))
            if not override or str(override.get("status") or "ACTIVE").upper() != "ACTIVE":
                result.append(template)
                continue
            copy = dict(template)
            if copy.get("responses"):
                copy["responses"] = [override.get("response") or ""]
            else:
                copy["response"] = override.get("response") or ""
            result.append(copy)
        return result

    def _with_captions(self, template_id: str, images: List[str]) -> List[Any]:
        """
        Pairs each card with its own caption from the template's `image_captions`,
        positionally. Any card without a caption stays a plain URL, so a template
        that defines fewer captions than cards still sends every card.
        """
        tmpl = self._template_by_id(template_id)
        captions = tmpl.get("image_captions") or []
        paired: List[Any] = []
        for index, url in enumerate(images):
            caption = captions[index] if index < len(captions) else None
            paired.append({"url": url, "caption": caption} if caption else url)
        return paired

    @staticmethod
    def _append_sondesh_crosssell_images(image_url: Any) -> List[Any]:
        """Keeps the savoury answer first, then sends the three approved photos."""
        supplied = [
            "/images/sondesh_addon_01.jpeg",
            "/images/sondesh_addon_02.jpeg",
            "/images/sondesh_addon_03.jpeg",
        ]
        existing = list(image_url) if isinstance(image_url, list) else ([image_url] if image_url else [])
        images = existing + supplied

        # Instagram, WhatsApp and /chat all understand this marker. It ensures
        # the customer's savoury answer and the cross-sell copy finish before
        # the photos arrive, even when the savoury template also has a menu card.
        first = images[0]
        if isinstance(first, dict):
            images[0] = {**first, "text_first": True}
        else:
            images[0] = {"url": first, "text_first": True}
        return images

    def _current_week(self) -> Dict[str, Any]:
        """
        The live weekly menu block — always the last entry, matching every
        write path (photo upload, pasted text, Sheet sync, the dashboard).
        """
        weekly_menus = self.menu_data.get("weekly_menus", [])
        return weekly_menus[-1] if weekly_menus else {}

    def _weekly_menu_expired(self) -> bool:
        """
        True once the printed date range on the live menu has passed.

        Nothing dated has been uploaded yet on a fresh deployment, and
        undated text never expires, so this stays False until a real flyer
        with a readable date range goes stale.
        """
        current_week = self._current_week()
        return (
            not current_week.get("is_published", True)
            or menu_dates.is_expired(current_week.get("week_of"))
        )

    def _current_weekly_image(self, today_weekday: int) -> Tuple[Optional[str], Optional[str]]:
        """
        The weekly menu card to send, and a caption override when it is the
        operator's own upload rather than one of the two fixed images shipped
        with the repo.

        An uploaded flyer takes priority over the day-of-week Till Thursday /
        From Friday rotation, because it is more current by definition — the
        operator posted it precisely to reflect what changed. The rotation is
        the fallback for a deployment where nothing has been uploaded yet.

        Returns no card at all once the menu's printed dates have passed. The
        static fallback is not a substitute there: it is a picture of a menu
        too, and sending any dish photo for a week that has ended is the exact
        thing expiry exists to prevent.
        """
        # An uploaded flyer is judged on its own printed date (or, when that
        # could not be read, on when it was uploaded). menu.json's week no
        # longer gets a veto here: since the photo is saved even when text
        # extraction fails, the two can legitimately disagree, and letting a
        # stale text date suppress this week's photo is exactly the "upload
        # did nothing" failure the split exists to prevent.
        # A deliberately published Menu Manager image wins immediately and is
        # read from durable metadata on every request; no process restart is
        # needed. An explicitly archived image returns no card.
        managed = db.get_menu_image_current("weekly_vegetarian")
        if managed:
            return managed["image_path"], None
        if db.has_menu_image_archive("weekly_vegetarian"):
            return None, None

        uploaded = weekly_flyer.get()
        if uploaded:
            if not weekly_flyer.is_current(uploaded):
                return None, None
            return uploaded["url"], uploaded.get("caption")
        if self._weekly_menu_expired():
            return None, None
        img_till_thursday = "/images/Till%20Thursday.jpeg"
        img_from_friday = "/images/From%20Friday.jpeg"
        fallback = img_from_friday if today_weekday in [4, 5, 6] else img_till_thursday
        return fallback, None

    def _detect_relevant_images(self, user_message: str) -> Optional[Any]:
        """Attaches relevant menu photo cards when user inquires about products or menus in AI-First mode."""
        lowered = user_message.lower().strip()
        img_signature_sondesh = current_menu_image("signature", "/images/Signature%20Sondesh.jpeg")
        img_aamar_mishti = current_menu_image("aamar_mishti", "/images/AAMAR%20MISHTI.jpeg")

        import datetime, zoneinfo
        try:
            ist = zoneinfo.ZoneInfo("Asia/Kolkata")
        except Exception:
            ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        today_weekday = datetime.datetime.now(tz=ist).weekday()
        current_weekly_img, _ = self._current_weekly_image(today_weekday)

        if any(w in lowered for w in ["collection 1", "collection1", "1st collection", "first collection", "aamar mishti", "col 1", "option 1"]):
            return [img_aamar_mishti]
        elif any(w in lowered for w in ["collection 2", "collection2", "2nd collection", "second collection", "signature", "desi a2", "col 2", "option 2", "mahua"]):
            return [img_signature_sondesh]
        elif any(w in lowered for w in ["product", "all products", "what do you have", "what do you sell", "overview", "know more", "full menu", "both collections", "difference", "custom", "customize", "customise", "assorted", "mix"]):
            # The Sondesh cards are undated and always valid; only the weekly
            # card drops out when its week has ended.
            return [i for i in [img_aamar_mishti, img_signature_sondesh, current_weekly_img] if i]
        elif any(w in lowered for w in ["sondesh", "mishti", "sweet", "sweets", "kanchagolla", "jolbhora", "nolen gur", "250g", "500g", "1kg", "rose", "classic", "mango", "kesar", "price"]):
            return [img_aamar_mishti, img_signature_sondesh]
        elif any(w in lowered for w in ["menu", "veg", "non veg", "non-veg", "bento", "combo", "meal", "meals", "lunch", "dinner", "halal", "oil", "ghee", "savoury"]):
            return current_weekly_img
        return None

    @staticmethod
    def _collection_cards_static(aamar_url: str, signature_url: str) -> List[Any]:
        """
        The two Sondesh cards in the sequence the client asked for.

        "First the general text, then the aamar collection note, then the
        aamar collection menu, then signature note, signature menu." So the
        reply text leads, and each collection's note introduces its own card
        rather than trailing it. The notes are the client's own wording for
        the two collections.
        """
        return [
            {
                "url": aamar_url,
                "caption": (
                    "Collection 1. Aamar Mishti আমার মিষ্টি — indulgence for your sweet "
                    "cravings at home or work. Good local dairy milk, partially mechanised, "
                    "made in-house at our kitchen. Sharing the menu for Aamar Mishti below."
                ),
                "text_first": True,
                "caption_first": True,
            },
            {
                "url": signature_url,
                "caption": (
                    "Collection 2. Signature collection — mishti for special occasions, "
                    "gifting & when you are in the mood to give yourself a treat. Made using "
                    "desi A2 cow milk that has not been industrially processed. The mishti is "
                    "completely hand crafted end to end. Sharing the menu for signature "
                    "collection below."
                ),
                "caption_first": True,
            },
        ]

    def render_approved_template(self, template_id: str, channel: str = "instagram") -> Tuple[str, Optional[Any]]:
        """Render the same approved copy used by a live deterministic reply.

        This is intentionally side-effect free for admin Preview/Dry-run.  It
        resolves the managed Response Library override on every call (so an
        edit is visible without a process restart) and selects the canonical
        Instagram greeting variant used by ``match_and_respond``.
        """
        template = self._template_by_id(template_id)
        if not template:
            return "", None
        responses = template.get("responses") or []
        response = (responses[0] if responses else template.get("response")) or ""
        rendered, images = self._format_dynamic_response(
            template_id, response, user_id="__response_library_preview__",
            channel=channel, user_message=(template.get("triggers") or [""])[0],
        )
        if runtime_quarantine_enabled(getattr(db, "db_path", "")):
            rendered = quarantine_template_response(
                template_id,
                _strip_flower_emoji(rendered),
                canonical_text=self._canonical_template_response(template_id),
            )
        else:
            rendered = _strip_flower_emoji(rendered)
        return rendered, _quarantine_image_claims(images)

    def _format_dynamic_response(
        self, template_id: str, default_response: str, user_id: str = None, channel: str = None, user_message: str = ""
    ) -> Tuple[str, Optional[Any]]:
        """Attaches the exact photo card(s) a template should send alongside its text whenever requested."""
        img_signature_sondesh = current_menu_image("signature", "/images/Signature%20Sondesh.jpeg")
        img_aamar_mishti = current_menu_image("aamar_mishti", "/images/AAMAR%20MISHTI.jpeg")

        import datetime, zoneinfo
        try:
            ist = zoneinfo.ZoneInfo("Asia/Kolkata")
        except Exception:
            # zoneinfo available Python 3.9+; fallback to fixed +5:30 offset
            ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        today_weekday = datetime.datetime.now(tz=ist).weekday()  # 0=Mon … 6=Sun
        current_weekly_img, weekly_caption_override = self._current_weekly_image(today_weekday)

        target_images = None
        formatted_text = default_response
        lowered_msg = (user_message or "").lower().strip()

        # "I want to know more" & Product Overview -> both collection cards + this week's meal menu.
        # Each card carries its own description as a caption so the customer sees
        # card, detail, card, detail — rather than one block of text followed by a
        # burst of photos they then have to match up themselves.
        if template_id == "previous_menus":
            archived = weekly_flyer.list_archive()
            if not archived:
                # Never imply an archive exists when it does not — a customer
                # told "here are the past menus" and shown nothing assumes the
                # bot is broken. Point at the live menu instead.
                return (
                    "I don't have any earlier menus on hand right now — we keep the "
                    "last two weeks. Say *Menu* and I'll send this week's! \U0001F338",
                    None,
                )
            cards = []
            for entry in archived:
                week = entry.get("week_of")
                cards.append({
                    "url": entry["url"],
                    "caption": (f"*Menu — {week}*" if week else "*An earlier menu*"),
                })
            plural = "menu" if len(cards) == 1 else "menus"
            return (
                f"Here {'is' if len(cards) == 1 else 'are'} the {plural} from the past "
                f"two weeks. \U0001F338\n\nThese weeks have finished — say *Menu* for "
                f"what we're cooking now.",
                cards,
            )

        # The subscription source requires the supplied Everyday Meal rate
        # chart itself.  Keep this asset distinct from the Aamar, Signature,
        # and weekly menu cards; the response text must not recreate its table.
        elif template_id == "subscription_details":
            target_images = [
                "/images/Everyday%20Meal%20Subscription%20Rate%20Chart.jpeg"
            ]

        if template_id in ["know_more", "product_overview"]:
            # The two Sondesh cards are undated and stay; only the weekly card
            # drops out once its week has ended, so a customer browsing still
            # sees the sweets rather than nothing at all.
            # Same sequence the client asked for on the price flow: the reply
            # text, then each collection's note introducing its own menu card.
            # The weekly meals card keeps its own caption from the template.
            target_images = self._collection_cards_static(img_aamar_mishti, img_signature_sondesh)
            if current_weekly_img:
                weekly_caption = (self._with_captions(template_id, [None, None, current_weekly_img]) or [None, None, None])[2]
                target_images.append(
                    weekly_caption if isinstance(weekly_caption, dict)
                    else {"url": current_weekly_img, "caption": None}
                )
            # An uploaded flyer replaces the qa_templates.json caption for just
            # this one card. That caption ("fresh lunch & dinner across Delhi
            # NCR...") describes the fixed fallback image; once a real photo is
            # in play the caption should say so, in the same no-prices,
            # no-dish-names style rather than repeating what the fallback said.
            if weekly_caption_override:
                target_images[-1] = {"url": current_weekly_img, "caption": weekly_caption_override}

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
                    formatted_text = "*Collection 1 — Aamar Mishti (Rose Sondesh)*\nFresh artisanal chhana infused with fragrant organic rose petals, made with pure dairy milk in our kitchen:\n\n```\n+-----------------------+-------+-------+-------+\n| Pack Size             | 250g  | 500g  |  1kg  |\n+-----------------------+-------+-------+-------+\n| Piece Count           | 9 pcs | 18 pcs| 36 pcs|\n| Price (INR)           | ₹450  | ₹800  | ₹1500 |\n+-----------------------+-------+-------+-------+\n```\n\nWhich pack size (250g, 500g, or 1kg) would you like to order?"
                elif has_classic and not has_rose:
                    formatted_text = "*Collection 1 — Aamar Mishti (Classic Sondesh)*\nFresh artisanal chhana and delicate aromatic cardamom, made with pure dairy milk in our kitchen:\n\n```\n+-----------------------+-------+-------+-------+\n| Pack Size             | 250g  | 500g  |  1kg  |\n+-----------------------+-------+-------+-------+\n| Piece Count           | 9 pcs | 18 pcs| 36 pcs|\n| Price (INR)           | ₹450  | ₹800  | ₹1500 |\n+-----------------------+-------+-------+-------+\n```\n\nWhich pack size (250g, 500g, or 1kg) would you like to order?"
                else:
                    formatted_text = "*Collection 1 — Aamar Mishti (Classic & Rose Sondesh)*\nMade with pure dairy milk in our kitchen:\n\n```\n+-----------------------+-------+-------+-------+\n| Item / Flavour        | 250g  | 500g  |  1kg  |\n+-----------------------+-------+-------+-------+\n| Classic / Rose        | ₹450  | ₹800  | ₹1500 |\n+-----------------------+-------+-------+-------+\n* Box count: 9 pcs (250g) | 18 pcs (500g) | 36 pcs (1kg)\n```\n\nWhich flavour and pack size would you like to order?"

            elif has_col2 and not has_col1:
                target_images = [img_signature_sondesh]
                if has_rose and not has_classic:
                    formatted_text = "*Collection 2 — Signature A2 (Rose Sondesh)*\n100% handcrafted with pure desi A2 cow milk and fragrant organic rose petals:\n\n```\n+-----------------------+-------+-------+-------+\n| Pack Size             | 250g  | 500g  |  1kg  |\n+-----------------------+-------+-------+-------+\n| Piece Count           | 9 pcs | 18 pcs| 36 pcs|\n| Price (INR)           | ₹600  | ₹1100 | ₹2000 |\n+-----------------------+-------+-------+-------+\n```\n\nWhich pack size (250g, 500g, or 1kg) would you like to order?"
                elif has_classic and not has_rose:
                    formatted_text = "*Collection 2 — Signature A2 (Classic Sondesh)*\n100% handcrafted with pure desi A2 cow milk and delicate aromatic cardamom:\n\n```\n+-----------------------+-------+-------+-------+\n| Pack Size             | 250g  | 500g  |  1kg  |\n+-----------------------+-------+-------+-------+\n| Piece Count           | 9 pcs | 18 pcs| 36 pcs|\n| Price (INR)           | ₹600  | ₹1100 | ₹2000 |\n+-----------------------+-------+-------+-------+\n```\n\nWhich pack size (250g, 500g, or 1kg) would you like to order?"
                else:
                    formatted_text = "*Collection 2 — Signature A2 (Classic & Rose Sondesh)*\n100% handcrafted with pure desi A2 cow milk:\n\n```\n+-----------------------+-------+-------+-------+\n| Item / Flavour        | 250g  | 500g  |  1kg  |\n+-----------------------+-------+-------+-------+\n| Classic / Rose        | ₹600  | ₹1100 | ₹2000 |\n+-----------------------+-------+-------+-------+\n* Box count: 9 pcs (250g) | 18 pcs (500g) | 36 pcs (1kg)\n```\n\nWhich flavour and pack size would you like to order?"

            elif has_rose and not has_classic:
                target_images = [img_aamar_mishti, img_signature_sondesh]
                formatted_text = "Our handcrafted *Rose Sondesh* is made with fresh artisanal chhana infused with fragrant organic rose petals. Available in both collections:\n\n*Rose Sondesh Price Breakdown:*\n```\n+-------------------------------------+-------+-------+-------+\n| Collection                          | 250g  | 500g  |  1kg  |\n+-------------------------------------+-------+-------+-------+\n| Collection 1 — Aamar Mishti (Dairy) | ₹450  | ₹800  | ₹1500 |\n| Collection 2 — Signature (Desi A2)  | ₹600  | ₹1100 | ₹2000 |\n+-------------------------------------+-------+-------+-------+\n* Box count: 9 pcs (250g) | 18 pcs (500g) | 36 pcs (1kg)\n```\n\n• *Collection 1 (Aamar Mishti)*: Daily indulgence, good dairy milk, partially mechanised, made in-house at our kitchen.\n• *Collection 2 (Signature A2)*: Special occasions & gifting — made with pure desi A2 cow milk (not industrially processed), completely handcrafted end to end.\n\nWhich collection and pack size would you like to order (250g, 500g, or 1kg)?"

            elif has_classic and not has_rose:
                target_images = [img_aamar_mishti, img_signature_sondesh]
                formatted_text = "Our traditional *Classic Sondesh* is made with fresh artisanal chhana and delicate aromatic cardamom. Available in both collections:\n\n*Classic Sondesh Price Breakdown:*\n```\n+-------------------------------------+-------+-------+-------+\n| Collection                          | 250g  | 500g  |  1kg  |\n+-------------------------------------+-------+-------+-------+\n| Collection 1 — Aamar Mishti (Dairy) | ₹450  | ₹800  | ₹1500 |\n| Collection 2 — Signature (Desi A2)  | ₹600  | ₹1100 | ₹2000 |\n+-------------------------------------+-------+-------+-------+\n* Box count: 9 pcs (250g) | 18 pcs (500g) | 36 pcs (1kg)\n```\n\n• *Collection 1 (Aamar Mishti)*: Daily indulgence, good dairy milk, partially mechanised, made in-house at our kitchen.\n• *Collection 2 (Signature A2)*: Special occasions & gifting — made with pure desi A2 cow milk (not industrially processed), completely handcrafted end to end.\n\nWhich collection and pack size would you like to order (250g, 500g, or 1kg)?"

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

            if template_id == "sondesh_price_detailed":
                # The client-approved price reply is text first, followed by
                # each collection note and its matching menu card.
                target_images = self._collection_cards_static(img_aamar_mishti, img_signature_sondesh)

        elif template_id in ["sondesh_details", "sondesh_unfamiliar"]:
            target_images = self._collection_cards_static(img_aamar_mishti, img_signature_sondesh)
            if current_weekly_img:
                target_images.append({"url": current_weekly_img, "caption": None})

        # Savoury food intro, Sondesh->Savoury cross-sell & Bento details — attach current weekly menu image card alongside text
        elif template_id in ["savoury_food_intro", "sondesh_crosssell_savoury", "bento_details"]:
            target_images = current_weekly_img
            formatted_text = default_response

        # Weekly menu & bento photo card queries
        elif template_id in ["weekly_menu_veg", "weekly_menu_nonveg", "combo_bento"]:
            # Once the printed week has passed, the dishes and prices below are
            # last week's. A customer would order against them, so say the menu
            # is being updated instead of sending anything stale.
            if self._weekly_menu_expired():
                return _WEEKLY_MENU_BEING_UPDATED, None

            target_images = current_weekly_img
            # The last entry is "current" everywhere else that touches this
            # array — every write path (photo upload, pasted text, Sheet sync,
            # the admin dashboard's own JS) treats weekly_menus[-1] as the live
            # week. This was the one place still picking weekly_menus[0] on
            # Monday-Thursday, so an uploaded menu's dishes and date reached
            # customers only on Fri/Sat/Sun — four days out of seven, an
            # upload silently had no effect on what this reply showed.
            current_week = self._current_week()

            week_name = current_week.get("week_of", "Current Rotation")
            service_window = current_week.get("service_window", "Friday Lunch – Thursday Dinner")

            if template_id == "weekly_menu_veg":
                veg_list = current_week.get("vegetarian", [])
                if veg_list:
                    lines = [
                        f"*Camellia Artisanal — This Week's Vegetarian Menu* 🌿",
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
                        f"*Camellia Artisanal — This Week's Non-Vegetarian Menu* 🍗",
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
                        f"*Camellia Artisanal — Bengali Bento Meals & Combos* 🍱",
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
                    lines.append("\nWhich Bento meal would you like to order?")
                    formatted_text = "\n".join(lines)
                else:
                    formatted_text = default_response

        elif template_id == "delivery_area":
            # The customer may name their area in the same breath ("do you
            # deliver to sector 90 gurgaon"). Asking "Which area are you in?"
            # right after they told us reads badly, so swap the closing
            # question for a confirmation of the area they named.
            #
            # Only on a message that actually asked: the bare follow-up
            # "gurgaon" that answers our own area question is handled by the
            # duplicate-reply guard, which deliberately asserts nothing about
            # coverage — see test_duplicate_guard_invents_no_delivery_claim.
            asked_about_delivery = re.search(
                r'\b(deliver|delivery|courier|ship|shipping)\b', lowered_msg
            )
            area_match = re.search(r'\bsector\s*\d+[a-z]?\b', lowered_msg)
            # "Do you deliver outside Delhi NCR?" names the zone, not the
            # customer's own area — they are asking precisely because they do
            # not know whether their city counts. Reading that "delhi" as their
            # location swapped out the "Which area are you in?" question, which
            # is the string that arms the area-confirmation flow. The customer
            # then answered "Gurgaon" anyway and got the whole coverage message
            # again: the repeat loop the client reported, by another route.
            asks_about_the_zone_itself = re.search(
                r'\b(outside|out of|beyond|apart from|other than|other cit|another cit|different cit)\b',
                lowered_msg,
            )
            city_match = None if asks_about_the_zone_itself else next(
                (c for c in ("gurgaon", "gurugram", "delhi", "noida", "ghaziabad", "faridabad") if c in lowered_msg),
                None,
            )
            previous_reply = _recent_bot_replies(user_id, channel, limit=1)
            is_area_followup = (
                not asked_about_delivery
                and not (user_message or "").strip().endswith("?")
                and len((user_message or "").split()) <= 8
                and not _QUESTION_OPENER_RE.match((user_message or "").strip())
                and _bot_asked_a_question(previous_reply[0] if previous_reply else "")
            )
            if is_area_followup and _looks_like_an_area_answer(user_message):
                # A short location is the answer to the bot's preceding
                # "Which area are you in?" prompt, not a new request for the
                # delivery template. Delhi University North Campus is within
                # the configured Delhi NCR service area.
                area = " ".join(user_message.split())
                formatted_text = (
                    f"Yes, we deliver to {area} — courier charges on actuals "
                    "from Sushant Lok, Gurgaon."
                )
            elif asked_about_delivery and (area_match or city_match):
                if area_match:
                    area = area_match.group(0).title()
                    if city_match:
                        area = f"{area}, {city_match.title()}"
                    confirmation = f"Yes — {area} is well within our Delhi NCR delivery zone"
                else:
                    confirmation = f"Yes — we deliver right across {city_match.title()}"
                formatted_text = default_response.replace("Which area are you in?", confirmation)

            # Menu + delivery asked in one message ("can i get a menu for
            # today and do you deliver to sector 90 gurgaon"): the matcher
            # answers one template, so attach the menu card the other half
            # asked for. Narrow by design — a general multi-intent composer
            # stays unbuilt until the COMPOUND_INTENT_CANDIDATE log shows
            # real traffic justifying it.
            if "menu" in lowered_msg:
                target_images = [current_weekly_img]
                formatted_text = formatted_text.rstrip() + (
                    "\n\nAnd here's this week's menu card — dishes change every week! 🍱"
                )

        elif template_id == "recall_order_flavour":
            session = session_memory.get_session(user_id or "default_user", channel or "default_channel")
            flv = session.get("facts", {}).get("order_draft", {}).get("flavour")
            if flv:
                formatted_text = f"Yes! We already have your selected flavour (*{flv.title()}*) saved in your active session — no need to repeat it at all! Which pack size (250g, 500g, or 1kg) and delivery address would you like to proceed with?"
            else:
                formatted_text = "I don't have a flavour saved yet. Which flavour (Classic, Rose, Nolen Gur, Mango, Kesar, Jolbhora) would you like?"

        elif template_id == "artisanal_differentiation":
            target_images = [img_aamar_mishti, img_signature_sondesh]

        if isinstance(target_images, list):
            target_images = [image for image in target_images if image]
        if target_images:
            return formatted_text, target_images

        return formatted_text, None

    def match_and_respond(
        self,
        user_message: str,
        user_id: str = None,
        channel: str = None,
        referral_source: str = None,
        referral_ad_id: str = None,
        referral_ad_title: str = None,
    ):
        """Resolve a message, applying the last-mile content quarantine.

        The raw resolver remains unchanged for routing/evidence.  This wrapper
        is the single public entry point used by Instagram, WhatsApp and the
        admin dry-run, so unaudited legacy claims cannot leak through a stale
        template, dynamic caption or generated refinement.
        """
        text, matched, template_id, image_url = self._match_and_respond_raw(
            user_message, user_id, channel, referral_source, referral_ad_id, referral_ad_title
        )
        if runtime_quarantine_enabled(getattr(db, "db_path", "")):
            addon_suffix = "\n\n" + ALACARTE_SONDESH_ADDON_COPY
            if (
                session_memory.get_facts(user_id or "default_user", channel or "default_channel").get(
                    "alacarte_sondesh_addon_sent"
                )
                and addon_suffix in (text or "")
            ):
                primary_text = (text or "").rsplit(addon_suffix, 1)[0]
                safe_primary = quarantine_template_response(
                    template_id,
                    primary_text,
                    canonical_text=self._canonical_template_response(template_id),
                )
                safe_text = safe_primary.rstrip() + addon_suffix
            else:
                safe_text = quarantine_template_response(
                    template_id,
                    text or "",
                    canonical_text=self._canonical_template_response(template_id),
                )
            safe_images = _quarantine_image_claims(image_url)
        else:
            safe_text, safe_images = text or "", image_url
        if safe_text != (text or "") or safe_images != image_url:
            log.warning(
                "CONTENT_INTEGRITY_QUARANTINE template=%s channel=%s",
                template_id, channel,
            )
        return safe_text, matched, template_id, safe_images

    def respond(
        self,
        user_message: str,
        user_id: str = None,
        channel: str = None,
        referral_source: str = None,
        referral_ad_id: str = None,
        referral_ad_title: str = None,
    ) -> BotResponse:
        """Convenience wrapper returning a structured BotResponse object with .text attribute."""
        text, was_matched, tmpl_id, image_url = self.match_and_respond(
            user_message, user_id, channel, referral_source, referral_ad_id, referral_ad_title
        )
        return BotResponse(text=text, was_matched=was_matched, template_id=tmpl_id, image_url=image_url)

responder = DeterministicResponder()
