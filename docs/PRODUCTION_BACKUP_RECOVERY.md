# Camellia Artisanal production backup and recovery

This procedure describes the recovery boundary that is implemented in the
repository. It does not claim that Railway snapshots, point-in-time recovery,
or an off-host backup schedule is enabled. Those settings require operator
verification in Railway.

## Storage contract

The production deployment contract is:

- SQLite database: `/app/var/camellia.db`
- Persistent Railway volume mount: `/app/var`
- Seed/runtime files that are intended to persist: `menu.json`,
  `qa_templates.json`, and runtime uploads under the same durable storage
- Code rollback and database rollback are separate operations

The committed `railway.json` starts the service and configures restart policy;
it does not prove that a Railway volume is attached. Confirm the live volume
and the `DB_PATH` environment value in Railway before relying on persistence.

## What the application backup verifies

The authenticated `POST /admin/db-backup` endpoint is implemented in
`camellia/app.py`. It:

1. checks admin authentication;
2. checkpoints SQLite WAL when possible;
3. copies the live database to a timestamped sibling such as
   `/app/var/camellia.db.backup-YYYYMMDD_HHMMSS`;
4. runs `PRAGMA integrity_check` against the copy; and
5. returns the source path, backup path, size, integrity result, and a boolean
   `verified` result.

The endpoint does not delete older backups and does not copy the backup off the
Railway volume. A returned `verified=true` proves application-level SQLite
integrity for that copy, not independent disaster protection.

## State included in a database backup

The SQLite snapshot contains the durable application state, including, where
present:

- Instagram Topics and their active/inactive state;
- Instagram Story/Post/Reel/Ad source routes and exact Meta identifiers;
- menu and Topic associations;
- customer conversation/session state;
- human handoffs and escalation state; and
- notification outbox rows and retry state.

The database snapshot does not replace the deployment code, environment
secrets, or an external copy of uploaded files. Never put secrets in this
document or in a backup filename.

## Pre-migration procedure

Before a schema or data migration:

1. Record the current deployment ID, production SHA, and `/health` result.
2. Confirm the change has an explicit production approval plan.
3. Call the authenticated backup endpoint.
4. Record `backup_path`, `size_bytes`, `integrity_check`, and `verified`.
5. Confirm the backup file remains on the durable volume.
6. If disaster recovery requires protection from volume loss, have an operator
   copy the verified backup to an approved external location.

Do not wipe the database, replace it with a stale full copy, or run a restore
as a debugging shortcut.

## Recovery procedures

### Bad deploy, healthy database

1. Disable Auto Resolve/Assisted Resolution if the incident involves replies.
2. Record health, deployment, and safe correlation evidence.
3. Roll back code to the approved rollback reference:
   `0ced6ed4050a2c295e25d4c2bc64dd1cbaec7804`.
4. Do not roll back or overwrite the database automatically.
5. Verify health, webhook signature enforcement, Instagram routing, and the
   WhatsApp human-only policy.

### Bad schema/data migration

This is a destructive operator action and requires explicit approval.

1. Stop or isolate the service so new writes do not race the restore.
2. Preserve the current database before changing it.
3. Select a verified backup whose timestamp and schema are appropriate.
4. Replace the live database with the selected backup using an operator-owned,
   auditable procedure.
5. Restart the service and verify SQLite integrity, Topics, source mappings,
   handoffs, outbox state, and business/menu configuration.
6. Record that code and data were restored independently.

### Railway volume failure

The repository cannot verify or repair Railway volume loss. The operator must
verify whether a Railway snapshot or external backup exists, restore the
volume/database through the approved Railway procedure, and then run the
application verification checklist. If no verified copy exists, stop and
escalate; do not seed a new empty production database.

### Meta credential failure

Do not restore a database for a token failure. Verify the environment secret,
Meta account/page identity, webhook signature configuration, and `/health`.
Credential rotation is an explicit operator gate.

### Groq/provider failure

Do not restore a database for an AI provider outage. Keep deterministic and
canonical responses available, keep Auto Resolve off, and inspect provider
status/HTTP error classes with privacy-safe logs. Retries must remain bounded.

## Verification status

| Item | Status | Evidence or required operator check |
|---|---|---|
| `/app/var/camellia.db` application path | Implemented in deployment contract | Verify live `DB_PATH` in Railway |
| Timestamped SQLite backup endpoint | Verified in code | Run authenticated endpoint when approved |
| SQLite integrity check of backup | Verified in code | Confirm returned `integrity_check=ok` |
| Topic/source/handoff/outbox persistence | Covered by database schema and tests | Verify rows after any live restore |
| Railway persistent volume attached | Requires operator verification | Railway service settings |
| Railway backup/PITR schedule | Requires operator verification | Railway plan/service settings |
| Off-host backup retention | Requires operator verification | Approved storage/audit record |
| Production restore | Not performed | Explicit approval required |
