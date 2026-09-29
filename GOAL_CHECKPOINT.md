# Ralph stabilization checkpoint

> HISTORICAL — prior routing stabilization evidence, not the current branch,
> release baseline or operating procedure. See [current operations](docs/PRODUCTION_RUNBOOK.md).

- Branch: `codex/routing-stabilization-ralph-20260920`
- Base: `ae8b554fc737d8383ee9ec218d3beeb2569c9386`
- Completed: client-QA precedence fixes, additive handoff schema migration,
  generic callback vs purchase WhatsApp policy, route lanes/traces, grounded
  Bento comparisons, and sensitive/customisation handoff guards.
- Current validation: golden, dirty-state, focused, full, schema, compile, and
  fatal-lint checks are green; hosted CI and controlled live acceptance remain.
- Stop condition: all acceptance checks pass, or an external CI/Railway/live
  blocker is evidenced and recorded.
