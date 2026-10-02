"""Optional ordered Custom Q&A replies referencing eligible catalogue assets."""
import json
import re
from camellia import menu_catalogue, menu_images


def validate(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 30:
        raise ValueError("Response sequence must contain at most 30 blocks")
    clean = []
    for block in value:
        if not isinstance(block, dict):
            raise ValueError("Invalid response block")
        if block.get("type") == "text" and set(block) <= {"type", "text"}:
            text = block.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 10000:
                raise ValueError("Text blocks require nonempty text (maximum 10000 characters)")
            clean.append({"type": "text", "text": text})
        elif block.get("type") == "menu" and set(block) <= {"type", "menu_id"}:
            menu_id = block.get("menu_id")
            if not isinstance(menu_id, str) or not any(m["menu_id"] == menu_id for m in menu_catalogue.list_menus(include_inactive=False)):
                raise ValueError("Select an existing catalogue menu/image")
            if not menu_images.current_url(menu_id):
                raise ValueError("Selected menu/image is unavailable or not customer-eligible")
            clean.append({"type": "menu", "menu_id": menu_id})
        else:
            raise ValueError("Only text or catalogue menu blocks are supported; arbitrary URLs are forbidden")
    return clean


def resolve(value):
    """Resolve the entire reply first; never send a partial/stale sequence."""
    blocks = validate(json.loads(value) if isinstance(value, str) else value)
    result = []
    for block in blocks:
        if block["type"] == "text":
            result.append({"kind": "TEXT", "text": block["text"]})
        else:
            url = menu_images.current_url(block["menu_id"])
            if not url:
                raise ValueError("Selected menu/image is unavailable")
            result.append({"kind": "IMAGE", "url": url, "menu_id": block["menu_id"]})
    return result


def attachment_warning(answer, blocks):
    text = answer + " " + " ".join(b.get("text", "") for b in blocks)
    promised = re.search(r"(?:attached|sharing|sending).{0,60}(?:this week|weekly).{0,30}menu", text, re.I | re.S)
    weekly = any(b.get("type") == "menu" and b.get("menu_id") in {"weekly_vegetarian", "weekly_non_vegetarian"} for b in blocks)
    return ["Text promises this week's menu but no eligible Weekly Menu block is configured."] if promised and not weekly else []
