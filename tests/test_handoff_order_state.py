"""
A finished order must not follow the customer into later conversations.

Reproduced from production: handoff CAM-96013 was minted for a Jolbhora
order, and a "Talk to human" message the next day reused it *and* rebuilt the
same order into the WhatsApp link — so a customer who only asked to speak to
somebody received a prefilled message reading "I'm continuing my Camellia
Instagram order" with a 250g Jolbhora at ₹750 attached.

Two things were wrong and both are covered here: the cart was offered to every
escalation rather than to order messages only, and nothing ever retired it
once its contents had been handed to a human.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

os.environ.setdefault("DB_PATH", "test_camellia.db")

import pytest

from camellia import handoff as handoff_module
from camellia import escalator as escalator_module
from camellia.handoff import (
    get_or_create_handoff,
    retire_order_state,
    verified_order_summary,
    whatsapp_link,
)
from camellia.session_memory import session_memory

JOLBHORA_CART = [{
    "collection": "aamar_mishti", "flavour": "jolbhora", "size": "250g",
    "quantity": 1, "total_price": 750, "type": "sweet",
}]


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    from camellia.database import Database
    database = Database(str(tmp_path / "handoff.db"))
    monkeypatch.setattr(handoff_module, "db", database)
    return database


def _facts_with_cart(uid):
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    facts["cart"] = [dict(item) for item in JOLBHORA_CART]
    return facts


# --- the contamination itself ----------------------------------------------

def test_a_general_escalation_never_carries_the_cart(clean_db):
    """The reported failure: "Talk to human" must not become an order."""
    uid = "ho_general"
    facts = _facts_with_cart(uid)
    result = get_or_create_handoff(
        user_id=uid, channel="instagram", reason="Unmatched Instagram Query",
        user_message="Talk to human", facts=facts,
    )
    assert result["verified_order"] is None


def test_an_order_handoff_still_carries_the_cart(clean_db):
    uid = "ho_order"
    facts = _facts_with_cart(uid)
    result = get_or_create_handoff(
        user_id=uid, channel="instagram", reason="Order Confirmation & Intake Request",
        user_message="I want to order jolbhora 250g", facts=facts, include_order=True,
    )
    assert result["verified_order"]["items"][0]["flavour"] == "jolbhora"
    assert result["verified_order"]["estimated_item_total"] == 750


def test_a_general_escalation_does_not_store_an_order_on_a_new_handoff(clean_db):
    """Staff must not be handed a record implying an order that was never placed."""
    uid = "ho_store"
    facts = _facts_with_cart(uid)
    result = get_or_create_handoff(
        user_id=uid, channel="instagram", reason="Unmatched Instagram Query",
        user_message="Talk to human", facts=facts,
    )
    known = result.get("known_entities") or {}
    assert "verified_order" not in known


def test_reusing_an_existing_handoff_for_a_general_ask_carries_no_order(clean_db):
    """The exact production shape: order first, unrelated human ask after."""
    uid = "ho_reuse"
    facts = _facts_with_cart(uid)
    first = get_or_create_handoff(
        user_id=uid, channel="instagram", reason="Order Confirmation & Intake Request",
        user_message="I want to order jolbhora 250g", facts=facts, include_order=True,
    )
    assert first["newly_created"] is True

    second = get_or_create_handoff(
        user_id=uid, channel="instagram", reason="Unmatched Instagram Query",
        user_message="Talk to human", facts=facts,
    )
    assert second["newly_created"] is False           # dedup preserved
    assert second["handoff_ref"] == first["handoff_ref"]
    assert second["verified_order"] is None           # but no order attached


def test_the_whatsapp_link_for_a_general_ask_mentions_no_order(clean_db):
    uid = "ho_link"
    facts = _facts_with_cart(uid)
    result = get_or_create_handoff(
        user_id=uid, channel="instagram", reason="Unmatched Instagram Query",
        user_message="Talk to human", facts=facts,
    )
    link = whatsapp_link(result["handoff_ref"], result["verified_order"])
    if link:  # None when WHATSAPP_BUSINESS_NUMBER is unset
        assert "Jolbhora" not in link
        assert "750" not in link


# --- retiring the order once a human has it --------------------------------

def test_retiring_clears_the_cart_and_draft(clean_db):
    uid = "ho_retire"
    facts = _facts_with_cart(uid)
    facts["order_draft"] = {"flavour": "jolbhora", "collection": "aamar_mishti",
                            "size": "250g", "quantity": 1}
    retire_order_state(uid, "instagram")
    after = session_memory.get_facts(uid, "instagram")
    assert after["cart"] == []
    assert after["order_draft"]["flavour"] is None
    assert after["order_draft"]["size"] is None
    assert verified_order_summary(after) is None


def test_retiring_leaves_reel_source_routing_intact(clean_db):
    """Order state is retired; conversational context is not."""
    uid = "ho_retire_route"
    facts = _facts_with_cart(uid)
    facts["source_route"] = {"routing_id": 1, "shortcode": "DdAnO6MTok1",
                             "routing_topic": "Wedding / Event Sondesh",
                             "priority_products": ["jolbhora"]}
    facts["source_match_method"] = "shared_media_url"
    retire_order_state(uid, "instagram")
    after = session_memory.get_facts(uid, "instagram")
    assert after["cart"] == []
    assert after["source_route"]["shortcode"] == "DdAnO6MTok1"
    assert after["source_match_method"] == "shared_media_url"


def test_after_retiring_a_later_general_ask_is_clean(clean_db):
    """End to end: order, hand off, retire — then ask for a human."""
    uid = "ho_sequence"
    facts = _facts_with_cart(uid)
    ordered = get_or_create_handoff(
        user_id=uid, channel="instagram", reason="Order Confirmation & Intake Request",
        user_message="I want to order jolbhora 250g", facts=facts, include_order=True,
    )
    assert ordered["verified_order"] is not None
    retire_order_state(uid, "instagram")

    later_facts = session_memory.get_facts(uid, "instagram")
    later = get_or_create_handoff(
        user_id=uid, channel="instagram", reason="Unmatched Instagram Query",
        user_message="Do you have gift boxes?", facts=later_facts,
    )
    assert later["verified_order"] is None
    # Even if this one were somehow treated as an order, there is nothing left.
    as_order = get_or_create_handoff(
        user_id=uid, channel="instagram", reason="Order Confirmation & Intake Request",
        user_message="anything", facts=later_facts, include_order=True,
    )
    assert as_order["verified_order"] is None


def test_an_incomplete_cart_is_still_ignored(clean_db):
    """Unchanged guarantee: a half-built order never reaches a handoff."""
    uid = "ho_incomplete"
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    facts["cart"] = [{"collection": "aamar_mishti", "flavour": "jolbhora",
                      "size": None, "quantity": 1, "total_price": 750}]
    result = get_or_create_handoff(
        user_id=uid, channel="instagram", reason="Order Confirmation & Intake Request",
        user_message="order", facts=facts, include_order=True,
    )
    assert result["verified_order"] is None


def test_concurrent_duplicate_handoffs_reuse_one_durable_reference(clean_db):
    """Concurrent duplicate webhook work cannot mint two active handoffs."""
    uid = "ho_concurrent"
    facts = _facts_with_cart(uid)

    def create_one():
        return get_or_create_handoff(
            user_id=uid, channel="instagram", reason="Unmatched Instagram Query",
            user_message="I need a human", facts=facts,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _unused: create_one(), range(2)))

    assert len({result["handoff_ref"] for result in results}) == 1
    assert sum(result["newly_created"] for result in results) == 1
    rows = clean_db.get_handoffs(limit=20)
    assert len([row for row in rows if row["source_user_id"] == uid]) == 1


def test_notification_failure_preserves_the_durable_handoff(clean_db, monkeypatch):
    """Staff-alert failure must not erase the customer handoff record."""
    monkeypatch.setattr(escalator_module, "db", clean_db)
    uid = "ho_notification_failure"
    handoff = get_or_create_handoff(
        user_id=uid, channel="instagram", reason="Unmatched Instagram Query",
        user_message="I need a person", facts=_facts_with_cart(uid),
    )

    with patch.object(
        escalator_module.escalator, "send_human_alert",
        return_value={"status": "failed", "error": "provider timeout"},
    ):
        result = escalator_module.escalator.handle_escalation(
            user_id=uid, channel="instagram", user_message="I need a person",
            reason="Unmatched Instagram Query",
            context_data={"handoff_ref": handoff["handoff_ref"]},
        )

    assert result["alert_sent"] is False
    assert clean_db.get_handoff_by_ref(handoff["handoff_ref"])["status"] == "CREATED"
    assert any(row["id"] == result["escalation_id"] for row in clean_db.get_escalations())
