# Phase 0B — Full-Suite Failure Triage

## Final verification snapshot — 2026-09-24

- Branch: `codex/staging-fast-dev-20260922`
- HEAD: `f5c40fc3c359e9989d262e29da9bd83a780d16a5`
- Initial baseline: 2,122 collected; 1,938 passed; 184 failed; 0 skipped; 0 xfailed; 1 warning; 548.04s.
- Final repository run: 2,123 passed; 0 failed; 0 skipped; 0 xfailed; 1 warning; 431.08s.
- Final CI fast pytest gate: 280 passed; 0 failed; 1 warning; 42.45s.
- Webhook signature/token gate: 11 passed; 51 deselected; 1 warning; 2.63s.
- Persistence/migration group: 108 passed; 1 warning; 25.39s.
- No deploy, push, production promotion, live Meta request, or production/Railway database access occurred.

The extra collected test (2,123 vs the original 2,122) is current repository coverage. The full suite passed on the final run.

## Original inventory by file

The preserved full-run log records 185 failed node IDs, including one event-handoff node fixed before the agreed 184-node snapshot. Removing that node produces this exact per-file 184 count:

| File | Original failed nodes |
|---|---:|
| `tests/test_grounded_answer_lanes.py` | 24 |
| `tests/test_simple_source_mapping.py` | 19 |
| `tests/test_subtopic_sufficiency_and_safety.py` | 12 |
| `tests/test_routing_golden.py` | 11 |
| `tests/test_topic_menu_catalogue.py` | 10 |
| `tests/test_bot.py` | 10 |
| `tests/test_phase3b_live_output_contract.py` | 10 |
| `tests/test_provider_outage_routing.py` | 9 |
| `tests/test_primary_nlu_architecture.py` | 9 |
| `tests/test_sondesh_addon.py` | 7 |
| `tests/test_admin_portal.py` | 7 |
| `tests/test_stress_25_agents.py` | 6 |
| `tests/test_ordered_approved_response.py` | 6 |
| `tests/test_hybrid_rag_architecture.py` | 5 |
| `tests/test_client_approved_only_policy.py` | 5 |
| `tests/test_client_followups.py` | 5 |
| `tests/test_custom_qa.py` | 5 |
| `tests/test_opening_hours_canonical.py` | 4 |
| `tests/test_phase3b_live_runtime_remediation.py` | 3 |
| `tests/test_menu_manager.py` | 3 |
| `tests/test_confirmed_substitution.py` | 3 |
| `tests/test_canonical_knowledge_layer.py` | 2 |
| `tests/test_hybrid_understanding.py` | 2 |
| `tests/test_phase3b_client_content.py` | 1 |
| `tests/test_conversation_improvements.py` | 1 |
| `tests/test_llm_triage_shadow.py` | 1 |
| `tests/test_semantic_router_fallback.py` | 1 |
| `tests/test_asset_request_routing.py` | 1 |
| `tests/test_structured_understanding.py` | 1 |
| `tests/test_conversational_routing_audit.py` | 1 |
| **Total** | **184** |

## Classification limitation

The preserved original pytest output used `--tb=no`: it has the exact node IDs and file counts, but no assertion output or tracebacks. The current `.pytest_cache/v/cache/lastfailed` is not a usable substitute: it contains 127 keys, including obsolete/renamed tests and a removed scratch test. The original 157 unchanged node IDs were rerun and passed; renamed/reparameterized cases were run in their current modules. The final full run is green.

However, the existing evidence does not support assigning an accurate A–F classification and failure summary to each of the original 184 assertions without guessing. A green current suite proves the present test set passes; it does not prove whether each historical failure was A, B, C, D, or E. No test was reproduced intermittently, so none is classified F. Therefore **do not claim an exhaustive classification or fabricate category totals**. This is the remaining Phase 0B audit gap.

## Confirmed root-cause groups and actions

| Root cause | Original failures | Files / evidence | Current contract and action | Status |
|---|---:|---|---|---|
| Retired copy and channel assumptions | Not reliably countable from `--tb=no` report alone | `test_client_followups.py`, `test_whatsapp_cleanup.py`, `test_stress_25_agents.py`, output-contract modules | Updated stale copy/link assertions; Instagram AI, WhatsApp manual-only; no legacy answer restoration | Focused and full suite green |
| Unsupported source-topic/template inference | Not reliably countable from original trace | Source mapping, Story/Reel/Post and approved-QA modules | Exact mapped source provides context; configured Description or approved explicit subject only; unsupported source question hands off; priority products alone do not authorize answers | Focused and full suite green |
| Opening-hours precedence | 4 in `test_opening_hours_canonical.py`; account-guard cases were added/reconciled after baseline | `camellia/responder.py`; opening-hours and account-guard tests | Canonical approved hours precede stale source context; paraphrases return exact approved copy without NLU/Groq | Focused and full suite green |
| Contextual state, order intent, Bento derivation, subscription chart routing | Not reliably countable from original trace | responder and focused intent/source modules | Current explicit intent and active state win; deterministic approved derivation/assets only; unsupported facts hand off | Focused and full suite green |
| Current menu claim quarantine | New issue found on final persistence gate, not part of original 184 | `camellia/content_integrity.py`, `camellia/responder.py`, `tests/test_content_integrity_*` | Omit an unconfirmed menu description while retaining sourced item, quantity, and price; test expectations now derive from current `menu.json` | 14 focused and 108 gate tests green |
| Admin topic description validation | One updated fixture expectation; original per-node classification unavailable | `test_topic_menu_catalogue.py`, `test_simple_mapping_ui.py` | Required topic description remains enforced; test supplies valid fixture copy | Full suite green |
| Test isolation/configuration | Earlier failure-only replay showed 9 cases changed from fail to pass after fixture isolation | `tests/conftest.py`, provider/bot modules | Disable developer credentials/providers for normal tests and reset mutable config per test | Full suite green; not labeled flaky |
| Event/bulk audit metadata | One original structured-understanding node, plus a separately fixed event node | responder and client-QA acceptance tests | Customer reply remains human handoff; metadata allowlist is event, quantity, location; no RAG/Groq; phone is not copied into structured metadata | Focused and full suite green |

## Explicit Phase 0 production metadata audit

- Necessary: **YES, for the current handoff-audit contract.** Fail-closed routing previously left recognized event/bulk intent details out of structured handoff metadata when NLU was disabled/unavailable.
- Exposing tests: `tests/test_structured_understanding.py::test_event_request_uses_deterministic_human_handoff` and the separately fixed `tests/test_client_qa_acceptance.py::test_unsupported_event_after_home_table_context_hands_off`.
- Customer-facing response: remains the existing human handoff; no unsupported answer is generated.
- Retained fields: allowlisted `event`, `quantity`, `location`, only when recognized and non-empty.
- Privacy: no arbitrary customer text, phone number, or name is copied into `last_entities`. Existing knowledge-gap logging of the original utterance predates this change.
- Tests: client-QA acceptance asserts entity-key allowlist, absence of a phone number, human handoff, and no RAG/Groq call. The full suite passed.

## Known groups and removed tests

- `client_followups`: current-contract expectations updated; focused/full suite green.
- `whatsapp_cleanup`: human-only/manual-only contract retained; focused/full suite green.
- `instagram_account_guard`: canonical approved-hours questions now answer approved copy; unsupported requests still hand off; full suite green.
- Story/Reel/source: exact identity, explicit-intent precedence, Description-only source details, and fail-closed unsupported follow-ups retained; full suite green.
- Admin archive expectations: no obsolete archive UI was restored; migrations/history compatibility remains covered.
- No test was removed during Phase 0B. Two pre-existing deletions (`test_escalation_destination.py`, `test_no_flowers_anywhere.py`) were already in the initial dirty worktree; their replacement coverage is documented in `TEST_SUITE_AUDIT.md`.

## Final gates

- Fast CI gate: **280 passed, 0 failed, 42.45s**.
- Phase 1 approved-answer policy: PASS.
- Phase 2 mapped priority product cannot authorize legacy answer: PASS.
- Phase 3 active state/current intent precedence: PASS.
- Phase 4 POST signature fail-closed: PASS.
- Phase 5 GET token fail-closed: PASS.
- Migration/persistence group: **108 passed** across session persistence, source/topic persistence and migration, seed reconciliation, durable jobs, candidates, quarantine/remediation, and admin session restart.
- Full suite: **2,123 passed, 0 failed, 0 skipped, 0 xfailed, 1 warning, 431.08s**.
- Static: `python -m compileall -q camellia`, schema validator, CI Flake8 selector, and `git diff --check` passed. YAML parsed with `fast-gate` and `full-regression` jobs.
- Warning: Starlette warns that `starlette.testclient` is deprecated in favor of `httpx2`; non-blocking for this run.

## Phase status

**PHASE 0 STATUS: BLOCKED — the complete suite and critical gates pass, but the requested per-failure A–F classification of all original 184 failures cannot be honestly certified from the surviving `--tb=no` artifact.**

Release readiness: **NOT READY — complete the evidence-backed 184-node A–F disposition before staging validation.** No deployment, push, or promotion was performed.
