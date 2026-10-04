# Regression execution performance

This is test/CI infrastructure only. No application code, test assertions about
customer behavior, production data, credentials, or frozen baseline are changed.

## Baseline measured before implementation

Base: `e80b68bd913da3466c2b2b11568075449393ef35` (merged PR #62).
Windows, Python 3.14.0, the existing virtual environment, 20 logical CPUs.
Exactly one complete serial run through `python scripts/run_impacted_tests.py`:
**3,791 passed, zero skipped/failed; 1,447.58 seconds** (pytest: 1,447.25s).
An ignored profiling plugin recorded every case and setup/call/teardown report.
The reference 1,497â€“1,568 seconds is historical, not used as a measured baseline.

| Phase | Seconds |
| --- | ---: |
| Setup | 603.62 |
| Call | 832.36 |
| Teardown | 4.53 |
| Collection/startup/report overhead | 7.08 |

Slowest files (sum of setup/call/teardown):

| File | Seconds |
| --- | ---: |
| test_bot.py | 76.36 |
| test_approved_response_alignment.py | 72.37 |
| test_instagram_story_sync.py | 55.20 |
| test_approved_source_mapping.py | 54.93 |
| test_custom_qa.py | 45.18 |
| test_final_routing_menu_handoff.py | 44.80 |
| test_qa_menu3.py | 40.69 |
| test_phase4_security_acceptance.py | 39.24 |
| test_phase2b_pending_menu_handoff.py | 37.89 |
| test_order_intake.py | 31.02 |

Slowest individual calls:

| Test | Seconds |
| --- | ---: |
| story sync: page cap marks partial without reconciling absent stories | 48.28 |
| client approved QA: every canonical source block through real responder | 6.37 |
| bot: identity queries | 4.26 |
| bot: capabilities queries | 3.90 |
| phase3b output contract: deterministic output | 3.71 |
| phase3b output contract: stale source | 3.55 |
| mapped Home Table reels: real send | 3.39 |
| persistent session: same-user subject boundaries | 3.37 |

The top ten tests consume only 5.89% of test-phase time; the top ten files
consume 34.55%. Broad fixture/SQLite costs dominate, not a handful of tests.
Setup is about 42% of measured test-phase time. Existing autouse fixtures open
SQLite, check schema, delete session rows and commit per case; numerous tests
construct full temporary schemas and persist real rows. The 48-second story
test retains all 1,000 mocked story rows and real SQLite writes. Service tokens
are already cleared and dotenv loading suppressed before collection by the
existing conftest; external reads use mocks. No DB durability settings, network
mocks, sleeps, or application code were changed to manufacture speed.

## Audited execution

Install test dependencies with `python -m pip install -r requirements-test.txt`.
The normal command remains **`python scripts/run_impacted_tests.py`**.
The logical impact selection is unchanged. A bounded executor runs audited
files on four process workers, then all remaining cases serially. CI uses two
workers. `--workers 1` or `--serial` gives an explicit serial fallback; missing
xdist or changed isolation dependencies also falls back to serial.

`scripts/test_parallel_policy.json` records exact file fingerprints and serial
reasons. New or edited tests default to serial; line ending changes do not
invalidate the fingerprint. Changes to conftest, pytest configuration, config,
Database, bootstrap or upload-path helpers invalidate all parallel eligibility.
Updating this policy requires reviewing isolation again. The policy is test
execution ownership, not impact/routing/business authority.

Each process already creates a unique OS temporary singleton SQLite DB before
collection. Audited fixtures use tmp_path/TemporaryDirectory or that isolated
DB. Globals, monkeypatch and environment changes are local to a process.
No audited test depends on another file's execution order. Each file remains
together with `--dist loadfile`, as specified by the
[official xdist distribution documentation](https://pytest-xdist.readthedocs.io/en/stable/distribution.html).
All selected files are collected in each phase to preserve import-time behavior.
The serial phase begins only after workers exit.

Serial-sensitive files remain serial without rewriting the tests/application:

- Default menu/shared upload writes: bot, admin portal, menu review before publish.
- Launchers, background work, concurrent SQLite and clocks: access log,
  handoff order state, shared post pairing, durable Instagram jobs.
- Startup, restart and persistent singleton checks: session persistence,
  phase3b persistent session, story startup cleanup, phase2c startup authority.
- Legacy provider cache/config/reload fixtures: Groq stabilization, router and
  triage shadows, subtopic safety, hybrid understanding, primary NLU,
  AI consolidation, OCR resilience, upload decoupling, menu manager,
  stress scenarios and Admin second pass.
- Runner/executor/git/launcher contract tests: impacted runner, executor,
  repository cleanup. Exact file list and rationale are in the policy.

Per-run phase reports and aggregate evidence are kept under ignored
`.test-results/`. Collection inventories must agree across workers and phases.
The exact selected/executed union must equal the original collection once:
omissions, duplicates, crashes and missing reports fail the command. Pytest
failures remain failures; the serial phase still runs after a parallel failure.
Empty phases are accepted only when their complete collection proves that the
other phase owns the cases. Existing skips are reported, never manufactured.

## Impact and CI gates

The frozen `test_baseline.json` remains unchanged. The default comparison still
includes every commit since that accepted baseline, plus staged/unstaged and
untracked changes. Older merged edits to app.py/database/responder therefore
continue to trigger CRITICAL even for a newer small UI change. The monolithic
app and shared runtime dependencies make that escalation justified. No impact
area was downgraded without dependency proof. New executor/policy/dependency
inputs are explicitly CRITICAL.

`--current-contract` is an explicit CI mode, not a full-release substitute.
It retains all 22 existing contract files plus every one of the eight webhook
security functions (11 parameterized cases), and adds executor safety tests.
It consolidates two pytest launches, without dropping either suite.

Both jobs reuse a composite setup action and pip cache keyed by runtime **and**
test requirements. Both keep Python 3.11, existing lint/schema checks, required
job names, event conditions and timeouts. Reports upload even on failure.
Push and PR checks remain separate because head and merge commits differ;
nightly/dispatch/release full regression remains separate from the PR contract
gate. No required protection is removed.

## Reliability and measured optimized runs

Both optimized runs used the normal impact command and classified CRITICAL/full.

| Run | Cases passed | Wall seconds | Reduction from measured serial |
| --- | ---: | ---: | ---: |
| Serial baseline | 3,791 | 1,447.58 | — |
| Optimized first proof | 3,815 | 284.04 | 80.38% |
| Optimized final repeat | 3,818 | 275.48 | 80.97% |

All runs had zero failures/skips. The first proof added 23 executor cases plus
one automatic no-shadowed-test case; final repeat adds three more executor
safety cases (26 new executor tests, one automatic case). All **3,791 original
node IDs** are present in both optimized collected/executed unions, with no
omission or duplicate. Original sorted-node SHA256:
`320c9383edde6e1ed3cf3f04b5dffaaee564010b0f2e70eddab345132e7181d9`.
Final partition: **3,078 parallel cases in 125 audited files; 740 serial cases**.
The final report phase durations are 139.54s parallel and
132.31s serial, plus subprocess/aggregation overhead.

Focused infrastructure checks: 147 passed before the first full proof; final
executor checks: 26 passed. Real two-process temporary suites exercise both
failure phases, exact union, temp SQLite isolation and legitimately empty
phases. Duplicate execution, worker inventory mismatch, omitted cases and
missing evidence are tested as failures. Existing behavior assertions were
unchanged; the two legacy workflow-command assertions now check the preserved
runner/gate contract instead of the replaced direct pytest invocation.

These are Windows/Python 3.14 measurements on this machine, not guaranteed CI
speedups. CI runs the same executor on Ubuntu/Python 3.11 with two workers.

