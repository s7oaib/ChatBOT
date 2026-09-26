import io
import re
import json
import time
import base64
import httpx
from typing import Dict, Any, List, Optional, Tuple
from camellia.config import config
from camellia.monitoring import get_logger

log = get_logger("camellia.menu_parser")

NON_VEG_KEYWORDS = [
    "mutton", "chicken", "fish", "egg", "non-veg", "non veg", "prawn", 
    "mangsho", "kosha", "cutlet", "kalia", "chingri", "machh", "biryani", "bhetki", "paturi"
]

# A menu upload is one deliberate action by the operator, not a per-message
# hot path, so it can afford to try again — unlike the reply path, where a
# retry is paid for by a customer waiting. That difference is why these
# numbers are far more generous than ai_responder's.
#
# The failure this exists for is Groq's own JSON mode giving up:
#   400 {"code": "json_validate_failed", "failed_generation": ""}
# It is transient. The same flyer, byte for byte, failed once and parsed on
# the next attempt — which is exactly why the single-shot version made an
# upload look like a broken feature roughly one time in three.
_VISION_MAX_ATTEMPTS = 3
_VISION_RETRY_BACKOFF_SECONDS = 1.5
# A ceiling is needed — a truncated completion is invalid JSON, which looks
# identical to the model failing the schema — but it must stay small.
#
# Groq bills max_tokens as *reserved* budget against the per-minute limit, not
# as what the reply actually costs. The free tier allows 8000 TPM and a flyer
# photo is already ~5000 of those, so an over-generous reservation turns a
# request that would have succeeded into a guaranteed 429. A first pass at
# this used 4096 and made uploads fail that had previously worked.
#
# 2048 was still too high, for a reason the TPM number hides: Groq caps
# *output* separately, and on qwen/qwen3.6-27b that output-token ceiling is 1000.
# Reserving 2048 against a 1000 ceiling is rejected before a single token is
# generated — "Request too large ... on output tokens per minute:
# Limit 1000". Every full-flyer upload therefore failed, saved the photo, and
# left the dish text on the previous week. Confirmed by replaying the
# client's own flyer: 400 json_validate_failed, then two 429s, then give up.
#
# So this must stay under 1000, and the prompt must keep the reply small
# enough to finish inside it — hence the short-description rule below.
_VISION_MAX_TOKENS = 900
_VISION_TIMEOUT_SECONDS = 60.0

# A 429 here is the per-minute token window being spent, not a fault, and it
# refills on a clock rather than immediately. Retrying 1.5s later just spends
# another attempt on the same exhausted minute, so quota failures wait longer
# and honour the provider's own hint when it sends one. Capped because the
# operator is holding an open HTTP request while this runs — better to hand
# back a clear "try again in a minute" than to hang the admin page.
_VISION_QUOTA_BACKOFF_SECONDS = 8.0
_VISION_MAX_BACKOFF_SECONDS = 20.0

# Retrying a bad key or a model the account cannot serve changes nothing.
_VISION_FATAL_STATUSES = (401, 403, 404)


def _is_retryable_vision_failure(status_code: int, body: str) -> bool:
    """
    Whether another attempt at the same image could plausibly succeed.

    400 is normally a configuration error, but Groq returns it for
    json_validate_failed too — the model simply produced something that did
    not parse. That one is worth retrying; a malformed request is not.
    """
    if status_code in _VISION_FATAL_STATUSES:
        return False
    if status_code in (429, 500, 502, 503, 520, 522, 524):
        return True
    if status_code == 400:
        return "json_validate_failed" in (body or "")
    return False


# A flyer straight off a phone is a few hundred KB, and base64 of that is
# roughly 5000 of the 8000 tokens a minute the free tier allows — most of the
# budget spent on resolution the model does not need to read printed text.
# Shrinking it first is what stops quota failures happening at all, rather
# than retrying around them.
#
# 1024px on the long edge keeps menu text comfortably legible; below that,
# prices start to smear on a densely printed flyer.
_VISION_MAX_EDGE_PX = 1024
_VISION_JPEG_QUALITY = 85


def _downscale_for_vision(image_bytes: bytes, mime_type: str) -> Tuple[bytes, str]:
    """
    A smaller copy of the flyer for the vision model.

    Only ever used for the model call — the photo customers are sent is saved
    from the original bytes at full quality, because that one is looked at by
    people rather than parsed by a model.

    Falls back to the original on any problem, including Pillow being absent.
    A failure to shrink an image must never fail an upload that would
    otherwise have worked.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        log.warning("Pillow is not installed; sending the flyer at full size.")
        return image_bytes, mime_type

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            # Phones record orientation in EXIF rather than rotating pixels, so
            # a flyer shot in portrait can reach the model on its side. The
            # model reads it far worse that way, and the operator sees an
            # extraction failure with no clue why.
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            if max(img.size) > _VISION_MAX_EDGE_PX:
                img.thumbnail((_VISION_MAX_EDGE_PX, _VISION_MAX_EDGE_PX), Image.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=_VISION_JPEG_QUALITY, optimize=True)
        shrunk = buffer.getvalue()
    except Exception as e:
        log.warning("Could not downscale the flyer, sending it as uploaded: %s", e)
        return image_bytes, mime_type

    if len(shrunk) >= len(image_bytes):
        # Already small, or re-encoding gained nothing. Sending the original
        # avoids a pointless extra generation of JPEG loss.
        return image_bytes, mime_type

    log.info(
        "Flyer downscaled for the vision model: %.0fKB -> %.0fKB",
        len(image_bytes) / 1024, len(shrunk) / 1024,
    )
    return shrunk, "image/jpeg"


def _vision_backoff_seconds(resp: Any, status_code: int) -> float:
    """How long to wait before the next attempt, honouring Retry-After."""
    hint = None
    try:
        raw = resp.headers.get("retry-after") if resp is not None else None
        if raw:
            hint = float(raw)
    except (AttributeError, TypeError, ValueError):
        hint = None
    if hint is None:
        hint = _VISION_QUOTA_BACKOFF_SECONDS if status_code == 429 else _VISION_RETRY_BACKOFF_SECONDS
    return max(0.0, min(hint, _VISION_MAX_BACKOFF_SECONDS))


def _extract_json_object(content: str) -> Optional[Dict[str, Any]]:
    """
    The first complete JSON object in a model reply.

    Used for the last attempt, which asks without response_format. Models that
    trip strict JSON mode will still usually emit a perfectly good object
    wrapped in prose or a ```json fence, and throwing that away because of the
    wrapper would fail an upload we can actually read.
    """
    if not content:
        return None
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    if start == -1:
        return None
    # Walk to the matching brace rather than regexing: descriptions routinely
    # contain braces-free prose but the object itself nests.
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


DESCRIPTION_SECTIONS = (
    "vegetarian",
    "non_vegetarian",
    "bengali_bento",
    "norom_pak_sondesh",
)


class MenuParser:
    """
    Auto-extracts structured menu data (Items, Descriptions, Quantities, Prices, Categories)
    from raw unstructured text or uploaded menu card photos / flyers.

    Text is parsed deterministically by regex, which costs nothing and needs no
    provider. Photos need a model that can see, and that is Groq — the same key
    and endpoint the bot already uses for replies.
    """

    def parse_menu_text(self, raw_text: str) -> Dict[str, Any]:
        """Parses raw menu text into structured vegetarian, non-vegetarian, bento, and sweet item lists."""
        if not raw_text or not raw_text.strip():
            return {"vegetarian": [], "non_vegetarian": []}

        # Deterministic by design: pasted text needs no model, so this path
        # costs nothing, cannot hallucinate a dish, and works with no key set.
        return self._parse_with_regex(raw_text)



    def _parse_with_regex(self, raw_text: str) -> Dict[str, Any]:
        """Extract only printed facts. Ambiguous lines remain reviewable."""
        result = {key: [] for key in ("vegetarian", "non_vegetarian", "bengali_bento", "norom_pak_sondesh", "unclassified")}
        section = "unclassified"
        headings = {"vegetarian": "vegetarian", "veg": "vegetarian",
                    "non-veg": "non_vegetarian", "non vegetarian": "non_vegetarian",
                    "non-vegetarian": "non_vegetarian", "bento": "bengali_bento", "bento menu": "bengali_bento",
                    "sweets": "norom_pak_sondesh", "sondesh": "norom_pak_sondesh"}
        for line in raw_text.splitlines():
            line = re.sub(r"^\s*[•*\-]\s*", "", line).strip()
            if not line:
                continue
            heading = re.sub(r"\s+items?$", "", line.lower().rstrip(":").strip())
            if heading in headings:
                section = headings[heading]
                continue
            # A quantity is never a price. Bare trailing numbers are ambiguous.
            price = re.search(r"(?:₹|rs\.?|inr)\s*(-?\d[\d,]*(?:\.\d{1,2})?)|(-?\d[\d,]*(?:\.\d{1,2})?)\s*/-|(?:\s+-\s+)(-?\d[\d,]*(?:\.\d{1,2})?)\s*$", line, re.I)
            qty = re.search(r"\b\d+(?:\.\d+)?\s*(?:kg|g|pcs|pieces|box|pack)\b", line, re.I)
            name = line
            for match in sorted([m for m in (price, qty) if m], key=lambda m: m.start(), reverse=True):
                name = name[:match.start()] + name[match.end():]
            amount = next((value for value in price.groups() if value is not None), None) if price else None
            amount = amount.replace(",", "") if amount else None
            result[section].append({"item": name.strip(" ()|:-,"),
                "description": "", "quantity": qty.group(0) if qty else "",
                "price_inr": float(amount) if amount else None})
        return result

    def parse_and_update_menu(self, raw_text: str) -> Dict[str, Any]:
        """Compatibility extraction name; never writes or publishes."""
        return self._apply_extracted_data_to_menu(self.parse_menu_text(raw_text))

    def parse_image_and_update_menu(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> Dict[str, Any]:
        """
        Multimodal OCR & Data Extraction:
        Reads a menu card photo / flyer with a Groq vision model, converts it
        to JSON, updates menu.json, and hot-reloads.
        """
        if not image_bytes:
            return {"status": "failed", "reason": "No image data received."}

        if config.GROQ_ENABLED and config.GROQ_API_KEY:
            res = self._parse_image_with_groq(image_bytes, mime_type)
            if res.get("status") == "success":
                return res
            return res

        log.warning("GROQ_API_KEY is not set, so photo OCR cannot run.")
        return {
            "status": "warning",
            "reason": "Vision model not configured for live image OCR.",
            "message": "Image received successfully. Set GROQ_API_KEY — the same key the bot already uses for replies — to extract dishes from photos."
        }


    def _parse_image_with_groq(self, image_bytes: bytes, mime_type: str) -> Dict[str, Any]:
        """
        Uses a Groq multimodal model to read a menu photo.

        Tried before the others because it reuses GROQ_API_KEY, which the bot
        already needs for its replies — so menu photo OCR costs no second
        provider account. Groq's chat model has no vision, but its multimodal
        models take images on the same OpenAI-compatible endpoint.
        """
        image_bytes, mime_type = _downscale_for_vision(image_bytes, mime_type)
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        data_uri = f"data:{mime_type};base64,{b64_image}"
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {config.GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        # Every key is named on its own line, and the date is asked for first
        # and read first. Against the client's real flyer, a version that
        # described the schema as one dense line with the date mentioned
        # mid-paragraph returned only {"vegetarian": [...]} — the model
        # answered the part of the task it noticed and silently dropped the
        # rest, including a date printed in large type across the header.
        prompt = (
            "You are an OCR data extraction AI reading a printed food menu flyer.\n\n"
            "Return ONE JSON object containing ALL SIX of these keys, every time:\n"
            '  "week_of"            - the date range printed near the top of the flyer, copied verbatim\n'
            '                         (e.g. "28th August - 3rd September, 2026" or "19, 20, 21 June").\n'
            "                         Use null ONLY if no date is printed anywhere. Never invent one.\n"
            '  "service_window"     - e.g. "Friday Lunch - Thursday Dinner", or null if not printed.\n'
            '  "vegetarian"         - list of vegetarian dishes.\n'
            '  "non_vegetarian"     - list of non-vegetarian dishes.\n'
            '  "bengali_bento"      - list of bento/combo boxes, or [].\n'
            '  "norom_pak_sondesh"  - list of sweets/sondesh, or [].\n\n'
            'Every dish is {"item": str, "price_inr": int, "quantity": str}.\n'
            "Copy only visible text. Never infer or fill missing facts. Use null for missing price and blank for missing quantity/item.\n"
            # No description is asked for, and that is deliberate. The whole
            # reply has to land inside _VISION_MAX_TOKENS, which sits under
            # the model's 1000 output-tokens-per-minute ceiling. Descriptions
            # are most of the text on a flyer: with them, a full ~15-dish menu
            # ran to 825+ output tokens and the JSON never closed, so every
            # upload died on json_validate_failed and burned the minute. Names
            # and prices alone come in around half that. Descriptions are
            # filled in afterwards by _apply_extracted_data_to_menu.
            "Add no keys beyond those three. Output compact JSON with no line breaks\n"
            "and no indentation.\n\n"
            "Read the flyer header FIRST and fill in week_of before extracting any dishes.\n"
            "Return strict JSON with all six keys present."
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
            # The vision model is a reasoning model, and its thinking tokens
            # are billed as output. Left on, it spent 700-800 tokens thinking
            # about a menu flyer, hit the 1000 output-tokens-per-minute
            # ceiling before writing any JSON, and came back as HTTP 400
            # json_validate_failed with an empty failed_generation - the
            # validator rejecting a reply that was never written. Every client
            # upload failed that way: photo saved, dishes never updated. With
            # reasoning off the same flyer's date costs 19 tokens.
            "reasoning_effort": "none",
        }
        payload["max_tokens"] = _VISION_MAX_TOKENS

        last_reason = "Groq vision parsing did not return items."
        for attempt in range(1, _VISION_MAX_ATTEMPTS + 1):
            # The final attempt drops strict JSON mode and parses the object
            # out of whatever comes back. When the model keeps tripping the
            # schema validator it is usually still producing readable JSON,
            # just wrapped — and an upload that can be read should not fail.
            lenient = attempt == _VISION_MAX_ATTEMPTS
            backoff = _VISION_RETRY_BACKOFF_SECONDS
            if lenient:
                payload.pop("response_format", None)
            try:
                with httpx.Client(timeout=_VISION_TIMEOUT_SECONDS) as client:
                    resp = client.post(url, headers=headers, json=payload)

                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    data = _extract_json_object(content)
                    if data is None:
                        last_reason = "Vision model returned no readable JSON."
                        backoff = _VISION_RETRY_BACKOFF_SECONDS
                        log.warning(
                            "Groq vision attempt %d/%d returned unparseable content.",
                            attempt, _VISION_MAX_ATTEMPTS,
                        )
                    else:
                        if attempt > 1:
                            log.info(
                                "Groq vision succeeded on attempt %d/%d%s.",
                                attempt, _VISION_MAX_ATTEMPTS,
                                " (without strict JSON mode)" if lenient else "",
                            )
                        return self._apply_extracted_data_to_menu(data)
                else:
                    body = resp.text or ""
                    # Say which of the two it was. "Could not read the flyer"
                    # sends the operator hunting for a clearer photo; a spent
                    # token quota just needs a minute, and the photo they
                    # uploaded is already live either way.
                    last_reason = (
                        "The vision model's per-minute quota is spent. Your published menu is "
                        "unchanged; try extraction again in a minute."
                        if resp.status_code == 429
                        else f"Vision model HTTP {resp.status_code}."
                    )
                    log.warning(
                        "Groq vision HTTP %s on attempt %d/%d (model %r)",
                        resp.status_code, attempt, _VISION_MAX_ATTEMPTS,
                        config.GROQ_VISION_MODEL,
                    )
                    if not _is_retryable_vision_failure(resp.status_code, body):
                        return {"status": "failed", "reason": last_reason}
                    backoff = _vision_backoff_seconds(resp, resp.status_code)
            except Exception as e:
                last_reason = str(e)
                backoff = _VISION_RETRY_BACKOFF_SECONDS
                log.warning(
                    "Groq vision attempt %d/%d failed: %s",
                    attempt, _VISION_MAX_ATTEMPTS, e,
                )

            if attempt < _VISION_MAX_ATTEMPTS:
                time.sleep(backoff)

        log.error(
            "Groq vision gave up after %d attempts: %s",
            _VISION_MAX_ATTEMPTS, last_reason,
        )
        return {"status": "failed", "reason": last_reason}


    def _apply_extracted_data_to_menu(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize provider output to an UNAPPROVED extraction, never live data."""
        keys = ("vegetarian", "non_vegetarian", "bengali_bento", "norom_pak_sondesh", "unclassified")
        parsed = {}
        for key in keys:
            entries = data.get(key) or []
            if not isinstance(entries, list):
                return {"status": "failed", "reason": "Menu details need manual review."}
            parsed[key] = [
                {field: row.get(field, "" if field != "price_inr" else None)
                 for field in ("item", "description", "quantity", "price_inr")}
                for row in entries if isinstance(row, dict)
            ]
        total = sum(len(rows) for rows in parsed.values())
        return {"status": "success" if total else "failed", "parsed": parsed,
                "week_of": data.get("week_of") or "", "service_window": data.get("service_window") or "",
                "extracted_items_count": total, "published": False,
                "message": "Review and approve these details before publishing."}


menu_parser = MenuParser()
