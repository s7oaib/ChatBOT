# Production observability

Camellia uses the existing `/health`, authenticated Admin endpoints, durable
audit tables, and structured application logs. There is no second monitoring
framework to configure.

## Health endpoint

`GET /health` is the uptime target and returns HTTP 200 for a healthy service
or HTTP 503 when the bot cannot answer or live delivery is broken. The response
is safe for operator use and includes:

- `answering`: template/menu readiness and seed reconciliation state;
- `storage`: database/menu paths and expected durability;
- `reply_delivery`: live/mock configuration and delivery readiness;
- `webhook_signature`: signature enforcement state;
- `outbound_delivery`: consecutive failure counts and alerting state;
- `instagram_token`: cached Meta token/account status without the token;
- `instagram_outbound`: delivery audit summary and failure classes;
- `ai`: provider/model, last safe call status and cooldown state;
- `reply_latency`: last/average latency and slow threshold;
- `auto_resolve` and `language_understanding`: enabled/simulation state; and
- `session_state` and retention configuration.

The `human_escalation_notifications` health block reports whether the optional
external WhatsApp notification gate is enabled and documents `SKIPPED` as the
disabled status without exposing credentials.

The normal production policy is Instagram AI enabled, WhatsApp human-only,
web tester off, webhook signatures enforced, Story auto-sync off, and LLM Auto
Resolve off. Confirm the actual environment through `/health` and the
authenticated Admin evaluation endpoints; repository defaults alone cannot
prove Railway environment settings.

## Authenticated operator surfaces

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

The normal human handoff records a durable outbox event as `SKIPPED` when
`HUMAN_ESCALATION_WHATSAPP_API_ENABLED` is unset or false. This is an
intentional policy state, not a provider failure or a failed CAM handoff.

## Log safety contract

Normal production logs may contain event shape, booleans, counts, route/template
identifiers, HTTP status, safe error classes, hashed identifiers, and bounded
correlation/trace IDs. They must not contain customer message bodies, raw Meta
customer identifiers, access tokens, webhook signatures, expected HMAC values,
or raw Meta/Groq provider response bodies.

When an incident requires more evidence, preserve the safe correlation ID and
the authenticated audit row. Do not temporarily enable raw-payload logging.

## Operator review cadence

Review `/health` and recent safe logs after deployment, after a provider
incident, and during production soak. Investigate repeated outbound failures,
HTTP 429/5xx, latency degradation, notification retry growth, database
durability warnings, or unexpected channel-policy flags. A single harmless
dependency warning is not a production incident.
