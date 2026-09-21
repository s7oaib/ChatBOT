"""Authoritative client-approved QA registry and deterministic router.

The client source remains plain text and is parsed without changing its
customer-facing wording. Routing metadata is deliberately separate from the
answer text: aliases, context, assets, lifecycle, and safety guards are
structured fields, never generated answer content.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SOURCE_PATH = Path(__file__).resolve().parents[1] / "data" / "client_approved_qa_source.txt"


@dataclass(frozen=True)
class ApprovedQA:
    qa_id: str
    canonical_id: str
    question: str
    answer: str
    context: str = "general"
    aliases: Tuple[str, ...] = ()
    context_requirements: Tuple[Tuple[str, str], ...] = ()
    source: Tuple[Tuple[str, str], ...] = ()
    actions: Tuple[str, ...] = ()
    asset_keys: Tuple[str, ...] = ()
    # Ordered source segments retain the placement of operational asset
    # markers. ``answer`` remains the marker-free canonical copy used by
    # audits and existing callers; transports may use these segments when a
    # customer-facing reply must interleave text and media.
    response_segments: Tuple[Tuple[str, str], ...] = ()
    lane: str = "EXACT_APPROVED"
    status: str = "ACTIVE"
    business_fact_guard: Optional[str] = None

    @property
    def canonical_question(self) -> str:
        return self.question

    @property
    def canonical_answer(self) -> str:
        return self.answer

    @property
    def entity(self) -> str:
        return self.context

    @property
    def intent(self) -> str:
        return _INTENT_BY_CANONICAL_ID.get(self.canonical_id, self.canonical_id.lower())

    @property
    def topic(self) -> str:
        return self.context

    @property
    def context_requirements_dict(self) -> Dict[str, str]:
        return dict(self.context_requirements)

    @property
    def provenance(self) -> Dict[str, str]:
        return dict(self.source)


@dataclass(frozen=True)
class ApprovedMatch:
    entry: Optional[ApprovedQA]
    match_kind: str
    asset_keys: Tuple[str, ...] = ()
    clarification: Optional[str] = None


@dataclass(frozen=True)
class _Definition:
    qa_id: str
    canonical_id: str
    question: str
    context: str
    status: str = "ACTIVE"
    occurrence: int = 1
    aliases: Tuple[str, ...] = ()
    actions: Tuple[str, ...] = ()
    guard: Optional[str] = None


def _definition(
    qa_id: str,
    canonical_id: str,
    question: str,
    context: str,
    *,
    status: str = "ACTIVE",
    occurrence: int = 1,
    aliases: Sequence[str] = (),
    actions: Sequence[str] = (),
    guard: Optional[str] = None,
) -> _Definition:
    return _Definition(
        qa_id, canonical_id, question, context, status, occurrence,
        tuple(aliases), tuple(actions), guard,
    )


def _line_key(value: str) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def _normalise_question(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.casefold().replace("’", "'")
    value = re.sub(r"[^\w\s]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


# The source has 23 approved blocks, including one superseded pre-book block.
# The second pre-book block is later in the authoritative source and is current.
_DEFINITIONS: Tuple[_Definition, ...] = (
    _definition("home_table_details", "HOME_TABLE_DETAILS", "tell me more about this", "home_table", aliases=("tell me more", "share details about this", "tell me about the Bengali Home Table")),
    _definition("home_table_price", "HOME_TABLE_PRICE", "Whats the price?", "home_table", aliases=("what is the price", "how much is this", "how much does the Home Table cost")),
    _definition("home_table_payment", "HOME_TABLE_PAYMENT", "How to pay", "home_table", aliases=("how do I pay for the Home Table", "payment options for the home table", "where should I pay for this"), actions=("SEND_RESPONSE",)),
    _definition("general_price", "GENERAL_MENU_PRICE", "whats the price", "general", aliases=("what do your meals cost", "how much do your menu options cost", "what are your rates")),
    _definition("general_details", "GENERAL_CAMELLIA_DETAILS", "Can you tell me more about this", "general", aliases=("tell me more about your food", "what do you offer", "share details about your menu")),
    _definition("alacarte_prebook", "PREBOOK_LEGACY", "WHAT ARE THE NEXT STEPS TO PRE BOOK?", "alacarte", status="SUPERSEDED", occurrence=1, aliases=("how do I pre book the old way",), actions=("MANUAL_WHATSAPP_HANDOFF",), guard="historical_only"),
    _definition("subscription_price", "SUBSCRIPTION_PRICE", "What is the price of SUBSCRIPTION - HOME FOOD AWAY FROM HOME", "subscription", aliases=("how much is the meal subscription", "subscription rates", "everyday meal plan price"), actions=("SEND_ASSET",)),
    _definition("subscription_details_client", "SUBSCRIPTION_DETAILS", "Tell me more about SUBSCRIPTION - HOME FOOD AWAY FROM HOME", "subscription", aliases=("tell me about the everyday meal plan", "what is home food away from home", "explain the subscription"), actions=("SEND_ASSET",)),
    _definition("price_is_high", "PRICING_CONCERN", "PRICE IS HIGH", "general", aliases=("why is the price high", "this is expensive", "the price seems too high"), guard="approved_pricing_facts_only"),
    _definition("bento_details_client", "BENTO_DETAILS", "tell me about BENGALI BENTO", "bento", aliases=("tell me more about Bengali Bento", "what is in the Bento", "Bengali Bento details"), actions=("SEND_ASSET",)),
    _definition("bento_price_client", "BENTO_PRICE", "what is pirce of  BENGALI BENTO", "bento", aliases=("what is the price of Bengali Bento", "how much is a Bento", "Bento cost"), actions=("SEND_ASSET",)),
    _definition("best_time_to_book", "BEST_TIME_TO_BOOK", "whats the BEST TIME TO BOOK", "general", aliases=("when should I book", "how early should I order", "when is the best time to reserve")),
    _definition("savoury_delivery_time", "GENERAL_DELIVERY_TIME", "WHATS THE DELIVERY TIME", "savoury", aliases=("how long does meal delivery take", "how soon can you deliver the food", "what is the delivery ETA")),
    _definition("chicken_kosha_price_client", "CHICKEN_KOSHA_PRICE", "WHATS THE PRICE OF CHICKEN KOSHA", "savoury", aliases=("how much is Chicken Kosha", "Chicken Kosha cost", "price per portion of Chicken Kosha")),
    _definition("order_ready_handoff", "ORDER_READY_HANDOFF", "When a customer has placed order OR is ready to place order", "ordering", aliases=("I am ready to place my order", "I have placed the order", "I want to confirm my order"), actions=("MANUAL_WHATSAPP_HANDOFF",)),
    _definition("sondesh_delivery_time", "SONDESH_DELIVERY_TIME", "How much time do you take to deliver sondesh", "sondesh", aliases=("how long will Sondesh delivery take", "when can Sondesh be dispatched", "Sondesh delivery ETA")),
    _definition("sondesh_price_client", "SONDESH_PRICE", "Whats the price of sondesh?", "sondesh", aliases=("what does Sondesh cost", "how much is a box of Sondesh", "Sondesh rates"), actions=("SEND_ASSET",)),
    _definition("sondesh_details_client", "SONDESH_DETAILS", "Tell me more about sondesh", "sondesh", aliases=("tell me about Sondesh", "what is Sondesh", "share Sondesh details"), actions=("SEND_ASSET",)),
    _definition("sondesh_unfamiliar_client", "SONDESH_UNFAMILIAR", "Sondesh unfamiliar (new category)", "sondesh", aliases=("I am new to Sondesh", "I have never tried Sondesh", "what is this sweet"), actions=("SEND_ASSET",)),
    _definition("delivery_out_of_area_client", "OUT_OF_AREA_SONDESH_DELIVERY", "Do you deliver to xyz city? ( any city other than delhi gurgaon faridabad ghaziabad noida, kolkata)", "delivery", aliases=("do you deliver Sondesh outside Delhi NCR", "can you courier Sondesh to Bangalore", "delivery to another city"), guard="approved_delivery_area_only"),
    _definition("order_prebook", "PREBOOK_CURRENT", "WHAT ARE THE NEXT STEPS TO PRE BOOK?", "ordering", occurrence=2, aliases=("how do I pre book", "what do I need to pre-order", "how can I place an order"), actions=("MANUAL_WHATSAPP_HANDOFF",)),
    _definition("general_payment_client", "GENERAL_PAYMENT", "How do I pay?", "general", aliases=("what payment methods do you accept", "how can I make payment", "payment options"), actions=("SEND_RESPONSE",)),
    _definition("minimum_sondesh_quantity_client", "SONDESH_MINIMUM_ORDER_REASON", "Why is the Minimum order 9 pieces 250g for some flavours", "sondesh", aliases=("why is Sondesh sold in multiples of nine", "what is the minimum Sondesh quantity", "why nine pieces"), guard="approved_sondesh_facts_only"),
)

_INTENT_BY_CANONICAL_ID = {
    "HOME_TABLE_DETAILS": "details",
    "HOME_TABLE_PRICE": "price",
    "HOME_TABLE_PAYMENT": "payment",
    "GENERAL_MENU_PRICE": "price",
    "GENERAL_CAMELLIA_DETAILS": "details",
    "PREBOOK_LEGACY": "prebook",
    "PREBOOK_CURRENT": "prebook",
    "SUBSCRIPTION_PRICE": "price",
    "SUBSCRIPTION_DETAILS": "details",
    "PRICING_CONCERN": "pricing_concern",
    "BENTO_DETAILS": "details",
    "BENTO_PRICE": "price",
    "BEST_TIME_TO_BOOK": "booking_advice",
    "GENERAL_DELIVERY_TIME": "delivery_time",
    "CHICKEN_KOSHA_PRICE": "price",
    "ORDER_READY_HANDOFF": "order_ready",
    "SONDESH_DELIVERY_TIME": "delivery_time",
    "SONDESH_PRICE": "price",
    "SONDESH_DETAILS": "details",
    "SONDESH_UNFAMILIAR": "details",
    "OUT_OF_AREA_SONDESH_DELIVERY": "delivery_area",
    "GENERAL_PAYMENT": "payment",
    "SONDESH_MINIMUM_ORDER_REASON": "minimum_order_reason",
}

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


def _clean_answer(
    raw_lines: Sequence[str],
) -> Tuple[str, Tuple[str, ...], Tuple[Tuple[str, str], ...]]:
    assets: List[str] = []
    kept: List[str] = []
    segments: List[Tuple[str, str]] = []
    segment_lines: List[str] = []

    def flush_text_segment() -> None:
        if not segment_lines:
            return
        segment = "\n".join(segment_lines).strip()
        segment = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", segment)
        if segment:
            segments.append(("text", segment))
        segment_lines.clear()

    for raw_line in raw_lines:
        line = raw_line.rstrip()
        marker = line.strip()
        asset = _PLACEHOLDER_ASSETS.get(marker)
        if asset:
            flush_text_segment()
            assets.append(asset)
            segments.append(("asset", asset))
            continue
        # Obsolete generic support instruction: the current architecture uses
        # CAM. Manual WhatsApp is represented only on booking/order records.
        if marker == "ADD LINK TO WHATSAPP":
            continue
        kept.append(line)
        segment_lines.append(line)
    flush_text_segment()
    answer = "\n".join(kept).strip()
    answer = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", answer)
    return answer, tuple(dict.fromkeys(assets)), tuple(segments)


def _metadata(
    definition: _Definition,
    assets: Tuple[str, ...],
    response_segments: Tuple[Tuple[str, str], ...],
    line_number: int,
) -> ApprovedQA:
    requirements = {"source_type": "none", "topic": definition.context, "required": "false"}
    if definition.context == "home_table":
        requirements.update(source_type="instagram_story_post_reel", topic="bengali_home_table", required="true")
    elif definition.context in {"subscription", "bento", "sondesh", "savoury", "delivery", "ordering"}:
        requirements.update(topic=definition.context)
    actions = list(definition.actions)
    if assets and "SEND_ASSET" not in actions:
        actions.append("SEND_ASSET")
    return ApprovedQA(
        qa_id=definition.qa_id,
        canonical_id=definition.canonical_id,
        question=definition.question,
        answer="",
        context=definition.context,
        aliases=definition.aliases,
        context_requirements=tuple(sorted(requirements.items())),
        source=(("path", str(SOURCE_PATH)), ("heading", definition.question), ("occurrence", str(definition.occurrence)), ("line", str(line_number))),
        actions=tuple(dict.fromkeys(actions)),
        asset_keys=assets,
        response_segments=response_segments,
        status=definition.status,
        business_fact_guard=definition.guard,
    )


def parse_source(source_text: Optional[str] = None) -> Tuple[ApprovedQA, ...]:
    """Parse the source in order, retaining superseded evidence."""
    text = SOURCE_PATH.read_text(encoding="utf-8") if source_text is None else source_text
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    by_heading: Dict[str, List[_Definition]] = {}
    for definition in _DEFINITIONS:
        by_heading.setdefault(_line_key(definition.question), []).append(definition)
    occurrences: Dict[str, int] = {}
    positions: List[Tuple[int, _Definition]] = []
    for index, line in enumerate(lines):
        key = _line_key(line)
        definitions = by_heading.get(key)
        if not definitions:
            continue
        occurrence = occurrences.get(key, 0) + 1
        occurrences[key] = occurrence
        definition = next((item for item in definitions if item.occurrence == occurrence), None)
        if definition:
            positions.append((index, definition))

    entries: List[ApprovedQA] = []
    for position, (start, definition) in enumerate(positions):
        next_heading = positions[position + 1][0] if position + 1 < len(positions) else len(lines)
        end = next_heading
        for marker in _STOP_MARKERS:
            marker_key = _line_key(marker)
            for index in range(start + 1, next_heading):
                if _line_key(lines[index]) == marker_key:
                    end = min(end, index)
                    break
        answer, assets, response_segments = _clean_answer(lines[start + 1:end])
        if definition.qa_id in {"bento_details_client", "bento_price_client"} and re.search(r"sharing our full menu for the week below", answer, re.I):
            assets = tuple(dict.fromkeys((*assets, "weekly_menu")))
        entry = _metadata(definition, assets, response_segments, start + 1)
        entries.append(ApprovedQA(**{**entry.__dict__, "answer": answer}))

    order_ready_index = next((index for index, line in enumerate(lines) if _line_key(line) == _line_key(_ORDER_READY_MARKER)), None)
    if order_ready_index is not None and not any(item.qa_id == "order_ready_handoff" for item in entries):
        definition = next(item for item in _DEFINITIONS if item.qa_id == "order_ready_handoff")
        next_heading = next((index for index, _item in positions if index > order_ready_index), len(lines))
        answer, assets, response_segments = _clean_answer(lines[order_ready_index + 1:next_heading])
        entry = _metadata(definition, assets, response_segments, order_ready_index + 1)
        entries.append(ApprovedQA(**{**entry.__dict__, "answer": answer}))
    return tuple(entries)


REGISTRY: Tuple[ApprovedQA, ...] = parse_source()
REGISTRY_BY_ID: Dict[str, ApprovedQA] = {entry.qa_id: entry for entry in REGISTRY}
REGISTRY_BY_CANONICAL_ID: Dict[str, ApprovedQA] = {entry.canonical_id: entry for entry in REGISTRY}


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
    return _has_home_table_context(facts) or bool(re.search(r"\b(?:bengali|kolkata)\s+home\s+(?:table|lunch)|home\s+table\b", message, re.I))


def _generic_price(message: str) -> bool:
    if re.fullmatch(r"(?:how much|price|cost|rate|what(?:'s| is)? the price)[?!., ]*", message.strip(), re.I):
        return True
    if re.search(r"\b(?:this|that|it)\b", message, re.I):
        return False
    return bool(re.search(r"\b(?:how much|price|cost|rate)\b", message, re.I)) and not bool(re.search(r"\b(?:sondesh|bento|chicken|kosha|subscription|home\s+table|meal\s+plan|mutton|mangsho|jolbhora|kanchagolla|mishti|mithai|aamar|signature|nolen\s+gur)\b", message, re.I))


def _find(entry_id: str) -> Optional[ApprovedQA]:
    return REGISTRY_BY_ID.get(entry_id) or REGISTRY_BY_CANONICAL_ID.get(entry_id)


def _result(entry_id: str, kind: str = "paraphrase") -> Optional[ApprovedMatch]:
    entry = _find(entry_id)
    if not entry or entry.status != "ACTIVE":
        return None
    return ApprovedMatch(entry, kind, entry.asset_keys)


def match(message: str, facts: Optional[Dict[str, Any]] = None) -> Optional[ApprovedMatch]:
    """Resolve explicit current intent before source context and state."""
    text = " ".join(str(message or "").split()).strip()
    normalized = _normalise_question(text)
    state = facts or {}
    home = _home_context(text, state)
    exact = lambda question: normalized == _normalise_question(question)

    # 1. Explicit current entity/product/topic.
    if re.search(r"\b(?:out\s+of\s+area|other\s+city|different\s+city|xyz\s+city|outside\s+(?:delhi|ncr)|deliver(?:\s+\w+){0,5}\s+to\s+(?:bangalore|bengaluru|mumbai|chennai|hyderabad|pune|kolkata))\b", text, re.I):
        return _result("OUT_OF_AREA_SONDESH_DELIVERY")
    if re.search(r"\b(?:minimum|multiples?\s+of\s+(?:nine|9)|9\s*pieces|250\s*g)\b", text, re.I) and re.search(r"\b(?:flavou?r|sondesh|order)\b", text, re.I):
        return _result("SONDESH_MINIMUM_ORDER_REASON")
    if re.search(r"\bsubscriptions?\b|\bmeal\s+plan\b|\beveryday\s+meal\b|\bhome\s+food\s+away\s+from\s+home\b", text, re.I):
        return _result("SUBSCRIPTION_PRICE" if re.search(r"\b(?:prices?|cost|rates?|how\s+much)\b", text, re.I) else "SUBSCRIPTION_DETAILS")
    if re.search(r"\bchicken\s+kosha\b", text, re.I):
        return _result("CHICKEN_KOSHA_PRICE")
    if re.search(r"\b(?:bengali\s+bento|bento)\b", text, re.I) and not re.search(r"\b(?:without|no|remove|replace|substitut|customi[sz]|cheapest|lowest|how\s+much\s+more|difference|cheaper)\b", text, re.I):
        return _result("BENTO_PRICE" if re.search(r"\b(?:price|cost|how\s+much|rate|pirce)\b", text, re.I) else "BENTO_DETAILS")
    named_sondesh = re.search(r"\b(?:sondesh|sandesh)\b", text, re.I)
    # Jolbhora/Kanchagolla/Mishti have richer existing product routes. The
    # explicit subject boundary in the main responder owns those names; this
    # compact registry owns the approved Sondesh category blocks.
    if named_sondesh:
        if re.search(r"\b(?:how\s+much\s+time|how\s+long|delivery\s+time|dispatch|deliver)\b", text, re.I):
            return _result("SONDESH_DELIVERY_TIME")
        if re.search(r"\b(?:price|cost|how\s+much|rate|pieces|box)\b", text, re.I):
            return _result("SONDESH_PRICE")
        if re.search(r"\b(?:unfamiliar|new\s+to|never\s+tried|what\s+is\s+this)\b", text, re.I):
            return _result("SONDESH_UNFAMILIAR")
        if re.search(r"\b(?:tell|talk|more|details|about|what\s+is)\b", text, re.I):
            return _result("SONDESH_DETAILS")
    if re.search(r"\b(?:price\s+(?:is\s+)?high|high\s+price|too\s+expensive|quite\s+expensive|why\s+is\s+it\s+so\s+expensive)\b", text, re.I):
        return _result("PRICING_CONCERN", "exact" if exact("PRICE IS HIGH") else "paraphrase")

    # 2. Explicit current intent; active state is only used when the message
    # itself remains entity-free.
    if re.search(r"\b(?:how\s+to\s+pay|how\s+do\s+i\s+pay|payment\s+(?:method|options?)|payment)\b", text, re.I):
        return _result("HOME_TABLE_PAYMENT" if home else "GENERAL_PAYMENT", "exact" if exact("How to pay") else "paraphrase")
    if re.search(r"\b(?:best\s+time|when)\s+to\s+(?:book|reserve)\b|\bwhen\s+is\s+the\s+best\s+time\b", text, re.I):
        return _result("BEST_TIME_TO_BOOK")
    if re.search(r"\bdelivery\s+time\b|\bhow\s+long\b.*\bdeliver(?:y)?\b", text, re.I):
        return _result("GENERAL_DELIVERY_TIME")
    if re.search(r"\b(?:pre\s*[- ]?book|pre\s*[- ]?order|how\s+to\s+order|place\s+an?\s+order|order\s+ala\s*[- ]?carte)\b", text, re.I):
        return _result("PREBOOK_CURRENT")
    if re.search(r"\b(?:ready\s+to\s+(?:place|order)|placed\s+(?:my|the)\s+order|confirm\s+(?:my|the)\s+order|finali[sz]e\s+(?:it|my\s+order))\b", text, re.I):
        return _result("ORDER_READY_HANDOFF")

    # 3. Exact source context resolves only deictic Home Table language.
    if re.search(r"\b(?:jolbhora|kanchagolla|mishti|mithai)\b", text, re.I) and re.search(r"\b(?:price|cost|how\s+much|rate)\b", text, re.I):
        # Existing flavour-specific routes own these explicit products. The
        # Home Table source may not swallow a named current product.
        return None
    if home and re.search(r"\b(?:tell|talk|share|give)\s+me\s+(?:more|details)\s+(?:about\s+)?(?:this|(?:the\s+)?(?:bengali\s+)?home\s+table|the\s+event)\b|\btell\s+me\s+about\s+(?:the\s+)?(?:bengali\s+)?home\s+table\b", text, re.I):
        return _result("HOME_TABLE_DETAILS", "exact" if exact("tell me more about this") else "paraphrase")
    if home and (_generic_price(text) or re.search(r"\b(?:price|cost|how\s+much)\b", text, re.I)):
        return _result("HOME_TABLE_PRICE", "exact" if exact("Whats the price?") else "paraphrase")

    # 4/5. Details have one safe general answer. A bare price has multiple
    # approved candidates and must clarify unless general context is explicit.
    if normalized in {_normalise_question("can you tell me more about this"), _normalise_question("tell me more about your food"), _normalise_question("what do you offer")} or re.search(r"\b(?:share|tell|explain|describe)\b.*\b(?:food|meal|format|offer|menu)\b", text, re.I):
        return _result("GENERAL_CAMELLIA_DETAILS", "exact" if exact("Can you tell me more about this") else "paraphrase")
    if re.search(r"\b(?:meal|menu|food)\s+options?\b|\byour\s+(?:meals?|menu)\b", text, re.I) and re.search(r"\b(?:price|cost|rate|how\s+much)\b", text, re.I):
        return _result("GENERAL_MENU_PRICE", "paraphrase")
    if _generic_price(text):
        # The source intentionally has a separate general heading written as
        # ``whats the price``. Preserve that exact approved trigger; the
        # punctuated/deictic form remains genuinely ambiguous without source
        # context and therefore asks for clarification.
        if exact("whats the price") and not text.rstrip().endswith("?"):
            return _result("GENERAL_MENU_PRICE", "exact")
        if state.get("general_context") or state.get("explicit_general_context") or str(state.get("last_topic") or "").casefold() in {"general", "savoury"}:
            return _result("GENERAL_MENU_PRICE", "exact" if exact("whats the price") else "paraphrase")
        return ApprovedMatch(None, "clarification", clarification="Are you asking about the Bengali Home Table, Bengali Bento, Sondesh, or the regular menu?")
    return None


def source_fingerprint() -> str:
    return hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest()


def registry_summary() -> Dict[str, Any]:
    return {
        "source": str(SOURCE_PATH),
        "canonical_entry_count": len(REGISTRY),
        "active_entry_count": sum(entry.status == "ACTIVE" for entry in REGISTRY),
        "canonical_ids": [entry.canonical_id for entry in REGISTRY],
        "source_fingerprint": source_fingerprint(),
        "duplicate_resolution": {
            "PREBOOK_LEGACY": "SUPERSEDED",
            "PREBOOK_CURRENT": "ACTIVE",
            "HOME_TABLE_DETAILS_vs_GENERAL_CAMELLIA_DETAILS": "context_required",
            "HOME_TABLE_PRICE_vs_GENERAL_MENU_PRICE": "context_required",
            "HOME_TABLE_PAYMENT_vs_GENERAL_PAYMENT": "context_required",
        },
    }
