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
