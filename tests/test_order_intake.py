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


def test_how_to_order_starts_request_draft_with_the_exact_customer_instruction(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    uid = "order-intake-start-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")

    response, matched, template_id, _images = _respond(uid, "how do i order")

    assert matched is True
    assert template_id == "order_intake_draft"
    assert response == instruction_message()
    assert "Need 2 boxes Sondesh for Friday dinner in Gurgaon" in response
    assert "Sondesh Classic 250g to Gurgaon" in response
    facts = session_memory.get_facts(uid, "instagram")
    assert facts["order_intake_draft"]["status"] == ORDER_DRAFT_INCOMPLETE
    assert not [row for row in db.get_handoffs(limit=300) if row["source_user_id"] == uid]


def test_complete_natural_order_sentence_extracts_explicit_fields_and_asks_only_missing_details(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
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
    assert "full delivery address" in response
    assert "receiver's phone number" in response
    assert "preferred delivery day" not in response


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


def test_sondesh_classic_size_and_area_only_asks_for_missing_slot(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
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
    assert response == "Please send your preferred delivery day and slot, such as Friday lunch or Friday dinner."
    assert "address" not in response.lower()
    assert "phone" not in response.lower()


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
    assert "preferred delivery day and slot" in response


def test_multiturn_draft_persists_supplied_fields_and_renders_customer_values(monkeypatch):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    uid = "order-intake-multi-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")

    _respond(uid, "Need 2 boxes Sondesh in Gurgaon")
    second, *_ = _respond(uid, "Friday dinner")
    assert "full delivery address" in second and "receiver's phone number" in second
    third, *_ = _respond(uid, "DLF Phase 4, Gurgaon")
    assert "receiver's phone number" in third
    assert "full delivery address" not in third
    final, matched, template_id, _images = _respond(uid, "9876543210")

    draft = session_memory.get_facts(uid, "instagram")["order_intake_draft"]
    assert draft["status"] == ORDER_DRAFT_READY
    assert matched is True and template_id == "order_intake_draft"
    assert "Item: Sondesh" in final
    assert "Quantity: 2 boxes" in final
    assert "Delivery area: Gurgaon" in final
    assert "Delivery address: DLF Phase 4, Gurgaon" in final
    assert "Preferred slot: Friday Dinner" in final
    assert "Receiver phone: 9876543210" in final
    assert "Location pin: Not required" in final
    assert "This is an order request only." in final
    assert "Our kitchen team will confirm availability, delivery charges and the invoice." in final
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
    assert "Quantity: 500g" in edited
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
    assert "Quantity: 3 boxes" in changed
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
    assert "receiver's phone number" in response
    assert "Google Maps location pin" in response
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
        "Hi, I'd like to place an order with Camellia Artisanal.", "ORDER REQUEST",
        "Item: Sondesh Classic", "Quantity: 250g", "Delivery area: Gurgaon",
        "Delivery address: DLF Phase 4, Gurgaon", "Preferred slot: Friday Dinner",
        "Receiver phone: 9876543210", "Please confirm availability, final delivery charges and invoice.",
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
    assert send.call_args.args[1] == instruction_message()
    escalate.assert_not_called()
    assert not [row for row in db.get_handoffs(limit=300) if row["source_user_id"] == uid]


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
    assert "ORDER REQUEST" in final_reply
    assert "https://wa.me/919800000000?text=" in final_reply
    assert "This is an order request only." in final_reply
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

    assert matched is True and template_id == "order_intake_draft"
    assert response == instruction_message()
    assert "Source description" not in response


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

