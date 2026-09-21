"""Canonical mapping from understood meaning to approved business knowledge.

A knowledge record is addressed by *meaning* — an intent plus the entities that
narrow it — never by the customer's wording. Nothing here stores a phrasing a
customer might use, and nothing here restates a price, a policy, a product
description or a contact detail: the approved copy stays in its existing
runtime source (`qa_templates.json`, rendered with live menu data), and the
record points at it. Migration therefore cannot fork the truth.

    meaning  ->  KnowledgeRecord  ->  approved copy + deterministic actions

`SOURCE_MAP` below names the one authoritative source for each business domain,
so a fact that also appears somewhere else has a documented precedence rather
than a silent winner.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from camellia.understanding import BusinessAction, Intent, UnderstandingResult

# The one preferred source of truth per business domain. Where the same fact is
# reachable from more than one place, the first entry wins and the others are
# presentation or convenience layers over it.
SOURCE_MAP: Dict[str, Dict[str, Any]] = {
    "weekly_menu": {
        "source": "data/menu.json > menu_dates.resolve_current_menu(weekly_menus)",
        "rendered_by": "responder._format_dynamic_response(weekly_menu_veg|weekly_menu_nonveg)",
        "also_appears_in": ["weekly flyer uploads (weekly_flyer.py)"],
        "precedence": "menu.json is authoritative for dish names and prices; a "
                      "flyer upload supersedes it only for the week it covers.",
    },
    "prices": {
        "source": "data/menu.json (per-item price_inr) and qa_templates price tables",
        "rendered_by": "order_calculator for order totals",
        "precedence": "menu.json wins for weekly dishes; the approved Sondesh "
                      "price tables win for the standing catalogue.",
    },
    "products": {"source": "data/qa_templates.json product templates"},
    "flavours": {"source": "data/qa_templates.json flavour templates"},
    "pack_sizes": {"source": "data/qa_templates.json pack_size_* templates"},
    "bento": {"source": "data/qa_templates.json > bento_details"},
    "delivery": {"source": "data/qa_templates.json > delivery_area / delivery_out_of_area"},
    "opening_hours": {"source": "data/qa_templates.json > opening_hours"},
    "delivery_time": {"source": "data/qa_templates.json > savoury_delivery_time / sondesh_dispatch_time"},
    "storage": {"source": "data/qa_templates.json > GUARD_shelf_life_storage"},
    "packaging": {"source": "data/qa_templates.json > packaging"},
    "subscriptions": {"source": "data/qa_templates.json > subscription_details / subscription_booking_steps"},
    "event_bulk": {"source": "human handover only — no approved self-serve quote"},
    "contact": {"source": "data/qa_templates.json > GUARD_staff_names_manager_contact"},
    "allergens": {"source": "data/qa_templates.json > GUARD_allergen_nutfree_glutenfree"},
    "dietary": {"source": "data/qa_templates.json > GUARD_dietary_suitability"},
    "availability": {"source": "human handover only — stock is not tracked in any runtime source"},
    "payment": {"source": "data/qa_templates.json > GUARD_payment_method"},
    "ordering": {"source": "data/qa_templates.json > how_to_order / alacarte_prebooking_steps"},
}

# Intents whose honest answer is "a person will confirm this". They are listed
# once, here, so every path treats them the same way.
HUMAN_ONLY_INTENTS = frozenset({
    Intent.UNKNOWN.value,
    Intent.BUSINESS_POLICY_UNKNOWN.value,
    Intent.ESCALATION.value,
    Intent.EVENT_ORDER.value,
    Intent.BULK_ORDER.value,
    Intent.DISCOUNT_REQUEST.value,
    Intent.AVAILABILITY_QUERY.value,
})


@dataclass(frozen=True)
class ApprovedKnowledge:
    knowledge_id: str
    intent: str
    template_id: Optional[str]
    approved_response: str
    source_ref: str
    actions: Tuple[str, ...]
    domain: str = ""
    sufficient: bool = True


@dataclass(frozen=True)
class KnowledgeRecord:
    """One approved business fact, addressed by meaning rather than wording."""

    knowledge_id: str
    domain: str
    intents: Tuple[str, ...]
    template_id: str
    actions: Tuple[BusinessAction, ...]
    # Narrows a record to the entity shape it answers, e.g. product "jolbhora".
    entity_match: Optional[Callable[[Dict[str, str]], bool]] = None
    priority: int = 0


def _product(*needles: str) -> Callable[[Dict[str, str]], bool]:
    return lambda entities: any(needle in entities.get("product", "") for needle in needles)


def _no_product() -> Callable[[Dict[str, str]], bool]:
    return lambda entities: not entities.get("product")


def _generic_product() -> Callable[[Dict[str, str]], bool]:
    return lambda entities: (
        not entities.get("product")
        or any(word in entities.get("product", "") for word in
               ("sondesh", "sweet", "mishti", "mithai", "sandesh"))
    )


def _subtopic_domain(subtopic: str) -> Optional[str]:
    from camellia.semantic_fallback import subtopic_domain

    return subtopic_domain(subtopic)


def _subtopic(*names: str) -> Callable[[Dict[str, str]], bool]:
    return lambda entities: entities.get("subtopic", "") in names


def _menu_type(*values: str) -> Callable[[Dict[str, str]], bool]:
    return lambda entities: entities.get("menu_type", "") in values


def _pack_size(*sizes: str) -> Callable[[Dict[str, str]], bool]:
    return lambda entities: any(size in entities.get("pack_size", "") for size in sizes)


SEND = BusinessAction.SEND_RESPONSE
MENU = BusinessAction.SEND_MENU
PRICE = BusinessAction.SEND_PRICE_SEQUENCE

# Ordered by how specific each record is: the first matching record answers.
CATALOGUE: Tuple[KnowledgeRecord, ...] = (
    # --- menu ------------------------------------------------------------
    KnowledgeRecord("menu:weekly_vegetarian", "weekly_menu", (Intent.MENU_REQUEST.value,),
                    "weekly_menu_veg", (MENU,), _menu_type("vegetarian"), priority=20),
    KnowledgeRecord("menu:weekly_non_vegetarian", "weekly_menu", (Intent.MENU_REQUEST.value,),
                    "weekly_menu_nonveg", (MENU,), _menu_type("non_veg", "non_vegetarian"), priority=20),
    KnowledgeRecord("menu:overview", "weekly_menu", (Intent.MENU_REQUEST.value,),
                    "know_more", (MENU,)),

    # --- products, flavours, pack sizes ----------------------------------
    KnowledgeRecord("product:collection_difference", "products",
                    (Intent.PRODUCT_INFORMATION.value,),
                    "GUARD_collection_differentiation", (SEND,),
                    _subtopic("collection_difference"), priority=30),
    KnowledgeRecord("product:jolbhora", "products", (Intent.PRODUCT_INFORMATION.value,),
                    "jolbhora", (SEND,), _product("jolbhora"), priority=20),
    KnowledgeRecord("product:kanchagolla", "products", (Intent.PRODUCT_INFORMATION.value,),
                    "kanchagolla", (SEND,), _product("kanchagolla"), priority=20),
    KnowledgeRecord("product:nolen_gur", "flavours", (Intent.PRODUCT_INFORMATION.value,),
                    "sondesh_nolen_gur", (SEND,), _product("nolen gur"), priority=20),
    KnowledgeRecord("product:mahua", "products", (Intent.PRODUCT_INFORMATION.value,),
                    "mahua_signature_special", (SEND,), _product("mahua"), priority=20),
    KnowledgeRecord("product:aamar_mishti", "products", (Intent.PRODUCT_INFORMATION.value,),
                    "aamar_mishti_collection", (SEND,), _product("aamar"), priority=20),
    KnowledgeRecord("product:signature_collection", "products", (Intent.PRODUCT_INFORMATION.value,),
                    "signature_collection", (SEND,), _product("signature"), priority=20),
    KnowledgeRecord("product:bento", "bento", (Intent.PRODUCT_INFORMATION.value,),
                    "bento_details", (SEND,), _product("bento", "bengali bento"), priority=20),
    KnowledgeRecord("product:classic_rose", "flavours", (Intent.PRODUCT_INFORMATION.value,),
                    "sondesh_classic_rose", (SEND,), _product("rose", "classic"), priority=15),
    KnowledgeRecord("product:mango_kesar", "flavours", (Intent.PRODUCT_INFORMATION.value,),
                    "sondesh_mango_kesar", (SEND,),
                    _product("mango", "kesar", "vanilla", "cinnamon", "dalchini"), priority=15),
    KnowledgeRecord("product:overview", "products", (Intent.PRODUCT_INFORMATION.value,),
                    "product_overview", (SEND,), _generic_product()),

    KnowledgeRecord("pack_size:250g", "pack_sizes", (Intent.PACK_SIZE_QUESTION.value,),
                    "pack_size_250g", (SEND,), _pack_size("250"), priority=20),
    KnowledgeRecord("pack_size:500g", "pack_sizes", (Intent.PACK_SIZE_QUESTION.value,),
                    "pack_size_500g", (SEND,), _pack_size("500"), priority=20),
    KnowledgeRecord("pack_size:1kg", "pack_sizes", (Intent.PACK_SIZE_QUESTION.value,),
                    "pack_size_1kg", (SEND,), _pack_size("1kg", "1 kg", "1000"), priority=20),

    # --- prices ----------------------------------------------------------
    KnowledgeRecord("price:jolbhora", "prices", (Intent.PRODUCT_PRICE.value,),
                    "jolbhora", (PRICE,), _product("jolbhora"), priority=20),
    KnowledgeRecord("price:kanchagolla", "prices", (Intent.PRODUCT_PRICE.value,),
                    "kanchagolla", (PRICE,), _product("kanchagolla"), priority=20),
    KnowledgeRecord("price:nolen_gur", "prices", (Intent.PRODUCT_PRICE.value,),
                    "sondesh_nolen_gur", (PRICE,), _product("nolen gur"), priority=20),
    KnowledgeRecord("price:mahua", "prices", (Intent.PRODUCT_PRICE.value,),
                    "mahua_signature_special", (PRICE,), _product("mahua"), priority=20),
    KnowledgeRecord("price:aamar_mishti", "prices", (Intent.PRODUCT_PRICE.value,),
                    "aamar_mishti_collection", (PRICE,), _product("aamar"), priority=20),
    KnowledgeRecord("price:signature_collection", "prices", (Intent.PRODUCT_PRICE.value,),
                    "signature_collection", (PRICE,), _product("signature"), priority=20),
    # A named flavour ("Classic price", "Kesar rate") is a price question the
    # same way naming the collection is — these two templates already carry
    # the price table for exactly these flavours; they were wired for
    # product_information only, so a price-worded question about them fell
    # through to the generic Sondesh catalogue matcher below, which does not
    # accept a bare flavour name.
    KnowledgeRecord("price:classic_rose", "flavours", (Intent.PRODUCT_PRICE.value,),
                    "sondesh_classic_rose", (PRICE,), _product("rose", "classic"), priority=20),
    KnowledgeRecord("price:mango_kesar", "flavours", (Intent.PRODUCT_PRICE.value,),
                    "sondesh_mango_kesar", (PRICE,),
                    _product("mango", "kesar", "vanilla", "cinnamon", "dalchini"), priority=20),
    KnowledgeRecord("price:bento", "bento", (Intent.PRODUCT_PRICE.value,),
                    "bento_details", (PRICE,), _product("bento"), priority=20),
    KnowledgeRecord("price:sondesh_catalogue", "prices", (Intent.PRODUCT_PRICE.value,),
                    "sondesh_price_detailed", (PRICE,),
                    lambda entities: bool(entities.get("product")) and any(
                        word in entities.get("product", "")
                        for word in ("sondesh", "sweet", "mishti", "mithai")
                    )),

    # --- delivery, ordering, policies ------------------------------------
    KnowledgeRecord("delivery:area", "delivery", (Intent.DELIVERY_QUERY.value,),
                    "delivery_area", (SEND,)),
    KnowledgeRecord("delivery:opening_hours", "opening_hours",
                    (Intent.DELIVERY_TIME_QUESTION.value,),
                    "opening_hours", (SEND,), _subtopic("opening_hours"), priority=30),
    KnowledgeRecord("delivery:time_savoury", "delivery_time", (Intent.DELIVERY_TIME_QUESTION.value,),
                    "savoury_delivery_time", (SEND,),
                    lambda entities: "bento" in entities.get("product", "")
                    or "meal" in entities.get("product", ""), priority=20),
    KnowledgeRecord("delivery:time_sondesh", "delivery_time", (Intent.DELIVERY_TIME_QUESTION.value,),
                    "sondesh_dispatch_time", (SEND,)),
    KnowledgeRecord("ordering:alacarte", "ordering", (Intent.ORDER_PROCESS.value,),
                    "alacarte_prebooking_steps", (SEND,), _subtopic("alacarte"), priority=20),
    KnowledgeRecord("ordering:process", "ordering", (Intent.ORDER_PROCESS.value,),
                    "how_to_order", (SEND,)),
    KnowledgeRecord("policy:storage", "storage", (Intent.STORAGE_QUESTION.value,),
                    "GUARD_shelf_life_storage", (SEND,)),
    KnowledgeRecord("policy:allergens", "allergens", (Intent.ALLERGEN_QUESTION.value,),
                    "GUARD_allergen_nutfree_glutenfree", (SEND,)),
    KnowledgeRecord("policy:dietary", "dietary", (Intent.DIETARY_QUESTION.value,),
                    "GUARD_dietary_suitability", (SEND,)),
    KnowledgeRecord("policy:ingredients", "products", (Intent.INGREDIENTS_QUESTION.value,),
                    "freshness_ingredients", (SEND,)),
    KnowledgeRecord("policy:packaging", "packaging", (Intent.PACKAGING_QUESTION.value,),
                    "packaging", (SEND,)),
    KnowledgeRecord("policy:payment", "payment", (Intent.PAYMENT_QUESTION.value,),
                    "GUARD_payment_method", (SEND,)),
    KnowledgeRecord("contact:team", "contact", (Intent.CONTACT_QUESTION.value,),
                    "GUARD_staff_names_manager_contact", (SEND,)),

    # --- subscriptions ---------------------------------------------------
    # The business facts are canonical; which turn of the conversation the
    # customer is on stays deterministic state in the responder.
    KnowledgeRecord("subscription:plans", "subscriptions", (Intent.SUBSCRIPTION_QUERY.value,),
                    "subscription_details", (SEND,)),
    KnowledgeRecord("subscription:booking_steps", "subscriptions",
                    (Intent.SUBSCRIPTION_BOOKING.value,),
                    "subscription_booking_steps", (SEND,)),
    KnowledgeRecord("subscription:change_notice", "subscriptions",
                    (Intent.SUBSCRIPTION_CHANGE.value,),
                    "subscription_booking_steps", (SEND,)),
)

# Every template the catalogue can serve, for the compatibility path to check
# whether an answer it selected is backed by canonical knowledge.
CANONICAL_TEMPLATE_IDS: Dict[str, Tuple[str, str]] = {
    record.template_id: (record.knowledge_id, record.domain) for record in CATALOGUE
}


@dataclass
class KnowledgeResolution:
    items: List[ApprovedKnowledge] = field(default_factory=list)
    sufficient_by_intent: Dict[str, bool] = field(default_factory=dict)
    missing_intents: List[str] = field(default_factory=list)

    @property
    def fully_sufficient(self) -> bool:
        return bool(self.sufficient_by_intent) and all(self.sufficient_by_intent.values())

    @property
    def partially_sufficient(self) -> bool:
        """Some meaning was answered from approved knowledge, some was not."""
        values = self.sufficient_by_intent.values()
        return any(values) and not all(values)


class ApprovedKnowledgeService:
    """Resolves intent/entity pairs without matching customer wording."""

    PRICE_CLARIFICATION = "Would you like prices for Sondesh or Bengali Meals?"

    def domains(self) -> Dict[str, List[str]]:
        """Business domains the catalogue covers, for reporting and tests."""
        coverage: Dict[str, List[str]] = {}
        for record in CATALOGUE:
            coverage.setdefault(record.domain, []).append(record.knowledge_id)
        return coverage

    def is_canonical_template(self, template_id: Optional[str]) -> bool:
        return bool(template_id) and template_id in CANONICAL_TEMPLATE_IDS

    def knowledge_id_for_intent(
        self, intent: str, template_id: Optional[str],
    ) -> Optional[str]:
        """The record that makes this template the answer to this intent."""
        for record in CATALOGUE:
            if record.template_id == template_id and intent in record.intents:
                return record.knowledge_id
        return None

    def knowledge_id_for_template(self, template_id: Optional[str]) -> Optional[str]:
        entry = CANONICAL_TEMPLATE_IDS.get(template_id or "")
        return entry[0] if entry else None

    def _item(
        self,
        record: KnowledgeRecord,
        intent: str,
        templates: Dict[str, Dict[str, Any]],
    ) -> Optional[ApprovedKnowledge]:
        template = templates.get(record.template_id)
        if not template or template.get("escalate"):
            return None
        response = template.get("response")
        if not response:
            responses = template.get("responses") or []
            response = responses[0] if responses else ""
        if not response:
            return None
        return ApprovedKnowledge(
            knowledge_id=f"approved:{record.template_id}",
            intent=intent,
            template_id=record.template_id,
            approved_response=response,
            source_ref=template.get("source_ref") or f"qa_templates.json#{record.template_id}",
            actions=tuple(action.value for action in record.actions),
            domain=record.domain,
        )

    def _record_for(self, intent: str, entities: Dict[str, str]) -> Optional[KnowledgeRecord]:
        candidates = [
            record for record in CATALOGUE
            if intent in record.intents
            and (record.entity_match is None or record.entity_match(entities))
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda record: -record.priority)[0]

    def resolve(
        self,
        understood: UnderstandingResult,
        templates: Iterable[Dict[str, Any]],
        *,
        serviceable_location: Optional[bool] = None,
    ) -> KnowledgeResolution:
        by_id = {item.get("id"): item for item in templates if item.get("id")}
        raw_entities = understood.entities or {}
        entities = {
            key: str(value).lower()
            for key, value in raw_entities.items()
            if isinstance(value, str)
        }
        topic = entities.get("topic", "")
        result = KnowledgeResolution()

        # A topic the catalogue has no record for is a knowledge gap, whatever
        # it happens to be. Nothing is coerced into the nearest domain — but
        # only the part of the message that carries the unknown topic becomes
        # a gap: "do you deliver to Delhi and can I smoke there" still gets its
        # delivery answer, with the smoking half handed to a person.
        uncovered_topic = bool(topic) and not self._topic_is_covered(topic)

        for intent in dict.fromkeys(understood.intents):
            item: Optional[ApprovedKnowledge] = None

            if uncovered_topic and intent in {
                Intent.UNKNOWN.value, Intent.BUSINESS_POLICY_UNKNOWN.value,
            }:
                result.sufficient_by_intent[intent] = False
                result.missing_intents.append(intent)
                continue
            if intent == Intent.GREETING.value:
                record = KnowledgeRecord("conversation:greeting", "conversation",
                                         (intent,), "greeting", (SEND,))
                item = self._item(record, intent, by_id)
            elif intent == Intent.PRODUCT_INFORMATION.value and any(
                value in understood.intents
                for value in (Intent.EVENT_ORDER.value, Intent.BULK_ORDER.value)
            ):
                # In an event enquiry the product mention is context for the
                # human quote, not a separate request for a catalogue.
                result.sufficient_by_intent[intent] = True
                continue
            elif intent == Intent.PRODUCT_PRICE.value and not entities.get("product"):
                item = ApprovedKnowledge(
                    knowledge_id="conversation:price_clarification",
                    intent=intent,
                    template_id="price_clarify_product",
                    approved_response=self.PRICE_CLARIFICATION,
                    source_ref="responder.price_clarification",
                    actions=(SEND.value,),
                    domain="prices",
                )
            elif intent == Intent.DELIVERY_QUERY.value:
                template_id = "delivery_out_of_area" if serviceable_location is False else "delivery_area"
                action = (
                    BusinessAction.CONFIRM_DELIVERY_AREA if entities.get("location")
                    else BusinessAction.ASK_DELIVERY_AREA
                )
                record = KnowledgeRecord(
                    f"delivery:{'out_of_area' if serviceable_location is False else 'area'}",
                    "delivery", (intent,), template_id, (action,),
                )
                item = self._item(record, intent, by_id)
            else:
                record = self._record_for(intent, entities)
                if record:
                    item = self._item(record, intent, by_id)

            sufficient = item is not None and intent not in HUMAN_ONLY_INTENTS
            # Matching a domain does not mean the answer covers every corner of
            # it. "Do you gift wrap?" is a packaging question, and the approved
            # packaging copy is about food-safe delivery boxes — it says nothing
            # about wrapping, so the honest result is a gap, not that copy.
            # Only the domain the subtopic actually narrows is checked, so
            # "Jolbhora's price and do you gift wrap?" still quotes the price.
            subtopic = entities.get("subtopic")
            if subtopic and item and _subtopic_domain(subtopic) not in (None, item.domain):
                subtopic = None
            if sufficient and item and subtopic and not self._copy_covers_subtopic(
                subtopic, item.approved_response,
            ):
                sufficient = False
                item = None
            result.sufficient_by_intent[intent] = sufficient
            if sufficient and item:
                result.items.append(item)
            else:
                result.missing_intents.append(intent)

        return result

    @staticmethod
    def _copy_covers_subtopic(subtopic: str, approved_response: str) -> bool:
        """Ask the approved copy itself whether it addresses this subtopic.

        Nothing is maintained twice: if the client's wording starts mentioning
        gift wrapping tomorrow, the subtopic becomes supported the same day,
        and if it never does, no amount of domain matching will imply it.
        """
        from camellia.semantic_fallback import subtopic_evidence

        evidence = subtopic_evidence(subtopic)
        if not evidence:
            # An unfamiliar subtopic is not evidence of coverage.
            return False
        haystack = " ".join((approved_response or "").lower().split())
        return any(phrase in haystack for phrase in evidence)

    def _topic_is_covered(self, topic: str) -> bool:
        """A free-text topic is only trusted when a record's domain owns it.

        Entities the model can fill freely ("valet parking", "washrooms",
        "wheelchair access") must not select the closest-sounding domain, so a
        topic counts only when it names a domain the catalogue actually has.
        """
        words = {word for word in topic.replace("-", " ").split() if len(word) > 2}
        if not words:
            return False
        vocabulary = set()
        for record in CATALOGUE:
            vocabulary.update(record.domain.split("_"))
            vocabulary.update(record.knowledge_id.replace(":", "_").split("_"))
        return bool(words & vocabulary)


approved_knowledge = ApprovedKnowledgeService()
