"""Focused contract tests for the customer-request-only Instagram order draft."""

import uuid
from urllib.parse import unquote
from unittest.mock import patch

import pytest

from camellia import app as app_module
from camellia.config import config
from camellia.database import Database, db
from camellia.order_intake import (
    ORDER_DRAFT_INCOMPLETE,
    ORDER_DRAFT_READY,
    instruction_message,
    is_order_request,
    new_order_draft,
    update_order_draft,
    whatsapp_draft_link,
)
from camellia.responder import responder
from camellia.session_memory import SessionMemoryManager, session_memory


def _respond(uid, message):
    return responder.match_and_respond(message, user_id=uid, channel="instagram")


def test_generic_how_to_order_starts_request_draft_with_customer_instructions(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000")
    uid = "order-intake-start-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")

    response, matched, template_id, _images = _respond(uid, "how do i order")

    assert matched is True
    assert template_id == "alacarte_prebooking_steps"
    from camellia.client_qa import effective_entry
    assert response.startswith(effective_entry("order_prebook").answer + "\n\nContinue on WhatsApp:")
    assert "https://wa.me/" in response
    assert "wa.me" in response
    facts = session_memory.get_facts(uid, "instagram")
    assert facts["order_intake_draft"]["status"] == ORDER_DRAFT_INCOMPLETE
    assert not [row for row in db.get_handoffs(limit=300) if row["source_user_id"] == uid]


def test_complete_natural_order_sentence_immediately_hands_off_partial_draft(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000")
    uid = "order-intake-natural-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")

    response, matched, template_id, _images = _respond(
        uid, "Need 2 boxes Sondesh for Friday dinner in Gurgaon",
    )

    draft = session_memory.get_facts(uid, "instagram")["order_intake_draft"]
    assert matched is True and template_id == "order_intake_draft"
    assert (draft["item"], draft["quantity"], draft["delivery_slot"], draft["delivery_area"]) == (
        "Sondesh", "2 boxes", "Friday Dinner", "Gurgaon",
    )
    assert draft["status"] == ORDER_DRAFT_INCOMPLETE
    assert "ORDER DRAFT" in response
    assert "Preferred slot: Friday Dinner" in response
    assert "Delivery address/area: Gurgaon\n" in response
    assert "Receiver phone: Not provided\n" in response
    assert "Location pin:" not in response
    assert "https://wa.me/919800000000?text=" in response
    assert not any(value in response for value in ("Not supplied", "Needed", "N/A", "Unknown"))
    assert not any(value in response.lower() for value in (
        "confirmed", "available", "delivery confirmed", "price confirmed",
    ))
    assert "full delivery address" not in response
    assert "receiver's phone number" not in response
    assert "Please send your preferred delivery day" not in response
    assert "Please send" not in response


def test_bento_sentence_extracts_only_customer_supplied_fields():
    draft = new_order_draft()
    update_order_draft(draft, "2 chicken bento tomorrow lunch gurgaon")
    assert draft["item"] == "Chicken Bento"
    assert draft["quantity"] == "2"
    assert draft["delivery_slot"] == "Tomorrow Lunch"
    assert draft["delivery_area"] == "Gurgaon"
    assert draft["delivery_address"] is None
    assert draft["receiver_phone"] is None


def test_informational_menu_request_is_not_misclassified_as_order_request():
    assert not is_order_request("I want to know about the menu")
    assert not is_order_request("I want to see the menu")


def test_sondesh_classic_size_and_area_leaves_missing_fields_blank(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000")
    uid = "order-intake-slot-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")

    response, _matched, _template_id, _images = _respond(
        uid, "Sondesh Classic 250g to Gurgaon",
    )

    draft = session_memory.get_facts(uid, "instagram")["order_intake_draft"]
    assert draft["item"] == "Sondesh Classic"
    assert draft["quantity"] == "250g"
    assert draft["delivery_area"] == "Gurgaon"
    assert draft["delivery_slot"] is None
    assert draft["status"] == ORDER_DRAFT_INCOMPLETE
    assert "ORDER DRAFT" in response
    assert "Preferred slot: Not provided\n" in response
    assert "Delivery address/area: Gurgaon\n" in response
    assert "Receiver phone: Not provided\n" in response
    assert "https://wa.me/919800000000?text=" in response
    body = unquote(response.split("?text=", 1)[1])
    assert body == (
        "Hi, I'd like to place a pre-order with Camellia Artisanal.\n\n"
        "ORDER DRAFT\n\n"
        "1. Items & quantity: 250g Sondesh Classic\n"
        "2. Delivery address/area: Gurgaon\n"
        "3. Receiver phone: Not provided\n"
        "4. Preferred slot: Not provided\n\n"
        "Please help me complete any missing details and confirm this order."
    )


def test_missing_whatsapp_number_keeps_draft_without_inventing_link(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "WHATSAPP_BUSINESS_NUMBER", "")
    uid = "order-intake-no-number-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")

    response, matched, template_id, _images = _respond(uid, "Sondesh Classic 250g to Gurgaon")

    assert matched is True and template_id == "order_intake_draft"
    assert "ORDER DRAFT" in response
    assert "https://wa.me/918796551990?text=" in response
    assert "wa.me" in response
    assert session_memory.get_facts(uid, "instagram")["order_intake_draft"]["status"] == ORDER_DRAFT_INCOMPLETE


@pytest.mark.parametrize("message", (
    "Need 2 boxes Sondesh in Gurgaon",
    "2 boxes Sondesh for Gurgaon",
    "Chicken Bento 2 boxes to Noida",
    "Need 1kg Sondesh for Friday dinner in Gurgaon",
))
def test_natural_partial_orders_immediately_offer_human_whatsapp_handoff(monkeypatch, message):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000")
    uid = "order-intake-variant-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")

    response, matched, template_id, _images = _respond(uid, message)

    assert matched is True and template_id == "order_intake_draft"
    assert "ORDER DRAFT" in response
    assert "https://wa.me/919800000000?text=" in response
    assert "Please send" not in response
    assert session_memory.get_facts(uid, "instagram")["order_intake_draft"]["status"] == ORDER_DRAFT_INCOMPLETE


def test_ordering_question_keeps_product_named_in_the_same_message(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    uid = "order-intake-question-product-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")

    response, matched, template_id, _images = _respond(uid, "How do I order Sondesh Classic 250g to Gurgaon?")

    draft = session_memory.get_facts(uid, "instagram")["order_intake_draft"]
    assert matched is True and template_id == "order_intake_draft"
    assert draft["item"] == "Sondesh Classic"
    assert draft["quantity"] == "250g"
    assert draft["delivery_area"] == "Gurgaon"
    assert "What quantity" not in response
    assert "ORDER DRAFT" in response
    assert "Preferred slot: Not provided\n" in response


def test_multiturn_draft_persists_supplied_fields_and_renders_customer_values(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    uid = "order-intake-multi-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")

    _respond(uid, "Need 2 boxes Sondesh in Gurgaon")
    second, *_ = _respond(uid, "Friday dinner")
    assert "Preferred slot: Friday Dinner" in second
    assert "Delivery address/area: Gurgaon\n" in second and "Receiver phone: Not provided\n" in second
    third, *_ = _respond(uid, "DLF Phase 4, Gurgaon")
    assert "Delivery address/area: DLF Phase 4, Gurgaon" in third
    assert "Receiver phone: Not provided\n" in third
    final, matched, template_id, _images = _respond(uid, "9876543210")

    draft = session_memory.get_facts(uid, "instagram")["order_intake_draft"]
    assert draft["status"] == ORDER_DRAFT_READY
    assert matched is True and template_id == "order_intake_draft"
    assert "Items & quantity: 2 boxes Sondesh" in final
    assert "Delivery address/area: DLF Phase 4, Gurgaon" in final
    assert "Preferred slot: Friday Dinner" in final
    assert "Receiver phone: 9876543210" in final
    assert "Location pin:" not in final
    assert "Our manager will review availability, delivery charges, and the final invoice." in final
    assert "order confirmed" not in final.lower()
    assert "booking confirmed" not in final.lower()
    assert "order is placed" not in final.lower()


def test_editing_quantity_updates_the_same_draft_without_reasking_other_fields(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    uid = "order-intake-edit-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")
    _respond(uid, "Need 2 boxes Sondesh for Friday dinner in Gurgaon")
    _respond(uid, "DLF Phase 4, Gurgaon")
    _respond(uid, "9876543210")
    facts = session_memory.get_facts(uid, "instagram")
    original = facts["order_intake_draft"]

    edited, _matched, _template_id, _images = _respond(uid, "make it 500g")

    updated = session_memory.get_facts(uid, "instagram")["order_intake_draft"]
    assert updated is original
    assert updated["quantity"] == "500g"
    assert updated["delivery_address"] == "DLF Phase 4, Gurgaon"
    assert updated["receiver_phone"] == "9876543210"
    assert "Items & quantity: 500g Sondesh" in edited
    assert "full delivery address" not in edited
    assert "receiver's phone number" not in edited


def test_edit_order_command_and_quantity_change_reuse_the_same_draft(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    uid = "order-intake-edit-command-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")
    _respond(uid, "Need 2 boxes Sondesh for Friday dinner in Gurgaon")
    _respond(uid, "DLF Phase 4, Gurgaon")
    _respond(uid, "9876543210")
    draft = session_memory.get_facts(uid, "instagram")["order_intake_draft"]

    edit_prompt, *_ = _respond(uid, "Edit Order")
    assert "What would you like to change?" in edit_prompt
    changed, *_ = _respond(uid, "change to 3 boxes")

    assert session_memory.get_facts(uid, "instagram")["order_intake_draft"] is draft
    assert draft["quantity"] == "3 boxes"
    assert "Items & quantity: 3 boxes Sondesh" in changed
    assert draft["delivery_address"] == "DLF Phase 4, Gurgaon"


def test_an_unrelated_field_does_not_satisfy_a_pending_edit():
    draft = new_order_draft()
    draft.update({"item": "Sondesh", "quantity": "2 boxes", "edit_field": "quantity"})
    update_order_draft(draft, "Friday lunch")
    assert draft["delivery_slot"] == "Friday Lunch"
    assert draft["quantity"] == "2 boxes"
    assert draft["edit_field"] == "quantity"
    assert draft["status"] == ORDER_DRAFT_INCOMPLETE


def test_standalone_house_requires_a_pin_before_ready(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    uid = "order-intake-pin-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")
    _respond(uid, "Need 2 boxes Sondesh for Friday dinner in Gurgaon")
    response, *_ = _respond(uid, "Standalone house, DLF Phase 4 Gurgaon")

    draft = session_memory.get_facts(uid, "instagram")["order_intake_draft"]
    assert draft["location_pin_required"] is True
    assert "Receiver phone: Not provided\n" in response
    assert "Location pin:" not in response
    assert "Please send" not in response
    assert draft["status"] == ORDER_DRAFT_INCOMPLETE


def test_whatsapp_link_prefills_the_order_request_but_only_creates_a_click_link(monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000")
    draft = {
        "item": "Sondesh Classic", "quantity": "250g", "delivery_area": "Gurgaon",
        "delivery_address": "DLF Phase 4, Gurgaon", "delivery_slot": "Friday Dinner",
        "receiver_phone": "9876543210", "location_pin": None,
    }
    with patch.object(app_module, "send_instagram_reply") as send_instagram, \
         patch.object(app_module.escalator, "handle_escalation") as escalate:
        link = whatsapp_draft_link(draft)

    assert link.startswith("https://wa.me/919800000000?text=")
    body = unquote(link)
    for value in (
        "Hi, I'd like to place a pre-order with Camellia Artisanal.", "ORDER DRAFT",
        "Items & quantity: 250g Sondesh Classic",
        "Delivery address/area: DLF Phase 4, Gurgaon", "Preferred slot: Friday Dinner",
        "Receiver phone: 9876543210",
        "Please help me complete any missing details and confirm this order.",
    ):
        assert value in body
    send_instagram.assert_not_called()
    escalate.assert_not_called()


def test_instagram_order_start_sends_one_reply_and_creates_no_human_handoff(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    uid = "order-intake-no-handoff-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")
    with patch.object(app_module, "send_instagram_reply") as send, patch.object(
        app_module.escalator, "handle_escalation",
    ) as escalate:
        app_module._process_instagram_event(uid, "how do i order", "ad-order-intake")

    assert send.call_count == 1
    from camellia.client_qa import effective_entry
    assert send.call_args.args[1].startswith(effective_entry("order_prebook").answer + "\n\nContinue on WhatsApp:")
    assert "https://wa.me/" in send.call_args.args[1]
    escalate.assert_not_called()
    assert not [row for row in db.get_handoffs(limit=300) if row["source_user_id"] == uid]
    assert session_memory.get_facts(uid, "instagram")["order_intake_draft"]["status"] == ORDER_DRAFT_INCOMPLETE


def test_complete_intake_is_a_click_to_chat_not_an_automatic_handoff(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000")
    uid = "order-intake-click-only-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")
    with patch.object(app_module, "send_instagram_reply") as send, patch.object(
        app_module.escalator, "handle_escalation",
    ) as escalate:
        app_module._process_instagram_event(uid, "Need 2 boxes Sondesh for Friday dinner in Gurgaon", "ad-order-intake")
        app_module._process_instagram_event(uid, "DLF Phase 4, Gurgaon", "ad-order-intake")
        app_module._process_instagram_event(uid, "9876543210", "ad-order-intake")

    final_reply = send.call_args.args[1]
    assert "ORDER DRAFT" in final_reply
    assert "https://wa.me/919800000000?text=" in final_reply
    assert "Our manager will review availability" in final_reply
    assert send.call_count == 3
    escalate.assert_not_called()
    assert not [row for row in db.get_handoffs(limit=300) if row["source_user_id"] == uid]


def test_bulk_event_order_keeps_existing_human_route(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    uid = "order-intake-bulk-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")
    _response, matched, template_id, _images = _respond(
        uid, "I want 50 boxes of Jolbhora for a wedding in Delhi",
    )
    assert session_memory.get_facts(uid, "instagram")["order_intake_draft"] is None
    assert not matched or template_id != "order_intake_draft"


def test_draft_state_survives_active_session_reload(tmp_path):
    database = Database(str(tmp_path / "order-intake-session.db"))
    first = SessionMemoryManager(persistence_enabled=True, database=database)
    facts = first.get_facts("persist-order-intake", "instagram")
    facts["order_intake_draft"] = new_order_draft()
    facts["order_intake_draft"].update({"item": "Sondesh", "quantity": "2 boxes"})
    first.add_turn("persist-order-intake", "instagram", "Need 2 boxes Sondesh", "Which area?")

    second = SessionMemoryManager(persistence_enabled=True, database=database)
    assert second.get_facts("persist-order-intake", "instagram")["order_intake_draft"]["quantity"] == "2 boxes"


def test_how_to_order_overrides_mapped_source_without_sending_source_description(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    uid = "order-intake-source-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    facts["source_route"] = {
        "routing_id": 55, "active": True, "routing_topic": "Sondesh",
        "source_description": "Source description must not be shown.",
        "escalation_policy": "always_human",
    }

    response, matched, template_id, _images = _respond(uid, "how do i order")

    assert matched is True and template_id == "alacarte_prebooking_steps"
    from camellia.client_qa import effective_entry
    assert response.startswith(effective_entry("order_prebook").answer + "\n\nContinue on WhatsApp:")
    assert "https://wa.me/" in response
    assert "Source description" not in response
    assert facts["source_context_superseded"] is True
    assert facts["order_intake_draft"]["status"] == ORDER_DRAFT_INCOMPLETE


def test_price_payment_finalized_order_and_unsupported_questions_keep_separate_routes(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    price_id = "order-intake-price-" + uuid.uuid4().hex[:8]
    pay_id = "order-intake-payment-" + uuid.uuid4().hex[:8]
    final_id = "order-intake-final-" + uuid.uuid4().hex[:8]
    unsupported_id = "order-intake-unsupported-" + uuid.uuid4().hex[:8]
    discount_id = "order-intake-discount-" + uuid.uuid4().hex[:8]
    active_price_id = "order-intake-active-price-" + uuid.uuid4().hex[:8]
    active_payment_id = "order-intake-active-payment-" + uuid.uuid4().hex[:8]
    for uid in (price_id, pay_id, final_id, unsupported_id, discount_id, active_price_id, active_payment_id):
        session_memory.clear_session(uid, "instagram")

    _price, matched_price, price_template, _ = _respond(price_id, "What's the price of Sondesh?")
    _payment, matched_payment, payment_template, _ = _respond(pay_id, "How do I pay?")
    _final, _matched_final, final_template, _ = _respond(final_id, "I've finalized my order")
    _availability, matched_availability, availability_template, _ = _respond(unsupported_id, "Is Sondesh available?")
    _discount, _matched_discount, discount_template, _ = _respond(discount_id, "Can I get a discount?")
    _respond(active_price_id, "how do i order")
    _active_price, active_price_match, active_price_template, _ = _respond(active_price_id, "What's the price of Sondesh?")
    _respond(active_payment_id, "how do i order")
    _active_payment, active_payment_match, active_payment_template, _ = _respond(active_payment_id, "How do I pay?")

    assert matched_price is True and price_template == "sondesh_price_detailed"
    assert matched_payment is True and payment_template == "GUARD_payment_method"
    assert final_template == "order_next_steps"
    assert session_memory.get_facts(final_id, "instagram")["order_intake_draft"] is None
    assert not matched_availability or availability_template not in {"order_intake_draft", "order_draft_receipt"}
    assert "available" not in _availability.lower() or not matched_availability
    assert discount_template != "order_intake_draft"
    assert active_price_match is True and active_price_template == "sondesh_price_detailed"
    assert session_memory.get_facts(active_price_id, "instagram")["order_intake_draft"]["status"] == ORDER_DRAFT_INCOMPLETE
    assert active_payment_match is True and active_payment_template == "GUARD_payment_method"
    assert session_memory.get_facts(active_payment_id, "instagram")["order_intake_draft"]["status"] == ORDER_DRAFT_INCOMPLETE


# QA-O1: one response creates a manual handoff draft, never an interview.
@pytest.fixture
def one_shot_customer(monkeypatch):
    monkeypatch.setattr(config, 'BYPASS_AD_GATE', True)
    monkeypatch.setattr(config, 'WHATSAPP_BUSINESS_NUMBER', '918796551990')
    uid = 'one-shot-' + uuid.uuid4().hex
    session_memory.clear_session(uid, 'instagram')
    def reply(message):
        from camellia.order_intake import draft_fingerprint, is_link_resend_request, is_order_process_question
        facts = session_memory.get_facts(uid, 'instagram')
        previous = facts.get('order_intake_last_presented_fingerprint')
        with patch('camellia.ai_responder.ai_responder._query_groq', side_effect=AssertionError('LLM called')):
            text, matched, template, images = _respond(uid, message)
        assert matched and images is None
        if template == 'alacarte_prebooking_steps':
            from camellia.client_qa import effective_entry
            assert facts['client_qa_id'] in {'order_prebook', 'alacarte_prebook'}
            assert text.startswith(effective_entry(facts['client_qa_id']).answer + '\n\nContinue on WhatsApp:')
            assert text.count('https://wa.me/') == 1
        else:
            assert template in {'order_intake_draft', 'system_conversation'}
        unchanged = previous == draft_fingerprint(facts['order_intake_draft']) and not (is_link_resend_request(message) or is_order_process_question(message))
        if unchanged:
            assert 'https://wa.me/' not in text and 'ORDER DRAFT' not in text
        else:
            assert 'https://wa.me/918796551990?text=' in text
        assert 'order confirmed' not in text.lower()
        assert 'Please send' not in text
        return text, session_memory.get_facts(uid, 'instagram')['order_intake_draft']
    return reply

@pytest.mark.parametrize('message', ['how do I order?', 'how can I place an order?'])
def test_zero_detail_request_has_exact_four_point_instruction_and_immediate_template(one_shot_customer, message):
    expected = (
        'To place a pre-order, Kindly call / WhatsApp our kitchen at 8796551990 with following details\n\n'
        '1. Your choice of items & the quantity\n'
        '2. delivery address so that I can tell u Porter charges,\n'
        '3. phone number of person who will receive the package\n'
        '4. which slot - example : Friday / Saturday / Sunday - lunch or dinner or any day next week'
    )
    text, draft = one_shot_customer(message)
    assert instruction_message() == expected
    from camellia.client_qa import effective_entry
    assert text.startswith(effective_entry("order_prebook").answer + "\n\nContinue on WhatsApp:")
    body = unquote(text.split('?text=', 1)[1])
    assert body.count('Not provided') == 4
    assert body.endswith('Please help me complete any missing details and confirm this order.')
    assert all(draft[field] is None for field in ('item','quantity','delivery_area','receiver_phone','delivery_slot'))

@pytest.mark.parametrize('message', [
    '1. 2 boxes Sondesh\n2. Flat 12, Sector 51, Gurgaon\n3. 9876543210\n4. Friday dinner',
    'Item: Sondesh\nQuantity: 2 boxes\nDelivery address: Flat 12, Sector 51, Gurgaon\nPhone: 9876543210\nPreferred slot: Friday dinner',
    'Item: Sondesh; Quantity: 2 boxes; Delivery address: Flat 12, Sector 51, Gurgaon; Phone: 9876543210; Slot: Friday dinner',
    'Need 2 boxes Sondesh delivery to Flat 12, Sector 51, Gurgaon Friday dinner 9876543210',
])
def test_complete_numbered_labelled_and_natural_forms_capture_all_fields(one_shot_customer, message):
    text, draft = one_shot_customer(message)
    assert draft['item'] == 'Sondesh'
    assert draft['quantity'] == '2 boxes'
    assert draft['delivery_area'] == 'Gurgaon'
    assert draft['receiver_phone'] == '9876543210'
    assert draft['delivery_slot'].casefold() == 'friday dinner'
    assert draft['delivery_address'] == 'Flat 12, Sector 51, Gurgaon'
    body = unquote(text.split('?text=',1)[1])
    for field in ('item','quantity','delivery_address','receiver_phone','delivery_slot'):
        assert draft[field] in body

@pytest.mark.parametrize('message,missing', [
    ('2 boxes sondesh Gurgaon Friday dinner','receiver_phone'),
    ('2 boxes sondesh Gurgaon 9876543210','delivery_slot'),
    ('2 boxes sondesh','delivery_area'),
])
def test_partial_order_immediately_hands_off_with_explicit_missing_values(one_shot_customer, message, missing):
    text, draft = one_shot_customer(message)
    assert draft['item'] == 'Sondesh' and draft['quantity'] == '2 boxes'
    assert draft[missing] is None
    assert 'Not provided' in unquote(text.split('?text=',1)[1])
    assert 'full delivery address' not in text
    assert "receiver's phone" not in text


def test_sequential_item_area_and_slot_preserve_prior_values_without_questions(one_shot_customer):
    _, draft = one_shot_customer('Sondesh')
    assert draft['item'] == 'Sondesh' and draft['quantity'] is None
    one_shot_customer('2 boxes')
    first, _ = one_shot_customer('Gurgaon')
    second, _ = one_shot_customer('Gurgaon')
    assert 'https://wa.me/' in first and 'https://wa.me/' not in second
    assert draft['delivery_area'] == 'Gurgaon' and draft['delivery_address'] is None
    one_shot_customer('Friday dinner')
    assert draft['quantity'] == '2 boxes' and draft['delivery_slot'] == 'Friday Dinner'
    assert draft['item'] == 'Sondesh' and draft['delivery_area'] == 'Gurgaon'


def test_voluntary_address_and_explicit_corrections_regenerate_link(one_shot_customer):
    first, draft = one_shot_customer('2 boxes Sondesh Gurgaon Friday dinner')
    one_shot_customer('Flat 12, Sector 51, Gurgaon')
    assert draft['delivery_address'] == 'Flat 12, Sector 51, Gurgaon'
    one_shot_customer('phone is 9876543210')
    one_shot_customer('make it Saturday dinner')
    changed, _ = one_shot_customer('change delivery to Noida')
    assert changed != first
    assert draft['delivery_area'] == 'Noida' and draft['delivery_address'] is None
    assert draft['item'] == 'Sondesh' and draft['quantity'] == '2 boxes'
    assert draft['receiver_phone'] == '9876543210'
    assert draft['delivery_slot'] == 'Saturday Dinner'
    body = unquote(changed.split('?text=',1)[1])
    assert 'Delivery address/area: Noida' in body and 'Preferred slot: Saturday Dinner' in body


def test_order_process_area_only_does_not_invent_item(one_shot_customer):
    one_shot_customer('how do I order?')
    text, draft = one_shot_customer('Gurgaon')
    assert draft['delivery_area'] == 'Gurgaon'
    assert draft['item'] is None and draft['delivery_address'] is None
    assert 'Items & quantity: Not provided' in unquote(text.split('?text=',1)[1])


def test_day_slot_and_bare_quantity_are_captured_in_separate_messages(one_shot_customer):
    one_shot_customer('Sondesh')
    one_shot_customer('2')
    one_shot_customer('Gurgaon')
    one_shot_customer('Friday')
    text, draft = one_shot_customer('dinner')
    assert draft['quantity'] == '2'
    assert draft['delivery_slot'] == 'Friday Dinner'
    assert draft['item'] == 'Sondesh' and draft['delivery_area'] == 'Gurgaon'
    first, _ = one_shot_customer('phone is 9876543210')
    second, _ = one_shot_customer('phone is 9876543210')
    assert 'https://wa.me/' in first and 'https://wa.me/' not in second
    assert draft['quantity'] == '2' and draft['receiver_phone'] == '9876543210'


def test_numbered_quantity_only_preserves_existing_item(one_shot_customer):
    one_shot_customer('2 boxes Sondesh Gurgaon Friday dinner')
    text, draft = one_shot_customer('1. 3 boxes')
    assert draft['item'] == 'Sondesh' and draft['quantity'] == '3 boxes'
    assert draft['delivery_area'] == 'Gurgaon' and draft['delivery_slot'] == 'Friday Dinner'
    assert 'Items & quantity: 3 boxes Sondesh' in unquote(text.split('?text=',1)[1])


def test_area_only_completes_explicit_address_edit_without_repeated_question(one_shot_customer):
    _, draft = one_shot_customer('2 boxes Sondesh Gurgaon Friday dinner')
    # Simulate an explicitly requested address edit; a city is enough.
    draft['edit_field'] = 'delivery_address'
    one_shot_customer('Noida')
    assert draft['delivery_area'] == 'Noida' and draft['edit_field'] is None
    assert draft['delivery_address'] is None and draft['item'] == 'Sondesh'


# QA-O2: messy customer data remains a draft for a human to validate.
@pytest.mark.parametrize("message,item,quantity,area,slot,phone,address", [
    ("2 boxes sondesh gurgaon friday dinner 9876543210", "Sondesh", "2 boxes", "Gurgaon", "Friday Dinner", "9876543210", None),
    ("need 2 boxes sondesh for friday dinner gurgaon ph 9876543210", "Sondesh", "2 boxes", "Gurgaon", "Friday Dinner", "9876543210", None),
    ("sondesh 2 boxes saturday lunch gurugram 98765 43210", "Sondesh", "2 boxes", "Gurugram", "Saturday Lunch", "9876543210", None),
    ("chicken bento 3 boxes noida sunday dinner 9812345678", "Chicken Bento", "3 boxes", "Noida", "Sunday Dinner", "9812345678", None),
    ("need 2 portions chicken kosha Gurgaon tomorrow lunch", "Chicken Kosha", "2 portions", "Gurgaon", "Tomorrow Lunch", None, None),
    ("need kanchagolla 250g sector 56 gurgaon friday lunch", "Kanchagolla", "250g", "Gurgaon", "Friday Lunch", None, "sector 56 gurgaon"),
    ("Friday dinner Gurgaon 2 boxes Sondesh", "Sondesh", "2 boxes", "Gurgaon", "Friday Dinner", None, None),
    ("I want 2 portions chicken kosha", "Chicken Kosha", "2 portions", None, None, None, None),
    ("Need 1 Bengali meal", "Bengali meal", "1", None, None, None, None),
    ("I'd like 2 plates luchi and aloo dom", "luchi and aloo dom", "2 plates", None, None, None, None),
    ("I want 2 fish curry", "fish curry", "2", None, None, None, None),
    ("Need 2 boxes Sondesh and 1 portion chicken kosha", "Sondesh and chicken kosha", "2 boxes + 1 portion", None, None, None, None),
])
def test_messy_customer_orders_have_literal_fields_and_manual_link(one_shot_customer, message, item, quantity, area, slot, phone, address):
    text, draft = one_shot_customer(message)
    assert {key: draft[key] for key in ("item", "quantity", "delivery_area", "delivery_slot", "receiver_phone", "delivery_address")} == {
        "item": item, "quantity": quantity, "delivery_area": area, "delivery_slot": slot, "receiver_phone": phone, "delivery_address": address,
    }
    assert not any(claim in text.casefold() for claim in ("is available", "confirmed", "booked", "reserved"))


@pytest.mark.parametrize("quantity", ["1 box", "2 boxes", "1 pack", "2 packs", "1 piece", "2 pieces", "2 pcs", "1 portion", "2 portions", "1 serving", "2 servings", "1 plate", "2 plates", "1kg", "0.5 kg", "250g", "250 gm", "250 grams", "2"])
def test_quantity_units_accumulate_without_changing_existing_item(one_shot_customer, quantity):
    one_shot_customer("Chicken Kosha")
    text, draft = one_shot_customer(quantity)
    assert draft["item"] == "Chicken Kosha" and draft["quantity"] == quantity
    assert "Items & quantity: " + quantity + " Chicken Kosha" in unquote(text)


@pytest.mark.parametrize("message,field,value", [
    ("actually make it 3 boxes", "quantity", "3 boxes"),
    ("change quantity to 3 boxes", "quantity", "3 boxes"),
    ("Saturday dinner instead", "delivery_slot", "Saturday Dinner"),
    ("actually Sunday lunch", "delivery_slot", "Sunday Lunch"),
    ("make it Friday dinner", "delivery_slot", "Friday Dinner"),
    ("change slot to tomorrow lunch", "delivery_slot", "Tomorrow Lunch"),
    ("not Friday, Saturday dinner", "delivery_slot", "Saturday Dinner"),
    ("not Friday lunch, Saturday dinner", "delivery_slot", "Saturday Dinner"),
    ("lunch Friday", "delivery_slot", "Lunch Friday"),
    ("dinner Saturday", "delivery_slot", "Dinner Saturday"),
    ("next week", "delivery_slot", "Next Week"),
    ("phone is 9811111111 instead", "receiver_phone", "9811111111"),
    ("use 9811111111", "receiver_phone", "9811111111"),
    ("actually Noida", "delivery_area", "Noida"),
    ("not Gurgaon, Noida", "delivery_area", "Noida"),
    ("change delivery to Noida", "delivery_area", "Noida"),
    ("address is DLF Phase 4 Gurgaon", "delivery_address", "DLF Phase 4 Gurgaon"),
    ("change address to Sector 56 Gurgaon", "delivery_address", "Sector 56 Gurgaon"),
])
def test_natural_corrections_change_only_the_intended_field(one_shot_customer, message, field, value):
    _, draft = one_shot_customer("2 boxes Sondesh Gurgaon Friday dinner 9876543210")
    before = draft.copy()
    text, _ = one_shot_customer(message)
    assert draft[field] == value
    for key in ("item", "quantity", "delivery_area", "delivery_slot", "receiver_phone", "delivery_address"):
        if key != field:
            assert draft[key] == before[key]
    if draft[field] != before[field]:
        assert value in unquote(text)
    else:
        assert "https://wa.me/" not in text


@pytest.mark.parametrize("correction", ["actually Noida", "not Gurgaon, Noida", "change delivery to Noida"])
def test_area_corrections_clear_only_incompatible_address_and_pin(one_shot_customer, correction):
    one_shot_customer("2 boxes Sondesh Gurgaon Friday dinner 9876543210")
    _, draft = one_shot_customer("DLF Phase 4, Gurgaon")
    draft["location_pin"] = "https://maps.app.goo.gl/oldGurgaonPin"
    one_shot_customer(correction)
    assert draft["delivery_area"] == "Noida" and draft["delivery_address"] is None
    assert draft["location_pin"] is None
    assert (draft["item"], draft["quantity"], draft["delivery_slot"], draft["receiver_phone"]) == ("Sondesh", "2 boxes", "Friday Dinner", "9876543210")


@pytest.mark.parametrize("message", ["Gurgaon", "Friday dinner", "9876543210", "okay thanks"])
def test_repeated_or_unrecognized_details_do_not_erase_existing_values(one_shot_customer, message):
    one_shot_customer("2 boxes Sondesh Gurgaon Friday dinner 9876543210")
    _, draft = one_shot_customer("DLF Phase 4, Gurgaon")
    before = draft.copy()
    first, _ = one_shot_customer(message)
    second, _ = one_shot_customer(message)
    assert draft == before
    assert first == second


@pytest.mark.parametrize("message", [
    "what is chicken kosha price?", "how much is 2 boxes sondesh?", "tell me about Bengali Bento",
    "is sondesh available?", "what is minimum order?", "do you deliver to Noida?", "how do I pay?",
    "what is the weather?", "do you have bento?", "can you deliver bento to Noida?", "what is the minimum quantity?",
])
@pytest.mark.parametrize("active", [False, True])
def test_questions_never_start_or_mutate_order_data(monkeypatch, message, active):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    uid = "question-draft-" + uuid.uuid4().hex
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    if active:
        facts["order_intake_draft"] = new_order_draft()
        update_order_draft(facts["order_intake_draft"], "2 boxes Sondesh Gurgaon Friday dinner 9876543210")
    before = facts["order_intake_draft"].copy() if active else None
    assert not is_order_request(message)
    if active:
        update_order_draft(facts["order_intake_draft"], message)
        assert facts["order_intake_draft"] == before
    with patch("camellia.ai_responder.ai_responder._query_groq", side_effect=AssertionError("LLM called")):
        _text, _matched, template, _images = _respond(uid, message)
    assert template != "order_intake_draft"
    assert facts["order_intake_draft"] == before


def test_four_field_template_roundtrips_and_optional_pin_is_not_internal_clutter(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    monkeypatch.setattr(config, "WHATSAPP_BUSINESS_NUMBER", "918796551990")
    draft = new_order_draft()
    update_order_draft(draft, "2 boxes Sondesh Gurgaon Friday dinner")
    link = whatsapp_draft_link(draft)
    assert urlsplit(link).netloc == "wa.me" and urlsplit(link).path == "/918796551990"
    body = parse_qs(urlsplit(link).query)["text"][0]
    assert body == (
        "Hi, I'd like to place a pre-order with Camellia Artisanal.\n\n"
        "ORDER DRAFT\n\n"
        "1. Items & quantity: 2 boxes Sondesh\n"
        "2. Delivery address/area: Gurgaon\n"
        "3. Receiver phone: Not provided\n"
        "4. Preferred slot: Friday Dinner\n\n"
        "Please help me complete any missing details and confirm this order."
    )
    draft["location_pin"] = "https://maps.app.goo.gl/CustomerPin"
    assert "Location pin: https://maps.app.goo.gl/CustomerPin" in unquote(whatsapp_draft_link(draft))


def test_explicit_empty_phone_edit_is_the_only_reason_to_ask_for_phone(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    uid = "phone-edit-" + uuid.uuid4().hex
    session_memory.clear_session(uid, "instagram")
    _respond(uid, "2 boxes Sondesh Gurgaon Friday dinner")
    text, matched, template, _images = _respond(uid, "change my phone number")
    assert matched and template == "order_intake_draft"
    assert text == "What receiver phone number should I use?"
    text, *_ = _respond(uid, "9876543210")
    assert "https://wa.me/" in text and "Receiver phone: 9876543210" in text


@pytest.mark.parametrize("message", ["how do I order?", "how can I place an order?", "how can I pre-book?", "I want to order", "what are the next steps to order?"])
def test_process_question_scaffolding_is_never_recorded_as_an_item(one_shot_customer, message):
    _, draft = one_shot_customer(message)
    assert draft["item"] is None and draft["quantity"] is None


def test_unknown_item_in_explicit_process_question_is_customer_data(one_shot_customer):
    _, draft = one_shot_customer("How do I order 2 plates luchi and aloo dom?")
    assert draft["item"] == "luchi and aloo dom" and draft["quantity"] == "2 plates"


def test_plain_numeric_item_order_does_not_invent_a_full_address(one_shot_customer):
    _, draft = one_shot_customer("I want 2 fish curry Gurgaon")
    assert draft["item"] == "fish curry" and draft["quantity"] == "2"
    assert draft["delivery_area"] == "Gurgaon" and draft["delivery_address"] is None


def test_voluntary_location_pin_is_captured_and_included_in_manual_link(one_shot_customer):
    one_shot_customer("2 boxes Sondesh Gurgaon Friday dinner")
    text, draft = one_shot_customer("https://maps.app.goo.gl/CustomerPin")
    assert draft["location_pin"] == "https://maps.app.goo.gl/CustomerPin"
    assert "Location pin: https://maps.app.goo.gl/CustomerPin" in unquote(text)


@pytest.mark.parametrize("message", ["Friday lunch or Saturday dinner", "Gurgaon or Noida"])
def test_ambiguous_multiple_values_preserve_existing_fields(one_shot_customer, message):
    _, draft = one_shot_customer("2 boxes Sondesh Gurgaon Friday dinner 9876543210")
    before = draft.copy()
    one_shot_customer(message)
    assert draft == before


def test_address_first_order_keeps_address_separate_from_item_and_slot(one_shot_customer):
    _, draft = one_shot_customer("Sector 56 Gurgaon Friday dinner 2 boxes Sondesh")
    assert draft["item"] == "Sondesh" and draft["quantity"] == "2 boxes"
    assert draft["delivery_address"] == "Sector 56 Gurgaon"
    assert draft["delivery_area"] == "Gurgaon" and draft["delivery_slot"] == "Friday Dinner"


def test_gurugram_alias_does_not_clear_a_gurgaon_address(one_shot_customer):
    one_shot_customer("2 boxes Sondesh Gurgaon Friday dinner")
    _, draft = one_shot_customer("DLF Phase 4, Gurgaon")
    one_shot_customer("Gurugram")
    assert draft["delivery_area"] == "Gurugram" and draft["delivery_address"] == "DLF Phase 4, Gurgaon"


@pytest.mark.parametrize("message", ["use 9876543210 or 9811111111", "9876543210 9811111111"])
def test_ambiguous_phone_values_do_not_overwrite_the_receiver(one_shot_customer, message):
    _, draft = one_shot_customer("2 boxes Sondesh Gurgaon Friday dinner 9800000000")
    before = draft.copy()
    one_shot_customer(message)
    assert draft == before


def test_new_explicit_plain_count_updates_existing_quantity(one_shot_customer):
    one_shot_customer("2 boxes Sondesh Gurgaon Friday dinner")
    _, draft = one_shot_customer("I want 3 fish curry")
    assert draft["item"] == "fish curry" and draft["quantity"] == "3"
    assert draft["delivery_area"] == "Gurgaon" and draft["delivery_slot"] == "Friday Dinner"
