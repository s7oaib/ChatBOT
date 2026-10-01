# Camellia Artisanal client handover

Handover date: **2 October 2026, Asia/Kolkata**. This document contains no credentials.

## Accepted release and evidence

- Accepted application/runtime SHA: `4071402099390dafc28c22c8c2a35920e385d470` (QA-M1B, PR #47).
- Complete source tree: `99a560be27af38f9ce95a555a3eb0338d6a33b53`, identical to tested feature head `72150044d5222b279170bf656021a154076dc59d`.
- Accepted Railway deployment: `709ed586-3b7a-4137-9de7-d0cd0667e9c1`, SUCCESS.
- Final exact-candidate (merge tree identical) [full CI](https://github.com/s7oaib/ChatBOT/actions/runs/36938786442): 3432 passed, 0 failed, 1 existing Starlette/httpx warning; critical flake8 and schemas PASS.
- Production: [Admin portal](https://web-production-0cad70.up.railway.app/admin); `/health` and `/admin` returned 200, startup complete, no observed new HTTP 5xx.
- Original immutable handover tag: `camellia-handover-2026-10-02`, preserved at `f8643e7366902e95ad7f2098a13ec053d14327fd` as historical evidence. New native release tag: `camellia-native-reels-2026-10-02`, points to the accepted QA-M1B runtime.
- `test_baseline.json` remains on the accepted runtime SHA above. A later docs-only deployment does not require another baseline promotion or full regression.

## Production controls and access

Instagram is the only AI chatbot channel. Production is **TEST_ONLY**, with three allowed sender IDs (two earlier entries preserved plus the verified real-Camellia sender for the existing tester). The global status card sits below the Admin header and above navigation on every tab. `INSTAGRAM_CHATBOT_ENABLED` controls channel capability; durable Admin LIVE/TEST_ONLY controls customer access. Only exact enabled Meta sender IDs are allowed in TEST_ONLY; usernames are labels, not authorization. Non-allowlisted customers cannot reach responder, session business processing, orders, handoffs, RAG, Groq, candidates, or customer chatbot sending.

WhatsApp is **HUMAN ONLY**: a customer-facing `wa.me` draft/continuation may be offered for order, purchase, prebook or payment. There is no WhatsApp chatbot, inbound WhatsApp AI route or Cloud API bot sender. Preserve captured order fields and the CAM reference; a draft never confirms an order. Prebook Approved Q&A may append one continuation link. Informational questions such as best time to book must not force a purchase handoff.

Groq is the only AI provider. Approved copy and facts remain authoritative; provider assistance cannot invent facts or rewrite protected approved answers. LLM Auto Resolve actual customer sending is OFF; Web Tester is OFF. Do not change credentials, mode, allowlist, business facts, Meta or Railway configuration without explicit approval.

Browser Admin login uses `ADMIN_USERNAME` and `ADMIN_PASSWORD_HASH`. Machine API access uses separate `ADMIN_API_KEY`. Legacy `ADMIN_PIN` is not browser authentication. Never include passwords, tokens, keys, PINs or recovery destinations in tickets, logs, URLs or Git.

## Approved knowledge and Response Library

Approved Q&A contains canonical client-approved business answers: 23 effective ACTIVE entries at handover. Effective lifecycle is ACTIVE, SCHEDULED, EXPIRED or ARCHIVED. Preview before publishing; verify exact wording, flexible aliases/questions, controlled assets/actions and validity. Admin edits are durable overrides stored separately from immutable source; do not edit seed files to change production copy.

Response Library contains protected SYSTEM templates and operator-managed CUSTOM Q&A. At handover: 67 SYSTEM, 0 CUSTOM. SYSTEM entries are not freely deletable; CUSTOM entries use their normal editing, lifecycle and safe-delete workflow.

QA-CLEAN2 reviewed 16 duplicate candidates, retired **0**: all remain KEEP_DEPENDENCY because live references exist. Do not delete them as presumed duplicates. `combo_bento` remains **KEEP_NEEDS_REVIEW**. See [decision evidence](QA_MENU31_CLEAN2.md). Legacy storage guidance is not an effective canonical Approved Q&A answer; the accepted Instagram policy safely hands off that unsupported question.

## Weekly Savoury and catalogue authority

Weekly Savoury's human-reviewed published timeline is the single changing savoury authority:

`DRAFT → human review → PUBLISHED/UPCOMING → CURRENT at valid_from → SUPERSEDED / EXPIRED`.

Only CURRENT reviewed data is customer authority. Upcoming facts must not appear early. Unverified OCR never auto-publishes. Current at handover: version 5, **2–8 October 2026**, 13 reviewed rows and current uploaded flyer. Review dates as time advances; this is a dated observation, not a permanent menu claim.

Menu Catalogue's Savoury Meal Menu is a projection of Weekly Savoury, not another publish system. **Open weekly workspace** opens the real timeline workspace (production click verified). Ordinary menu browse sends the current approved flyer without an item/price text list. Explicit item-price questions use the current reviewed row and never guess. Bento details use approved copy plus the same current flyer.

## Important: Bento validity decision

**`bento_details_client` and `bento_price_client` expire after 11 October 2026, 23:59:59 Asia/Kolkata. At 12 October 2026, 00:00 IST they become ineligible unless deliberately extended.** This is expected lifecycle behavior.

Before expiry, the operator must choose whether to let both expire or extend their approved validity through the normal Admin preview/publish workflow. This handover does not extend either entry.

## Instagram source mapping and Home Table

Story/Post/Reel routing uses exact Meta identity. Choose Custom Response (`CUSTOM`) or Approved Q&A (`APPROVED_QA`, stable `qa_id`). `source_description` is operator-supplied. Never infer identity from OCR, captions, images or timing. Existing mappings remain intact.

A new qualifying Home Table request sends approved opening text, four ordered native Reel cards (canonical fallback URLs below), then remaining approved text:

1. https://www.instagram.com/reel/DdJhXQMT4VZ/
2. https://www.instagram.com/reel/Dcd5bbEzbCo/
3. https://www.instagram.com/reel/DcLZpuQzCKE/
4. https://www.instagram.com/reel/DcQYp72zf8D/

The same inbound-event retry must not duplicate delivery; partial retries recover only missing links using durable receipts. A new qualifying request can receive its own sequence. Native MEDIA_SHARE is verified and enabled for the four exact owned Camellia media IDs. Explicit unsupported native rejections can use the canonical URL fallback; ambiguous acceptance/timeouts do not immediately send duplicate URLs. See [delivery provenance](HOME_TABLE_REEL_DELIVERY.md).

## Human handoff

`WAITING_FOR_HUMAN` suppresses autonomous Instagram AI replies and persists across restart/deploy. Only explicit operator resume restores AI handling. Repeated messages reuse the active durable handoff. Generic support handoffs do not force WhatsApp; order/payment flows may offer manual continuation. Inspect pending handoffs in Admin rather than clearing them through storage edits.

## Storage, backup and recovery

Production uses durable SQLite at `/app/var/camellia.db` on the `/app/var` Railway volume. Health reports database available and expected durable database/menu placement. Confirm attachment before maintenance. Follow [PRODUCTION_BACKUP_RECOVERY.md](PRODUCTION_BACKUP_RECOVERY.md).

Code rollback and data rollback are separate: never overwrite live SQLite merely because code is rolled back. Back up before schema migration and verify integrity. The authenticated backup endpoint verifies its copy; it does not prove off-host retention or snapshots. Restore requires explicit operator approval. Never initialize an empty production DB over damaged or unavailable storage. QA-M1B created a verified pre-migration backup with integrity_check=ok; no restore or destructive production data operation was performed.

## Operator checklists

**Daily / before changes:** check `/health`, storage, intentional TEST_ONLY/LIVE mode, allowlist if TEST_ONLY, menu and Approved Q&A lifecycle, and pending handoffs.

**Before menu publish:** verify validity dates, reviewed rows/prices and flyer; obtain human approval, then publish.

**Before Approved Q&A publish:** preview exact wording, variations, assets/actions and validity; publish only reviewed content.

**Before deploy:** run impacted tests and selected smoke, full regression for CRITICAL/core/release changes, review diff, require CI and deployment authorization.

**After deploy:** verify exact SHA, Railway SUCCESS, `/health` 200, `/admin` 200, no new observed 5xx and limited authorized production smoke.

## Maintenance validation

Normal PR/push green CI is a fast current-contract gate, not proof of a full suite. Use `python scripts/run_impacted_tests.py`; failure debugging uses `python -m pytest --lf -q`. CRITICAL/core/release/handover validation requires `python -m pytest -q`. Final handover executed one manual full CI on the exact accepted runtime candidate with exact merge-tree equality and reuses that evidence for documentation-only changes.

See [testing policy](TESTING_STRATEGY.md), [runbook](PRODUCTION_RUNBOOK.md), [architecture](architecture.md), [chatbot control](INSTAGRAM_CHATBOT_CONTROL.md) and [validated dependency snapshot](HANDOVER_DEPENDENCY_SNAPSHOT.md).
