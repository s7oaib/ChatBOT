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


@dataclass(frozen=True)
class ApprovedQA:
    qa_id: str
    question: str
    answer: str
    context: str = "general"
    asset_keys: Tuple[str, ...] = ()


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
    "SHARE WEEKLY MENU",
)
_PLACEHOLDER_ASSETS = {
    "INSERT CURRENT MENU HERE": "weekly_menu",
    "INSERT 3 PICTURES OF SONDESH": "sondesh_addon_images",
    "ATTACH RATE CHART": "subscription_rate_chart",
    "INSERT AAMAR MENU HERE": "aamar_menu",
    "INSERT SIGNATURE MENU HERE": "signature_menu",
    "INSERT LATEST WEEKLY LUNCH/ DINNER MENU HERE": "weekly_menu",
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
        entries.append(ApprovedQA(qa_id, question, answer, context, assets))
    return tuple(entries)


REGISTRY: Tuple[ApprovedQA, ...] = parse_source()
REGISTRY_BY_ID: Dict[str, ApprovedQA] = {entry.qa_id: entry for entry in REGISTRY}


def _has_home_table_context(facts: Dict[str, Any]) -> bool:
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


def match(message: str, facts: Optional[Dict[str, Any]] = None) -> Optional[ApprovedMatch]:
    """Return only a canonical source entry; never generate approved copy."""
    text = " ".join(str(message or "").split()).strip()
    normalized = _normalise_question(text)
    state = facts or {}
    home = _home_context(text, state)

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
    if re.search(r"\b(?:pre\s*[- ]?book|pre\s*[- ]?order|how\s+to\s+order\s+(?:from|an?)\s+menu|order\s+ala\s*[- ]?carte)\b", text, re.I):
        return result(
            "order_prebook"
            if state.get("order_flow_context") or re.search(r"\bto\s+place\s+an\s+order\b", text, re.I)
            else "alacarte_prebook"
        )
    if re.search(r"\b(?:ready\s+to\s+(?:place|order)|placed\s+(?:my|the)\s+order|confirm\s+(?:my|the)\s+order|finali[sz]e\s+(?:it|my\s+order))\b", text, re.I):
        return result("order_ready_handoff")
    if re.search(r"\b(?:how\s+do\s+i\s+pay|payment)\b", text, re.I):
        return result("general_payment_client")
    return None


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
