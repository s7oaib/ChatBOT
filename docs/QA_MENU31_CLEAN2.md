# Weekly workspace navigation and safe legacy retirement

Baseline: `925d84147d94830bd323b276f2a280a698ad9ec7`.

The catalogue projection previously reloaded weekly data while leaving the
catalogue visible. Its existing Open button now opens the existing weekly
workspace, hides the catalogue and independent editor, selects Current, loads
Current/Upcoming without analysing an operator's draft, and scrolls/focuses the
heading. Menu Catalogue returns through the existing view helper. Ordinary
menu Open is unchanged. The browser-script regression clicks the rendered
projection button and exercises both shipped scripts.

## CLEAN2 decisions

The recovered CLEAN1 inventory was checked against current source and
authenticated read-only production endpoints. The machine-readable decisions
are in `QA_CLEAN2_DECISIONS.json`, including replacement QA IDs, current
statuses, code references, assets/actions, source dependencies and reasons.

All 16 duplicate candidates are KEEP_DEPENDENCY. Zero are RETIRE_SAFE or
KEEP_AMBIGUOUS. `combo_bento` is KEEP_NEEDS_REVIEW. The other 50 entries are
KEEP_NOT_DUPLICATE. None is changed or retired in production.

| Legacy IDs | Reason to keep |
|---|---|
| bengali_home_table_details, bengali_home_table_pricing | Home Table fallback/context and Reel delivery contracts; existing CUSTOM source paths remain. |
| bengali_home_table_payment | Home Table contextual payment fallback and existing CUSTOM source paths. |
| bento_details | Dynamic knowledge record, Bento context and current weekly asset handling. |
| chicken_kosha_price | Intent shortcut, legacy tier and ordered Bento/menu media handling. |
| delivery_out_of_area | Named-city recognition and session area-confirmation safety. |
| minimum_sondesh_quantity | Distinctive intent shortcut and legacy selection tier. |
| pricing_value_context | Price-objection shortcut and legacy selection tier. |
| savoury_best_time_to_book | Explicit best-booking-time fallback and legacy scoring. |
| savoury_delivery_time | Dynamic delivery knowledge and context-sensitive legacy scoring. |
| savoury_food_intro | Dynamic source route, menu/price fallback and weekly asset handling. |
| sondesh_details | Product context/media builder and dedicated legacy route. |
| sondesh_dispatch_time | Dispatch clarification/context and legacy intent fallback. |
| sondesh_price_detailed | Dynamic price knowledge/source target, legacy selection and ordered collection cards. |
| sondesh_unfamiliar | Newcomer shortcut and Sondesh media/context handling. |
| subscription_details | Subscription QA route IDs, rate chart, booking state and CUSTOM source paths. |

## Durable retirement contract

Inactive copy overrides still restore shipped copy; they are not retirement.
The separate additive `response_template_retirements` table records status,
canonical replacement, reason, actor, timestamp and append-only event history.
No shipped definitions, Approved Q&A or Custom rows are deleted or rewritten.

Internal `camellia.template_retirement.retire(template_id, actor)` accepts only
an individually reviewed RETIRE_SAFE entry. It rechecks effective canonical QA,
current source references, dynamic knowledge records and database source/topic
dependencies. The shipped decision file permits **zero** IDs. There is no
automatic cleanup, bulk mutation, retirement UI button or public write endpoint.

Suppressed IDs are excluded from responder lookup/fallback, dynamic knowledge,
semantic output, RAG retrieval, AI template context and active source selectors.
Direct legacy lookup fails closed; canonical Approved Q&A remains independent
and retains its exact wording and ordered actions/assets. Admin/audit listing
retains the protected historical row, marks RETIRED and names its replacement.

Internal `restore(template_id, actor)` records an explicit unretire event and
restores eligibility without editing shipped source. Suppression survives
startup, override changes and inactive override fallback. Retire/restore and
all exclusion paths are verified with isolated test databases. No production
retirement or restore is authorized by the current zero-safe decision set.

Production representative EXACT/SEMANTIC *retired* smoke is not applicable
because no entries qualify. Verify the existing guard, conversation and an
Approved Q&A asset response with the existing allowlisted tester. Keep
TEST_ONLY, allowlist, mappings, menu data and configuration unchanged.
