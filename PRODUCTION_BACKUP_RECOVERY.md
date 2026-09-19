# Camellia Production Backup & Recovery Runbook

Status: maintenance documentation only. This file does not enable, create, restore, or deploy backups.

## Frozen references

- Accepted production SHA: `75642a2ef763bcf46e214487f5e6ded1538b8cdc`
- Frozen release branch: `release/production-freeze-2026-09-19`
- Railway production deployment: `7855239b-1116-4d0c-a98a-cfe48b1cd110`
- Code rollback reference: `0ced6ed4050a2c295e25d4c2bc64dd1cbaec7804`
- Production database path: `/app/var/camellia.db`
- Railway volume mount: `/app/var`

## Recovery principle

Code rollback and data rollback are separate operations.

Reverting application code does not restore SQLite state. Restoring a Railway volume does not change the Git commit by itself. During an incident, determine whether the defect is in code, configuration, provider state, or persistent data before changing either one.

## Railway volume backups

Railway's volume backup feature covers arbitrary files stored in a mounted volume, including SQLite databases. Railway currently supports manual backups and Daily, Weekly, and Monthly schedules.

Documented retention:

- Daily: every 24 hours, retained for 6 days.
- Weekly: every 7 days, retained for 27 days.
- Monthly: every 30 days, retained for 89 days.

Recommended production policy, subject to account/cost approval:

- Daily + Weekly scheduled volume backups.
- Monthly backup if longer recovery history is desired.
- Manual backup immediately before any approved destructive schema/data migration.

The current connector does not expose backup schedule state, so this repository must not claim that any schedule is enabled unless verified in Railway's Service > Backups UI.

Railway documentation: https://docs.railway.com/volumes/backups

## Before a destructive data change

1. Confirm the active production commit and Railway deployment.
2. Confirm `/app/var` is mounted and `DB_PATH` resolves to `/app/var/camellia.db`.
3. Verify a recent usable Railway volume backup exists in the production service Backups tab.
4. If required, create a manual backup from Railway before the change.
5. Record the backup timestamp and the intended code rollback SHA.
6. Do not wipe, replace, or detach the production volume as a debugging shortcut.
7. Run the migration only after explicit production approval.

## Railway restore behavior

Railway restores a selected volume backup by staging a replacement volume at the same mount point. The previous volume is retained but unmounted. The staged restore must be reviewed and deployed before it becomes active.

Backups newer than the selected restore point remain attached to the previous volume; they are not copied onto the restored volume. Railway documents that backups can only be restored inside the same project and environment.

A volume restore can therefore change persistent data and trigger a service redeploy. Treat restore as a production mutation requiring explicit approval.

## Restore procedure

Use only when a data rollback is actually required.

1. Identify the incident start time and the last known-good data state.
2. Identify the matching Railway backup in Service > Backups.
3. Record the currently mounted volume ID and current production SHA.
4. Select Restore on the chosen backup.
5. Review Railway's staged changes. Do not deploy yet if the wrong mount, environment, or timestamp is shown.
6. Confirm the staged replacement volume mounts at `/app/var`.
7. Confirm the code version that should run against the restored database.
8. Obtain explicit production approval.
9. Deploy the staged restore.
10. Verify application startup, `/health`, persistent database access, Instagram account guard, webhook-signature enforcement, channel policies, and critical deterministic responses.
11. Verify business configuration that lives in SQLite, including Instagram Topics/source mappings, human handoff state where relevant, and menu/topic associations.
12. If validation fails, stop customer-impacting changes and reassess before another restore or code rollback.

## Post-restore acceptance checks

Minimum checks:

- Service starts successfully.
- `/health` returns HTTP 200.
- `/app/var` volume is mounted.
- Instagram intended-account guard remains active.
- Foreign-account webhooks are ignored safely.
- Webhook signatures remain enforced.
- Instagram AI remains enabled.
- WhatsApp general AI remains disabled/human-only.
- Story Auto Sync remains OFF unless separately approved.
- LLM Auto Resolve remains OFF.
- Opening-hours deterministic answer remains exactly:
  `Our kitchen is open 10:00 AM – 7:30 PM every day.`
- No locked business facts changed.
- No unexpected duplicate handoffs or notification retry storms appear.

## SQLite consistency note

A Railway volume snapshot protects the volume as an infrastructure recovery point. The repository currently has no dedicated application-level SQLite online-backup utility. Do not add one speculatively.

If operational evidence later shows a need for application-consistent export/restore beyond Railway snapshots, implement it as a separate maintenance change using SQLite's supported backup mechanisms, with focused tests and an explicit production procedure.

## Volume safety caveats

Railway documents these caveats:

- Manual backup size is limited relative to volume capacity.
- Wiping a volume deletes its backups.
- Restore is limited to the same project and environment.
- A restore stages a replacement volume and requires deployment to apply.

Never wipe the production volume to solve an application defect.

## Code-only rollback

Use a code rollback only when persistent data is known to be compatible and the incident is caused by application code.

Reference rollback SHA:

`0ced6ed4050a2c295e25d4c2bc64dd1cbaec7804`

Before rolling back, verify that no intervening database migration makes the older code incompatible with current data. If compatibility is uncertain, treat the incident as a combined code/data recovery problem instead of assuming code rollback is safe.

## Healthy-state rule

If production is healthy, no security/privacy incident exists, and there is no approved data maintenance requirement, do not restore or modify the production volume.

The correct action is:

`NO CHANGE REQUIRED`
