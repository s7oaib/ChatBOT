# Camellia Artisanal production runbook

## Health and safety checks

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

Before every schema migration, call authenticated `POST /admin/db-backup`.
Record the returned `backup_path` and `size_bytes`, then verify the file exists
and opens with `sqlite3 ... "PRAGMA integrity_check;"`. A restore is a manual,
operator-approved operation: stop the service, preserve the current database,
replace it with the verified backup, and restart. Do not perform a destructive
production restore as part of routine deployment.

## LLM/provider outage

Provider timeouts, rate limits, malformed JSON, or grounding failures must fall
back to deterministic replies or Assisted/Human review. Do not increase retry
counts or create a retry storm. Confirm `/health` reports the provider
degradation, then inspect logs for the error class and keep Auto Resolve off.

## Instagram webhook and source routing

Verify Meta's challenge and signature configuration, then send one controlled
test message. A shared reel/post or ad route must win before Assisted or Auto
Resolve. If `source-routing overrides` becomes non-zero, disable Assisted and
Auto Resolve, preserve the evidence, and roll back the deployment.

## WhatsApp safety

WhatsApp is HUMAN ONLY. Confirm `WHATSAPP_CHATBOT_ENABLED=false` and
`WHATSAPP_CHATBOT_ENABLE_CONFIRM=false`. An inbound WhatsApp message may be
recorded and linked to its CAM reference, but must receive no AI, Assisted, or
Auto Resolve reply. Staff reply manually from the business account.

## Rollback deployment

1. Disable Auto Resolve and Assisted Resolution in the host environment.
2. Record the deployment ID and the failing health/evaluation evidence.
3. Redeploy the last known-good commit or use the host's rollback action.
4. Re-run `/health`, webhook verification, and the WhatsApp human-only check.
5. Keep the database backup and decision audit rows for review.

## Future Auto Resolve pilot (not executed now)

Only after the readiness dashboard shows `PILOT ELIGIBLE` and human-reviewed
evidence is confirmed: select one safe category, set its category control on,
keep a high confidence threshold and all hard exclusions, monitor closely, and
roll back immediately on an unsafe result. Real customer sending is not enabled
by this runbook's current production configuration.
