from camellia import client_qa


EXPECTED_IDS = {
    "home_table_details",
    "home_table_price",
    "home_table_payment",
    "general_price",
    "general_details",
    "alacarte_prebook",
    "subscription_price",
    "subscription_details_client",
    "price_is_high",
    "bento_details_client",
    "bento_price_client",
    "best_time_to_book",
    "savoury_delivery_time",
    "chicken_kosha_price_client",
    "order_ready_handoff",
    "sondesh_delivery_time",
    "sondesh_price_client",
    "sondesh_details_client",
    "sondesh_unfamiliar_client",
    "delivery_out_of_area_client",
    "order_prebook",
    "general_payment_client",
    "minimum_sondesh_quantity_client",
}


def _match(question, facts=None):
    result = client_qa.match(question, facts or {})
    assert result is not None, question
    return result


def test_registry_is_parsed_from_authoritative_source_without_duplicate_merging():
    assert len(client_qa.REGISTRY) == 23
    assert {entry.qa_id for entry in client_qa.REGISTRY} == EXPECTED_IDS
    assert client_qa.REGISTRY_BY_ID["home_table_price"].answer != client_qa.REGISTRY_BY_ID["general_price"].answer
    assert client_qa.REGISTRY_BY_ID["home_table_payment"].answer != client_qa.REGISTRY_BY_ID["general_payment_client"].answer
    assert client_qa.REGISTRY_BY_ID["alacarte_prebook"].answer != client_qa.REGISTRY_BY_ID["order_prebook"].answer


def test_exact_questions_return_their_canonical_ids_and_source_copy():
    cases = [
        ("tell me more about this", {"home_table_context": True}, "home_table_details"),
        ("Whats the price?", {"home_table_context": True}, "home_table_price"),
        ("How to pay", {"home_table_context": True}, "home_table_payment"),
        ("whats the price", {}, "general_price"),
        ("Can you tell me more about this", {}, "general_details"),
        ("WHAT ARE THE NEXT STEPS TO PRE BOOK?", {}, "alacarte_prebook"),
        ("What is the price of SUBSCRIPTION - HOME FOOD AWAY FROM HOME", {}, "subscription_price"),
        ("Tell me more about SUBSCRIPTION - HOME FOOD AWAY FROM HOME", {}, "subscription_details_client"),
        ("PRICE IS HIGH", {}, "price_is_high"),
        ("tell me about BENGALI BENTO", {}, "bento_details_client"),
        ("what is pirce of  BENGALI BENTO", {}, "bento_price_client"),
        ("whats the BEST TIME TO BOOK", {}, "best_time_to_book"),
        ("WHATS THE DELIVERY TIME", {}, "savoury_delivery_time"),
        ("WHATS THE PRICE OF CHICKEN KOSHA", {}, "chicken_kosha_price_client"),
        ("How much time do you take to deliver sondesh", {}, "sondesh_delivery_time"),
        ("Whats the price of sondesh?", {}, "sondesh_price_client"),
        ("Tell me more about sondesh", {}, "sondesh_details_client"),
        ("Sondesh unfamiliar (new category)", {}, "sondesh_unfamiliar_client"),
        ("Do you deliver to xyz city?", {}, "delivery_out_of_area_client"),
        ("How do I pay?", {}, "general_payment_client"),
        ("Why is the Minimum order 9 pieces 250g for some flavours", {}, "minimum_sondesh_quantity_client"),
    ]
    for question, facts, expected_id in cases:
        result = _match(question, facts)
        assert result.entry.qa_id == expected_id
        assert result.entry.answer == client_qa.REGISTRY_BY_ID[expected_id].answer


def test_paraphrases_keep_the_same_canonical_answer_id():
    cases = [
        ("How much is the Bengali Bento?", "bento_price_client"),
        ("What does Bengali Bento cost?", "bento_price_client"),
        ("Tell me about your Bento options", "bento_details_client"),
        ("How long will Sondesh delivery take?", "sondesh_delivery_time"),
        ("What is the minimum quantity for a flavour?", "minimum_sondesh_quantity_client"),
        ("Can you explain why Sondesh is priced in multiples of nine?", "minimum_sondesh_quantity_client"),
        ("Is the price high because of extra charges?", "price_is_high"),
        ("Please tell me more about the Bengali home table", "home_table_details"),
        ("What are the payment options for the home table?", "home_table_payment"),
        ("Can you share details about your food formats?", "general_details"),
        ("How much do your meal options cost?", "general_price"),
        ("What is the best time to reserve?", "best_time_to_book"),
    ]
    for question, expected_id in cases:
        facts = {"home_table_context": True} if "home table" in question.lower() else {}
        assert _match(question, facts).entry.qa_id == expected_id


def test_dirty_state_cannot_hijack_explicit_current_intent():
    assert _match("whats the price", {"subscription_context": True}).entry.qa_id == "general_price"
    assert _match(
        "Can you tell me more about this",
        {"source_route": {"routing_topic": "Sondesh Story"}, "last_topic": "sondesh"},
    ).entry.qa_id == "general_details"
    assert _match("Whats the price?", {"home_table_context": True, "subscription_context": True}).entry.qa_id == "home_table_price"
    assert _match("How do I pay?", {"home_table_context": True, "source_context_superseded": True}).entry.qa_id == "general_payment_client"


def test_operational_placeholders_are_not_customer_output():
    forbidden = (
        "INSERT CURRENT MENU HERE",
        "INSERT 3 PICTURES OF SONDESH",
        "ATTACH RATE CHART",
        "INSERT AAMAR MENU HERE",
        "INSERT SIGNATURE MENU HERE",
        "INSERT LATEST WEEKLY LUNCH/ DINNER MENU HERE",
        "ADD LINK TO WHATSAPP",
        "24 HOURS AFTER SENDING ABOVE, SEND THIS",
    )
    for entry in client_qa.REGISTRY:
        assert not any(marker in entry.answer for marker in forbidden), entry.qa_id

    assert "sondesh_addon_images" in client_qa.REGISTRY_BY_ID["general_price"].asset_keys
    assert "subscription_rate_chart" in client_qa.REGISTRY_BY_ID["subscription_price"].asset_keys
    assert "aamar_menu" in client_qa.REGISTRY_BY_ID["sondesh_details_client"].asset_keys


def test_every_canonical_source_block_resolves_through_the_real_responder(monkeypatch):
    from camellia.config import config
    from camellia.responder import responder
    from camellia.session_memory import session_memory

    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    cases = [
        ("home_table_details", "tell me more about this", {"home_table_context": True}),
        ("home_table_price", "Whats the price?", {"home_table_context": True}),
        ("home_table_payment", "How to pay", {"home_table_context": True}),
        ("general_price", "whats the price", {}),
        ("general_details", "Can you tell me more about this", {}),
        ("alacarte_prebook", "WHAT ARE THE NEXT STEPS TO PRE BOOK?", {}),
        ("subscription_price", "What is the price of SUBSCRIPTION - HOME FOOD AWAY FROM HOME", {}),
        ("subscription_details_client", "Tell me more about SUBSCRIPTION - HOME FOOD AWAY FROM HOME", {}),
        ("price_is_high", "PRICE IS HIGH", {}),
        ("bento_details_client", "tell me about BENGALI BENTO", {}),
        ("bento_price_client", "what is pirce of BENGALI BENTO", {}),
        ("best_time_to_book", "whats the BEST TIME TO BOOK", {}),
        ("savoury_delivery_time", "WHATS THE DELIVERY TIME", {}),
        ("chicken_kosha_price_client", "WHATS THE PRICE OF CHICKEN KOSHA", {}),
        ("order_ready_handoff", "I am ready to place my order", {}),
        ("sondesh_delivery_time", "How much time do you take to deliver sondesh", {}),
        ("sondesh_price_client", "Whats the price of sondesh?", {}),
        ("sondesh_details_client", "Tell me more about sondesh", {}),
        ("sondesh_unfamiliar_client", "Sondesh unfamiliar (new category)", {}),
        ("delivery_out_of_area_client", "Do you deliver to xyz city?", {}),
        ("order_prebook", "WHAT ARE THE NEXT STEPS TO PRE BOOK?", {"order_flow_context": True}),
        ("general_payment_client", "How do I pay?", {}),
        ("minimum_sondesh_quantity_client", "Why is the Minimum order 9 pieces 250g for some flavours", {}),
    ]
    for index, (expected_id, question, seed) in enumerate(cases):
        user_id = f"source_golden_{index}_{expected_id}"
        session_memory.clear_session(user_id, "instagram")
        session_memory.get_facts(user_id, "instagram").update(seed)
        response, matched, _template_id, _images = responder.match_and_respond(
            question, user_id=user_id, channel="instagram",
        )
        facts = session_memory.get_facts(user_id, "instagram")
        assert matched is (expected_id != "order_ready_handoff"), expected_id
        if expected_id == "order_ready_handoff":
            continue
        # Existing deterministic routes remain authoritative for source,
        # state, media, and handoff semantics. The registry itself is tested
        # above; this integration check does not couple legacy route IDs to
        # the registry's internal ID.
        assert facts["decision_lane"] == "EXACT_APPROVED"
        assert facts["knowledge_sufficient"] is True
        assert response
