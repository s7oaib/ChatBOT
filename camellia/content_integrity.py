"""Small, idempotent customer-copy remediations from the content audit.

These edits deliberately use the versioned Response Library override store.  The
protected template triggers and routing metadata remain untouched, and an
operator's existing edit is never overwritten silently.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional


ACTOR = "content_integrity_audit_2026-09-13"
REASON = "approved_canonical_content_correction"

# Client-approved copy for the narrowly scoped à la carte Sondesh add-on.
# This is restored as an exact suffix after the generic claim scrubber has
# checked the primary answer; it must not be silently shortened to a vague
# offer while preserving the customer-requested asset bundle.
ALACARTE_SONDESH_ADDON_COPY = (
    "We also make handcrafted Norom Pak Sondesh if you’d like to add something "
    "sweet to your order. Sharing a few pictures below."
)

# These claims were identified by the production content audit as requiring
# current operator evidence.  They are deliberately narrow: confirmed facts
# (prices, Porter actuals, gifting/events, A2 and the all-women-team wording)
# are not matched here.  The same helper is used for Response Library seed
# migration and the runtime's last-mile safety check, so an old template or a
# stale database override cannot put these claims back in front of a customer.
UNCONFIRMED_CLAIM_PATTERNS = (
    r"small[- ]batch artisanal farmers",
    r"trusted dairies",
    r"carefully selected partners",
    r"general retail sourcing",
    r"sourced directly from",
    r"fresh artisanal chhana",
    r"organic rose petals?",
    r"forest mahua flowers?",
    r"partially mechanised",
    r"hand\s*-?crafted end to end",
    r"completely hand\s*-?crafted",
    r"\bhand\s*-?crafted\b",
    r"made from scratch",
    r"made in[- ]house",
    r"made fresh daily",
    r"freshly made to order",
    r"freshly cooked",
    r"small batches",
    r"zero artificial preservatives?",
    r"not\s+(?:been\s+)?industrially processed",
    r"limited season",
    r"limited quantities",
    r"run out of stock",
    r"sell out",
    r"available only for",
    r"quantities are limited",
    r"un[- ]processed desi a2",
    r"unadulterated desi a2",
    r"never milk powder",
    r"commercial stabilizers",
    # Exact preparation/dispatch timings are not maintained in menu.json;
    # leave the approved pre-order guidance, but quarantine stale time claims.
    r"within\s+15\s+minutes",
    r"within\s+30\s+minutes",
    r"within\s+45\s+minutes",
    r"within\s+an\s+hour",
    r"takes\s+about\s+30\s+minutes",
    r"takes\s+about\s+an\s+hour",
    r"delivery time after that depends",
    r"delivery time can be at your convenience",
    r"exactly as you'd like",
    r"exactly what you want",
    r"all milk protein",
    r"no added fat",
    r"subtle natural flavours?",
)
_UNCONFIRMED_RE = re.compile("|".join(f"(?:{p})" for p in UNCONFIRMED_CLAIM_PATTERNS), re.IGNORECASE)

QUARANTINE_TEMPLATE_FALLBACKS = {
    "bengali_home_table_details": (
        "Our team can assist you personally on WhatsApp. Please continue your enquiry "
        "using the link below."
    ),
    "bengali_home_table_pricing": (
        "Our team can assist you personally on WhatsApp. Please continue your enquiry "
        "using the link below."
    ),
    "bengali_home_table_payment": (
        "Our team can assist you personally on WhatsApp. Please continue your enquiry "
        "using the link below."
    ),
    "GUARD_dietary_suitability": (
        "Please tell us the exact dietary requirement. Our team will confirm the "
        "ingredients for the specific item before ordering."
    ),
    "delivery_out_of_area": (
        "Please share your city and area so our team can confirm the current "
        "delivery options for you."
    ),
    "kolkata_expansion_info": (
        "Our all-women team can confirm current operations. For the latest details, "
        "please contact Guru Dayal Singh at camellia.artisanal.gurgaon@gmail.com."
    ),
    "delivery_time": (
        "Please share the item and order details so our team can confirm the current "
        "preparation and dispatch information. Pre-ordering is recommended where applicable."
    ),
    "sondesh_dispatch_time": (
        "Please share the item and order details so our team can confirm the current "
        "preparation and dispatch information. Pre-ordering is recommended where applicable."
    ),
    "savoury_delivery_time": (
        "Please share the item and order details so our team can confirm the current "
        "preparation and dispatch information. Pre-ordering is recommended where applicable."
    ),
    "freshness_ingredients": (
        "Signature Collection may be described as using desi A2 cow milk. For exact "
        "ingredients and current preparation details, please ask our team before ordering."
    ),
    "savoury_crosssell_sondesh": (
        "We also offer Sondesh. Please ask our team for the current flavour and "
        "ingredient details."
    ),
    "sondesh_crosssell_savoury": (
        "We also offer Bengali meals across Delhi NCR. Please ask our team for the "
        "current menu and availability."
    ),
    "savoury_best_time_to_book": (
        "We always recommend pre-ordering. Please share your preferred items and "
        "date so our team can confirm the current options."
    ),
    "GUARD_collection_differentiation": (
        "Aamar Mishti is our everyday sweets collection. Signature Collection is "
        "for special occasions and gifting, and may be described as using desi "
        "A2 cow milk. Please ask our team for current product details."
    ),
}

# These are the client-confirmed Phase 3B responses.  The template ID is not a
# blanket bypass: a later edit remains subject to the last-mile guard.  When a
# trusted resolver selects one of these IDs, the safe result of that guard is
# always this exact versioned copy, never an older fallback or edited wording.
CLIENT_CONFIRMED_EXACT_RESPONSES = {
    "bengali_home_table_details": (
        "Hi! Thank you for connecting. Sharing details of the Camellia.artisanal Bengali "
        "Home Table, Kolkata. 4th & 11th October 2026 | 1–6 pm Diamond Park, Joka, Kolkata "
        "Think of the lunch as a Bengali omakase — you don't choose the menu; the season "
        "does. An elaborate vegetarian & non-vegetarian Bengali home lunch, slow-cooked using "
        "traditional methods, followed by several kinds of mishti made by us, a Darjeeling "
        "tea ceremony and a sit-down storytelling session. Story & lunch theme 4th October — "
        "MALE FOLK DEITIES OF BENGAL 11th October — DURGA PUJO Next steps to book a seat "
        "Please let me know how many guests Which of the two dates would you prefer Sharing 4 "
        "videos for you to take a look at details & also a bit of videos from the 100 Bengali "
        "home tables we did at gurgaon. If this interests you, please let me know. I will "
        "share the pricing & other details."
    ),
    "bengali_home_table_pricing": (
        "This is a meal that unfolds slowly, one course and one story at a time. It is a "
        "leisurely Bengali experience where each dish has its own place in the sequence—"
        "allowing you to experience Bengal through its flavours, histories, rituals and "
        "memories. The Vegetarian Experience includes 8 individually served dishes. The "
        "Non-Vegetarian Experience includes 9 individually served dishes. Both conclude with "
        "a seasonal pre-dessert, followed by three distinct desserts made entirely in our "
        "kitchen—including our signature norom-pak Jolbhora Sondesh. Vegetarian Experience — "
        "₹1,500 per person Non-Vegetarian Experience — ₹2,500 per person Prices include GST. "
        "Please let me know if you decide to book. I will share the payment details."
    ),
    "bengali_home_table_payment": (
        "A seat is booked on full payment of the total amount. You can transfer by UPI to "
        "9625083171 Or bank transfer to Yummraj Culinary Expeditions Pvt Ltd Account number- "
        "50200077136337 IFSC - HDFC0000572 Account type - Current OR credit card at "
        "https://razorpay.me/@camelliaartisanal We will send a GST invoice as proof of "
        "payment & confirmation of your seat"
    ),
    "sondesh_crosssell_savoury": (
        "We also prepare and deliver freshly cooked Bengali meals across Delhi NCR. "
        "I've attached this week's menu in case you'd like to have a look. Everything "
        "is cooked fresh in small batches, and some dishes are available only in limited "
        "quantities. If you'd like a recommendation based on whether you prefer "
        "vegetarian, fish or meat, just let me know — I'd be happy to help."
    ),
    "delivery_out_of_area": (
        "Hi, thank you for getting in touch.\n\n"
        "I would have loved to. However, the Norom Pak Sondesh that we make can be "
        "transported from one city to another only through a reliable cold-chain courier "
        "service. Unfortunately, we haven't yet found a partner who can offer this at a "
        "reasonable cost.\n\n"
        "So for now, we're unable to ship our sondesh outside Delhi NCR — we do not "
        "deliver to Bangalore or other cities at this time. We'd love to, though, and "
        "will certainly start as soon as we find a dependable cold-chain delivery option.\n\n"
        "That said, many of our regular customers who live in different parts of India "
        "and the world do buy our sondesh by asking friends or family travelling from "
        "Delhi NCR to carry a few boxes for them. We're happy to deliver the order to "
        "their home, office, hotel, or any other convenient location before they travel.\n\n"
        "Thank you for thinking of camellia.artisanal!"
    ),
    "savoury_best_time_to_book": (
        "We always recommend pre-ordering. It helps us ensure that the dishes you want "
        "are available, exactly as you'd like them. Since we cook everything in small "
        "batches, some dishes may sell out if you order at the last minute. Pre-ordering "
        "is the best way to avoid disappointment."
    ),
    "savoury_delivery_time": (
        "Hi! Thank you for getting in touch.\n\n"
        "We request you to always pre order. So that way the delivery time can be at your "
        "convenience subject to our kitchen timings. If you are unable to pre order, once "
        "your order is confirmed, we usually dispatch it from our kitchen in Sushant Lok, "
        "Gurgaon within 45 minutes.\n\n"
        "Delivery time after that depends on the Porter rider's travel time, which varies "
        "based on traffic and the time of day.\n\n"
        "Our kitchen is open 10:00 AM – 7:30 PM every day. You may also call +91 87965 "
        "51990 or DM to pre order."
    ),
    "kolkata_expansion_info": (
        "Over the past two months, we have personally run our Gurgaon kitchen to ensure "
        "every aspect of the operation met the standards we had envisioned.\n\n"
        "Today, we have a professional team in place, led by our Kitchen Manager, Guru "
        "Dayal Singh (camellia.artisanal.gurgaon@gmail.com). Our women kitchen team has "
        "been with us for the past four years and has been extensively trained by us. "
        "Together, they now run the kitchen through well-established systems, processes, "
        "quality checks and balances.\n\n"
        "Over the last few weeks, we have deliberately reduced our physical presence in "
        "the kitchen and have been monitoring operations remotely. The transition has gone "
        "exactly as planned, and the kitchen is now operating independently while "
        "consistently maintaining our standards.\n\n"
        "This gives us the confidence to move to Kolkata, knowing that nothing changes for "
        "our guests in Delhi NCR. The same kitchen, the same team, the same recipes, the "
        "same ingredients, and the same goal of trying to be at uncompromising standards "
        "will continue — exactly as they do today."
    ),
    "sondesh_dispatch_time": (
        "Hi! Thank you for getting in touch.\n\n"
        "Once your sondesh order is confirmed, we usually dispatch it from our kitchen "
        "in Sushant Lok, Gurgaon within 15 minutes. Kanchagolla takes about 30 minutes. "
        "Jolbhora takes about an hour.\n\n"
        "Delivery time after that depends on the Porter rider's travel time, which varies "
        "based on traffic and the time of day.\n\n"
        "Our kitchen is open 10:00 AM – 7:30 PM every day. We always request a pre order "
        "to ensure we are able to make & send exactly what you want. Else sometimes we "
        "might run out of stock that we make in small batches."
    ),
    "GUARD_staff_names_manager_contact": (
        "Our Manager of Kitchens, Mr. Guru Dayal Singh, is the point of contact for kitchen "
        "enquiries. You can reach the kitchen at +91 87965 51990 or "
        "camellia.artisanal.gurgaon@gmail.com."
    ),
}

PHASE3B_REMEDIATION_ACTOR = "phase3b_client_confirmed_content"
PHASE3B_REMEDIATION_REASON = "phase3b_client_confirmed_content_upgrade"
HOME_TABLE_REMEDIATION_ACTOR = "phase3b_home_table_durable_copy"
HOME_TABLE_REMEDIATION_REASON = "phase3b_home_table_stale_handoff_upgrade"
HOME_TABLE_TEMPLATE_IDS = frozenset({
    "bengali_home_table_details",
    "bengali_home_table_pricing",
    "bengali_home_table_payment",
})
SUBSCRIPTION_TEMPLATE_ID = "subscription_details"
SUBSCRIPTION_REMEDIATION_ACTOR = "phase3a_subscription_durable_copy"
SUBSCRIPTION_REMEDIATION_REASON = "phase3a_subscription_legacy_upgrade"
SUBSCRIPTION_LIVE_STALE_REMEDIATION_REASON = "phase3a_subscription_live_stale_override_upgrade"
KNOWN_LEGACY_SUBSCRIPTION_MARKERS = (
    "₹1,500 for 5 meals",
    "₹1,750",
    "₹2,000 for 5 meals",
    "luchi or polao",
    "gift you the 11th",
    "à la carte",
)
LEGACY_SUBSCRIPTION_RESPONSE = (
    "*SUBSCRIPTION - HOME FOOD AWAY FROM HOME*\n\n"
    "Hi! Thank you for asking about our Everyday Meal Subscription.\n\n"
    "The Everyday Meal Subscription is available in Gurgaon only.\n\n"
    "Freshly cooked, home-style balanced food, using good-quality ingredients, home recipes and no shortcuts.\n\n"
    "Price of 5 MEALS (including GST). Porter delivery charges at actuals, depending on fare from our Sushant Lok Gurgaon kitchen to your location\n\n"
    "• Option 1 Vegetarian — ₹1,500 for 5 meals\n"
    "(Rice/roti + 2 sabzis + chatni/mishti)\n"
    "• Option 2 Veg + Chicken — ₹1,750 (vegetarian on 3 days, chicken on 2 days) for 5 meals\n"
    "• Option 3 Chicken — ₹2,000 for 5 meals\n"
    "(Rice/roti + 1 sabzi + chicken + chatni/mishti)\n\n"
    "Once during the package, you can opt for luchi or polao\n"
    "• No fixed schedule — have your meals over 5 days, 10 days or at your own pace\n"
    "• For every 10 meals, we gift you the 11th\n\n"
    "Simple, varied everyday food — as close as we can make it to a meal from a home kitchen.\n\n"
    "Attached below is our à la carte menu that changes every week. In case you don't want a subscription, you can just order a dish any day from here.\n\n"
    "Would you like to book?"
)
KNOWN_LEGACY_SUBSCRIPTION_SHA256 = "1c1254cc608774fa65675d6f29aad9014e7c936bd251757f47addd6c35437940"
# Exact normalized fingerprint captured from the active production override
# during the Phase 3A live acceptance investigation.  This is an allowlist of
# one known stale durable object, not a content-pattern matcher.
KNOWN_LIVE_STALE_SUBSCRIPTION_SHA256 = "b9b8de0e125cd1cfdf4f15894c674c2576fee0a27a4f6805246e69d3b9e991f4"
KNOWN_LEGACY_SUBSCRIPTION_SHA256S = frozenset({
    KNOWN_LEGACY_SUBSCRIPTION_SHA256,
    KNOWN_LIVE_STALE_SUBSCRIPTION_SHA256,
})


def normalize_legacy_subscription_response(text: Any) -> str:
    """Normalize only line endings and trailing whitespace for fingerprinting."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n")).rstrip()


def legacy_subscription_response_fingerprint(text: Any) -> str:
    return subscription_response_fingerprint(text)


def subscription_response_fingerprint(text: Any) -> str:
    """Return the shared exact fingerprint for a subscription response."""
    return hashlib.sha256(
        normalize_legacy_subscription_response(text).encode("utf-8")
    ).hexdigest()


def is_known_legacy_subscription_response(text: Any) -> bool:
    """Return true only for an exact allowlisted stale subscription copy."""
    return subscription_response_fingerprint(text) in KNOWN_LEGACY_SUBSCRIPTION_SHA256S


def is_client_confirmed_exact_response(template_id: str, text: Any) -> bool:
    """Return true only for an exact, versioned Phase 3B approved response."""
    approved = CLIENT_CONFIRMED_EXACT_RESPONSES.get(str(template_id or ""))
    return approved is not None and str(text or "").strip() == approved.strip()


def contains_unconfirmed_claim(text: Any) -> bool:
    """Return whether customer-visible copy contains an unaudited claim."""
    return bool(_UNCONFIRMED_RE.search(str(text or "")))


def runtime_quarantine_enabled(db_path: str = "") -> bool:
    """Enable the last-mile guard for production/durable remediation paths.

    Unit tests intentionally exercise legacy seed copies in isolated local
    databases.  Production uses the durable `/app/var` database; explicit
    remediation mode enables the same guard for local acceptance runs.
    """
    normalized = str(db_path or "").replace("\\", "/")
    return normalized.startswith("/app/var/") or os.getenv("CONTENT_INTEGRITY_REMEDIATION") == "true"


def quarantine_unconfirmed_claims(text: str) -> str:
    """Remove only unsupported claim sentences/lines, preserving safe copy.

    Templates often put an approved sentence beside one unaudited marketing
    sentence.  Split on sentence/line boundaries first and drop only the
    offending unit.  If a paragraph has no safe text left it is omitted rather
    than replaced with a guessed business fact.
    """
    if not text:
        return text
    if not contains_unconfirmed_claim(text):
        return str(text)
    # Remove an unaudited qualifier without dropping the confirmed fact that
    # surrounds it (especially the approved Signature A2 wording).
    source = str(text)
    source = re.sub(r"\s*\(?that has not been industrially processed\)?", "", source, flags=re.IGNORECASE)
    source = re.sub(r",?\s*(?:completely\s+)?hand\s*-?crafted(?:\s+end to end)?", "", source, flags=re.IGNORECASE)
    source = re.sub(r",?\s*partially mechanised", "", source, flags=re.IGNORECASE)
    source = re.sub(r",?\s*made in[- ]house", "", source, flags=re.IGNORECASE)
    source = re.sub(r"\s*from scratch", "", source, flags=re.IGNORECASE)
    source = re.sub(r"(?i)\b(?:the mishti is|it is)\s*\.", "", source)

    paragraphs = re.split(r"\n\s*\n", source)
    kept_paragraphs = []
    for paragraph in paragraphs:
        units = re.split(r"(?<=[.!?])\s+|\n", paragraph)
        kept_units = [unit.strip() for unit in units if unit.strip() and not contains_unconfirmed_claim(unit)]
        if kept_units:
            kept_paragraphs.append("\n".join(kept_units))
    return "\n\n".join(kept_paragraphs).strip()


def quarantine_template_response(
    template_id: str, text: str, canonical_text: Optional[str] = None
) -> str:
    """Apply a whole-template safe fallback where every claim is unverified."""
    if (
        str(template_id or "") == SUBSCRIPTION_TEMPLATE_ID
        and canonical_text
        and str(text or "").strip() == str(canonical_text).strip()
    ):
        # The subscription seed is the approved, durable copy.  Its confirmed
        # wording includes phrases also used by the generic claim scrubber;
        # exact equality must not reformat or partially remove that copy.
        return str(canonical_text)
    approved = CLIENT_CONFIRMED_EXACT_RESPONSES.get(str(template_id or ""))
    if approved:
        # Deterministic routing can append a secondary nudge or card caption,
        # and a stale durable override can contain an older or edited copy.
        # Keep only the approved versioned response.  This is quarantine, not
        # an approval of the edited text.
        return approved
    fallback = QUARANTINE_TEMPLATE_FALLBACKS.get(str(template_id or ""))
    # Non-allowlisted templates remain behind the existing whole-template
    # fallback, preserving the safety behavior even when edited wording does
    # not happen to match today's claim-pattern list.
    if fallback and text:
        return fallback
    return quarantine_unconfirmed_claims(text)

# Preserved in version history when the seed was already upgraded before the
# durable override migration ran. This is audit evidence only and is never
# returned to a customer.
LEGACY_RESPONSES = {
    "sugar_free_options": (
        "At Camellia Artisanal, all our Sondesh are handcrafted without artificial "
        "chemical sweeteners or corn syrups:\n\n• *Authentic Nolen Gur Norom Pak*: "
        "Made with 100% natural, unrefined date palm jaggery (Nolen Gur). It has a "
        "lower glycemic index and a mild, natural sweetness compared to refined white "
        "sugar sweets.\n• *Mild Sweetness Profile*: Because we make fresh small batches "
        "with pure dairy / A2 cow milk chhena, our Sondesh is noticeably light and "
        "subtly sweetened, never cloying.\n• *Diabetic Guidance*: While naturally "
        "sweetened with pure Nolen Gur or minimal unrefined cane sugar, it is not "
        "certified medical zero-calorie sugar-free. We recommend moderation or "
        "consulting with a physician for strict diabetic diets.\n\nWould you like "
        "to try our subtly sweetened Nolen Gur Norom Pak or Classic Sondesh?"
    ),
}


def _week(menu_data: Mapping[str, Any]) -> Mapping[str, Any]:
    weeks = menu_data.get("weekly_menus") or []
    return weeks[-1] if weeks else {}


def _weekly_text(title: str, emoji: str, entries: Iterable[Mapping[str, Any]], closing: str) -> str:
    lines = [f"*Camellia Artisanal — This Week's {title}* {emoji}",
             "_Current Week (Friday Lunch – Thursday Dinner)_\n"]
    for item in entries:
        name = item.get("item", "")
        quantity = item.get("quantity", "")
        price = item.get("price_inr", "")
        description = item.get("description", "")
        suffix = f" ({quantity})" if quantity else ""
        detail = f" • _{description}_" if description else ""
        lines.append(f"• *{name}*{suffix} — *₹{price}*{detail}")
    lines.extend([
        "\n📦 *Packaging*: 100% plastic-free, eco-friendly cornstarch boxes.",
        "🚚 *Delivery*: Across Delhi NCR from Sushant Arcade, Gurgaon (Courier on actuals).",
        f"\n{closing}",
    ])
    return "\n".join(lines)


CONFIRMED_COPY_TEMPLATES = (
    "jolbhora", "jolbhora_talgash_details", "sondesh_price_detailed",
    "aamar_mishti_collection", "signature_collection",
    "pack_size_250g", "pack_size_500g", "pack_size_1kg",
)


def canonical_override_responses(
    menu_data: Mapping[str, Any], templates: Optional[Iterable[Mapping[str, Any]]] = None
) -> Dict[str, str]:
    """Build the four safe corrections from the current canonical menu file."""
    week = _week(menu_data)
    vegetarian = _weekly_text(
        "Vegetarian Menu", "🌿", week.get("vegetarian", []),
        "Which vegetarian dishes would you like to order? 🙏",
    )
    nonvegetarian = _weekly_text(
        "Non-Vegetarian Menu", "🍗", week.get("non_vegetarian", []),
        "Which non-vegetarian dishes would you like to order? 🙏",
    )
    bentos = week.get("bengali_bento", []) or week.get("combos", [])
    bento_lines = ["*Camellia Artisanal — Bengali Bento Meals & Combos* 🍱", ""]
    for bento in bentos:
        includes = ", ".join(bento.get("includes", []))
        suffix = f" (Includes: {includes})" if includes else ""
        bento_lines.append(f"• *{bento.get('name', '')}* — *₹{bento.get('price_inr', '')}*{suffix}")
    bento_lines.extend([
        "\n📦 *Packaging*: 100% plastic-free, eco-friendly cornstarch boxes.",
        "🚚 *Delivery*: Across Delhi NCR from Sushant Arcade, Gurgaon.",
        "\nWhich Bento meal would you like to order?",
    ])
    bento_text = "\n".join(bento_lines)
    result = {
        "weekly_menu_veg": vegetarian,
        "weekly_menu_nonveg": nonvegetarian,
        "combo_bento": bento_text,
        "bento_details": bento_text,
        "sugar_free_options": (
            "Our Sondesh is made in small batches without artificial chemical sweeteners "
            "or corn syrups. We do not offer a certified sugar-free product. If you have "
            "a dietary or medical requirement, please tell us before ordering so our team "
            "can check the ingredients with you."
        ),
    }
    # These templates already contain the client-confirmed Jolbhora matrix.
    # Materialising them as managed overrides makes the Response Library and
    # its version history the same authoritative copy used by production.
    if templates is not None:
        for template in templates:
            template_id = str(template.get("id") or "")
            if template_id in CLIENT_CONFIRMED_EXACT_RESPONSES:
                source_copy = template.get("response") or ((template.get("responses") or [""])[0])
                if is_client_confirmed_exact_response(template_id, source_copy):
                    result[template_id] = source_copy
                elif contains_unconfirmed_claim(source_copy):
                    result[template_id] = quarantine_template_response(template_id, source_copy)
            elif template_id in CONFIRMED_COPY_TEMPLATES:
                source_copy = template.get("response") or ((template.get("responses") or [""])[0])
                result[template_id] = (
                    quarantine_template_response(template_id, source_copy)
                    if contains_unconfirmed_claim(source_copy) else source_copy
                )
            else:
                # Materialise a safe managed copy for every template that still
                # carries an unaudited sourcing/ingredient/availability or
                # process claim.  This keeps the Response Library searchable
                # and editable while ensuring the active runtime override is
                # the quarantined version.
                source_copy = template.get("response") or ((template.get("responses") or [""])[0])
                if contains_unconfirmed_claim(source_copy):
                    result[template_id] = quarantine_template_response(template_id, source_copy)
    return result


def apply_safe_content_remediations(
    database: Any,
    menu_data: Mapping[str, Any],
    templates: Iterable[Mapping[str, Any]],
    logger: Optional[Any] = None,
) -> Dict[str, str]:
    """Apply missing/legacy overrides, preserving any distinct operator edit.

    Returns a per-template action map (``created``, ``updated``, ``skipped``).
    The function is safe to call on every startup and in isolated test databases.
    """
    defaults = {str(t.get("id")): (t.get("response") or ((t.get("responses") or [""])[0]))
                for t in templates if t.get("id")}
    desired = canonical_override_responses(menu_data, templates)
    # Subscription copy has its own exact legacy-object migration below.  Do
    # not let the generic claim quarantine turn it into a different managed
    # response or overwrite a deliberate operator copy.
    desired.pop(SUBSCRIPTION_TEMPLATE_ID, None)
    actions: Dict[str, str] = {}
    unsafe_health_terms = (
        "glycemic", "diabetic-friendly", "sugar-safe", "healthier for diabetes",
        "suitable for diabetics",
    )

    # The subscription template is repaired only when the durable override is
    # the complete known legacy copy.  The repository template remains the
    # canonical source, so no customer-facing copy is duplicated here.
    subscription_default = defaults.get(SUBSCRIPTION_TEMPLATE_ID, "")
    if subscription_default:
        current = database.get_response_template_override(SUBSCRIPTION_TEMPLATE_ID)
        if current and str(current.get("status") or "ACTIVE").upper() == "ACTIVE":
            current_copy = (current.get("response") or "").strip()
            if current_copy == subscription_default.strip():
                actions[SUBSCRIPTION_TEMPLATE_ID] = "unchanged"
            elif is_known_legacy_subscription_response(current_copy):
                reason = (
                    SUBSCRIPTION_LIVE_STALE_REMEDIATION_REASON
                    if subscription_response_fingerprint(current_copy)
                    == KNOWN_LIVE_STALE_SUBSCRIPTION_SHA256
                    else SUBSCRIPTION_REMEDIATION_REASON
                )
                ok = database.save_response_template_override(
                    SUBSCRIPTION_TEMPLATE_ID,
                    subscription_default,
                    SUBSCRIPTION_REMEDIATION_ACTOR,
                    change_reason=reason,
                    previous_response=current_copy,
                )
                actions[SUBSCRIPTION_TEMPLATE_ID] = "updated" if ok else "failed"
            else:
                actions[SUBSCRIPTION_TEMPLATE_ID] = "skipped_operator_override"
        elif current:
            actions[SUBSCRIPTION_TEMPLATE_ID] = "skipped_operator_override"
        else:
            # With no durable override, the shipped template is already the
            # authoritative runtime copy; avoid manufacturing a DB row.
            actions[SUBSCRIPTION_TEMPLATE_ID] = "unchanged"

    for template_id, response in desired.items():
        current = database.get_response_template_override(template_id)
        default = defaults.get(template_id, "")

        # Only the exact legacy fallback generated by this subsystem is
        # eligible for automatic Phase 3B upgrade.  A distinct operator/client
        # response remains intact and auditable.
        approved = CLIENT_CONFIRMED_EXACT_RESPONSES.get(template_id)
        if approved is not None:
            if current and str(current.get("status") or "ACTIVE").upper() == "ACTIVE":
                current_copy = (current.get("response") or "").strip()
                if current_copy == approved.strip():
                    actions[template_id] = "unchanged"
                    continue
                if current_copy != QUARANTINE_TEMPLATE_FALLBACKS.get(template_id, "").strip():
                    actions[template_id] = "skipped"
                    continue
                actor = (
                    HOME_TABLE_REMEDIATION_ACTOR
                    if template_id in HOME_TABLE_TEMPLATE_IDS
                    else PHASE3B_REMEDIATION_ACTOR
                )
                reason = (
                    HOME_TABLE_REMEDIATION_REASON
                    if template_id in HOME_TABLE_TEMPLATE_IDS
                    else PHASE3B_REMEDIATION_REASON
                )
                ok = database.save_response_template_override(
                    template_id, approved, actor, change_reason=reason,
                )
                actions[template_id] = "updated" if ok else "failed"
                continue
            if current:
                actions[template_id] = "skipped"
                continue
        if current and str(current.get("status") or "ACTIVE").upper() == "ACTIVE":
            if (current.get("response") or "").strip() == response.strip():
                actions[template_id] = "unchanged"
                continue
            # Never overwrite a client-authored copy.  The audit can be rerun
            # after confirmation without destroying a deliberate edit.
            current_copy = (current.get("response") or "").strip()
            unsafe_existing = (
                (template_id == "sugar_free_options" and any(
                    term in current_copy.lower() for term in unsafe_health_terms
                ))
                or contains_unconfirmed_claim(current_copy)
            )
            managed_existing = str(current.get("updated_by") or "") == ACTOR
            if current_copy != default.strip() and not unsafe_existing and not managed_existing:
                actions[template_id] = "skipped"
                continue
            ok = database.save_response_template_override(
                template_id, response, ACTOR, change_reason=REASON,
                previous_response=LEGACY_RESPONSES.get(template_id, default),
            )
            actions[template_id] = "updated" if ok else "failed"
            continue
        actor = (
            HOME_TABLE_REMEDIATION_ACTOR
            if template_id in HOME_TABLE_TEMPLATE_IDS
            else ACTOR
        )
        reason = (
            HOME_TABLE_REMEDIATION_REASON
            if template_id in HOME_TABLE_TEMPLATE_IDS
            else REASON
        )
        ok = database.save_response_template_override(
            template_id, response, actor, change_reason=reason,
            previous_response=LEGACY_RESPONSES.get(template_id, default),
        )
        actions[template_id] = "created" if ok else "failed"
    if logger:
        for template_id, action in actions.items():
            if action in {"created", "updated", "skipped", "skipped_operator_override"}:
                logger.info("Content-integrity remediation %s: %s", template_id, action)
    return actions


def apply_confirmed_menu_facts(menu_path: str, logger: Optional[Any] = None) -> bool:
    """Merge only the newly confirmed facts into the durable menu file.

    Weekly customer edits are left untouched.  The merge is intentionally
    narrow: Jolbhora pricing/pieces, Bento tiers, and the confirmation record.
    """
    path = Path(menu_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    changed = False
    collections = data.get("collections") or {}
    targets = {
        "aamar_mishti": {"250g": (6, 750), "500g": (12, 1400), "1kg": (24, 2500)},
        "signature_collection": {"250g": (6, 900), "500g": (12, 1600), "1kg": (24, 2900)},
    }
    for collection_id, packs in targets.items():
        for product in (collections.get(collection_id, {}).get("products") or []):
            if str(product.get("item") or "").strip().lower() != "jolbhora":
                continue
            prices = product.setdefault("prices", [])
            by_pack = {str(row.get("pack_size")): row for row in prices}
            for pack, (pieces, price) in packs.items():
                row = by_pack.get(pack)
                if row is None:
                    prices.append({"pack_size": pack, "pieces": pieces, "price_inr": price})
                    changed = True
                else:
                    if row.get("pieces") != pieces or row.get("price_inr") != price:
                        row.update({"pieces": pieces, "price_inr": price})
                        changed = True
    confirmed_bentos = [
        {"name": "Veg Bento", "price_inr": 400, "includes": ["6 Luchi", "Chholar Daal", "Tomato Aamsottwo Chatni", "1 Classic Aamar Sondesh"]},
        {"name": "Non-Veg Bento", "price_inr": 700, "includes": ["6 Luchi", "Mangsho (Mutton Kosha)", "Tomato Aamsottwo Chatni", "1 Classic Aamar Sondesh"]},
    ]
    weeks = data.get("weekly_menus") or []
    if weeks and weeks[-1].get("bengali_bento") != confirmed_bentos:
        weeks[-1]["bengali_bento"] = confirmed_bentos
        changed = True
    order_types = data.setdefault("order_types", {})
    bento_type = order_types.setdefault("bengali_bento", {})
    if bento_type.get("tiers") != [
        {"tier_name": "Vegetarian Bento", "price_inr": 400, "includes": confirmed_bentos[0]["includes"]},
        {"tier_name": "Non-Vegetarian Bento", "price_inr": 700, "includes": confirmed_bentos[1]["includes"]},
    ]:
        bento_type["tiers"] = [
            {"tier_name": "Vegetarian Bento", "price_inr": 400, "includes": confirmed_bentos[0]["includes"]},
            {"tier_name": "Non-Vegetarian Bento", "price_inr": 700, "includes": confirmed_bentos[1]["includes"]},
        ]
        bento_type.pop("status", None)
        changed = True
    meta = data.setdefault("meta", {})
    facts = meta.setdefault("client_confirmed_facts", {})
    if facts.get("confirmed_at") != "2026-09-13":
        facts.update({"confirmed_at": "2026-09-13", "jolbhora_prices_inr": {
            "aamar_mishti": {"250g": 750, "500g": 1400, "1kg": 2500},
            "signature": {"250g": 900, "500g": 1600, "1kg": 2900},
        }, "jolbhora_pieces": {"250g": 6, "500g": 12, "1kg": 24}})
        changed = True
    if changed:
        try:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            if logger:
                logger.info("Applied client-confirmed Jolbhora/Bento facts to %s", path)
            return True
        except Exception:
            return False
    return False
