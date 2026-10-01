"""Durable LIVE / TEST_ONLY access control for the Instagram chatbot.

This is deliberately an outer channel gate, not a routing or answer system.
LIVE preserves today's Instagram behaviour. TEST_ONLY admits only exact Meta
sender IDs explicitly allowlisted by an authenticated admin.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("camellia.instagram_chatbot_control")

LIVE = "LIVE"
TEST_ONLY = "TEST_ONLY"
MODES = {LIVE, TEST_ONLY}
_MAX_TESTERS = 20
_lock = threading.RLock()
_snapshots: Dict[tuple, Dict[str, Any]] = {}


def _database():
    from camellia.database import db
    return db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_key(database) -> tuple:
    return (id(database), str(getattr(database, "db_path", "")))


def _sender_hash(sender_id: str) -> str:
    return hashlib.sha256(str(sender_id).encode("utf-8")).hexdigest()[:12]


def _normalize_username(value: Optional[str]) -> Optional[str]:
    cleaned = str(value or "").strip().lstrip("@").strip()
    return cleaned[:200] or None


def _normalize_sender_id(value: Any) -> str:
    sender_id = str(value or "").strip()
    if not sender_id or len(sender_id) > 200 or any(ch.isspace() for ch in sender_id):
        raise ValueError("Enter a valid exact Meta sender ID")
    return sender_id


def _ensure_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS instagram_chatbot_control (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            mode TEXT NOT NULL CHECK(mode IN ('LIVE', 'TEST_ONLY')),
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS instagram_chatbot_test_accounts (
            sender_id TEXT PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS instagram_chatbot_control_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            previous_value TEXT,
            new_value TEXT,
            subject_hash TEXT,
            subject_label TEXT,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO instagram_chatbot_control (id, mode, updated_at, updated_by) "
        "VALUES (1, 'LIVE', ?, 'system:bootstrap')",
        (now,),
    )


def _snapshot_from_conn(conn) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT mode, updated_at, updated_by FROM instagram_chatbot_control WHERE id = 1"
    ).fetchone()
    if not row:
        raise RuntimeError("Instagram chatbot control row is missing")
    testers = [
        dict(item) for item in conn.execute(
            "SELECT sender_id, username, display_name, created_at, created_by, updated_at, updated_by "
            "FROM instagram_chatbot_test_accounts WHERE enabled = 1 ORDER BY updated_at DESC, sender_id"
        ).fetchall()
    ]
    return {
        "mode": str(row["mode"]),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "test_accounts": testers,
        "tester_count": len(testers),
        "storage_available": True,
    }


def _remember(database, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    stored = copy.deepcopy(snapshot)
    _snapshots[_cache_key(database)] = stored
    return copy.deepcopy(stored)


def clear_runtime_cache(database=None) -> None:
    """Test/recovery helper. Persistent database state is never changed."""
    with _lock:
        if database is None:
            _snapshots.clear()
        else:
            _snapshots.pop(_cache_key(database), None)


def get_state(*, database=None, strict: bool = False) -> Dict[str, Any]:
    """Read durable control state, retaining last-known-good on read failure.

    A completely cold read failure fails closed as TEST_ONLY with an empty
    allowlist. It never guesses LIVE.
    """
    database = database or _database()
    with _lock:
        try:
            with database.get_connection() as conn:
                _ensure_schema(conn)
                conn.commit()
                snapshot = _snapshot_from_conn(conn)
            return _remember(database, snapshot)
        except Exception:
            if strict:
                raise
            log.exception(
                "Instagram chatbot control storage unavailable; using last-known-good or fail-closed state"
            )
            cached = _snapshots.get(_cache_key(database))
            if cached is not None:
                result = copy.deepcopy(cached)
                result["storage_available"] = False
                return result
            return {
                "mode": TEST_ONLY,
                "updated_at": None,
                "updated_by": "fail-closed",
                "test_accounts": [],
                "tester_count": 0,
                "storage_available": False,
            }


def is_sender_allowed(sender_id: Any, *, database=None) -> bool:
    sender = str(sender_id or "").strip()
    if not sender:
        return False
    state = get_state(database=database)
    if state["mode"] == LIVE:
        return True
    return any(
        sender == str(account.get("sender_id") or "")
        for account in state.get("test_accounts", [])
    )


def observe_sender(
    sender_id: Any,
    *,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
    database=None,
) -> None:
    """Best-effort identity observation for the Admin Recent Accounts picker.

    No customer message content is stored here. Failure cannot affect webhook
    acknowledgement or routing.
    """
    try:
        sender = _normalize_sender_id(sender_id)
        username = _normalize_username(username)
        display_name = str(display_name or "").strip()[:300] or None
        database = database or _database()
        with database.get_connection() as conn:
            conn.execute("""
                INSERT INTO instagram_customer_identities (
                    customer_external_id, customer_username, customer_display_name,
                    identity_status, updated_at
                ) VALUES (?, ?, ?, 'UNRESOLVED', CURRENT_TIMESTAMP)
                ON CONFLICT(customer_external_id) DO UPDATE SET
                    customer_username = COALESCE(excluded.customer_username, instagram_customer_identities.customer_username),
                    customer_display_name = COALESCE(excluded.customer_display_name, instagram_customer_identities.customer_display_name),
                    updated_at = CURRENT_TIMESTAMP
            """, (sender, username, display_name))
            conn.commit()
    except Exception:
        log.warning(
            "Could not update recent Instagram identity for sender_hash=%s",
            _sender_hash(str(sender_id or "")),
        )


def _audit(conn, action: str, actor: str, *, previous=None, new=None, sender_id=None, label=None) -> None:
    conn.execute("""
        INSERT INTO instagram_chatbot_control_audit (
            action, previous_value, new_value, subject_hash, subject_label, actor, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        action,
        str(previous) if previous is not None else None,
        str(new) if new is not None else None,
        _sender_hash(sender_id) if sender_id else None,
        _normalize_username(label) if label else None,
        str(actor or "admin")[:200],
        _now(),
    ))


def _commit_and_remember(conn, database) -> Dict[str, Any]:
    snapshot = _snapshot_from_conn(conn)
    conn.commit()
    return _remember(database, snapshot)


def set_mode(mode: str, actor: str, *, database=None) -> Dict[str, Any]:
    mode = str(mode or "").strip().upper()
    if mode not in MODES:
        raise ValueError("Mode must be LIVE or TEST_ONLY")
    database = database or _database()
    with _lock:
        with database.get_connection() as conn:
            _ensure_schema(conn)
            current = conn.execute(
                "SELECT mode FROM instagram_chatbot_control WHERE id = 1"
            ).fetchone()["mode"]
            if current != mode:
                now = _now()
                conn.execute(
                    "UPDATE instagram_chatbot_control SET mode = ?, updated_at = ?, updated_by = ? WHERE id = 1",
                    (mode, now, str(actor or "admin")[:200]),
                )
                _audit(conn, "MODE_CHANGED", actor, previous=current, new=mode)
            return _commit_and_remember(conn, database)


def _identity_for_sender(conn, sender_id: str) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT customer_username, customer_display_name FROM instagram_customer_identities "
        "WHERE customer_external_id = ?",
        (sender_id,),
    ).fetchone()
    return dict(row) if row else {}


def resolve_sender_id(username: str, *, database=None) -> str:
    target = _normalize_username(username)
    if not target:
        raise ValueError("Enter a username or exact Meta sender ID")
    database = database or _database()
    with database.get_connection() as conn:
        rows = conn.execute(
            "SELECT customer_external_id, customer_username FROM instagram_customer_identities "
            "WHERE customer_username IS NOT NULL"
        ).fetchall()
    matches = [
        str(row["customer_external_id"])
        for row in rows
        if (_normalize_username(row["customer_username"]) or "").casefold() == target.casefold()
    ]
    matches = list(dict.fromkeys(matches))
    if len(matches) != 1:
        if not matches:
            raise ValueError("Username was not found in Recent Instagram Accounts; enter the exact Meta sender ID")
        raise ValueError("Username matches more than one sender; select the exact Recent Instagram Account")
    return matches[0]


def add_test_account(
    *,
    sender_id: Optional[str] = None,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
    actor: str,
    database=None,
) -> Dict[str, Any]:
    database = database or _database()
    if not sender_id:
        sender_id = resolve_sender_id(username or "", database=database)
    sender = _normalize_sender_id(sender_id)
    username = _normalize_username(username)
    display_name = str(display_name or "").strip()[:300] or None
    with _lock:
        with database.get_connection() as conn:
            _ensure_schema(conn)
            enabled_count = conn.execute(
                "SELECT COUNT(*) AS n FROM instagram_chatbot_test_accounts WHERE enabled = 1 AND sender_id <> ?",
                (sender,),
            ).fetchone()["n"]
            if int(enabled_count or 0) >= _MAX_TESTERS:
                raise ValueError(f"At most {_MAX_TESTERS} test accounts may be enabled")
            identity = _identity_for_sender(conn, sender)
            username = username or _normalize_username(identity.get("customer_username"))
            display_name = display_name or (str(identity.get("customer_display_name") or "").strip() or None)
            now = _now()
            conn.execute("""
                INSERT INTO instagram_chatbot_test_accounts (
                    sender_id, username, display_name, enabled,
                    created_at, created_by, updated_at, updated_by
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(sender_id) DO UPDATE SET
                    username = COALESCE(excluded.username, instagram_chatbot_test_accounts.username),
                    display_name = COALESCE(excluded.display_name, instagram_chatbot_test_accounts.display_name),
                    enabled = 1,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
            """, (sender, username, display_name, now, str(actor or "admin")[:200], now, str(actor or "admin")[:200]))
            _audit(conn, "TEST_ACCOUNT_ADDED", actor, sender_id=sender, label=username)
            return _commit_and_remember(conn, database)


def remove_test_account(sender_id: Any, actor: str, *, database=None) -> Dict[str, Any]:
    sender = _normalize_sender_id(sender_id)
    database = database or _database()
    with _lock:
        with database.get_connection() as conn:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT username, enabled FROM instagram_chatbot_test_accounts WHERE sender_id = ?",
                (sender,),
            ).fetchone()
            if not row or not bool(row["enabled"]):
                raise KeyError(sender)
            now = _now()
            conn.execute(
                "UPDATE instagram_chatbot_test_accounts SET enabled = 0, updated_at = ?, updated_by = ? WHERE sender_id = ?",
                (now, str(actor or "admin")[:200], sender),
            )
            _audit(conn, "TEST_ACCOUNT_REMOVED", actor, sender_id=sender, label=row["username"])
            return _commit_and_remember(conn, database)


def recent_accounts(*, limit: int = 50, database=None) -> List[Dict[str, Any]]:
    database = database or _database()
    limit = max(1, min(int(limit or 50), 100))
    state = get_state(database=database, strict=True)
    allowed = {str(row["sender_id"]) for row in state["test_accounts"]}
    with database.get_connection() as conn:
        rows = conn.execute("""
            SELECT customer_external_id, customer_username, customer_display_name,
                   identity_status, updated_at
            FROM instagram_customer_identities
            ORDER BY updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [
        {
            "sender_id": str(row["customer_external_id"]),
            "username": row["customer_username"],
            "display_name": row["customer_display_name"],
            "identity_status": row["identity_status"],
            "last_seen_at": row["updated_at"],
            "allowed": str(row["customer_external_id"]) in allowed,
        }
        for row in rows
    ]


def audit_log(*, limit: int = 50, database=None) -> List[Dict[str, Any]]:
    database = database or _database()
    limit = max(1, min(int(limit or 50), 100))
    with database.get_connection() as conn:
        _ensure_schema(conn)
        conn.commit()
        rows = conn.execute("""
            SELECT id, action, previous_value, new_value, subject_hash,
                   subject_label, actor, created_at
            FROM instagram_chatbot_control_audit
            ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(row) for row in rows]


def admin_summary(*, database=None) -> Dict[str, Any]:
    database = database or _database()
    state = get_state(database=database, strict=True)
    with database.get_connection() as conn:
        recent_count = conn.execute(
            "SELECT COUNT(*) AS n FROM instagram_customer_identities"
        ).fetchone()["n"]
    return {**state, "recent_account_count": int(recent_count or 0)}
