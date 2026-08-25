import csv
import io
import json
import httpx
from urllib.parse import urlparse
from typing import Dict, Any
from camellia.config import config
from camellia.monitoring import get_logger
from camellia.responder import responder

log = get_logger("camellia.sheet_sync")

ALLOWED_SHEET_DOMAINS = {"docs.google.com", "drive.google.com", "google.com", "sheets.google.com"}

def is_safe_csv_url(url: str) -> bool:
    """Validates that URL uses HTTPS and points to an authorized Google Sheets domain to prevent SSRF."""
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme.lower() != "https":
            return False
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return False
        return any(hostname == domain or hostname.endswith("." + domain) for domain in ALLOWED_SHEET_DOMAINS)
    except Exception:
        return False

class SheetSync:
    """
    Google Sheet / Self-Service Menu Sync Engine (Option A in PRD Section 8).
    
    Allows client to update prices & weekly items in a Google Sheet CSV without code edits.
    Hot-reloads responder memory instantly.
    """

    def __init__(self, sheet_url: str = None):
        self.sheet_url = sheet_url or config.GOOGLE_SHEET_CSV_URL
        self.menu_path = config.MENU_JSON_PATH

    def sync_from_csv_url(self, csv_url: str = None) -> Dict[str, Any]:
        """Fetches published Google Sheet CSV and updates current weekly menu items."""
        target_url = (csv_url or self.sheet_url or "").strip()
        if not target_url:
            return {"status": "skipped", "reason": "No GOOGLE_SHEET_CSV_URL configured"}

        if not is_safe_csv_url(target_url):
            log.warning("Rejected suspicious/non-Google CSV URL for SSRF prevention: %s", target_url)
            return {
                "status": "error",
                "reason": "Invalid or untrusted CSV URL. Must be an HTTPS URL from docs.google.com or sheets.google.com"
            }

        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                resp = client.get(target_url)
                resp.raise_for_status()
                csv_text = resp.text

            # Parse CSV rows
            reader = csv.DictReader(io.StringIO(csv_text))
            updated_items = list(reader)

            if not updated_items:
                return {"status": "warning", "message": "CSV contained no rows"}

            # Load existing menu.json
            with open(self.menu_path, "r", encoding="utf-8") as f:
                menu_data = json.load(f)

            # Update latest weekly menu block with fresh CSV rows
            if "weekly_menus" in menu_data and menu_data["weekly_menus"]:
                current_week = menu_data["weekly_menus"][-1]
                
                # Separate items into veg vs nonveg based on category column in CSV
                veg_list = []
                nonveg_list = []
                for row in updated_items:
                    cat = str(row.get("Category", "")).lower()
                    try:
                        price = int(row.get("Price_INR", 300))
                    except (ValueError, TypeError):
                        log.warning("Skipping row with non-numeric Price_INR: %s", row)
                        continue
                    item_dict = {
                        "item": row.get("Item", "Custom Dish"),
                        "description": row.get("Description", ""),
                        "price_inr": price,
                        "quantity": row.get("Quantity", "150g")
                    }
                    if "non" in cat:
                        nonveg_list.append(item_dict)
                    else:
                        veg_list.append(item_dict)

                if veg_list:
                    current_week["vegetarian"] = veg_list
                if nonveg_list:
                    current_week["non_vegetarian"] = nonveg_list

                # Save updated menu.json back to disk
                with open(self.menu_path, "w", encoding="utf-8") as f:
                    json.dump(menu_data, f, indent=2, ensure_ascii=False)

                # Hot-reload responder
                responder.reload_data()
                return {"status": "success", "updated_items_count": len(updated_items)}
            
            return {"status": "failed", "reason": "No weekly_menus block in menu.json"}

        except Exception as e:
            log.error("Sheet sync failed: %s", e)
            return {"status": "error", "error": str(e)}

    def sync_local_reload(self) -> Dict[str, Any]:
        """Manually trigger hot-reload of menu.json & qa_templates.json from disk."""
        try:
            responder.reload_data()
            return {"status": "success", "message": "Menu & QA templates reloaded from disk."}
        except Exception as e:
            return {"status": "error", "error": str(e)}

sheet_sync = SheetSync()
