# Frozen baseline and change-impact testing

Previously validated unrelated application code is **frozen**. Change only what the scoped task requires; justify unrelated edits explicitly. Frozen does not mean bugs cannot be fixed. Never refactor working business logic for test speed.

## Current baselines

- Accepted application/runtime and comparison baseline: `3d412c1e4f94be7552c8882cd127eed1e8eb9e14` (PR #65).
- Production acceptance: Railway `91f4c509-3589-492e-b667-a3a1d16e79de`, SUCCESS, exact accepted runtime SHA deployed; Online with one running replica and normal application startup/Approved Q&A alignment. Read-only mapping audit preserved all bindings/routes/history, including a real scheduled ad expiry.
- Final complete regression: **3904 passed, 0 failed, 0 skipped, 308.33 seconds**, on tested HEAD `aeec381ee900049a08ca2aba591d366b48c3eb24`. Tested tree `1cb2ba1fc1f207a342747e0ebb4674fa139793af` is identical to accepted runtime merge.
- PR #63 audited hybrid policy retained: 2991 parallel-safe cases, 913 serial cases. The 63 PR #64 contextual-price cases remain protected; 23 schedule cases and executed Admin JS expiry assertions were added.
- Final feature CI: **872 passed, 0 failed/skipped, 65.39 seconds** in the Python 3.11 current-contract gate; required PR/push/main CI passed. See [client handover](CLIENT_HANDOVER.md) for full release and production evidence.
- Previous frozen baseline: `f58b1f4157e32361487320f6db2ec72bcdba3342`. Promotion was deliberate through the existing utility only after complete regression and production acceptance, not an impact-classification shortcut.
- Later handover documentation/baseline metadata commits are non-runtime and distinct from this runtime baseline. Do not advance the application baseline for each docs-only commit or rerun the full suite merely for Markdown/metadata.

`test_baseline.json` records the comparison baseline used by `scripts/run_impacted_tests.py`. It never auto-advances.

To deliberately promote a future validated commit from a clean checkout:

```powershell
python scripts/update_test_baseline.py --sha <validated-SHA> --test-count <passed-count> --confirm-full-regression
```

Only after actual production acceptance, additionally provide:

```powershell
--production-validated --deployment-id <actual-id>
```

The utility resolves the commit and records the operator's explicit attestation; it cannot execute tests or independently prove production acceptance. Review and commit the changed record separately.

Baseline metadata (`test_baseline.json`) and this baseline documentation are non-runtime/LOW-risk. Missing, malformed or unresolvable baseline metadata still fails safely. Runner/map implementation changes and any core or unknown runtime change remain CRITICAL and escalate to the full suite.

## Developer workflow

```powershell
python scripts/run_impacted_tests.py
python scripts/run_impacted_tests.py --list
python scripts/run_impacted_tests.py --smoke-only
python scripts/run_impacted_tests.py --full
python scripts/run_impacted_tests.py --baseline <validated-SHA>
```

The default command compares `known_good_sha...HEAD`, unstaged changes, staged changes and non-ignored untracked paths. Renames are treated as deletion plus addition, so risk from the old path is preserved. Deleted files do not become low risk.

The runner prints every changed path, its risk/reason and the exact pytest command. Test selection uses exact paths/node IDs, not fuzzy `-k` expressions. File targets subsume node IDs from the same file. Missing test files or renamed/missing node IDs remain hard failures. No xfail/skip injection, JSON repair, test deletion, assertion weakening, exit-code suppression or collection-error hiding is allowed.

| Risk | Inputs | Default action |
| --- | --- | --- |
| LOW | Allow-listed docs/README and non-runtime baseline metadata | No runtime pytest |
| MEDIUM | Menu Admin/catalogue presentation; changed test modules | Feature suites + smoke |
| HIGH | Menu authority, responder/RAG, source routing, ordering, handoffs/candidates | Feature suites + safety/authority smoke |
| CRITICAL | Database, app, config, auth, sessions, OAuth, startup, test infrastructure, shared fixtures, workflow | Full suite |
| Unknown | Any unmapped runtime/config/data/dependency input | Full suite, never silent low risk |

Even comment-only edits in executable modules retain that module's risk. A zero-target runtime mapping escalates to full. `--smoke-only` is a diagnostic, not a replacement for high-risk/critical/release validation. `--list` does not execute pytest. `--full` deliberately works even when baseline metadata is unavailable.

## Smoke coverage and ownership

`scripts/test_impact_map.py` owns the explicit smoke and feature mappings. The current smoke set covers representative startup/health, Admin authentication and CSRF, webhook signature verification, Instagram/WhatsApp channel policy, Groq-only policy, approved QA, unsupported fact refusal, draft/pending-menu authority, WAITING_FOR_HUMAN, candidate approval safety, exact Meta identity behavior and disabled public tester/debug paths.

Do not duplicate hundreds of markers. New runtime files deliberately escalate until ownership is reviewed and mapped.

## Failure debugging

```powershell
python -m pytest --lf -q
python -m pytest --ff -q
```

`--lf` and `--ff` are debugging aids, not release evidence.

## CI and full-regression policy

The existing CI philosophy is preserved:

- Push/PR: fast current-contract suites, webhook security, static/schema checks, plus the impact-runner unit tests.
- Scheduled: the existing daily full job.
- Manual/release: the existing full job executes `python -m pytest -q`.

No duplicate nightly jobs and no automatic baseline promotion.

A full regression remains mandatory for critical/core escalation, unsafe/unknown classification, nightly, release, final handover and explicit operator request. Small well-mapped feature edits use affected tests + smoke + diff review.

## Parallelism and profiling

Parallel pytest remains **DEFERRED**. Shared database/module state, caches, environment mutation and subprocess usage need a separate isolation study before enabling pytest-xdist in CI.

Measured validation for the initial infrastructure on 2026-09-30:

- Runner tests: 75 passed in 35.40 seconds.
- Smoke: 68 passed in 20.31 seconds.
- Full regression: 2,673 passed, zero failed, one existing Starlette/httpx warning, 750.54 seconds.
- Largest measured test: mocked 1,000-row Story page-cap sync, 25.12 seconds.
- The subsequently authorized metadata-classification correction passed 88 runner tests and 68 smoke tests (13.74s), plus compile/static/schema/YAML/diff checks. The full suite was not repeated for that non-runtime classification correction.

## Frozen-code rule

For future scoped work:

1. Change only the required code.
2. Run `python scripts/run_impacted_tests.py`.
3. Run/retain the mandatory smoke coverage selected by the runner.
4. Inspect the diff.
5. Do not refactor unrelated validated modules.

This is conservative test selection, not proof that unrelated suites could never catch a cross-feature problem. When impact cannot be bounded safely, escalate.
