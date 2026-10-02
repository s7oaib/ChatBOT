# QA-FINAL1 Response Library Alignment Audit

All 67 protected SYSTEM entries are accounted for: 22 Approved-Q&A delegates, 5 menu/asset delegates, 10 system guards, and 30 entries without a supported runtime authority. Canonical Approved Q&A and greeting are unchanged.

Every APPROVED_QA_OVERLAP uses DYNAMIC_DELEGATION: runtime, Preview, and Admin library reads resolve the current effective Approved Q&A on each read. Durable overrides are versioned audit snapshots, never runtime business authority. Scheduled, expired, archived, unavailable, or unreadable authority fails closed. There is no unsafe STATIC_COPY.

500g/1kg packs are HANDOFF_ONLY because no exact active approved pack authority exists; a future binding requires explicit approved authorization rather than inferring availability from general pricing. Customer-requested quantities may still be captured in an unconfirmed draft for the human manager. 250g remains supported. Generic delivery-time compatibility wording requires clear Sondesh or cooked-food context and otherwise hands off. Explicit approved delivery questions keep their correct authority. Delivery-area compatibility wording has no dedicated approved equivalent and hands off; the approved out-of-area answer is preserved. Weekly menu IDs delegate to the current eligible Weekly Savoury flyer, never a static general_details answer or legacy menu rows.

Menu assets are a separate approved runtime authority. Aamar and Signature delegate to current catalogue/image eligibility; weekly IDs delegate to the current reviewed flyer; previous menus use the existing archive contract. Missing assets fail closed. No legacy Response Library prose is used for these assets. Supported source and deterministic flows run before the unsupported-business fallback.

| Template ID | Purpose | Classification | Approved Q&A ID | Action | Effective runtime authority |
|---|---|---|---|---|---|
| `GUARD_allergen_nutfree_glutenfree` | nut allergy | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `GUARD_collection_differentiation` | difference between collection 1 and collection 2 | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `GUARD_customization_flavour_mixing` | can i mix rose and classic | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `GUARD_dietary_suitability` | vegan | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `GUARD_halal_ingredients_oil` | halal | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `GUARD_payment_method` | payment methods | APPROVED_QA_OVERLAP | general_payment_client | ALIGNED | APPROVED_QA_DELEGATION |
| `GUARD_pickup_address_hours` | kitchen address | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `GUARD_shelf_life_storage` | shelf life | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `GUARD_staff_names_manager_contact` | who is the owner | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `aamar_mishti_collection` | collection 1 | MENU_ASSET_DELEGATION | — | DYNAMIC_MENU_DELEGATION | MENU_ASSET_DELEGATION |
| `alacarte_prebooking_steps` | how to order from menu | APPROVED_QA_OVERLAP | alacarte_prebook | ALIGNED | APPROVED_QA_DELEGATION |
| `artisanal_differentiation` | different from | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `bengali_home_table_details` | bengali home table | APPROVED_QA_OVERLAP | home_table_details | ALIGNED | APPROVED_QA_DELEGATION |
| `bengali_home_table_payment` | bengali home table how to pay | APPROVED_QA_OVERLAP | home_table_payment | ALIGNED | APPROVED_QA_DELEGATION |
| `bengali_home_table_pricing` | pricing of bengali home lunch tables kolkata | APPROVED_QA_OVERLAP | home_table_price | ALIGNED | APPROVED_QA_DELEGATION |
| `bento_details` | bento | APPROVED_QA_OVERLAP | bento_details_client | ALIGNED | APPROVED_QA_DELEGATION |
| `bot_capabilities` | how can you assist me | SYSTEM_GUARD | — | PRESERVED | SYSTEM_GUARD |
| `bot_identity` | who are you | SYSTEM_GUARD | — | PRESERVED | SYSTEM_GUARD |
| `chicken_kosha_price` | chicken kosha price | APPROVED_QA_OVERLAP | chicken_kosha_price_client | ALIGNED | APPROVED_QA_DELEGATION |
| `combo_bento` | combo | APPROVED_QA_OVERLAP | bento_details_client | ALIGNED | APPROVED_QA_DELEGATION |
| `contact_social` | instagram | SYSTEM_GUARD | — | PRESERVED | SYSTEM_GUARD |
| `delivery_area` | delivery | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `delivery_out_of_area` | bangalore | APPROVED_QA_OVERLAP | delivery_out_of_area_client | ALIGNED | APPROVED_QA_DELEGATION |
| `delivery_time` | how much time | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `freshness_ingredients` | fresh | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `greeting` | hi | SYSTEM_GUARD | — | PRESERVED | SYSTEM_GUARD |
| `how_to_order` | how to order | APPROVED_QA_OVERLAP | order_prebook | ALIGNED | APPROVED_QA_DELEGATION |
| `jolbhora` | jolbhora | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `jolbhora_talgash_details` | jolbhora | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `kanchagolla` | kanchagolla | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `know_more` | want to know more | SYSTEM_GUARD | — | PRESERVED | SYSTEM_GUARD |
| `kolkata_expansion_info` | kolkata | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `mahua_signature_special` | mahua | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `minimum_sondesh_quantity` | why is the minimum order 9 pieces | APPROVED_QA_OVERLAP | minimum_sondesh_quantity_client | ALIGNED | APPROVED_QA_DELEGATION |
| `name_introduction` | my name is | SYSTEM_GUARD | — | PRESERVED | SYSTEM_GUARD |
| `nolen_gur_aamar` | nolen gur aamar | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `opening_hours` | opening hours | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `order_next_steps` | final selection | APPROVED_QA_OVERLAP | order_ready_handoff | ALIGNED | APPROVED_QA_DELEGATION |
| `order_status_tracking` | order status | SYSTEM_GUARD | — | PRESERVED | SYSTEM_GUARD |
| `pack_size_1kg` | 1kg | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `pack_size_250g` | 250g | APPROVED_QA_OVERLAP | sondesh_price_client | ALIGNED | APPROVED_QA_DELEGATION |
| `pack_size_500g` | 500g | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `packaging` | packaging | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `previous_menus` | previous menu | MENU_ASSET_DELEGATION | — | DYNAMIC_MENU_DELEGATION | MENU_ASSET_DELEGATION |
| `pricing_value_context` | price is high | APPROVED_QA_OVERLAP | price_is_high | ALIGNED | APPROVED_QA_DELEGATION |
| `product_overview` | tell me about your product | SYSTEM_GUARD | — | PRESERVED | SYSTEM_GUARD |
| `product_recommendation` | whats the best one | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `recall_name` | what is my name | SYSTEM_GUARD | — | PRESERVED | SYSTEM_GUARD |
| `recall_order_flavour` | earlier flavour choice | SYSTEM_GUARD | — | PRESERVED | SYSTEM_GUARD |
| `savoury_best_time_to_book` | best time to order | APPROVED_QA_OVERLAP | best_time_to_book | ALIGNED | APPROVED_QA_DELEGATION |
| `savoury_crosssell_sondesh` | savoury crosssell | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `savoury_delivery_time` | savoury delivery time | APPROVED_QA_OVERLAP | savoury_delivery_time | ALIGNED | APPROVED_QA_DELEGATION |
| `savoury_food_intro` | lunch menu | APPROVED_QA_OVERLAP | general_details | ALIGNED | APPROVED_QA_DELEGATION |
| `signature_collection` | collection 2 | MENU_ASSET_DELEGATION | — | DYNAMIC_MENU_DELEGATION | MENU_ASSET_DELEGATION |
| `sondesh_classic_rose` | what is the price of rose sondesh | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `sondesh_crosssell_savoury` | sondesh crosssell | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `sondesh_details` | tell me more about sondesh | APPROVED_QA_OVERLAP | sondesh_details_client | ALIGNED | APPROVED_QA_DELEGATION |
| `sondesh_dispatch_time` | how long to deliver | APPROVED_QA_OVERLAP | sondesh_delivery_time | ALIGNED | APPROVED_QA_DELEGATION |
| `sondesh_mango_kesar` | mango sondesh | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `sondesh_nolen_gur` | nolen gur sondesh | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `sondesh_price_detailed` | how much is sondesh | APPROVED_QA_OVERLAP | sondesh_price_client | ALIGNED | APPROVED_QA_DELEGATION |
| `sondesh_unfamiliar` | sondesh unfamiliar | APPROVED_QA_OVERLAP | sondesh_unfamiliar_client | ALIGNED | APPROVED_QA_DELEGATION |
| `subscription_booking_steps` | how to book subscription | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `subscription_details` | subscription | APPROVED_QA_OVERLAP | subscription_details_client | ALIGNED | APPROVED_QA_DELEGATION |
| `sugar_free_options` | sugar free | NO_APPROVED_AUTHORITY | — | HANDOFF_ONLY | HANDOFF_ONLY |
| `weekly_menu_nonveg` | non veg menu | MENU_ASSET_DELEGATION | — | DYNAMIC_MENU_DELEGATION | MENU_ASSET_DELEGATION |
| `weekly_menu_veg` | veg menu | MENU_ASSET_DELEGATION | — | DYNAMIC_MENU_DELEGATION | MENU_ASSET_DELEGATION |
