"""Canonical customer intent must outrank ACTIVE learned knowledge."""

import uuid

import pytest

from camellia.database import Database
from camellia.responder import responder
from camellia.session_memory import session_memory


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "routing_precedence.db"))
    for module in (
        "camellia.responder",
        "camellia.rag_fallback",
        "camellia.ai_responder",
        "camellia.triage",
        "camellia.assisted_resolution",
    ):
        monkeypatch.setattr(f"{module}.db", database)
    return database


def _reply(message):
    user_id = "precedence_" + uuid.uuid4().hex[:10]
    session_memory.clear_session(user_id, "whatsapp")
    return responder.match_and_respond(message, user_id=user_id, channel="whatsapp")


def _seed_competing_active_entry(database):
    # One intentionally broad ACTIVE row, with aliases covering the supported
    # questions, reproduces the class of stale Custom Q&A entry that hijacked
    # the production pre-order query.
    return database.add_learned_knowledge(
        "General customer help",
        "how do i order from the menu",
        "UNRELATED LEARNED ANSWER MUST NOT OVERRIDE CANONICAL INTENT",
        aliases=[
            "how do i pre-order a meal",
            "where is your kitchen",
            "who can i speak to directly",
            "can i pay cash on delivery",
            "how should i store the sondesh",
            "does sondesh contain nuts",
        ],
    )


@pytest.mark.parametrize(
    "message, expected_template",
    [
        ("How do I pre-order a meal?", "alacarte_prebooking_steps"),
        ("How do I order from the menu?", {"alacarte_prebooking_steps", "how_to_order"}),
        ("How can I pre order food?", "alacarte_prebooking_steps"),
        ("I want to pre-book a meal", "alacarte_prebooking_steps"),
        ("How can I book lunch?", "alacarte_prebooking_steps"),
        ("How do I pre-order à la carte?", "alacarte_prebooking_steps"),
        ("Where is your kitchen?", "GUARD_pickup_address_hours"),
        ("Who can I speak to directly?", "GUARD_staff_names_manager_contact"),
        ("Can I pay cash on delivery?", "GUARD_payment_method"),
        ("How should I store the sondesh?", "GUARD_shelf_life_storage"),
        ("Does sondesh contain nuts?", "GUARD_allergen_nutfree_glutenfree"),
    ],
)
def test_canonical_intent_beats_broad_active_learned_entry(
    isolated_db, message, expected_template
):
    _seed_competing_active_entry(isolated_db)

    response, matched, template_id, _ = _reply(message)

    assert matched is True
    if isinstance(expected_template, set):
        assert template_id in expected_template
    else:
        assert template_id == expected_template
    assert "UNRELATED LEARNED ANSWER" not in response


def test_preorder_workflow_keeps_approved_kitchen_phone(isolated_db):
    _seed_competing_active_entry(isolated_db)

    response, matched, template_id, _ = _reply("How do I pre-order a meal?")

    assert matched is True
    assert template_id == "alacarte_prebooking_steps"
    assert "+91 87965 51990" in response


def test_active_learned_answer_still_serves_uncovered_question(isolated_db):
    knowledge_id = isolated_db.add_learned_knowledge(
        "Catering policy",
        "what is your catering policy",
        "ACTIVE LEARNED ANSWER IS STILL AVAILABLE",
    )

    response, matched, template_id, _ = _reply("What is your catering policy?")

    assert matched is True
    assert template_id == f"learned_{knowledge_id}"
    assert response == "ACTIVE LEARNED ANSWER IS STILL AVAILABLE"


@pytest.mark.parametrize("status", ["ARCHIVED", "DISABLED"])
def test_non_active_learned_answers_are_never_customer_served(isolated_db, status):
    knowledge_id = isolated_db.add_learned_knowledge(
        "Retired policy",
        "what is your retired policy",
        f"{status} ANSWER MUST NEVER BE SERVED",
    )
    assert isolated_db.set_custom_qa_status(knowledge_id, status, "routing-test")

    response, matched, template_id, _ = _reply("What is your retired policy?")

    assert f"{status} ANSWER MUST NEVER BE SERVED" not in response
    assert template_id != f"learned_{knowledge_id}"
    assert not (matched and template_id == f"learned_{knowledge_id}")
