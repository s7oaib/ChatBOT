"""Isolated additive schema; never writes the canonical approved source."""
import json
from datetime import datetime, timezone

BENTO_UNTIL = "2026-10-11T23:59:59+05:30"


def initialize(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS approved_qa_overrides (
        qa_id TEXT PRIMARY KEY, record_type TEXT NOT NULL,
        question TEXT, answer TEXT, context TEXT, aliases_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'ACTIVE', valid_from TEXT, valid_until TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by TEXT NOT NULL, updated_by TEXT NOT NULL
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS approved_qa_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, qa_id TEXT NOT NULL,
        snapshot_json TEXT NOT NULL, changed_at TEXT NOT NULL,
        changed_by TEXT NOT NULL, change_reason TEXT NOT NULL
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_approved_qa_versions ON approved_qa_versions(qa_id, id)")
    now = datetime.now(timezone.utc).isoformat()
    for qa_id in ("bento_details_client", "bento_price_client"):
        cursor.execute("""INSERT OR IGNORE INTO approved_qa_overrides
            (qa_id,record_type,valid_until,created_at,updated_at,created_by,updated_by)
            VALUES (?, 'BASE_OVERRIDE', ?, ?, ?, 'bootstrap', 'bootstrap')""",
            (qa_id, BENTO_UNTIL, now, now))
        if cursor.rowcount:
            row = {"qa_id": qa_id, "record_type": "BASE_OVERRIDE", "question": None,
                   "answer": None, "context": None, "aliases_json": "[]", "status": "ACTIVE",
                   "valid_from": None, "valid_until": BENTO_UNTIL,
                   "created_at": now, "updated_at": now, "created_by": "bootstrap", "updated_by": "bootstrap"}
            cursor.execute("""INSERT INTO approved_qa_versions
                (qa_id,snapshot_json,changed_at,changed_by,change_reason) VALUES (?,?,?,'bootstrap','Initial Bento validity')""",
                (qa_id, json.dumps(row), now))
