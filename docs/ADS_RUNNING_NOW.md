# Running Now Ads workflow

## Delivery rule

Running Now uses fresh, read-only server-side Marketing API reads:

- `GET /{account}?fields=timezone_name` establishes the account reporting day.
- `GET /{account}/ads?fields=id,effective_status` establishes current effective status.
- `GET /{account}/insights?level=ad&fields=ad_id,impressions,date_start,date_stop` for that exact reporting day establishes delivered impressions.

An exact Ad ID qualifies only when its current effective status is `ACTIVE` and its reported impressions today are greater than zero. Impressions are evidence of delivery; configured status alone is insufficient. This is **delivery reported today**, not a guarantee of instantaneous delivery. Reporting can lag, and the day resets at account-local midnight. Newly enabled ads with no reported impressions do not qualify yet. Ads paused after delivering today are excluded by the fresh status read.

Primary field references: Meta's [Ad Account SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/adaccount.py) and [Ads Insights SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/adsinsights.py).

The authenticated default `GET /admin/instagram-ads?view=running` returns the safe reporting window, timezone, check timestamp, exact evidence IDs and counts. Reads occur inside the server using its existing environment token; no token is returned, logged, put in URLs or copied locally. Pagination reconstructs the original account endpoint using only its cursor. Partial/malformed/error responses produce unavailable evidence, never a fallback to ACTIVE or a false zero. If delivering IDs are missing from stored discovery inventory, the group count is unavailable and the UI requests a sync; unresolved IDs remain in the diagnostic.

## Read-only production diagnostic before this PR

Observed from production Admin on 2026-10-05 IST, following merged PR #61:

| Measure | Observed count |
| --- | ---: |
| Meta-reported active creative groups | 94 |
| Raw Meta ACTIVE Ad IDs | 129 |
| All retained raw Ad IDs | 156 |
| Raw unmapped / mapped | 155 / 1 |
| Raw inactive / ended | 27 |
| Running Now | **Unverified: Insights endpoint not deployed** |

Last inventory sync shown: `2026-10-04T20:14:41.295887+00:00`. No sync, mapping or binding was submitted during this diagnostic. The operator's Ads Library count of three is not used as API evidence or a forced result.

The existing production Admin APIs expose inventory metadata only. With tokens retained server-side and no branch deployment/automatic merge, a live Insights count cannot be obtained before review. First live validation follows an explicitly approved merge and production deployment. Read the default Ads response, compare its evidence/counts to Meta Active, record the actual result even if it differs from three, and check the reporting window and unresolved IDs. No mapping submission is needed for validation.

## Identity and mapping

Meta Active and All retain the existing technical inventory and presentation grouping. Running Now permits a multi-ID mapping confirmation only for complete matching `(effective_instagram_media_id, canonical Instagram permalink)` pairs that have no conflicting identities anywhere in inventory. Incomplete/conflicting identities remain individual cards. All verified siblings are exposed, including IDs that did not supply today's delivery evidence; the card lists evidence IDs separately.

Names, thumbnails and captions are display information only. Choosing Map Ad lists every affected exact ID and its current mapping. Selecting an existing source then requires confirmation of the IDs and the selected source identity, including any replaced binding. Each confirmed member calls the existing PR #58 per-ID binding endpoint individually. No group binding record or runtime authority is introduced. Requests stop on failure; confirmed IDs are reported, the last attempted ID must be verified, and blind retry is disabled until the operator refreshes and reselects.

No source route, ad binding, Railway variable, credential, Instagram identity or Approved Q&A configuration is changed by implementation or diagnostic reads.
