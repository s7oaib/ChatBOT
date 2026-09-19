# Camellia Artisanal client handover

This is the operational handover for the accepted production baseline. It
contains no credentials. Secrets and environment values belong in Railway's
secret manager or the approved local development environment.

## 1. Architecture

Camellia is a FastAPI webhook service with durable SQLite state on the Railway
volume. Instagram is the AI chatbot channel. WhatsApp is human-only except for
the already approved, narrowly scoped continuation of an Instagram handoff.

Customer response order is:

1. deterministic and canonical approved knowledge;
2. structured understanding where needed;
3. controlled semantic/RAG/provider assistance;
4. strict business-fact and content-integrity guardrails; and
5. durable human escalation when the request is unsupported or sensitive.

Provider assistance is never the source of truth. If Groq is unavailable,
approved deterministic responses and safe escalation continue.

## 2. Production baseline

- Accepted production SHA:
  `75642a2ef763bcf46e214487f5e6ded1538b8cdc`
- Frozen release reference: `release/production-freeze-2026-09-19`
- Railway deployment: `7855239b-1116-4d0c-a98a-cfe48b1cd110`
- Approved rollback reference: `0ced6ed4050a2c295e25d4c2bc64dd1cbaec7804`
- Freeze record: GitHub Issue #27 — Production Freeze — 2026-09-19

The frozen release is not edited directly. Future work uses an isolated
`maintenance/`, `hotfix/`, or explicitly approved `feature/` branch.

## 3. Channel and safety policy

- Instagram AI: enabled.
- WhatsApp general AI: disabled; human-only.
- WhatsApp order continuation: only the approved narrow continuation path.
- Story Auto Sync: off; manual Admin sync remains available.
- LLM Auto Resolve: off; simulation/evaluation is not customer sending.
- Web Tester: off in production.
- Webhook signatures: enforced.
- Intended Instagram account guard: enforced when configured.

Do not change these settings, locked business facts, Meta configuration, or
credentials without explicit owner approval.

## 4. Models

- Structured NLU: `openai/gpt-oss-120b`.
- Main responder/refinement: `openai/gpt-oss-20b`.
- `groq/compound-mini` must not be reintroduced.

The models may classify or refine within approved context. They may not invent
prices, availability, delivery promises, ingredients, allergy guarantees,
shelf-life claims, or dispatch estimates.

## 5. Admin login and daily operation

Open the authenticated `/admin` portal over HTTPS. Browser login uses the
configured admin username/password hash and production MFA. Machine-only
Admin API calls use the separate `ADMIN_API_KEY`; the legacy `ADMIN_PIN` is
not a browser or API authentication path.

Never put a credential in a ticket, screenshot, URL, log, or repository file.

### Create or edit a Topic

1. Open Instagram Content Routing in Admin.
2. Choose `+ Add Topic`.
3. Enter the operator-approved Topic name and description.
4. Optionally choose an approved default menu, priority products, knowledge
   scope, and reply order.
5. Leave the Topic active only when it is ready for new mappings.
6. Save and confirm it appears in the active Topic selectors.

An empty Topic catalogue is valid on a new database. The portal explains that
the operator must create a Topic; it does not seed fake business Topics.
Inactive Topics remain historical records and are not offered for new
selectors.

### Map Story, Post, or Reel content

1. Use the captured exact Meta Story media ID for a Story; never type a guessed
   ID or route from OCR/caption/visual similarity.
2. For a Post or Reel, paste the canonical Instagram link and let the service
   normalize and verify its source identity.
3. Select an active Topic and choose the menu behavior.
4. Save the mapping and verify the exact source ID, Topic, route status, and
   approved preview.
5. For shared Posts without a usable shortcode, use the deliberate admin
   pairing flow and its short expiry window.

Existing mappings are persisted in SQLite and are independent copies of the
Topic defaults. Editing a Topic does not silently rewrite existing routes.

### Menu management

Use the authenticated Menu Manager to edit a draft, preview it, and publish
only after checking the approved content. Menu state is persisted under the
configured durable path, normally `/app/var/menu.json`. Do not insert prices
or claims from an unreviewed image; the image parser is not a business-fact
authority.

## 6. Human escalation workflow

Unsupported, sensitive, order/event, or explicit human requests create or
reuse an opaque durable CAM handoff reference. Repeated customer messages do
not create duplicate active handoffs. Staff notifications use the bounded
outbox retry path.

While a handoff is `WAITING_FOR_HUMAN`, autonomous AI replies are suppressed.
The operator must explicitly resume the handoff before chatbot behavior can
continue. A notification failure must not be represented to the customer as a
successful handoff.

## 7. Health verification

Check `GET /health` after a deploy, restart, provider incident, or environment
change. Verify:

- `status=healthy` and `warnings` are understood;
- durable storage points to `/app/var/camellia.db`;
- reply delivery is live when production delivery is expected;
- webhook signatures are enforced;
- Instagram token/account state is valid or explicitly being remediated;
- outbound delivery has no unexplained repeated failures;
- `ai` reports the intended provider/model and safe cooldown state;
- reply latency is within the configured threshold;
- `auto_resolve.actual_enabled=false`; and
- WhatsApp remains human-only.

Use authenticated Admin audit/evaluation endpoints for detailed outbound,
Topic/source, Story, handoff, notification, and AI-resolution state.

## 8. Backup and recovery

Follow [`PRODUCTION_BACKUP_RECOVERY.md`](PRODUCTION_BACKUP_RECOVERY.md). The
authenticated `POST /admin/db-backup` endpoint creates a timestamped SQLite
copy and checks `PRAGMA integrity_check`. It does not prove Railway snapshots
or off-host retention. Production restore and destructive data migration are
approval gates.

Code rollback and data rollback are separate. The approved code rollback is
`0ced6ed4050a2c295e25d4c2bc64dd1cbaec7804`; do not overwrite the live database
just because code was rolled back.

## 9. Privacy and security

Normal logs use safe event shape, counts, HTTP status, error classes, hashes,
and bounded correlation IDs. They must not contain customer message bodies,
raw Meta identifiers, tokens, webhook signatures, expected HMAC values, or raw
provider response bodies. Webhook signature verification and admin
authentication must remain enabled.

## 10. Troubleshooting

| Symptom | First checks | Safe action |
|---|---|---|
| Instagram replies fail | `/health`, Instagram token state, outbound audit, Meta HTTP status | Keep canonical routes available; renew credentials only through approval |
| Groq 429/5xx/timeout | `/health` AI state, safe error class, latency | Do not create retry storms; keep deterministic fallback and Auto Resolve off |
| Topic selector empty | Topic API/admin session, active Topic count, database durability | Create/configure a Topic; do not seed fake production data |
| Story routes incorrectly | Exact Story media ID, route status, source audit | Disable the bad mapping and preserve the exact ID evidence |
| Duplicate handoff | Handoff reference and outbox audit | Do not mint another reference; inspect durable deduplication state |
| Bot replies while waiting for human | Handoff state and resume audit | Stop autonomous paths, preserve evidence, investigate before deploy |
| Database warning | `storage` block and `/app/var` mount | Do not wipe data; follow backup/recovery procedure |
| WhatsApp AI reply observed | Channel-policy health/evaluation and environment flags | Treat as an incident; disable the path and seek approval before change |

## 11. Maintenance and change approval

Every change must document the issue, evidence, severity, root cause, branch,
base/candidate SHA, files, behavior changed/unchanged, focused and full tests,
CI, policy/fact/privacy/database impact, deployment need, rollback, residual
risk, and Git status.

The workflow is:

`observe → reproduce → minimum safe fix → focused tests → full suite → diff and policy review → CI → explicit production approval → deploy → live verification`

Green tests do not authorize production deployment. Never deploy, restore,
rotate credentials, change Meta configuration, enable Story Auto Sync, enable
Auto Resolve, enable WhatsApp general AI, or alter locked business facts
without explicit approval.
