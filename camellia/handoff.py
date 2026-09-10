"""Instagram -> WhatsApp human handoff.

Channel policy: Instagram is the only AI chatbot channel. When Instagram
cannot answer (an unsupported question), or the customer asks for a person,
or the request is order/event/complaint-shaped, this module creates an opaque
reference ("CAM-48371") and a WhatsApp click-to-chat link so the customer can
continue with a human on WhatsApp. It never answers the customer itself, and
it never sends the conversation contents anywhere public — the reference
resolves context server-side, through this module and the existing
`escalator` staff-notification path.

A repeated "human please" from the same customer reuses the active handoff
(`db.get_active_handoff`) instead of minting a new reference every time.
"""

import random
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from camellia.config import config
from camellia.database import db
from camellia.monitoring import get_logger

log = get_logger("camellia.handoff")

# The reference is opaque by design: no database id, no phone number, no
# sequential counter a competitor could use to estimate order volume.
_REF_RE = re.compile(r"\bCAM-(\d{5,6})\b", re.IGNORECASE)

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


def extract_reference(text: str) -> Optional[str]:
    """Pulls a "CAM-48371"-shaped reference out of free text, if present."""
    match = _REF_RE.search(text or "")
    return f"CAM-{match.group(1)}" if match else None


def _known_entities(facts: Dict[str, Any]) -> Dict[str, Any]:
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
    order_draft = facts.get("order_draft") or {}
    for key, value in order_draft.items():
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
) -> Dict[str, Any]:
    """Returns the customer's active handoff, creating one only if needed.

    Deduplication: a customer who says "human", then "human please", then
    "can I speak to someone" a minute later gets ONE reference, not three —
    `db.get_active_handoff` is the single source of truth for "is there
    already an open enquiry for this customer".
    """
    order_summary = verified_order_summary(facts)
    existing = db.get_active_handoff(channel, user_id)
    if existing:
        existing["newly_created"] = False
        existing["verified_order"] = order_summary
        return existing

    known_entities = _known_entities(facts)
    if order_summary:
        known_entities["verified_order"] = order_summary
    unresolved_topic = str((facts.get("last_entities") or {}).get("topic") or "").strip() or None
    summary_lines = [f"Reason: {reason}"]
    summary_lines.extend(_summarize_known_entities(known_entities))
    if unresolved_topic:
        summary_lines.append(f"Needs human confirmation: {unresolved_topic}")
    summary = "\n".join(summary_lines)

    expires_at = (
        datetime.utcnow() + timedelta(hours=config.CROSS_CHANNEL_HANDOFF_EXPIRY_HOURS)
    ).strftime("%Y-%m-%d %H:%M:%S")

    reference = _generate_reference()
    handoff = db.create_handoff(
        handoff_ref=reference,
        source_channel=channel,
        source_user_id=user_id,
        reason=reason,
        summary=summary,
        known_entities=known_entities,
        unresolved_topic=unresolved_topic,
        escalation_id=escalation_id,
        expires_at=expires_at,
    )
    log.info("Created handoff %s for %s/%s (%s).", reference, channel, user_id, reason)
    handoff["newly_created"] = True
    handoff["verified_order"] = order_summary
    return handoff


def whatsapp_link(handoff_ref: str, order_summary: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """A wa.me click-to-chat link to the client's own WhatsApp number.

    Returns None when WHATSAPP_BUSINESS_NUMBER is not yet configured — the
    caller falls back to naming the reference only, rather than sending a
    broken link. The prefilled text is deliberately minimal: no product
    details, no customer data, nothing beyond the opaque reference the human
    team looks up on their side.
    """
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
    lines.append("Customer moved to: WhatsApp")
    return "\n".join(lines)


def mark_referred(handoff_ref: str) -> None:
    """Called once the WhatsApp continuation link has actually been sent."""
    db.update_handoff_status(handoff_ref, "REFERRED_TO_WHATSAPP")


def mark_whatsapp_connected(handoff_ref: str) -> Optional[Dict[str, Any]]:
    """Called when the reference shows up in an inbound WhatsApp message."""
    handoff = db.get_handoff_by_ref(handoff_ref)
    if not handoff:
        return None
    db.update_handoff_status(handoff_ref, "WHATSAPP_CONNECTED")
    log.info("Handoff %s connected on WhatsApp.", handoff_ref)
    return handoff
