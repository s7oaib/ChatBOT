"""Whole-turn authority through the production Instagram responder entry point."""
import uuid

import pytest

from camellia import client_qa
from camellia.database import db
from camellia.responder import responder
from camellia.session_memory import session_memory
from camellia.intent_guards import can_candidate_own_turn, evaluate_turn_authority, is_payment_method_request


SUNDAY_LUNCH = """Hello do you have 3 places free for coming Sunday lunch? My dad suddenly
underwent an emergency eye surgery last week. He has his check up post surgery
tomorrow. If all goes well, i will make payments by tomorrow night latest
Wednesday morning. Do let me know please."""

BLOCKED = [
    "Would you be open to a collaboration where our products are featured in your menu?",
    "We would love to partner with Camellia for upcoming events.",
    "Can we work together? We would like you to stock our products.",
    "I run a food brand and would like to collaborate with you.",
    "Do you have 3 places free for coming Sunday lunch?",
    "Are there 2 seats available this weekend?", "Do you have space for four for lunch?",
    "Can I reserve 3 places for Sunday?", "Can you hold 4 seats for us?",
    "Is there room for 6 people at Sunday lunch?", "Any availability for lunch this Sunday?",
    "Table for 3 available on Sunday?", SUNDAY_LUNCH,
    "Do you have 3 places free Sunday? I can pay tomorrow.",
    "Are there 2 seats available? We'll make payment tonight.",
    "Can you reserve 4 places? Once confirmed I will pay.",
    "Do you cater private events and create a custom menu?",
    "Can I reserve 3 seats for lunch? I saw your menu.",
    "Do you have wheelchair access? I was looking at your menu.",
    "Is Chicken Kosha available Sunday?", "Can I reserve Chicken Kosha for Sunday?",
    "Can you customize Chicken Kosha?", "Is Bento available Sunday?",
    "Can I reserve 3 Bento lunches for an event?",
    "Can I pay tomorrow after you confirm availability?", "Can you customize the Bento?",
    "Can I cancel my subscription?", "Can I pause my subscription?", "Can I skip next week?",
    "Can I reschedule my subscription delivery?", "Can I change my subscription dates?",
    "Is there space for 3 at the Home Table Sunday? If yes I can pay tomorrow.",
    "What is Bento price and do you have parking?",
    "How can I pay and can I reserve three seats Sunday?",
    "Can I book Sunday lunch for 3?", "Do you have lunch this Sunday?",
    "Tell me about Chicken Kosha.", "Do you deliver Chicken Kosha?",
    "Does Bento contain lupin?",
    "What is Bento price and do you gift wrap?",
]


@pytest.fixture
def instagram(monkeypatch, eligible_weekly_asset):
    user = "authority-" + uuid.uuid4().hex
    monkeypatch.setattr("camellia.responder.config.STRUCTURED_NLU_ENABLED", False)
    monkeypatch.setattr("camellia.responder.config.SEMANTIC_ROUTER_ENABLED", False)
    def reply(message):
        return responder.match_and_respond(message, user_id=user, channel="instagram")
    return reply, session_memory.get_facts(user, "instagram")


def assert_handoff(result, facts):
    text, matched, route, images = result
    assert matched is False
    assert images in (None, [])
    assert text == responder._generic_fallback()
    assert route not in {"approved_menu", "GUARD_payment_method", "general_payment_client"}
    assert "WEEKLY_SAVOURY_BROWSE" not in facts.get("last_intents", [])
    assert not any(word in text.lower() for word in ("upi", "razorpay", "bank transfer", "confirmed"))


@pytest.mark.parametrize("message", BLOCKED)
def test_unsupported_primary_request_cannot_leak_secondary_answer(instagram, message):
    reply, facts = instagram
    assert_handoff(reply(message), facts)


@pytest.mark.parametrize("context", ["payment", "menu", "bento", "subscription", "home_table", "sondesh", "order", "story", "post", "reel", "ad"])
@pytest.mark.parametrize("message", [SUNDAY_LUNCH, "Can I cancel my subscription?", "Do you have parking? I saw your menu."])
def test_explicit_new_gap_outranks_stale_context(instagram, context, message):
    reply, facts = instagram
    if context in {"story", "post", "reel"}:
        facts["source_route"] = {"routing_id": "exact-existing-test-route", "source_type": context,
                                 "active": True, "source_description": "Bento menu", "response_mode": "CUSTOM"}
    elif context == "order":
        facts["order_intake_draft"] = {"item": "bento", "status": "ORDER_DRAFT_INCOMPLETE"}
    else:
        facts.update(last_topic=context, active_topic=context, active_product_context=context,
                     ad_product="bento" if context == "ad" else None,
                     home_table_context=True, subscription_context=context == "subscription")
    assert_handoff(reply(message), facts)


@pytest.mark.parametrize("message,qa_id", [
    ("How can I pay?", "general_payment_client"), ("How do I pay?", "general_payment_client"),
    ("What payment options do you accept?", "general_payment_client"),
    ("Can I pay by UPI?", "general_payment_client"), ("Do you accept bank transfer?", "general_payment_client"),
    ("Please share payment details.", "general_payment_client"), ("Can I pay by card?", "general_payment_client"),
    ("What is the price of Chicken Kosha?", "chicken_kosha_price_client"),
    ("How much is Chicken Kosha?", "chicken_kosha_price_client"), ("Chicken Kosha rate?", "chicken_kosha_price_client"),
    ("Tell me about Bengali Bento.", "bento_details_client"), ("What is Bengali Bento price?", "bento_price_client"),
    ("How much for the Bento?", "bento_price_client"),
    ("Tell me about the subscription.", "subscription_details_client"),
    ("What subscriptions do you offer?", "subscription_details_client"),
    ("What is the subscription price?", "subscription_price"), ("How much is the meal plan?", "subscription_price"),
    ("How do I pay for the subscription?", "general_payment_client"),
    ("How do I pay for Chicken Kosha?", "general_payment_client"),
    ("How do I pay for the Home Table?", "home_table_payment"),
    ("What is the Home Table price?", "home_table_price"),
    ("Tell me about the Bengali Home Table.", "home_table_details"),
])
def test_exact_approved_positive_controls(instagram, message, qa_id):
    reply, facts = instagram
    text, matched, route, images = reply(message)
    assert matched is True
    assert facts.get("client_qa_id") == qa_id
    expected = "\n\n".join(block.value for block in client_qa.effective_entry(qa_id).blocks if block.kind == "TEXT")
    assert text == expected


@pytest.mark.parametrize("message", ["Show me this week's menu.", "Can I see the weekly menu?",
    "Please send the current menu.", "What's on the menu this week?", "Veg menu please.", "Non veg weekly menu please."])
def test_genuine_weekly_menu_owns_turn(instagram, eligible_weekly_asset, message):
    reply, facts = instagram
    result = reply(message)
    assert result[1] is True
    assert result[2] == "approved_menu"
    assert result[3] == [eligible_weekly_asset]


@pytest.mark.parametrize("question", ["Do you have seats available for Sunday lunch?", "Can I pause my subscription?",
    "Can I collaborate with you?"])
def test_active_exact_custom_authority_remains_eligible(instagram, question):
    reply, facts = instagram
    answer = "Approved test authority for this exact enquiry."
    key = db.add_learned_knowledge("Exact test authority", question, answer, source="client_admin",
        response_sequence=[{"type": "text", "text": answer}])
    result = reply(question)
    assert result[:3] == (answer, True, f"learned_{key}")


def test_nearby_custom_answer_cannot_cover_unresolved_compound(instagram):
    reply, facts = instagram
    db.add_learned_knowledge("Payment test", "How can I pay?", "Unrelated test payment answer.", source="client_admin",
        response_sequence=[{"type": "text", "text": "Unrelated test payment answer."}])
    assert_handoff(reply("How can I pay and do you have 3 seats Sunday?"), facts)


@pytest.mark.parametrize("message", ["event", "brand", "products", "menu", "Sunday", "lunch", "people"])
def test_bare_nouns_do_not_create_operational_intent(message):
    assert not evaluate_turn_authority(message).blocks_shortcut


@pytest.mark.parametrize("message", ["I will make payment tomorrow.", "We will pay tonight.", "I saw your payment details."])
def test_future_payment_statement_is_not_payment_method_request(message):
    assert not is_payment_method_request(message)


@pytest.mark.parametrize("message,candidate", [
    ("Is Chicken Kosha available Sunday?", "chicken_kosha_price_client"),
    ("How do I pay for the subscription?", "subscription_details_client"),
    ("Can I cancel my subscription?", "subscription_details_client"),
    ("I was looking at your menu.", "approved_menu"),
    ("I see your menu.", "approved_menu"),
    ("I can see your menu.", "approved_menu"),
])
def test_candidate_ownership_requires_actual_intent(message, candidate):
    assert not can_candidate_own_turn(message, candidate)


@pytest.mark.parametrize("message", ["I see your menu.", "I can see your menu."])
@pytest.mark.parametrize("context", [None, "story", "post", "reel", "ad"])
def test_menu_reference_does_not_send_flyer(instagram, message, context):
    reply, facts = instagram
    if context:
        facts["source_route"] = {"routing_id": "exact-existing-test-route", "source_type": context,
                                 "active": True, "routing_topic": "Bento", "response_mode": "CUSTOM",
                                 "source_description": "Existing approved source description."}
    result = reply(message)
    assert result[2] != "approved_menu"
    assert not result[3]
    assert "WEEKLY_SAVOURY_BROWSE" not in facts.get("last_intents", [])


def test_exact_custom_alias_covers_whole_turn(instagram):
    reply, facts = instagram
    question = "Do you have seats available for Sunday lunch?"
    answer = "Approved exact alias test authority."
    key = db.add_learned_knowledge("Alias authority", "Sunday seats enquiry", answer, source="client_admin",
        aliases=[question], response_sequence=[{"type": "text", "text": answer}])
    assert reply(question)[:3] == (answer, True, f"learned_{key}")


@pytest.mark.parametrize("enabled", [True, False])
def test_provider_cannot_override_known_gap(instagram, monkeypatch, enabled):
    from camellia.ai_responder import ai_responder
    monkeypatch.setattr("camellia.responder.config.STRUCTURED_NLU_ENABLED", enabled)
    monkeypatch.setattr("camellia.responder.config.SEMANTIC_ROUTER_ENABLED", enabled)
    def forbidden(*args, **kwargs):
        raise AssertionError("A known unresolved primary intent must not call the model")
    monkeypatch.setattr(ai_responder, "_query_groq", forbidden)
    reply, facts = instagram
    assert_handoff(reply(SUNDAY_LUNCH), facts)


def test_fully_covered_compound_keeps_both_approved_answers(instagram):
    reply, facts = instagram
    text, matched, route, images = reply("What is Bento price and how do I pay?")
    assert matched is True
    assert client_qa.effective_entry("general_payment_client").answer in text
    assert "Bento" in text
    assert facts["knowledge_sufficient"] is True


MENU_VARIANTS = [
    "Let me see the weekly menu.", "Let me see this week's menu.",
    "Let me see your weekly menu.", "Can I have a look at the weekly menu?",
    "Can I see this week's menu?", "Show me the weekly menu.",
    "Send me the weekly menu.", "Please share this week's menu.",
    "Hi, can I see the weekly menu?",
]
PAYMENT_VARIANTS = [
    "How do we pay?", "How can we pay?", "Where do we make the payment?",
    "Can we pay by UPI?", "What payment options do we have?",
    "Please share the payment details.", "Hey, how do we pay?",
]
COMPOUND_VARIANTS = [
    "What is the Bento price? Can you gift wrap?",
    "What is the Bento price. Do you gift wrap?",
    "What is the Bento price! Can you gift wrap?",
    "What is the Bento price; Can you gift wrap?",
    "Tell me about Sondesh? Can you make it sugar free?",
    "How much is Chicken Kosha? Is it available this Sunday?",
    "Show me the menu. Can you cater a wedding?",
    "How can I pay? Can you reserve 4 seats for Sunday?",
]


@pytest.mark.parametrize("message", MENU_VARIANTS)
def test_request_shaped_menu_variants(instagram, eligible_weekly_asset, message):
    reply, facts = instagram
    result = reply(message)
    assert result[1:3] == (True, "approved_menu")
    assert result[3] == [eligible_weekly_asset]
    assert "WEEKLY_SAVOURY_BROWSE" in facts["last_intents"]
    assert not facts.get("last_llm_called")


@pytest.mark.parametrize("message", [
    "I love your weekly menu.", "We would like our products featured in your menu.",
    "Would you feature our products in your menu?",
    "I saw your menu while asking about collaboration.",
    "Do you cater events with a custom menu?", "Your menu looks lovely.",
    "The menu was mentioned in the post.",
])
def test_descriptive_menu_is_not_browsing(instagram, message):
    reply, facts = instagram
    result = reply(message)
    assert result[2] != "approved_menu"
    assert not result[3]
    assert "WEEKLY_SAVOURY_BROWSE" not in facts.get("last_intents", [])


@pytest.mark.parametrize("message", COMPOUND_VARIANTS)
def test_sentence_compound_cannot_drop_unsupported_clause(instagram, message):
    reply, facts = instagram
    assert evaluate_turn_authority(message).blocks_shortcut
    assert client_qa._match_base(message) is None
    assert_handoff(reply(message), facts)


@pytest.mark.parametrize("message", PAYMENT_VARIANTS)
def test_plural_payment_uses_unchanged_approved_authority(instagram, message):
    reply, facts = instagram
    result = reply(message)
    assert result[1] is True
    assert facts["client_qa_id"] == "general_payment_client"
    expected = "\n\n".join(b.value for b in client_qa.effective_entry("general_payment_client").blocks if b.kind == "TEXT")
    assert result[0] == expected
    assert not facts.get("last_llm_called")


@pytest.mark.parametrize("message", [
    "We will pay tomorrow.", "We can make payment Wednesday.",
    "I already paid yesterday.", "Once you confirm availability we will pay.",
    "My father will make the payment.",
])
def test_incidental_payment_is_not_method_request(instagram, message):
    reply, facts = instagram
    assert not is_payment_method_request(message)
    result = reply(message)
    assert result[2] not in {"general_payment_client", "GUARD_payment_method", "home_table_payment"}
    assert facts.get("client_qa_id") != "general_payment_client"


@pytest.mark.parametrize("message", ["Hiii", "Namaste", "Hi there", "Hi", "Hello", "Hey", "Namaskar", "Hello there"])
def test_pure_greetings_use_existing_system_reply(instagram, message):
    from camellia.grounded_answers import system_reply
    reply, facts = instagram
    result = reply(message)
    assert result[:3] == (system_reply("Hi"), True, "system_conversation")
    assert not result[3]
    assert facts["last_route_source"] == "SYSTEM_CONVERSATION"
    assert not facts.get("last_llm_called")


@pytest.mark.parametrize("message", [
    "Hello, do you have 3 places free Sunday?",
    "Hiii, I'd like to collaborate with you.",
])
def test_greeting_prefix_does_not_hide_unsupported_request(instagram, message):
    reply, facts = instagram
    assert_handoff(reply(message), facts)


@pytest.mark.parametrize("context,message,expected", [
    ("bento", COMPOUND_VARIANTS[0], "handoff"),
    ("payment", MENU_VARIANTS[0], "menu"),
    ("menu", PAYMENT_VARIANTS[0], "payment"),
])
def test_new_grammar_outranks_stale_context(instagram, eligible_weekly_asset, context, message, expected):
    reply, facts = instagram
    facts.update(last_topic=context, active_topic=context, active_product_context=context)
    result = reply(message)
    if expected == "handoff":
        assert_handoff(result, facts)
    elif expected == "menu":
        assert result[1:3] == (True, "approved_menu")
        assert result[3] == [eligible_weekly_asset]
    else:
        assert result[1] is True
        assert facts["client_qa_id"] == "general_payment_client"


def test_sentence_compound_exact_custom_authority_only(instagram):
    reply, facts = instagram
    question = COMPOUND_VARIANTS[0]
    nearby = "What is the Bento price?"
    db.add_learned_knowledge("Nearby test", nearby, "Nearby answer.", source="client_admin",
        response_sequence=[{"type": "text", "text": "Nearby answer."}])
    assert_handoff(reply(question), facts)
    answer = "Approved exact whole-turn test authority."
    key = db.add_learned_knowledge("Whole-turn test", question, answer, source="client_admin",
        response_sequence=[{"type": "text", "text": answer}])
    assert reply(question)[:3] == (answer, True, f"learned_{key}")
