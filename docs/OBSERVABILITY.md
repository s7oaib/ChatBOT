# Production observability

Camellia uses the existing `/health`, authenticated Admin endpoints, durable
audit tables, and structured application logs. There is no second monitoring
framework to configure.

## Health endpoint

`GET /health` is the uptime target and returns HTTP 200 for a healthy service
or HTTP 503 when the bot cannot answer or live delivery is broken. The response
is safe for operator use and includes:

- `answering`: template/menu readiness and seed reconciliation state;
- `storage`: expected durability (no public filesystem paths);
- `reply_delivery`: live/mock configuration and delivery readiness;
- `webhook_signature`: signature enforcement state;
- `outbound_delivery`: consecutive failure counts and alerting state;
- `instagram_token`: cached state without token, account identity or raw provider message;
- `instagram_outbound`: delivery audit summary and failure classes;
- `ai`: provider/model, last safe call status and cooldown state;
- `reply_latency`: last/average latency and slow threshold;
- `auto_resolve` and `language_understanding`: enabled/simulation state; and
- `session_state` and retention configuration.

The `human_escalation_notifications` health block reports `SKIPPED` because
external staff notifications are intentionally manual-only; there is no
WhatsApp notification gate or server-side sender.

The normal production policy is Instagram AI enabled, WhatsApp automation
removed with manual handoff available,
web tester off, webhook signatures enforced, Story auto-sync off, and LLM Auto
Resolve off. Confirm the actual environment through `/health` and the
authenticated Admin evaluation endpoints; repository defaults alone cannot
prove Railway environment settings.

## Authenticated operator surfaces

`GET /admin/operations` is authenticated and read-only. Settings / Health uses
it to display latest-100 response paths/decision lanes, handoff lifecycle,
candidate statuses, review failures and recent menu versions. Counts are
explicitly samples, not all-time totals. Pre-instrumentation responses are
OTHER_PRE_INSTRUMENTATION, never guessed from text/template names. New selected
paths are stored in existing route-audit actions; no new schema or telemetry table.
Seven fixed SELECTs are bounded by LIMIT/primary-key or existing menu-key order.
Groq diagnostics are bounded to 50 events, counters explicitly since process
start, with UTC time, operation, outcome, latency and fallback indicator.
They contain no prompts, returned text, credentials or customer identifiers.
Full Auto Resolve readiness evaluation stays on its authenticated endpoint;
public health reports ADMIN_EVALUATION_REQUIRED rather than loading all history.
Settings reports local backup artifacts as unverified, not scheduled/verified
disaster protection. Off-host/schedule status remains Unknown.

Public health retains 200 during optional provider degradation, and 503 for
critical readiness failure. Paths and raw provider error detail are removed.

## Retention inventory

Existing DATA_RETENTION_DAYS controls startup purge of conversations,
escalations/notification rows, sessions and inbound/outbound jobs; zero disables
that existing purge. Existing session TTL remains separate and unchanged.
Candidate/handoff history, route/RAG audit, menu versions/source assets/review
drafts, backup artifacts and Railway log retention have no repository-enforced
approved duration: RETENTION_POLICY_UNDEFINED. Phase 2F adds no deletion policy.
Temporary test/restore files use disposable OS test directories only.

Use the existing Admin endpoints for detailed state:

- `/admin/instagram-outbound-audit` for Meta delivery status, HTTP status,
  safe error classes, trace IDs, and latency;
- `/admin/instagram-topics` and `/admin/instagram-routes` for Topic/source
  catalogue and exact mapping state;
- `/admin/instagram-story-sources` for Story lifecycle and mapping state;
- `/admin/escalations`, handoff views, and notification evaluation for human
  escalation and outbox state;
- `/admin/auto-resolve/evaluation` and assisted-resolution evaluation for
  simulation/read-only state; and
- `/admin/db-backup` for an authenticated, integrity-checked SQLite snapshot.

The normal human handoff records a durable outbox event as `SKIPPED`. This is
an intentional manual-only policy state, not a provider failure or a failed
CAM handoff. No WhatsApp credential, webhook, or Cloud API request is used.

## Log safety contract

Normal production logs may contain event shape, booleans, counts, route/template
identifiers, HTTP status, safe error classes, hashed identifiers, and bounded
correlation/trace IDs. They must not contain customer message bodies, raw Meta
customer identifiers, access tokens, webhook signatures, expected HMAC values,
or raw Meta/Groq provider response bodies.

When an incident requires more evidence, preserve the safe correlation ID and
the authenticated audit row. Do not temporarily enable raw-payload logging.

Application loggers additionally redact configured credentials and representative
Bearer/key/password/cookie/session/email/phone shapes. Exception logging keeps
class rather than arbitrary exception payloads. This is defence in depth, not
universal arbitrary-prose sanitization; structured telemetry must never accept
customer text. Route logs no longer emit entity dictionaries. Access logs remain
disabled to avoid webhook verification tokens in query strings.

## Operator review cadence

Review `/health` and recent safe logs after deployment, after a provider
incident, and during production soak. Investigate repeated outbound failures,
HTTP 429/5xx, latency degradation, notification retry growth, database
durability warnings, or an unexpected reappearance of a WhatsApp route or
sender. A single harmless dependency warning is not a production incident.
