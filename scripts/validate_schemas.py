"""
JSON Configuration & Menu Schema Validator for Camellia Artisanal.
Validates the structural integrity of menu.json and qa_templates.json to prevent runtime bot crashes.
"""

import os
import json
import re
import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple

# Runnable from any directory: put the repo root on the path, then use the
# same resolved data paths the bot itself loads.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from camellia.config import config

def validate_menu_schema(file_path: str = None) -> Tuple[bool, List[str]]:
    """Validates the structure, types, and pricing values of menu.json."""
    file_path = file_path or config.MENU_JSON_PATH
    errors = []
    if not os.path.exists(file_path):
        return False, [f"File not found: {file_path}"]

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, [f"Malformed JSON in {file_path}: {e}"]

    # 1. Brand and Contact Validation
    if not isinstance(data.get("brand"), str) or not data.get("brand"):
        errors.append("Missing or empty 'brand' string.")
    
    contact = data.get("contact", {})
    if not isinstance(contact, dict) or not contact.get("whatsapp"):
        errors.append("Missing or invalid 'contact.whatsapp' information.")

    # 2. Collections Validation
    collections = data.get("collections", {})
    if not isinstance(collections, dict):
        errors.append("Missing or invalid 'collections' dictionary.")
    else:
        for col_name, col_data in collections.items():
            if not isinstance(col_data, dict):
                errors.append(f"Collection '{col_name}' must be an object.")
                continue
            products = col_data.get("products", [])
            if not isinstance(products, list):
                errors.append(f"Collection '{col_name}': 'products' must be a list.")
            else:
                for prod in products:
                    if "item" not in prod:
                        errors.append(f"Collection '{col_name}': Product missing 'item' name.")

    # 3. Weekly Menus Validation
    weekly_menus = data.get("weekly_menus", [])
    if not isinstance(weekly_menus, list):
        errors.append("Missing or invalid 'weekly_menus' array.")
    else:
        for i, week in enumerate(weekly_menus):
            week_identifier = week.get("week_of", f"Week #{i+1}")
            if not isinstance(week.get("week_of"), str) or not week.get("week_of"):
                errors.append(f"{week_identifier}: Missing or invalid 'week_of' string.")
            
            for section in ["vegetarian", "non_vegetarian"]:
                if section in week:
                    items = week.get(section, [])
                    if not isinstance(items, list):
                        errors.append(f"{week_identifier} -> {section}: Must be a list of items.")
                    else:
                        for item in items:
                            if not isinstance(item, dict):
                                errors.append(f"{week_identifier} -> {section}: Item must be a dictionary.")
                                continue
                            if "item" not in item or not item["item"].strip():
                                errors.append(f"{week_identifier} -> {section}: Missing item name.")
                            price = item.get("price_inr") or item.get("price")
                            if price is not None and not isinstance(price, (int, float)):
                                errors.append(f"{week_identifier} -> {section}: Price must be a number for '{item.get('item')}'.")

    return len(errors) == 0, errors


def validate_qa_templates_schema(file_path: str = None) -> Tuple[bool, List[str]]:
    """Validates the QA templates dictionary, keywords, and placeholders."""
    file_path = file_path or config.QA_TEMPLATES_PATH
    errors = []
    if not os.path.exists(file_path):
        return False, [f"File not found: {file_path}"]

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, [f"Malformed JSON in {file_path}: {e}"]

    if not isinstance(data, dict):
        return False, ["qa_templates.json must be a JSON object."]

    templates = data.get("templates", [])
    if not isinstance(templates, list):
        errors.append("'templates' key in qa_templates.json must be a list.")
    else:
        seen_ids = set()
        for idx, tpl in enumerate(templates):
            if not isinstance(tpl, dict):
                errors.append(f"Template index {idx} must be a dictionary.")
                continue

            tpl_id = tpl.get("id")
            if not tpl_id or not isinstance(tpl_id, str):
                errors.append(f"Template index {idx}: Missing or invalid 'id'.")
            elif tpl_id in seen_ids:
                errors.append(f"Duplicate template ID found: '{tpl_id}'.")
            else:
                seen_ids.add(tpl_id)

            triggers = tpl.get("triggers", [])
            if not isinstance(triggers, list) or len(triggers) == 0:
                errors.append(f"Template '{tpl_id}': 'triggers' must be a non-empty list.")
            else:
                for kw in triggers:
                    if not isinstance(kw, str) or not kw.strip():
                        errors.append(f"Template '{tpl_id}': Trigger keyword must be a non-empty string.")
                    elif any(char in kw for char in ["(", "[", "\\", "^", "$", "*", "+"]):
                        try:
                            re.compile(kw)
                        except re.error as e:
                            errors.append(f"Template '{tpl_id}': Invalid regex pattern '{kw}': {e}")

            response = tpl.get("response")
            responses = tpl.get("responses")
            if tpl_id == "greeting":
                if not isinstance(responses, list) or not (8 <= len(responses) <= 12):
                    errors.append(f"Template '{tpl_id}': 'responses' must contain 8-12 replies.")
                elif any(not isinstance(reply, str) or not reply.strip() for reply in responses):
                    errors.append(f"Template '{tpl_id}': Every greeting response must be non-empty text.")
                continue
            is_guard_or_escalate = tpl.get("escalate") is True or (isinstance(tpl_id, str) and tpl_id.startswith("GUARD_"))
            if is_guard_or_escalate and response is None:
                pass
            elif not isinstance(response, str) or not response.strip():
                errors.append(f"Template '{tpl_id}': Missing or empty 'response' text.")

    return len(errors) == 0, errors


def run_all_validations() -> bool:
    """Runs all schema checks and returns True if all pass."""
    print("=" * 60)
    print("Camellia Artisanal Schema & Configuration Integrity Check")
    print("=" * 60)
    
    menu_ok, menu_errors = validate_menu_schema()
    qa_ok, qa_errors = validate_qa_templates_schema()

    if menu_ok:
        print("[PASS] menu.json schema is valid.")
    else:
        print("[FAIL] menu.json validation errors:")
        for err in menu_errors:
            print(f"  - {err}")

    if qa_ok:
        print("[PASS] qa_templates.json schema is valid.")
    else:
        print("[FAIL] qa_templates.json validation errors:")
        for err in qa_errors:
            print(f"  - {err}")

    all_passed = menu_ok and qa_ok
    print("-" * 60)
    if all_passed:
        print("ALL CONFIGURATION SCHEMAS VALIDATED SUCCESSFULLY.")
    else:
        print("SCHEMA VALIDATION FAILED.")
    print("=" * 60)
    return all_passed

if __name__ == "__main__":
    success = run_all_validations()
    sys.exit(0 if success else 1)
