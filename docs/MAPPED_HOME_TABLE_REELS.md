# Mapped Home Table source Reel links (QA-SM1)

An exact, active Story/Post/Reel source mapped to `Bengali Home Table`, whose operator Source Description contains `HOME_TABLE_VIDEO_ANCHOR`, uses the same ordered-block builder as Approved Q&A. Text is preserved exactly within the existing trimmed source-response contract; four canonical LINK blocks are inserted after the anchor; the sender presents them as verified native cards or safe URL fallback. The existing QA-M1.1 queue, per-link durable receipts, retry handling and TEST_ONLY guards remain authoritative. Current QA-M1B uses the same sender for native MEDIA_SHARE with safe canonical URL fallback; see [delivery provenance](HOME_TABLE_REEL_DELIVERY.md).

The source lane now supplies its own ordered blocks, and the Instagram send path consumes them. Other topics, descriptions without the anchor, topic-description fallback retain existing behavior. Every source response clears stale blocks; enrichment does not replace Source Description with canonical Q&A.

## Duplicate-text investigation

Production's observed source-description response 407 (2026-10-01 09:31:15 UTC) was attributed to Post mapping 10 via existing session context. Its response equals the stored description exactly. Both the stored description and reply contain `Next steps to book a seat` twice and `Please let me know how many guests` twice. This is stored operator content, not duplicate transport composition. It is intentionally preserved; an operator may edit its mapping separately if desired. No Approved Q&A or mapping content was rewritten.

At investigation time the current Story inventory endpoint returned no entries. A genuine mapped-Story production smoke must confirm its exact Story identity separately; a mapped Post/session response must not be reported as a Story smoke.

Tests in `tests/test_mapped_home_table_reels.py` cover Story/Post/Reel sources, exact text/order/URLs, stored duplicates preserved, other topics and missing anchor, stale-block clearing, real source send-to-queue path, completion after Meta acceptance, partial retries, new source delivery after earlier completion and blocked senders. Existing QA-M1.1 tests cover restart and mode changes through the reused delivery path.

Production smoke uses a genuinely new qualifying request from an allowed tester. Earlier completed delivery does not suppress a new request; replaying the same inbound job suppresses accepted shares. Do not reset sessions or repair legitimate completion to manufacture smoke evidence.


## QA-SM1.1: per-request delivery

Session-wide suppression is retired. Every new response containing the exact video anchor gets TEXT / four LINK / TEXT, despite earlier completion. Price/payment continuation without the anchor gets no invented links. The flag describes only the latest delivery.

Delivery identity hashes recipient, durable inbound job identity and HOME_TABLE_REELS. Rebuilding the same job retains its generation and original queue positions; unique inbound-job/sequence and durable receipts prevent confirmed sends from repeating. A new Meta message gets an independent generation. Direct synchronous callers may supply a stable inbound_message_id; an unidentified call represents a new request. No session reset, repair or TTL wait is required. QA-M1B native MEDIA_SHARE now uses this same per-request path.

QA-M1B shares the exact verified native mapping, durable receipts, explicit-rejection fallback and uncertain-send rules documented in [HOME_TABLE_REEL_DELIVERY.md](HOME_TABLE_REEL_DELIVERY.md). Approved and operator text, exact source identities, mappings and text/video-anchor order remain unchanged.
