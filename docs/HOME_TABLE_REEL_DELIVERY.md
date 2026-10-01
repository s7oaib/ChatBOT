# Home Table Reel delivery completion (QA-M1.1)

The Home Table response keeps the approved TEXT / four LINK / TEXT order. Current QA-M1B sends verified native media cards with canonical URL fallback; see the current native section below. Earlier URL-only evidence is historical.

## Confirmed failure

Before changing code, production diagnostics showed Home Table details conversation 399 (2026-10-01 08:07:28 UTC) with two successful text sends (audits 705 and 706), and no Reel-link send attempts for that response. The following price conversation 400 likewise had no Reel links. The existing sender set `home_table_videos_sent` after queue creation, before any Meta acceptance. Historical queue/session details were not available through the old Admin diagnostics; the precise earlier attempt that set the flag was therefore not yet proven.

## Durable delivery

- Home Table LINK jobs carry `_response_delivery`: family `HOME_TABLE_REELS`, a deterministic inbound-request delivery ID, the exact URL, and its one-based index. Existing job recipient, inbound job ID and conversation ID provide correlation.
- `home_table_reel_receipts` records each accepted URL against recipient and delivery ID, with Meta message ID, job/conversation correlation and timestamp. Queue completion and receipt insertion share a transaction.
- A queued, blocked, failed, or unacknowledged send never creates a receipt. `home_table_videos_sent` becomes true only after receipts exist for all four exact URLs in the current conversation generation.
- Retry skips confirmed URLs, including duplicate queued answers. Receipts survive restart. A terminal Reel failure cancels the remaining blocks of that answer so later independent requests can start a new full delivery.
- QA-C1 is checked before queueing and again before sending. Suppressed jobs create no receipt. A late job cannot complete a newer or expired conversation.
- Conversation expiration resets the delivery generation along with the existing flag. Receipt retention and customer erasure follow existing database policies.
- As with the existing durable queue, a process crash between Meta acceptance and the local receipt commit is an uncertain send; Meta does not offer this path an exactly-once transaction. Already committed receipts are never deliberately resent.

## Scoped production diagnostics and repair

Authenticated GET `/admin/home-table-reel-delivery?recipient_id=...` returns only this family's stored flag, receipt metadata and queue diagnostics. Customer text and raw recipient identity are excluded.

Authenticated POST `/admin/home-table-reel-delivery/repair` accepts the selected `recipient_id`. Use only after diagnostics establish a legacy false completion. It requires TEST_ONLY, current tester authorization, an unrepaired legacy true flag with no delivery UUID, and no in-flight Reel jobs. It locks/rechecks the stored state, creates a fresh generation, clears only the legacy completion flag, and commits an audit action `HOME_TABLE_REEL_COMPLETION_REPAIRED`. It neither changes the allowlist nor sends messages. A modern/partial/complete generation cannot be reset by this operation.

## Coverage

`tests/test_home_table_reel_delivery.py` covers enqueue versus each successful completion, partial failure, retry, restart, terminal failure recovery, tester removal before execution, duplicate queued answers, unacknowledged synchronous results, old-generation completion, authenticated one-time repair and refusal guards, and customer erasure. Existing ordered-response and live-output tests verify exact approved copy, URLs and order. Their success mocks return Meta message IDs rather than assuming a missing result proves delivery.

Release verification must include the impact-selected full regression, branch and main CI, exact deployed SHA, healthy startup/HTTP checks, unchanged TEST_ONLY/allowlist/configuration/Approved QA, and a real allowed-tester Home Table request followed by price continuation.


## QA-SM1.1: per-request delivery

Session-wide suppression is retired. Every new response containing the exact video anchor gets TEXT / four LINK / TEXT, despite earlier completion. Price/payment continuation without the anchor gets no invented links. The flag describes only the latest delivery.

Delivery identity hashes recipient, durable inbound job identity and HOME_TABLE_REELS. Rebuilding the same job retains its generation and original queue positions; unique inbound-job/sequence and durable receipts prevent confirmed sends from repeating. A new Meta message gets an independent generation. Direct synchronous callers may supply a stable inbound_message_id; an unidentified call represents a new request. No session reset, repair or TTL wait is required. Native MEDIA_SHARE is now implemented for the verified Camellia account; see QA-M1B below.

## QA-M1B: verified native delivery — 2 October 2026

Accepted runtime `4071402099390dafc28c22c8c2a35920e385d470`, production deployment `709ed586-3b7a-4137-9de7-d0cd0667e9c1`. Full CI
[36938786442](https://github.com/s7oaib/ChatBOT/actions/runs/36938786442)
passed 3432 tests, zero failed, one existing warning on candidate
`72150044d5222b279170bf656021a154076dc59d`; merge tree equality is verified.
Allowed-tester production smoke confirmed opening approved text, four native
cards in order, remaining text, four durable receipts, and a second new request
with four cards again. Same-event dedup and partial/restart retry are covered by
focused durable-job tests; no unsafe production replay or session repair was used.

Verified against authenticated `@camellia.artisanal` professional account
`17841455615744812`, Instagram Login at graph.instagram.com/v26.0:

| Canonical Reel shortcode | Exact owned media ID |
| --- | --- |
| DdJhXQMT4VZ | 18073790945717461 |
| Dcd5bbEzbCo | 18191102770349496 |
| DcLZpuQzCKE | 18158439172491979 |
| DcQYp72zf8D | 17908289592288134 |

All four were unique exact permalink matches in the account's own media edge
(six pages), VIDEO / REELS. No caption, OCR, visual or timestamp inference.
Reel #1 official MEDIA_SHARE was accepted with a Meta message ID and the tester
confirmed a native card before implementation. Native sends use the verified
account endpoint and exact static mapping, not per-message account enumeration.

The common sender preserves TEXT / Reel 1 / Reel 2 / Reel 3 / Reel 4 / TEXT.
Normal Approved Q&A and qualifying mapped sources use this same mechanism.
Canonical URLs remain fallback: only an explicit native-type unavailability
rejection permits a URL send. Timeout, unrelated recipient error, 5xx or missing
acceptance ID does not immediately send a URL duplicate. Existing durable retry
and uncertain-send limitations remain; Meta does not offer exactly-once storage
transactions. No session-wide suppression was reintroduced.

The existing receipts table has one additive delivery_metadata column containing
verified media ID, one-based link index and MEDIA_SHARE / URL_FALLBACK mode.
Canonical URL, recipient/generation, Meta message ID, job/conversation identity
and acceptance timestamp remain in the same durable receipts. Historical rows
are preserved; a null metadata field does not claim native acceptance.

TEST_ONLY remains active. An explicitly authorized Admin correction added the
observed real-account sender `2585…6743` for existing tester @x0s7o_aib0x after
Graph identity verification; both older entries remain. No username-only grant,
configuration change, credentials disclosure, menu or Approved Q&A change occurred.
The original handover tag remains immutable. Baseline points to the accepted
runtime, not the later metadata documentation merge.
