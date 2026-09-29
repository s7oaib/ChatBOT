"""Reviewed publication on the existing menu drafts, versions and image tables.

menu.json remains the legacy baseline. Approved version overlays are canonical
for reviewed menus, so SQLite can atomically switch facts AND the flyer. No
cross-filesystem transaction or startup rewrite of production JSON is needed.
"""
import copy
import hashlib
import io
import json
import re
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PIL import Image, ImageOps

from camellia.config import config
from camellia.database import db
from camellia import menu_dates, menu_images, weekly_flyer, weekly_savoury
from camellia.menu_parser import OCR_DIAGNOSTIC_CODES, menu_parser

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 150
FIELDS = ("item", "category", "quantity", "price_inr", "description")
SECTIONS = ("vegetarian", "non_vegetarian", "bengali_bento", "norom_pak_sondesh")
FAILURE_MESSAGE = "We couldn't read this menu reliably. Your current published menu is unchanged. Try again or enter the details manually."


class ReviewConflict(ValueError):
    pass


def _key(key):
    key = menu_images._key(key)
    # Weekly Savoury is an internal, durable timeline. Production may have
    # deleted the retired Weekly Vegetarian catalogue card (and tombstoned
    # it), so its review state must not depend on recreating that card.
    if key == weekly_savoury.WEEKLY_KEY:
        return key
    if not db.get_menu_catalogue_entry(key):
        raise KeyError(key)
    return key


def _private_dir():
    # Outside /uploads, which is publicly mounted by the application.
    path = Path(config.MENU_JSON_PATH).resolve().parent / "menu-review-private"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _audit(conn, draft, action, actor, details=None):
    conn.execute("INSERT INTO menu_review_audit(draft_id,menu_key,action,actor,details_json) VALUES(?,?,?,?,?)",
                 (draft["draft_id"], draft["menu_key"], action, actor, _dump(details or {})))
    from camellia.monitoring import get_logger
    # Transaction attempts are not claims of committed publication. Durable
    # menu_review_audit remains the source of truth after transaction commit.
    get_logger("camellia.menu_review").info("menu.review.audit_attempt action=%s", action)


def _store(conn, draft, actor):
    conn.execute("""INSERT INTO menu_drafts(menu_key,draft_json,review_json,updated_by)
        VALUES(?,?,?,?) ON CONFLICT(menu_key) DO UPDATE SET draft_json=excluded.draft_json,
        review_json=excluded.review_json,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP""",
        (draft["menu_key"], _dump(draft["rows"]), _dump(draft), actor))


def get_draft(key, conn=None):
    if conn is None:
        with db.get_connection() as connection:
            return get_draft(key, connection)
    row = conn.execute("SELECT review_json FROM menu_drafts WHERE menu_key=?", (_key(key),)).fetchone()
    if not row or not row[0]:
        return None
    draft = json.loads(row[0])
    draft.setdefault("diagnostic_codes", [])
    return draft


def pending_review_keys(conn=None):
    """Menu identities with a newer, unapproved upload than their live version."""
    if conn is None:
        with db.get_connection() as connection:
            return pending_review_keys(connection)
    pending = set()
    for row in conn.execute("SELECT menu_key, review_json FROM menu_drafts WHERE review_json IS NOT NULL"):
        draft = json.loads(row["review_json"])
        if draft.get("status") in {"PUBLISHED", "REJECTED"}:
            continue
        key = row["menu_key"]
        if key != weekly_savoury.WEEKLY_KEY:
            catalogue = conn.execute(
                "SELECT active FROM menu_catalogue WHERE menu_id=?", (key,)
            ).fetchone()
            if not catalogue or not catalogue[0]:
                continue
        live = conn.execute(
            "SELECT approval_json FROM menu_versions WHERE menu_key=? AND is_current=1 "
            "AND approval_json IS NOT NULL ORDER BY id DESC LIMIT 1", (key,)
        ).fetchone()
        approved_at = json.loads(live[0]).get("approved_at") if live else None
        if not approved_at or str(draft.get("created_at") or "") > approved_at:
            pending.add(key)
    return pending


def _safe_diagnostics(result):
    """Persist only known symbolic codes, never provider content or exceptions."""
    return list(dict.fromkeys(code for code in result.get("diagnostic_codes", [])
                              if isinstance(code, str) and code in OCR_DIAGNOSTIC_CODES))


def _metadata(values):
    result = {}
    for field in ("week_of", "service_window"):
        value = values.get(field) or ""
        if not isinstance(value, str) or len(value) > 160:
            raise ValueError("Menu date/service window must be at most 160 characters.")
        result[field] = value.strip()
    return result


def _rows(parsed, page=1, provenance="EXTRACTED"):
    result = []
    for section in (*SECTIONS, "unclassified"):
        for row in parsed.get(section) or []:
            if not isinstance(row, dict):
                continue
            result.append({**{f: row.get(f, "") for f in FIELDS},
                "row_id": uuid.uuid4().hex, "source_page": page,
                "category": section if section != "unclassified" else "",
                "provenance": {f: provenance for f in FIELDS}})
    return result


def categories(key):
    entry = db.get_menu_catalogue_entry(_key(key))
    if _key(key) in ("weekly_vegetarian", "weekly_non_vegetarian", "bento"):
        return list(SECTIONS)
    return list(dict.fromkeys([*SECTIONS, entry["category"]]))


def validate(rows, key, strict=False):
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError(f"Use at most {MAX_ROWS} menu rows.")
    errors = []
    if not rows:
        errors.append("Add at least one item.")
    seen = set()
    result = []
    allowed = categories(key)
    for index, source in enumerate(rows):
        if not isinstance(source, dict) or set(source) - set((*FIELDS, "row_id", "source_page", "warnings", "provenance", "aliases")):
            raise ValueError("Unrecognized menu row schema.")
        row = copy.deepcopy(source)
        warnings = []
        aliases = row.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 12 or any(not isinstance(alias, str) or len(alias) > 120 for alias in aliases):
            raise ValueError("Use at most 12 short menu-item aliases.")
        row["aliases"] = list(dict.fromkeys(alias.strip() for alias in aliases if alias.strip()))
        for f in FIELDS:
            if f != "price_inr":
                if not isinstance(row.get(f, ""), (str, type(None))):
                    raise ValueError("Menu text fields must be text.")
                row[f] = (row.get(f) or "").strip()
                if len(row[f]) > (1200 if f == "description" else 160):
                    raise ValueError("A menu field is too long.")
        if not row["item"]:
            warnings.append("Missing item name")
        if row["category"] not in allowed:
            warnings.append("Unknown category")
        try:
            raw_price = row.get("price_inr")
            if isinstance(raw_price, bool):
                raise InvalidOperation
            price = Decimal(str(raw_price))
            if not price.is_finite() or price <= 0 or price > 1000000 or price.as_tuple().exponent < -2:
                raise InvalidOperation
            row["price_inr"] = int(price) if price == int(price) else float(price)
        except (InvalidOperation, ValueError, TypeError):
            warnings.append("Missing or invalid price")
        identity = (row["item"].casefold(), row["quantity"].casefold())
        if identity in seen:
            warnings.append("Possible duplicate item and quantity")
        seen.add(identity)
        errors.extend(f"Row {index + 1}: {w}" for w in warnings)
        if not row["quantity"]:
            warnings.append("Missing quantity — leave blank only if not stated")
        row["warnings"] = warnings
        result.append(row)
    canonical_names = {}
    for index, row in enumerate(result):
        canonical_names.setdefault(weekly_savoury._name(row.get("item")), set()).add(index)
    alias_owners = {}
    for index, row in enumerate(result):
        for alias in row["aliases"]:
            normalized = weekly_savoury._name(alias)
            if normalized and (canonical_names.get(normalized, {index}) != {index}
                               or alias_owners.setdefault(normalized, index) != index):
                errors.append("An item alias matches more than one menu row.")
    if strict and errors:
        raise ValueError("; ".join(errors))
    return result, errors


def _safe_image(data, mime):
    extension = menu_images.validate_image(data, mime)
    with Image.open(io.BytesIO(data)) as image:
        original_format = image.format
        original_mime = Image.MIME.get(original_format)
        if original_mime != mime or image.width * image.height > 25000000:
            raise ValueError("Image type or dimensions are not supported.")
        # Apply the original EXIF rotation before metadata-stripping re-encoding.
        oriented = ImageOps.exif_transpose(image)
        oriented.load()
        output = io.BytesIO()
        oriented.save(output, format=original_format,
                      **({"quality": 100, "subsampling": 0} if original_format == "JPEG" else {}))
    return output.getvalue(), extension


def _pdf_extract(data, weekly=False):
    import pypdfium2 as pdfium
    if not data.startswith(b"%PDF-"):
        raise ValueError("Not a valid PDF.")
    rows, texts, previews, warnings, diagnostics = [], [], [], [], []
    with pdfium.PdfDocument(data) as document:
        if not 1 <= len(document) <= 8:
            raise ValueError("PDF must contain 1–8 pages.")
        for index in range(len(document)):
            with closing(document[index]) as page:
                width, height = page.get_size()
                if min(width, height) <= 0 or max(width, height) > 14400:
                    raise ValueError("PDF page dimensions are not supported.")
                with closing(page.get_textpage()) as textpage:
                    if textpage.count_chars() > 50000:
                        raise ValueError("PDF page contains too much text.")
                    text = textpage.get_text_range()
                texts.append(text)
                with closing(page.render(scale=min(2, 1800 / max(width, height)), may_draw_forms=False)) as bitmap:
                    preview = bitmap.to_pil().convert("RGB")
                previews.append(preview)
                parsed = menu_parser.parse_menu_text(text) if text.strip() else {}
                provenance = "TEXT_PARSED" if weekly else "EXTRACTED"
                if not any(parsed.values()):
                    buffer = io.BytesIO()
                    preview.save(buffer, format="PNG")
                    result = menu_parser.parse_image_and_update_menu(buffer.getvalue(), "image/png")
                    diagnostics.extend(_safe_diagnostics(result))
                    if result.get("status") == "success":
                        parsed = result["parsed"]
                        provenance = "OCR_EXTRACTED" if weekly else "EXTRACTED"
                    else:
                        # Keep the private page preview so an operator can
                        # transcribe it; extraction failure must not discard
                        # the source or make it appear publishable.
                        warnings.append(f"Page {index + 1} needs manual review.")
                rows.extend(_rows(parsed, index + 1, provenance))
    # A single lossless page strip is compatible with existing image sending.
    canvas = Image.new("RGB", (max(p.width for p in previews), sum(p.height for p in previews)), "white")
    y = 0
    for preview in previews:
        canvas.paste(preview, (0, y))
        y += preview.height
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return rows, "\n\n".join(texts), output.getvalue(), warnings, list(dict.fromkeys(diagnostics))


def extract(key, actor, *, text=None, data=None, mime=None, filename="", weekly=False, retry_from=None):
    key = _key(key)
    if weekly and key != weekly_savoury.WEEKLY_KEY:
        raise ValueError("Weekly Savoury uses the existing weekly menu identity.")
    if not weekly and key == weekly_savoury.WEEKLY_KEY and not db.get_menu_catalogue_entry(key):
        raise KeyError(key)
    if text is not None:
        if not isinstance(text, str) or not text.strip() or len(text) > 50000:
            raise ValueError("Paste 1–50,000 characters of menu text.")
        source = text.encode("utf-8")
        source_type = "text"
    else:
        if not data or len(data) > MAX_BYTES:
            raise ValueError("Choose a menu file of at most 10MB.")
        mime = (mime or "").split(";", 1)[0].lower()
        if mime not in (*menu_images.ALLOWED_TYPES, "application/pdf"):
            raise ValueError("Use JPG, PNG, WEBP or PDF.")
        source, source_type = data, "pdf" if mime == "application/pdf" else "image"
    draft = {"draft_id": uuid.uuid4().hex, "menu_key": key, "source_type": source_type,
        "source_hash": hashlib.sha256(source).hexdigest(),
        "original_filename": str(filename).replace("\\", "/").rsplit("/", 1)[-1][:160],
        "created_at": datetime.now(timezone.utc).isoformat(), "created_by": actor,
        "revision": 1, "status": "EXTRACTING", "rows": [], "warnings": [],
        "diagnostic_codes": [],
        "source_asset": None, "flyer_asset": None, "raw_text": "", "menu_metadata": _metadata({}),
        "weekly_mode": bool(weekly), "weekly_dates": None,
        "parser": {"version": 1, "provider": "deterministic" if source_type == "text" else "pdfium/groq",
                   "model": config.GROQ_VISION_MODEL if source_type != "text" else None}}
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = get_draft(key, conn)
        if retry_from is not None:
            _check(prior, *retry_from)
            _audit(conn, draft, "extraction_retried", actor, {"previous_draft_id": prior["draft_id"]})
        elif prior and prior["status"] not in ("REJECTED", "PUBLISHED"):
            raise ReviewConflict("A draft already exists. Review or reject it first.")
        _store(conn, draft, actor)
        _audit(conn, draft, "draft_created", actor)
        conn.commit()
    try:
        if source_type == "text":
            draft["raw_text"] = text
            draft["rows"] = _rows(menu_parser.parse_menu_text(text),
                                  provenance="TEXT_PARSED" if weekly else "EXTRACTED")
        else:
            if source_type == "pdf":
                rows, raw, flyer, pdf_warnings, pdf_diagnostics = _pdf_extract(source, weekly=weekly)
                draft["rows"], draft["raw_text"] = rows, raw
                draft["warnings"].extend(pdf_warnings)
                draft["diagnostic_codes"] = pdf_diagnostics
                extension = "png"
                original = source
            else:
                original, extension = _safe_image(source, mime)
                flyer = original
                # Retain a private preview even if the provider fails.
                source_name = draft["draft_id"] + "." + extension
                (_private_dir() / source_name).write_bytes(original)
                draft["source_asset"] = source_name
                draft["flyer_asset"] = source_name
                result = menu_parser.parse_image_and_update_menu(original, mime)
                draft["diagnostic_codes"] = _safe_diagnostics(result)
                if result.get("status") != "success":
                    # A provider failure is an incomplete draft, not a reason
                    # to fall back to the older menu or lose the private source.
                    draft["warnings"].append(FAILURE_MESSAGE)
                else:
                    draft["rows"] = _rows(result["parsed"],
                                          provenance="OCR_EXTRACTED" if weekly else "EXTRACTED")
                    draft["menu_metadata"] = _metadata(result)
            source_name = draft["draft_id"] + (".pdf" if source_type == "pdf" else "." + extension)
            (_private_dir() / source_name).write_bytes(original)
            flyer_name = draft["draft_id"] + "-flyer." + extension
            (_private_dir() / flyer_name).write_bytes(flyer)
            draft["source_asset"], draft["flyer_asset"] = source_name, flyer_name
        draft["rows"], validation_warnings = validate(draft["rows"], key)
        draft["warnings"] = list(dict.fromkeys([*draft["warnings"], *validation_warnings]))
        if weekly:
            printed = draft["menu_metadata"].get("week_of") or draft["raw_text"]
            parsed_dates = menu_dates.parse_week_date_range(printed)
            if parsed_dates:
                draft["weekly_dates"] = {"valid_from": parsed_dates[0].isoformat(),
                    "valid_until": parsed_dates[1].isoformat(),
                    "date_source": "OCR_EXTRACTED" if source_type == "image" else "TEXT_PARSED"}
        draft["status"] = "NEEDS_REVIEW"
    except Exception as exc:
        # Raw provider errors may contain private content; never return them.
        # The already-persisted review draft remains visible and actionable;
        # approval still requires the operator to review its empty/partial rows.
        draft["status"], draft["warnings"] = "NEEDS_REVIEW", [FAILURE_MESSAGE]
        draft["rows"] = []
        draft["diagnostic_codes"] = list(dict.fromkeys([
            *draft.get("diagnostic_codes", []), "EXTRACTION_EXCEPTION",
        ]))
        exception_type = type(exc).__name__
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", exception_type):
            draft["diagnostic_details"] = {"exception_type": exception_type}
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = get_draft(key, conn)
        if current["draft_id"] != draft["draft_id"] or current["status"] != "EXTRACTING":
            raise ReviewConflict("Draft changed while extraction was running.")
        _store(conn, draft, actor)
        _audit(conn, draft, "extraction_finished", actor, {"status": draft["status"]})
        conn.commit()
    return draft


def retry_extraction(key, draft_id, revision, actor):
    """Explicit reviewer retry of retained source; never publication or repair."""
    draft = get_draft(key)
    _check(draft, draft_id, revision)
    options = {"weekly": bool(draft.get("weekly_mode")), "retry_from": (draft_id, revision),
               "filename": draft.get("original_filename") or ""}
    if draft["source_type"] == "text":
        return extract(key, actor, text=draft.get("raw_text"), **options)
    asset = draft.get("source_asset")
    if not asset or Path(asset).name != asset:
        raise ValueError("Original source unavailable. Reject this draft and upload the source again.")
    path = _private_dir() / asset
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".pdf": "application/pdf"}.get(path.suffix.lower())
    if not mime or not path.is_file():
        raise ValueError("Original source unavailable. Reject this draft and upload the source again.")
    return extract(key, actor, data=path.read_bytes(), mime=mime, **options)


def _check(draft, draft_id, revision):
    if not draft or draft["draft_id"] != draft_id or draft["revision"] != revision:
        raise ReviewConflict("Draft changed. Reload before continuing.")
    if draft["status"] in ("PUBLISHED", "REJECTED", "EXTRACTING"):
        raise ReviewConflict("This draft cannot be edited or approved.")


def edit(key, draft_id, revision, rows, actor, metadata=None):
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        draft = get_draft(key, conn)
        _check(draft, draft_id, revision)
        previous = {r["row_id"]: r for r in draft["rows"]}
        if metadata is not None:
            draft["menu_metadata"] = _metadata(metadata)
        cleaned, errors = validate(rows, key)
        ids = set()
        for row in cleaned:
            old = previous.get(row.get("row_id"))
            row["row_id"] = old["row_id"] if old else uuid.uuid4().hex
            if row["row_id"] in ids:
                raise ValueError("Duplicate row identifier.")
            ids.add(row["row_id"])
            row["source_page"] = old.get("source_page") if old else None
            row["provenance"] = {f: old["provenance"][f] if old and row.get(f) == old.get(f)
                                  else "OPERATOR_EDITED" for f in FIELDS}
        draft.update(rows=cleaned, warnings=errors, revision=revision + 1,
                     status="NEEDS_REVIEW" if errors else "READY_TO_APPROVE")
        _store(conn, draft, actor)
        _audit(conn, draft, "draft_edited", actor, {"revision": draft["revision"]})
        conn.commit()
    return draft


def reject(key, draft_id, revision, actor):
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        draft = get_draft(key, conn)
        if not draft or draft["draft_id"] != draft_id or draft["revision"] != revision or draft["status"] == "PUBLISHED":
            raise ReviewConflict("Draft changed or was published.")
        draft.update(status="REJECTED", revision=revision + 1)
        _store(conn, draft, actor)
        _audit(conn, draft, "draft_rejected", actor)
        conn.commit()
    return draft


def current_versions(conn=None):
    if conn is None:
        with db.get_connection() as connection:
            return current_versions(connection)
    versions = [dict(r) for r in conn.execute("""SELECT v.* FROM menu_versions v
        JOIN menu_catalogue c ON c.menu_id=v.menu_key
        WHERE v.is_current=1 AND v.approval_json IS NOT NULL AND c.active=1
        AND COALESCE(v.weekly_state,'') != 'SUPERSEDED'""")]
    versions = [version for version in versions if not (version.get("valid_from") or version.get("valid_until"))
                or weekly_savoury.version_status(version) == "CURRENT"]
    # More than one valid approved version of a menu is ambiguous, not a reason
    # to choose an arbitrary row. The weekly timeline has its own same rule.
    counts = {}
    for version in versions:
        counts[version["menu_key"]] = counts.get(version["menu_key"], 0) + 1
    versions = [version for version in versions if counts[version["menu_key"]] == 1]
    if not weekly_savoury.lifecycle_enabled(conn):
        return versions
    versions = [v for v in versions if v["menu_key"] not in weekly_savoury.LEGACY_WEEKLY_KEYS]
    weekly = weekly_savoury.current_version(conn=conn)
    if weekly:
        versions.append(weekly)
    return versions


def has_current(key):
    return any(v["menu_key"] == menu_images._key(key) for v in current_versions())


def customer_versions():
    """Shared customer allowlist. Admin history/current views stay independent."""
    pending = pending_review_keys()
    if pending & set(weekly_savoury.LEGACY_WEEKLY_KEYS):
        pending.update(weekly_savoury.LEGACY_WEEKLY_KEYS)
    with db.get_connection() as conn:
        linked = {row[0] for row in conn.execute(
            "SELECT menu_id FROM menu_catalogue WHERE customer_topic IS NOT NULL "
            "OR legacy_customer_eligible=1"
        )}
    # Weekly Savoury has a reviewed timeline identity independent of its
    # retired catalogue row; tombstoning that old row must not hide the week.
    linked.add(weekly_savoury.WEEKLY_KEY)
    return [version for version in current_versions()
            if version["menu_key"] not in pending and version["menu_key"] in linked]


def customer_blocked_keys():
    """Known retired/inactive/pending/non-current authorities cannot use baseline fallback."""
    with db.get_connection() as conn:
        reviewed = {row[0] for row in conn.execute("SELECT DISTINCT menu_key FROM menu_versions WHERE approval_json IS NOT NULL")}
        inactive = {row[0] for row in conn.execute("SELECT menu_id FROM menu_catalogue WHERE active=0")}
        deleted = {row[0] for row in conn.execute("SELECT menu_id FROM menu_deletion_tombstones")}
    pending = pending_review_keys()
    visible = {version["menu_key"] for version in customer_versions()}
    blocked = (reviewed - visible) | inactive | deleted | pending
    weekly_keys = set(weekly_savoury.LEGACY_WEEKLY_KEYS)
    if visible & weekly_keys or (not pending & weekly_keys and weekly_savoury.legacy_baseline_current()):
        # A future reviewed schedule does not hide the still-current legacy week.
        blocked -= weekly_keys
    else:
        blocked |= weekly_keys
    return blocked


def menu_question_keys(message, facts=None):
    """Exact identity for safety relevance only; never authorizes draft facts."""
    facts = facts or {}
    normalized = weekly_savoury._name(message)
    def contains(name):
        name = weekly_savoury._name(name)
        return bool(name and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", normalized))
    explicit = set()
    with db.get_connection() as conn:
        for stored in conn.execute("SELECT menu_key,approval_json FROM menu_versions WHERE approval_json IS NOT NULL"):
            for row in json.loads(stored["approval_json"]).get("rows", []):
                if any(contains(name) for name in [row.get("item"), *(row.get("aliases") or [])]):
                    explicit.add(stored["menu_key"])
        for stored in conn.execute("SELECT menu_key,review_json FROM menu_drafts WHERE review_json IS NOT NULL"):
            draft = json.loads(stored["review_json"])
            if draft.get("status") in {"PUBLISHED", "REJECTED"}:
                continue
            for row in draft.get("rows", []):
                if contains(row.get("item")):
                    explicit.add(stored["menu_key"])
        for menu in conn.execute("SELECT menu_id,name FROM menu_catalogue"):
            if contains(menu["name"]):
                explicit.add(menu["menu_id"])
    # Legacy dated rows identify safety relevance, never authorize an expired
    # price. Current authority still comes from the shared date/approval gate.
    try:
        with open(config.MENU_JSON_PATH, encoding="utf-8") as stream:
            weeks = json.load(stream).get("weekly_menus") or []
    except (OSError, ValueError, TypeError):
        weeks = []  # No weekly authority; unrelated standing facts remain independent.
    for week in weeks:
        for section in ("vegetarian", "non_vegetarian", "norom_pak_sondesh"):
            if any(contains(row.get("item")) for row in week.get(section) or []):
                explicit.update(weekly_savoury.LEGACY_WEEKLY_KEYS)
    if explicit:
        return explicit
    if re.search(r"\b(?:subscription|home table|bengali home)\b", normalized):
        return set()
    if re.search(r"\b(?:aamar|signature|sondesh|sandesh|mishti|kanchagolla|jolbhora)\b", normalized):
        return {"aamar_mishti", "signature"} if not re.search(r"\b(?:aamar|signature)\b", normalized) else {
            "signature" if "signature" in normalized else "aamar_mishti"}
    if re.search(r"\bbento\b", normalized):
        return {"bento"}
    if re.search(r"\b(?:weekly|savoury|this week)\b", normalized):
        return set(weekly_savoury.LEGACY_WEEKLY_KEYS)
    topic = str(facts.get("last_topic") or facts.get("active_topic") or "")
    if topic in {"bengali_home_table", "subscription"}:
        return set()
    if topic.startswith("sondesh"):
        return {"aamar_mishti", "signature"}
    if topic in {"bento", "aamar_mishti", "signature"}:
        return {topic}
    if topic == "savoury":
        return set(weekly_savoury.LEGACY_WEEKLY_KEYS)
    if topic and db.get_menu_catalogue_entry(topic):
        return {topic}
    if facts.get("source_context_superseded"):
        return set()
    route = facts.get("source_route") or {}
    source_topic = facts.get("source_topic") or {}
    key = route.get("menu_id") or source_topic.get("default_menu_id")
    return {key} if key and route.get("menu_mode") != "NONE" else set()


def menu_question_blocked(message, facts=None):
    """Stable service FAQs remain independent of volatile menu authority."""
    lowered = (message or "").casefold()
    if re.search(r"\b(?:historical|historic|archive|last week|previous week|last month|previous month|old menu|menu from)\b", lowered) \
            and not re.search(r"\b(?:price|cost|rate|quantity|portion|available)\b", lowered):
        return False  # Explicit historical asset requests are not current facts.
    if re.search(r"\b(?:delivery|deliver|pickup|pick up|how do i order|how to order)\b", lowered) and not re.search(
            r"\b(?:menu|this week|current|on the menu|price|prices|cost|rate|much|quantity|portion|available|availability|stock)\b", lowered):
        return False
    if not re.search(r"\b(?:price|prices|cost|rate|much|menu|quantity|portion|serving|available|availability|stock)\b", lowered):
        return False
    return bool(menu_question_keys(message, facts) & customer_blocked_keys())


def canonical_menu(path=None):
    with open(path or config.MENU_JSON_PATH, encoding="utf-8") as stream:
        menu = json.load(stream)
    visible_versions = customer_versions()
    visible_versions.sort(key=lambda version: version["menu_key"] in weekly_savoury.LEGACY_WEEKLY_KEYS)
    for version in visible_versions:
        approval = json.loads(version["approval_json"])
        rows = approval["rows"]
        key = version["menu_key"]
        if key == "bento" and weekly_savoury.lifecycle_enabled():
            weekly = menu.setdefault("weekly_menus", [])
            if not weekly:
                weekly.append({})
            weekly[-1]["bengali_bento"] = [{f: r[f] for f in ("item", "quantity", "price_inr", "description")}
                                           for r in rows if r["category"] == "bengali_bento"]
        elif key in ("weekly_vegetarian", "weekly_non_vegetarian", "bento"):
            weekly = menu.setdefault("weekly_menus", [])
            if not weekly:
                weekly.append({})
            for field in SECTIONS:
                if field == "bengali_bento" and key in weekly_savoury.LEGACY_WEEKLY_KEYS and approval.get("weekly_mode"):
                    # Bengali Bento is its own permanent catalogue menu.
                    continue
                weekly[-1][field] = [{f: r[f] for f in ("item", "quantity", "price_inr", "description")}
                                     for r in rows if r["category"] == field]
            weekly[-1].update(approval.get("menu_metadata") or _metadata({}))
            if key in weekly_savoury.LEGACY_WEEKLY_KEYS and approval.get("weekly_dates"):
                # The confirmed range, including an operator correction, is
                # the only date the responder may use for this version.
                weekly[-1]["week_of"] = approval["weekly_dates"]["display_date_range"]
            weekly[-1]["is_published"] = True
        else:
            collection_key = "signature_collection" if key == "signature" else key
            products = []
            for row in rows:
                products.append({"flavour": row["item"], "active": True,
                                 "prices": {row["quantity"]: row["price_inr"]}, "note": row["description"]})
            menu.setdefault("collections", {}).setdefault(collection_key, {})["products"] = products
    if weekly_savoury.lifecycle_enabled() and not any(
            version["menu_key"] in weekly_savoury.LEGACY_WEEKLY_KEYS for version in visible_versions) \
            and not weekly_savoury.legacy_baseline_current():
        # An expired or not-yet-started reviewed week must not expose the
        # older JSON rotation as a current fallback through any consumer.
        weekly = menu.setdefault("weekly_menus", [])
        if weekly:
            for section in ("vegetarian", "non_vegetarian", "norom_pak_sondesh"):
                weekly[-1][section] = []
            weekly[-1]["is_published"] = False
    blocked = customer_blocked_keys()
    for key in blocked - set(weekly_savoury.LEGACY_WEEKLY_KEYS):
        if key == "bento":
            for week in menu.get("weekly_menus", []):
                week["bengali_bento"] = []
            menu.get("order_types", {}).pop("bengali_bento", None)
        else:
            collection_key = "signature_collection" if key == "signature" else key
            if collection_key in menu.get("collections", {}):
                menu["collections"][collection_key]["products"] = []
    if blocked & set(weekly_savoury.LEGACY_WEEKLY_KEYS):
        for week in menu.get("weekly_menus", []):
            for section in ("vegetarian", "non_vegetarian", "norom_pak_sondesh"):
                week[section] = []
            week["is_published"] = False
    return menu


def weekly_analysis(draft, options=None, conn=None):
    """Validate one proposed Weekly Savoury publication without changing data."""
    options = options or {}
    if not draft or not draft.get("weekly_mode") or draft.get("menu_key") != weekly_savoury.WEEKLY_KEY:
        raise ValueError("This is not a Weekly Savoury draft.")
    if conn is None:
        with db.get_connection() as connection:
            return weekly_analysis(draft, options, connection)
    rows, errors = validate(draft["rows"], weekly_savoury.WEEKLY_KEY)
    if any(row["category"] == "bengali_bento" for row in rows):
        errors.append("Bengali Bento belongs in Menu Catalogue, not Weekly Savoury.")
    canonical_names = {}
    for index, row in enumerate(rows):
        canonical_names.setdefault(weekly_savoury._name(row.get("item")), set()).add(index)
    alias_owners = {}
    for index, row in enumerate(rows):
        for name in row.get("aliases") or []:
            normalized = weekly_savoury._name(name)
            if normalized and (canonical_names.get(normalized, {index}) != {index}
                               or alias_owners.setdefault(normalized, index) != index):
                errors.append("An item alias matches more than one menu row.")
    original = draft.get("weekly_dates") or {}
    raw_start = options.get("valid_from", original.get("valid_from"))
    raw_end = options.get("valid_until", original.get("valid_until"))
    accepted_undated = options.get("accept_undated") is True
    now = datetime.now(timezone.utc)
    fallback_until = None
    if raw_start or raw_end:
        try:
            start, end = date.fromisoformat(raw_start), date.fromisoformat(raw_end)
        except (TypeError, ValueError):
            raise ValueError("Enter both valid dates as YYYY-MM-DD.") from None
        if not timedelta(0) <= end - start <= timedelta(days=14):
            raise ValueError("Weekly menu dates must be a valid range of at most 15 inclusive days.")
        provenance = ("OPERATOR_EDITED" if (raw_start, raw_end) !=
                      (original.get("valid_from"), original.get("valid_until"))
                      else original.get("date_source") or "TEXT_PARSED")
    elif accepted_undated:
        start = menu_dates.today_ist()
        fallback_until = now + timedelta(days=7)
        end = fallback_until.astimezone(menu_dates._IST).date()
        provenance = "UNDATED_FALLBACK"
    else:
        start = end = None
        provenance = "UNDETECTED"
    if end and end < menu_dates.today_ist():
        errors.append("These menu dates have already expired. Confirm new valid dates before publishing.")
    previous = weekly_savoury.previous_rows(conn, start.isoformat() if start else None)
    overlaps = weekly_savoury.overlap_versions(start, end, conn) if start and end else []
    if len(overlaps) == 1:
        # Replacing an overlapping schedule must still warn about changes to
        # that exact approved schedule, even when it has not started yet.
        previous = json.loads(overlaps[0]["approval_json"]).get("rows", [])
    # An unreadable/incomplete source is not an intentional remove-all menu.
    # Manual corrections re-enable comparison through the same validation.
    comparison_available = any(not row.get("warnings") for row in rows)
    changes = weekly_savoury.compare_rows(rows, previous) if comparison_available else weekly_savoury.compare_rows([], [])
    if changes["suspicious_quantity"]:
        errors.append("Unusual quantity change — review and confirm the printed quantity.")
    if changes["suspicious_price"]:
        errors.append("Unusual price change — review and confirm the printed price.")
    if not start:
        errors.append("Date unclear — confirm dates or deliberately accept the 7-day fallback.")
    return {"rows": rows, "errors": list(dict.fromkeys(errors)), "changes": changes,
            "comparison_available": comparison_available,
            "valid_from": start.isoformat() if start else None,
            "valid_until": end.isoformat() if end else None,
            "display_date_range": weekly_savoury.display_range(start, end) if start and end else None,
            "date_provenance": provenance,
            "fallback_until_at": fallback_until.isoformat() if fallback_until else None,
            "overlaps": [{"id": version["id"], "date_range": weekly_savoury.display_range(
                version["valid_from"], version["valid_until"])} for version in overlaps],
            "needs_review": bool(errors or overlaps or draft.get("warnings"))}


def publish_approved_menu(key, draft_id, revision, actor, weekly_options=None):
    key = _key(key)
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        draft = get_draft(key, conn)
        if draft and draft["draft_id"] == draft_id and draft["status"] == "PUBLISHED":
            return draft  # Idempotent retry of the same approval.
        _check(draft, draft_id, revision)
        weekly_mode = bool(draft.get("weekly_mode"))
        if weekly_mode:
            if weekly_options is None:
                raise ValueError("Confirm the Weekly Savoury dates and changes before publishing.")
            analysis = weekly_analysis(draft, weekly_options, conn)
            errors = analysis["errors"]
            if weekly_options.get("confirm_suspicious") is True:
                errors = [error for error in errors if not error.startswith((
                    "Unusual price change", "Unusual quantity change"))]
            if draft.get("warnings"):
                errors.extend(draft["warnings"])
            if errors:
                raise ValueError("; ".join(dict.fromkeys(errors)))
            overlaps = analysis["overlaps"]
            replace_id = weekly_options.get("replace_version_id")
            if overlaps and (len(overlaps) != 1 or replace_id != overlaps[0]["id"]):
                raise ReviewConflict("These menu dates overlap an existing weekly menu. Confirm which version to replace.")
            if replace_id and not overlaps:
                raise ReviewConflict("The overlapping weekly version changed. Reload before publishing.")
            live_week = weekly_savoury.current_version(conn=conn)
            if (overlaps and live_week and analysis["valid_from"] > menu_dates.today_ist().isoformat()
                    and any(version["id"] == live_week["id"] for version in overlaps)):
                raise ReviewConflict("A future replacement cannot retire the current weekly menu early.")
        elif weekly_options is not None:
            raise ValueError("Weekly publication options are not valid for catalogue menus.")
        rows, _ = validate(draft["rows"], key, strict=True)
        asset_url = None
        if draft["flyer_asset"]:
            name = draft["flyer_asset"]
            # Immutable file staged before COMMIT. A failed transaction leaves
            # an unreferenced file, never a half-published live version.
            target = Path(menu_images._dir()) / name
            content = (_private_dir() / name).read_bytes()
            with target.open("wb") as output:
                output.write(content)
                output.flush()
                import os
                os.fsync(output.fileno())
            asset_url = "/uploads/menu-images/" + name
        # Text-only publication explicitly removes the old flyer. Keeping it
        # would pair newly approved prices with the previous image's prices.
        if weekly_mode:
            keys = weekly_savoury.LEGACY_WEEKLY_KEYS
        elif key in (*weekly_savoury.LEGACY_WEEKLY_KEYS, "bento") and not weekly_savoury.lifecycle_enabled(conn):
            keys = (*weekly_savoury.LEGACY_WEEKLY_KEYS, "bento")
        else:
            keys = (key,)
        placeholders = ",".join("?" for _ in keys)
        is_live_now = weekly_mode and analysis["valid_from"] <= menu_dates.today_ist().isoformat()
        if not weekly_mode or is_live_now:
            conn.execute(f"UPDATE menu_image_versions SET status='archived',archived_at=CURRENT_TIMESTAMP WHERE menu_key IN ({placeholders}) AND status='current'", keys)
        if weekly_mode and analysis["overlaps"]:
            old_id = analysis["overlaps"][0]["id"]
            conn.execute("UPDATE menu_versions SET weekly_state='SUPERSEDED',is_current=0 WHERE id=?", (old_id,))
            conn.execute("UPDATE menu_image_versions SET status='archived',archived_at=CURRENT_TIMESTAMP WHERE review_version_id=?", (old_id,))
        image_version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM menu_image_versions WHERE menu_key=?", (key,)).fetchone()[0]
        image_status = "scheduled" if weekly_mode and not is_live_now else "current"
        conn.execute("""INSERT INTO menu_image_versions(menu_key,image_path,status,version,uploaded_by,published_at)
            VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)""", (key, asset_url or "", image_status, image_version, actor))
        if not weekly_mode or is_live_now:
            conn.execute(f"UPDATE menu_versions SET is_current=0 WHERE menu_key IN ({placeholders})", keys)
        approval = {**draft, "rows": rows, "approved_by": actor, "image_path": asset_url,
                    "approved_at": datetime.now(timezone.utc).isoformat()}
        if weekly_mode:
            approval["weekly_dates"] = {"valid_from": analysis["valid_from"],
                "valid_until": analysis["valid_until"], "date_source": analysis["date_provenance"],
                "display_date_range": analysis["display_date_range"]}
            cursor = conn.execute("""INSERT INTO menu_versions
                (menu_key,snapshot_json,action,changed_by,approval_json,is_current,
                 valid_from,valid_until,date_provenance,weekly_state,fallback_until_at)
                VALUES(?,?,'weekly_publish',?,?,?,?,?,?,NULL,?)""",
                (key, _dump(rows), actor, _dump(approval), 0,
                 analysis["valid_from"], analysis["valid_until"], analysis["date_provenance"],
                 analysis["fallback_until_at"]))
            conn.execute("UPDATE menu_image_versions SET review_version_id=? WHERE menu_key=? AND version=?",
                         (cursor.lastrowid, key, image_version))
            approval["version_id"] = cursor.lastrowid
            conn.execute("UPDATE menu_versions SET approval_json=? WHERE id=?", (_dump(approval), cursor.lastrowid))
        else:
            cursor = conn.execute("""INSERT INTO menu_versions(menu_key,snapshot_json,action,changed_by,approval_json,is_current)
                VALUES(?,?,'approve_publish',?,?,1)""", (key, _dump(rows), actor, _dump(approval)))
        draft.update(status="PUBLISHED", version_id=cursor.lastrowid, revision=revision + 1)
        if weekly_mode:
            draft["weekly_dates"] = approval["weekly_dates"]
        _store(conn, draft, actor)
        _audit(conn, draft, "draft_approved", actor, {"version_id": draft["version_id"]})
        _audit(conn, draft, "menu_published", actor, {"version_id": draft["version_id"]})
        conn.commit()
    reload_knowledge()
    return draft


def reload_knowledge():
    from camellia.responder import responder
    from camellia.rag_fallback import rag_fallback_engine
    from camellia.order_calculator import _weekly_cache
    responder.reload_data()
    rag_fallback_engine.reload_data()
    _weekly_cache["mtime"] = None


def source_file(key, draft_id):
    draft = get_draft(key)
    if not draft or draft["draft_id"] != draft_id or not draft["source_asset"]:
        raise KeyError(draft_id)
    return _private_dir() / draft["source_asset"]


def version_source_file(key, version_id):
    version = db.get_menu_version(version_id)
    if not version or version["menu_key"] != _key(key) or not version["approval_json"]:
        raise KeyError(version_id)
    approval = json.loads(version["approval_json"])
    asset = approval.get("source_asset")
    if asset and str(asset).lower().endswith(".pdf"):
        asset = approval.get("flyer_asset")
    if not asset or Path(asset).name != asset:
        raise KeyError(version_id)
    root = Path(config.MENU_JSON_PATH).resolve().parent / "menu-review-private"
    path = (root / asset).resolve()
    if path.parent != root.resolve() or not path.is_file():
        raise KeyError(version_id)
    return path


def version_history(key, limit=20, before_id=None):
    key = _key(key)
    if not 1 <= limit <= 100 or (before_id is not None and before_id < 1):
        raise ValueError("Invalid version page.")
    with db.get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM menu_versions WHERE menu_key=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
            (key, before_id, before_id, limit))]


def restore_as_draft(key, version_id, actor):
    version = db.get_menu_version(version_id)
    if not version or version["menu_key"] != _key(key) or not version["approval_json"]:
        raise KeyError(version_id)
    snapshot = json.loads(version["approval_json"])
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = get_draft(key, conn)
        if prior and prior["status"] not in ("PUBLISHED", "REJECTED"):
            raise ReviewConflict("Review or reject the existing draft first.")
        snapshot.update(draft_id=uuid.uuid4().hex, status="NEEDS_REVIEW", revision=1,
                        created_by=actor, created_at=datetime.now(timezone.utc).isoformat())
        _store(conn, snapshot, actor)
        _audit(conn, snapshot, "draft_created", actor, {"restored_version": version_id})
        conn.commit()
    return snapshot
