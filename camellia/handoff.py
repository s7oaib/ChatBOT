"""Instagram human handoff with an explicit support/order destination.

Channel policy: Instagram is the only AI chatbot channel. When Instagram
cannot answer (an unsupported question), or the customer asks for a person,
or the request is order/event/complaint-shaped, this module creates an opaque
reference ("CAM-48371") and a durable owner handoff. Purchase-related
requests may receive the approved WhatsApp click-to-chat continuation; generic
support requests receive a callback acknowledgement with no WhatsApp link. It
never sends the conversation contents anywhere public — the reference resolves
context server-side through the existing `escalator` staff-notification path.

A repeated "human please" from the same customer reuses the active handoff
(`db.get_active_handoff`) instead of minting a new reference every time.
"""

import hashlib
import random
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from camellia import order_intake
from camellia.config import config
from camellia.database import db
from camellia.monitoring import get_logger
from camellia.session_memory import session_memory

log = get_logger("camellia.handoff")


# A WhatsApp continuation is a purchase-flow tool, not a generic support
# inbox. Keep this decision explicit and deterministic so a CAM can never gain
# a WhatsApp link merely because it was escalated.
_PURCHASE_HANDOFF_RE = re.compile(
    r"\b(?:book(?:ing)?|order|ordering|purchase|buy|payment|pay|invoice|"
    r"pre[- ]?order|pre[- ]?book|checkout|reserve|reservation|seat|"
    r"place\s+(?:an?\s+)?order|ready\s+to\s+place|bulk\s+order|"
    r"custom\s+order|\d+\s*(?:boxes?|pieces?|guests?|people|kg)\b|"
    r"event\s+(?:order|catering))\b",
    re.IGNORECASE,
)
_HUMAN_HELP_RE = re.compile(
    r"\b(?:human\s+help|human|a\s+person|people|an?\s+agent|"
    r"someone|somebody|your\s+team|support|staff)\b|"
    r"\b(?:talk|speak|connect|put|reach|get)\s+(?:to|me|back|through)\b|"
    r"\b(?:can|could|would|will)\s+(?:someone|somebody|your\s+team|"
    r"a\s+person|an?\s+agent|staff)\b|\bget\s+back\s+to\s+me\b",
    re.IGNORECASE,
)
_MANAGER_CONTACT_RE = re.compile(
    r"\b(?:who\s+can\s+i\s+speak\s+to\s+directly|"
    r"how\s+can\s+i\s+contact\s+(?:your|the)\s+manager|"
    r"who\s+(?:do|should)\s+i\s+contact|"
    r"manager(?:'s)?\s+(?:contact|email|details?))\b",
    re.IGNORECASE,
)


def normalize_instagram_username(value: Any) -> Optional[str]:
    """Keep a supplied Instagram handle, never substituting a Meta ID."""
    candidate = str(value or "").strip().lstrip("@").strip()
    if not candidate or len(candidate) > 100 or candidate.isdigit():
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", candidate):
        return None
    return candidate


def handoff_contact_mode(user_message: str, *, template_id: Optional[str] = None, facts: Optional[Dict[str, Any]] = None) -> str:
    """Return the customer-facing continuation allowed for this request."""
    if is_human_help_request(user_message) and customer_order_intake_summary(facts or {}):
        return "whatsapp_order"
    if template_id in {"order_draft_receipt", "order_next_steps"}:
        return "whatsapp_order"
    if _PURCHASE_HANDOFF_RE.search(str(user_message or "")):
        return "whatsapp_order"
    return "support_callback"


def is_human_help_request(user_message: str) -> bool:
    """Recognize explicit requests for a person, including short probes."""
    text = " ".join(str(user_message or "").lower().split()).strip(" .?!")
    # These are approved informational manager-contact questions. They must
    # continue to use the canonical contact record rather than becoming a
    # generic callback handoff merely because they contain "speak" or
    # "contact".
    if _MANAGER_CONTACT_RE.search(text):
        return False
    return bool(
        text in {"human", "person", "people", "agent", "staff", "support", "manager", "manager please", "human please"}
        or re.search(r"\b(?:connect|speak|talk)\b.{0,30}\b(?:your |the )?manager\b", text)
        or _HUMAN_HELP_RE.search(text)
    )


def customer_query_summary(
    user_message: str,
    *,
    contact_mode: str,
    reason: str,
) -> str:
    """Create a bounded, privacy-safe owner-facing description of the need."""
    if contact_mode == "whatsapp_order":
        return "Help with a booking, order, purchase, payment, or invoice."
    if is_human_help_request(user_message):
        return "A team member to respond."

    value = " ".join(str(user_message or "").split()).strip()
    # Keep only a short operational excerpt and remove the most common
    # credential/contact shapes before it reaches email or Admin.
    value = re.sub(r"https?://\S+|www\.\S+", "[link removed]", value, flags=re.IGNORECASE)
    value = re.sub(r"\b[6-9]\d{9}\b|\+?91[\s-]*[6-9]\d{9}\b", "[phone removed]", value)
    value = re.sub(r"[^\s@]+@[^\s@]+\.[^\s@]+", "[email removed]", value)
    value = re.sub(r"\b(?:upi|ifsc|account(?: number)?|card|token|password|secret)\b[^.?!\n]{0,80}", "[payment detail removed]", value, flags=re.IGNORECASE)
    value = " ".join(value.split())[:180].rstrip(" .,;:")
    return f"Asks: {value}" if value else "Human assistance."


def _user_hash(user_id: str) -> str:
    return hashlib.sha256(str(user_id or "").encode("utf-8")).hexdigest()[:12]

# Entity keys worth surfacing to staff when present — the ones the responder
# already extracts (see camellia/understanding.py's entity schema) plus the
# order-draft/cart slots the deterministic flows fill in. Nothing here is
# customer-identifying beyond what the customer already typed to the bot.
_SUMMARY_ENTITY_LABELS = {
    "product": "Product", "collection": "Collection", "flavour": "Flavour",
    "menu_type": "Menu", "pack_size": "Pack size", "quantity": "Quantity",
    "event": "Occasion", "location": "Delivery area", "subtopic": "Detail",
}


def verified_order_summary(facts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return only the already-calculated order fields safe for a handoff.

    The order calculator writes these values into ``facts["cart"]`` after it
    has matched a product, pack and price.  A handoff must never reconstruct
    an order from prose or ask the language model to fill gaps, so incomplete
    cart rows are ignored entirely.
    """
    items = facts.get("cart") or []
    verified = []
    for item in items:
        if not isinstance(item, dict):
            continue
        required = ("collection", "flavour", "size", "quantity", "total_price")
        if any(item.get(key) in (None, "") for key in required):
            continue
        try:
            quantity = int(item["quantity"])
            total_price = int(item["total_price"])
        except (TypeError, ValueError):
            continue
        if quantity < 1 or total_price < 0:
            continue
        verified.append({
            "collection": str(item["collection"]),
            "flavour": str(item["flavour"]),
            "size": str(item["size"]),
            "quantity": quantity,
            "total_price": total_price,
            "type": str(item.get("type") or "sweet"),
        })
    if not verified:
        return None
    return {"items": verified, "estimated_item_total": sum(i["total_price"] for i in verified)}



def customer_order_intake_summary(facts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A copy of explicit customer data, never verified cart/business knowledge."""
    draft = facts.get("order_intake_draft")
    if not isinstance(draft, dict) or not draft.get("status"):
        return None
    fields = ("item", "quantity", "delivery_area", "delivery_address", "receiver_phone", "delivery_slot", "location_pin")
    result = {field: draft.get(field) if isinstance(draft.get(field), str) else None for field in fields}
    return result if any(result.values()) else None

def _order_item_lines(summary: Dict[str, Any]) -> List[str]:
    lines = []
    collection_names = {
        "aamar_mishti": "Aamar Mishti Collection",
        "signature": "Signature Collection",
    }
    for item in summary.get("items", []):
        collection = collection_names.get(item["collection"], item["collection"])
        flavour = item["flavour"].title()
        item_name = flavour if item.get("type") == "meal" else f"{flavour} Sondesh"
        lines.extend([
            f"Collection: {collection}",
            f"Item: {item_name}",
            f"Pack: {item['size']}",
            f"Quantity: {item['quantity']} box" + ("es" if item["quantity"] != 1 else ""),
        ])
    lines.append(f"Estimated Item Total: ₹{summary['estimated_item_total']:,}")
    return lines


def _generate_reference() -> str:
    """A short opaque token, retried on the rare collision."""
    for _ in range(10):
        candidate = f"CAM-{random.randint(10000, 99999)}"
        if not db.get_handoff_by_ref(candidate):
            return candidate
    # Astronomically unlikely at this volume; widen rather than loop forever.
    return f"CAM-{random.randint(100000, 999999)}"


def _known_entities(facts: Dict[str, Any], *, include_order: bool = False) -> Dict[str, Any]:
    """Whatever Instagram's own understanding already extracted this session.

    Reused as-is — never re-asked of the customer. Sourced from
    facts["last_entities"] (set by every responder.match_and_respond call)
    plus the order-draft/cart slots the deterministic flows already fill in,
    so "50 boxes of Jolbhora for a wedding in Delhi" arrives at the handoff
    with product/quantity/event/location already populated.
    """
    known: Dict[str, Any] = {}
    for key, value in (facts.get("last_entities") or {}).items():
        if value not in (None, ""):
            known[key] = value
    # The draft is accumulated order state, so it belongs to an order handoff
    # only. On a general escalation it is the same stale luggage the cart was:
    # an empty draft still carries a default quantity of 1, which reached
    # staff as a bare "Quantity: 1" against an enquiry about nothing, and a
    # half-built one would have handed over a product the customer had not
    # mentioned in this message. `last_entities` above is different — that is
    # what the current message itself said.
    order_draft = facts.get("order_draft") if include_order else None
    for key, value in (order_draft or {}).items():
        if value not in (None, "") and key not in known:
            known[key] = value
    if facts.get("ad_product") and "product" not in known:
        known["product"] = facts["ad_product"]
    return known


def _summarize_known_entities(known_entities: Dict[str, Any]) -> List[str]:
    lines = []
    for key, label in _SUMMARY_ENTITY_LABELS.items():
        if known_entities.get(key):
            lines.append(f"{label}: {known_entities[key]}")
    return lines


def get_or_create_handoff(
    *,
    user_id: str,
    channel: str,
    reason: str,
    user_message: str,
    facts: Dict[str, Any],
    escalation_id: Optional[int] = None,
    include_order: bool = False,
    instagram_username: Optional[str] = None,
    query_summary: Optional[str] = None,
    contact_mode: Optional[str] = None,
    customer_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Returns the customer's active handoff, creating one only if needed.

    Deduplication: a customer who says "human", then "human please", then
    "can I speak to someone" a minute later gets ONE reference, not three —
    `db.get_active_handoff` is the single source of truth for "is there
    already an open enquiry for this customer".

    `include_order` is what separates "I want to order this" from "can I
    speak to someone". Only the first is allowed to carry the cart into the
    handoff. Attaching it to every escalation is how a customer who asked
    for a human ended up with a WhatsApp message that opened "I'm continuing
    my Camellia Instagram order" and quoted a Jolbhora order from a previous
    day — an order they had not mentioned and might no longer want.
    """
    effective_mode = contact_mode or handoff_contact_mode(user_message, facts=facts)
    # Customer-provided intake fields also inform staff about an unsupported
    # question during that intake. They remain explicitly UNVERIFIED; this
    # does not promote a stale cart to an order or change the contact mode.
    customer_draft = customer_order_intake_summary(facts)
    order_summary = verified_order_summary(facts) if include_order else None
    known_entities = _known_entities(facts, include_order=include_order)
    if order_summary:
        known_entities["verified_order"] = order_summary
    if customer_draft:
        known_entities["customer_order_intake_draft"] = customer_draft
    unresolved_topic = str((facts.get("last_entities") or {}).get("topic") or "").strip() or None
    safe_username = normalize_instagram_username(instagram_username)
    effective_query_summary = query_summary or customer_query_summary(
        user_message, contact_mode=effective_mode, reason=reason,
    )
    summary_lines = [f"Reason: {reason}", f"Customer wants: {effective_query_summary}"]
    if safe_username:
        summary_lines.append(f"Instagram username: @{safe_username}")
    summary_lines.extend(_summarize_known_entities(known_entities))
    if unresolved_topic:
        summary_lines.append(f"Needs human confirmation: {unresolved_topic}")
    if customer_draft:
        summary_lines.extend(["", "CUSTOMER-PROVIDED ORDER DRAFT — UNVERIFIED", order_intake.request_summary(customer_draft).split("\n\n", 1)[1]])
    summary = "\n".join(summary_lines)

    expires_at = (
        datetime.utcnow() + timedelta(hours=config.CROSS_CHANNEL_HANDOFF_EXPIRY_HOURS)
    ).strftime("%Y-%m-%d %H:%M:%S")

    reference = _generate_reference()
    handoff, newly_created = db.get_or_create_handoff(
        handoff_ref=reference,
        source_channel=channel,
        source_user_id=user_id,
        reason=reason,
        summary=summary,
        known_entities=known_entities,
        unresolved_topic=unresolved_topic,
        escalation_id=escalation_id,
        expires_at=expires_at,
        instagram_username=safe_username,
        query_summary=effective_query_summary,
        contact_mode=effective_mode,
        customer_username=(customer_identity or {}).get("customer_username"),
        customer_external_id=(customer_identity or {}).get("customer_external_id") or user_id,
    )
    if newly_created:
        log.info("Created handoff %s for channel=%s user_hash=%s (%s).", reference, channel, _user_hash(user_id), reason)
    handoff["newly_created"] = newly_created
    handoff["customer_order_intake_draft"] = customer_draft
    handoff["verified_order"] = order_summary
    return handoff


def whatsapp_link(handoff_ref: str, order_summary: Optional[Dict[str, Any]] = None, *, customer_draft: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """A wa.me click-to-chat link to the client's own WhatsApp number.

    Returns None when WHATSAPP_BUSINESS_NUMBER is not yet configured — the
    caller falls back to naming the reference only, rather than sending a
    broken link. The prefilled text is deliberately minimal: no product
    details, no customer data, nothing beyond the opaque reference the human
    team looks up on their side.
    """
    if customer_draft:
        return order_intake.whatsapp_draft_link(customer_draft, reference=handoff_ref)
    number = re.sub(r"\D", "", config.WHATSAPP_BUSINESS_NUMBER or "")
    if not number:
        return None
    if order_summary:
        message = (
            "Hi, I'm continuing my Camellia Instagram order.\n\n"
            f"Ref: {handoff_ref}\n\n"
            "Order:\n" + "\n".join(_order_item_lines(order_summary)) +
            "\n\nPlease help me confirm the delivery details and final invoice."
        )
    else:
        message = f"Hi, I'm continuing my Camellia Instagram enquiry. Ref: {handoff_ref}"
    return f"https://wa.me/{number}?text={urllib.parse.quote(message)}"


def customer_facing_message(
    handoff_ref: str,
    order_summary: Optional[Dict[str, Any]] = None,
    *,
    order_receipt: bool = False,
) -> str:
    """The Instagram-side copy offering the WhatsApp continuation.

    config.INSTAGRAM_HANDOFF_MESSAGE is a placeholder pending client sign-off
    (see the manual-configuration report) — this function only appends the
    link/reference to whatever wording is configured, it never invents the
    lead sentence itself.
    """
    link = whatsapp_link(handoff_ref, order_summary)
    body = (
        "Continue on WhatsApp to send this order draft to our team."
        if order_receipt else config.INSTAGRAM_HANDOFF_MESSAGE.rstrip()
    )
    if link:
        return f"{body}\n\n{link}"
    # No business number configured yet — degrade to naming the reference so
    # the handoff is still usable once staff give it to the customer by hand.
    return f"{body}\n\nPlease quote reference {handoff_ref} when you message us."


def customer_facing_notice(*, order_receipt: bool = False, finalized_order: bool = False) -> str:
    """First Instagram message: the CTA copy, with no URL embedded in it."""
    if finalized_order:
        return (
            "Our Manager of Kitchens, Mr. Guru Dayal Singh (+918796551990), will "
            "connect with you shortly and share your invoice. He will be your "
            "single point of contact for this order and will be happy to assist "
            "you with any queries."
        )
    if order_receipt:
        return "Continue on WhatsApp to send this order draft to our team."
    return (
        "Thanks for your question. Our Manager of Kitchens will connect with "
        "you here shortly and help you with this."
    )


def customer_facing_link_message(
    handoff_ref: str,
    order_summary: Optional[Dict[str, Any]] = None,
    *, customer_draft: Optional[Dict[str, Any]] = None,
) -> str:
    """Second Instagram message: a raw URL on its own line when configured."""
    link = whatsapp_link(handoff_ref, order_summary, customer_draft=customer_draft)
    if link:
        return f"Continue on WhatsApp:\n{link}"
    return f"Please quote reference {handoff_ref} when you message us on WhatsApp."


def staff_summary(handoff: Dict[str, Any]) -> str:
    """The internal 'CAMELLIA HANDOFF' block for the human team."""
    lines = [
        "CAMELLIA HANDOFF",
        f"Reference: {handoff['handoff_ref']}",
        f"Source: {handoff['source_channel'].title()}",
        f"Reason: {handoff.get('reason') or 'Unspecified'}",
    ]
    if handoff.get("summary"):
        lines.append("")
        lines.append(handoff["summary"])
    if handoff.get("verified_order"):
        lines.append("")
        lines.append("Known order:")
        lines.extend(_order_item_lines(handoff["verified_order"]))
        lines.append("Pending: Delivery address verification; final invoice")
    lines.append("")
    lines.append(
        "Customer continuation: WhatsApp order handoff"
        if handoff.get("contact_mode") == "whatsapp_order"
        else "Customer continuation: Camellia team callback"
    )
    return "\n".join(lines)


def mark_referred(handoff_ref: str) -> None:
    """Put the customer into the explicit waiting-for-human state."""
    db.update_handoff_status(handoff_ref, "WAITING_FOR_HUMAN")


def retire_order_state(user_id: str, channel: str) -> None:
    """
    Drops the cart once its contents have been handed to WhatsApp.

    The order draft exists to build a receipt; once that receipt and its
    prefilled link have been sent, a human owns the order and Instagram's
    copy is finished. Keeping it alive is what let a days-old Jolbhora cart
    ride along on later, unrelated enquiries.

    Deliberately narrow: this clears the cart and order draft only.
    Source/reel routing, the handoff reference and its deduplication all
    survive, because none of them are order state — a customer who shared a
    reel and then ordered is still in that reel's context afterwards.
    """
    facts = session_memory.get_facts(user_id, channel)
    customer_draft = customer_order_intake_summary(facts)
    if isinstance(facts.get("order_intake_draft"), dict):
        facts["last_referred_order_draft"] = dict(customer_draft or {})
        facts["order_intake_referred"] = True
    session_memory.clear_order_draft(user_id, channel)
    log.info("Retired Instagram order state for channel=%s user_hash=%s after WhatsApp referral.", channel, _user_hash(user_id))
