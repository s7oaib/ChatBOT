# QA-MENU1 menu authority diagnosis

Read-only production Admin inspection on 1 October 2026 IST, starting from
`de0553eca7860204d8174012d8fb99b163719663` (QA-LIB1 merged).

## Production lifecycle

- Weekly Savoury identity: `weekly_vegetarian`, independently managed reviewed
  timeline; retired legacy catalogue records do not control its eligibility.
- Current approved/customer-servable weekly version: **none** on 1 October.
- Version 1: approved 30 September at 15:42:23 IST; validity 25–30 September;
  **EXPIRED**, 13 reviewed rows, image associated with its own approval.
- Version 3: approved/published 1 October at 19:49:57 IST; validity 2–8 October;
  **UPCOMING**, 13 reviewed rows; date provenance `OCR_EXTRACTED` accepted at
  publication; flyer belongs to the same immutable approval/version.
- Latest upload: `c4047e62098f41c998d0f6c52dcbab70`, image `2 october.jpg`,
  created 1 October at 19:49:28 IST, revision 2, **PUBLISHED**, warnings empty.
  It is newer than version 1 and is not an unresolved replacement draft.
- Dhokar Dalna is present in the upcoming approval. It is **not** in a current
  customer-servable approval on the inspection date. No price is authorized
  from that future version today.
- Admin active-menu view contains no weekly rows and `is_published=false`.
  Its legacy flyer metadata is not evidence of customer eligibility. Existing
  customer asset selection rejects expired/upcoming versions.

No production dates, rows, drafts, images or routing mappings were edited for
diagnosis. An older screenshot alone cannot establish which authority the
customer saw at that time.

## Diagnostic matrix at inspection time

| Question | State | Expected route | Source version / grounding |
| --- | --- | --- | --- |
| What is the price of Dhokar Dolna? | No current weekly version | Safe handoff | No eligible menu price |
| What is the price of Dhokar Dalna? | Item only upcoming | Safe handoff | Future approval excluded |
| How much is Dhokar Dolna? | No current weekly version | Safe handoff | No eligible menu price |
| Dhokar Dolna price? | No current weekly version | Safe handoff | No eligible menu price |
| give me weekly menu | Upcoming / previous expired | Current-confirmation handoff | No current flyer |
| give me savoury menu | Upcoming / previous expired | Current-confirmation handoff | No current flyer |
| show me the menu | Upcoming / previous expired | Menu safety route | Must not update/deduplicate an order |

These are read-only authority expectations, not claims that new Instagram
messages were sent during diagnosis. Isolated tests reproduce the same
upcoming/expired state and verify the responder's actual routes.

## Independent defects and fixes

1. Active order state previously consumed clear menu-browsing requests.
   Explicit general/weekly browsing now declines order intake, consults fresh
   reviewed authority, and leaves the order draft unchanged. Order, payment,
   historical and named standing-menu requests keep their own routes.
2. The unknown-price gate used static names and could reject reviewed aliases
   before approved-menu retrieval. Current reviewed matching now establishes
   the authority while canonical Approved Q&A retains its earlier precedence.
3. Retrieval normalized case but lacked consistent punctuation/space matching.
   Normalized approved names/aliases now match deterministically. A spelling
   variation may differ by one substituted character in one multiword token,
   with a minimum similarity of 0.9. Multiple plausible rows fail closed;
   pending and non-current matches remain safety relevance only.
4. Reviewed weekly browsing now constructs exact item/quantity/price text from
   the selected current approval and attaches that approval's flyer. Text-only
   approved publication can serve text without retaining an old flyer.
5. Admin review distinguishes NOT LIVE from LIVE / CURRENT and PUBLISHED —
   UPCOMING. It never changes publication or dates automatically.

Existing pending publication, lifecycle selection and image binding already
worked and are preserved. Regression covers replacement pending → publish →
immediate new rows/flyer, static fallback precedence, draft isolation,
ambiguity, order continuity and date transitions.

Route diagnostics retain approved-menu version source references, exact-fact
grounding and the explicit-menu order-decline reason. Unavailable authority
is distinguished from a genuine newer pending replacement.
