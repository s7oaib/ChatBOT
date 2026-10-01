# Home Table Reel delivery completion (QA-M1.1)

The Home Table response keeps the approved TEXT / four LINK / TEXT order. All four existing URLs remain separate Instagram text messages. Native Reel media delivery (QA-M1B) is outside this change.

## Confirmed failure

Before changing code, production diagnostics showed Home Table details conversation 399 (2026-10-01 08:07:28 UTC) with two successful text sends (audits 705 and 706), and no Reel-link send attempts for that response. The following price conversation 400 likewise had no Reel links. The existing sender set `home_table_videos_sent` after queue creation, before any Meta acceptance. Historical queue/session details were not available through the old Admin diagnostics; the precise earlier attempt that set the flag was therefore not yet proven.

## Durable delivery

- Home Table LINK jobs carry `_response_delivery`: family `HOME_TABLE_REELS`, a conversation delivery UUID, the exact URL, and its one-based index. Existing job recipient, inbound job ID and conversation ID provide correlation.
- `home_table_reel_receipts` records each accepted URL against recipient and delivery UUID, with Meta message ID, job/conversation correlation and timestamp. Queue completion and receipt insertion share a transaction.
- A queued, blocked, failed, or unacknowledged send never creates a receipt. `home_table_videos_sent` becomes true only after receipts exist for all four exact URLs in the current conversation generation.
- Retry skips confirmed URLs, including duplicate queued answers. Receipts survive restart. A terminal Reel failure cancels the remaining blocks of that answer so later customer retries can reach only missing URLs.
- QA-C1 is checked before queueing and again before sending. Suppressed jobs create no receipt. A late job cannot complete a newer or expired conversation.
- Conversation expiration resets the delivery generation along with the existing flag. Receipt retention and customer erasure follow existing database policies.
- As with the existing durable queue, a process crash between Meta acceptance and the local receipt commit is an uncertain send; Meta does not offer this path an exactly-once transaction. Already committed receipts are never deliberately resent.

## Scoped production diagnostics and repair

Authenticated GET `/admin/home-table-reel-delivery?recipient_id=...` returns only this family's stored flag, receipt metadata and queue diagnostics. Customer text and raw recipient identity are excluded.

Authenticated POST `/admin/home-table-reel-delivery/repair` accepts the selected `recipient_id`. Use only after diagnostics establish a legacy false completion. It requires TEST_ONLY, current tester authorization, an unrepaired legacy true flag with no delivery UUID, and no in-flight Reel jobs. It locks/rechecks the stored state, creates a fresh generation, clears only the legacy completion flag, and commits an audit action `HOME_TABLE_REEL_COMPLETION_REPAIRED`. It neither changes the allowlist nor sends messages. A modern/partial/complete generation cannot be reset by this operation.

## Coverage

`tests/test_home_table_reel_delivery.py` covers enqueue versus each successful completion, partial failure, retry, restart, terminal failure recovery, tester removal before execution, duplicate queued answers, unacknowledged synchronous results, old-generation completion, authenticated one-time repair and refusal guards, and customer erasure. Existing ordered-response and live-output tests verify exact approved copy, URLs and order. Their success mocks return Meta message IDs rather than assuming a missing result proves delivery.

Release verification must include the impact-selected full regression, branch and main CI, exact deployed SHA, healthy startup/HTTP checks, unchanged TEST_ONLY/allowlist/configuration/Approved QA, and a real allowed-tester Home Table request followed by price continuation.
