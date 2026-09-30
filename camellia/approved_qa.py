"""Explicit Admin approval overlay. Runtime reads are resilient; writes fail closed."""
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import uuid
from dataclasses import replace
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from camellia import client_qa

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger(__name__)
_snapshots = {}
_lock = threading.RLock()
_preview_secret = secrets.token_bytes(32)
EDIT_FIELDS = {"question", "answer", "context", "aliases", "valid_from", "valid_until"}


def now_utc():
    return datetime.now(timezone.utc)


def _database():
    from camellia.database import db
    return db


def _read(conn):
    return {row["qa_id"]: dict(row) for row in conn.execute("SELECT * FROM approved_qa_overrides ORDER BY qa_id")}


def overlays(*, strict=False):
    database = _database()
    with _lock:
        try:
            with database.get_connection() as conn:
                result = _read(conn)
            _snapshots[database] = result
            return result
        except Exception:
            if strict:
                raise
            log.exception("Approved Q&A overlay unavailable; retaining last known good snapshot or static source")
            return _snapshots.get(database, {})


def parse_date(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("Validity must be an ISO date/time in Asia/Kolkata")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid validity date/time") from exc
    # datetime-local from the Admin form explicitly represents IST.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc)


def status(row, at=None):
    at = at or now_utc()
    if row.get("status") == "ARCHIVED":
        return "ARCHIVED"
    start, end = parse_date(row.get("valid_from")), parse_date(row.get("valid_until"))
    if start and at < start:
        return "SCHEDULED"
    if end and at > end:
        return "EXPIRED"
    return "ACTIVE"


def _edited_blocks(base, answer):
    """Keep read-only media slots in order while editing only the text copy."""
    old = "\n\n".join(block.value for block in base.blocks if block.kind == "TEXT")
    opcodes = SequenceMatcher(None, old, answer, autojunk=False).get_opcodes()
    def boundary(offset):
        for tag, a, b, c, d in opcodes:
            if a <= offset <= b:
                if tag == "equal":
                    return c + offset - a
                return c if b == a else c + round((offset - a) * (d - c) / (b - a))
        return len(answer)
    blocks, offset, previous = [], 0, 0
    texts = [block for block in base.blocks if block.kind == "TEXT"]
    seen = 0
    for block in base.blocks:
        if block.kind != "TEXT":
            blocks.append(block)
            continue
        offset += len(block.value)
        seen += 1
        end = len(answer) if seen == len(texts) else boundary(offset)
        blocks.append(client_qa.ResponseBlock("TEXT", answer[previous:end]))
        previous = end
        offset += 2
    return tuple(blocks) or (client_qa.ResponseBlock("TEXT", answer),)


def _entry(qa_id, row):
    base = client_qa.REGISTRY_BY_ID.get(qa_id)
    if base is None:
        return client_qa.ApprovedQA(qa_id, row["question"], row["answer"], row["context"] or "general",
                                  blocks=(client_qa.ResponseBlock("TEXT", row["answer"]),))
    changes = {key: row[key] for key in ("question", "answer", "context") if row.get(key) is not None}
    if changes.get("answer", base.answer) != base.answer:
        changes["blocks"] = _edited_blocks(base, changes["answer"])
    return replace(base, **changes) if changes else base


def registry(rows=None, *, active_only=True, at=None):
    rows = overlays() if rows is None else rows
    ids = list(client_qa.REGISTRY_BY_ID) + [key for key in rows if key not in client_qa.REGISTRY_BY_ID]
    return tuple(_entry(key, rows.get(key, {})) for key in ids
                 if not active_only or status(rows.get(key, {}), at) == "ACTIVE")


def get(qa_id):
    return next((entry for entry in registry() if entry.qa_id == qa_id), None)


def match(message, facts=None, *, rows=None):
    rows = overlays() if rows is None else rows
    active = {entry.qa_id: entry for entry in registry(rows)}
    text = client_qa._normalise_match_text(message)
    exact = set()
    for key, row in rows.items():
        if key not in active:
            continue
        words = json.loads(row["aliases_json"])
        if row.get("question") is not None:
            words = [row["question"], *words]
        if any(client_qa._normalise_match_text(word) == text for word in words):
            exact.add(key)
    original = client_qa._match_base(message, facts)
    if original and original.entry.qa_id in active:
        # Existing QA-S1 semantic authority also participates in ambiguity checks.
        exact.add(original.entry.qa_id)
    if len(exact) != 1:
        return None
    key = exact.pop()
    entry = active[key]
    kind = original.match_kind if original and original.entry.qa_id == key else "admin_exact"
    assets = original.asset_keys if original and original.entry.qa_id == key else entry.asset_keys
    return client_qa.ApprovedMatch(entry, kind, assets)


def _record(entry, row):
    derived = status(row)
    end = parse_date(row.get("valid_until"))
    return {"qa_id": entry.qa_id, "question": entry.question, "answer": entry.answer,
            "context": entry.context, "aliases": json.loads(row.get("aliases_json", "[]")),
            "source_aliases": list(client_qa._PARAPHRASE_ALIASES.get(entry.qa_id, ())),
            "variation_count": len({client_qa._normalise_match_text(v) for v in [*client_qa._PARAPHRASE_ALIASES.get(entry.qa_id, ()), *json.loads(row.get("aliases_json", "[]"))]}),
            "asset_keys": list(entry.asset_keys), "blocks": [vars(block) for block in entry.blocks],
            "status": derived, "expiring_soon": bool(derived == "ACTIVE" and end and end <= now_utc() + timedelta(days=7)),
            "valid_from": row.get("valid_from"), "valid_until": row.get("valid_until"),
            "record_type": row.get("record_type", "BASE_OVERRIDE"),
            **{key: row.get(key) for key in ("created_at", "updated_at", "created_by", "updated_by")}}


def listing():
    rows = overlays(strict=True)
    records = [_record(entry, rows.get(entry.qa_id, {})) for entry in registry(rows, active_only=False)]
    counts = {key: sum(record["status"] == key for record in records) for key in ("ACTIVE", "SCHEDULED", "EXPIRED", "ARCHIVED")}
    counts["EXPIRING_SOON"] = sum(record["expiring_soon"] for record in records)
    return {"records": records, "counts": counts, "timezone": "Asia/Kolkata"}


def detail(qa_id):
    record = next((row for row in listing()["records"] if row["qa_id"] == qa_id), None)
    if record is None:
        raise KeyError(qa_id)
    with _database().get_connection() as conn:
        versions = [dict(row) for row in conn.execute(
            "SELECT * FROM approved_qa_versions WHERE qa_id=? ORDER BY id DESC", (qa_id,))]
    record["versions"] = [{**row, "snapshot": json.loads(row["snapshot_json"]),
                           "effective_snapshot": _record(_entry(qa_id, json.loads(row["snapshot_json"])), json.loads(row["snapshot_json"]))}
                          for row in versions]
    return record


def _digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _candidate(body, rows):
    if not isinstance(body, dict) or set(body) - EDIT_FIELDS - {"qa_id", "preview_token", "test_message", "create"}:
        raise ValueError("Only question, answer, context, aliases and validity may be edited")
    qa_id = body.get("qa_id") or "admin_" + uuid.uuid4().hex
    if not isinstance(qa_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", qa_id):
        raise ValueError("Invalid Q&A ID")
    if qa_id not in client_qa.REGISTRY_BY_ID and qa_id not in rows and not qa_id.startswith("admin_"):
        raise ValueError("New approved IDs must start with admin_")
    if body.get("create") and (qa_id in rows or qa_id in client_qa.REGISTRY_BY_ID):
        raise ValueError("Duplicate Q&A ID")
    existing = rows.get(qa_id, {})
    base = client_qa.REGISTRY_BY_ID.get(qa_id)
    result = {"qa_id": qa_id, "record_type": "BASE_OVERRIDE" if base else "ADMIN_ADDED",
              "status": existing.get("status", "ACTIVE"), "aliases_json": existing.get("aliases_json", "[]"),
              "question": existing.get("question"), "answer": existing.get("answer"), "context": existing.get("context"),
              "valid_from": existing.get("valid_from"), "valid_until": existing.get("valid_until")}
    for key in ("question", "answer", "context"):
        if key in body:
            value = body[key]
            if not isinstance(value, str) or (key != "context" and not value.strip()) or (key == "question" and not client_qa._normalise_match_text(value)):
                raise ValueError("Question and approved answer are required")
            if len(value) > (20000 if key == "answer" else 2000):
                raise ValueError("Approved field is too long")
            # Sending unchanged base copy must not manufacture an override.
            result[key] = None if base and value == getattr(base, key) else value
    if not base and (not result["question"] or not result["answer"]):
        raise ValueError("Question and approved answer are required")
    if "aliases" in body:
        aliases = body["aliases"]
        if not isinstance(aliases, list) or len(aliases) > 100 or any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in aliases):
            raise ValueError("Question variations must be a list of nonempty questions")
        normal = [client_qa._normalise_match_text(v) for v in aliases]
        if not all(normal) or len(set(normal)) != len(normal):
            raise ValueError("Duplicate question variations")
        result["aliases_json"] = json.dumps(aliases, ensure_ascii=False)
    for key in ("valid_from", "valid_until"):
        if key in body:
            date = parse_date(body[key])
            result[key] = date.isoformat() if date else None
    start, end = parse_date(result["valid_from"]), parse_date(result["valid_until"])
    if start and end and end < start:
        raise ValueError("Valid Until must be on or after Valid From")
    if base and result["answer"] is not None:
        if any(marker in result["answer"] for marker in client_qa._PLACEHOLDER_ASSETS):
            raise ValueError("Existing asset references are read-only")
        if base.context == "home_table" and client_qa.HOME_TABLE_VIDEO_ANCHOR in base.answer and client_qa.HOME_TABLE_VIDEO_ANCHOR not in result["answer"]:
            raise ValueError("Preserve the Home Table Reel anchor")
    if not base and re_search_asset(result["answer"]):
        raise ValueError("New approved Q&A must be text-only")
    return result


def re_search_asset(answer):
    return bool(re.search(r"\[.*(?:IMAGE|ASSET|VIDEO).*\]", answer, re.I))


def _conflicts(candidate, rows):
    entry = _entry(candidate["qa_id"], candidate)
    words = [entry.question, *json.loads(candidate["aliases_json"])]
    others = {e.qa_id: e for e in registry(rows) if e.qa_id != entry.qa_id}
    for word_index, word in enumerate(words):
        normalized = client_qa._normalise_match_text(word)
        for key, other in others.items():
            other_words = [other.question, *json.loads(rows.get(key, {}).get("aliases_json", "[]"))]
            if any(client_qa._normalise_match_text(v) == normalized for v in other_words):
                # Immutable source has a few intentionally context-dependent duplicate primary questions.
                if word_index == 0 and entry.qa_id in client_qa.REGISTRY_BY_ID and candidate.get("question") is None:
                    continue
                raise ValueError(f"This wording already matches: {key}")
        original = client_qa._match_base(word)
        if original and original.entry.qa_id != entry.qa_id and original.entry.qa_id in others:
            if word_index == 0 and entry.qa_id in client_qa.REGISTRY_BY_ID and candidate.get("question") is None:
                continue
            raise ValueError(f"This wording already matches: {original.entry.qa_id}")


def _token(candidate, rows):
    data = {"candidate": candidate, "registry": _digest(rows), "expires": int(now_utc().timestamp()) + 900}
    encoded = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8").hex()
    signature = hmac.new(_preview_secret, encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def preview(body):
    rows = overlays(strict=True)
    candidate = _candidate(body, rows)
    _conflicts(candidate, rows)
    proposed = {**rows, candidate["qa_id"]: candidate}
    message = body.get("test_message") or _entry(candidate["qa_id"], candidate).question
    tested = match(message, rows=proposed)
    return {"qa_id": candidate["qa_id"], "preview_token": _token(candidate, rows),
            "current": _record(_entry(candidate["qa_id"], rows.get(candidate["qa_id"], {})), rows.get(candidate["qa_id"], {}))
            if candidate["qa_id"] in rows or candidate["qa_id"] in client_qa.REGISTRY_BY_ID else None,
            "proposed": _record(_entry(candidate["qa_id"], candidate), candidate),
            "test": {"matched_id": tested.entry.qa_id if tested else None,
                     "answer": tested.entry.answer if tested else None,
                     "assets": list(tested.asset_keys) if tested else [],
                     "status": status(candidate)}}


def test(message):
    rows = overlays(strict=True)
    result = match(message, rows=rows)
    original = client_qa._match_base(message)
    key = result.entry.qa_id if result else original.entry.qa_id if original else None
    return {"matched_id": result.entry.qa_id if result else None,
            "status": status(rows.get(key, {})) if key else "NO_MATCH",
            "answer": result.entry.answer if result else None, "assets": list(result.asset_keys) if result else []}


def _save(conn, candidate, rows, actor, reason):
    now = now_utc().isoformat()
    key = candidate["qa_id"]
    old = rows.get(key)
    if old is None and key in client_qa.REGISTRY_BY_ID:
        old = {"qa_id": key, "record_type": "BASE_OVERRIDE", "question": None, "answer": None,
               "context": None, "aliases_json": "[]", "status": "ACTIVE", "valid_from": None, "valid_until": None}
    for snapshot, label in ((old, "Before: " + reason),):
        if snapshot is not None:
            conn.execute("INSERT INTO approved_qa_versions (qa_id,snapshot_json,changed_at,changed_by,change_reason) VALUES (?,?,?,?,?)",
                         (key, json.dumps(snapshot, ensure_ascii=False), now, actor, label))
    candidate = {**candidate, "created_at": (old or {}).get("created_at") or now,
                 "created_by": (old or {}).get("created_by") or actor, "updated_at": now, "updated_by": actor}
    columns = ("qa_id", "record_type", "question", "answer", "context", "aliases_json", "status", "valid_from", "valid_until", "created_at", "updated_at", "created_by", "updated_by")
    conn.execute("INSERT INTO approved_qa_overrides (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ") ON CONFLICT(qa_id) DO UPDATE SET " + ",".join(key + "=excluded." + key for key in columns if key != "qa_id"),
                 tuple(candidate[key] for key in columns))
    conn.execute("INSERT INTO approved_qa_versions (qa_id,snapshot_json,changed_at,changed_by,change_reason) VALUES (?,?,?,?,?)",
                 (key, json.dumps(candidate, ensure_ascii=False), now, actor, reason))


def publish(body, actor):
    try:
        encoded, signature = body.get("preview_token", "").split(".")
        if not hmac.compare_digest(signature, hmac.new(_preview_secret, encoded.encode(), hashlib.sha256).hexdigest()):
            raise ValueError()
        approval = json.loads(bytes.fromhex(encoded))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Preview / Test is required before Publish") from None
    with _lock, _database().get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = _read(conn)
        candidate = _candidate(body, rows)
        if approval["expires"] < now_utc().timestamp() or approval["registry"] != _digest(rows) or approval["candidate"] != candidate:
            raise ValueError("The draft or approved registry changed. Preview / Test again before Publish")
        _conflicts(candidate, rows)
        _save(conn, candidate, rows, actor, "Published approved Q&A")
    return detail(candidate["qa_id"])


def archive_or_restore(qa_id, actor, *, restore=False, version_id=None):
    with _lock, _database().get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = _read(conn)
        if qa_id not in rows and qa_id not in client_qa.REGISTRY_BY_ID:
            raise KeyError(qa_id)
        candidate = _candidate({"qa_id": qa_id}, rows)
        if version_id is not None:
            version = conn.execute("SELECT snapshot_json FROM approved_qa_versions WHERE qa_id=? AND id=?", (qa_id, version_id)).fetchone()
            if not version:
                raise KeyError(version_id)
            snapshot = json.loads(version[0])
            candidate.update({key: snapshot.get(key) for key in candidate if key not in {"qa_id", "record_type"}})
        candidate["status"] = "ACTIVE" if restore else "ARCHIVED"
        if restore:
            _conflicts(candidate, rows)
        _save(conn, candidate, rows, actor, "Restored version" if version_id is not None else "Restored" if restore else "Archived")
    return detail(qa_id)
