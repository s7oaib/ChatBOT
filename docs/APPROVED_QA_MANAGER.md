# Approved Q&A manager

The Admin **Approved Q&A** workspace reads only the immutable canonical client
registry and explicit Admin overrides. Learned knowledge, candidate templates,
AI Resolution and the legacy Custom Q&A remain separate. The legacy workspace
is available under **Advanced / Legacy**.

Add or edit a copy, run **Preview / Test**, then **Publish Approved Q&A**. Preview
and the customer-question tester do not send messages, create conversations,
modify sessions or call an LLM. Publication requires a signed, unexpired preview
of the exact draft against the current registry. A changed registry requires a
new preview. Primary questions and every variation are checked for conflicts
with existing exact and QA-S1 semantic routes. Publication and history writes
commit together; a failed write preserves the live answer.

Delete archives a record, preserving history. Restore creates another version
and rechecks conflicts. Version restoration also restores that version's validity
window; restoring an expired window does not make its answer eligible.

Dates entered in the form represent **Asia/Kolkata**. Timestamps are stored with
explicit offsets. Scheduled and expired status is evaluated at lookup time,
without a scheduler or restart. Expiring Soon means an active answer expires
within seven days. Archived and expired answers are available in history.

Only the two canonical Bengali Bento entries are bootstrapped with validity
through **11 October 2026, 23:59:59 Asia/Kolkata**. Bootstrap is idempotent and
never overwrites an operator's later changes. At 12 October midnight IST both
entries stop matching, including contextual follow-ups and derived price answers.

`approved_qa_overrides` stores explicit field overrides and text-only additions.
`approved_qa_versions` stores immutable history snapshots. These are isolated,
additive tables; no existing knowledge records are migrated. The canonical source
file is never written. Existing media references and Home Table Reel ordering
remain read-only. Empty overlays preserve all 23 original entries exactly.

Runtime DB read failures retain the last successful overlay snapshot. A cold
failure falls back to the immutable source and logs the failure. Admin writes and
previews fail closed when storage cannot be read. Existing authentication and
browser CSRF protection apply to every new endpoint.

Canonical acceptance tests use a clock fixed at the QA-A1 acceptance date;
lifecycle tests explicitly advance it across scheduling and expiry boundaries.
The deployed application always uses the real clock.
