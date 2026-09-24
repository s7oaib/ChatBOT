"""
Precedence between a shared reel's route and the rest of the matcher.

The production failure this covers: a customer shared the configured reel and
asked "Tell me more", and the reply came from a broad learned-knowledge entry
instead of the reel's route. Source context had resolved correctly
(``source_routing_id=1``, ``source_match_method="shared_media_url"``) but
``source_context_used`` was 0 — the branch was skipped because the session
still carried a ``last_topic`` from earlier in the day, and ``can_use_source``
reads any stale topic as "this conversation already has a subject".

Sharing a post *is* a change of subject, so establishing the context now
clears that stale topic. Explicit intent is untouched and still wins, and an
open question the bot asked still takes precedence over a vague follow-up.
"""
import os

# Deliberately not setting BYPASS_AD_GATE in the environment: config reads it
# at import, so a module-level default here would disable the ad gate for
# every other test file that happens to run after this one. The autouse
# fixture below patches it per test instead.
os.environ.setdefault("DB_PATH", "test_camellia.db")

import pytest

import camellia.app as app_module
from camellia import client_qa
from camellia.database import Database
from camellia.responder import responder
from camellia.session_memory import session_memory

SHORTCODE = "DdAnO6MTok1"
ROUTE = {
    "source_type": "reel",
    "canonical_url": f"https://www.instagram.com/reel/{SHORTCODE}/",
    "shortcode": SHORTCODE,
    "media_id": None, "ad_id": None, "campaign_id": None,
    "routing_topic": "Wedding / Event Sondesh",
    "knowledge_scope": [],
    "escalation_policy": "human_if_unsupported",
    "priority_products": ["jolbhora", "kanchagolla", "aamar_mishti"],
    "active": True,
}


@pytest.fixture(autouse=True)
def bypass_ad_gate(monkeypatch):
    """Production answers organic DMs, and a shared reel is organic traffic."""
    monkeypatch.setattr(app_module.config, "BYPASS_AD_GATE", True)


@pytest.fixture
def configured_route(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "precedence.db"))
    database.save_instagram_source_route(dict(ROUTE))
    import camellia.database as db_module
    monkeypatch.setattr(db_module, "db", database)
    return database


def _session(uid, **facts_extra):
    session_memory.clear_session(uid, "instagram")
    facts = session_memory.get_facts(uid, "instagram")
    facts.update(facts_extra)
    return facts


def _ask(uid, message):
    text, matched, template_id, _images = responder.match_and_respond(
        message, user_id=uid, channel="instagram",
    )
    used = bool(session_memory.get_facts(uid, "instagram").get("source_context_used"))
    return text, template_id, used


def _share_reel(uid, configured_route, *, stale_topic="sondesh_general"):
    """The real sequence: a session with history, then the reel is shared."""
    _session(uid, last_topic=stale_topic)
    app_module._establish_shared_reel_context(uid, {"shortcode": SHORTCODE})


# --- the reported failure --------------------------------------------------

def test_tell_me_more_without_a_configured_description_hands_off(configured_route):
    uid = "prec_tell_more"
    _share_reel(uid, configured_route)
    text, template_id, used = _ask(uid, "Tell me more")
    assert template_id is None
    assert used is False
    assert "manager of kitchens" in text.lower()


def test_a_stale_topic_does_not_authorize_a_legacy_reel_answer(configured_route):
    """A source label/priority is not customer-answer authority."""
    uid = "prec_stale"
    _share_reel(uid, configured_route, stale_topic="savoury")
    assert session_memory.get_facts(uid, "instagram").get("last_topic") is None
    text, template_id, used = _ask(uid, "Tell me more")
    assert used is False
    assert template_id is None
    assert "manager of kitchens" in text.lower()


def test_bare_price_after_a_shared_reel_hands_off_without_product_authority(configured_route):
    uid = "prec_price"
    # The source has legacy priority products, but its mapped topic does not
    # identify a product or provide an approved Description.
    configured_route.save_instagram_source_route({**ROUTE, "routing_topic": "Wedding / Event"})
    _share_reel(uid, configured_route)
    text, template_id, used = _ask(uid, "Price?")
    assert used is False
    assert template_id is None
    assert "manager of kitchens" in text.lower()


def test_what_about_this_after_a_shared_reel_uses_the_source_route(configured_route):
    uid = "prec_what_about"
    _share_reel(uid, configured_route)
    _text, template_id, used = _ask(uid, "What about this?")
    assert used is False


# --- explicit intent still wins -------------------------------------------

def test_explicit_kanchagolla_beats_priority_product_one(configured_route):
    uid = "prec_kancha"
    _share_reel(uid, configured_route)
    _text, template_id, used = _ask(uid, "What about Kanchagolla?")
    assert "jolbhora" not in (template_id or "")
    assert used is False


def test_explicit_delivery_question_beats_the_reel_context(configured_route):
    uid = "prec_delivery"
    _share_reel(uid, configured_route)
    text, template_id, used = _ask(uid, "Do you deliver to Delhi?")
    assert used is False
    assert "jolbhora" not in (template_id or "")


def test_a_named_topic_question_is_not_hijacked_by_the_reel(configured_route):
    """An explicit question about something else must still reach its own
    answer — the reel context only fills in for vague messages."""
    uid = "prec_named"
    _share_reel(uid, configured_route)
    _text, template_id, used = _ask(uid, "Tell me about the Bengali Home Table")
    assert used is False
    assert not (template_id or "").startswith("source_")


# --- everything else keeps working ----------------------------------------

def test_no_source_route_leaves_normal_matching_alone(configured_route):
    uid = "prec_none"
    _session(uid, last_topic="sondesh_general")
    _text, template_id, used = _ask(uid, "Tell me more")
    assert used is False
    assert not (template_id or "").startswith("source_")


def test_an_unconfigured_reel_does_not_clear_the_topic(configured_route):
    """Only a reel we actually route for is allowed to change the subject."""
    uid = "prec_unconfigured"
    _session(uid, last_topic="savoury")
    app_module._establish_shared_reel_context(uid, {"shortcode": "DdHBVMzpRq5"})
    assert session_memory.get_facts(uid, "instagram").get("last_topic") == "savoury"
    assert session_memory.get_facts(uid, "instagram").get("source_route") is None


def test_an_open_question_the_bot_asked_still_wins(configured_route):
    """awaiting_* is a reply the customer owes us; the share does not cancel it."""
    uid = "prec_flow"
    _share_reel(uid, configured_route)
    session_memory.get_facts(uid, "instagram")["awaiting_area_confirmation"] = True
    _text, _template_id, used = _ask(uid, "Tell me more")
    assert used is False


def test_vague_source_message_creates_no_order(configured_route):
    uid = "prec_no_order"
    _share_reel(uid, configured_route)
    _ask(uid, "Tell me more")
    facts = session_memory.get_facts(uid, "instagram")
    draft = facts.get("order_draft") or {}
    # An empty draft carries a default quantity of 1; what says no order was
    # started is that nothing has been chosen and no handoff exists.
    assert not draft.get("flavour")
    assert not draft.get("collection")
    assert not draft.get("size")
    assert not facts.get("handoff_ref")


def test_priority_products_cannot_authorize_a_legacy_source_answer(configured_route):
    """Without an approved QA match or Description, source context hands off."""
    uid = "prec_canonical"
    _share_reel(uid, configured_route)
    text, template_id, used = _ask(uid, "Tell me more")
    assert used is False
    assert template_id is None
    assert "manager of kitchens" in text.lower()
