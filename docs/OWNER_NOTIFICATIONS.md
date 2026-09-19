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

Email uses the explicitly selected provider when these variables are
provisioned; otherwise it remains safely skipped. The existing `smtp` and
`resend` adapters remain available, but production should select exactly one:

```text
ADMIN_EMAIL_PROVIDER=resend
ADMIN_EMAIL_FROM=approved-sender@example.com
ADMIN_RECOVERY_EMAIL=approved-owner@example.com
ADMIN_EMAIL_SMTP_HOST=smtp.example.com
ADMIN_EMAIL_SMTP_PORT=587
ADMIN_EMAIL_SMTP_USERNAME=...
ADMIN_EMAIL_SMTP_PASSWORD=...
ADMIN_EMAIL_SMTP_USE_TLS=true
```

For the existing Resend setup, `ADMIN_EMAIL_API_KEY` is used with the Resend
API and the SMTP settings are ignored. For SMTP, set
`ADMIN_EMAIL_PROVIDER=smtp` and provision the SMTP host/credentials.

```text
ADMIN_EMAIL_API_KEY=approved-provider-secret
```

`HUMAN_ESCALATION_EMAIL` is retained as a compatibility fallback for the
recipient. The Gmail API adapter uses HTTPS only; it never opens an SMTP
socket and does not require domain DNS verification.

### Gmail API over HTTPS runbook

Use this path when Railway SMTP egress is unavailable. The application uses a
normal Gmail or Google Workspace mailbox with OAuth 2.0 refresh tokens; it does
not use service accounts or domain-wide delegation.

1. In [Google Cloud Console](https://console.cloud.google.com/), create or
   select the production Google Cloud project for Camellia.
2. Enable **Gmail API** for that project.
3. Configure the **OAuth consent screen**. Use **Internal** only when the
   mailbox is in the same Google Workspace organization and the organization
   allows it; otherwise use **External**. For an External app in **Testing**,
   add the mailbox as a test user. Google limits Testing apps to 100 test users
   and refresh tokens issued to test users expire after seven days, so use
   **In production**/complete the required verification before relying on it
   for a long-lived production mailbox.
4. Create **Credentials → OAuth client ID**. A **Desktop app** client is
   acceptable for the one-time operator token acquisition; use the existing
   Web application flow instead if this repository already has one.
5. Request only this scope:
   `https://www.googleapis.com/auth/gmail.send`.
6. Run a one-time offline authorization flow for the owner mailbox with
   `access_type=offline` and `prompt=consent`, then securely capture the
   returned refresh token. The operator may use Google's OAuth Playground or a
   local helper; never send the authorization code or token through chat.
7. Add the resulting values directly to Railway for the `ChatBOT` service:

   ```text
   ADMIN_EMAIL_PROVIDER=gmail_api
   GMAIL_API_CLIENT_ID=<OAuth client ID>
   GMAIL_API_CLIENT_SECRET=<OAuth client secret>
   GMAIL_API_REFRESH_TOKEN=<offline refresh token>
   ADMIN_EMAIL_FROM=<approved sender mailbox>
   ADMIN_RECOVERY_EMAIL=<owner alert recipient>
   GMAIL_API_USER_ID=me
   ```

   `GMAIL_API_USER_ID=me` is optional because `me` is the default.
8. Never commit these values or paste them into chat, tickets, public logs, or
   health responses. If a refresh token is revoked or expires, repeat the
   offline-consent flow and replace only the Railway secret.

The send path first exchanges the refresh token at Google's OAuth token
endpoint, then sends an RFC 2822 MIME message to Gmail's
`users.messages.send` endpoint with base64url `raw` content. Token exchange
and Gmail API network timeouts, HTTP 408/429, and 5xx responses are retried by
the existing durable outbox. OAuth configuration/authentication errors and
other 4xx responses are terminal for that delivery but never block CAM
creation or change `WAITING_FOR_HUMAN`.

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
