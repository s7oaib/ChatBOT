# Mapped Home Table source Reel links (QA-SM1)

An exact, active Story/Post/Reel source mapped to `Bengali Home Table`, whose operator Source Description contains `HOME_TABLE_VIDEO_ANCHOR`, uses the same ordered-block builder as Approved Q&A. Text is preserved exactly within the existing trimmed source-response contract; four separate URL text messages are inserted after the anchor. The existing QA-M1.1 queue, per-link durable receipts, retry handling and TEST_ONLY guards remain authoritative. No native sharing is introduced.

The source lane now supplies its own ordered blocks, and the Instagram send path consumes them. Other topics, descriptions without the anchor, topic-description fallback and completed sessions retain existing behavior. Every source response clears stale blocks; enrichment does not replace Source Description with canonical Q&A.

## Duplicate-text investigation

Production's observed source-description response 407 (2026-10-01 09:31:15 UTC) was attributed to Post mapping 10 via existing session context. Its response equals the stored description exactly. Both the stored description and reply contain `Next steps to book a seat` twice and `Please let me know how many guests` twice. This is stored operator content, not duplicate transport composition. It is intentionally preserved; an operator may edit its mapping separately if desired. No Approved Q&A or mapping content was rewritten.

At investigation time the current Story inventory endpoint returned no entries. A genuine mapped-Story production smoke must confirm its exact Story identity separately; a mapped Post/session response must not be reported as a Story smoke.

Tests in `tests/test_mapped_home_table_reels.py` cover Story/Post/Reel sources, exact text/order/URLs, stored duplicates preserved, other topics and missing anchor, stale-block clearing, real source send-to-queue path, completion after Meta acceptance, partial retries, repeated source suppression and blocked senders. Existing QA-M1.1 tests cover restart and mode changes through the reused delivery path.

Production smoke needs a clean allowed tester for four first-send links. Legitimately completed testers can verify description and repeat suppression, but their flag must not be reset to force a first-send smoke. Tester-account preparation remains an operator action.
