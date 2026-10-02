# Camellia Artisanal production runbook

## Current handover operating state

Accepted runtime `f58b1f4157e32361487320f6db2ec72bcdba3342`, deployment
`5b1abab4-c414-4662-9a2f-ce41000eb3bb`, verified 3 October 2026 IST.
See [CLIENT_HANDOVER.md](CLIENT_HANDOVER.md) for dated authority, release evidence
and operator checklists. The final operator-selected Instagram chatbot mode is
**LIVE**. Durable LIVE/TEST_ONLY remains independent from channel capability;
never switch mode merely as a health remedy or to make smoke testing easier.
Only explicit operator resume clears WAITING_FOR_HUMAN; deploy/restart does not.

The accepted runtime includes PR #55 (manual exact-link Post/Reel mapping,
ordered Custom Q&A blocks, safe SYSTEM override reset and bare-product order
routing) and PR #56 (centralized Collection 1/2 Sondesh menu authority).

Both Bento QA entries expire 11 October 2026 at 23:59:59 Asia/Kolkata.
Review whether they should expire or receive deliberate Admin preview/publish
validity extensions before then; do not silently extend them.

Weekly Savoury CURRENT reviewed data alone is customer authority; catalogue
Savoury is its projection. Upcoming and unverified OCR are not current facts.
Approved Q&A preview/publish controls exact copy and validity; protected SYSTEM
response dependencies must not be permanently deleted.

Collection 1/Aamar Mishti and Collection 2/Signature resolve from their current
eligible Menu Catalogue records at response time. Replace/publish those menus in
one canonical place; do not add hardcoded customer-response image paths.

## Health and safety checks

Production: `https://web-production-0cad70.up.railway.app`, ChatBOT in production.
Staging: `https://chatbot-staging-staging.up.railway.app`, ChatBOT-Staging in staging.
Both belong to Camellia-Artisanal-Chatbot, project
`036c018e-2841-4412-8c97-fdf098af4a79`. Do not use the similarly named service
inside the production environment. Volume mount remains `/app/var`.
See authenticated Settings / Health for bounded operations diagnostics.
Provider outages can report degraded with HTTP 200 when deterministic readiness
remains available; missing critical knowledge/database gives HTTP 503.

1. Check `GET /health` and confirm `status=healthy`, Instagram delivery is
   configured, public Web Tester is disabled, and WhatsApp is human-only.
2. Check the global Admin chatbot-control card and confirm the mode is the
   intentional operator choice. Current handover state is LIVE; if deliberately
   switched to TEST_ONLY, verify the exact enabled sender allowlist.
3. Check the authenticated `GET /admin/auto-resolve/evaluation` endpoint.
   Keep automatic-send experimentation off unless explicitly authorized.
4. Check authenticated assisted-resolution evaluation before reviewing a
   customer reply. Never treat a simulation decision as a sent reply.

## Disable controls and kill switch

Set `LLM_ASSISTED_RESOLUTION_ENABLED=false` or `LLM_TRIAGE_ENABLED=false` in
the deployment environment when the corresponding review/triage path must be
stopped. Set `LLM_AUTO_RESOLVE_ENABLED=false` to keep automatic sending off.
The authenticated `POST /admin/auto-resolve/kill-switch` endpoint with
`{"enabled": false}` stops any future automatic-send path immediately without
waiting for a deploy. Leave simulation enabled only when observing decisions is
still useful and approved.

## Backup and restore

The complete storage and recovery contract, including verified versus
operator-only steps, is in [`PRODUCTION_BACKUP_RECOVERY.md`](PRODUCTION_BACKUP_RECOVERY.md).

Before every schema migration, call authenticated `POST /admin/db-backup`.
Record the returned `backup_path` and `size_bytes`, then verify the file exists
and opens with `sqlite3 ... "PRAGMA integrity_check;"`. A restore is a manual,
operator-approved operation: stop the service, preserve the current database,
replace it with the verified backup, and restart. Do not perform a destructive
production restore as part of routine deployment.

## Incident quick checks

Bot not responding: check public health, Railway deployment/startup logs, the
intentional LIVE/TEST_ONLY state, safe webhook receipt/signature rejection logs,
database readiness and active WAITING_FOR_HUMAN records. Run deterministic
regressions in an isolated fixture; check Groq only if long-tail answers are
affected. Never enable a public tester as a health workaround.

Wrong Sondesh menu: inspect Menu Catalogue `aamar_mishti` for Collection 1 and
`signature` for Collection 2. Customer paths must resolve their current eligible
image dynamically. Do not patch the responder with a filename or static URL.

Wrong weekly price/menu: inspect the current human-approved version, pending
replacement, validity window and existing Weekly Savoury authority state.
Pending review must block stale volatile facts. Review or reject through the
approved workflow; never edit SQLite or OCR truth to force a customer answer.
Human handoff is safe until approval.

Database issue: inspect safe error classes and storage attachment read-only.
Do not initialize an empty production DB or restore over live data. Obtain explicit
recovery authorization. Groq outage: deterministic answers stay available; long-tail
may hand off. Do not activate any alternate provider.

## LLM/provider outage procedure

Provider timeouts, rate limits, malformed JSON or grounding failures must fall
back to deterministic replies or Assisted/Human review. Do not increase retry
counts or create a retry storm. Confirm `/health` reports provider degradation,
inspect logs for the error class and keep automatic sending off.

## Instagram webhook and source routing

Verify Meta's challenge and signature configuration read-only, then use isolated
regression fixtures, not customer DMs. A shared Reel/Post or mapped source route
must win before Assisted/Auto Resolve.

Manual/collaborator exact-link entries are allowed for canonical `/p/` or
`/reel/` URLs but do not assert ownership. Shared Posts require explicit Meta ID
pairing when the webhook identity cannot be recovered from the permalink; never
infer identity from caption, OCR, title or image.

If source-routing overrides become abnormal, preserve evidence and request
explicit incident authorization before configuration changes or rollback.

## WhatsApp safety

WhatsApp automation has been removed. Confirm `/health` reports the expected
human-only policy. There is no WhatsApp webhook or Cloud API bot sender; the only
WhatsApp runtime surface is the business number used to build the customer-facing
`wa.me` continuation. Staff reply manually from the business account.

## Rollback deployment

1. Obtain explicit approval for rollback and any configuration changes.
2. Record the deployment ID and the failing health/evaluation evidence.
3. Redeploy the last known-good commit or use the host's rollback action.
4. Re-run `/health`, Instagram webhook verification and the manual handoff check.
5. Keep database backup and decision-audit evidence for review.

The accepted application rollback target at this handover is
`f58b1f4157e32361487320f6db2ec72bcdba3342`. Documentation-only commits after
handover do not become a new runtime baseline automatically.

## Future Auto Resolve pilot (not executed now)

Only after the readiness dashboard shows `PILOT ELIGIBLE` and human-reviewed
evidence is confirmed: select one safe category, set its category control on,
keep a high confidence threshold and all hard exclusions, monitor closely and
roll back immediately on an unsafe result. The chatbot being LIVE does not mean
LLM Auto Resolve is authorized.
