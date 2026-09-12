# Camellia Artisanal v1.0 Production Runbook

## Health check

```powershell
Invoke-WebRequest https://chatbot-production-8164.up.railway.app/health -UseBasicParsing
```

Require HTTP 200, `status=healthy`, `answering.ready=true`, seed versions in
sync, durable storage, and no warnings. Confirm
`auto_resolve.actual_enabled=false`.

## Railway restart / rollback

1. Record `/health`, deployment ID, and recent logs.
2. Keep `LLM_AUTO_RESOLVE_ENABLED=false` and WhatsApp human-only flags.
3. Use Railway's deployment rollback/redeploy action for the last known-good
   commit; do not reset or wipe the volume.
4. Re-run health and the manual Instagram checklist.

## Meta webhook and outbound troubleshooting

- Check webhook signature/HTTP status and event parser logs.
- Follow `inbound → responder → outbound audit` correlation.
- Inspect `/admin/instagram-outbound-audit` with admin authentication; never
  expose tokens or authorization headers.
- A failed text chunk stops later chunks; the audit records status, Meta error,
  trace ID, chunk number and overall result.
- Do not regenerate credentials or change permissions without evidence.

## Response Library editing

Use the authenticated Response Library. Search by trigger/alias, preview the
copy, run dry-run, save, and verify the next runtime response. Version History
and Restore are the supported recovery path; no redeploy is needed for an
override copy change.

## CAM / human handoff

Instagram order or sensitive requests may create/reuse a CAM reference and a
WhatsApp handoff link. Confirm required order fields are present and that
completed handoffs do not leak stale order state into a new conversation.

## WhatsApp HUMAN ONLY

Keep `WHATSAPP_CHATBOT_ENABLED=false` and
`WHATSAPP_CHATBOT_ENABLE_CONFIRM=false`. WhatsApp inbound messages are read by
staff; they must not invoke responder, RAG, Groq, QA templates, Assisted
Resolution or Auto Resolve. Staff replies manually from the business account.

## Auto Resolve kill switch

Keep `LLM_AUTO_RESOLVE_ENABLED=false`. Simulation may remain enabled for
evaluation, but simulation must never send customer messages. If any automatic
send is observed, use the authenticated kill-switch endpoint with
`{"enabled":false}`, disable Assisted/Triage if necessary, preserve audit
evidence, and roll back.

## Database backup

Before a schema migration, an authenticated operator should call
`POST /admin/db-backup`, record the returned path/size, and verify
`PRAGMA integrity_check` is `ok`. Railway volume PITR is not available on the
current plan, so retain the returned file on durable storage and never
overwrite an earlier backup.

## v1.0 release verification (2026-09-13)

- Accepted production code commit: `pending deployment of composite-intent fix`.
- Current deployed HEAD: `15458c202d956a7fdf9d35e0252705b1eb24ebde`.
- Authenticated backup: `/app/var/camellia.db.backup-20260912_220436`.
- Backup timestamp: `2026-09-12 22:04:36 UTC`.
- Backup size: `1,937,408` bytes; `PRAGMA integrity_check`: `ok`.
- Live database remains `/app/var/camellia.db`; prior backups are retained.
- Three consecutive production health checks: HTTP 200, healthy, answering
  ready, live delivery, and no warnings/startup or migration errors.
- Safety flags: `ENABLE_WEB_TESTER=false`,
  `LLM_AUTO_RESOLVE_ENABLED=false`,
  `WHATSAPP_CHATBOT_ENABLED=false`,
  `WHATSAPP_CHATBOT_ENABLE_CONFIRM=false`; WhatsApp remains HUMAN ONLY.
- Full automated suite: **1,398 passed, 0 failed, 1 warning**.
- Composite-intent hardening: leading greetings are parsed separately from
  meaningful remainder text; explicit intent takes precedence over source
  context; greeting-only messages bypass stale source content.
- Instagram live acceptance: **MANUAL INSTAGRAM SIGN-OFF REQUIRED**. Use
  `docs/MANUAL_INSTAGRAM_ACCEPTANCE.md`; no automated customer messages were
  sent.
- Rollback commit: `c1aa23edeb9fb31e7d3e14d3a63821563e7eaa60` (verified runtime
  baseline); business-data rollback point:
  `d0f13058cbbf1bbb1f35db09ad82d9baf69a52ee`.
