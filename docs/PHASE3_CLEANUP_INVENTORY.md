# Phase 3 cleanup inventory

Baseline: `d0559a35db810be788cbbddb0c69df2c5fb48887`.
Cleanup changes no customer facts, routes, schema, data paths or provider policy.

| Item | Evidence/current use | Classification/action |
| --- | --- | --- |
| Gemini | No runtime module/import or dependency; only two CI environment entries and nonexistent CodeRabbit path | REMOVE_SAFE: CI entries; point review rule at existing Groq responder |
| Duplicate LLM flags | Python's final assignments default triage/assisted/auto-resolve to false | CONSOLIDATE_SAFE: remove earlier assignments; preserve final parsing/defaults |
| ADMIN_PIN | Config load and startup deprecation log only; require_admin ignores provided_pin | REMOVE_SAFE: telemetry/config, fixture fallback and local sample; retain denial endpoint/header compatibility |
| noop-placeholder | No tracked repository hits; absent service variable-name inventories | No repository action; unknown infrastructure entries must not be deleted |
| railway.json | Live production/staging both RAILPACK; same Python start/restart contract | CONSOLIDATE_SAFE: declare RAILPACK, validate exact-SHA staging build |
| Dockerfile/compose | Valid local container build and durable local volume | ACTIVE_LOCAL / RECOVERY_REQUIRED: keep; correct browser auth environment names |
| Procfile/main.py | Valid Python startup fallback/actual entry point | RECOVERY_REQUIRED / ACTIVE_PRODUCTION: keep |
| Queue/checkpoint/v1 runbook | Old branch, fast-preview and seed/rollback instructions | HISTORICAL: label superseded, preserve evidence; current runbook linked |
| Root backup document | Contains unique recovery refs and historical volume guidance | RECOVERY_REQUIRED / HISTORICAL: keep, link authoritative Phase 2F online-backup procedure |
| docs/deployment.md | Pre-lifecycle menu mutation/static-flyer instructions | HISTORICAL: explicitly superseded; current domains/runbooks supplied |
| Other phase/test audit docs | Security, failure-disposition and handover evidence | HISTORICAL / HANDOVER_EVIDENCE: preserve |
| Runtime safety modules | Routes/imports/tests consume handoff, source, menu, RAG and auth guards | KEEP_ACTIVE: no speculative module deletion |
| Requirements | FastAPI/uvicorn startup; httpx provider/Meta/email; dotenv config; slowapi limits; argon2 auth; multipart upload; Pillow assets; pypdfium2 PDF review | KEEP_ACTIVE: no dependencies added, removed or upgraded |
| Local benchmark/cache/worktrees | Separate historical/dirty user checkouts, ignored DB/uploads and generated Graphify | DEFER: preserve user/recovery work; not included in commits |
| Disabled tester/diagnostics | Guarded local test architecture; authenticated bounded diagnostics | ACTIVE_REQUIRED / TEST_ONLY: keep fail-closed guards, no new public routes |

## Menu compatibility

`menu.json` is a shipped compatibility/read baseline, not permission to publish
an OCR draft or bypass reviewed Phase 2C versions. Existing approved publication
and pending-replacement protections remain authoritative. Startup fact mutation
is disabled. No file migration, path change or business-data rewrite is performed.

## Railway deferred inventory — no variable/resource changes authorized

- Production GEMINI_API_KEY/GEMINI_ENABLED/GEMINI_MODEL/GEMINI_TIMEOUT/ADMIN_PIN
  and staging GEMINI_ENABLED: DEFERRED_AMBIGUOUS_RAILWAY_CONFIG. OAuth cannot
  prove incoming references from hidden expressions. These variables are inert
  to this repository; retain values without exposing or recreating them.
- Production-environment ChatBOT-Staging: DEFERRED_DESTRUCTIVE_RAILWAY_RESOURCE_CLEANUP.
  Service `ccf79d27-2073-4424-8e0d-ba7827157c7f`, domain
  `chatbot-staging-production-50c3.up.railway.app`, volume
  `810a1c93-74e0-4e66-a0c9-ac3e00e12cdb`. Deployment exists; complete traffic/
  dependency absence is not proven. Do not delete it.
- Correct staging is that service's instance in environment
  `6f1691a7-f2f9-4eb0-8293-69b768c37f10`, domain
  `chatbot-staging-staging.up.railway.app`, volume
  `399ffab7-57c6-4f80-9d57-34a07354f9d5`.
- Empty staged patches `39f6e9da-198e-4f3e-b5fc-7c8234a0c8fd` (staging) and
  `c38751ba-8b0f-4688-98d4-e166e9a8a0fa` (production): DEFERRED_RAILWAY_HYGIENE.
  No accept/apply/dismiss action is performed.
- Remote branch deletion is optional and not performed. Preserve staging source,
  all recovery refs, unique/unmerged work and active branches. An ancestor tip is
  merely a possible future deletion candidate, not authorization to delete.
  Baseline audit: 107 origin refs including symbolic HEAD; 74 merged retained,
  26 unique/unmerged retained, five recovery retained, two main/source refs.
  No branch or tag is deleted or moved.

## Remaining search classifications

Retired provider/PIN names in this inventory are historical/deferred metadata;
cleanup tests naming them are TEST_ONLY. Ignored legacy PIN headers and the
retired verify-pin denial route are ACTIVE_REQUIRED security compatibility.
Older domains/build descriptions/rollback SHAs in labelled historical documents
are HISTORICAL_DOC / RECOVERY_REQUIRED, never current deployment instructions.
No executable alternate-provider integration remains.
