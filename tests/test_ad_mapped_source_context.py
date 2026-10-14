"""Exact Instagram ad-ID bindings inherit only their explicitly mapped source."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.background import BackgroundTasks

from camellia import app as app_module, database, handoff, instagram_chatbot_control
from camellia import instagram_routing, menu_catalogue, menu_images, menu_review, responder as responder_module
from camellia.ad_gate import describe_entry_point, EntryPoint
from camellia.config import config
from camellia.instagram_chatbot_control import TEST_ONLY
from camellia.instagram_routing import route_for_inbound, validate_route_payload
from camellia.session_memory import session_memory
from camellia import client_qa, approved_qa


@pytest.fixture
def ad_customer(tmp_path, monkeypatch):
    db = database.Database(str(tmp_path / "ad-source.db"))
    for module in (database, app_module, handoff, menu_catalogue,
                   menu_images, menu_review, responder_module):
        monkeypatch.setattr(module, "db", db)
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "ADMIN_API_KEY", "ad-source-test-admin")
    monkeypatch.setattr(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000")
    uid = "ad-source-" + uuid.uuid4().hex
    session_memory.clear_session(uid, "instagram")
    return uid, session_memory.get_facts(uid, "instagram"), db


def _mapped_source(ad_customer, kind, ad_id, *, topic_name="Bengali Bento",
                   response_mode="CUSTOM", approved_qa_id=None, approved_qa_ids=None,
                   source_context="sondesh"):
    _uid, _facts, db = ad_customer
    topic = instagram_routing.create_topic(topic_name)
    db.save_menu_image_draft("aamar_mishti", "/uploads/ad-source-menu.jpg", "fixture")
    db.publish_menu_image("aamar_mishti")
    shortcode = "ADPOST123" if kind == "post" else "ADREEL123"
    url = f"https://www.instagram.com/{'p' if kind == 'post' else 'reel'}/{shortcode}/"
    payload = {
        "source_type": kind,
        "url": url,
        "shortcode": shortcode,
        "media_id": f"meta-{kind}-media-12345",
        "ad_id": ad_id,
        "topic_id": topic["topic_id"],
        "source_description": f"Operator-authored {kind} description.",
        "response_mode": response_mode,
        "approved_qa_id": approved_qa_id,
        "menu_id": "aamar_mishti",
        "menu_mode": "SELECTED",
        "active": True,
    }
    if approved_qa_ids is not None:
        payload["approved_qa_ids"] = approved_qa_ids
        payload["approved_qa_id"] = approved_qa_ids[0]
        payload["source_context"] = source_context
    route = validate_route_payload(payload)
    saved = db.save_instagram_source_route(route)
    return saved


def _add_qa_alias(db, qa_id, context, question, alias):
    with db.get_connection() as conn:
        conn.execute('''INSERT INTO approved_qa_overrides
            (qa_id,record_type,question,answer,context,aliases_json,created_at,updated_at,created_by,updated_by)
            VALUES (?,'ADMIN_ADDED',?,'Exact approved test answer.',?, ?, 'now','now','test','test')''',
            (qa_id, question, context, '["' + alias + '"]'))


def _ad_entry(sender_id, ad_id, text, *, title="Untrusted display title"):
    return describe_entry_point({
        "sender": {"id": sender_id},
        "message": {"text": text, "referral": {
            "source": "ADS", "ad_id": ad_id,
            "ads_context_data": {"ad_title": title},
        }},
    })


def _process(ad_customer, text, entry):
    sender, _facts, _db = ad_customer
    with patch.object(app_module, "_send_or_queue_instagram_reply", return_value={"status": "sent"}) as send, \
         patch.object(app_module.escalator, "handle_escalation"):
        app_module._process_instagram_event(sender, text, entry.ad_id, entry_point=entry)
    return send


def _dispatch_durable_instagram_events(events):
    background_tasks = BackgroundTasks()
    durable_tasks = app_module._DurableInstagramTasks(background_tasks)
    app_module._dispatch_instagram_payload(
        {"entry": [{"id": "test-account", "messaging": events}]}, durable_tasks,
    )
    asyncio.run(background_tasks())


PRICE_VARIATIONS = (
    "How much does it cost?", "How much is it?", "What does it cost?",
    "What's the price?", "What is the price?", "Price please",
    "How much for this?", "Can you tell me the price?", "What does this cost?",
    "  WHAT’S THE PRICE?!  ",
)
PRICE_SUBJECTS = (
    ("Norom Pak Sondesh", "aamar_mishti", "sondesh_price_client"),
    ("Bengali Bento", "bento", "bento_price_client"),
    ("Bengali Home Table", None, "home_table_price"),
    ("Subscription", None, "subscription_price"),
)


def _price_source(ad_customer, name, product, qa_id):
    ad_customer[2].save_menu_image_draft("weekly_vegetarian", "/uploads/test-reviewed-current-weekly.jpeg", "fixture")
    ad_customer[2].publish_menu_image("weekly_vegetarian")
    route = instagram_routing.decode_route(_mapped_source(ad_customer, "post", "120246385008280540", topic_name=name))
    # Persist authoritative product IDs / Approved Q&A reference in the temp
    # test database. A free-form Sondesh title alone is intentionally insufficient.
    route.update(priority_products=[product] if product else [], menu_mode="NONE", menu_id=None)
    if product is None:
        route.update(response_mode="APPROVED_QA", approved_qa_id=qa_id)
    return ad_customer[2].save_instagram_source_route({**validate_route_payload(route), "routing_id": route["routing_id"]})


@pytest.mark.parametrize("message", PRICE_VARIATIONS)
@pytest.mark.parametrize("name,product,qa_id", PRICE_SUBJECTS)
def test_natural_price_uses_bound_canonical_subject_and_unchanged_approved_copy(
        eligible_weekly_asset, ad_customer, name, product, qa_id, message):
    route = _price_source(ad_customer, name, product, qa_id)
    sender, facts, db = ad_customer
    send = _process(ad_customer, message, _ad_entry(sender, "120246385008280540", message,
                    title="Misleading unrelated sweet/caption"))
    entry = client_qa.effective_entry(qa_id)
    assert facts["client_qa_id"] == qa_id
    assert facts["last_route_source"] == "EXACT_APPROVED"
    assert facts["source_route"]["routing_id"] == route["routing_id"]
    assert db.get_active_handoff("instagram", sender) is None
    stored = db.get_conversations(limit=1, channel="instagram", user_id=sender)[0]
    assert stored["bot_response"] == entry.answer
    assert send.called
    text = "\n\n".join(b["text"] for b in facts["ordered_response_blocks"] if b["kind"] == "TEXT")
    if qa_id == "home_table_price":
        assert facts["ordered_response_blocks"] == client_qa.build_home_table_reel_blocks(entry.answer)
    else:
        assert text == entry.answer
    expected = {"weekly_menu": eligible_weekly_asset,
                "aamar_menu": "/uploads/ad-source-menu.jpg",
                "signature_menu": "/images/Signature%20Sondesh.jpeg",
                "subscription_rate_chart": "/images/Everyday%20Meal%20Subscription%20Rate%20Chart.jpeg"}
    urls = [url for b in facts["ordered_response_blocks"] if b["kind"] != "TEXT" for url in b.get("urls", [])]
    assert urls == [expected[key] for key in entry.asset_keys]


@pytest.mark.parametrize("name,product,qa_id", PRICE_SUBJECTS)
def test_durable_ad_referral_natural_price_and_followup_real_routing(
        eligible_weekly_asset, ad_customer, name, product, qa_id):
    _price_source(ad_customer, name, product, qa_id)
    sender, facts, db = ad_customer
    event = {"sender": {"id": sender}, "message": {"mid": "natural-price-ad",
             "text": "How much does it cost?", "referral": {"source": "ADS",
             "ad_id": "120246385008280540", "ads_context_data": {"ad_title": "Untrusted display"}}}}
    with patch.object(app_module, "send_instagram_reply", return_value={"status": "sent", "message_id": "reply"}), \
         patch.object(app_module.escalator, "handle_escalation") as escalate:
        _dispatch_durable_instagram_events([event])
        _dispatch_durable_instagram_events([event])
    assert facts["client_qa_id"] == qa_id
    assert not escalate.called
    assert db.get_active_handoff("instagram", sender) is None
    assert len([r for r in db.get_conversations(limit=20, user_id=sender, channel="instagram")
                if r["user_message"] == "How much does it cost?"]) == 1
    _process(ad_customer, "What does this cost?", EntryPoint(source="typed"))
    assert facts["client_qa_id"] == qa_id
    assert db.get_active_handoff("instagram", sender) is None


@pytest.mark.parametrize("message,qa_id", [
    ("How much is the Bengali Bento?", "bento_price_client"),
    ("How much is the subscription?", "subscription_price"),
])
def test_explicit_price_subject_overrides_mapped_source_and_next_followup(
        eligible_weekly_asset, ad_customer, message, qa_id):
    _price_source(ad_customer, "Norom Pak Sondesh", "aamar_mishti", "sondesh_price_client")
    sender, facts, db = ad_customer
    _process(ad_customer, message, _ad_entry(sender, "120246385008280540", message))
    assert facts["client_qa_id"] == qa_id
    assert facts["source_context_superseded"]
    _process(ad_customer, "How much does it cost?", EntryPoint(source="typed"))
    assert facts["client_qa_id"] == qa_id
    assert db.get_active_handoff("instagram", sender) is None


@pytest.mark.parametrize("message", ["How much does XYZ sweet cost?", "What does paneer cost?", "chicken price"])
def test_unknown_named_price_never_inherits_bound_source(ad_customer, message):
    _price_source(ad_customer, "Norom Pak Sondesh", "aamar_mishti", "sondesh_price_client")
    sender, facts, db = ad_customer
    assert client_qa.match(message, {"last_topic": "sondesh"}) is None
    _process(ad_customer, message, _ad_entry(sender, "120246385008280540", message))
    assert facts.get("client_qa_id") != "sondesh_price_client"
    assert facts["last_route_source"] == "HUMAN_HANDOFF"
    assert db.get_active_handoff("instagram", sender)


@pytest.mark.parametrize("state", [
    {}, {"ad_product": "sondesh", "ad_title": "Sondesh"},
    {"last_topic": "sondesh", "active_topic": "bento"},
    {"source_route": {"routing_id": 1, "routing_topic": "Norom Pak Sondesh"}, "last_topic": "sondesh"},
    {"source_route": {"routing_id": 1, "priority_products": ["aamar_mishti", "bento"]}},
    {"source_route": {"routing_id": 1, "priority_products": ["unknown"]}, "last_topic": "bento"},
    {"source_route": {"routing_id": 1, "active": False, "priority_products": ["bento"]}},
    {"source_route": {"routing_id": 1, "priority_products": ["bento"], "approved_qa_id": "sondesh_price_client"}},
])
def test_missing_or_conflicting_subject_fails_closed(state):
    for message in PRICE_VARIATIONS:
        # Canonical general-price copy remains available only without context,
        # as before. Pronoun follow-ups never guess from the display-only ad.
        if any(k != "ad_product" and k != "ad_title" for k in state) or message not in {"What's the price?", "What is the price?", "Price please", "Can you tell me the price?", "  WHAT’S THE PRICE?!  "}:
            assert client_qa.match(message, state) is None


def test_unbound_ad_natural_price_does_not_borrow_title(ad_customer):
    sender, facts, db = ad_customer
    _process(ad_customer, "How much does it cost?", _ad_entry(sender, "unbound", "How much does it cost?", title="Bengali Bento"))
    assert facts.get("client_qa_id") not in {p[2] for p in PRICE_SUBJECTS}
    assert db.get_active_handoff("instagram", sender)


@pytest.mark.parametrize("key,value,qa_id", [
    ("last_topic", "sondesh_general", "sondesh_price_client"),
    ("active_topic", "bento", "bento_price_client"),
    ("active_product_context", "subscription", "subscription_price"),
    ("client_qa_id", "home_table_details", "home_table_price"),
])
def test_canonical_conversation_context_resolves_same_entry(key, value, qa_id):
    for message in PRICE_VARIATIONS:
        matched = client_qa.match(message, {key: value, "last_answer_id": "bengali_home_table_details"} if key == "client_qa_id" else {key: value})
        assert matched.entry == client_qa.effective_entry(qa_id)


def test_contextual_price_still_participates_in_admin_alias_ambiguity():
    row = {"aliases_json": '["How much does it cost?"]', "context": "bento", "status": "ACTIVE"}
    state = {"last_topic": "sondesh"}
    assert approved_qa.match("How much does it cost?", state, rows={"bento_price_client": row}) is None
    same = approved_qa.match("HOW MUCH DOES IT COST?!", state, rows={"sondesh_price_client": row})
    assert same.entry.qa_id == "sondesh_price_client"


@pytest.mark.parametrize("kind,ad_id", [("post", "ad-post-42"), ("reel", "ad-reel-42")])
def test_post_and_reel_ad_referrals_resolve_only_their_explicit_source_binding(ad_customer, kind, ad_id):
    saved = _mapped_source(ad_customer, kind, ad_id)
    route, method = route_for_inbound(ad_id=ad_id, media_id=None)

    assert method == "ad_id"
    assert route["routing_id"] == saved["routing_id"]
    assert route["source_type"] == kind
    assert route["canonical_url"].endswith("ADPOST123/" if kind == "post" else "ADREEL123/")


def test_ad_price_question_uses_canonical_answer_not_custom_attachment(ad_customer):
    _mapped_source(ad_customer, "post", "ad-price-42")
    sender = ad_customer[0]

    send = _process(ad_customer, "What is the price?", _ad_entry(sender, "ad-price-42", "What is the price?"))

    stored = ad_customer[2].get_conversations(limit=1, channel="instagram", user_id=sender)[0]
    assert "₹" in stored["bot_response"]
    assert "Operator-authored" not in stored["bot_response"]
    assert ad_customer[1]["last_route_source"] == "EXACT_APPROVED"
    assert send.call_count == 1


@pytest.mark.parametrize("kind,ad_id", [("post", "legacy-post-single-qa"), ("reel", "legacy-reel-single-qa")])
def test_exact_ad_binding_disambiguates_a_legacy_single_qa_from_global_alias_collision(
        ad_customer, kind, ad_id):
    sender, facts, db = ad_customer
    selected = client_qa.effective_entry("home_table_price")
    _add_qa_alias(db, "other_context_price", "bento", "How much does the Bento cost?", "price?")
    _mapped_source(ad_customer, kind, ad_id, topic_name="Bengali Home Table",
                   response_mode="APPROVED_QA", approved_qa_id=selected.qa_id)

    # The active source gives global matching a context, but the same wording
    # remains ambiguous when another active Q&A has an exact matching alias.
    route, _ = route_for_inbound(ad_id=ad_id, media_id=None)
    assert client_qa.match("price?", {"source_route": route}) is None
    send = _process(ad_customer, "price?", _ad_entry(sender, ad_id, "price?"))

    stored = db.get_conversations(limit=1, channel="instagram", user_id=sender)[0]
    assert facts["source_route"]["routing_id"] == route["routing_id"]
    assert facts.get("last_grounding") == "exact_approved"
    assert facts.get("client_qa_id") == selected.qa_id
    assert stored["bot_response"] == selected.answer, facts
    assert db.get_active_handoff("instagram", sender) is None
    assert send.called


def test_ad_tell_me_more_sends_bound_description_then_exact_selected_image(ad_customer):
    _mapped_source(ad_customer, "reel", "ad-more-42")
    sender = ad_customer[0]

    send = _process(ad_customer, "Tell me more", _ad_entry(sender, "ad-more-42", "Tell me more"))

    assert ad_customer[1]["last_route_source"] == "SOURCE_DESCRIPTION"
    assert ad_customer[1]["ordered_response_blocks"] == [
        {"kind": "TEXT", "text": "Operator-authored reel description."},
        {"kind": "IMAGE", "urls": ["/uploads/ad-source-menu.jpg"]},
    ]
    assert [call.kwargs.get("image_url") for call in send.call_args_list] == [None, ["/uploads/ad-source-menu.jpg"]]


def test_ad_source_context_persists_to_next_message_without_referral(ad_customer):
    _mapped_source(ad_customer, "post", "ad-followup-42")
    sender = ad_customer[0]
    _process(ad_customer, "Hi", _ad_entry(sender, "ad-followup-42", "Hi"))
    followup = EntryPoint(source="typed")

    _process(ad_customer, "Tell me more", followup)

    assert ad_customer[1]["ordered_response_blocks"] == [
        {"kind": "TEXT", "text": "Operator-authored post description."},
        {"kind": "IMAGE", "urls": ["/uploads/ad-source-menu.jpg"]},
    ]


def test_exact_ad_bundle_uses_canonical_answer_once_on_duplicate_delivery(ad_customer):
    sender, facts, db = ad_customer
    _mapped_source(ad_customer, "post", "bundle-ad", topic_name="Bengali Home Table",
                   response_mode="APPROVED_QA", approved_qa_ids=["home_table_price", "home_table_details"],
                   source_context="home_table")
    event = {"sender": {"id": sender}, "message": {"mid": "bundle-question-mid", "text": "price?",
             "referral": {"source": "ADS", "ad_id": "bundle-ad"}}}
    with patch.object(app_module, "send_instagram_reply", return_value={"status": "sent", "message_id": "bundle-reply"}) as send:
        _dispatch_durable_instagram_events([event])
        first_delivery_send_count = send.call_count
        _dispatch_durable_instagram_events([event])

    rows = [row for row in db.get_conversations(limit=20, channel="instagram", user_id=sender)
            if row["user_message"] == "price?"]
    assert facts["client_qa_id"] == "home_table_price"
    assert rows[0]["bot_response"] == client_qa.effective_entry("home_table_price").answer
    assert len(rows) == 1
    # A canonical answer may be split into several delivery chunks (text and
    # reviewed assets). A duplicate durable event must add no new sends.
    assert first_delivery_send_count > 0
    assert send.call_count == first_delivery_send_count


def test_exact_ad_with_unavailable_selected_qa_fails_closed_to_handoff(ad_customer):
    sender, facts, db = ad_customer
    route = _mapped_source(ad_customer, "reel", "invalid-qa-ad", topic_name="Bengali Home Table",
                            response_mode="APPROVED_QA", approved_qa_id="home_table_price")
    # Model legacy/stale persisted production data that passed validation when
    # saved but whose selected Q&A was later removed or made ineligible.
    with db.get_connection() as conn:
        conn.execute("UPDATE instagram_source_routes SET approved_qa_id=? WHERE routing_id=?",
                     ("expired_or_missing_qa", route["routing_id"]))

    send = _process(ad_customer, "What is the price?", _ad_entry(sender, "invalid-qa-ad", "What is the price?"))

    row = db.get_conversations(limit=1, channel="instagram", user_id=sender)[0]
    assert row["template_id"] == "instagram_support_handoff"
    assert row["bot_response"] != client_qa.effective_entry("home_table_price").answer
    assert facts.get("client_qa_id") != "expired_or_missing_qa"
    assert db.get_active_handoff("instagram", sender)
    assert send.called


def test_greeting_plus_selected_question_uses_canonical_answer(ad_customer):
    sender, facts, db = ad_customer
    _mapped_source(ad_customer, "reel", "greeting-question-ad",
                   response_mode="APPROVED_QA", approved_qa_ids=["general_price"], source_context="sondesh")

    send = _process(ad_customer, "Hi, what's the price?", _ad_entry(sender, "greeting-question-ad", "Hi, what's the price?"))

    expected = client_qa.effective_entry("general_price").answer
    row = db.get_conversations(limit=1, channel="instagram", user_id=sender)[0]
    assert facts["client_qa_id"] == "general_price"
    assert row["bot_response"] == expected
    assert send.called


def test_organic_greeting_uses_normal_greeting_and_non_greeting_stays_behind_ad_gate(ad_customer, monkeypatch):
    sender, facts, db = ad_customer
    route = _mapped_source(ad_customer, "post", "unrelated-ad")
    facts["source_route"] = instagram_routing.decode_route(route)
    monkeypatch.setattr(config, "BYPASS_AD_GATE", False)
    organic = {"sender": {"id": sender}, "message": {"mid": "organic-hi-mid", "text": "Hi"}}
    tasks = BackgroundTasks()
    app_module._dispatch_instagram_payload({"entry": [{"id": "test-account", "messaging": [organic]}]}, tasks)
    with patch.object(app_module, "send_instagram_reply", return_value={"status": "sent", "message_id": "organic-reply"}):
        asyncio.run(tasks())

    greeting = db.get_conversations(limit=1, channel="instagram", user_id=sender)[0]
    assert greeting["template_id"] in {"greeting", "system_conversation"}
    assert greeting["bot_response"]

    blocked = {"sender": {"id": sender}, "message": {"mid": "organic-question-mid", "text": "What is the price?"}}
    result = app_module._dispatch_instagram_payload({"entry": [{"id": "test-account", "messaging": [blocked]}]}, BackgroundTasks())
    assert result["results"][0]["action"] == "ignored_organic_dm"
    assert len(db.get_conversations(limit=10, channel="instagram", user_id=sender)) == 1


def test_unknown_or_unbound_ad_id_never_guesses_from_title_or_another_route(ad_customer):
    prior = _mapped_source(ad_customer, "post", "only-bound-ad", topic_name="Private Mapped Source")

    assert route_for_inbound(ad_id="unrelated-ad", media_id=None) == (None, None)
    assert route_for_inbound(ad_id="unbound-ad", media_id=None) == (None, None)
    assert route_for_inbound(ad_id=None, media_id=None) == (None, None)
    sender = ad_customer[0]
    ad_customer[1]["source_route"] = instagram_routing.decode_route(prior)
    _process(ad_customer, "Tell me more", _ad_entry(
        sender, "unrelated-ad", "Tell me more", title="Private Mapped Source",
    ))
    assert ad_customer[1].get("source_route") is None
    assert ad_customer[1].get("ordered_response_blocks") != [
        {"kind": "TEXT", "text": "Operator-authored post description."},
        {"kind": "IMAGE", "urls": ["/uploads/ad-source-menu.jpg"]},
    ]


def test_two_exact_ads_keep_selected_qa_bundles_isolated(ad_customer):
    sender, facts, db = ad_customer
    first = _mapped_source(ad_customer, "post", "isolated-ad-one", topic_name="Norom Pak Sondesh",
                           response_mode="APPROVED_QA", approved_qa_ids=["sondesh_price_client"],
                           source_context="sondesh")
    second = _mapped_source(ad_customer, "reel", "isolated-ad-two", topic_name="Bengali Home Table",
                            response_mode="APPROVED_QA", approved_qa_ids=["home_table_price"],
                            source_context="home_table")
    one, one_method = route_for_inbound(ad_id="isolated-ad-one", media_id=None)
    two, two_method = route_for_inbound(ad_id="isolated-ad-two", media_id=None)

    assert one_method == two_method == "ad_id"
    assert one["routing_id"] == first["routing_id"] and one["approved_qa_ids"] == ["sondesh_price_client"]
    assert two["routing_id"] == second["routing_id"] and two["approved_qa_ids"] == ["home_table_price"]

    send = _process(ad_customer, "price?", _ad_entry(sender, "isolated-ad-two", "price?"))
    stored = db.get_conversations(limit=1, channel="instagram", user_id=sender)[0]
    assert stored["bot_response"] == client_qa.effective_entry("home_table_price").answer
    assert facts["client_qa_id"] == "home_table_price"
    assert send.called


def test_disabled_exact_binding_remains_unmapped(ad_customer):
    route = _mapped_source(ad_customer, "post", "disabled-ad")
    assert ad_customer[2].set_instagram_ad_binding_active("disabled-ad", False)
    assert route_for_inbound(ad_id="disabled-ad", media_id=None) == (None, None)
    assert route["routing_id"] is not None


def test_ad_referral_does_not_claim_a_post_or_reel_identity(ad_customer):
    entry = _ad_entry(ad_customer[0], "ad-id-only", "Tell me more", title="A post title")

    assert entry.ad_id == "ad-id-only"
    assert entry.ad_title == "A post title"
    assert entry.media_id is None


def test_ad_id_binding_is_durable_and_cannot_be_ambiguous(ad_customer):
    first = _mapped_source(ad_customer, "post", "durable-ad")
    assert ad_customer[2].get_instagram_source_route(first["routing_id"])["ad_id"] == "durable-ad"
    assert route_for_inbound(ad_id="durable-ad", media_id=None)[0]["routing_id"] == first["routing_id"]

    second_topic = instagram_routing.create_topic("Other exact source")
    conflicting = validate_route_payload({
        "source_type": "reel", "url": "https://www.instagram.com/reel/OTHER123/",
        "ad_id": "durable-ad", "topic_id": second_topic["topic_id"],
        "source_description": "A different source.", "active": True,
    })
    with pytest.raises(ValueError, match="already bound"):
        ad_customer[2].save_instagram_source_route(conflicting)

    # Legacy ambiguous rows fail closed at runtime instead of selecting the
    # most recently edited description or asset.
    other = ad_customer[2].save_instagram_source_route(validate_route_payload({
        "source_type": "reel", "url": "https://www.instagram.com/reel/OTHER456/",
        "source_description": "Legacy duplicate.", "active": True,
    }))
    with ad_customer[2].get_connection() as conn:
        conn.execute("UPDATE instagram_source_routes SET ad_id=? WHERE routing_id=?", ("durable-ad", other["routing_id"]))
        conn.commit()
    assert route_for_inbound(ad_id="durable-ad", media_id=None) == (None, None)
    assert ad_customer[2].instagram_ad_binding_status("durable-ad") == "AMBIGUOUS"


def test_admin_can_persist_an_exact_ad_binding_on_an_existing_mapped_source(ad_customer):
    _sender, _facts, db = ad_customer
    db.save_menu_image_draft("aamar_mishti", "/uploads/admin-bound-menu.jpg", "fixture")
    db.publish_menu_image("aamar_mishti")
    client = TestClient(app_module.app)
    headers = {"x-admin-key": config.ADMIN_API_KEY}
    source_payload = {
        "url": "https://www.instagram.com/p/ADMINPOST123/",
        "routing_topic": "Bengali Bento",
        "source_description": "Operator-authored admin-bound description.",
        "menu_id": "aamar_mishti", "menu_mode": "SELECTED", "active": True,
    }

    created = client.post("/admin/instagram-routes", headers=headers, json=source_payload)
    assert created.status_code == 200, created.text
    route_id = created.json()["route"]["routing_id"]
    bound = client.post("/admin/instagram-routes", headers=headers,
                        json={**source_payload, "ad_id": "admin-exact-ad-123"})

    assert bound.status_code == 200, bound.text
    saved = db.get_instagram_source_route(route_id)
    assert bound.json()["route"]["routing_id"] == route_id
    assert saved["ad_id"] == "admin-exact-ad-123"
    assert saved["source_description"] == source_payload["source_description"]
    assert saved["menu_id"] == "aamar_mishti"
    assert route_for_inbound(ad_id="admin-exact-ad-123", media_id=None)[0]["routing_id"] == route_id


def test_multiple_ads_can_share_one_exact_source_mapping(ad_customer):
    route = _mapped_source(ad_customer, "post", "ad-alias-primary")
    db = ad_customer[2]
    db.bind_instagram_ad("ad-alias-secondary", route["routing_id"], bound_by="test")
    db.bind_instagram_ad("ad-alias-tertiary", route["routing_id"], bound_by="test")

    for ad_id in ("ad-alias-primary", "ad-alias-secondary", "ad-alias-tertiary"):
        resolved, method = route_for_inbound(ad_id=ad_id, media_id=None)
        assert method == "ad_id"
        assert resolved["routing_id"] == route["routing_id"]
    assert {item["ad_id"] for item in db.list_instagram_ad_bindings(route["routing_id"])} == {
        "ad-alias-primary", "ad-alias-secondary", "ad-alias-tertiary",
    }


def test_one_ad_id_cannot_bind_to_two_active_routes(ad_customer):
    first = _mapped_source(ad_customer, "post", "unique-bind-ad")
    second_topic = instagram_routing.create_topic("Second exact mapping")
    second = ad_customer[2].save_instagram_source_route(validate_route_payload({
        "source_type": "reel", "url": "https://www.instagram.com/reel/SECOND123/",
        "topic_id": second_topic["topic_id"], "source_description": "Second source", "active": True,
    }))
    with pytest.raises(ValueError, match="already bound"):
        ad_customer[2].bind_instagram_ad("unique-bind-ad", second["routing_id"], bound_by="test")
    assert route_for_inbound(ad_id="unique-bind-ad", media_id=None)[0]["routing_id"] == first["routing_id"]


def test_unknown_ads_are_observed_and_can_be_bound_by_admin(ad_customer):
    sender = ad_customer[0]
    db = ad_customer[2]
    route = _mapped_source(ad_customer, "post", "known-source-ad")
    event = {
        "sender": {"id": sender},
        "message": {"mid": "unknown-ad-event-1", "text": "Hello", "referral": {
            "source": "ADS", "ad_id": "new-unmapped-ad", "ads_context_data": {"ad_title": "Untrusted name"},
        }},
    }
    app_module._dispatch_instagram_payload({"entry": [{"id": "test-account", "messaging": [event]}]}, BackgroundTasks())

    observed = db.list_observed_instagram_ads()
    assert any(item["ad_id"] == "new-unmapped-ad" and item["display_title"] == "Untrusted name" for item in observed)
    assert route_for_inbound(ad_id="new-unmapped-ad", media_id=None) == (None, None)

    client = TestClient(app_module.app)
    response = client.post("/admin/instagram-ads/new-unmapped-ad/bind",
                           headers={"x-admin-key": config.ADMIN_API_KEY},
                           json={"routing_id": route["routing_id"]})
    assert response.status_code == 200, response.text
    assert route_for_inbound(ad_id="new-unmapped-ad", media_id=None)[0]["routing_id"] == route["routing_id"]
    assert db.list_instagram_ad_binding_history("new-unmapped-ad")[-1]["action"] == "BOUND"


@pytest.mark.parametrize("same_payload,order", [
    (False, "post_first"), (False, "ad_first"),
    (True, "post_first"), (True, "ad_first"),
])
def test_both_ad_post_race_orders_use_durable_dispatch_and_are_idempotent(ad_customer, same_payload, order):
    sender = ad_customer[0]
    db = ad_customer[2]
    route = _mapped_source(ad_customer, "post", "race-bound-ad")
    suffix = uuid.uuid4().hex
    post = {
        "sender": {"id": sender}, "timestamp": 1700000000000,
        "message": {"mid": f"shared-post-{suffix}", "attachments": [{"type": "ig_post", "payload": {
            "ig_post_media_id": "race-post-media-123", "title": "non-authoritative",
        }}]},
    }
    ads = {
        "sender": {"id": sender}, "timestamp": 1700000000100,
        "message": {"mid": f"ads-referral-{suffix}", "text": "What is the price?", "referral": {
            "source": "ADS", "ad_id": "race-bound-ad",
        }},
    }
    ordered = [post, ads] if order == "post_first" else [ads, post]
    with patch.object(app_module, "send_instagram_reply", return_value={"status": "sent", "message_id": "sent-test-mid"}) as send, \
         patch.object(app_module.escalator, "handle_escalation"):
        if same_payload:
            _dispatch_durable_instagram_events(ordered)
        else:
            _dispatch_durable_instagram_events([ordered[0]])
            _dispatch_durable_instagram_events([ordered[1]])
        # Meta retries both deliveries with the same message IDs. Durable job
        # identity and the consumed media state must make the replay inert.
        if same_payload:
            _dispatch_durable_instagram_events(ordered)
        else:
            _dispatch_durable_instagram_events([ordered[0]])
            _dispatch_durable_instagram_events([ordered[1]])

    assert db.get_active_handoff("instagram", sender) is None
    assert ad_customer[1]["last_route_source"] == "EXACT_APPROVED"
    assert ad_customer[1]["source_route"]["routing_id"] == route["routing_id"]
    stored = db.get_conversations(limit=1, channel="instagram", user_id=sender)[0]
    assert stored["bot_response"]
    assert "Operator-authored" not in stored["bot_response"]
    query_rows = [row for row in db.get_conversations(limit=20, channel="instagram", user_id=sender)
                  if row["user_message"] == "What is the price?"]
    assert len(query_rows) == 1
    assert query_rows[0]["template_id"] != "waiting_for_human"
    assert send.call_count >= 1

    assert db.get_pending_instagram_media_status(sender, "race-post-media-123") == "CONSUMED"


def test_consumed_media_handler_is_defensively_terminal(ad_customer):
    sender = ad_customer[0]
    db = ad_customer[2]
    db.record_instagram_ads_referral_seen(sender, "observed-ad", "ad-before-post-event")
    pending = db.record_pending_instagram_media("post-after-ad-event", sender, "consumed-post-123")
    assert pending["status"] == "CONSUMED"

    app_module._handle_shared_attachment(sender, None, "consumed-post-123")

    assert db.get_active_handoff("instagram", sender) is None
    assert db.get_pending_instagram_media_status(sender, "consumed-post-123") == "CONSUMED"


def test_ads_referral_does_not_cancel_or_resume_an_older_unrelated_handoff(ad_customer):
    sender = ad_customer[0]
    db = ad_customer[2]
    route = _mapped_source(ad_customer, "post", "handoff-protection-ad")
    handoff_row, created = db.get_or_create_handoff(
        handoff_ref="CAM-OLD-UNRELATED", source_channel="instagram", source_user_id=sender,
        reason="CUSTOMER_REQUESTED_HUMAN", summary="Earlier independent support request.",
        known_entities={}, unresolved_topic="refund question", escalation_id=None,
        expires_at=None,
    )
    assert created
    post = {
        "sender": {"id": sender}, "timestamp": 1700000000300,
        "message": {"mid": "post-after-old-handoff", "attachments": [{"type": "ig_post", "payload": {
            "ig_post_media_id": "post-after-old-handoff-123",
        }}]},
    }
    ads = {
        "sender": {"id": sender}, "timestamp": 1700000000400,
        "message": {"mid": "ad-after-old-handoff", "text": "How much does it cost?", "referral": {
            "source": "ADS", "ad_id": "handoff-protection-ad",
        }},
    }
    payload = lambda event: {"entry": [{"id": "test-account", "messaging": [event]}]}
    app_module._dispatch_instagram_payload(payload(post), BackgroundTasks())
    app_module._dispatch_instagram_payload(payload(ads), BackgroundTasks())
    current = db.get_active_handoff("instagram", sender)
    assert current["handoff_ref"] == handoff_row["handoff_ref"]
    assert current["reason"] == "CUSTOMER_REQUESTED_HUMAN"
    assert current["status"] not in {"RESUMED", "RESOLVED", "EXPIRED"}
    assert route_for_inbound(ad_id="handoff-protection-ad", media_id=None)[0]["routing_id"] == route["routing_id"]


def test_unpaired_post_is_finalized_after_grace_period(ad_customer):
    sender = ad_customer[0]
    db = ad_customer[2]
    event = {
        "sender": {"id": sender}, "timestamp": 1700000000200,
        "message": {"mid": "unpaired-post-mid", "attachments": [{"type": "ig_post", "payload": {
            "ig_post_media_id": "unpaired-post-media-123",
        }}]},
    }
    app_module._dispatch_instagram_payload({"entry": [{"id": "test-account", "messaging": [event]}]}, BackgroundTasks())
    with db.get_connection() as conn:
        conn.execute("UPDATE instagram_pending_media_events SET expires_at=0 WHERE media_id=?", ("unpaired-post-media-123",))
        conn.commit()
    with patch.object(app_module, "send_instagram_reply", return_value={"status": "sent", "message_id": "handoff-notice"}), \
         patch.object(app_module.escalator, "handle_escalation"):
        app_module._finalize_expired_unmapped_posts()

    handoff = db.get_active_handoff("instagram", sender)
    assert handoff
    assert "Unmapped shared Instagram Post" in handoff["reason"]
    with db.get_connection() as conn:
        assert conn.execute("SELECT status FROM instagram_pending_media_events WHERE media_id=?",
                            ("unpaired-post-media-123",)).fetchone()[0] == "FINALIZED"


def test_home_table_menu_from_bound_ad_still_hands_off_without_source_asset(ad_customer):
    _mapped_source(ad_customer, "post", "home-table-ad", topic_name="Bengali Home Table")
    sender = ad_customer[0]
    send = _process(
        ad_customer, "show me the menu",
        _ad_entry(sender, "home-table-ad", "show me the menu"),
    )

    assert ad_customer[1]["last_route_source"] == "HUMAN_HANDOFF"
    assert ad_customer[1]["ordered_response_blocks"] == []
    assert send.call_count == 1
    assert send.call_args.kwargs.get("image_url") is None
    assert ad_customer[2].get_active_handoff("instagram", sender)


def test_unsupported_question_from_bound_ad_still_creates_human_handoff(ad_customer):
    _mapped_source(ad_customer, "reel", "ad-unsupported-42")
    sender = ad_customer[0]
    send = _process(
        ad_customer, "Can you arrange a violin player?",
        _ad_entry(sender, "ad-unsupported-42", "Can you arrange a violin player?"),
    )

    assert ad_customer[2].get_active_handoff("instagram", sender)
    assert ad_customer[1]["last_route_source"] == "HUMAN_HANDOFF"
    assert send.call_count == 1
    assert send.call_args.kwargs.get("image_url") is None


def test_explicit_source_ad_binding_does_not_change_test_only_gate(ad_customer):
    sender, _facts, db = ad_customer
    _mapped_source(ad_customer, "reel", "test-only-ad")
    instagram_chatbot_control.set_mode(TEST_ONLY, "test", database=db)
    instagram_chatbot_control.add_test_account(sender_id=sender, actor="test", database=db)
    before = instagram_chatbot_control.get_state(database=db)

    assert instagram_chatbot_control.is_sender_allowed(sender, database=db)
    assert not instagram_chatbot_control.is_sender_allowed("not-allowlisted", database=db)
    _process(ad_customer, "Tell me more", _ad_entry(sender, "test-only-ad", "Tell me more"))
    after = instagram_chatbot_control.get_state(database=db)

    assert after["mode"] == before["mode"] == TEST_ONLY
    assert [row["sender_id"] for row in after["test_accounts"]] == [sender]
