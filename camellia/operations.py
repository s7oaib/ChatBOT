"""Read-only, bounded operations diagnostics; no business-fact authority."""
import json
import sqlite3
from contextlib import closing
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from itertools import islice


def backup_database(source):
    """SQLite online snapshot includes committed WAL, never checkpoints live DB."""
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError("Database unavailable")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    target = source.with_name(source.name + f".backup-{stamp}-{uuid4().hex[:8]}")
    # Exclusive creation: no backup is overwritten, even on colliding names.
    with target.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
                integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError("Backup integrity verification failed")
    except Exception:
        target.unlink(missing_ok=True)  # Only the exclusive file created here.
        raise
    return {"status": "backed_up", "source_path": str(source),
            "backup_path": str(target), "size_bytes": target.stat().st_size,
            "integrity_check": "ok", "verified": True,
            "timestamp": datetime.now(timezone.utc).isoformat()}


def backup_visibility(source):
    """No invented schedule or off-host guarantee; only local artifact evidence."""
    path = Path(source)
    latest = None
    # Do not load/read backup DBs during an Admin page request.
    artifacts = list(islice(path.parent.glob(path.name + ".backup-*"), 201))
    if len(artifacts) > 200:
        return {"status": "unknown_scan_limit", "last_modified": None,
                "off_host": "unknown", "schedule": "unknown"}
    for item in artifacts:
        if item.is_file() and (latest is None or item.stat().st_mtime > latest.stat().st_mtime):
            latest = item
    return {"status": "local_artifact_present_unverified" if latest else "unknown",
            "last_modified": datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc).isoformat() if latest else None,
            "off_host": "unknown", "schedule": "unknown"}


def database_summary(database, limit=100):
    """Fixed query count, latest-N index/primary-key scans, no content returned."""
    limit = max(1, min(int(limit), 200))
    with closing(database.get_connection()) as conn:
        conn.execute("SELECT 1").fetchone()
        routes = conn.execute("SELECT id, actions, decision_lane, route_source, knowledge_gap, "
                              "escalation_status, timestamp FROM message_route_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        handoffs = conn.execute("SELECT status, created_at, updated_at, expires_at, "
                                "(expires_at IS NOT NULL AND expires_at <= CURRENT_TIMESTAMP) AS expired "
                                "FROM cross_channel_handoffs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        candidates = conn.execute("SELECT status FROM candidate_templates ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        drafts = conn.execute("SELECT menu_key, review_json, updated_at FROM menu_drafts ORDER BY menu_key LIMIT ?", (limit,)).fetchall()
        versions = conn.execute("SELECT v.id,v.menu_key,v.action,v.changed_at,v.is_current,v.valid_from,v.valid_until,v.weekly_state, "
                                "v.fallback_until_at,(v.approval_json IS NOT NULL) AS human_review_recorded, "
                                "c.name,c.active AS catalogue_active FROM menu_versions v "
                                "LEFT JOIN menu_catalogue c ON c.menu_id=v.menu_key ORDER BY v.id DESC LIMIT ?", (limit,)).fetchall()
        menu_events = conn.execute("SELECT action, created_at FROM menu_review_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    pending = []
    for row in drafts:
        try:
            review = json.loads(row["review_json"] or "{}")
        except (ValueError, TypeError):
            review = {}
        pending.append({"menu_key": row["menu_key"], "status": review.get("status", "unknown"),
                        "updated_at": row["updated_at"],
                        "extraction_failed": any(code in {"EXTRACTION_EXCEPTION", "SCHEMA_INCOMPLETE", "NO_ITEMS_FOUND"}
                                                 for code in review.get("diagnostic_codes", [])) or
                        any(str(code).startswith(("PROVIDER_", "INVALID_")) for code in review.get("diagnostic_codes", []))})
    paths = Counter()
    for row in routes:
        actions = json.loads(row["actions"] or "[]")
        recorded = [action.split(":", 1)[1] for action in actions if action.startswith("OBSERVABILITY_PATH:")]
        paths[recorded[0] if recorded else "OTHER_PRE_INSTRUMENTATION"] += 1
    handoff_counts = Counter()
    for row in handoffs:
        # Identical to the existing active-handoff SQL expiry boundary.
        effective = "EXPIRED" if row["status"] in {"CREATED", "WAITING_FOR_HUMAN"} and row["expired"] else row["status"]
        handoff_counts[effective] += 1
    from camellia.weekly_savoury import version_status
    version_metadata = []
    for row in versions:
        version = dict(row)
        version["lifecycle_window_status"] = version_status(version) if version.get("valid_from") or version.get("valid_until") else "UNDATED_USE_MENU_WORKSPACE"
        version_metadata.append(version)
    return {"database": "available", "scope": f"latest_{limit}_records_per_stream",
            "sample_sizes": {"responses": len(routes), "handoffs": len(handoffs), "candidates": len(candidates)},
            "response_lanes": dict(Counter(row["decision_lane"] or "OTHER" for row in routes)),
            "answer_paths": dict(paths),
            "route_sources": dict(Counter(row["route_source"] or "OTHER" for row in routes)),
            "unsupported": sum(bool(row["knowledge_gap"]) for row in routes),
            "handoffs": dict(handoff_counts),
            "candidates": dict(Counter(row["status"] for row in candidates)),
            "menu_reviews": pending, "menu_versions": version_metadata,
            "menu_authority": "Use existing Menu workspace; stored flags alone are not current authority",
            "menu_events": [dict(row) for row in menu_events],
            "last_response_at": routes[0]["timestamp"] if routes else None}
