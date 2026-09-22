# Camellia Engineering Queue

This file is the single source of truth for deferred engineering work during fast development.

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

No deferred items yet. Add the next implemented staging change as `Q-001`.

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
