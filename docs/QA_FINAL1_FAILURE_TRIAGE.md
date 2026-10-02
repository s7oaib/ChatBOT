# QA-FINAL1 full-suite failure triage

Evidence: existing final run — 3605 passed, 67 failed, 1 warning. No full rerun during triage.

A: 3 · B: 64 · C: 0 · D: 0

| Group | A | B |
|---|---:|---:|
| test_canonical_knowledge_layer | 0 | 10 |
| test_client_approved_only_policy | 0 | 1 |
| test_client_confirmed_answers | 0 | 11 |
| test_client_qa_acceptance | 0 | 1 |
| test_groq_model_stabilization | 0 | 1 |
| test_home_table_context_progression | 0 | 1 |
| test_ice_breaker_write | 0 | 1 |
| test_instagram_account_guard | 1 | 9 |
| test_instagram_source_referential | 0 | 2 |
| test_menu_archive | 2 | 0 |
| test_menu_permanent_delete | 0 | 1 |
| test_phase2c_menu_authority | 0 | 2 |
| test_provider_outage_routing | 0 | 3 |
| test_stress_25_agents | 0 | 19 |
| test_weekly_dishes_orderable | 0 | 1 |
| test_weekly_savoury_lifecycle | 0 | 1 |

| Class | Exact original failing test | Justification |
|---|---|---|
| B | `tests.test_canonical_knowledge_layer.TestUnknownTopicsCannotBorrowAnUnrelatedAnswer::test_a_real_question_is_still_answered[what is the price of jolbhora-jolbhora]` | Legacy Jolbhora/delivery/storage/packaging copy has no effective Approved Q&A binding; handoff must record insufficiency rather than approved legacy facts. |
| B | `tests.test_canonical_knowledge_layer.TestUnknownTopicsCannotBorrowAnUnrelatedAnswer::test_a_real_question_is_still_answered[do you deliver to noida-delivery_area]` | Legacy Jolbhora/delivery/storage/packaging copy has no effective Approved Q&A binding; handoff must record insufficiency rather than approved legacy facts. |
| B | `tests.test_canonical_knowledge_layer.TestUnknownTopicsCannotBorrowAnUnrelatedAnswer::test_a_real_question_is_still_answered[how do i store the sondesh-GUARD_shelf_life_storage]` | Legacy Jolbhora/delivery/storage/packaging copy has no effective Approved Q&A binding; handoff must record insufficiency rather than approved legacy facts. |
| B | `tests.test_canonical_knowledge_layer.TestUnknownTopicsCannotBorrowAnUnrelatedAnswer::test_a_real_question_is_still_answered[how do you pack the sweets-packaging]` | Legacy Jolbhora/delivery/storage/packaging copy has no effective Approved Q&A binding; handoff must record insufficiency rather than approved legacy facts. |
| B | `tests.test_canonical_knowledge_layer.TestEveryFactualAnswerIsTraceable::test_a_compatibility_answer_records_its_source_and_sufficiency[how do you pack the sweets]` | Legacy Jolbhora/delivery/storage/packaging copy has no effective Approved Q&A binding; handoff must record insufficiency rather than approved legacy facts. |
| B | `tests.test_canonical_knowledge_layer.TestEveryFactualAnswerIsTraceable::test_a_compatibility_answer_records_its_source_and_sufficiency[how do i store the sondesh]` | Legacy Jolbhora/delivery/storage/packaging copy has no effective Approved Q&A binding; handoff must record insufficiency rather than approved legacy facts. |
| B | `tests.test_canonical_knowledge_layer.TestEveryFactualAnswerIsTraceable::test_a_compatibility_answer_records_its_source_and_sufficiency[what is the price of jolbhora]` | Legacy Jolbhora/delivery/storage/packaging copy has no effective Approved Q&A binding; handoff must record insufficiency rather than approved legacy facts. |
| B | `tests.test_canonical_knowledge_layer.TestEveryFactualAnswerIsTraceable::test_a_compatibility_answer_records_its_source_and_sufficiency[do you deliver to noida]` | Legacy Jolbhora/delivery/storage/packaging copy has no effective Approved Q&A binding; handoff must record insufficiency rather than approved legacy facts. |
| B | `tests.test_canonical_knowledge_layer.TestPartialMultiIntent::test_the_verified_price_is_answered_and_the_discount_is_not_invented` | Legacy Jolbhora/delivery/storage/packaging copy has no effective Approved Q&A binding; handoff must record insufficiency rather than approved legacy facts. |
| B | `tests.test_canonical_knowledge_layer.TestPartialMultiIntent::test_availability_is_escalated_while_the_product_answer_stands` | Legacy Jolbhora/delivery/storage/packaging copy has no effective Approved Q&A binding; handoff must record insufficiency rather than approved legacy facts. |
| B | `tests.test_client_approved_only_policy::test_live_menu_uses_only_the_documented_deterministic_menu_source` | Instagram weekly browse now sends only the current authoritative flyer, using approved_menu; no menu-price prose. |
| B | `tests.test_client_confirmed_answers::test_a_confirmed_question_reaches_its_confirmed_answer[are you halal certified-GUARD_halal_ingredients_oil]` | Historical seed assertions remain valid as seed history; live unanswered topics must hand off, while effective Approved Q&A bindings retain their answers. |
| B | `tests.test_client_confirmed_answers::test_a_confirmed_question_reaches_its_confirmed_answer[what oil do you cook in-GUARD_halal_ingredients_oil]` | Historical seed assertions remain valid as seed history; live unanswered topics must hand off, while effective Approved Q&A bindings retain their answers. |
| B | `tests.test_client_confirmed_answers::test_a_confirmed_question_reaches_its_confirmed_answer[is it nut free-GUARD_allergen_nutfree_glutenfree]` | Historical seed assertions remain valid as seed history; live unanswered topics must hand off, while effective Approved Q&A bindings retain their answers. |
| B | `tests.test_client_confirmed_answers::test_a_confirmed_question_reaches_its_confirmed_answer[does it contain nuts-GUARD_allergen_nutfree_glutenfree]` | Historical seed assertions remain valid as seed history; live unanswered topics must hand off, while effective Approved Q&A bindings retain their answers. |
| B | `tests.test_client_confirmed_answers::test_a_confirmed_question_reaches_its_confirmed_answer[my child has a severe nut allergy is it safe-GUARD_allergen_nutfree_glutenfree]` | Historical seed assertions remain valid as seed history; live unanswered topics must hand off, while effective Approved Q&A bindings retain their answers. |
| B | `tests.test_client_confirmed_answers::test_a_confirmed_question_reaches_its_confirmed_answer[how should i store the sondesh-GUARD_shelf_life_storage]` | Historical seed assertions remain valid as seed history; live unanswered topics must hand off, while effective Approved Q&A bindings retain their answers. |
| B | `tests.test_client_confirmed_answers::test_a_confirmed_question_reaches_its_confirmed_answer[where is your kitchen-GUARD_pickup_address_hours]` | Historical seed assertions remain valid as seed history; live unanswered topics must hand off, while effective Approved Q&A bindings retain their answers. |
| B | `tests.test_client_confirmed_answers::test_a_confirmed_question_reaches_its_confirmed_answer[who can i speak to directly-GUARD_staff_names_manager_contact]` | Historical seed assertions remain valid as seed history; live unanswered topics must hand off, while effective Approved Q&A bindings retain their answers. |
| B | `tests.test_client_confirmed_answers::test_a_confirmed_question_reaches_its_confirmed_answer[what number should i call for pickup directions-GUARD_pickup_address_hours]` | Historical seed assertions remain valid as seed history; live unanswered topics must hand off, while effective Approved Q&A bindings retain their answers. |
| B | `tests.test_client_confirmed_answers::test_a_confirmed_question_reaches_its_confirmed_answer[difference between aamar mishti and signature-GUARD_collection_differentiation]` | Historical seed assertions remain valid as seed history; live unanswered topics must hand off, while effective Approved Q&A bindings retain their answers. |
| B | `tests.test_client_confirmed_answers::test_a_confirmed_question_reaches_its_confirmed_answer[how do i book the subscription-subscription_booking_steps]` | Historical seed assertions remain valid as seed history; live unanswered topics must hand off, while effective Approved Q&A bindings retain their answers. |
| B | `tests.test_client_qa_acceptance::test_client_questions_use_the_specific_approved_route[share weekly menu-savoury_food_intro]` | Weekly browse is the current flyer-only approved_menu route, not savoury_food_intro prose. |
| B | `tests.test_groq_model_stabilization::test_both_model_failures_keep_the_deterministic_compound_route` | Provider outages cannot revive unapproved Jolbhora prices or Delhi coverage; retain bounded two-model retry and safe handoff. |
| B | `tests.test_home_table_context_progression::test_explicit_new_subject_overrides_home_table[Show this week's Savoury menu]` | Explicit new weekly subject exits Home Table via approved_menu, the current flyer route. |
| B | `tests.test_ice_breaker_write.TestTheDefaultsAreAnswerable::test_each_default_routes_to_the_answer_it_promises[delivery_area-Do you deliver to my area?]` | Delivery default remains deterministic but cannot promise coverage without an effective approved answer. |
| B | `tests.test_instagram_account_guard::test_approved_opening_hours_variants_use_canonical_answer[opening hours]` | Unapproved legacy opening hours now require exactly one human handoff, not historical hours prose; account guard behavior unchanged. |
| B | `tests.test_instagram_account_guard::test_approved_opening_hours_variants_use_canonical_answer[What are your opening hours?]` | Unapproved legacy opening hours now require exactly one human handoff, not historical hours prose; account guard behavior unchanged. |
| B | `tests.test_instagram_account_guard::test_approved_opening_hours_variants_use_canonical_answer[What are the opening hours?]` | Unapproved legacy opening hours now require exactly one human handoff, not historical hours prose; account guard behavior unchanged. |
| B | `tests.test_instagram_account_guard::test_approved_opening_hours_variants_use_canonical_answer[When are you open?]` | Unapproved legacy opening hours now require exactly one human handoff, not historical hours prose; account guard behavior unchanged. |
| B | `tests.test_instagram_account_guard::test_approved_opening_hours_variants_use_canonical_answer[When do you open?]` | Unapproved legacy opening hours now require exactly one human handoff, not historical hours prose; account guard behavior unchanged. |
| B | `tests.test_instagram_account_guard::test_approved_opening_hours_variants_use_canonical_answer[What time are you open?]` | Unapproved legacy opening hours now require exactly one human handoff, not historical hours prose; account guard behavior unchanged. |
| B | `tests.test_instagram_account_guard::test_approved_opening_hours_variants_use_canonical_answer[What time do you open?]` | Unapproved legacy opening hours now require exactly one human handoff, not historical hours prose; account guard behavior unchanged. |
| B | `tests.test_instagram_account_guard::test_approved_opening_hours_variants_use_canonical_answer[kitchen hours]` | Unapproved legacy opening hours now require exactly one human handoff, not historical hours prose; account guard behavior unchanged. |
| B | `tests.test_instagram_account_guard::test_approved_opening_hours_variants_use_canonical_answer[kitchen opening hours]` | Unapproved legacy opening hours now require exactly one human handoff, not historical hours prose; account guard behavior unchanged. |
| A | `tests.test_instagram_account_guard::test_approved_opening_hours_does_not_create_a_handoff` | App informational exemption suppressed the durable handoff for an unmatched retired hours route. Require matched informational answers; assert one notice and one escalation. |
| B | `tests.test_instagram_source_referential::test_composite_greeting_keeps_menu_and_handoff_intents` | Explicit weekly browse outranks generic detail NLU and sends current flyer only, not meals/order prose. |
| B | `tests.test_instagram_source_referential::test_explicit_weekly_menu_precedes_generic_detail_nlu` | Explicit weekly browse outranks generic detail NLU and sends current flyer only, not meals/order prose. |
| A | `tests.test_menu_archive::test_asking_for_previous_menus_sends_the_archived_cards` | Empty delegated template was escalated before archive formatter; empty archive incorrectly required an image. Restore existing archive authority. |
| A | `tests.test_menu_archive::test_no_archive_means_no_promise_of_one` | Empty delegated template was escalated before archive formatter; empty archive incorrectly required an image. Restore existing archive authority. |
| B | `tests.test_menu_permanent_delete::test_weekly_vegetarian_hard_delete_is_not_reseeded_after_restart` | Deleted menu still stays deleted; unavailable asset fails closed through Manager handoff, not retired finalising copy. |
| B | `tests.test_phase2c_menu_authority::test_reviewed_template_price_cannot_be_overridden_by_old_response_copy` | Response Library menu delegates contain no business prose; reviewed price is sourced separately through approved menu retrieval. |
| B | `tests.test_phase2c_menu_authority::test_materialized_templates_cannot_bypass_reviewed_price` | Response Library menu delegates contain no business prose; reviewed price is sourced separately through approved menu retrieval. |
| B | `tests.test_provider_outage_routing::test_a_named_flavour_price_is_still_answered_during_an_outage` | Outage cannot revive unapproved flavour prices, locality confirmation interview, or subscription change policy. |
| B | `tests.test_provider_outage_routing.TestOpenStatesKeepTheirOwnFlow::test_an_area_answer_still_completes_the_delivery_slot` | Outage cannot revive unapproved flavour prices, locality confirmation interview, or subscription change policy. |
| B | `tests.test_provider_outage_routing.TestOpenStatesKeepTheirOwnFlow::test_delivery_wording_inside_a_subscription_flow_stays_on_the_subscription` | Outage cannot revive unapproved flavour prices, locality confirmation interview, or subscription change policy. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_01_bengali_native_speaker` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_03_budget_haggler` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_04_health_and_dietary_inquirer` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_06_multi_turn_order_with_session_memory` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_07_out_of_area_bangalore_inquiry` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_08_typo_and_slang_comprehension` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_11_delhi_ncr_localities_coverage` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_12_kitchen_self_pickup` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_13_artisanal_differentiation` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_15_high_value_gifting_signature_a2` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_16_weekly_rotation_dishes` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_17_payment_methods_and_qr_code` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_18_shelf_life_and_storage` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_22_allergen_nut_safety` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_23_collection_comparison` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_24_packaging_sustainability` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_26_savoury_lunch_dinner_to_bento` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_27_subscription_plan_to_booking_flow` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_stress_25_agents.Test25AgentStressScenarios::test_agent_29_returning_customer_after_24h_gap_flow` | Persona transcripts expect retired independent business prose or obsolete weekly browse output; preserve messages while asserting approved current text, asset routing, or human handoff. No unsupported pack/price facts. |
| B | `tests.test_weekly_dishes_orderable::test_the_price_quoted_is_the_price_charged` | Price equality belongs to an explicit item price question; menu browsing is asset delegation and does not quote a price list. |
| B | `tests.test_weekly_savoury_lifecycle::test_unavailable_weekly_price_never_uses_expired_or_upcoming` | Unavailable/expired/upcoming weekly authority returns handoff with no price/image; retired finalising wording is not required. |

## Corrections and validation

Runtime corrections in this triage:
- `camellia/responder.py`: delegated menu templates may reach their existing formatter without independent library prose; `previous_menus` may return the supported deterministic empty-archive response without an image. Other missing menu assets still fail closed.
- `camellia/app.py`: informational-route exemption requires a matched answer, so an unmatched retired hours route creates the ordinary durable human handoff.

Test corrections preserve original customer messages where supported, replace retired factual assertions with controlled handoff checks, pin current Approved Q&A/source answers and flyer-only routes, and verify reviewed price authority independently of empty menu delegation copy. The explicit weekly price fixture now includes an approved current version in its isolated database. The previous empty-archive handoff expectation in `test_subtopic_sufficiency_and_safety.py` was corrected to the existing deterministic empty-archive response.

Validation:
- Directly affected focused modules: **646 passed, 0 failed, 1 existing warning**.
- Explicit targeted impacted runtime modules: **991 passed, 0 failed, 1 existing warning** (includes final weekly-price fixture).
- Mandatory smoke through `run_impacted_tests.py --smoke-only`: **68 passed, 0 failed, 1 existing warning**.
- Critical flake8 (E9/F63/F7/F82): PASS, 0 findings.
- Menu/template schema validation: PASS.
- `git diff --check`: PASS.
- Impact runner selection: CRITICAL, full regression selected; full execution deliberately deferred under operator instruction. Targeted impacted checks do not satisfy the final full-regression release gate.
- Full suite rerun: NO.
- Commit/push/PR, baseline promotion, handover update, production changes: NONE.

## Exact files edited during this triage

- `camellia/app.py`
- `camellia/responder.py`
- `tests/test_canonical_knowledge_layer.py`
- `tests/test_client_approved_only_policy.py`
- `tests/test_client_confirmed_answers.py`
- `tests/test_client_qa_acceptance.py`
- `tests/test_groq_model_stabilization.py`
- `tests/test_home_table_context_progression.py`
- `tests/test_ice_breaker_write.py`
- `tests/test_instagram_account_guard.py`
- `tests/test_instagram_source_referential.py`
- `tests/test_menu_permanent_delete.py`
- `tests/test_phase2c_menu_authority.py`
- `tests/test_provider_outage_routing.py`
- `tests/test_stress_25_agents.py`
- `tests/test_subtopic_sufficiency_and_safety.py`
- `tests/test_weekly_dishes_orderable.py`
- `tests/test_weekly_savoury_lifecycle.py`
- `docs/QA_FINAL1_FAILURE_TRIAGE.md`
