# Camellia Artisanal Chatbot v1.0 — Final Acceptance

Date: 2026-09-13  
Verified production baseline: `c1aa23edeb9fb31e7d3e14d3a63821563e7eaa60`  
Business-data implementation: `d0f13058cbbf1bbb1f35db09ad82d9baf69a52ee`

## Release status

Automated acceptance is complete. Manual Instagram account testing remains
with the account owner; no live customer messages were sent by automation.
The release is **NOT READY — BLOCKERS REMAIN** until the account owner signs
off the live Instagram checklist, a production SQLite backup is created, and
the remaining legacy sourcing/ingredient/availability/operational claims are
either confirmed or moved behind human-confirmation handling.

## Automated acceptance

- Full suite: **1,382 passed**, 1 deprecation warning.
- Deterministic 50-scenario pack: **50 passed, 0 failed**.
- Reel/Post/Ad, referential-intent, order/CAM and channel-policy tests:
  **162 passed**.
- Response Library/admin tests: **172 passed**.
- Instagram outbound audit/chunking/backup tests: **13 passed**.
- Unsupported active medical claims: **0**.
- Canonical pricing/menu/Bento/Jolbhora conflicts: **0**.
- Unconfirmed legacy business-claim templates: **present** (not silently
  rewritten; see the audit matrix and `TEMPLATE_CLIENT_CONFIRMATIONS.md`).

The previous assisted-resolution failure was fixed in the test fixture: the
mock now accepts the sender's existing audit/correlation keyword arguments.
Production code was not weakened.

## Canonical data and safety

`data/menu.json` is authoritative for the confirmed Jolbhora matrix, pieces,
Bento tiers and approved customer claims. Versioned Response Library
overrides preserve provenance and make library, preview, dry-run and runtime
copies consistent. Medical/diabetic/low-glycemic claims remain excluded.

Production checks: `/health` returned HTTP 200 with `status=healthy`,
`answering.ready=true`, seed `20/20`, no warnings, durable database, and a
valid Instagram token. `ENABLE_WEB_TESTER=false`,
`LLM_AUTO_RESOLVE_ENABLED=false`, `WHATSAPP_CHATBOT_ENABLED=false`, and
`WHATSAPP_CHATBOT_ENABLE_CONFIRM=false`; WhatsApp remains HUMAN ONLY.

## Manual Instagram sign-off

The account owner must run the checklist in `docs/MANUAL_INSTAGRAM_ACCEPTANCE.md`
for greeting, Jolbhora pricing, configured reel follow-up, greeting bypass,
referential follow-up, explicit Kanchagolla override and human handoff.

## Backup and rollback

Production SQLite backup: **PENDING OPERATOR ACTION**. Railway's current plan
does not expose volume backups/PITR, and the authenticated application backup
endpoint requires the production admin credential. Do not treat a local test
database copy as a production backup.

Rollback target: `d0f13058cbbf1bbb1f35db09ad82d9baf69a52ee` (business-data
implementation) or the previously verified `c1aa23edeb9fb31e7d3e14d3a63821563e7eaa60`
runtime baseline. Follow `V1_PRODUCTION_RUNBOOK.md`.

## Release blockers

1. Account owner must complete the manual Instagram checklist.
2. An authenticated operator must create and verify the production SQLite
   backup; Railway PITR is unavailable on the current plan.
3. Confirm or safely quarantine the remaining legacy sourcing, ingredient,
   availability and operational claims before declaring customer-facing
   content fully approved.

## Architecture summary

FastAPI webhook service with durable SQLite state, deterministic template and
canonical-menu resolution, versioned Response Library overrides, Meta
Instagram outbound auditing/chunking, source routing for Reel/Post/Ad, safe
CAM/human handoff, and a hard WhatsApp human-only boundary. Auto Resolve is
simulation-only and sends no customer messages.
