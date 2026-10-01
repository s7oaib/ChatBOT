"""Authoritative Camellia client QA source and deterministic matcher.

The source file in ``data/client_approved_qa_source.txt`` is deliberately kept
as plain text: it is the client's approved copy, not a generated knowledge
summary.  This module parses its question blocks once, maps paraphrases to the
same canonical entry, and returns the approved answer without an LLM rewrite.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


SOURCE_PATH = Path(__file__).resolve().parents[1] / "data" / "client_approved_qa_source.txt"

HOME_TABLE_VIDEO_ANCHOR = (
    "Sharing 4 videos for you to take a look at details & also a bit of videos "
    "from the 100 Bengali home tables we did at gurgaon."
)
HOME_TABLE_REEL_URLS = (
    "https://www.instagram.com/reel/DdJhXQMT4VZ/",
    "https://www.instagram.com/reel/Dcd5bbEzbCo/",
    "https://www.instagram.com/reel/DcLZpuQzCKE/",
    "https://www.instagram.com/reel/DcQYp72zf8D/",
)


def build_home_table_reel_blocks(text: str) -> List[Dict[str, Any]]:
    """Insert the one authoritative URL set without changing authored text."""
    if HOME_TABLE_VIDEO_ANCHOR not in text:
        return [{"kind": "TEXT", "text": text}]
    end = text.index(HOME_TABLE_VIDEO_ANCHOR) + len(HOME_TABLE_VIDEO_ANCHOR)
    blocks = [{"kind": "TEXT", "text": text[:end]}]
    blocks.extend({"kind": "LINK", "url": url} for url in HOME_TABLE_REEL_URLS)
    if text[end:]:
        blocks.append({"kind": "TEXT", "text": text[end:]})
    return blocks


@dataclass(frozen=True)
class ApprovedQA:
    qa_id: str
    question: str
    answer: str
    context: str = "general"
    asset_keys: Tuple[str, ...] = ()
    blocks: Tuple["ResponseBlock", ...] = ()


@dataclass(frozen=True)
class ResponseBlock:
    kind: str
    value: str


@dataclass(frozen=True)
class ApprovedMatch:
    entry: ApprovedQA
    match_kind: str
    asset_keys: Tuple[str, ...]


# Ordered definitions matter: the source contains two identical pre-book
# headings and two context-sensitive generic questions.
_QUESTION_DEFINITIONS: Tuple[Tuple[str, str, str], ...] = (
    ("home_table_details", "tell me more about this", "home_table"),
    ("home_table_price", "Whats the price?", "home_table"),
    ("home_table_payment", "How to pay", "home_table"),
    ("general_price", "whats the price", "general"),
    ("general_details", "Can you tell me more about this", "general"),
    ("alacarte_prebook", "WHAT ARE THE NEXT STEPS TO PRE BOOK?", "alacarte"),
    ("subscription_price", "What is the price of SUBSCRIPTION - HOME FOOD AWAY FROM HOME", "subscription"),
    ("subscription_details_client", "Tell me more about SUBSCRIPTION - HOME FOOD AWAY FROM HOME", "subscription"),
    ("price_is_high", "PRICE IS HIGH", "general"),
    ("bento_details_client", "tell me about BENGALI BENTO", "bento"),
    ("bento_price_client", "what is pirce of  BENGALI BENTO", "bento"),
    ("best_time_to_book", "whats the BEST TIME TO BOOK", "general"),
    ("savoury_delivery_time", "WHATS THE DELIVERY TIME", "savoury"),
    ("chicken_kosha_price_client", "WHATS THE PRICE OF CHICKEN KOSHA", "savoury"),
    ("sondesh_delivery_time", "How much time do you take to deliver sondesh", "sondesh"),
    ("sondesh_price_client", "Whats the price of sondesh?", "sondesh"),
    ("sondesh_details_client", "Tell me more about sondesh", "sondesh"),
    ("sondesh_unfamiliar_client", "Sondesh unfamiliar (new category)", "sondesh"),
    ("delivery_out_of_area_client", "Do you deliver to xyz city? ( any city other than delhi gurgaon faridabad ghaziabad noida, kolkata)", "delivery"),
    ("order_prebook", "WHAT ARE THE NEXT STEPS TO PRE BOOK?", "ordering"),
    ("general_payment_client", "How do I pay?", "general"),
    ("minimum_sondesh_quantity_client", "Why is the Minimum order 9 pieces 250g for some flavours", "sondesh"),
)

_ORDER_READY_MARKER = "When a customer has placed order OR is ready to place order"
_STOP_MARKERS = (
    "After custoner has finalized order:",
    "When customer is asking questions that are not in this list",
    "24 HOURS AFTER SENDING ABOVE, SEND THIS",
)
_PLACEHOLDER_ASSETS = {
    "INSERT CURRENT MENU HERE": "weekly_menu",
    "INSERT 3 PICTURES OF SONDESH": "sondesh_addon_images",
    "ATTACH RATE CHART": "subscription_rate_chart",
    "INSERT AAMAR MENU HERE": "aamar_menu",
    "INSERT SIGNATURE MENU HERE": "signature_menu",
    "INSERT LATEST WEEKLY LUNCH/ DINNER MENU HERE": "weekly_menu",
    "SHARE WEEKLY MENU": "weekly_menu",
}


def _line_key(value: str) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def _normalise_question(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.casefold().replace("’", "'")
    value = re.sub(r"[^\w\s]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _clean_answer(raw_lines: Sequence[str]) -> Tuple[str, Tuple[str, ...]]:
    assets: List[str] = []
    kept: List[str] = []
    for raw_line in raw_lines:
        line = raw_line.rstrip()
        marker = line.strip()
        asset = _PLACEHOLDER_ASSETS.get(marker)
        if asset:
            assets.append(asset)
            continue
        if marker == "ADD LINK TO WHATSAPP":
            # The current architecture keeps unsupported/general support as a
            # manual handoff. Never leak an old operational instruction.
            continue
        kept.append(line)
    answer = "\n".join(kept).strip()
    # The source is spaced for human editing. Transport formatting may be
    # normalized, but sentence order and wording are not changed.
    answer = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", answer)
    return answer, tuple(dict.fromkeys(assets))


def _ordered_blocks(raw_lines: Sequence[str], qa_id: str) -> Tuple[ResponseBlock, ...]:
    """Keep approved wording and place each asset at its approved response slot."""
    lines = list(raw_lines)
    if qa_id in {"sondesh_details_client", "sondesh_price_client"}:
        # The latest client delivery instruction explicitly places Signature's
        # card before the Porter sentence. Keep that sentence verbatim.
        signature = next((i for i, line in enumerate(lines) if line.strip() == "INSERT SIGNATURE MENU HERE"), None)
        if signature is not None:
            porter = next((i for i, line in enumerate(lines[:signature]) if line.strip() == "Porter charges are on actuals."), None)
            if porter is not None:
                sentence = lines.pop(porter)
                signature -= 1
                lines.insert(signature + 1, sentence)
    blocks: List[ResponseBlock] = []
    pending: List[str] = []

    def flush() -> None:
        if pending:
            text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "\n".join(pending).strip())
            if text:
                blocks.append(ResponseBlock("TEXT", text))
            pending.clear()

    for line in lines:
        marker = line.strip()
        asset = _PLACEHOLDER_ASSETS.get(marker)
        if asset:
            flush()
            kind = "IMAGE_GROUP" if asset == "sondesh_addon_images" else "MENU" if "menu" in asset else "ASSET"
            blocks.append(ResponseBlock(kind, asset))
        elif marker != "ADD LINK TO WHATSAPP":
            pending.append(line.rstrip())
    flush()
    return tuple(blocks)


def parse_source(source_text: Optional[str] = None) -> Tuple[ApprovedQA, ...]:
    """Parse all client question blocks, preserving duplicate contexts."""
    text = SOURCE_PATH.read_text(encoding="utf-8") if source_text is None else source_text
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    definitions = [_line_key(question) for _id, question, _context in _QUESTION_DEFINITIONS]
    positions: List[Tuple[int, str, str, str]] = []
    used_occurrence: Dict[str, int] = {}
    for index, line in enumerate(lines):
        key = _line_key(line)
        if key not in definitions:
            continue
        candidates = [item for item in _QUESTION_DEFINITIONS if _line_key(item[1]) == key]
        occurrence = used_occurrence.get(key, 0)
        if occurrence >= len(candidates):
            continue
        qa_id, question, context = candidates[occurrence]
        used_occurrence[key] = occurrence + 1
        positions.append((index, qa_id, question, context))

    order_ready_index = next(
        (index for index, line in enumerate(lines) if _line_key(line) == _line_key(_ORDER_READY_MARKER)),
        None,
    )
    if order_ready_index is not None:
        positions.append((order_ready_index, "order_ready_handoff", _ORDER_READY_MARKER, "ordering"))
    positions.sort(key=lambda item: item[0])

    entries: List[ApprovedQA] = []
    for position, (start, qa_id, question, context) in enumerate(positions):
        next_heading = positions[position + 1][0] if position + 1 < len(positions) else len(lines)
        end = next_heading
        for marker in _STOP_MARKERS:
            marker_key = _line_key(marker)
            for index in range(start + 1, next_heading):
                if _line_key(lines[index]) == marker_key:
                    end = min(end, index)
                    break
        raw_answer = lines[start + 1:end]
        answer, assets = _clean_answer(raw_answer)
        if qa_id in {"bento_details_client", "bento_price_client"} and re.search(
            r"sharing our full menu for the week below", answer, re.I,
        ):
            assets = tuple(dict.fromkeys((*assets, "weekly_menu")))
        entries.append(ApprovedQA(qa_id, question, answer, context, assets, _ordered_blocks(raw_answer, qa_id)))
    return tuple(entries)


REGISTRY: Tuple[ApprovedQA, ...] = parse_source()
REGISTRY_BY_ID: Dict[str, ApprovedQA] = {entry.qa_id: entry for entry in REGISTRY}


def _has_home_table_context(facts: Dict[str, Any]) -> bool:
    # An explicit current Home Table answer supersedes an older source route,
    # but remains the active subject for the customer's next short question.
    if (facts.get("home_table_context")
            and str(facts.get("last_topic") or "").casefold() == "bengali_home_table"):
        return True
    if facts.get("source_context_superseded"):
        return False
    if facts.get("home_table_context"):
        return True
    if str(facts.get("last_topic") or "").casefold() == "bengali_home_table":
        return True
    route = facts.get("source_route")
    if isinstance(route, dict):
        topic = " ".join(str(route.get(key) or "") for key in ("routing_topic", "topic_name", "name"))
        return bool(re.search(r"bengali\s+home\s+table|home\s+table|kolkata\s+home", topic, re.I))
    return False


def _home_context(message: str, facts: Dict[str, Any]) -> bool:
    return _has_home_table_context(facts) or bool(
        re.search(r"\b(?:bengali|kolkata)\s+home\s+(?:table|lunch)|home\s+table\b", message, re.I)
    )


def _generic_price(message: str) -> bool:
    if re.fullmatch(r"(?:how much|price|cost|rate)[?!., ]*", message.strip(), re.I):
        return False
    if re.search(r"\b(?:this|that|it)\b", message, re.I):
        return False
    return bool(re.search(r"\b(?:how much|price|cost|rate)\b", message, re.I)) and not bool(
        re.search(r"\b(?:sondesh|bento|chicken|kosha|subscription|home\s+table|meal\s+plan|mutton|mangsho|jolbhora|kanchagolla|mishti|mithai|aamar|signature|nolen\s+gur)\b", message, re.I)
    )


def _find(entry_id: str) -> Optional[ApprovedQA]:
    return REGISTRY_BY_ID.get(entry_id)


def _match_existing(message: str, facts: Optional[Dict[str, Any]] = None) -> Optional[ApprovedMatch]:
    """Return only a canonical source entry; never generate approved copy."""
    text = " ".join(str(message or "").split()).strip()
    normalized = _normalise_question(text)
    state = facts or {}
    home = _home_context(text, state)
    # A named product in the current message is a new subject, even when a
    # previous Home Table exchange left that context in session memory. Keep
    # an explicit Home Table phrase authoritative when it is present in the
    # current message itself.
    if (
        re.search(r"\b(?:jolbhora|kanchagolla|sondesh|mishti|mithai|nolen\s+gur|aamar|signature|bento|subscription|meal\s+plan|weekly\s+savoury|chicken\s+kosha)\b", text, re.I)
        and not re.search(r"\b(?:bengali|kolkata)\s+home\s+(?:table|lunch)|home\s+table\b", text, re.I)
    ):
        home = False

    def result(entry_id: str, kind: str = "paraphrase") -> Optional[ApprovedMatch]:
        entry = _find(entry_id)
        return ApprovedMatch(entry, kind, entry.asset_keys) if entry else None

    # Explicit subject intent outranks stale state and generic old triggers.
    if re.search(r"\b(?:bengali\s+home\s+table|home\s+table|kolkata\s+home\s+lunch)\b", text, re.I):
        home = True
    if re.search(r"\b(?:price\s+(?:is\s+)?high|high\s+price|too\s+expensive|quite\s+expensive|why\s+is\s+it\s+so\s+expensive)\b", text, re.I):
        return result("price_is_high", "exact" if normalized == _normalise_question("PRICE IS HIGH") else "paraphrase")
    if home and re.search(r"\b(?:how\s+to\s+pay|payment|pay)\b", text, re.I):
        return result("home_table_payment", "exact" if normalized == _normalise_question("How to pay") else "paraphrase")
    if home and (_generic_price(text) or re.search(r"\b(?:price|cost|how\s+much)\b", text, re.I)):
        return result("home_table_price", "exact" if normalized in {_normalise_question("Whats the price?"), _normalise_question("What's the price?")} else "paraphrase")
    if home and re.search(r"\b(?:tell|talk|share|give)\s+me\s+(?:more|details)\s+(?:about\s+)?(?:this|(?:the\s+)?(?:bengali\s+)?home\s+table|the\s+event)\b|\btell\s+me\s+about\s+(?:the\s+)?(?:bengali\s+)?home\s+table\b", text, re.I):
        return result("home_table_details", "exact" if normalized == _normalise_question("tell me more about this") else "paraphrase")

    # Specific product and flow questions precede the broad generic price.
    if re.search(r"\b(?:minimum|multiples?\s+of\s+(?:nine|9)|9\s*pieces|250\s*g)\b", text, re.I) and re.search(r"\b(?:flavou?r|sondesh|order)\b", text, re.I):
        return result("minimum_sondesh_quantity_client")
    if re.search(r"\b(?:out\s+of\s+area|other\s+city|different\s+city|xyz\s+city|outside\s+(?:delhi|ncr)|deliver(?:\s+\w+){0,5}\s+to\s+(?:bangalore|bengaluru|mumbai|chennai|hyderabad|pune|kolkata))\b", text, re.I):
        return result("delivery_out_of_area_client")
    if re.search(r"\bsubscriptions?\b|\bmeal\s+plan\b|\beveryday\s+meal\b", text, re.I):
        return result("subscription_price" if re.search(r"\b(?:price|cost|rate|how\s+much)\b", text, re.I) else "subscription_details_client")
    if re.search(r"\bbengali\s+bento\b|\bbento\b", text, re.I) and not re.search(
        r"\b(?:without|no|remove|replace|substitut|customi[sz]|cheapest|lowest|how\s+much\s+more|difference|cheaper)\b",
        text,
        re.I,
    ):
        return result("bento_price_client" if re.search(r"\b(?:price|cost|how\s+much|rate|pirce)\b", text, re.I) else "bento_details_client")
    if re.search(r"\bchicken\s+kosha\b", text, re.I):
        return result("chicken_kosha_price_client")
    if re.search(r"\bsondesh\b", text, re.I):
        if re.search(r"\b(?:how\s+much\s+time|how\s+long|delivery\s+time|dispatch|deliver)\b", text, re.I):
            return result("sondesh_delivery_time")
        if re.search(r"\b(?:price|cost|how\s+much|rate|pieces|box)\b", text, re.I):
            return result("sondesh_price_client")
        if re.search(r"\b(?:unfamiliar|new\s+to|never\s+tried|what\s+is\s+this)\b", text, re.I):
            return result("sondesh_unfamiliar_client")
        if re.search(r"\b(?:tell|talk|more|details|about)\b", text, re.I):
            return result("sondesh_details_client")

    if re.search(r"\b(?:price\s+(?:is\s+)?high|high\s+price|too\s+expensive|quite\s+expensive|why\s+is\s+it\s+so\s+expensive)\b", text, re.I):
        return result("price_is_high")
    if re.search(r"\b(?:best\s+time|when)\s+to\s+(?:book|reserve)\b|\bwhen\s+is\s+the\s+best\s+time\b", text, re.I):
        return result("best_time_to_book")
    if re.search(r"\bdelivery\s+time\b|\bhow\s+long\b.*\bdeliver(?:y)?\b", text, re.I):
        return result("savoury_delivery_time")

    # The general two-line question variants are intentionally separate from
    # the Home Table variants above.
    if not home and (
        normalized in {
            _normalise_question("can you tell me more about this"),
            _normalise_question("tell me more about your food"),
            _normalise_question("what do you offer"),
        }
        or re.search(r"\b(?:share|tell|explain|describe)\b.*\b(?:food|meal|format|offer|menu)\b", text, re.I)
    ):
        return result("general_details", "exact" if normalized == _normalise_question("Can you tell me more about this") else "paraphrase")
    if not home and _generic_price(text):
        return result("general_price", "exact" if normalized == _normalise_question("whats the price") else "paraphrase")

    if re.search(r"\b(?:how\s+to\s+pay|how\s+do\s+i\s+pay|payment\s+(?:method|options?))\b", text, re.I):
        return result("home_table_payment" if home else "general_payment_client")
    if re.search(
        r"\b(?:how\s+(?:do\s+i|can\s+i|to)\s+(?:place\s+)?(?:an?\s+)?order|"
        r"how\s+can\s+i\s+place\s+(?:an?\s+)?order|"
        r"i\s+(?:want|would\s+like)\s+to\s+(?:place\s+)?(?:an?\s+)?order|"
        r"what\s+are\s+the\s+next\s+steps\s+to\s+(?:order|place\s+(?:an?\s+)?order)|"
        r"how\s+(?:do\s+i|can\s+i)\s+(?:pre\s*[- ]?book|pre\s*[- ]?order))\b",
        text,
        re.I,
    ):
        return result("order_prebook")
    if re.search(r"\b(?:pre\s*[- ]?book|pre\s*[- ]?order|how\s+to\s+order\s+(?:from|an?)\s+menu|order\s+ala\s*[- ]?carte)\b", text, re.I):
        return result(
            "order_prebook"
            if state.get("order_flow_context") or re.search(r"\bto\s+place\s+an\s+order\b", text, re.I)
            else "alacarte_prebook"
        )
    if re.search(
        r"\b(?:ready\s+to\s+(?:place|order)|placed\s+(?:my|the)\s+order|"
        r"confirm\s+(?:my|the)\s+order|finali[sz]e\s+(?:it|my\s+order)|"
        r"(?:i\s*(?:'ve|have)\s+)?finali[sz]ed\s+(?:my\s+)?order|"
        r"order\s+is\s+final|i\s*(?:'ve|have)\s+selected\s+everything)\b",
        text,
        re.I,
    ):
        return result("order_ready_handoff")
    if re.search(r"\b(?:how\s+do\s+i\s+pay|payment)\b", text, re.I):
        return result("general_payment_client")
    return None


# Matching examples only: all copy, blocks and assets still come from REGISTRY.
_PARAPHRASE_ALIASES = {
    "general_price": ("menu prices please", "what does your food cost", "what are your rates", "show me your prices"),
    "general_details": ("what food do you offer", "what are your food options", "describe your menu", "tell me about your food"),
    "general_payment_client": ("how can i pay", "payment options please", "can i pay by upi", "do you accept bank transfer"),
    "best_time_to_book": ("when should i book", "when should i reserve", "best booking time", "how early should i book"),
    "alacarte_prebook": ("how to prebook a la carte", "a la carte booking steps", "preorder from the menu", "prebook menu items"),
    "order_prebook": ("how can i order", "how do i prebook", "what details do you need to place my order", "steps to place an order"),
    "order_ready_handoff": ("i am ready to order", "my order is finalized", "i have placed my order", "i want to confirm my order"),
    "subscription_price": ("subscription price", "monthly meal subscription cost", "meal plan rates", "home food away from home price"),
    "subscription_details_client": ("tell me about meal plan", "what is the subscription", "monthly meal plan details", "home food away from home details"),
    "bento_details_client": ("tell me about bento", "what is bengali bento", "bento details please", "explain the meal box"),
    "bento_price_client": ("bento price", "how much for bento", "price of bengali bento", "meal box rate"),
    "savoury_delivery_time": ("savoury delivery time", "when will my savoury meal arrive", "how long for savoury delivery", "when do you deliver meals"),
    "chicken_kosha_price_client": ("chicken kosha price", "how much is chicken kosha", "chicken kosha rate", "cost of chicken kosha"),
    "sondesh_delivery_time": ("how long for sondesh", "when can sondesh reach", "sondesh shipping time", "when will sondesh arrive"),
    "sondesh_price_client": ("sondesh price", "sandesh price", "how much for sondesh", "mishti price please"),
    "sondesh_details_client": ("tell me about sondesh", "sondesh details please", "describe sondesh", "more information about sondesh"),
    "sondesh_unfamiliar_client": ("what is sondesh", "i have never tried sondesh", "sondesh is new to me", "i am unfamiliar with sandesh"),
    "minimum_sondesh_quantity_client": ("why minimum 9", "why do i need 9 pieces", "why minimum 250g", "why multiples of nine for sondesh"),
    "delivery_out_of_area_client": ("delivery to mumbai", "shipping to bangalore", "can you send to chennai", "do you deliver outside ncr"),
    "home_table_details": ("what is the home table", "home table details please", "tell me about the bengali home table", "explain the home table"),
    "home_table_price": ("home table price", "home table rate", "cost of bengali home table", "how much for the home table"),
    "home_table_payment": ("how can i pay for home table", "home table payment options", "home table upi payment", "how to pay for bengali home table"),
    "price_is_high": ("too pricey", "that is expensive", "your prices are high", "the cost is too high"),
}

_MATCH_TERMS = {
    "prise": "price", "pirce": "price", "prce": "price", "prices": "price",
    "rates": "rate", "costs": "cost", "sandesh": "sondesh", "mishti": "sondesh",
    "payments": "payment", "deliveries": "delivery", "pieces": "piece",
    "pls": "please", "plz": "please", "hw": "how", "mch": "much",
    "u": "you", "ur": "your", "wats": "whats",
}


def _normalise_match_text(message: str) -> str:
    # Bounded matching vocabulary, never arbitrary customer autocorrection.
    value = unicodedata.normalize("NFKC", str(message or "")).casefold()
    value = re.sub(r"['’]", "", value)
    value = _normalise_question(value)
    value = " ".join(_MATCH_TERMS.get(word, word) for word in value.split())
    value = re.sub(r"\bmeal (?:box|boxes)\b", "bento", value)
    value = re.sub(r"\bhome food away from home\b", "subscription", value)
    return re.sub(r"\bpre\s*(?:book|order)\b|\bpreorder\b", "prebook", value)


_ALIAS_IDS: Dict[str, set[str]] = {}
for _qa_id, _aliases in _PARAPHRASE_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_IDS.setdefault(_normalise_match_text(_alias), set()).add(_qa_id)


def _match_subjects(text: str) -> set[str]:
    patterns = {
        "home_table": r"\b(?:home table|kolkata home lunch|bengali home lunch)\b",
        "bento": r"\bbento\b",
        "sondesh": r"\b(?:sondesh|jolbhora|kanchagolla|aamar|signature|mithai|nolen gur)\b",
        "subscription": r"\b(?:subscriptions?|meal plan|everyday meal|home food away from home)\b",
        "chicken_kosha": r"\bchicken kosha\b",
        "savoury": r"\b(?:savoury|a la carte|alacarte)\b",
    }
    return {subject for subject, pattern in patterns.items() if re.search(pattern, text)}


def _followup_subject(state: Dict[str, Any]) -> str:
    if _has_home_table_context(state):
        return "home_table"
    topic = str(state.get("last_topic") or "").casefold()
    if topic.startswith("sondesh"):
        return "sondesh"
    return {"bento": "bento", "subscription": "subscription", "savoury": "savoury"}.get(topic, "")


def _match_base(message: str, facts: Optional[Dict[str, Any]] = None) -> Optional[ApprovedMatch]:
    """Extend existing matching with bounded aliases; return source entries only."""
    state = facts or {}
    text = _normalise_match_text(message)
    subjects = _match_subjects(text)
    if len(subjects) > 1:
        return None
    if re.search(r"\b(?:vegan|allerg\w*|gluten|shelf life|discount)\b", text):
        return None
    # A chicken price is ambiguous; a named unsupported price must not inherit
    # an old Home Table context or fall into the broad general-price pattern.
    if re.search(r"\bchicken\b", text) and "chicken_kosha" not in subjects and "bento" not in subjects:
        return None
    price = bool(re.search(r"\b(?:price|cost|rate|how much)\b", text))
    timing = bool(re.search(r"\b(?:time|how long|when|arrive|reach|dispatch)\b", text))
    objection = bool(re.search(r"\b(?:high|expensive|pricey)\b", text))
    if price and re.search(r"\b(?:paneer|fish|mutton|curry)\b", text):
        return None
    if price and not timing and not objection and not subjects:
        generic_words = set("what whats is are the a an price cost rate how much do does your you food meal menu options for please tell me can show give it this that of".split())
        generic_text = re.sub(r"\b\d+(?: \d+)?\s*(?:g|kg)\b", "", text)
        if set(generic_text.split()) - generic_words:
            return None
    subject = next(iter(subjects), "") or _followup_subject(state)
    # Only genuine short follow-ups use session context. Explicit named
    # subjects always win. Canonical generic questions retain their old route.
    short_price = bool(re.fullmatch(r"(?:how much|price|cost|rate)(?: please)?", text))
    short_details = text in {"tell me more", "more details", "tell me about it"}
    short_time = bool(re.fullmatch(r"(?:how long|how long (?:will |does )?delivery take|delivery time|when will it arrive)", text))
    context_ids = {
        "home_table": ("home_table_price", "home_table_details", None),
        "bento": ("bento_price_client", "bento_details_client", None),
        "sondesh": ("sondesh_price_client", "sondesh_details_client", "sondesh_delivery_time"),
        "subscription": ("subscription_price", "subscription_details_client", None),
        "savoury": (None, None, "savoury_delivery_time"),
    }
    if not subjects and subject in context_ids and (short_price or short_details or short_time):
        entry_id = context_ids[subject][0 if short_price else 1 if short_details else 2]
        if entry_id:
            entry = REGISTRY_BY_ID[entry_id]
            return ApprovedMatch(entry, "context", entry.asset_keys)
        return None
    if not subjects and (short_price or short_details):
        return None

    candidates = set(_ALIAS_IDS.get(text, ()))
    existing = _match_existing(text, state)
    if not existing:
        existing = _match_existing(message, state)
    if existing and (not candidates or existing.entry.qa_id in candidates):
        return existing
    # Exact canonical blocks not covered by the older intent regexes.
    for entry in REGISTRY:
        if text == _normalise_match_text(entry.question):
            return ApprovedMatch(entry, "exact", entry.asset_keys)

    if not candidates and subject:
        if subject == "home_table" and re.fullmatch(r"tell me about (?:your |the )?(?:kolkata|bengali) home lunch", text):
            candidates.add("home_table_details")
        elif subject == "sondesh" and re.search(
            r"\b(?:delivery time|shipping time|arrive|reach|how long for (?:the )?sondesh)\b", text,
        ) and not re.search(r"\b(?:invented|created|started|founded|history)\b", text):
            candidates.add("sondesh_delivery_time")
        elif subject == "sondesh" and re.search(r"\b(?:minimum|multiples?)\b", text):
            candidates.add("minimum_sondesh_quantity_client")
        elif subject == "subscription" and price:
            candidates.add("subscription_price")
    if not candidates and re.fullmatch(r"why (?:minimum |do i need )?(?:9|nine)(?: piece)?(?: please)?", text):
        candidates.add("minimum_sondesh_quantity_client")
    if not candidates and re.fullmatch(r"(?:how (?:can|do) i pay|can i pay)(?: by (?:upi|bank transfer|card))?", text):
        candidates.add("home_table_payment" if subject == "home_table" else "general_payment_client")
    if len(candidates) != 1:
        return None
    entry = REGISTRY_BY_ID[candidates.pop()]
    return ApprovedMatch(entry, "semantic_alias", entry.asset_keys)


def match(message: str, facts: Optional[Dict[str, Any]] = None) -> Optional[ApprovedMatch]:
    from camellia import approved_qa
    return approved_qa.match(message, facts)


def effective_registry():
    from camellia import approved_qa
    return approved_qa.registry()


def effective_entry(qa_id):
    from camellia import approved_qa
    return approved_qa.get(qa_id)



def resolve_effective_approved_qa_by_id(qa_id):
    """Resolve a source reference directly, failing closed on invalid lifecycle/storage."""
    from camellia import approved_qa
    try:
        entry = next((entry for entry in approved_qa.registry(approved_qa.overlays(strict=True))
                      if entry.qa_id == qa_id), None)
    except Exception:
        return None
    return ApprovedMatch(entry, "source_qa_id", entry.asset_keys) if entry else None


def source_fingerprint() -> str:
    import hashlib

    return hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest()


def registry_summary() -> Dict[str, Any]:
    return {
        "source": str(SOURCE_PATH),
        "canonical_entry_count": len(REGISTRY),
        "canonical_ids": [entry.qa_id for entry in REGISTRY],
        "source_fingerprint": source_fingerprint(),
    }
