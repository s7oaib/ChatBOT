# Camellia Artisanal production runbook

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
   configured, `ENABLE_WEB_TESTER=false`, and WhatsApp is human-only.
2. Check the authenticated `GET /admin/auto-resolve/evaluation` endpoint.
   The normal production state is `AUTO RESOLVE: OFF`, simulation enabled, and
   zero WhatsApp activity, source-route overrides, and order/CAM interference.
3. Check the authenticated assisted-resolution evaluation before reviewing a
   customer reply. Never treat a simulation decision as a sent reply.

## Disable controls and kill switch

Set `LLM_ASSISTED_RESOLUTION_ENABLED=false` or `LLM_TRIAGE_ENABLED=false` in
the deployment environment when the corresponding review/triage path must be
stopped. Set `LLM_AUTO_RESOLVE_ENABLED=false` to keep automatic sending off.
The authenticated `POST /admin/auto-resolve/kill-switch` endpoint with
`{"enabled": false}` stops any future automatic-send path immediately without
waiting for a deploy. Leave `LLM_AUTO_RESOLVE_SIMULATION_ENABLED=true` when
observing decisions is still useful.

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

Bot not responding: check public health, Railway deployment/startup logs, safe
webhook receipt/signature rejection logs, database readiness, and active
WAITING_FOR_HUMAN records. Run deterministic regressions in an isolated fixture;
check Groq only if long-tail answers are affected. Never enable a public tester.

Wrong price/menu: inspect the current human-approved version, pending replacement,
validity window and existing Menu workspace authority state. Pending review must
block stale volatile facts. Review or reject through the approved workflow; never
edit SQLite or OCR truth to force a customer answer. Human handoff is safe until
approval. A failed extraction does not mean a failed publication.

Database issue: inspect safe error classes and storage attachment read-only.
Do not initialize an empty production DB or restore over live data. Obtain explicit
recovery authorization. Groq outage: deterministic answers stay available; long-tail
may hand off. Do not activate any alternate provider.

## LLM/provider outage procedure

Provider timeouts, rate limits, malformed JSON, or grounding failures must fall
back to deterministic replies or Assisted/Human review. Do not increase retry
counts or create a retry storm. Confirm `/health` reports the provider
degradation, then inspect logs for the error class and keep Auto Resolve off.

## Instagram webhook and source routing

Verify Meta's challenge and signature configuration read-only, then use isolated
regression fixtures, not customer DMs. A shared reel/post or ad route must win before Assisted or Auto
Resolve. If `source-routing overrides` becomes non-zero, preserve the evidence
and request explicit incident authorization before configuration changes or rollback.

## WhatsApp safety

WhatsApp automation has been removed. Confirm `/health` reports
`whatsapp.automation=removed` and `manual_handoff=available`. There is no
WhatsApp webhook or Cloud API sender; the only WhatsApp runtime surface is the
business number used to build the customer-facing `wa.me` link. Staff reply
manually from the business account.

## Rollback deployment

1. Obtain explicit approval for rollback and any configuration changes.
2. Record the deployment ID and the failing health/evaluation evidence.
3. Redeploy the last known-good commit or use the host's rollback action.
4. Re-run `/health`, Instagram webhook verification, and the manual handoff check.
5. Keep the database backup and decision audit rows for review.

## Future Auto Resolve pilot (not executed now)

Only after the readiness dashboard shows `PILOT ELIGIBLE` and human-reviewed
evidence is confirmed: select one safe category, set its category control on,
keep a high confidence threshold and all hard exclusions, monitor closely, and
roll back immediately on an unsafe result. Real customer sending is not enabled
by this runbook's current production configuration.
