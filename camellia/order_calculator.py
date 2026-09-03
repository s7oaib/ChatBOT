import re
from typing import Dict, Any, Optional, List
from camellia.slang_normalizer import normalize_slang

PRICING_TABLE = {
    "aamar_mishti": {
        "classic": { "250g": 450, "500g": 800, "1kg": 1500 },
        "rose": { "250g": 450, "500g": 800, "1kg": 1500 },
        "mango": { "250g": 550, "500g": 1000, "1kg": 1800 },
        "kesar": { "250g": 550, "500g": 1000, "1kg": 1800 },
        "aam ada": { "250g": 550, "500g": 1000, "1kg": 1800 },
        "nolen gur": { "250g": 600, "500g": 1100, "1kg": 2000 },
        "jolbhora": { "250g": 750, "500g": 1400, "1kg": 2500 },
        "kanchagolla": { "250g": 450, "500g": 800, "1kg": 1500 },
    },
    "signature": {
        "classic": { "250g": 600, "500g": 1100, "1kg": 2000 },
        "rose": { "250g": 600, "500g": 1100, "1kg": 2000 },
        "mango": { "250g": 650, "500g": 1200, "1kg": 2200 },
        "kesar": { "250g": 650, "500g": 1200, "1kg": 2200 },
        "vanilla": { "250g": 650, "500g": 1200, "1kg": 2200 },
        "cinnamon": { "250g": 650, "500g": 1200, "1kg": 2200 },
        "dalchini": { "250g": 650, "500g": 1200, "1kg": 2200 },
        "nolen gur": { "250g": 700, "500g": 1300, "1kg": 2500 },
        "thandai": { "250g": 700, "500g": 1300, "1kg": 2500 },
        "medjool dates": { "250g": 700, "500g": 1300, "1kg": 2500 },
        "dates": { "250g": 700, "500g": 1300, "1kg": 2500 },
        "chocolate": { "250g": 700, "500g": 1300, "1kg": 2500 },
        "jolbhora": { "250g": 900, "500g": 1600, "1kg": 2900 },
        "mahua": { "250g": 900, "500g": 1600, "1kg": 2900 },
        "kanchagolla": { "250g": 600, "500g": 1100, "1kg": 2000 },
    }
}

MEAL_PRICING_TABLE = {
    "vegetarian bento": { "name": "Vegetarian Bento (Aloo Dom + Luchi + Chatni + Sondesh)", "price": 350 },
    "veg bento": { "name": "Vegetarian Bento (Aloo Dom + Luchi + Chatni + Sondesh)", "price": 350 },
    "chicken bento": { "name": "Chicken Bento (Kolaghat Dhaba Chicken + Luchi + Chatni + Sondesh)", "price": 400 },
    "non veg bento": { "name": "Chicken Bento (Kolaghat Dhaba Chicken + Luchi + Chatni + Sondesh)", "price": 400 },
    "mangsho bento": { "name": "Mangsho Bento (Slow-cooked Mutton + Luchi + Chatni + Sondesh)", "price": 600 },
    "kosha mangsho bento": { "name": "Mangsho Bento (Slow-cooked Mutton + Luchi + Chatni + Sondesh)", "price": 600 },
    "mutton bento": { "name": "Mangsho Bento (Slow-cooked Mutton + Luchi + Chatni + Sondesh)", "price": 600 },
    "aloor dom combo": { "name": "Luchi + Niramish Aloor Dom Combo", "price": 400 },
    "mutton combo": { "name": "Luchi + Robibarer Mangsho Combo", "price": 700 },
    "niramish aloor dom": { "name": "Niramish Aloor Dom (150g)", "price": 300 },
    "aloor dom": { "name": "Niramish Aloor Dom (150g)", "price": 300 },
    "dhokar dalna": { "name": "Dhokar Dalna (150g / 4 pcs)", "price": 300 },
    "chhanar kofta": { "name": "Chhanar Kofta (120g / 6 pcs)", "price": 300 },
    "shorshey diye dhyarosh": { "name": "Shorshey Diye Dhyarosh (150g)", "price": 300 },
    "potoler dolma veg": { "name": "Potoler Dolma Veg (150g / 4 pcs)", "price": 300 },
    "potoler dolma non veg": { "name": "Potoler Dolma Non-Veg (150g / 4 pcs)", "price": 400 },
    "potoler dolma": { "name": "Potoler Dolma (150g / 4 pcs)", "price": 300 },
    "basonti polao": { "name": "Basonti Polao (250g)", "price": 300 },
    "aromatic rice": { "name": "Aromatic Rice (250g)", "price": 150 },
    "contai dhaba chicken": { "name": "Contai Dhaba Chicken (150g / 1 pc)", "price": 400 },
    "contai chicken": { "name": "Contai Dhaba Chicken (150g / 1 pc)", "price": 400 },
    "bhetki paturi": { "name": "Bhetki Paturi (150g / 2 pcs)", "price": 700 },
    "golda chingri malai curry": { "name": "Golda Chingri Malai Curry (180g / 1 pc)", "price": 900 },
    "chingri malai curry": { "name": "Golda Chingri Malai Curry (180g / 1 pc)", "price": 900 },
    "robibarer mangsho": { "name": "Robibarer Mangsho / Mutton Curry (200g / 4 pcs)", "price": 600 },
    "mutton curry": { "name": "Robibarer Mangsho / Mutton Curry (200g / 4 pcs)", "price": 600 },
    "mutton kosha": { "name": "Mutton Kosha (200g / 4 pcs)", "price": 600 },
    "kosha mangsho": { "name": "Mutton Kosha / Kosha Mangsho (200g / 4 pcs)", "price": 600 },
    "chicken kosha": { "name": "Chicken Kosha (150g / 1 pc)", "price": 400 },
    "chhoto aloor dom": { "name": "Chhoto Aloor Dom (150g)", "price": 300 },
    "chaal potol": { "name": "Chaal Potol (150g)", "price": 300 },
    "dhhokar dalna": { "name": "Dhhokar Dalna (150g / 4 pcs)", "price": 300 },
    "chhanar kofta malay curry": { "name": "Chhanar Kofta Malay Curry (120g / 6 pcs)", "price": 300 },
    "golda chingri malay curry": { "name": "Golda Chingri Malay Curry (150g / 1 pc)", "price": 900 },
    "aamra chatni": { "name": "Aamra Chatni (100g)", "price": 100 },
}

class OrderCalculator:
    """Parses order intent from chat text and generates itemized price breakdowns."""

    def _parse_single_clause(self, clause_text: str, default_collection: str = "aamar_mishti") -> Optional[Dict[str, Any]]:
        norm_clause = normalize_slang(clause_text)
        text_lower = norm_clause.lower()

        # Check for Meal / Bento / Curry order first (sorted by length descending to match 'non veg bento' before 'veg bento')
        for meal_key, meal_info in sorted(MEAL_PRICING_TABLE.items(), key=lambda x: len(x[0]), reverse=True):
            if re.search(r'\b' + re.escape(meal_key) + r'\b', text_lower):
                qty = 1
                qty_match = re.search(r'\b(\d+)\s*(?:box|boxes|pack|packs|portion|portions|plate|plates)?\b', text_lower)
                if qty_match and qty_match.group(1):
                    try:
                        parsed_q = int(qty_match.group(1))
                        if 1 <= parsed_q <= 50:
                            qty = parsed_q
                    except ValueError:
                        qty = 1
                unit_price = meal_info["price"]
                total_price = unit_price * qty
                return {
                    "type": "meal",
                    "collection": "weekly_menu",
                    "flavour": meal_info["name"],
                    "size": "Portion",
                    "quantity": qty,
                    "unit_price": unit_price,
                    "total_price": total_price
                }

        # Determine collection for sweets
        collection = default_collection
        if "signature" in text_lower or "a2" in text_lower:
            collection = "signature"
        elif "aamar mishti" in text_lower or "aamar_mishti" in text_lower or "aamar" in text_lower:
            collection = "aamar_mishti"

        # Determine pack size
        size = None
        if any(kw in text_lower for kw in ["1kg", "1 kg", "1000g", "36 pcs", "36pcs"]):
            size = "1kg"
        elif any(kw in text_lower for kw in ["500g", "500 g", "18 pcs", "18pcs"]):
            size = "500g"
        elif any(kw in text_lower for kw in ["250g", "250 g", "9 pcs", "9pcs", "6 pcs", "6pcs"]):
            size = "250g"
        elif any(kw in text_lower for kw in ["box", "boxes", "pack", "packs", "portion"]):
            size = "250g"

        # Determine flavour
        flavour = None
        for fl in [
            "jolbhora", "mahua", "thandai", "medjool dates", "dates", "chocolate",
            "aam ada", "dalchini", "kanchagolla", "mango", "kesar", "rose",
            "vanilla", "cinnamon", "classic", "nolen gur"
        ]:
            if fl in text_lower:
                flavour = fl
                break

        # Fallback for generic 'sondesh' / 'mishti'
        if not flavour and "sondesh" in text_lower:
            flavour = "classic"

        if not flavour or not size:
            return None

        # Quantity detection
        qty = 1
        qty_match = re.search(r'\b(\d+)\s*(?:box|boxes|pack|packs|pcs)?\b', text_lower)
        if qty_match and qty_match.group(1):
            try:
                parsed_q = int(qty_match.group(1))
                if 1 <= parsed_q <= 50:
                    qty = parsed_q
            except ValueError:
                qty = 1

        # Lookup price
        coll_prices = PRICING_TABLE.get(collection, PRICING_TABLE["aamar_mishti"])
        flavor_prices = coll_prices.get(flavour, coll_prices.get("rose", list(coll_prices.values())[0]))
        unit_price = flavor_prices.get(size, 450)
        total_price = unit_price * qty

        return {
            "type": "sweet",
            "collection": collection,
            "flavour": flavour,
            "size": size,
            "quantity": qty,
            "unit_price": unit_price,
            "total_price": total_price
        }

    @staticmethod
    def _has_order_intent_or_attributes(text_lower: str) -> bool:
        """Returns True if the message contains explicit order keywords, flavours, sizes, or collections."""
        norm_text = normalize_slang(text_lower).lower()
        flavours = ["jolbhora", "mahua", "mango", "kesar", "rose", "vanilla", "cinnamon", "classic", "nolen gur", "sondesh", "mishti", "bento", "combo", "curry", "luchi", "paturi", "chingri", "mangsho", "kofta", "dalna", "polao"]
        if any(fl in norm_text for fl in flavours):
            return True
        sizes = ["1kg", "1 kg", "1000g", "500g", "500 g", "250g", "250 g", "36 pcs", "36pcs", "18 pcs", "18pcs", "9 pcs", "9pcs", "box", "pack", "dabba"]
        if any(re.search(rf'\b{re.escape(sz)}\b', norm_text) for sz in sizes):
            return True
        if any(cl in norm_text for cl in ["signature", "aamar"]):
            return True
        actions = ["order", "buy", "purchase", "cart", "receipt", "add", "remove", "delete", "drop", "clear", "pack", "box", "boxes", "pcs", "send", "give", "deliver", "chahiye", "bhejo"]
        if any(re.search(rf'\b{re.escape(act)}\b', norm_text) for act in actions):
            return True
        return False

    def parse_and_calculate(self, text: str, existing_draft: Optional[Dict[str, Any]] = None, existing_cart: Optional[list] = None) -> Optional[Dict[str, Any]]:
        norm_text = normalize_slang(text)
        text_lower = norm_text.lower().strip()
        
        # Pure price/informational inquiries without order actions should not generate an order receipt
        is_inquiry = any(phrase in text_lower for phrase in [
            "how much", "what is the price", "what's the price", "whats the price",
            "cost of", "price of", "rate of", "how much is", "how much for", "how much does",
            "what is", "what are", "how is", "what lentil", "what ingredients", "used in",
            "made of", "made with", "tell me about", "describe", "explain", "how do you make",
            "can i customize", "can i customise", "can you customize", "can you customise",
            "can i mix", "can we mix", "can you mix", "custom box", "customise the box", "customize the box",
            "assorted", "mix of", "mix flavours", "mix two", "mix and match",
            "can you deliver", "can i get delivery", "do you deliver", "deliver to",
            "tomorrow morning", "tomorrow evening", "by 10 am", "by 10am", "morning by",
            "shelf life", "how many pieces", "how many pcs", "how many piece",
            "is it possible to", "do you take", "do you accept", "is your food", "is meat", "what oil",
            "halal", "payment method", "payment methods", "take cod", "cash on delivery", "failed", "transaction failed",
            "bento", "combo", "combos", "packaging", "eco friendly", "eco-friendly", "what options", "what meals", "what dishes"
        ])
        has_order_action = any(verb in text_lower for verb in [
            "i want to order", "want to order", "please order", "place order", "i would like to order",
            "book an order", "confirm order", "need to order", "chahiye", "bhejo"
        ]) or (
            any(verb in text_lower for verb in ["order", "send", "give", "add", "pack of", "deliver"])
            and any(sz in text_lower for sz in ["250g", "500g", "1kg", "1 kg", "2 portions", "1 portion", "3 portions", "2 plate", "1 plate"])
            and not any(q in text_lower for q in ["can you", "can i", "do you", "how much", "what is", "is it possible", "how long", "when will"])
        )
        if is_inquiry and not has_order_action:
            return None

        if not self._has_order_intent_or_attributes(text_lower):
            return None

        draft = existing_draft or {}
        cart_history = list(existing_cart or [])

        # Split text into potential multi-item clauses (by 'and', ',', '+', '&', 'also', 'with')
        clauses = re.split(r'\b(?:and|also|with)\b|[,+&]', text_lower)
        extracted_items = []

        for clause in clauses:
            item = self._parse_single_clause(clause)
            if item:
                extracted_items.append(item)

        # Fallback to merging with existing_draft if single-clause didn't yield a complete item
        if not extracted_items:
            collection = "signature" if "signature" in text_lower else ("aamar_mishti" if "aamar" in text_lower else draft.get("collection", "aamar_mishti"))
            size = None
            if any(kw in text_lower for kw in ["1kg", "1 kg", "1000g", "36 pcs", "36pcs"]):
                size = "1kg"
            elif any(kw in text_lower for kw in ["500g", "500 g", "18 pcs", "18pcs"]):
                size = "500g"
            elif any(kw in text_lower for kw in ["250g", "250 g", "9 pcs", "9pcs"]):
                size = "250g"
            elif draft.get("size"):
                size = draft["size"]

            flavour = None
            for fl in ["jolbhora", "mahua", "mango", "kesar", "rose", "vanilla", "cinnamon", "classic", "nolen gur"]:
                if fl in text_lower:
                    flavour = fl
                    break
            if not flavour and draft.get("flavour"):
                flavour = draft["flavour"]

            if flavour and size:
                coll_prices = PRICING_TABLE.get(collection, PRICING_TABLE["aamar_mishti"])
                flavor_prices = coll_prices.get(flavour, coll_prices.get("rose", list(coll_prices.values())[0]))
                unit_price = flavor_prices.get(size, 450)
                qty = draft.get("quantity", 1)
                total_price = unit_price * qty
                extracted_items.append({
                    "type": "sweet",
                    "collection": collection,
                    "flavour": flavour,
                    "size": size,
                    "quantity": qty,
                    "unit_price": unit_price,
                    "total_price": total_price
                })

        # Check for Removal / Deletion intent
        is_remove_intent = any(kw in text_lower for kw in ["remove", "delete", "drop", "take out", "cancel item", "dont want", "don't want", "clear cart", "empty cart"])
        if is_remove_intent and cart_history:
            if any(kw in text_lower for kw in ["clear cart", "empty cart", "remove all", "delete all", "cancel order"]):
                return {
                    "items": [],
                    "total_price": 0,
                    "formatted_receipt": "🛒 *Your order cart has been cleared.* What would you like to order instead?"
                }

            target_flavour = None
            for fl in ["jolbhora", "mahua", "mango", "kesar", "rose", "vanilla", "cinnamon", "classic", "nolen gur", "bento", "combo", "chicken", "mutton", "paturi"]:
                if fl in text_lower:
                    target_flavour = fl
                    break

            target_index = None
            idx_match = re.search(r'\b(?:item|#)\s*(\d+)\b', text_lower)
            if idx_match:
                try:
                    target_index = int(idx_match.group(1)) - 1
                except ValueError:
                    target_index = None

            new_cart = []
            removed_name = None
            for idx, itm in enumerate(cart_history):
                if target_index is not None and idx == target_index:
                    removed_name = f"{itm['flavour'].title()} Sondesh" if itm.get("type") != "meal" else itm["flavour"]
                    continue
                if target_flavour and target_flavour in itm["flavour"].lower():
                    removed_name = f"{itm['flavour'].title()} Sondesh" if itm.get("type") != "meal" else itm["flavour"]
                    continue
                new_cart.append(itm)

            if removed_name:
                if not new_cart:
                    return {
                        "items": [],
                        "total_price": 0,
                        "formatted_receipt": f"🗑️ *{removed_name}* has been removed from your cart. Your cart is now empty! What would you like to order instead?"
                    }
                combined_items = new_cart
                removal_prefix = f"🗑️ *{removed_name}* has been removed from your cart!\n\n"
            else:
                combined_items = cart_history
                removal_prefix = ""
        else:
            removal_prefix = ""
            # Determine final active cart items
            is_add_operation = any(kw in text_lower for kw in ["add", "also", "plus", "another", "both"])
            if is_add_operation and cart_history:
                combined_items = list(cart_history) + extracted_items
            elif extracted_items:
                combined_items = extracted_items
            else:
                combined_items = cart_history

        if not combined_items:
            return None

        # Build formatted receipt (single item vs multi-item)
        if len(combined_items) == 1:
            itm = combined_items[0]
            if itm.get("type") == "meal":
                coll_title = "Fresh Weekly Bengali Menu"
                item_title = itm['flavour']
                pack_desc = f"{itm['quantity']} box{'es' if itm['quantity'] > 1 else ''}"
            else:
                coll_title = "Signature A2 Collection" if itm["collection"] == "signature" else "Aamar Mishti Collection"
                item_title = f"{itm['flavour'].title()} Sondesh"
                pack_desc = f"{itm['size']} ({itm['quantity']} box{'es' if itm['quantity'] > 1 else ''})"

            receipt = (
                f"{removal_prefix}"
                f"💐 *CAMELLIA ARTISANAL — ORDER DRAFT RECEIPT*\n"
                f"─────────────────────────────\n"
                f"• *Collection:* {coll_title}\n"
                f"• *Item:* {item_title}\n"
                f"• *Quantity / Pack:* {pack_desc}\n"
                f"• *Unit Price:* ₹{itm['unit_price']}\n"
                f"• *Subtotal:* ₹{itm['total_price']}\n"
                f"• *Packaging:* ₹0 (Eco-Friendly Cornstarch Box)\n"
                f"• *Courier (Porter):* Actuals at dispatch (from Sushant Arcade, Gurgaon)\n"
                f"─────────────────────────────\n"
                f"💰 *Total Estimated Item Price:* ₹{itm['total_price']:,}\n\n"
                f"📞 *Next Steps:* Our team (+91 87965 51990) "
                f"will verify your delivery address & share the official invoice via WhatsApp!"
            )
            return {
                "collection": itm["collection"],
                "flavour": itm["flavour"],
                "size": itm.get("size", "Portion"),
                "quantity": itm["quantity"],
                "unit_price": itm["unit_price"],
                "total_price": itm["total_price"],
                "items": combined_items,
                "formatted_receipt": receipt,
                "has_new_items": bool(extracted_items or is_remove_intent)
            }
        else:
            item_lines = []
            grand_total = 0
            for idx, itm in enumerate(combined_items, 1):
                grand_total += itm["total_price"]
                if itm.get("type") == "meal":
                    coll_title = "Weekly Bengali Kitchen"
                    item_title = itm['flavour']
                    pack_desc = f"{itm['quantity']} portion{'s' if itm['quantity'] > 1 else ''}"
                else:
                    coll_title = "Signature A2 Collection" if itm["collection"] == "signature" else "Aamar Mishti Collection"
                    item_title = f"{itm['flavour'].title()} Sondesh"
                    pack_desc = f"{itm['size']} ({itm['quantity']} box{'es' if itm['quantity'] > 1 else ''})"

                item_lines.append(
                    f"• *Item {idx}:* {item_title} ({coll_title})\n"
                    f"  - Pack / Qty: {pack_desc} @ ₹{itm['unit_price']} = ₹{itm['total_price']:,}"
                )
            items_formatted = "\n".join(item_lines)
            receipt = (
                f"{removal_prefix}"
                f"💐 *CAMELLIA ARTISANAL — ORDER DRAFT RECEIPT*\n"
                f"─────────────────────────────\n"
                f"{items_formatted}\n"
                f"─────────────────────────────\n"
                f"• *Packaging:* ₹0 (Eco-Friendly Cornstarch Box)\n"
                f"• *Courier (Porter):* Actuals at dispatch (from Sushant Arcade, Gurgaon)\n"
                f"─────────────────────────────\n"
                f"💰 *Total Estimated Item Price:* ₹{grand_total:,}\n\n"
                f"📞 *Next Steps:* Our team (+91 87965 51990) "
                f"will verify your delivery address & share the official invoice via WhatsApp!"
            )
            return {
                "items": combined_items,
                "total_price": grand_total,
                "formatted_receipt": receipt,
                "has_new_items": bool(extracted_items or is_remove_intent)
            }

order_calculator = OrderCalculator()
