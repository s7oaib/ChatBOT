# Instagram chatbot control

The global Admin card sits directly below the Admin header and above navigation, remaining visible across tabs after authentication. ON means LIVE; OFF means TEST ACCOUNTS ONLY. First initialization preserves production behavior with LIVE and never overwrites an operator mode on restart. No configuration changes or redeploy are required.

Three isolated additive tables store durable mode, enabled exact Meta sender IDs, and operator audit history: instagram_chatbot_control, instagram_chatbot_test_accounts, instagram_chatbot_control_audit. Recent Accounts reuses instagram_customer_identities; blocked DMs update identity metadata only, never customer message content or conversation state. Username labels do not grant access except when explicitly resolved by an Admin to one unique stored identity.

TEST_ONLY rejects non-allowlisted senders before routing, sessions, responder, orders, handoffs or inbound queuing. Central processing and bot reply boundaries check again, suppressing previously queued work after mode changes. Human Admin replies remain independent. Echoes and receipt events retain existing handling.

Temporary storage read failures retain the last successfully read mode and allowlist; cold failures allow nobody. Write failures leave durable state and cache unchanged. Writes use existing Admin authentication and CSRF. At most 20 test accounts may be enabled; zero testers in TEST_ONLY intentionally silences all bot responses.

Accepted production at 2 October 2026 is TEST_ONLY with two testers.
`INSTAGRAM_CHATBOT_ENABLED` is channel capability; durable LIVE/TEST_ONLY is
the independent access control. It must not be reset by restart/deploy.
Production verification is read-only: confirm TEST_ONLY, tester count, Recent Accounts, audit, exact deployed SHA, successful startup and health. Do not switch mode or add customers solely for smoke testing. QA-M1B, credentials, Meta configuration, Railway variables, approved answers, Bento validity, ordering and manual WhatsApp policy remain outside this feature.
