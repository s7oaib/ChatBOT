"""Canonical mapping from understood meaning to approved business knowledge."""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from camellia.understanding import BusinessAction, Intent, UnderstandingResult


@dataclass(frozen=True)
class ApprovedKnowledge:
    knowledge_id: str
    intent: str
    template_id: Optional[str]
    approved_response: str
    source_ref: str
    actions: tuple[str, ...]
    sufficient: bool = True


@dataclass
class KnowledgeResolution:
    items: List[ApprovedKnowledge] = field(default_factory=list)
    sufficient_by_intent: Dict[str, bool] = field(default_factory=dict)
    missing_intents: List[str] = field(default_factory=list)

    @property
    def fully_sufficient(self) -> bool:
        return bool(self.sufficient_by_intent) and all(self.sufficient_by_intent.values())


class ApprovedKnowledgeService:
    """Resolves intent/entity pairs without matching customer wording.

    Facts remain in the existing approved runtime sources. This service owns
    only the canonical intent-to-knowledge mapping, so migration does not copy
    prices, policies, contact details, or product descriptions into another
    file that could drift.
    """

    PRICE_CLARIFICATION = "Would you like prices for Sondesh or Bengali Meals?"

    def _item(
        self,
        intent: str,
        template_id: str,
        templates: Dict[str, Dict[str, Any]],
        actions: Iterable[BusinessAction],
    ) -> Optional[ApprovedKnowledge]:
        template = templates.get(template_id)
        if not template or template.get("escalate"):
            return None
        response = template.get("response")
        if not response:
            responses = template.get("responses") or []
            response = responses[0] if responses else ""
        if not response:
            return None
        return ApprovedKnowledge(
            knowledge_id=f"approved:{template_id}",
            intent=intent,
            template_id=template_id,
            approved_response=response,
            source_ref=template.get("source_ref") or f"qa_templates.json#{template_id}",
            actions=tuple(action.value for action in actions),
        )

    def resolve(
        self,
        understood: UnderstandingResult,
        templates: Iterable[Dict[str, Any]],
        *,
        serviceable_location: Optional[bool] = None,
    ) -> KnowledgeResolution:
        by_id = {item.get("id"): item for item in templates if item.get("id")}
        entities = understood.entities or {}
        product = str(entities.get("product") or "").lower()
        menu_type = str(entities.get("menu_type") or "").lower().replace("-", "_")
        topic = str(entities.get("topic") or "").lower()
        result = KnowledgeResolution()

        # An explicit topic outside the approved catalogue must not be coerced
        # into the closest-sounding known topic (the former parking/packaging
        # failure). It is a real knowledge gap.
        if "parking" in topic or "valet" in topic:
            for intent in dict.fromkeys(understood.intents):
                result.sufficient_by_intent[intent] = False
                result.missing_intents.append(intent)
            return result

        for intent in dict.fromkeys(understood.intents):
            item: Optional[ApprovedKnowledge] = None
            if intent == Intent.GREETING.value:
                item = self._item(intent, "greeting", by_id, [BusinessAction.SEND_RESPONSE])
            elif intent == Intent.MENU_REQUEST.value:
                template_id = (
                    "weekly_menu_nonveg" if menu_type in {"non_veg", "non_vegetarian"} else
                    "weekly_menu_veg" if menu_type == "vegetarian" else
                    "know_more"
                )
                item = self._item(intent, template_id, by_id, [BusinessAction.SEND_MENU])
            elif intent == Intent.PRODUCT_INFORMATION.value:
                if any(value in understood.intents for value in (
                    Intent.EVENT_ORDER.value, Intent.BULK_ORDER.value,
                )):
                    # In an event enquiry the product mention is context for
                    # the human quote, not a separate request for a catalogue.
                    result.sufficient_by_intent[intent] = True
                    continue
                template_id = (
                    "jolbhora" if "jolbhora" in product else
                    "aamar_mishti_collection" if "aamar" in product else
                    "kanchagolla" if "kanchagolla" in product else
                    "sondesh_nolen_gur" if "nolen gur" in product else
                    "mahua_signature_special" if "mahua" in product else
                    "bento_details" if "bento" in product else
                    "product_overview" if not product or any(
                        value in product for value in ("sondesh", "sweet", "mishti", "mithai")
                    ) else ""
                )
                if template_id:
                    item = self._item(intent, template_id, by_id, [BusinessAction.SEND_RESPONSE])
            elif intent == Intent.PRODUCT_PRICE.value:
                if not product:
                    item = ApprovedKnowledge(
                        knowledge_id="conversation:price_clarification",
                        intent=intent,
                        template_id="price_clarify_product",
                        approved_response=self.PRICE_CLARIFICATION,
                        source_ref="responder.price_clarification",
                        actions=(BusinessAction.SEND_RESPONSE.value,),
                    )
                else:
                    template_id = (
                        "jolbhora" if "jolbhora" in product else
                        "kanchagolla" if "kanchagolla" in product else
                        "sondesh_nolen_gur" if "nolen gur" in product else
                        "mahua_signature_special" if "mahua" in product else
                        "aamar_mishti_collection" if "aamar" in product else
                        "sondesh_price_detailed" if any(
                            value in product for value in ("sondesh", "sweet", "mishti", "mithai")
                        ) else ""
                    )
                    if template_id:
                        item = self._item(intent, template_id, by_id, [BusinessAction.SEND_PRICE_SEQUENCE])
            elif intent == Intent.DELIVERY_QUERY.value:
                template_id = "delivery_out_of_area" if serviceable_location is False else "delivery_area"
                action = BusinessAction.CONFIRM_DELIVERY_AREA if entities.get("location") else BusinessAction.ASK_DELIVERY_AREA
                item = self._item(intent, template_id, by_id, [action])
            elif intent == Intent.ORDER_PROCESS.value:
                item = self._item(intent, "how_to_order", by_id, [BusinessAction.SEND_RESPONSE])
            elif intent == Intent.STORAGE_QUESTION.value:
                item = self._item(intent, "GUARD_shelf_life_storage", by_id, [BusinessAction.SEND_RESPONSE])
            elif intent == Intent.ALLERGEN_QUESTION.value:
                item = self._item(intent, "GUARD_allergen_nutfree_glutenfree", by_id, [BusinessAction.SEND_RESPONSE])
            elif intent == Intent.INGREDIENTS_QUESTION.value:
                item = self._item(intent, "freshness_ingredients", by_id, [BusinessAction.SEND_RESPONSE])
            elif intent == Intent.PACKAGING_QUESTION.value:
                item = self._item(intent, "packaging", by_id, [BusinessAction.SEND_RESPONSE])
            elif intent == Intent.PAYMENT_QUESTION.value:
                item = self._item(intent, "GUARD_payment_method", by_id, [BusinessAction.SEND_RESPONSE])
            elif intent == Intent.CONTACT_QUESTION.value:
                item = self._item(intent, "GUARD_staff_names_manager_contact", by_id, [BusinessAction.SEND_RESPONSE])

            sufficient = item is not None and intent not in {
                Intent.UNKNOWN.value,
                Intent.ESCALATION.value,
                Intent.EVENT_ORDER.value,
                Intent.BULK_ORDER.value,
                Intent.DISCOUNT_REQUEST.value,
                Intent.AVAILABILITY_QUERY.value,
            }
            result.sufficient_by_intent[intent] = sufficient
            if sufficient and item:
                result.items.append(item)
            else:
                result.missing_intents.append(intent)

        return result


approved_knowledge = ApprovedKnowledgeService()
