# Test Suite Audit — Phase 0

## Methodology

Baseline captured on branch `codex/staging-fast-dev-20260922`, at HEAD
`f5c40fc3c359e9989d262e29da9bd83a780d16a5`. The starting working tree already
contained Phases 1–5 and older user changes; this audit preserves them. Inventory
used the top-level `tests/test_*.py` list, pytest collection, test names and
assertions, fixture/runtime behavior, repository architecture notes, and focused
runs of the previously failing modules. Starting inventory: 107 files, 2,122
collected tests. Durations below are expected bands until the full run supplies
measurements: **L** = local/unit, **M** = stateful/API/mock integration, **H** =
large scenario or expensive provider/menu path. No test was removed before its
unique coverage was reviewed.

Classifications are KEEP, UPDATE, MERGE, EXTRACT THEN REMOVE, REMOVE, or
FINAL-ONLY. UPDATE records below identify stale assertions or contract checks to
reconcile; an initially failing test is not presumed to be stale until its
current contract is checked.

## File-by-file inventory

| File | Classification | Current purpose | Unique coverage | Duplicate/superseded by | Action | Reason |
|---|---|---|---|---|---|---|
| `test_ad_routing.py` | KEEP | Ad ID/title/admin route precedence | Exact configured ID and saved route win | `test_instagram_source_routing.py` (broader source mapping) | Retain | Small direct precedence contract |
| `test_admin_maintenance.py` | KEEP | Maintenance-mode boundary | Health and Meta GET remain available | `test_admin_portal.py` | Retain | Distinct maintenance contract |
| `test_admin_portal.py` | KEEP | Admin APIs, health, menu/flyer and webhook setup | Broad live API/UI and Phase 4–5 security | `test_admin_security.py` (auth-focused) | Retain; fast gate uses focused nodes | Broad module remains valuable, not suitable as the whole fast gate |
| `test_admin_second_pass.py` | KEEP | Handoff identity, source and references | Stable CAM identity and customer/admin identity boundaries | `test_waiting_for_human.py` | Retain | Distinct cross-surface handoff assertions |
| `test_admin_security.py` | KEEP | Browser auth/session/CSRF/rate limits | Security headers and protected writes | `test_admin_portal.py` | Retain | Focused security suite |
| `test_admin_ui_structure.py` | KEEP | Admin UI accessibility/structure contracts | Large UI surface shape | `test_admin_portal.py` (behavioral API tests) | Retain | Static UI contracts do not duplicate API behavior |
| `test_ai_resolution_consolidation.py` | KEEP | Candidate/status and navigation consolidation | Non-destructive status filtering and unified approved answers | `test_candidate_approval_editing.py` | Retain | Protects additive resolution workflow |
| `test_anonymized_export.py` | KEEP | Export privacy | Identifiers and transcripts are anonymized | `test_privacy_logging.py` | Retain | Separate export path |
| `test_asset_request_routing.py` | KEEP | Asset requests and order routing | Correct approved asset selection | `test_menu_card_sequence.py` | Retain | Asset selection differs from delivery order |
| `test_assisted_evaluation.py` | KEEP | Evaluation workflow | Evaluation remains explicit and bounded | `test_assisted_resolution.py` | Retain | Separate evaluator contract |
| `test_assisted_resolution_risk_flags.py` | KEEP | Resolution safety flags | Risk is surfaced instead of silently auto-resolved | `test_assisted_resolution.py` | Retain | Focused risk boundary |
| `test_assisted_resolution.py` | KEEP | Human-assisted resolution | Human decision and resolution state | `test_auto_resolve.py` | Retain | Covers assisted path, not automation |
| `test_auto_resolve.py` | KEEP | Auto-resolve guard | Only eligible records auto-resolve | `test_assisted_resolution.py` | Retain | Separate automation policy |
| `test_bot.py` | KEEP | Legacy end-to-end bot scenarios | Broad unique integration, webhook and conversation coverage | Many focused current modules | Retain pending complete extraction | 152 collected cases; unsafe to remove without a full unique-coverage map |
| `test_candidate_approval_editing.py` | KEEP | Candidate review/edit workflow | Approval and editing affect verified knowledge safely | `test_custom_qa.py` | Retain | Candidate lifecycle is unique |
| `test_canonical_knowledge_layer.py` | KEEP | Meaning-to-approved-knowledge mapping | Canonical source and entity coverage | `test_grounded_answer_lanes.py` | Retain | Core architecture contract |
| `test_channel_policy.py` | UPDATE | Channel separation | Instagram AI vs WhatsApp manual-only, safe opaque handoff | `test_whatsapp_cleanup.py` | Update retired callback/link copy; retain channel policy and draft boundaries | Keeps current channel invariant |
| `test_client_approved_only_policy.py` | KEEP | Approved-only customer answer boundary | Unsupported facts hand off; no RAG/Groq leakage | `test_client_approved_qa.py` | Retain | Phase 1 gate |
| `test_client_approved_qa.py` | KEEP | Client QA registry behavior | Exact/paraphrase IDs, assets and precedence | `test_client_qa_acceptance.py` | Retain | Registry parser/runtime boundary |
| `test_client_confirmed_answers.py` | KEEP | Confirmed answer dispatch | Confirmed customer copy is exact | `test_client_qa_acceptance.py` | Retain | Distinct dispatch behavior |
| `test_client_followups.py` | UPDATE | Instagram follow-up and handoff | Repeated handoff, identity and intent boundaries | `test_handoff_order_state.py` | Update stale copy/link expectations; keep handoff assertions | Initial run exposed 5 stale/contract-mismatched assertions |
| `test_client_qa_acceptance.py` | UPDATE | Client-approved QA acceptance | Registry behavior, unsupported event handoff and source precedence | `test_client_approved_qa.py` | Update unsupported event case to current handoff contract; preserve intent trace | High-value acceptance gate |
| `test_confirmed_substitution.py` | KEEP | Approved substitution behavior | Exact allowed substitutions only | `test_client_confirmed_answers.py` | Retain | Distinct product substitution constraint |
| `test_content_integrity_quarantine.py` | KEEP | Unsafe-copy quarantine | Unsupported claims never reach output | `test_content_integrity_remediation.py` | Retain | Quarantine vs remediation are distinct |
| `test_content_integrity_remediation.py` | KEEP | Safe seed remediation | Versioned overrides and client-copy preservation | `test_content_integrity_quarantine.py` | Retain | Migration/remediation behavior |
| `test_conversation_improvements.py` | KEEP | Conversation-state improvements | State-aware interpretation | `test_routing_precedence.py` | Retain | Additional transition coverage |
| `test_conversational_routing_audit.py` | KEEP | Routing audit/traces | Route explanations and audit state | `test_routing_golden.py` | Retain | Diagnostic contract |
| `test_custom_qa.py` | KEEP | Learned knowledge and response library | CRUD, aliases, lifecycle, search/versioning | `test_candidate_approval_editing.py` | Retain | Large data-model suite with unique persistence |
| `test_db_backup_endpoint.py` | KEEP | Database backup API | Auth, missing DB and file-copy behavior | `test_admin_security.py` | Retain | Backup-specific operational contract |
| `test_delivery_time_and_kolkata_disambiguation.py` | KEEP | Delivery-time and region disambiguation | No invented travel or dispatch promises | `test_opening_hours_canonical.py` | Retain | Separate business intents |
| `test_entry_point_logging.py` | KEEP | Entry-point metadata logging | Exact source/media audit data | `test_instagram_identity.py` | Retain | Logging differs from route identity |
| `test_explicit_intent_interrupts_stale_flow.py` | UPDATE | Stale state interruption | Current intent replaces stale contextual flow and clears stale clarification | `test_routing_precedence.py` | Update coverage for reachable early interruption and stale state clearing | Phase 3 invariant |
| `test_flyer_downscaling.py` | KEEP | Flyer image sizing | Image constraints and safe downscaling | `test_menu_image_manager.py` | Retain | Image processing contract |
| `test_graph_media_lookup.py` | KEEP | Graph media API lookup | Lookup failures do not trigger guesses | `test_verify_media_by_id.py` | Retain | Lookup vs identity verification |
| `test_groq_model_stabilization.py` | KEEP | Provider model selection | Stable configured model/provider behavior | `test_provider_outage_routing.py` | Retain | Provider config is distinct |
| `test_grounded_answer_lanes.py` | KEEP | Approved/derived/source-description lanes | Evidence lane is explicit | `test_canonical_knowledge_layer.py` | Retain | Core answer provenance |
| `test_handoff_order_state.py` | KEEP | Handoff order state | Active draft and handoff do not lose order state | `test_order_intake.py` | Retain | Handoff boundary is distinct |
| `test_health_seed_drift.py` | KEEP | Health and seed drift | Health reports material data drift | `test_monitoring.py` | Retain | Seed drift is operationally unique |
| `test_home_table_retirement.py` | KEEP | Retired home-table knowledge | Archive/history preserved | `test_phase3b_live_runtime_remediation.py` | Retain | Retirement/tombstone migration |
| `test_human_notifications.py` | KEEP | Staff notification policy | Safe/manual notification behavior | `test_whatsapp_cleanup.py` | Retain | Notification service boundary |
| `test_hybrid_rag_architecture.py` | KEEP | Retrieval architecture | Retrieval cannot bypass approved answer policy | `test_client_approved_only_policy.py` | Retain | Separate retrieval boundary |
| `test_hybrid_understanding.py` | KEEP | Understanding pipeline | Intent/entity integration and fallback | `test_structured_understanding.py` | Retain | End-to-end vs component contract |
| `test_ice_breaker_write.py` | KEEP | Ice-breaker data write path | Admin write durability and validation | `test_custom_qa.py` | Retain | Separate record type |
| `test_instagram_account_guard.py` | UPDATE | Entry account filtering and opening-hours behavior | Foreign-account silence and exact account matching | `test_opening_hours_canonical.py` | Remove duplicate hours parameter set only if integration coverage is transferred; fix handoff mock | Initial run: 9 hours variants plus one mock/type failure |
| `test_instagram_api_versions.py` | KEEP | Graph API version construction | Configured version reaches API paths | `test_graph_media_lookup.py` | Retain | Version contract is distinct |
| `test_instagram_attachment_diagnostics.py` | KEEP | Attachment diagnostics | Media-only and unreadable attachment safety | `test_asset_request_routing.py` | Retain | Diagnostic events differ from route identity |
| `test_instagram_durable_jobs.py` | KEEP | Durable webhook job queue | Idempotence, retry, restart behavior | `test_instagram_outbound_audit.py` | Retain | Persistence and retry contract |
| `test_instagram_identity.py` | KEEP | Instagram identity lookup | Username/ID verification and privacy | `test_verify_media_by_id.py` | Retain | Customer identity is distinct from media identity |
| `test_instagram_oauth.py` | KEEP | Instagram OAuth | State, callback and account binding | `test_instagram_profile_check.py` | Retain | Authentication flow is unique |
| `test_instagram_outbound_audit.py` | KEEP | Outbound audit/delivery classification | Retryable vs permanent failures | `test_instagram_durable_jobs.py` | Retain | Durable queue vs audit outcome |
| `test_instagram_profile_check.py` | KEEP | Profile/account validation | Account ownership/profile checks | `test_instagram_oauth.py` | Retain | Post-auth validation |
| `test_instagram_routing_remove_archive.py` | KEEP | Route removal/archive behavior | Route retirement and no accidental resurrection | `test_menu_archive.py` | Retain | Separate Instagram route records |
| `test_instagram_source_referential.py` | UPDATE | Genuine source-reference questions | Exact mapped Description, explicit intent, and no guessed identity | `test_simple_source_mapping.py` | Replace metadata-authorized legacy answers with approved/handoff contract | Source reference contract |
| `test_instagram_source_routing.py` | KEEP | Story/Post/Reel exact route behavior | Mapped/unmapped route and explicit intent | `test_shared_post_pairing.py` | Retain | Broad source routing integration |
| `test_instagram_story_lifecycle.py` | KEEP | Story lifecycle | Expiry and state transitions | `test_instagram_story_sync.py` | Retain | Lifecycle differs from synchronization |
| `test_instagram_story_routing.py` | UPDATE | Story route behavior | Exact Story identity, source description, and media-only silence | `test_instagram_source_routing.py` | Update stale template expectations; retain exact-ID behavior | Story-specific paths |
| `test_instagram_story_runtime.py` | KEEP | Story runtime behavior | Runtime context/media-only rules | `test_instagram_story_routing.py` | Retain | Runtime integration |
| `test_instagram_story_sequence.py` | KEEP | Story sequence state | Multi-item sequence tracking | `test_instagram_story_runtime.py` | Retain | Sequence-specific state |
| `test_instagram_story_sync.py` | KEEP | Story synchronization | Sync persistence, removals and resync | `test_instagram_story_lifecycle.py` | Retain | External sync boundary |
| `test_llm_triage_shadow.py` | KEEP | Read-only LLM triage | Shadow output never mutates candidates/escalations | `test_ai_resolution_consolidation.py` | Retain | Safety of shadow evaluation |
| `test_menu_archive.py` | KEEP | Menu archive/restore | Reversible archive and history | `test_menu_permanent_delete.py` | Retain | Archive differs from permanent deletion |
| `test_menu_card_sequence.py` | KEEP | Menu media ordering | Stable order of text/assets | `test_asset_request_routing.py` | Retain | Output sequencing is distinct |
| `test_menu_category_management.py` | KEEP | Menu category CRUD | Category constraints and persistence | `test_menu_manager.py` | Retain | Category-specific API |
| `test_menu_image_manager.py` | KEEP | Menu image management | Image write/replace paths | `test_flyer_downscaling.py` | Retain | Persistent images vs preprocessing |
| `test_menu_manager.py` | KEEP | Menu CRUD | Menu data operations | `test_menu_category_management.py` | Retain | Broad CRUD coverage |
| `test_menu_permanent_delete.py` | KEEP | Permanent delete safeguards | Explicit permanent deletion semantics | `test_menu_archive.py` | Retain | Destructive path distinct from archive |
| `test_menu_review_before_publish.py` | KEEP | Review-before-publish | Human confirmation before publication | `test_upload_decoupling.py` | Retain | Publication safety |
| `test_monitoring.py` | KEEP | Monitoring contract | Minimal health/monitoring signals | `test_health_seed_drift.py` | Retain | Separate monitor behavior |
| `test_no_flower_emoji.py` | KEEP | Customer-visible copy sanitation | Shipped copy, replies, candidates and prompts | Deleted `test_no_flowers_anywhere.py` | Retain merged coverage | Current file covers the deleted file's unique candidate/prompt/transform cases |
| `test_no_shadowed_tests.py` | KEEP | Test discovery integrity | No accidentally shadowed test modules | pytest collection | Retain | Protects suite integrity |
| `test_opening_hours_canonical.py` | KEEP | Canonical hours and quarantine | Approved source, seed recovery and grounded resolution | `test_instagram_account_guard.py` | Retain; absorb any removed integration check | Canonical source is the stronger hours contract |
| `test_order_intake.py` | KEEP | Order workflow | Required fields, quantity and confirmation | `test_handoff_order_state.py` | Retain | Core order state |
| `test_ordered_approved_response.py` | KEEP | Ordered response blocks | Client-approved text/assets preserve authored order | `test_menu_card_sequence.py` | Retain | Phase 1 output contract |
| `test_phase3a_golden_responses.py` | UPDATE | Golden answers and precedence | Explicit intent/state precedence and handoff routing | `test_routing_golden.py`, `test_client_approved_only_policy.py` | Update old template expectations; preserve explicit-intent safety | WhatsApp and legacy response assumptions are retired |
| `test_phase3a_subscription_durable_template.py` | KEEP | Durable subscription migration | Idempotence, operator override and history | `test_phase3a_subscription_live_db_override.py` | Retain | Explicitly protected migration coverage |
| `test_phase3a_subscription_live_db_override.py` | KEEP | Live DB operator override | Durable operator copy is not overwritten | `test_phase3a_subscription_durable_template.py` | Retain | Real DB override path is unique |
| `test_phase3b_client_content.py` | KEEP | Client-confirmed content | Exact copy, domain separation and no unsupported facts | `test_client_followups.py` | Retain | Current approved content contract |
| `test_phase3b_live_output_contract.py` | KEEP | Live output and provider independence | Approved output survives runtime and provider outage | `test_phase3b_live_runtime_remediation.py` | Retain | Delivery contract vs remediation |
| `test_phase3b_live_runtime_remediation.py` | KEEP | Runtime quarantine and migration | Unsafe legacy overrides are quarantined; edits preserved | `test_content_integrity_remediation.py` | Retain | Phase-specific durable remediation |
| `test_phase3b_persistent_session.py` | UPDATE | Persistent conversation state | Restart/session subject boundaries and handoff pause/resume | `test_session_persistence.py` | Update stale staff-contact and multipart-send assertions | Session behavior differs from storage smoke |
| `test_primary_nlu_architecture.py` | KEEP | Primary NLU architecture | Provider-independent deterministic decision boundaries | `test_structured_understanding.py` | Retain | Architecture-level tests |
| `test_privacy_logging.py` | KEEP | Privacy-safe logging | No raw customer/credential leakage | `test_anonymized_export.py` | Retain | Runtime logs vs export data |
| `test_provider_outage_routing.py` | KEEP | Provider outage behavior | Safe deterministic fallback/handoff | `test_groq_model_stabilization.py` | Retain | Routing consequences of failure |
| `test_router_shadow.py` | KEEP | Shadow router | Candidate routing is non-authoritative | `test_llm_triage_shadow.py` | Retain | Different shadow components |
| `test_routing_golden.py` | KEEP | Routing golden contracts | Current intent and approved-answer routing | `test_phase3a_golden_responses.py` | Retain | Compact routing-level golden cases |
| `test_routing_precedence.py` | KEEP | Conversation/source precedence | Active order and current intent win | `test_explicit_intent_interrupts_stale_flow.py` | Retain | Core Phase 3 contract |
| `test_seed_reconciliation.py` | KEEP | Seed reconciliation | Idempotent seed update and operator data preservation | `test_health_seed_drift.py` | Retain | Reconciliation differs from monitoring |
| `test_semantic_router_fallback.py` | KEEP | Semantic-router fallback | Safe refusal and compatibility lane | `test_hybrid_understanding.py` | Retain | Focused fallback policy |
| `test_session_persistence.py` | KEEP | Persistent session smoke | State survives new connection/instance | `test_phase3b_persistent_session.py` | Retain | Low-cost durability smoke |
| `test_shared_post_pairing.py` | UPDATE | Shared Post identity | Exact Meta ID matching and pairing | `test_shared_reel_precedence.py` | Update legacy source-copy assumptions; preserve exact-ID mapping | Post-specific mapping |
| `test_shared_reel_precedence.py` | UPDATE | Shared Reel precedence | Exact Reel IDs beat fallback metadata | `test_shared_reel_routing.py` | Update legacy template expectations; preserve exact-ID precedence | Precedence is distinct |
| `test_shared_reel_routing.py` | UPDATE | Shared Reel route resolution | Mapped/unmapped Reel behavior | `test_shared_reel_precedence.py` | Update against approved-answer/source-description rules | End-to-end route result |
| `test_simple_mapping_ui.py` | KEEP | Source mapping UI | Admin controls for mappings | `test_simple_source_mapping.py` | Retain | UI vs persistence/runtime |
| `test_simple_source_mapping.py` | UPDATE | Durable exact source mapping | Save/reopen mapped and unmapped states, exact Description and approved switch | `test_instagram_source_routing.py` | Update stale template/priority assumptions; retain identity and persistence checks | Persistence is unique |
| `test_sondesh_addon.py` | KEEP | Sondesh add-on flow | One-time add-on and price handling | `test_no_flower_emoji.py` | Retain | Add-on business flow |
| `test_sondesh_tell_me_more_routing.py` | KEEP | Sondesh details routing | Explicit details and stale context behavior | `test_client_approved_qa.py` | Retain | Product-specific precedence |
| `test_stress_25_agents.py` | FINAL-ONLY | Multi-agent/customer stress scenarios | 30 broad scenario cases | `test_bot.py` (some scenarios) | Keep out of push fast gate; run in full gate | Highest expected runtime and broad scenario overlap |
| `test_structured_understanding.py` | KEEP | Structured NLU contract | Intent/entity validation and failure semantics | `test_hybrid_understanding.py` | Retain | Component-level validation |
| `test_subscription_followups.py` | KEEP | Subscription conversation state | Distinguishes subscription follow-ups from other topics | `test_phase3a_golden_responses.py` | Retain | Focused state transitions |
| `test_subtopic_sufficiency_and_safety.py` | KEEP | Grounding sufficiency and claim safety | Partial fulfilment without invented claims | `test_grounded_answer_lanes.py` | Retain | Strong unsupported-fact safety coverage |
| `test_topic_menu_catalogue.py` | KEEP | Topic/menu CRUD, mappings and previews | Migration and ID persistence | `test_simple_source_mapping.py` | Retain | Broad catalogue integration |
| `test_upload_decoupling.py` | KEEP | Flyer upload/publish separation | Failed OCR does not suppress customer-safe image flow | `test_vision_ocr_resilience.py` | Retain | Upload policy differs from parser resilience |
| `test_verify_media_by_id.py` | KEEP | Exact media-ID verification | Invalid IDs/errors never guess nearest content | `test_graph_media_lookup.py` | Retain | Security and exact identity |
| `test_vision_ocr_resilience.py` | KEEP | Admin menu-flyer OCR/parser robustness | Retries, malformed JSON and quota handling | `test_upload_decoupling.py` | Retain | Menu OCR is not Instagram source identity |
| `test_waiting_for_human.py` | KEEP | Human waiting/resume state | No AI reply while waiting; explicit resume is idempotent | `test_handoff_order_state.py` | Retain | Human wait lifecycle |
| `test_weekly_dishes_orderable.py` | KEEP | Weekly menu/order availability | Current menu and price precedence | `test_menu_manager.py` | Retain | Customer orderability contract |
| `test_whatsapp_cleanup.py` | UPDATE | WhatsApp human-only behavior | Route unavailable, tester manual-only, no responder call | `test_channel_policy.py`; deleted `test_escalation_destination.py` | Update stale handoff wording; preserve channel gate | Initial run had one old-copy failure; other safety coverage is current |

### Pre-existing deletions reviewed

| File at HEAD | Classification | Unique coverage | Replacement / decision |
|---|---|---|---|
| `tests/test_escalation_destination.py` | MERGE (deletion already present at baseline) | Manual-only alert result and health shape duplicate current checks; its customer-copy assertion expects retired wording | Keep deletion; equivalent alert/health assertions are in `test_whatsapp_cleanup.py`, whose copy assertion is updated to current wording |
| `tests/test_no_flowers_anywhere.py` | MERGE (deletion already present at baseline) | Emoji transforms, candidate persistence/read sanitation, prompt prohibition | Keep deletion; those categories are present in `test_no_flower_emoji.py` |

No active test module is removed at this inventory stage. The 152-test `test_bot.py`
is retained because a safe, complete extraction of its unique integration
coverage is not established; fast CI will use focused contract tests instead of
deleting that historical evidence.

## Grouped first audit results

Command:

```bash
python -m pytest -q tests/test_client_followups.py tests/test_whatsapp_cleanup.py tests/test_instagram_account_guard.py tests/test_phase3a_golden_responses.py tests/test_phase3b_client_content.py
```

Result: 104 passed, 21 failed, 1 warning, 51.20s. Failures were reviewed rather
than suppressed: five in follow-up/handoff expectations, one retired WhatsApp
copy expectation, ten in the account-guard/hours module (nine duplicated hours
answer cases and one mock shape issue), and five Instagram golden-routing cases.
The exact decisions and post-reconciliation results will be added after fixes
and the full regression run.

## Initial CI and test strategy

The existing workflow runs all pytest tests on every push/PR (plus compile-time
flake8 and schema validation). Replace it with a fast current-contract push/PR
gate plus a full regression job on manual dispatch and a nightly schedule. Keep
`python -m pytest -q` available and do not skip tests in the full gate. The fast
gate will explicitly include approved QA, order precedence, exact source
identity smoke, WhatsApp human-only, webhook security, admin security, and
persistence/migration smoke, together with compile/schema/static checks.

## Reconciliation results

The five previously known stale-failure groups were reconciled and focused
checks now pass: `test_client_followups.py`, `test_whatsapp_cleanup.py`,
`test_instagram_account_guard.py`, `test_phase3a_golden_responses.py`, and
`test_phase3b_client_content.py` jointly passed 125 tests after updates. The
first run had 104 passed and 21 failed. Current follow-up wording no longer
pushes customers to WhatsApp; account-guard tests assert canonical hours and
manual escalation behavior. Story/Post/Reel modules retain exact Meta-ID,
description, explicit-intent and media-only-silence checks while dropping
legacy-template expectations. The archived-Admin expectation was not present
in the active inventory. No test module was removed during this Phase 0.

## Phase 0 validation and completion record

### Inventory and disposition

The starting Phase 0 working-tree inventory was 107 active top-level
`test_*.py` files and 2,122 collected cases. Final inventory remains 107 files
and 2,123 collected cases. Current-contract test modules were updated; 92 are
KEEP; one stress module is FINAL-ONLY. Two files already deleted before Phase 0
are recorded as MERGE dispositions below, so they are not counted as Phase 0
removals or as active starting files. No active test file was removed in this
phase, and no test was blanket-skipped or xfailed.

| Disposition | Count | Notes |
|---|---:|---|
| KEEP | 92 | Retained active modules |
| UPDATE | See working-tree diff | Current contracts/assertions reconciled |
| MERGE | 2 | Pre-existing deleted modules; coverage represented elsewhere |
| EXTRACT THEN REMOVE | 0 | No active legacy module was safely fully extracted |
| REMOVE | 0 | No active test module removed |
| FINAL-ONLY | 1 | `test_stress_25_agents.py`, still included in full regression |

Pre-existing merge mappings: `test_no_flowers_anywhere.py` →
`test_no_flower_emoji.py` (customer-visible and candidate/prompt sanitation);
`test_escalation_destination.py` → `test_whatsapp_cleanup.py` and
`test_channel_policy.py` (manual-only escalation/health behavior). Both source
deletions were already present at Phase 0 start. `test_bot.py` remains intact;
unique webhook and broad integration coverage was not fully extracted.

Updated test modules: `test_client_followups.py`, `test_whatsapp_cleanup.py`,
`test_instagram_account_guard.py`, `test_phase3a_golden_responses.py`,
`test_explicit_intent_interrupts_stale_flow.py`,
`test_instagram_source_referential.py`, `test_instagram_story_routing.py`,
`test_shared_post_pairing.py`, `test_shared_reel_precedence.py`,
`test_shared_reel_routing.py`, `test_simple_source_mapping.py`,
`test_channel_policy.py`, `test_phase3b_persistent_session.py`, and
`test_client_qa_acceptance.py`. Changes correct retired WhatsApp/link/copy
expectations and legacy template assumptions while retaining exact source
identity, safe handoff, state precedence, and persistence contracts.

### Grouped and security checks

The final exact CI fast current-contract pytest command passed **280 tests** in
**42.45s**, with one Starlette/httpx deprecation warning. It covers approved QA,
unsupported handoff, ordering/state, admin security, session smoke, explicit
intent and Story/Post/Reel routing. The separate Meta webhook security command
(`python -m pytest -q tests/test_admin_portal.py -k "signature or token or challenge"`)
passed **11 tests**, 51 deselected, in **2.63s**. Combined fast-gate coverage is
291 tests, measured local runtime 45.08s. Earlier focused groups also passed:
23 approved-policy tests; 53 source/referential tests; 39 Story/Reel tests; 91
Post/Reel/source mapping tests; 25 channel-policy tests; 4 persistent-session
tests; 7 explicit-interruption tests; and 89 order/context tests. A security/
webhook mini-gate passed 11 tests. Final database/persistence validation passed
108 tests in 25.39s across session persistence, topic/source migration and
reopen, seed reconciliation, durable jobs, candidates, quarantine/remediation,
and admin sessions.

The 11 focused security cases verify signed Instagram POST acceptance, missing
and invalid signature rejection, fail-closed missing secret, secure signature
default, and GET challenge configured/wrong/missing/empty/whitespace token
behavior. Admin auth/CSRF is in `test_admin_security.py`; privacy controls
remain in `test_privacy_logging.py` and related privacy modules. Durable job,
session, migration, source mapping, retirement/quarantine and operator-override
tests remain in the full suite. Stress remains available only on full/manual
and scheduled runs.

Critical coverage checklist (focused suites pass unless noted):

| Category | Result | Evidence |
|---|---|---|
| Approved QA / approved-only | PASS | Core gate and approved QA suites |
| Unsupported-fact handoff | PASS | Policy and client QA acceptance gate |
| Exact Meta source identity | PASS | Story/Post/Reel source-routing tests |
| Source Description | PASS | Referential and durable source-mapping tests |
| Explicit current-intent precedence | PASS | Stale-flow interruption and routing tests |
| Order/context state | PASS | Order intake and contextual state suites |
| WhatsApp HUMAN ONLY | PASS | Channel policy and cleanup suites |
| Instagram POST signature | PASS | Focused signature suite included in CI fast gate |
| GET verify-token security | PASS | Focused token/challenge suite included in CI fast gate |
| Admin/auth/privacy | PASS | Admin security fast gate; privacy module passed full run |
| Persistence/migrations | PASS | Final focused migration/persistence group: 108 passed |
| Operator overrides | PASS | Durable subscription override tests are retained |

### Full regression results

Post-remediation full run: **2,123 passed; 0 failed; 0 skipped; 0 xfailed;
1 warning; 431.08s**. An intermediate run found 13 failures; affected modules
were corrected and rerun before the final clean full suite. The original 184
node IDs and file counts were recovered from the preserved log. That log used
`--tb=no`, so the surviving evidence does not support a complete per-node A–F
classification without guessing. See [PHASE0_FAILURE_TRIAGE.md](PHASE0_FAILURE_TRIAGE.md)
for the known groups and the explicit audit gap. No active test module was
removed in Phase 0B.

### CI strategy and size comparison

Old CI ran the entire pytest suite on every push/PR. New CI runs the focused
current-contract gate on push/PR, including admin auth, Meta POST signature and
GET verify-token security, schema validation and Flake8 syntax/undefined-name
checks. `test_stress_25_agents.py` is not in the fast gate. Full regression runs
on manual dispatch and nightly schedule; it keeps `python -m pytest -q`
available and does not skip tests. Local measured runtime is 45.08s for 291
fast-gate tests versus 431.08s for 2,123 full-suite tests (about 90% less
elapsed time; local comparison only, not a CI guarantee). The full GitHub job
has a 30-minute timeout; fast CI has a 12-minute timeout.

| Measure | Before Phase 0 | After Phase 0 |
|---|---:|---:|
| Active test files | 107 | 107 |
| Collected tests | 2,122 | 2,123 |
| Active files removed | — | 0 |
| Files merged/extracted in this phase | — | 0 (2 inherited MERGE decisions) |
| Files updated | — | See working-tree diff |
| Retained KEEP files | — | 92 |
| FINAL-ONLY files | — | 1 |

### Files changed for Phase 0

| Area | Files |
|---|---|
| Test cleanup | No active module deleted. Two inherited deletions are documented above. |
| Test updates | The 14 modules listed in “Updated test modules” above. |
| CI | `.github/workflows/ci.yml` — two-tier workflow plus focused POST/GET webhook security in the fast gate. |
| Documentation | `docs/TEST_SUITE_AUDIT.md` — inventory, decisions, measurements and this completion record. |
| Production fixes | `camellia/responder.py` — approved-hours precedence and fail-closed event/bulk metadata; `camellia/content_integrity.py` — suppress unconfirmed current-menu descriptions. |

### Static checks, production edit, and readiness

Passed after the final production edit: `python -m compileall -q camellia`,
`python scripts/validate_schemas.py` (menu and QA schemas), Flake8 CI selector
(`--select=E9,F63,F7,F82`, 0 findings), `git diff --check` (exit 0; only
working-copy LF/CRLF warnings), and YAML parsing of `.github/workflows/ci.yml`.

One small production fix in `camellia/responder.py` preserves deterministic
`event_order`/`bulk_order` entities in the human-handoff audit when structured
NLU is disabled/unavailable. It does not change customer copy or handoff policy.
No production settings, Railway, Meta webhook, branch promotion, customer
message, or tag was changed.

Release status: **NOT READY — current full suite and gates pass, but the
required evidence-backed A–F disposition of all original failures is not
complete.** No deployment or staging promotion was performed.

Phase 1–5 regression checkpoints:

| Phase | Result | Basis |
|---|---|---|
| Phase 1 — approved-answer boundary | PASS focused | Approved-only/current policy gate passes |
| Phase 2 — source priority is context only | PASS focused | Mapped priority cannot authorize legacy copy in the gate |
| Phase 3 — active state/current intent precedence | PASS focused | Order, acknowledgement and stale-flow gates pass |
| Phase 4 — POST signature fail-closed | PASS focused | Signed/missing/bad/missing-secret tests pass |
| Phase 5 — GET verify token fail-closed | PASS focused | Configured/wrong/missing/empty/whitespace cases pass |

Starting state: branch `codex/staging-fast-dev-20260922`, HEAD
`f5c40fc3c359e9989d262e29da9bd83a780d16a5`. The checkout was already dirty
before Phase 0: the baseline snapshot had 19 modified tracked files, two
pre-existing test deletions, and untracked `doc_work/` plus
`tests/test_instagram_durable_jobs.py`. Those Phase 1–5/user edits were
preserved. Current working-tree edits are listed by `git status --short`,
`git diff --name-only`, and `git diff --stat`; the active inventory and baseline
state are recorded above.

### Phase 0B final continuation — 2026-09-24

The final complete suite passed **2,123/2,123** in **431.08s**. The final exact
push/PR fast gate passed **280/280** in **42.45s**; webhook security passed
11/11; the persistence/migration gate passed 108 tests. One non-blocking
Starlette/httpx deprecation warning remains. Schema validation, the CI Flake8
selector, `compileall`, `git diff --check`, and workflow YAML parsing passed.

The first post-remediation full run reported 13 failures. Their focused modules
were updated or reconciled with current behavior and passed before the final
full run. Changes included canonical approved opening-hours handling; source
Description/approved-topic test reconciliation; topic-description fixture
correction; and removal of unsupported claims from menu descriptions while
retaining the sourced dish and price. WhatsApp remains HUMAN ONLY/manual-only.

The original pytest artifact retained failed node IDs but used `--tb=no`, so
the individual historical assertion messages are unavailable. The current
`.pytest_cache/v/cache/lastfailed` contains 127 stale keys (including renamed
tests and a removed scratch test), and is not a reliable clean-snapshot tool.
The original per-file inventory and known causes are recorded in
[docs/PHASE0_FAILURE_TRIAGE.md](PHASE0_FAILURE_TRIAGE.md), which explicitly
marks exhaustive A–F classification as uncertified rather than inventing
counts. No deployment, push, production promotion, live Meta message, or
production/Railway database action occurred.

**PHASE 0 STATUS: BLOCKED** solely on the requested evidence-backed
classification of every original failure. **NOT READY FOR STAGING VALIDATION**
until that historical disposition is completed. The current tests themselves
are fully green.
