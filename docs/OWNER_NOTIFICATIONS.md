# Owner notifications

Owner notification is additive to the accepted customer architecture:

`Instagram AI → durable CAM handoff → customer receives wa.me → human continues on WhatsApp`

WhatsApp automation remains removed. A newly-created CAM handoff creates one
parent row in `human_notification_events`, then idempotently creates three
delivery rows in `notification_deliveries`: `EMAIL`, `WEB_PUSH`, and `ADMIN`.
The Admin channel is durable in-app state; the other two are provider
deliveries. Reused active CAM handoffs do not create another parent event.

## Channel states

Each delivery uses `SENT`, `FAILED`, `RETRYING`, or `SKIPPED`. Temporary email
or push failures use bounded exponential retry through the existing parent
outbox. A missing configuration is `SKIPPED`, not a handoff failure. The CAM
row is created and remains `WAITING_FOR_HUMAN` regardless of provider state.

## Configuration

Email is intentionally off until the client's approved infrastructure is
provisioned:

```text
ADMIN_EMAIL_PROVIDER=smtp
ADMIN_EMAIL_FROM=approved-sender@example.com
ADMIN_RECOVERY_EMAIL=approved-owner@example.com
ADMIN_EMAIL_SMTP_HOST=smtp.example.com
ADMIN_EMAIL_SMTP_PORT=587
ADMIN_EMAIL_SMTP_USERNAME=...
ADMIN_EMAIL_SMTP_PASSWORD=...
ADMIN_EMAIL_SMTP_USE_TLS=true
```

`HUMAN_ESCALATION_EMAIL` is retained as a compatibility fallback for the
recipient. `ADMIN_EMAIL_API_KEY` is read only for forward-compatible provider
selection; this build does not guess or call an unknown API provider.

Web Push requires all of the following in the deployment secret store:

```text
WEB_PUSH_ENABLED=true
WEB_PUSH_VAPID_PUBLIC_KEY=<public key>
WEB_PUSH_VAPID_PRIVATE_KEY=<private key>
WEB_PUSH_VAPID_SUBJECT=mailto:approved-owner@example.com
```

Do not commit, print, or paste the private key into chat. If any VAPID value is
missing, the Admin opt-in control remains disabled and `/health` reports the
safe blocker. The public key is returned only to an authenticated Admin page.

## Additive SQLite migration

Startup creates, if absent, `notification_deliveries`,
`admin_notification_states`, and `web_push_subscriptions`, and adds nullable
`handoff_ref` plus `payload_version` columns to the existing
`human_notification_events` table. It also creates due/active indexes. The
migration is idempotent and does not delete, rewrite, or archive existing
conversation, escalation, CAM, menu, routing, or session rows.

## Verification

After a controlled Instagram handoff, check `/health`, the authenticated Admin
notification list, and the durable SQLite outbox. There must be one CAM ref,
one parent event, one Admin state, and at most one delivery attempt per
configured channel. Repeat the same customer message: the CAM ref and
notification counts must remain unchanged. Confirm the CAM remains
`WAITING_FOR_HUMAN` until an operator explicitly resumes it.
