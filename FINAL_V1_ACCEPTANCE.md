# Camellia Artisanal Chatbot v1.0 — Final Acceptance

Date: 2026-09-13  
Accepted production commit: `pending deployment of composite-intent fix`
Current deployed HEAD: `15458c202d956a7fdf9d35e0252705b1eb24ebde`
Verified production baseline: `c1aa23edeb9fb31e7d3e14d3a63821563e7eaa60`  
Business-data implementation: `d0f13058cbbf1bbb1f35db09ad82d9baf69a52ee`

## Release status

Automated acceptance and the production backup gate are complete. Manual
Instagram account testing remains with the account owner; no live customer
messages were sent by automation. The release is **READY FOR V1.0 — MANUAL
INSTAGRAM SIGN-OFF PENDING**. The remaining legacy
sourcing/ingredient/availability/operational claims are behind a versioned
content-integrity quarantine; they are omitted or answered with a neutral
confirmation request.

## Automated acceptance

- Full suite: **1,398 passed**, 0 failed, 1 deprecation warning.
- Deterministic 50-scenario pack: **50 passed, 0 failed**.
- Reel/Post/Ad, referential-intent, order/CAM and channel-policy tests:
  **162 passed**.
- Response Library/admin tests: **172 passed**.
- Instagram outbound audit/chunking/backup tests: **13 passed**.
- Unsupported active medical claims: **0**.
- Canonical pricing/menu/Bento/Jolbhora conflicts: **0**.
- Unconfirmed legacy business-claim templates: **quarantined** (0 emitted;
  see the post-quarantine section in the audit matrix).

The previous assisted-resolution failure was fixed in the test fixture: the
mock now accepts the sender's existing audit/correlation keyword arguments.
Production code was not weakened.

The final pre-release production bug fix adds deterministic conversational
opener parsing. A leading greeting is separated from its semantic remainder;
explicit product/order/human intent wins, then referential source context is
used only for a meaningful remainder and only when valid context exists. This
prevents `hi tell me more` from selecting stale legacy content while preserving
`hi` greeting bypass and later source-context follow-ups.

## Canonical data and safety

`data/menu.json` is authoritative for the confirmed Jolbhora matrix, pieces,
Bento tiers and approved customer claims. Versioned Response Library
overrides preserve provenance and make library, preview, dry-run and runtime
copies consistent. Medical/diabetic/low-glycemic claims remain excluded.

Production checks: three consecutive `/health` calls returned HTTP 200 with
`status=healthy`, `answering.ready=true`, live delivery, seed `20/20`, no
warnings, durable database, and a valid Instagram token. `ENABLE_WEB_TESTER=false`,
`LLM_AUTO_RESOLVE_ENABLED=false`, `WHATSAPP_CHATBOT_ENABLED=false`, and
`WHATSAPP_CHATBOT_ENABLE_CONFIRM=false`; WhatsApp remains HUMAN ONLY.

## Manual Instagram sign-off

The account owner must run the checklist in `docs/MANUAL_INSTAGRAM_ACCEPTANCE.md`
for greeting, Jolbhora pricing, configured reel follow-up, greeting bypass,
referential follow-up, explicit Kanchagolla override and human handoff.

## Backup and rollback

Production SQLite backup: **VERIFIED** via the authenticated application
backup endpoint. Path: `/app/var/camellia.db.backup-20260912_220436`;
timestamp: `2026-09-12 22:04:36 UTC`; size: `1,937,408` bytes;
`PRAGMA integrity_check`: `ok`. The live database remains
`/app/var/camellia.db`; the backup operation copies without deleting prior
backups. Railway volume PITR is not available on the current plan.

Rollback target: `d0f13058cbbf1bbb1f35db09ad82d9baf69a52ee` (business-data
implementation) or the previously verified `c1aa23edeb9fb31e7d3e14d3a63821563e7eaa60`
runtime baseline. Follow `V1_PRODUCTION_RUNBOOK.md`.

## Release blockers

1. Account owner must complete the manual Instagram checklist.
2. No content blocker remains from the audited legacy-claim set; any future
   business claim must be confirmed before its quarantine is removed.

## Architecture summary

FastAPI webhook service with durable SQLite state, deterministic template and
canonical-menu resolution, versioned Response Library overrides, Meta
Instagram outbound auditing/chunking, source routing for Reel/Post/Ad, safe
CAM/human handoff, and a hard WhatsApp human-only boundary. Auto Resolve is
simulation-only and sends no customer messages.
