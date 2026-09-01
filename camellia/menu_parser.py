import re
import json
import base64
import httpx
from typing import Dict, Any, List, Optional
from camellia.config import config
from camellia.monitoring import get_logger
from camellia.sheet_sync import sheet_sync

log = get_logger("camellia.menu_parser")

NON_VEG_KEYWORDS = [
    "mutton", "chicken", "fish", "egg", "non-veg", "non veg", "prawn", 
    "mangsho", "kosha", "cutlet", "kalia", "chingri", "machh", "biryani", "bhetki", "paturi"
]

class MenuParser:
    """
    Auto-extracts structured menu data (Items, Descriptions, Quantities, Prices, Categories)
    from raw unstructured text or uploaded menu card photos / flyers.
    Supports Google Gemini (see GEMINI_MODEL) and OpenAI Vision (gpt-4o / gpt-4o-mini).
    """

    def parse_menu_text(self, raw_text: str) -> Dict[str, Any]:
        """Parses raw menu text into structured vegetarian, non-vegetarian, bento, and sweet item lists."""
        if not raw_text or not raw_text.strip():
            return {"vegetarian": [], "non_vegetarian": []}

        # Try Gemini AI extraction if enabled and key present
        if config.GEMINI_ENABLED and config.GEMINI_API_KEY:
            ai_result = self._parse_text_with_gemini(raw_text)
            if ai_result and (ai_result.get("vegetarian") or ai_result.get("non_vegetarian")):
                log.info("Successfully extracted menu using Gemini AI")
                return ai_result

        # Try OpenAI if configured
        if config.OPENAI_API_KEY:
            ai_result = self._parse_text_with_openai(raw_text)
            if ai_result and (ai_result.get("vegetarian") or ai_result.get("non_vegetarian")):
                log.info("Successfully extracted menu using OpenAI")
                return ai_result

        # Fallback to regex & heuristic extraction
        log.info("Using regex fallback for menu text parsing")
        return self._parse_with_regex(raw_text)

    def _parse_text_with_gemini(self, raw_text: str) -> Dict[str, Any]:
        """Uses Gemini API to convert unstructured menu card text into JSON."""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
        
        prompt = (
            "You are a structured data extraction AI for Camellia Artisanal (Bengali kitchen & sweets bakery).\n"
            "Extract all menu items from the following raw menu text into a strict valid JSON object.\n\n"
            "JSON SCHEMA REQUIREMENT:\n"
            "{\n"
            '  "vegetarian": [\n'
            '    {"item": "Item Name", "description": "Short description", "price_inr": 300, "quantity": "150g"}\n'
            "  ],\n"
            '  "non_vegetarian": [\n'
            '    {"item": "Item Name", "description": "Short description", "price_inr": 400, "quantity": "150g, 1 pc"}\n'
            "  ],\n"
            '  "bengali_bento": [\n'
            '    {"name": "Veg Bento", "price_inr": 400, "includes": ["6 Luchi", "Chholar Daal", "1 Sondesh"]}\n'
            "  ]\n"
            "}\n\n"
            "RULES:\n"
            "1. Extract exact price as integer (price_inr).\n"
            "2. Extract quantity or weight (e.g., '150g', '200g, approx. 4 pcs', '150g, 2 pcs', '250g').\n"
            "3. Categorize items with meat/poultry/fish/egg/mutton/chicken/prawn into 'non_vegetarian', and sweets/dairy/veg into 'vegetarian'.\n"
            "4. Return ONLY valid JSON block without markdown formatting.\n\n"
            f"RAW MENU TEXT:\n{raw_text}"
        )

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048}
        }

        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(url, json=payload)
                if resp.status_code == 200:
                    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    text = re.sub(r"^```json\s*", "", text)
                    text = re.sub(r"^```\s*", "", text)
                    text = re.sub(r"\s*```$", "", text)
                    data = json.loads(text)
                    if isinstance(data, dict):
                        return {
                            "vegetarian": data.get("vegetarian", []),
                            "non_vegetarian": data.get("non_vegetarian", []),
                            "bengali_bento": data.get("bengali_bento", [])
                        }
        except Exception as e:
            log.warning("Gemini menu parsing failed: %s", e)
        return {}

    def _parse_text_with_openai(self, raw_text: str) -> Dict[str, Any]:
        """Uses OpenAI API to convert unstructured menu card text into JSON."""
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        prompt = (
            "You are a structured data extraction AI for Camellia Artisanal (Bengali kitchen & sweets bakery).\n"
            "Extract all dishes, quantities, descriptions, and prices into strict JSON.\n"
            "JSON Format:\n"
            "{\n"
            '  "vegetarian": [{"item": "Name", "description": "Desc", "price_inr": 300, "quantity": "150g"}],\n'
            '  "non_vegetarian": [{"item": "Name", "description": "Desc", "price_inr": 400, "quantity": "150g, 1 pc"}],\n'
            '  "bengali_bento": [{"name": "Name Bento", "price_inr": 400, "includes": ["Item 1", "Item 2"]}]\n'
            "}\n"
            f"RAW TEXT:\n{raw_text}"
        )
        payload = {
            "model": "gpt-4o-mini" if "o3" in config.OPENAI_MODEL else config.OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"}
        }
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    return json.loads(content)
        except Exception as e:
            log.warning("OpenAI menu text parsing failed: %s", e)
        return {}

    def _parse_with_regex(self, raw_text: str) -> Dict[str, Any]:
        """Regex and heuristic parser for extracting items, prices, and quantities from text lines."""
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        veg_items = []
        nonveg_items = []

        current_category = "vegetarian"

        for line in lines:
            line_lower = line.lower()
            price_match = re.search(r'(?:₹|rs\.?|inr)?\s*(\d{2,5})\s*(?:/-)?', line_lower)

            # Pure Category headers (no price digits present in header line)
            if not price_match:
                if "non-veg" in line_lower or "non veg" in line_lower or "non vegetarian" in line_lower:
                    current_category = "non_vegetarian"
                elif "veg" in line_lower or "vegetarian" in line_lower or "sweets" in line_lower:
                    current_category = "vegetarian"
                continue
            
            price_val = int(price_match.group(1))

            # Extract quantity / weight
            qty_match = re.search(r'(\d+\s*(?:g|kg|pcs|pieces|box|pack))', line_lower)
            quantity_val = qty_match.group(1) if qty_match else "150g"

            # Extract clean item name (strip price and bullet points)
            clean_name = line
            clean_name = re.sub(r'[•\-*\d+\.]+\s*', '', clean_name)
            clean_name = re.sub(r'(?:₹|rs\.?|inr)?\s*\d{2,5}\s*(?:/-)?', '', clean_name, flags=re.IGNORECASE)
            clean_name = re.sub(r'\(\d+\s*(?:g|kg|pcs|box)\)', '', clean_name, flags=re.IGNORECASE)
            clean_name = clean_name.strip(" |:-,")

            if not clean_name:
                clean_name = "Artisanal Specialty"

            # Determine category
            is_nonveg = any(kw in clean_name.lower() for kw in NON_VEG_KEYWORDS) or current_category == "non_vegetarian"
            
            item_dict = {
                "item": clean_name.title(),
                "description": f"Freshly handcrafted ({quantity_val})",
                "price_inr": price_val,
                "quantity": quantity_val
            }

            if is_nonveg:
                nonveg_items.append(item_dict)
            else:
                veg_items.append(item_dict)

        return {"vegetarian": veg_items, "non_vegetarian": nonveg_items}

    def parse_and_update_menu(self, raw_text: str) -> Dict[str, Any]:
        """Parses raw text, updates menu.json, and hot-reloads the bot."""
        parsed = self.parse_menu_text(raw_text)
        veg_items = parsed.get("vegetarian", [])
        nonveg_items = parsed.get("non_vegetarian", [])
        bento_items = parsed.get("bengali_bento", [])

        total_extracted = len(veg_items) + len(nonveg_items)
        if total_extracted == 0:
            return {
                "status": "failed",
                "reason": "Could not extract any valid items with prices from the text.",
                "parsed": parsed
            }

        menu_path = config.MENU_JSON_PATH
        try:
            with open(menu_path, "r", encoding="utf-8") as f:
                menu_data = json.load(f)

            if "weekly_menus" in menu_data and menu_data["weekly_menus"]:
                current_week = menu_data["weekly_menus"][-1]
                if veg_items:
                    current_week["vegetarian"] = veg_items
                if nonveg_items:
                    current_week["non_vegetarian"] = nonveg_items
                if bento_items:
                    current_week["bengali_bento"] = bento_items

                with open(menu_path, "w", encoding="utf-8") as f:
                    json.dump(menu_data, f, indent=2, ensure_ascii=False)

                sheet_sync.sync_local_reload()
                return {
                    "status": "success",
                    "updated_items_count": total_extracted,
                    "vegetarian_count": len(veg_items),
                    "non_vegetarian_count": len(nonveg_items),
                    "parsed": parsed,
                    "message": f"Successfully extracted {total_extracted} items and updated menu.json."
                }
            else:
                return {"status": "failed", "reason": "No weekly_menus section found in menu.json"}
        except Exception as e:
            log.error("Failed to write to menu.json: %s", e)
            return {"status": "error", "error": str(e)}

    def parse_image_and_update_menu(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> Dict[str, Any]:
        """
        Multimodal OCR & Data Extraction:
        Reads menu card photo / flyer using Gemini Vision or OpenAI Vision, converts to JSON, updates menu.json, and hot-reloads.
        """
        if not image_bytes:
            return {"status": "failed", "reason": "No image data received."}

        # Groq first: the bot already holds this key for its replies, so photo
        # OCR needs no second provider account.
        if config.GROQ_ENABLED and config.GROQ_API_KEY:
            res = self._parse_image_with_groq(image_bytes, mime_type)
            if res.get("status") == "success":
                return res

        # Try Gemini Vision next
        if config.GEMINI_ENABLED and config.GEMINI_API_KEY:
            res = self._parse_image_with_gemini(image_bytes, mime_type)
            if res.get("status") == "success":
                return res

        # Try OpenAI Vision as fallback
        if config.OPENAI_API_KEY:
            res = self._parse_image_with_openai(image_bytes, mime_type)
            if res.get("status") == "success":
                return res

        log.warning("No vision AI key configured for live OCR extraction.")
        return {
            "status": "warning",
            "reason": "AI Vision API not configured for live image OCR.",
            "message": "Image received successfully. Set GROQ_API_KEY (the same key the bot already uses for replies) for live visual OCR extraction, or GEMINI_API_KEY / OPENAI_API_KEY."
        }

    def _parse_image_with_gemini(self, image_bytes: bytes, mime_type: str) -> Dict[str, Any]:
        """Uses Gemini Vision API to convert menu card photos into structured JSON."""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
        
        prompt = (
            "You are an expert OCR & culinary structured data extraction AI for Camellia Artisanal (Authentic Bengal-Rooted Food & Sweets).\n"
            "Analyze this menu card photo / flyer image with utmost precision.\n"
            "Extract EVERY single dish name, description, portion weight (e.g. 150g, 250g), piece count (e.g. 2 pcs, 4 pcs, 6 pcs), price in INR, and Bento boxes into a strict valid JSON object.\n\n"
            "JSON SCHEMA:\n"
            "{\n"
            '  "vegetarian": [\n'
            '    {"item": "Chhoto Aloor Dom", "description": "Baby potatoes slow cooked in a mildly spiced, aromatic gravy", "price_inr": 300, "quantity": "150g"}\n'
            "  ],\n"
            '  "non_vegetarian": [\n'
            '    {"item": "Chicken Kosha", "description": "Chicken slow-cooked to a rich, celebratory Bengali curry", "price_inr": 400, "quantity": "150g, 1 pc"}\n'
            "  ],\n"
            '  "bengali_bento": [\n'
            '    {"name": "Veg Bento", "price_inr": 400, "includes": ["6 Luchi", "Chholar Daal", "Tomato Aamsottwo Chatni", "1 Classic Aamar Sondesh"]}\n'
            "  ],\n"
            '  "norom_pak_sondesh": [\n'
            '    {"flavour": "Classic", "pack_size": "250g", "pieces": 9, "price_inr": 450}\n'
            "  ]\n"
            "}\n\n"
            "RULES:\n"
            "1. Extract exact price as integer (price_inr).\n"
            "2. Extract quantity or weight with piece count if stated (e.g., '150g', '120g, approx. 6 pcs', '150g, 2 pcs', '180g raw weight, 1 pc', '200g, approx. 4 pcs').\n"
            "3. Categorize meat/poultry/fish/prawn/chicken/mutton dishes into 'non_vegetarian', and veg/dal/rice/chutney/paneer/chhana into 'vegetarian'.\n"
            "4. Return ONLY valid JSON block without markdown backticks."
        )

        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": b64_image
                        }
                    }
                ]
            }],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048}
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, json=payload)
                if resp.status_code == 200:
                    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    text = re.sub(r"^```json\s*", "", text)
                    text = re.sub(r"^```\s*", "", text)
                    text = re.sub(r"\s*```$", "", text)
                    data = json.loads(text)
                    return self._apply_extracted_data_to_menu(data)
                else:
                    log.error("Gemini Vision HTTP %s: %s", resp.status_code, resp.text)
                    return {"status": "error", "error": f"Gemini Vision error HTTP {resp.status_code}"}
        except Exception as e:
            log.error("Failed Gemini vision parsing: %s", e)
            return {"status": "error", "error": str(e)}

    def _parse_image_with_groq(self, image_bytes: bytes, mime_type: str) -> Dict[str, Any]:
        """
        Uses a Groq multimodal model to read a menu photo.

        Tried before the others because it reuses GROQ_API_KEY, which the bot
        already needs for its replies — so menu photo OCR costs no second
        provider account. Groq's chat model has no vision, but its multimodal
        models take images on the same OpenAI-compatible endpoint.
        """
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        data_uri = f"data:{mime_type};base64,{b64_image}"
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {config.GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        prompt = (
            "You are an expert OCR and data extraction AI for Camellia Artisanal.\n"
            "Extract all vegetarian dishes, non-vegetarian dishes, bento boxes, and sweets into valid JSON.\n"
            "Schema: {\"vegetarian\": [{\"item\": str, \"description\": str, \"price_inr\": int, \"quantity\": str}], \"non_vegetarian\": [...], \"bengali_bento\": [...], \"norom_pak_sondesh\": [...]}\n"
            "Return strict JSON."
        )
        payload = {
            "model": config.GROQ_VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    data = json.loads(content)
                    return self._apply_extracted_data_to_menu(data)
                log.warning(
                    "Groq vision HTTP %s (model %r): %s",
                    resp.status_code, config.GROQ_VISION_MODEL, resp.text[:200],
                )
        except Exception as e:
            log.error("Groq vision parsing failed: %s", e)
            return {"status": "error", "error": str(e)}
        return {"status": "failed", "reason": "Groq vision parsing did not return items."}

    def _parse_image_with_openai(self, image_bytes: bytes, mime_type: str) -> Dict[str, Any]:
        """Uses OpenAI Vision API (GPT-4o) to convert menu card photos into structured JSON."""
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        data_uri = f"data:{mime_type};base64,{b64_image}"
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        prompt = (
            "You are an expert OCR and data extraction AI for Camellia Artisanal.\n"
            "Extract all vegetarian dishes, non-vegetarian dishes, bento boxes, and sweets into valid JSON.\n"
            "Schema: {\"vegetarian\": [{\"item\": str, \"description\": str, \"price_inr\": int, \"quantity\": str}], \"non_vegetarian\": [...], \"bengali_bento\": [...], \"norom_pak_sondesh\": [...]}\n"
            "Return strict JSON."
        )
        payload = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}}
                    ]
                }
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"}
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    data = json.loads(content)
                    return self._apply_extracted_data_to_menu(data)
        except Exception as e:
            log.error("OpenAI vision parsing failed: %s", e)
            return {"status": "error", "error": str(e)}
        return {"status": "failed", "reason": "OpenAI vision parsing did not return items."}

    def _apply_extracted_data_to_menu(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Applies extracted JSON to menu.json and triggers hot reloads."""
        veg_items = data.get("vegetarian", [])
        nonveg_items = data.get("non_vegetarian", [])
        bento_items = data.get("bengali_bento", [])
        sondesh_items = data.get("norom_pak_sondesh", [])
        total = len(veg_items) + len(nonveg_items)

        if total == 0:
            return {"status": "failed", "reason": "No dishes could be identified from image."}

        menu_path = config.MENU_JSON_PATH
        with open(menu_path, "r", encoding="utf-8") as f:
            menu_data = json.load(f)

        if "weekly_menus" in menu_data and menu_data["weekly_menus"]:
            current_week = menu_data["weekly_menus"][-1]
            if veg_items:
                current_week["vegetarian"] = veg_items
            if nonveg_items:
                current_week["non_vegetarian"] = nonveg_items
            if bento_items:
                current_week["bengali_bento"] = bento_items
            if sondesh_items:
                current_week["norom_pak_sondesh"] = sondesh_items

            with open(menu_path, "w", encoding="utf-8") as f:
                json.dump(menu_data, f, indent=2, ensure_ascii=False)

            sheet_sync.sync_local_reload()
            return {
                "status": "success",
                "updated_items_count": total,
                "vegetarian_count": len(veg_items),
                "non_vegetarian_count": len(nonveg_items),
                "bengali_bento_count": len(bento_items),
                "parsed": data,
                "message": f"Successfully extracted {total} items from menu image ({len(veg_items)} veg, {len(nonveg_items)} non-veg) and updated menu.json."
            }
        return {"status": "failed", "reason": "weekly_menus not found in menu.json"}

menu_parser = MenuParser()
