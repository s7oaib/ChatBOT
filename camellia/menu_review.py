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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PIL import Image

from camellia.config import config
from camellia.database import db
from camellia import menu_images, weekly_flyer
from camellia.menu_parser import menu_parser

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 150
FIELDS = ("item", "category", "quantity", "price_inr", "description")
SECTIONS = ("vegetarian", "non_vegetarian", "bengali_bento", "norom_pak_sondesh")
FAILURE_MESSAGE = "We couldn't read this menu reliably. Your current published menu is unchanged. Try again or enter the details manually."


class ReviewConflict(ValueError):
    pass


def _key(key):
    key = menu_images._key(key)
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
    return json.loads(row[0]) if row and row[0] else None


def _metadata(values):
    result = {}
    for field in ("week_of", "service_window"):
        value = values.get(field) or ""
        if not isinstance(value, str) or len(value) > 160:
            raise ValueError("Menu date/service window must be at most 160 characters.")
        result[field] = value.strip()
    return result


def _rows(parsed, page=1):
    result = []
    for section in (*SECTIONS, "unclassified"):
        for row in parsed.get(section) or []:
            if not isinstance(row, dict):
                continue
            result.append({**{f: row.get(f, "") for f in FIELDS},
                "row_id": uuid.uuid4().hex, "source_page": page,
                "category": section if section != "unclassified" else "",
                "provenance": {f: "EXTRACTED" for f in FIELDS}})
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
        if not isinstance(source, dict) or set(source) - set((*FIELDS, "row_id", "source_page", "warnings", "provenance")):
            raise ValueError("Unrecognized menu row schema.")
        row = copy.deepcopy(source)
        warnings = []
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
    if strict and errors:
        raise ValueError("; ".join(errors))
    return result, errors


def _safe_image(data, mime):
    extension = menu_images.validate_image(data, mime)
    with Image.open(io.BytesIO(data)) as image:
        if Image.MIME.get(image.format) != mime or image.width * image.height > 25000000:
            raise ValueError("Image type or dimensions are not supported.")
        # Lossless re-encoding strips metadata/trailing payload; no downscaling.
        image.load()
        output = io.BytesIO()
        image.save(output, format=image.format, **({"quality": 100, "subsampling": 0} if image.format == "JPEG" else {}))
    return output.getvalue(), extension


def _pdf_extract(data):
    import pypdfium2 as pdfium
    if not data.startswith(b"%PDF-"):
        raise ValueError("Not a valid PDF.")
    rows, texts, previews, warnings = [], [], [], []
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
                if not any(parsed.values()):
                    buffer = io.BytesIO()
                    preview.save(buffer, format="PNG")
                    result = menu_parser.parse_image_and_update_menu(buffer.getvalue(), "image/png")
                    if result.get("status") == "success":
                        parsed = result["parsed"]
                    else:
                        # Keep the private page preview so an operator can
                        # transcribe it; extraction failure must not discard
                        # the source or make it appear publishable.
                        warnings.append(f"Page {index + 1} needs manual review.")
                rows.extend(_rows(parsed, index + 1))
    # A single lossless page strip is compatible with existing image sending.
    canvas = Image.new("RGB", (max(p.width for p in previews), sum(p.height for p in previews)), "white")
    y = 0
    for preview in previews:
        canvas.paste(preview, (0, y))
        y += preview.height
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return rows, "\n\n".join(texts), output.getvalue(), warnings


def extract(key, actor, *, text=None, data=None, mime=None, filename=""):
    key = _key(key)
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
        "source_asset": None, "flyer_asset": None, "raw_text": "", "menu_metadata": _metadata({}),
        "parser": {"version": 1, "provider": "deterministic" if source_type == "text" else "pdfium/groq",
                   "model": config.GROQ_VISION_MODEL if source_type != "text" else None}}
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = get_draft(key, conn)
        if prior and prior["status"] not in ("REJECTED", "PUBLISHED"):
            raise ReviewConflict("A draft already exists. Review or reject it first.")
        _store(conn, draft, actor)
        _audit(conn, draft, "draft_created", actor)
        conn.commit()
    try:
        if source_type == "text":
            draft["raw_text"] = text
            draft["rows"] = _rows(menu_parser.parse_menu_text(text))
        else:
            if source_type == "pdf":
                rows, raw, flyer, pdf_warnings = _pdf_extract(source)
                draft["rows"], draft["raw_text"] = rows, raw
                draft["warnings"].extend(pdf_warnings)
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
                if result.get("status") != "success":
                    raise ValueError(FAILURE_MESSAGE)
                draft["rows"] = _rows(result["parsed"])
                draft["menu_metadata"] = _metadata(result)
            source_name = draft["draft_id"] + (".pdf" if source_type == "pdf" else "." + extension)
            (_private_dir() / source_name).write_bytes(original)
            flyer_name = draft["draft_id"] + "-flyer." + extension
            (_private_dir() / flyer_name).write_bytes(flyer)
            draft["source_asset"], draft["flyer_asset"] = source_name, flyer_name
        draft["rows"], draft["warnings"] = validate(draft["rows"], key)
        draft["status"] = "NEEDS_REVIEW"
    except Exception:
        # Raw provider errors may contain private content; never return them.
        draft["status"], draft["warnings"] = "FAILED", [FAILURE_MESSAGE]
        draft["rows"] = []
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = get_draft(key, conn)
        if current["draft_id"] != draft["draft_id"] or current["status"] != "EXTRACTING":
            raise ReviewConflict("Draft changed while extraction was running.")
        _store(conn, draft, actor)
        _audit(conn, draft, "extraction_finished", actor, {"status": draft["status"]})
        conn.commit()
    return draft


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
    return [dict(r) for r in conn.execute("""SELECT v.* FROM menu_versions v
        JOIN menu_catalogue c ON c.menu_id=v.menu_key
        WHERE v.is_current=1 AND v.approval_json IS NOT NULL AND c.active=1""")]


def has_current(key):
    return any(v["menu_key"] == menu_images._key(key) for v in current_versions())


def canonical_menu(path=None):
    with open(path or config.MENU_JSON_PATH, encoding="utf-8") as stream:
        menu = json.load(stream)
    for version in current_versions():
        approval = json.loads(version["approval_json"])
        rows = approval["rows"]
        key = version["menu_key"]
        if key in ("weekly_vegetarian", "weekly_non_vegetarian", "bento"):
            weekly = menu.setdefault("weekly_menus", [])
            if not weekly:
                weekly.append({})
            for field in SECTIONS:
                weekly[-1][field] = [{f: r[f] for f in ("item", "quantity", "price_inr", "description")}
                                     for r in rows if r["category"] == field]
            weekly[-1].update(approval.get("menu_metadata") or _metadata({}))
            weekly[-1]["is_published"] = True
        else:
            collection_key = "signature_collection" if key == "signature" else key
            products = []
            for row in rows:
                products.append({"flavour": row["item"], "active": True,
                                 "prices": {row["quantity"]: row["price_inr"]}, "note": row["description"]})
            menu.setdefault("collections", {}).setdefault(collection_key, {})["products"] = products
    return menu


def publish_approved_menu(key, draft_id, revision, actor):
    key = _key(key)
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        draft = get_draft(key, conn)
        if draft and draft["draft_id"] == draft_id and draft["status"] == "PUBLISHED":
            return draft  # Idempotent retry of the same approval.
        _check(draft, draft_id, revision)
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
        keys = ("weekly_vegetarian", "weekly_non_vegetarian", "bento") if key in ("weekly_vegetarian", "weekly_non_vegetarian", "bento") else (key,)
        placeholders = ",".join("?" for _ in keys)
        conn.execute(f"UPDATE menu_image_versions SET status='archived',archived_at=CURRENT_TIMESTAMP WHERE menu_key IN ({placeholders}) AND status='current'", keys)
        image_version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM menu_image_versions WHERE menu_key=?", (key,)).fetchone()[0]
        conn.execute("""INSERT INTO menu_image_versions(menu_key,image_path,status,version,uploaded_by,published_at)
            VALUES(?,?,'current',?,?,CURRENT_TIMESTAMP)""", (key, asset_url or "", image_version, actor))
        conn.execute(f"UPDATE menu_versions SET is_current=0 WHERE menu_key IN ({placeholders})", keys)
        approval = {**draft, "rows": rows, "approved_by": actor, "image_path": asset_url,
                    "approved_at": datetime.now(timezone.utc).isoformat()}
        cursor = conn.execute("""INSERT INTO menu_versions(menu_key,snapshot_json,action,changed_by,approval_json,is_current)
            VALUES(?,?,'approve_publish',?,?,1)""", (key, _dump(rows), actor, _dump(approval)))
        draft.update(status="PUBLISHED", version_id=cursor.lastrowid, revision=revision + 1)
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


def version_history(key):
    return db.get_menu_versions(_key(key))


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
