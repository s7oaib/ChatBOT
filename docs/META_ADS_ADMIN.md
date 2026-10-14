# Meta Ads Admin

Ads is the paid inventory workspace. Posts & Reels and Stories manage source mappings. Sync discovers ads before the first enquiry; webhook observations remain visible under Unmapped and All even without a Marketing result. Unknown observations are not claimed to be active or ended.

## Operator workflow

1. Open Ads and select **Sync Now**. Check last successful sync and any warning.
2. Choose Active, Unmapped, Mapped, Inactive or All. Active shows unique promoted identity groups among Meta `effective_status=ACTIVE` objects; the summary separately reports raw ACTIVE Ad IDs. This is configured status, not proof of current delivery or Ads Library visibility. All, Unmapped, Mapped and Inactive retain individual Ad objects.
3. Enter an exact Meta Ad ID to locate its group in Active, or its individual card in other views. Expand **Manage one exact Ad ID** to inspect member status and use that member's controls. A search never selects a sibling Ad ID for binding.
4. Verify the exact creative media ID or canonical Instagram permalink. Names and thumbnails are display data.
5. If an exact, unambiguous existing source is found, choose **Use Existing Mapping**. Otherwise Map Ad opens cards showing active existing source identities, topics and descriptions.
6. Confirm the existing source. PR #58's bind endpoint attaches the exact Ad ID to that route; it creates no route. Re-read MAPPED and BOUND/Binding ON.
7. Change Mapping, Disable/Enable Binding and History operate on local bindings. Edit Mapping opens the existing source editor. Inactive ads keep mapping and history.

Post/Reel source editors own the approved opening message and selected Approved Q&A IDs. Ads display those effective source settings as read-only inherited configuration; changing the linked source updates every bound ad without an ad-specific Q&A copy. A greeting-only message uses the opening message only when it carries the trusted referral for that exact active Ad ID binding. Organic greetings use the normal short greeting. The existing ad gate continues to govern organic non-greeting messages.

No automatic binding or Marketing write is performed. Sync failure does not interrupt customer replies: customer routing reads only local exact bindings and the existing approved-answer/menu/source pipeline. A previous human-handoff conversation must be resumed by an authorized operator before it can serve as a fresh automated-reply acceptance test.

Presentation grouping prefers exact Instagram media ID, then canonical permalink, then creative ID when Instagram identity is absent, then the individual Ad ID. Conflicting exact media/permalink identities remain separate. An unambiguous permalink-only member may join its media group. Titles, captions, thumbnails and bindings never establish group identity. Group membership is not routing authority: each action still submits one exact Ad ID through PR #58.

## Production grouping diagnostic — 5 October 2026 IST

Read-only inspection of the existing production Admin inventory before this change found **129 raw ACTIVE Ad IDs → 129 unique creative IDs → 94 unique exact Instagram media IDs / canonical permalinks**. Every active row had both Instagram identity fields, with one-to-one media/permalink pairs. There were 94 presentation groups: 35 additional Ad objects repeat existing promoted posts. No Meta request, sync, binding, source update or credential change was needed for this diagnostic.

The Norom Pak Sondesh group has exact media `18132043057680460` and permalink `https://www.instagram.com/p/DdnULrmA3J2/`. Its five members are `120246207525530540`, `120246242722460540`, `120246291802650540`, `120246338108980540` and `120246385008280540`.

This snapshot supports Active=94, not 3. Duplicate grouping explains 129 versus 94; the reported three visible Ads Library creatives cannot be explained by deduplicating these stored authoritative fields. Determining the remaining difference requires matching Ads Library account/filter/time scope and current delivery evidence; this release neither guesses delivery nor manufactures a count of three.

## Server configuration and diagnostics

- Existing Railway `META_MARKETING_ACCESS_TOKEN` and `META_AD_ACCOUNT_ID` are read inside the server. Never copy the token to a browser, ticket, local file or database.
- Account IDs accept numeric and `act_` forms. All reads use `config.INSTAGRAM_API_VERSION`, shared with existing Instagram integration.
- `GET /admin/instagram-ads/sync-status` exposes configured booleans, last success, active count, HTTP success/account acceptance and safe failure category.
- `POST /admin/instagram-ads/sync` performs a read-only server-side Marketing inventory sync; it is also the permanent live diagnostic. Both endpoints require existing Admin authentication and writes require the existing browser CSRF flow.
- Settings / Health displays variable names with Configured/Not configured, timestamp and warning. It exposes no credential values.
- `INVALID_OR_EXPIRED_TOKEN`, `PERMISSION_DENIED`, `RATE_LIMITED`, `ACCOUNT_OR_API_UNAVAILABLE`, `OPTIONAL_FIELDS_UNAVAILABLE`, `NETWORK_OR_INVALID_RESPONSE`, `INVALID_RESPONSE`, `INVALID_PAGINATION`, `INCONSISTENT_PAGINATION`, `PAGINATION_LIMIT`, `SYNC_TIMEOUT` and `LOCAL_SYNC_FAILED` are safe fixed diagnostics. Upstream error messages are not returned or logged.
- Unsupported optional creative fields cause a full restart with a smaller field set. Required ad identity/effective status remain required. Exact-source suggestions are unavailable if Meta does not return exact source identity.
- All pages are buffered before one SQLite transaction publishes the inventory. A failed later page or failed local transaction retains the prior complete inventory. Only complete snapshots mark absent ads ENDED; rows, source routes and binding history remain.
- A durable ten-minute sync lease prevents overlapping writers. Expired crash leases recover automatically. Manual sync is available; this release introduces no background Marketing polling.

## Release validation

The release path is feature branch → PR → impacted/full tests as required → GitHub CI → review → merge to main → Railway production validation. Pre-merge Meta validation uses tests and mocks. This release does not depend on a Railway staging environment; do not create, deploy to or restore ChatBOT-Staging.

Immediately after the reviewed PR is merged and its exact commit is deployed to production, run the authenticated server-side sync endpoint for the first real Meta API read. The server reads the existing Railway environment variables and returns only safe diagnostic results. Keep Marketing API access read-only, never copy the token locally, and do not change production secrets or remove Railway variables as part of this release.

`tests/test_instagram_ads_inventory.py` covers account normalization, version/GET/header safety, pre-DM discovery, full pagination, later-page failures, optional fallback/restart, safe errors, inactive/ended retention, exact suggestions, ambiguity, deduplication, multiple ads per source, idempotence, authenticated PR #58 bind/rebind/disable/history, leases, Post/Reel identity/filter truth, stale flags, JavaScript parsing and executed picker/filter behavior.

Existing `test_ad_mapped_source_context`, `test_shared_post_pairing`, `test_shared_reel_precedence`, `test_instagram_source_referential`, `test_simple_source_mapping`, `test_ordered_approved_response`, `test_client_approved_qa`, `test_instagram_chatbot_control` and full regression preserve the price/source/menu, race, native Reel, SYSTEM/Custom Q&A and LIVE/TEST_ONLY contracts. CI fast gate includes the new inventory and PR #58 suites. The impacted runner escalates app/database/config/test-infrastructure changes to full regression.

Production acceptance requires exact deployment SHA, healthy startup, successful live sync, exact known-ad identity/binding reread, unchanged unrelated routes/bindings, visual Ads/filter/mapping checks, and a real ad-entry customer conversation with the three required questions. Promote the test baseline and accepted handover only after those checks pass. Existing historical tags remain unchanged.
