"""Explicit, reviewed test ownership; unknown executable inputs fail closed.

Keep existing safety tests intact. Entries are pytest paths/node IDs, not -k
expressions, so a renamed or missing test produces a collection failure.
"""

SMOKE_TESTS = (
    "tests/test_admin_portal.py::test_health_reports_healthy_when_the_bot_can_answer",
    "tests/test_admin_security.py::test_valid_password_login_creates_secure_server_session",
    "tests/test_admin_security.py::test_authenticated_browser_write_requires_csrf_and_accepts_valid_csrf",
    "tests/test_admin_portal.py::test_missing_or_invalid_instagram_signature_is_rejected",
    "tests/test_admin_portal.py::test_missing_secret_with_enforcement_enabled_rejects_instagram_post",
    "tests/test_admin_portal.py::test_instagram_get_challenge_rejects_untrusted_verify_tokens",
    "tests/test_channel_policy.py::test_instagram_approved_faq_still_works_without_handoff",
    "tests/test_channel_policy.py::test_unregistered_product_price_request_hands_off",
    "tests/test_whatsapp_cleanup.py::test_whatsapp_webhook_surface_is_removed_and_instagram_route_remains",
    "tests/test_whatsapp_cleanup.py::test_web_tester_reports_manual_only_without_calling_responder",
    "tests/test_phase3_repository_cleanup.py::test_no_executable_retired_provider_or_live_configuration_reference",
    "tests/test_client_approved_qa.py::test_registry_is_parsed_from_authoritative_source_without_duplicate_merging",
    "tests/test_client_approved_only_policy.py::test_unapproved_business_question_cannot_reach_rag_or_groq",
    "tests/test_menu_review_approval.py::test_text_draft_never_publishes_and_retains_missing_facts",
    "tests/test_phase2b_pending_menu_handoff.py::test_newer_draft_blocks_old_approved_price",
    "tests/test_waiting_for_human.py::test_waiting_customer_message_is_stored_without_ai_reply_or_new_escalation",
    "tests/test_candidate_approval_editing.py::test_existing_unsafe_rows_are_hidden_and_not_eligible",
    "tests/test_candidate_approval_editing.py::test_approval_audit_records_approver_without_losing_edit_audit",
    "tests/test_instagram_source_referential.py::test_referential_message_without_source_clarifies",
    "tests/test_instagram_source_referential.py::test_explicit_product_intent_overrides_source_context",
    "tests/test_simple_source_mapping.py::test_description_and_attachment_survive_reopen_and_exact_id",
    "tests/test_phase2f_observability_operations.py::test_no_public_debug_or_metrics_routes",
    "tests/test_phase4_security_acceptance.py::test_public_tester_debug_and_whatsapp_unavailable",
    "tests/test_phase2f_observability_operations.py::test_provider_failure_does_not_kill_deterministic_readiness",
    "tests/test_phase3_repository_cleanup.py::test_sensitive_flags_keep_effective_defaults_and_parsing",
)

AREA_TESTS = {
    "menu_admin": (
        "tests/test_menu_upload_manager_ui.py", "tests/test_admin_ui_structure.py",
        "tests/test_pre_handover_menu_taxonomy.py", "tests/test_menu_category_management.py",
        "tests/test_topic_menu_catalogue.py", "tests/test_weekly_publish_operator_ux.py",
        "tests/test_phase2c_menu_workspace.py", "tests/test_phase2c_weekly_workspace.py",
        "tests/test_phase45_menu_linking.py", "tests/test_menu_permanent_delete.py",
    ),
    "menu_authority": (
        "tests/test_weekly_savoury_lifecycle.py", "tests/test_menu_review_approval.py",
        "tests/test_menu_review_before_publish.py", "tests/test_phase2b_pending_menu_handoff.py",
        "tests/test_phase2c_menu_authority.py", "tests/test_phase2c_review_workflow.py",
        "tests/test_phase2c_startup_authority.py", "tests/test_phase2a_customer_correctness.py",
        "tests/test_menu_image_manager.py", "tests/test_menu_approved_rag.py",
        "tests/test_weekly_dishes_orderable.py", "tests/test_upload_decoupling.py",
        "tests/test_flyer_downscaling.py", "tests/test_vision_ocr_resilience.py",
        "tests/test_post_phase3_menu_vision_hotfix.py", "tests/test_admin_portal.py",
    ),
    "responder": (
        "tests/test_client_approved_only_policy.py", "tests/test_client_approved_qa.py",
        "tests/test_client_qa_acceptance.py", "tests/test_client_followups.py",
        "tests/test_home_table_context_progression.py", "tests/test_ordered_approved_response.py",
        "tests/test_hybrid_rag_architecture.py", "tests/test_grounded_answer_lanes.py",
        "tests/test_menu_approved_rag.py", "tests/test_semantic_router_fallback.py",
        "tests/test_explicit_intent_interrupts_stale_flow.py", "tests/test_channel_policy.py",
        "tests/test_waiting_for_human.py", "tests/test_instagram_source_referential.py",
        "tests/test_routing_precedence.py", "tests/test_routing_golden.py",
        "tests/test_provider_outage_routing.py", "tests/test_phase2a_customer_correctness.py",
        "tests/test_phase2b_pending_menu_handoff.py", "tests/test_subtopic_sufficiency_and_safety.py",
    ),
    "instagram": (
        "tests/test_instagram_ads_inventory.py", "tests/test_ad_mapped_source_context.py",
        "tests/test_instagram_identity.py", "tests/test_instagram_source_routing.py",
        "tests/test_instagram_source_referential.py", "tests/test_instagram_story_routing.py",
        "tests/test_instagram_story_sync.py", "tests/test_instagram_story_lifecycle.py",
        "tests/test_instagram_attachment_diagnostics.py", "tests/test_graph_media_lookup.py",
        "tests/test_verify_media_by_id.py", "tests/test_shared_post_pairing.py",
        "tests/test_shared_reel_routing.py", "tests/test_shared_reel_precedence.py",
        "tests/test_simple_source_mapping.py", "tests/test_channel_policy.py",
        "tests/test_admin_portal.py", "tests/test_phase4_security_acceptance.py",
    ),
    "ordering": (
        "tests/test_order_intake.py", "tests/test_weekly_dishes_orderable.py",
        "tests/test_confirmed_substitution.py", "tests/test_handoff_order_state.py",
        "tests/test_phase2c_menu_authority.py", "tests/test_phase2b_pending_menu_handoff.py",
        "tests/test_phase2a_customer_correctness.py", "tests/test_weekly_savoury_lifecycle.py",
    ),
    "handoff": (
        "tests/test_waiting_for_human.py", "tests/test_handoff_order_state.py",
        "tests/test_channel_policy.py", "tests/test_whatsapp_cleanup.py",
        "tests/test_candidate_approval_editing.py", "tests/test_assisted_resolution.py",
        "tests/test_assisted_resolution_risk_flags.py", "tests/test_auto_resolve.py",
        "tests/test_human_notifications.py", "tests/test_ai_resolution_consolidation.py",
    ),
}

# Exact runtime ownership. New files require review; name similarity is not proof.
AREA_FILES = {
    "menu_admin": ("admin_menu_review.py", "admin_weekly_savoury.py", "menu_catalogue.py"),
    "menu_authority": (
        "menu_review.py", "weekly_savoury.py", "menu_dates.py", "menu_images.py",
        "weekly_flyer.py", "menu_parser.py", "menu_manager.py",
    ),
    "responder": (
        "responder.py", "rag_fallback.py", "semantic_fallback.py", "client_qa.py",
        "conversation_resolver.py", "grounded_answers.py", "knowledge.py",
        "content_integrity.py", "router.py", "route_contract.py", "trigger_index.py",
        "understanding.py", "text_rules.py", "slang_normalizer.py", "triage.py", "ai_responder.py",
    ),
    "instagram": (
        "instagram_ads.py", "admin_ads.py",
        "instagram_routing.py", "instagram_media.py", "instagram_identity.py",
        "instagram_story_sync.py", "ad_routing.py", "ad_gate.py", "ice_breakers.py",
    ),
    "ordering": ("order_calculator.py", "order_intake.py"),
    "handoff": (
        "handoff.py", "escalator.py", "candidate_safety.py", "assisted_resolution.py", "auto_resolve.py",
    ),
}
FILE_AREAS = {f"camellia/{name}": area for area, names in AREA_FILES.items() for name in names}
CORE_FILES = {
    "camellia/app.py", "camellia/database.py", "camellia/config.py",
    "camellia/admin_security.py", "camellia/session_memory.py", "camellia/bootstrap.py",
    "camellia/instagram_oauth.py", "camellia/__init__.py", "main.py",
    "pytest.ini", "tests/conftest.py",
    "scripts/run_impacted_tests.py", "scripts/test_impact_map.py", "scripts/update_test_baseline.py",
    "scripts/test_execution.py", "scripts/test_parallel_policy.json", "requirements-test.txt",
}
AREA_RISK = {area: "HIGH" for area in AREA_TESTS}
AREA_RISK["menu_admin"] = "MEDIUM"

# Deliberately narrow non-executable allow-list, not a catch-all for unknown data.
LOW_ROOT_FILES = {"LICENSE", ".gitignore", ".gitattributes", ".editorconfig", "test_baseline.json"}

# Exact existing CI contracts plus executor safety tests. This explicit gate is
# separate from frozen-baseline impact/release classification.
CURRENT_CONTRACT_TESTS = (
    'tests/test_impacted_test_runner.py',
    'tests/test_client_approved_only_policy.py',
    'tests/test_phase2f_observability_operations.py',
    'tests/test_phase3_repository_cleanup.py',
    'tests/test_client_approved_qa.py',
    'tests/test_client_qa_acceptance.py',
    'tests/test_approved_qa_manager.py',
    'tests/test_instagram_chatbot_control.py',
    'tests/test_ordered_approved_response.py',
    'tests/test_channel_policy.py',
    'tests/test_admin_security.py',
    'tests/test_session_persistence.py',
    'tests/test_explicit_intent_interrupts_stale_flow.py',
    'tests/test_order_intake.py',
    'tests/test_instagram_identity.py',
    'tests/test_instagram_source_referential.py',
    'tests/test_instagram_story_routing.py',
    'tests/test_shared_post_pairing.py',
    'tests/test_shared_reel_precedence.py',
    'tests/test_simple_source_mapping.py',
    'tests/test_ad_mapped_source_context.py',
    'tests/test_instagram_ads_inventory.py',
    'tests/test_test_execution.py',
    'tests/test_admin_portal.py::test_webhook_signature_status_is_reported_honestly',
    'tests/test_admin_portal.py::test_secure_webhook_signature_default_when_environment_value_is_absent',
    'tests/test_admin_portal.py::test_signed_instagram_webhook_passes_signature_gate',
    'tests/test_admin_portal.py::test_missing_or_invalid_instagram_signature_is_rejected',
    'tests/test_admin_portal.py::test_instagram_get_challenge_does_not_require_post_signature',
    'tests/test_admin_portal.py::test_instagram_get_challenge_rejects_untrusted_verify_tokens',
    'tests/test_admin_portal.py::test_meta_verify_token_defaults_to_unconfigured_when_environment_is_absent',
    'tests/test_admin_portal.py::test_require_webhook_signature_rejects_unsigned_when_unconfigured',
)
