# Frozen baseline and change-impact testing

Previously validated unrelated application code is **frozen**. Change only what
the scoped task requires; justify unrelated edits explicitly. Frozen does not
mean bugs cannot be fixed. Never refactor working business logic for test speed.

## Baseline and promotion

`test_baseline.json` records the operator-approved release
`1d19a97bef4967bff806b14ac58c9328cd0aaa74`, with 2,597 passing tests and accepted
production deployment `a784414d-d8db-48b8-9a31-11c1083338fe`. This is evidence for
that exact release, not a claim about uncommitted changes. It never auto-advances.

After a full regression and review of a new committed candidate, an operator
may deliberately promote the comparison baseline from a **clean** checkout:

```powershell
python scripts/update_test_baseline.py --sha <validated-SHA> --test-count <passed-count> --confirm-full-regression
```

The utility resolves a commit and records the operator's explicit attestation;
it cannot verify that tests or production acceptance actually happened. Production
validation defaults to false. Only after actual release acceptance, additionally
provide `--production-validated --deployment-id <actual-id>`. Review and commit
the changed record separately. It stores the prior SHA and timestamp. Never run
this utility automatically from CI or nightly tests.

**One-time transition:** new runner/map infrastructure intentionally escalates to
full regression. While those files remain changed relative to the old application
baseline, default runs remain full. Once infrastructure is committed and fully
validated, deliberately promote its comparison baseline (without pretending it
was deployed). Otherwise use `--baseline <validated-infrastructure-SHA>` locally.
Keep the original release evidence in Git history. Do not lie about production
validation to unlock faster testing.

The separate promotion commit's `test_baseline.json` and baseline documentation
are non-runtime/LOW-risk metadata. They do not request a full regression by
themselves. The baseline is still parsed and its immutable SHA resolved before
selection: missing, invalid, or unresolvable metadata remains a safe error.
Runner/map implementation changes, and any accompanying core or unknown runtime
change, still escalate to full regression. Only the exact root metadata path is
allow-listed; similarly named application/data files are not downgraded.

## Developer workflow

```powershell
python scripts/run_impacted_tests.py
python scripts/run_impacted_tests.py --list
python scripts/run_impacted_tests.py --smoke-only
python scripts/run_impacted_tests.py --full
python scripts/run_impacted_tests.py --baseline <validated-SHA>
```

The default command compares `known_good_sha...HEAD`, unstaged changes, staged
changes, and non-ignored untracked paths. Renames are treated as deletion plus
addition, retaining risk of the old path. Deleted files do not become low risk.
Git-ignored files (including local credentials) are not read or staged. Baseline
overrides never write the record. Use a baseline from the current validated
lineage; divergent history may conservatively select additional tests.

The runner prints every changed path, its risk/reason, and the exact pytest
command. Test selection uses exact paths/node IDs, not fuzzy `-k` expressions.
File targets subsume node IDs from the same file. Missing test files are errors;
renamed/missing node IDs remain pytest collection failures. Tests must be updated
in the map when deliberately renamed. No xfail/skip injection, JSON repair, test
deletion, assertion weakening, exit-code suppression, or collection-error hiding.

| Risk | Inputs | Default action |
| --- | --- | --- |
| LOW | Allow-listed docs/README and non-runtime metadata | No runtime pytest |
| MEDIUM | Menu Admin/catalogue presentation; changed test modules | Feature suites + smoke |
| HIGH | Menu authority, responder/RAG, source routing, ordering, handoffs/candidates | Feature suites + safety/authority smoke |
| CRITICAL | Database, app, config, auth, sessions, OAuth, startup, test infrastructure, shared fixtures, workflow | Full suite |
| Unknown | Any unmapped runtime/config/data/dependency input | Full suite, never silent low risk |

Even comment-only edits in executable modules retain that module's risk: the
selector does not attempt to prove arbitrary code edits harmless. A docs directory
does not make executable files low risk. A zero-target runtime mapping escalates
to full. A deleted selected test blocks selection rather than pretending coverage.

`--smoke-only` is a deliberately requested diagnostic, **not** a replacement for
critical/high-risk/release validation; it prints this warning. `--list` does not
execute pytest. `--full` deliberately works without baseline metadata so missing
or unresolved baseline cannot prevent broad validation. Otherwise invalid or
unresolvable baseline returns error 2, never a successful zero-test run. In CI,
resolve baseline history or explicitly use `--full`; never silently skip. Actual
pytest failure/collection/no-tests exit codes propagate unchanged.

## Smoke coverage and ownership

`scripts/test_impact_map.py` maintains exact existing node IDs. Collection expands
parameterized cases. This small set checks startup/import and health, Admin login,
CSRF, POST signatures and GET verify token fail-closed, Instagram approved answers,
WhatsApp manual-only/absent webhook, Groq-only/no retired provider references,
approved QA registry, unsupported product price and unapproved fact refusal,
private drafts and pending-menu stale-price blocking, WAITING_FOR_HUMAN,
candidate approval eligibility/audit, source clarification and explicit intent,
public tester/debug absence, and deterministic readiness during provider failure.

Do not duplicate these tests or add hundreds of markers. Current `pytest.ini`
has no registered feature markers; explicit mapping is simpler. Runtime node
IDs must be maintained with the original tests. New runtime files deliberately
escalate until ownership is reviewed and explicitly added.

Feature ownership lives in the same map: menu UX/taxonomy/category/topic tests;
menu lifecycle/review/publication, stale facts, assets and price authority;
approved responses, RAG and explicit context; exact Instagram identity and
Story/Post/Reel mapping; order price authority; handoff/candidate approval safety.
Shared/core edits and unknown files run everything. This is conservative selection,
not a proof that unrelated test suites could never detect a cross-feature bug.

## Failure debugging

```powershell
python -m pytest --lf -q
python -m pytest --ff -q
```

Classify failure root causes, make the smallest correct fix, rerun affected
modules, then widen by risk. `--lf` cache is a diagnostic, not release evidence;
`--ff` still executes the selected full collection, failed tests first.

## CI and full-regression policy

Existing `.github/workflows/ci.yml` triggers and gates are preserved; only the
new runner unit-test module is added to the fast current-contract command:

- Push/PR: fast current-contract suites, webhook security, static and schema checks.
- Scheduled: one existing daily full job, cron `23 2 * * *` (UTC).
- Manual/release: the existing full job executes `python -m pytest -q`.

No duplicate nightly jobs or auto-baseline promotion. Local impact testing is the
default developer workflow. Changed-file selection in CI is intentionally deferred:
keep the proven fast gate plus runner tests rather than adding redundant suites
or accidentally running all tests on every ordinary push. If integrating later,
fetch baseline history and
fail/escalate on missing history; never downgrade coverage because checkout is shallow.

Full regression remains mandatory for critical/core escalation, unsafe/unknown
classification, nightly, release, final handover, and explicit operator request.
Small well-mapped feature edits use affected tests + smoke + diff review.

## Parallelism and profiling

Parallel pytest: **DEFERRED**, not demonstrated safe. `tests/conftest.py` pins one
process database singleton, clears shared tables/caches, and imports a shared
FastAPI app. Other tests patch module globals/environment and use Node subprocesses.
Worker-local temp roots alone do not prove subprocess/file/collection isolation.
No pytest-xdist dependency or `-n` flags are introduced. A separate measured
isolation study is required before enabling `-n 2` or `-n 4` in CI.

This phase profiles **one** final full run with `python -m pytest -q --durations=30`;
the duration option does not alter selection or outcomes. Summarize measured slow
categories in the final report; do not rewrite production behavior to speed them up.

## Infrastructure validation

Run impact-runner tests first, then the existing-node smoke set, compile/severe
flake8/schema/workflow checks, the exact existing fast CI commands, and one final
full regression. Tests include real disposable Git repositories for changed-file
detection and isolated fake subprocesses for CLI/exit-code tests. Baseline promotion
tests write only pytest temp directories. No Railway, Meta, production data,
customer messages, or business-functionality changes belong in this task.

## Measured validation — 2026-09-30

- Runner tests: 75 passed in 35.40 seconds.
- Smoke: 68 passed in 20.31 seconds.
- One final full run through `--full`, with `PYTEST_ADDOPTS=--durations=30`:
  2,673 passed, zero failed, one existing Starlette/httpx warning, 750.54 seconds.
- The increase from the frozen 2,597 is 75 new runner cases plus one additional
  parameter of the existing per-test-file duplicate-definition safety check.
- Largest duration: mocked 1,000-row Story page-cap sync, 25.12 seconds.
  Next: bot identity queries 2.62s, actionable handoff counting 2.50s, multi-turn
  order/session stress 2.39s, catalogue image query-growth check 2.11s, authentication
  cooldown 2.08s, and 50-menu bounded-reference projection 2.00s.
- These measurements identify DB-heavy/source projection, responder multi-turn,
  and authentication categories; they do not establish universal speed savings.
  No slow tests, safety assertions, application behavior or dependencies changed.

The record still points to the original approved release. Promotion requires a
separate deliberate clean-tree invocation after a validated commit exists.

The subsequently authorized metadata-classification correction passed 88 runner
tests and 68 smoke tests (13.74s), plus compile/static/schema/YAML/diff checks.
The earlier full run was not repeated for this non-runtime classification change.
Core/unknown-runtime escalation remains covered by the focused regression tests.
