# Camellia Artisanal client handover

Handover date: **5 October 2026, Asia/Kolkata**. This document contains no credentials.

## Accepted release and evidence

- Accepted application/runtime and comparison baseline: `3d412c1e4f94be7552c8882cd127eed1e8eb9e14` (merged [PR #65](https://github.com/s7oaib/ChatBOT/pull/65), Running Now schedule expiry).
- Tested feature HEAD: `aeec381ee900049a08ca2aba591d366b48c3eb24`. Its tree `1cb2ba1fc1f207a342747e0ebb4674fa139793af` is identical to the accepted feature merge tree.
- Normal `python scripts/run_impacted_tests.py`: **3904 passed, 0 failed, 0 skipped, 308.33 seconds** (5 minutes 8 seconds), CRITICAL/full regression. PR #63's unchanged policy ran 2991 audited cases on four process workers and 913 serial cases. All 3881 prior cases remain, with 23 new schedule cases and additional executed Admin JavaScript expiry assertions. Existing deprecation warnings do not change the pass result.
- Both final feature CI gates and main CI passed; the final PR Python 3.11 current-contract gate ran **872 passed, 0 failed/skipped, 65.39 seconds**. Local independent safety/code review found no blocker. CodeRabbit reviewed the initial feature without findings; its final small malformed-response correction was rate-limited and reviewed locally. No blocking review thread remained.
- Accepted Railway runtime deployment: `91f4c509-3589-492e-b667-a3a1d16e79de`, **SUCCESS**, exact merge SHA above. The production environment was observed **Online**, with one RUNNING replica, zero crashed replicas, zero runtime log warnings/criticals, zero failed deployments during this release and empty pending work. Container startup, application startup completion and Approved Q&A alignment `aligned=0 unchanged=22 failed=0` were observed. Railway classifies four standard Uvicorn INFO startup lines on stderr as error-severity records; they are not application startup failures. `/health` remains healthy with its pre-existing informational WhatsApp/manual-handoff notice unchanged.
- Live server-side Meta schedule reads succeeded in `Asia/Kolkata`. Before the first expiry: **3 Running Now groups / 3 evidence IDs**, **94 Meta Active groups / 129 raw ACTIVE IDs**, **156 retained raw IDs**. At **2026-10-05T06:30:00.88228+05:30**, after the actual **6:27:52 am IST** end of Ad `120246384641290540`, Running Now reported **2 groups / 2 evidence IDs**. That ad was absent from delivery evidence and its exact inventory, binding to route 27 and history remained unchanged. No count is forced to three.
- Read-only acceptance audit retained **12 individual exact Ad-ID bindings, 19 current source routes and one archived route**. All three initially running creatives had complete, globally consistent exact media/permalink identities; 2/5/5 member IDs remained available, all BOUND to existing routes 27/37/23. No exact ID resolved to two active routes, no current Running Now member was unmapped, and no display field supplied mapping authority. Full source/binding/history/raw-inventory snapshots matched before and after release and the real expiry.
- PR #64's accepted real customer result remains protected: mapped ad → exact source → canonical contextual Approved Q&A → no unnecessary handoff for “How much does it cost?”. The operator confirmed the real production conversation before this task; this release retained its 63 regression cases and verified the current source/binding structure without sending another real customer message. Sondesh/Bento/Home Table/Subscription resolve to their existing price Q&As; explicit subjects win and ambiguous/unbound/unknown-product context fails closed. Approved wording/assets are unchanged.
- `test_baseline.json` is promoted with the existing utility to the validated runtime SHA and deployment above. A subsequent documentation/baseline-only merge/deployment is non-runtime; it does not replace this application baseline. The prior baseline `f58b1f4157e32361487320f6db2ec72bcdba3342` and historical tags remain immutable evidence.

### Completed release sequence

| PR | Completed work |
| --- | --- |
| [#58](https://github.com/s7oaib/ChatBOT/pull/58) | Durable individual exact Meta Ad-ID bindings: final runtime routing authority. |
| [#59](https://github.com/s7oaib/ChatBOT/pull/59) | Dedicated Meta Ads workspace and safe server-side discovery. |
| [#60](https://github.com/s7oaib/ChatBOT/pull/60) | Exact creative presentation grouping, exact ID search, centered mapping dialog. |
| [#61](https://github.com/s7oaib/ChatBOT/pull/61) | Clear group/raw counters, display previews/names, expandable individual IDs. |
| [#62](https://github.com/s7oaib/ChatBOT/pull/62) | Running Now from current status and current-day delivery evidence; technical inventory preserved. |
| [#63](https://github.com/s7oaib/ChatBOT/pull/63) | Audited process parallelism plus serial-sensitive tests, cached CI, unchanged assertions and gates. |
| [#64](https://github.com/s7oaib/ChatBOT/pull/64) | Canonical contextual natural price follow-ups through existing Approved Q&A and alias safeguards. |
| [#65](https://github.com/s7oaib/ChatBOT/pull/65) | Authoritative schedule window `[start, end)` intersects ad/ad-set boundaries; expiry affects only Running Now. |

### Running Now lifecycle and routing authority

Require current Meta ACTIVE status, positive ad-level impressions on the account-local day, and every supplied schedule boundary: reached start, not reached end. Ad fields `ad_schedule_start_time` / `ad_schedule_end_time` intersect expanded `adset.start_time` / `adset.end_time`; absolute timestamp comparison handles offsets/DST folds. Missing boundaries create no invented expiry. Invalid/rejected schedule data makes delivery evidence unavailable. Reporting lag cannot extend a supplied end. The open Admin view expires evidence using server check time plus elapsed browser time, then refreshes the read-only endpoint.

Expired ads remain in Meta Active/All/appropriate diagnostic history according to retained inventory status. Exact ID search, previews, canonical Instagram identity, all group members, mapping state and audit history remain. Grouping is discovery/presentation only: runtime binding is always an individual exact Ad ID. No sibling binding is created silently; any operator-approved multi-ID confirmation still invokes existing per-ID endpoints. No source, binding or history is deleted because a schedule ends. See [Running Now contract](ADS_RUNNING_NOW.md).

No business mapping, Approved Q&A answer, Meta identity, credential, Railway variable, environment topology or `/app/var` volume was changed for this release. Previous PR #55/#56 exact-link/custom-block and canonical menu-asset behavior remains accepted and protected.

## Production controls and access

Instagram is the only AI chatbot channel. The final operator-selected production mode is **LIVE**. PR #55, PR #56, and their deployments did not change the durable LIVE/TEST_ONLY control. `INSTAGRAM_CHATBOT_ENABLED` remains the separate channel-capability switch. If an operator deliberately changes to TEST_ONLY, only exact enabled Meta sender IDs may reach bot business processing and customer sending; usernames remain labels, not authorization.

WhatsApp is **HUMAN ONLY**. A customer-facing `wa.me` draft/continuation may be offered for order, purchase, prebook or payment, but there is no WhatsApp chatbot, inbound WhatsApp AI route or Cloud API bot sender. Preserve captured order fields and the CAM reference; a draft never confirms an order.

Groq remains the only AI provider. Approved copy and reviewed menu facts remain authoritative; provider assistance cannot invent business facts or rewrite protected approved answers. Keep public Web Tester and automatic-send experimentation disabled unless explicitly authorized. Do not change credentials, chatbot mode, business facts, Meta configuration or Railway configuration as a health workaround.

Browser Admin authentication and machine API authentication remain separate. Never include passwords, tokens, keys, PINs or recovery destinations in tickets, logs, URLs or Git.

## Approved Q&A and Response Library

Approved Q&A remains the canonical authority for supported client-approved business answers. Effective lifecycle states are ACTIVE, SCHEDULED, EXPIRED and ARCHIVED. Preview before publishing; verify exact wording, question-side flexibility, controlled assets/actions and validity. Admin edits remain durable overrides stored separately from the immutable source.

Response Library contains protected SYSTEM responses and operator-managed CUSTOM Q&A:

- **CUSTOM Q&A** supports Add, Edit, safe Delete/archive workflow, aliases, priority/status and optional ordered response blocks.
- Ordered Custom Q&A blocks can be composed as **Text → Menu/Image → Text → Menu/Image → Text**. Menu/image blocks reference current eligible Menu Catalogue records and resolve the current asset at preview/send time rather than storing stale resolved URLs.
- **SYSTEM** responses remain protected. Operator edits are overlays. **Reset / Delete Override** removes only the operator override after confirmation, records history and restores protected default/delegated behavior; it does not delete the underlying SYSTEM template or triggers.
- Protected SYSTEM templates are not permanently deletable. If a protected response should stop being customer-facing, retire/disable through an explicitly designed safe lifecycle rather than deleting its runtime dependency.

QA-CLEAN2 retired zero dependency-bearing templates. Do not delete templates merely because wording appears duplicated when live references still exist.

## Sondesh Collection 1 / Collection 2 menu authority

There is now one runtime menu authority for the two Sondesh collections:

- **Collection 1 / Aamar Mishti / Option 1 / `aamar_menu` / `AAMAR_MISHTI_MENU` → Menu Catalogue `aamar_mishti`**.
- **Collection 2 / Signature / Option 2 / `signature_menu` / `SIGNATURE_SONDESH_MENU` → Menu Catalogue `signature`**.

All customer runtime paths resolve these images dynamically through the current eligible menu record. Publishing a replacement Aamar Mishti or Signature menu therefore changes the next customer lookup without code edits or restart. Approved Q&A asset blocks, direct collection requests, repeated menu requests, mapped source routes and Custom Q&A menu blocks must not carry separate hardcoded customer-authority image paths.

The original baked-in Aamar/Signature images remain only as central built-in fallback assets before a managed current version exists. Archived, inactive, blocked or otherwise ineligible menus fail closed and must not be resurrected by a separate responder path or substituted across collections.

## Weekly Savoury and catalogue authority

Weekly Savoury's human-reviewed published timeline remains the single changing savoury authority:

`DRAFT → human review → PUBLISHED/UPCOMING → CURRENT at valid_from → SUPERSEDED / EXPIRED`.

Only CURRENT reviewed data is customer authority. Upcoming facts must not appear early. Unverified OCR never auto-publishes. At this handover, the current reviewed window remains **2–8 October 2026**; treat this as a dated observation and re-check it as time advances.

Menu Catalogue's Savoury Meal Menu remains a projection of Weekly Savoury rather than a second publish system. Ordinary savoury browsing uses the current approved flyer; explicit item/price questions use reviewed current authority and never guess.

## Important: Bento validity decision

**`bento_details_client` and `bento_price_client` expire after 11 October 2026, 23:59:59 Asia/Kolkata. At 12 October 2026, 00:00 IST they become ineligible unless deliberately extended.**

Before expiry, the operator must choose whether to let both expire or extend validity through the normal Admin preview/publish workflow. This handover does not silently extend them.

## Instagram Post/Reel/Story mapping

Story/Post/Reel routing uses exact Meta identity. Select CUSTOM or APPROVED_QA (`qa_id`) and keep `source_description` operator-supplied. Never infer route identity from caption, OCR, title, preview image or timing.

The Admin Posts & Reels workflow now supports **Add Post / Reel by Link** for an exact Instagram `/p/` or `/reel/` URL that may not appear in the owned `/me/media` inventory, including collaborator/manual cases. Such entries remain visibly manual/exact-link records; pasting a URL does **not** assert Camellia ownership or convert the item to verified CURRENT owned media.

For shared Reels, an exact canonical permalink can be matched safely. For shared Posts, the webhook supplies the Meta Post attachment/media ID rather than the canonical shortcode URL, so collaborator/manual Post routes require the explicit pairing mechanism. Never guess the Post ID from caption, OCR, image or shortcode. Unmapped identifiable media must continue to hand off rather than invent a route.

## Home Table native Reels

A qualifying Home Table request sends approved opening text, four ordered native Reel cards, then remaining approved text. The canonical fallback links remain:

1. https://www.instagram.com/reel/DdJhXQMT4VZ/
2. https://www.instagram.com/reel/Dcd5bbEzbCo/
3. https://www.instagram.com/reel/DcLZpuQzCKE/
4. https://www.instagram.com/reel/DcQYp72zf8D/

The same inbound-event retry must not duplicate delivery; partial retries recover only missing links using durable receipts. Explicit unsupported native rejection may use the canonical URL fallback, while ambiguous acceptance/timeouts must not immediately duplicate the URLs. Bengali Home Table menu requests remain human handoff and must never receive Weekly Savoury as a Home Table menu.

## Order intake

A bare product/category word such as `sondesh`, `bento` or `subscription` is informational context and does **not** start an order draft by itself. Explicit buying intent, quantity/order details, or genuine active-order continuation can start/update the draft. Supported business questions continue to escape an active draft and use their normal Approved Q&A/menu authority; unsupported business questions hand off.

## Human handoff

`WAITING_FOR_HUMAN` suppresses autonomous Instagram AI replies and persists across restart/deploy. Only explicit operator resume restores AI handling. Repeated messages reuse the active durable handoff. Generic support handoffs do not force WhatsApp; order/payment flows may offer manual continuation.

## Storage, backup and recovery

Production uses durable SQLite on the `/app/var` Railway volume. Code rollback and data rollback remain separate operations: never overwrite live SQLite merely because code is rolled back. Back up before schema migration and verify integrity. Restore requires explicit operator approval. Never initialize an empty production database over damaged or unavailable durable storage.

Follow [PRODUCTION_BACKUP_RECOVERY.md](PRODUCTION_BACKUP_RECOVERY.md) for the recovery contract.

## Operator checklists

**Daily / before changes:** check `/health`, storage, intentional LIVE/TEST_ONLY mode, allowlist if TEST_ONLY, current menus, Approved Q&A lifecycle and pending human handoffs.

**Before menu publish:** verify validity/review state, facts/prices and the asset; obtain human approval, then publish. Aamar Mishti and Signature replacement should be performed once in their canonical Menu Catalogue records so every dependent response updates automatically.

**Before Approved Q&A publish:** preview exact wording, aliases, assets/actions and validity; publish only reviewed content.

**Before deploy:** run `python scripts/run_impacted_tests.py` and follow its classification using the existing hybrid safety policy. Review the exact diff/HEAD, require green CI and release evidence, and obtain deployment authorization when it has not already been given. Do not separately repeat a traditional full pytest command.

**After deploy:** verify exact SHA, Railway SUCCESS, application startup, public health/Admin availability, no new 5xx/startup error and a limited authorized production smoke. Do not alter chatbot mode merely to make a smoke test easier.

## Maintenance validation

Normal PR/push CI is the fast current-contract gate, not proof of a complete regression. The single developer command remains `python scripts/run_impacted_tests.py`. Follow its classification: CRITICAL selects the complete hybrid regression; documentation/baseline metadata alone is LOW and selects no runtime pytest. A deliberate full validation, when justified, uses the runner’s `--full` option. Never bypass or weaken its parallel/serial policy.

The accepted runtime evidence is **3904 passed / 0 failed / 0 skipped in 308.33 seconds** on tested HEAD `aeec381ee900049a08ca2aba591d366b48c3eb24`, whose tree is identical to accepted runtime merge `3d412c1e4f94be7552c8882cd127eed1e8eb9e14`. Documentation/baseline metadata changes after this point are non-runtime and do not require another full regression unless they also alter executable/runtime inputs.

See [testing policy](TESTING_STRATEGY.md), [runbook](PRODUCTION_RUNBOOK.md), [architecture](architecture.md), [chatbot control](INSTAGRAM_CHATBOT_CONTROL.md) and [validated dependency snapshot](HANDOVER_DEPENDENCY_SNAPSHOT.md).
