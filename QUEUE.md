# Camellia Engineering Queue

This file is the single source of truth for deferred engineering work during fast development.

## Staging target

- Git branch: `codex/staging-fast-dev-20260922`
- Railway service: `ChatBOT-Staging`
- Production promotion is forbidden until the queue is processed and the user explicitly approves it.

## Operating mode

**Default mode: FAST IMPLEMENTATION + STAGING PREVIEW**

When the user requests a code/UI/routing change:

1. Start from the current staging branch unless the user explicitly names another baseline.
2. Implement the requested change immediately and keep the diff narrowly scoped.
3. Run only the minimum immediate safety checks needed to avoid stacking obviously broken code:
   - syntax/import/compile check for touched Python files,
   - the smallest directly affected smoke or focused test set,
   - `git diff --check`.
4. Commit and push the change to the staging branch.
5. Deploy/update **staging only** so the user can inspect the change.
6. Append all expensive engineering validation to this file instead of running it immediately.
7. Do **not** deploy to production unless the user explicitly asks to finalize/promote after the queue has been processed.
8. Do **not** run live Instagram tests unless the user explicitly changes that instruction.
9. Do **not** rewrite approved QA/canonical answers unless the user explicitly requests a QA change.

## Never defer these checks

The following must still happen immediately when relevant because postponing them can make later changes unsafe:

- Python syntax/import/startup sanity for touched runtime code.
- Database migration/schema initialization sanity when schema code changes.
- A minimal auth/security smoke check when authentication, CSRF, webhook signature, or permissions code changes.
- A minimal route-startup check when webhook/runtime startup code changes.
- `git diff --check`.

These are **not** substitutes for the final validation gate below.

## Status model

Every queued change uses one of these states:

- `IMPLEMENTED`
- `STAGING_DEPLOYED`
- `USER_APPROVED`
- `VALIDATION_PENDING`
- `VALIDATED`
- `PRODUCTION_DEPLOYED`

Do not describe a change as fully validated or production-ready while its status is `VALIDATION_PENDING`.

## Queue item template

```md
### Q-XXX — Short title

Status: IMPLEMENTED / STAGING_DEPLOYED / USER_APPROVED / VALIDATION_PENDING
Commit: <sha>
Branch: <branch>
Risk: Low | Medium | High

#### Implemented
- What changed

#### Immediate checks already run
- Minimal checks only

#### Deferred validation
- Focused regression tests
- Security/auth/privacy/channel/webhook checks as applicable
- Migration/restart durability checks as applicable
- Approved QA byte-for-byte verification
- Full repository test suite
- Schema validation
- Final compile/diff/lint checks

#### Notes / dependencies
- Any follow-up or ordering requirement
```

---

# Open queue

### Q-001 — Instagram Media Gallery V2

Status: IMPLEMENTED / VALIDATION_PENDING
Commit: `b972ba03802866a513e87e8ccabd35bc7ed6c7f5`
Branch: `codex/staging-fast-dev-20260922`
Risk: Medium

#### Implemented
- Additive durable `instagram_media` SQLite registry with exact Meta-ID uniqueness.
- Paginated authenticated Admin API and route-mode editing (`DEFAULT`, `CUSTOM`, `DISABLED`).
- Instagram-style Posts/Reels/Carousel gallery, technical detail drawer, and context-only caption label.
- Manual Sync Now infrastructure that returns safe `NOT_CONFIGURED` without a Graph request when credentials are absent.
- Staging-only fixture support, with Auto Sync and Auto Route explicitly OFF.

#### Immediate checks already run
- Python compile check for modified runtime files.
- SQLite initialization, upsert, route-mode, and pagination smoke check.
- `tests/test_instagram_source_routing.py` and `tests/test_admin_portal.py` (68 passed).
- `git diff --check`.

#### Deferred validation
- Full migration and restart-durability regression.
- 5,000-record gallery performance and pagination regression.
- Graph pagination, token-expiry, permission failure, duplicate/upsert, and resync route-preservation tests.
- Story, shared Reel, shared Post, and explicit-current-intent precedence regressions.
- Admin auth/CSRF/security and privacy/logging validation for the new endpoints.
- Webhook and canonical-QA byte comparison regressions.
- Full repository suite, production Meta sync acceptance, and final deployment verification.

### Q-002 — Gallery Simplification and Topic Safety

Status: IMPLEMENTED / VALIDATION_PENDING
Risk: Medium

#### Implemented
- Simplified Instagram Media to its gallery-first controls.
- Added guarded Topic deletion with reference detection, replacement, or explicit association clearing.
- Kept historical archive data intact while removing gallery archive controls.

#### Deferred validation
- Client-approved QA completeness and generated-template visibility audit.
- Internal guard regression after QA-library separation.
- Topic deletion/reference integrity, gallery and Story mapping regressions.
- Archived-data compatibility, migration/restart durability, Admin auth/CSRF/security, privacy/logging, webhook, 5,000-media performance, full suite, and final production validation.

### Q-003 — Archive-free Admin and Client Q&A Library

Status: IMPLEMENTED / VALIDATION_PENDING
Risk: Medium

#### Implemented
- Removed normal archive filters/actions and archived route loading from Admin mapping and Story flows.
- Made the Admin template library read-only and client-approved-only, backed by the parsed authoritative client Q&A registry.
- Kept legacy `qa_templates.json` guards as runtime-only safety mechanisms.

#### Deferred validation
- Full visual sweep for hidden compatibility controls and archive-data behavior.
- Full client-Q&A completeness/byte comparison and guard-routing regression.
- Admin auth/CSRF, migration/restart, webhook/privacy, gallery scale, full suite, and final production validation.

### Q-004 — Client-approved-only Answers and Handoff Identity

Status: IMPLEMENTED / VALIDATION_PENDING
Risk: High

#### Implemented
- Enforced the attached client Q&A registry as the only Instagram customer-answer source.
- Registry misses cannot reach legacy templates, learned answers, RAG, or Groq customer output.
- Unsupported questions create/reuse an Instagram handoff with the exact approved manager notice and no automatic WhatsApp link.
- Preserved exact sender ID and verified username together; Admin identity priority is username, sender ID, then generic label.
- Added Admin handoff search across username, CAM reference, and sender ID.

#### Deferred validation
- Full approved-answer byte comparison and semantic-match boundary corpus.
- Full conversation/source-routing, order, CAM, privacy, auth/CSRF, webhook, migration/restart, and repository regressions.
- Staging Meta username-resolution acceptance without sending live messages.

---

# Final release gate

Run this section only when the user says **"execute queue"**, **"finalize candidate"**, **"prepare for production"**, or equivalent.

Process all open queue items together and de-duplicate overlapping work. The final gate must include, as applicable:

1. Review every queued diff and dependency in implementation order.
2. Run grouped focused regression suites once rather than once per historical patch.
3. Run Admin/auth/CSRF/security regressions.
4. Run privacy/channel/webhook regressions.
5. Run canonical/freeze/routing regressions.
6. Validate all DB migrations, startup initialization, restart durability, and tombstone behavior changed by queued work.
7. Run schema validation.
8. Run Python compilation/import checks.
9. Run `git diff --check`.
10. Run approved QA source/template byte-for-byte verification.
11. Run the complete repository test suite once.
12. Fix failures, rerun affected focused tests, then run one final complete suite.
13. Verify GitHub HEAD equals the final tested SHA and the worktree is clean.
14. Promote the **exact same tested SHA** to production only after explicit user approval.
15. Verify Railway startup, health, webhook verification, and customer-facing runtime without live Instagram messaging.
16. Create the release tag only after production verification and explicit release approval.

## Permanent Camellia safeguards

- Instagram is the AI chatbot channel; WhatsApp remains human-only.
- Explicit current customer intent overrides stale media/source context.
- Never hallucinate prices, shelf life, availability, delivery promises, ingredients, allergens, or unsupported business facts.
- Approved canonical answers remain exact unless explicitly changed through the approval workflow.
- Groq/LLM failure must not break deterministic canonical routes.
- Story/Post/Reel identity must use exact Meta identifiers; no OCR, caption matching, image similarity, timing inference, or nearest-content guessing.
- Story Auto Sync remains OFF unless explicitly approved.
- LLM Auto Resolve remains OFF unless explicitly approved.
- Production Admin maintenance mode is controlled independently from staging.
- Never run live Instagram tests unless the user explicitly authorizes them.
