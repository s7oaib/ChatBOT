# Camellia Artisanal client handover

Handover date: **3 October 2026, Asia/Kolkata**. This document contains no credentials.

## Accepted release and evidence

- Accepted application/runtime SHA: `f58b1f4157e32361487320f6db2ec72bcdba3342` (PR #56, final Sondesh menu-authority correction).
- Final tested feature HEAD: `113aaec38fdb32c2f3f530e00b5a37e032f98a01`; its tree `b0e8882c602a376862d5b76121ff31fd654af15d` is identical to the accepted merge commit tree.
- Final full regression on the tested tree: **3715 passed, 0 failed**, with one existing Starlette/httpx deprecation warning. Final focused suites: **399 passed**. Mandatory smoke: **68 passed**.
- GitHub fast current-contract gate for the final HEAD completed successfully; the workflow's remote full-regression job was intentionally skipped because the complete local release regression had already passed.
- Accepted Railway runtime deployment: `5b1abab4-c414-4662-9a2f-ce41000eb3bb`, **SUCCESS**, exact commit `f58b1f4157e32361487320f6db2ec72bcdba3342`. Startup completed normally; Approved Q&A startup alignment reported `aligned=0 unchanged=22 failed=0` and no new startup failure was observed.
- PR #55 (`607614ba8bb0e703e4c98ebd9341b127e19a10e2`) added manual exact-link Instagram media mapping, ordered Custom Q&A text/menu blocks, safe SYSTEM override reset, and the bare-product order-routing correction. PR #56 then centralized Collection 1/2 menu resolution on the current Menu Catalogue authority.
- `test_baseline.json` is promoted to the accepted runtime SHA above with production deployment evidence and the 3715-test full-regression count.
- Historical tags remain immutable evidence: `camellia-handover-2026-10-02` and `camellia-native-reels-2026-10-02` are not moved. Any later documentation-only merge/deployment is non-runtime evidence and does not advance the accepted application baseline by itself.

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

**Before deploy:** run impacted tests and mandatory smoke, run the complete full regression for CRITICAL/core/release/handover changes, review the diff, require green release evidence and explicit deployment authorization.

**After deploy:** verify exact SHA, Railway SUCCESS, application startup, public health/Admin availability, no new 5xx/startup error and a limited authorized production smoke. Do not alter chatbot mode merely to make a smoke test easier.

## Maintenance validation

Normal PR/push CI is the fast current-contract gate, not proof of a complete regression. Use `python scripts/run_impacted_tests.py`; failure debugging uses `python -m pytest --lf -q`. CRITICAL/core/release/handover validation requires `python -m pytest -q`.

The final accepted runtime evidence is the **3715 passed / 0 failed** full regression on tested HEAD `113aaec38fdb32c2f3f530e00b5a37e032f98a01`, whose tree is identical to accepted merge `f58b1f4157e32361487320f6db2ec72bcdba3342`. Documentation/baseline metadata changes after this point are non-runtime and do not require another full regression unless they also alter executable/runtime inputs.

See [testing policy](TESTING_STRATEGY.md), [runbook](PRODUCTION_RUNBOOK.md), [architecture](architecture.md), [chatbot control](INSTAGRAM_CHATBOT_CONTROL.md) and [validated dependency snapshot](HANDOVER_DEPENDENCY_SNAPSHOT.md).
