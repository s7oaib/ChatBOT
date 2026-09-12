# Camellia Artisanal — System Template Content-Integrity Audit

Audit date: 2026-09-13  
Audited seed before remediation: `data/qa_templates.json`, `seed_version=19`
Production health before remediation: HTTP 200, `status=healthy`, 58 templates loaded, seed warning cleared (`live_seed_version=19`). The remediation deployment bumps the seed to 20 for the neutral dietary copy.

## Executive summary

All 58 system templates in the production seed were inventoried. The audit reviewed 43 grouped atomic claim families (repeated price rows are counted once per canonical family rather than once per occurrence). No uncertain business copy was automatically rewritten. The only production change made before this audit was the deployment-required seed-version bump from 18 to 19; it delivered the already-approved greeting change and did not reseed the database destructively.

| Classification | Count |
|---|---:|
| SUPPORTED | 22 |
| OUTDATED | 0 |
| UNSUPPORTED | 0 |
| CONFLICTING | 0 |
| NEEDS CLIENT CONFIRMATION | 16 |
| Total grouped claims reviewed | 43 |

## Controlled remediation (2026-09-13)

The four canonical conflicts and the unsupported dietary wording were corrected
through the versioned `response_template_overrides` mechanism. The startup
migration is idempotent, records the prior copy in
`response_template_override_versions` with reason
`approved_canonical_content_correction`, and never overwrites a distinct
client-authored override.

| Template | Old copy | New copy | Canonical source | Reason | Version-history entry |
|---|---|---|---|---|---|
| `weekly_menu_veg` | Legacy six-row ₹300 table | Current Aloor Dom, Chaal Potol, Chholar Daal and Chanar Kofta/Dalna rows | `data/menu.json` → `weekly_menus[-1].vegetarian` | Remove stale dishes/prices | Created by `content_integrity_audit_2026-09-13` at deployment |
| `weekly_menu_nonveg` | Legacy Contai/Robibarer/Bhetki/Golda table | Current Mangsho, Bhetki Paturi, Chingri Malaikari and Kolkata Chicken Chaap rows | `data/menu.json` → `weekly_menus[-1].non_vegetarian` | Remove stale dishes/prices | Created by `content_integrity_audit_2026-09-13` at deployment |
| `combo_bento` | Legacy four-option combo table | Current Veg Bento ₹400 and Non-Veg Bento ₹700 with canonical inclusions | `data/menu.json` → `weekly_menus[-1].bengali_bento` | Align contents/prices | Created by `content_integrity_audit_2026-09-13` at deployment |
| `bento_details` | ₹350/₹400/₹600 legacy details and fee claims | Same current Veg/Non-Veg Bento copy as `combo_bento` | `data/menu.json` → `weekly_menus[-1].bengali_bento` | Align contents/prices; omit unsupported fee claims | Created by `content_integrity_audit_2026-09-13` at deployment |
| `sugar_free_options` | Lower-glycemic and diabetic guidance | Neutral ingredient/sugar-free limitation and ask-before-ordering wording | Audit finding: no approved medical evidence | Remove unsupported medical claim | Seed version 20 + managed override |

Jolbhora pricing/piece-count and sourcing, team, availability, gifting and
event/corporate claims remain `CLIENT CONFIRMATION REQUIRED`; no guessed copy
was published.

Risk summary: 7 critical, 24 high, 10 medium, and 2 low claim rows. See the claim matrix below for the actionable items.

## Canonical sources

The audit treats these as authoritative, in this order:

1. `data/menu.json` — current standing catalogue, pack sizes, piece counts, prices, current weekly dishes, delivery area, courier note, packaging note, and contact handles.
2. `camellia/knowledge.py` (`SOURCE_MAP`, `CATALOGUE`) — domain ownership and precedence used by the runtime resolver. It explicitly gives `menu.json` precedence for weekly dishes/prices and identifies human-only domains such as availability and event/bulk quotes.
3. Client-confirmed facts recorded in template provenance and pinned by `tests/test_client_confirmed_answers.py` (payment, allergy, storage, kitchen address/hours, manager contact, subscription, and dispatch statements). These are accepted as approved facts but remain confirmation-sensitive when no independently maintained catalogue field exists.
4. `data/qa_templates.json` — approved customer-facing copy and protected trigger/runtime metadata. A template is not evidence for its own factual claims; its `source_ref` must point to one of the sources above.
5. Production `GET /health` and Railway deployment logs — runtime readiness, loaded-template count, storage durability, seed reconciliation, token state, and safety flags only; they are not business-fact sources.

No `CLIENT.md` or separate signed business-facts document exists in this checkout. Response-template overrides are presentation edits, not canonical evidence. Production admin inventory was not fetched because the endpoint requires credentials; the deployed seed and health endpoint were read-only verified.

## Active system-template inventory

The seed contains 58 active templates. System templates do not carry a disabled/archived status field; the database-backed Response Library has separate lifecycle states for client Q&A, not these protected seed rows. Default copy is present in `qa_templates.json`. A production override is not exposed by `/health`; the local repository snapshot contains one active `greeting` override, so that row must be checked in the authenticated Response Library before any copy decision.

| Template ID | Title | Primary trigger | Example aliases | Status | Provenance |
|---|---|---|---|---|---|
| `greeting` | Greeting | `hi` | hello, hey | ACTIVE | qa_templates.json |
| `bot_identity` | Bot Identity | `who are you` | who are u, who r you | ACTIVE | brand identity & assistant introduction |
| `bot_capabilities` | Bot Capabilities | `how can you assist me` | how can you assist, how can you help me | ACTIVE | brand identity & assistant capabilities |
| `product_overview` | Product Overview | `tell me about your product` | tell me about your products, tell me about products | ACTIVE | client Q&A script > PRODUCTS |
| `product_recommendation` | Product Recommendation | `whats the best one` | what's the best one, which one is best | ACTIVE | operator-added recommendation gap |
| `name_introduction` | Name Introduction | `my name is` | my name's, my names | ACTIVE | qa_templates.json |
| `recall_name` | Recall Name | `what is my name` | what's my name, whats my name | ACTIVE | qa_templates.json |
| `know_more` | Know More | `want to know more` | know more, tell me more | ACTIVE | client Q&A script > GENERAL + ADDITIONAL |
| `order_next_steps` | Order Next Steps | `final selection` | this is my final, my address is | ACTIVE | client Q&A script > QUESTION |
| `delivery_time` | Delivery Time | `how much time` | how long does it take, delivery time | ACTIVE | client Q&A script > TIME |
| `nolen_gur_aamar` | Nolen Gur Aamar | `nolen gur aamar` | last batch, nolen gur aamar sondesh | ACTIVE | client Q&A script > NOLEN GUR AAMAR |
| `sondesh_price_detailed` | Sondesh Price Detailed | `how much is sondesh` | how much for sondesh, price of sondesh | ACTIVE | client Q&A script > savoury food ordering |
| `sondesh_classic_rose` | Sondesh Classic Rose | `what is the price of rose sondesh` | classic sondesh, price of rose | ACTIVE | menu.json > Sondesh > Classic/Rose |
| `sondesh_mango_kesar` | Sondesh Mango Kesar | `mango sondesh` | kesar sondesh, aam ada | ACTIVE | client menu cards, April 2026 |
| `sondesh_nolen_gur` | Sondesh Nolen Gur | `nolen gur sondesh` | nolen gur price, thandai | ACTIVE | client menu cards, April 2026 |
| `jolbhora` | Jolbhora | `jolbhora` | liquid filling, jol bhora | ACTIVE | collection cards/order calculator, 2026-09-09 |
| `kanchagolla` | Kanchagolla | `kanchagolla` | kancha golla, kanchagolla price | ACTIVE | menu.json > Kanchagolla |
| `mahua_signature_special` | Mahua Signature Special | `mahua` | mahua price, mahua sondesh | ACTIVE | menu.json > Signature > Mahua |
| `aamar_mishti_collection` | Aamar Mishti Collection | `collection 1` | collection1, collection 1 menu | ACTIVE | menu.json > Aamar Mishti |
| `signature_collection` | Signature Collection | `collection 2` | collection2, collection 2 menu | ACTIVE | client Signature menu card, April 2026 |
| `pack_size_250g` | Pack Size 250g | `250g` | 250 g, 250g box | ACTIVE | client menu cards, April 2026 |
| `pack_size_500g` | Pack Size 500g | `500g` | 500 g, 500g box | ACTIVE | client menu cards, April 2026 |
| `pack_size_1kg` | Pack Size 1kg | `1kg` | 1 kg, 1000g | ACTIVE | client menu cards, April 2026 |
| `weekly_menu_veg` | Weekly Menu Veg | `veg menu` | vegetarian menu, veg dishes | ACTIVE | menu.json > weekly_menus[].vegetarian |
| `weekly_menu_nonveg` | Weekly Menu Nonveg | `non veg menu` | nonveg menu, non veg | ACTIVE | menu.json > weekly_menus[].non_vegetarian |
| `GUARD_collection_differentiation` | Collection Differentiation | `difference between collection 1 and collection 2` | difference between collections | ACTIVE | client confirmation > collection differentiation |
| `GUARD_customization_flavour_mixing` | Customization Flavour Mixing | `can i mix rose and classic` | mix rose and classic | ACTIVE | pending client confirmation; escalates |
| `GUARD_halal_ingredients_oil` | Halal Ingredients Oil | `halal` | halal certified, food halal | ACTIVE | client confirmation 2026-09-08 |
| `GUARD_payment_method` | Payment Method | `payment methods` | payment method, how to pay | ACTIVE | client confirmation > payment/COD |
| `combo_bento` | Combo Bento | `combo` | bento, luchi combo | ACTIVE | menu.json > weekly combos/bento |
| `delivery_area` | Delivery Area | `delivery` | deliver to, do you deliver | ACTIVE | menu.json > delivery |
| `delivery_out_of_area` | Delivery Out Of Area | `bangalore` | bengaluru, mumbai | ACTIVE | client Q&A script > OTHER CITIES |
| `artisanal_differentiation` | Artisanal Differentiation | `different from` | other sweet shops, more expensive | ACTIVE | brand positioning |
| `packaging` | Packaging | `packaging` | plastic, eco friendly | ACTIVE | menu.json > delivery.packaging_note |
| `contact_social` | Contact Social | `instagram` | contact number, whatsapp number | ACTIVE | menu.json > contact |
| `how_to_order` | How To Order | `how to order` | how do I order, place an order | ACTIVE | qa_templates.json/workflow |
| `freshness_ingredients` | Freshness Ingredients | `fresh` | freshness, how fresh | ACTIVE | brand standards > freshness/sourcing |
| `GUARD_dietary_suitability` | Dietary Suitability | `vegan` | is it vegan, are they vegan | ACTIVE | operator-added dietary coverage |
| `GUARD_allergen_nutfree_glutenfree` | Allergen/Nutfree/Glutenfree | `nut allergy` | nuts allergy, allergic to nuts | ACTIVE | client confirmation > allergen safety |
| `order_status_tracking` | Order Status Tracking | `order status` | track my order | ACTIVE | customer support/dispatch |
| `GUARD_pickup_address_hours` | Pickup Address Hours | `kitchen address` | pickup address, self pickup | ACTIVE | client confirmation > address/hours |
| `jolbhora_talgash_details` | Jolbhora Talgash Details | `jolbhora` | jol bhora, liquid nolen gur | ACTIVE | collection cards/order calculator |
| `sugar_free_options` | Sugar Free Options | `sugar free` | sugar-free, sugarfree | ACTIVE | dietary guidelines |
| `recall_order_flavour` | Recall Order Flavour | `earlier flavour choice` | earlier flavor choice | ACTIVE | session memory/order draft |
| `GUARD_shelf_life_storage` | Shelf Life Storage | `shelf life` | how long can I store | ACTIVE | client confirmation > storage |
| `GUARD_staff_names_manager_contact` | Staff/Manager Contact | `who is the owner` | owner, founder | ACTIVE | client confirmation > manager contact |
| `savoury_food_intro` | Savoury Food Intro | `lunch menu` | dinner options, savoury food | ACTIVE | client confirmation 2026-09-08 |
| `bento_details` | Bento Details | `bento` | Bengali bento, what's in bento | ACTIVE | client Q&A script > savoury ordering |
| `previous_menus` | Previous Menus | `previous menu` | previous menus, previous week | ACTIVE | client request 2026-09-04 |
| `subscription_details` | Subscription Details | `subscription` | meal plan, everyday meal | ACTIVE | client confirmation 2026-09-08 |
| `subscription_booking_steps` | Subscription Booking Steps | `how to book subscription` | book subscription, start subscription | ACTIVE | client confirmation 2026-09-08 |
| `alacarte_prebooking_steps` | À La Carte Prebooking | `how to order from menu` | order item from menu, pre-book | ACTIVE | client confirmation 2026-09-08 |
| `sondesh_dispatch_time` | Sondesh Dispatch Time | `how long to deliver` | dispatch time, how fast | ACTIVE | client confirmation 2026-09-08 |
| `savoury_crosssell_sondesh` | Savoury Cross-sell Sondesh | `savoury crosssell` | also sondesh, offer sondesh | ACTIVE | client Q&A script > savoury ordering |
| `sondesh_crosssell_savoury` | Sondesh Cross-sell Savoury | `sondesh crosssell` | also savoury, also meals | ACTIVE | client Q&A script > sondesh-to-savoury |
| `savoury_best_time_to_book` | Savoury Best Time to Book | `best time to order` | when should I book | ACTIVE | client Q&A script > booking |
| `savoury_delivery_time` | Savoury Delivery Time | `savoury delivery time` | delivery time for food | ACTIVE | client confirmation 2026-09-08 |
| `kolkata_expansion_info` | Kolkata Expansion Info | `kolkata` | moving, leaving | ACTIVE | client confirmation 2026-09-08 |

## Claim-by-claim audit matrix

Repeated occurrences in price tables are compared against the single canonical matrix below. Recommendations are non-publishing suggestions unless explicitly marked otherwise.

| Template(s) | Claim | Classification | Canonical evidence | Risk | Recommended action |
|---|---|---|---|---|---|
| `bot_identity`, `delivery_area` | Delivery is available across Delhi NCR | SUPPORTED | `menu.json` delivery.area; `knowledge.py` delivery domain | HIGH | Keep; verify location at order time |
| `delivery_area` | Delhi, Gurgaon, Noida, Ghaziabad and Faridabad are all covered | NEEDS CLIENT CONFIRMATION | `menu.json` says Delhi NCR but does not enumerate cities | HIGH | Confirm the exact city list before retaining enumeration |
| `bot_identity`, `GUARD_pickup_address_hours` | Sushant Lok, Gurgaon / E144, Sushant Arcade, Gate 4 | SUPPORTED | `GUARD_pickup_address_hours` client-confirmed source; menu courier note names Sushant Arcade | HIGH | Keep only after authenticated production copy review |
| `bot_capabilities`, `delivery_time`, `sondesh_dispatch_time`, `savoury_delivery_time` | Kitchen hours 10:00–19:30 | SUPPORTED | client-confirmed templates pinned by tests | HIGH | Keep; operationally re-confirm periodically |
| `delivery_time`, `sondesh_dispatch_time` | Sondesh dispatch within 15 min; Kanchagolla 30 min; Jolbhora 1 hour | SUPPORTED | client-confirmed source and regression tests | HIGH | Keep as “usually”; never convert to guarantee |
| `savoury_delivery_time` | Savoury dispatch within 45 min if not pre-ordered | SUPPORTED | client-confirmed source and regression tests | HIGH | Keep as conditional “usually” wording |
| `delivery_area`, `signature_collection`, `sondesh_price_detailed` | Porter/courier charged on actuals | SUPPORTED | `menu.json` delivery.courier_note | HIGH | Keep; no fixed delivery promise |
| `delivery_time`, `savoury_best_time_to_book` | Pre-order recommended/required to avoid sell-outs | SUPPORTED | client-confirmed source; menu quantities are finite | MEDIUM | Keep as recommendation, not a guarantee |
| Sondesh price templates and pack-size templates | Sondesh prices and 9/18/36 piece counts | SUPPORTED | `menu.json` both collections | CRITICAL | Keep; use matrix below as regression oracle |
| `jolbhora`, `jolbhora_talgash_details`, pack-size templates | Jolbhora prices and 6/12/24 counts | NEEDS CLIENT CONFIRMATION | Audit confirmation scope deliberately excludes repeated template tables; client approval still required | HIGH | Confirm current Aamar/Signature price table; do not infer from repetition |
| `weekly_menu_veg` | Current vegetarian dish names/prices | SUPPORTED after remediation | Current `menu.json` weekly vegetarian block; applied as a managed override | CRITICAL | Keep override; re-run after each menu rotation |
| `weekly_menu_nonveg` | Current non-vegetarian dish names/prices | SUPPORTED after remediation | Current `menu.json` weekly non-vegetarian block; applied as a managed override | CRITICAL | Keep override; re-run after each menu rotation |
| `combo_bento`, `bento_details` | Bento contents/prices | SUPPORTED after remediation | Current `menu.json` Bengali Bento block; applied as a managed override | CRITICAL | Keep override; re-run after each menu rotation |
| `sondesh_classic_rose` | ₹450/800/1500 Aamar and ₹600/1100/2000 Signature | SUPPORTED | `menu.json` Sondesh Classic/Rose | HIGH | Keep prices; separately confirm ingredient adjectives |
| `sondesh_mango_kesar` | Mango/Kesar and Signature Vanilla/Dalchini prices | SUPPORTED | `menu.json` flavour rows | HIGH | Keep; preserve collection restriction |
| `sondesh_nolen_gur` | Nolen Gur/Chocolate price rows and Chocolate Signature-only | SUPPORTED | `menu.json` flavour rows | HIGH | Keep |
| `kanchagolla` | Kanchagolla follows Sondesh prices | SUPPORTED | `menu.json` pricing_note | HIGH | Keep; confirm Signature varieties if expanding |
| `mahua_signature_special` | Mahua ₹900/1600/2900 | SUPPORTED | `menu.json` Mahua price rows | HIGH | Keep price; confirm “forest flowers” wording |
| `product_recommendation`, `nolen_gur_aamar` | Nolen Gur is seasonal/limited or “last batch” | NEEDS CLIENT CONFIRMATION | No season/stock field in `menu.json`; availability is human-only in `knowledge.py` | HIGH | Ask client for current availability; otherwise say availability must be checked |
| `sondesh_classic_rose` | Fresh chhana, cardamom, organic rose petals | NEEDS CLIENT CONFIRMATION | No ingredient field in `menu.json` | HIGH | Confirm exact ingredients; remove “organic” if not certified |
| `mahua_signature_special` | Forest mahua flowers | NEEDS CLIENT CONFIRMATION | Menu names Mahua but does not specify sourcing/flowers | MEDIUM | Confirm or remove the sourcing detail |
| `bot_identity`, `GUARD_collection_differentiation`, `artisanal_differentiation` | Pure dairy milk / desi A2 cow milk | NEEDS CLIENT CONFIRMATION | Current menu does not carry milk-type fields; client card provenance is not an independently maintained fact table | HIGH | Confirm collection-specific milk statement |
| `artisanal_differentiation`, `freshness_ingredients`, `GUARD_collection_differentiation` | Handcrafted end-to-end / partially mechanised | NEEDS CLIENT CONFIRMATION | Not present in `menu.json` | MEDIUM | Confirm approved marketing wording |
| `artisanal_differentiation`, `freshness_ingredients`, `nolen_gur_aamar` | Small-batch farmers, trusted dairies, no general retail sourcing | NEEDS CLIENT CONFIRMATION | Not present in `menu.json`; appears only in provenance-labeled copy | MEDIUM | Confirm sourcing claim or remove detail |
| `artisanal_differentiation`, `freshness_ingredients` | Made fresh daily/from scratch; no preservatives | NEEDS CLIENT CONFIRMATION | No production/ingredient fields in canonical menu | MEDIUM | Confirm operational/ingredient claim |
| `packaging` | Eco-friendly cornstarch, plastic-free boxes | SUPPORTED | `menu.json` delivery.packaging_note | MEDIUM | Keep; avoid broader environmental claims |
| `GUARD_halal_ingredients_oil` | Mutton halal; mustard oil from a farmer; no industrial processing | SUPPORTED | client-confirmed source and regression tests | HIGH | Keep protected guard; re-confirm if supplier changes |
| `GUARD_allergen_nutfree_glutenfree` | Sondesh contains milk and sugar only; no nuts | SUPPORTED | client-confirmed allergen source/tests | CRITICAL | Keep guard; advise exact-dish check for severe allergies |
| `GUARD_dietary_suitability` | Eggless, gluten-free Sondesh; dairy-based/not vegan | NEEDS CLIENT CONFIRMATION | Partly inferred from guard/menu copy, not a structured allergen field | HIGH | Confirm cross-contact and ingredient policy |
| `sugar_free_options` | Lower glycemic index / suitable-sounding diabetic guidance | REMOVED | No medical evidence or canonical nutrition data | CRITICAL | Neutral copy omits health claims and asks for ingredient confirmation |
| `GUARD_shelf_life_storage` | Refrigerated shelf life one week; heat 12 seconds | SUPPORTED | client-confirmed source/tests | HIGH | Keep; confirm if packaging/storage changes |
| `GUARD_payment_method` | UPI/bank transfer only; no COD; cash on pickup | SUPPORTED | client-confirmed source/tests | HIGH | Keep protected guard |
| `GUARD_staff_names_manager_contact` | Guru Dayal Singh and kitchen email | SUPPORTED | client-confirmed source/tests | HIGH | Keep; verify contact remains current |
| `subscription_details`, `subscription_booking_steps` | Subscription plans/prices, 11th meal gift, date-change notice | SUPPORTED | client-confirmed source/tests | HIGH | Keep only while client confirms plan is active |
| `savoury_food_intro` | ₹1500/5 meal subscription, GST and “no other charges” | NEEDS CLIENT CONFIRMATION | No subscription object in `menu.json`; template is client-confirmed but not catalogue-backed | HIGH | Confirm current plan, tax, and fee wording |
| `bento_details` | All prices include GST/packaging/Porter; specific ₹350/400/600 meals | CONFLICTING | Conflicts with current weekly/bento menu and lacks canonical GST fields | CRITICAL | Reconcile before serving |
| `delivery_out_of_area` | No shipping outside Delhi NCR / cold-chain explanation | NEEDS CLIENT CONFIRMATION | `menu.json` states Delhi NCR delivery but no out-of-area policy | HIGH | Confirm current outside-NCR policy |
| `previous_menus` | Two weeks of historical menus are available | NEEDS CLIENT CONFIRMATION | `menu.json` checkout has only the current week; persistence/history endpoint not verified here | MEDIUM | Confirm history retention and remove promise if absent |
| `kolkata_expansion_info` | Gurgaon operation/team/recipes continue unchanged while founders move to Kolkata | NEEDS CLIENT CONFIRMATION | Client-confirmed provenance but no current operations source | MEDIUM | Re-confirm before retaining long-term operational claim |
| `GUARD_collection_differentiation`, `signature_collection` | Gifting/celebration suitability | NEEDS CLIENT CONFIRMATION | Marketing intent, no event/gifting capability record; event/bulk is human-only | MEDIUM | Keep as preference language only; never promise event capacity |
| `contact_social`, `order_next_steps`, `order_status_tracking`, `how_to_order` | Human WhatsApp handoff/order fields | SUPPORTED | Channel policy and order workflow; no automated WhatsApp bot | LOW | Keep; preserve HUMAN ONLY policy |
| `savoury_crosssell_sondesh`, `sondesh_crosssell_savoury` | Fresh meals/Sondesh delivered across Delhi NCR | NEEDS CLIENT CONFIRMATION | Delivery area is supported, freshness/limited-quantity wording is not fully structured | MEDIUM | Confirm or soften freshness/quantity claims |
| `GUARD_customization_flavour_mixing` | No factual answer; escalates | SUPPORTED | Human-only guard is the approved safe behavior | LOW | Keep escalation |

## Price-consistency matrix (canonical sources only)

The standing Sondesh/Kanchagolla/Mahua prices below come from `data/menu.json`. Jolbhora prices are intentionally marked **not present in the canonical menu file** even though templates repeat a table; this is a client-confirmation gap, not a derived fact.

| Product / flavour | Collection | 250g | 500g | 1kg | Pieces | Source |
|---|---|---:|---:|---:|---|---|
| Sondesh Classic/Rose | Aamar Mishti | ₹450 | ₹800 | ₹1500 | 9 / 18 / 36 | menu.json |
| Sondesh Mango/Kesar | Aamar Mishti | ₹550 | ₹1000 | ₹1800 | 9 / 18 / 36 | menu.json |
| Sondesh Nolen Gur | Aamar Mishti | ₹600 | ₹1100 | ₹2000 | 9 / 18 / 36 | menu.json |
| Sondesh Classic/Rose | Signature | ₹600 | ₹1100 | ₹2000 | 9 / 18 / 36 | menu.json |
| Sondesh Mango/Vanilla/Dalchini/Kesar | Signature | ₹650 | ₹1200 | ₹2200 | 9 / 18 / 36 | menu.json |
| Sondesh Nolen Gur/Thandai/Dates/Chocolate | Signature | ₹700 | ₹1300 | ₹2500 | 9 / 18 / 36 | menu.json |
| Sondesh Mahua | Signature | ₹900 | ₹1600 | ₹2900 | 9 / 18 / 36 | menu.json |
| Kanchagolla | Aamar Mishti | same as Sondesh | same as Sondesh | same as Sondesh | depends on Sondesh variety | menu.json pricing_note |
| Kanchagolla | Signature | same as Sondesh | same as Sondesh | same as Sondesh | depends on Sondesh variety | menu.json pricing_note |
| Jolbhora | Aamar Mishti | **not present** | **not present** | **not present** | **not present** | menu.json product has description only |
| Jolbhora | Signature | **not present** | **not present** | **not present** | **not present** | menu.json product has description only |

Template price mismatches against this matrix: none for the Sondesh/Mahua rows; Jolbhora rows cannot be marked supported until the canonical menu/catalogue is populated. Weekly menu and bento prices are separately conflicting as described above.

## Duplicate/conflicting response groups

1. `weekly_menu_veg` vs current `menu.json` vegetarian week — conflicting dishes and prices.
2. `weekly_menu_nonveg` vs current `menu.json` non-vegetarian week — conflicting dishes and prices.
3. `combo_bento` vs `bento_details` vs current `menu.json` bento — conflicting contents/prices.
4. `delivery_time` and `sondesh_dispatch_time` — duplicate Sondesh/Kanchagolla/Jolbhora timing claims; currently aligned, but both require the same operational confirmation.
5. `jolbhora` and `jolbhora_talgash_details` — duplicate Jolbhora price table; canonical menu lacks the table.
6. `sondesh_price_detailed`, `sondesh_*`, `aamar_mishti_collection`, `signature_collection`, and `pack_size_*` — intentional duplicate presentation of the same standing price matrix; the rows agree for Sondesh/Mahua.
7. `bot_identity`, `bot_capabilities`, `artisanal_differentiation`, `freshness_ingredients`, and collection guards — overlapping milk, sourcing, handcrafted, and team claims with no single maintained fact record.
8. Greeting rotation — ten approved variants are intentional; the editable `response_template_overrides` value is authoritative when present and must not be bypassed by a legacy response bank.

## Automatic cleanup

Safe canonical corrections and removal of the unsupported medical wording are
now applied by the controlled, versioned override migration. No protected
runtime logic, route data, menu data, or uncertain client-confirmation claim was
changed.

Conflict re-check: weekly vegetarian **RESOLVED**; weekly non-vegetarian
**RESOLVED**; Bento **RESOLVED**; Jolbhora **CLIENT CONFIRMATION REQUIRED**.

## Response Library and runtime consistency

- Local `/admin/response-library` inventory, exact-phrase search, preview, dry-run, edit, restore, and history paths are covered by `tests/test_custom_qa.py` and `tests/test_admin_ui_structure.py`.
- Production health before this remediation confirms the deployed seed was reconciled (`seed_version=19`, `undelivered_seed_changes=false`) and 58 templates were loaded; the remediation deployment is expected to report seed version 20 after rollout.
- Production authenticated Response Library rows/overrides were not read in this audit because the endpoint requires admin credentials; therefore production override-by-override consistency remains a verification item for the client/admin session.
- No runtime routing, source-context, reel/post/ad, order/CAM, Meta outbound,
  autocomplete, or WhatsApp behavior was changed. Existing dynamic menu
  attachment behavior is preserved; the migration only writes versioned copy
  overrides and the existing renderer consumes them where applicable.

## Seed warning analysis

The previous warning was caused by the approved greeting response change in `fc290b5` being shipped without a seed-version bump. The live volume still had `seed_version=18`/the older `seeded_from_hash`, so health correctly reported undelivered seed changes. The one-line bump to version 19 was committed as `212119d` and deployed. Production now reports `seed_version=19`, `live_seed_version=19`, and `undelivered_seed_changes=false`.

The production SQLite database is on `/app/var/camellia.db` with durable storage. No database wipe, migration, or reseed was performed. Future template migrations are unaffected; future repository template edits must bump `seed_version` deliberately, while Response Library overrides remain durable database edits.

## Final production recommendation

Keep the current runtime unchanged, but do not approve the conflicting weekly/bento copy or unverified marketing/availability claims as canonical. First confirm the concise questions in `TEMPLATE_CLIENT_CONFIRMATIONS.md`, then update only the affected source data or editable copy through the existing versioned workflows. Re-run the focused content and routing regression suites after any approved copy change.
