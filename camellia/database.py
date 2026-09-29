import os
import re
import sqlite3
import json
import time
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple
from camellia.config import config
from camellia.text_rules import strip_flower_emoji
from camellia.candidate_safety import (
    candidate_eligibility,
    detect_business_commitment_warnings,
    knowledge_key,
    normalize_question,
    validate_candidate_answer,
)

_KOLKATA_TZ = ZoneInfo("Asia/Kolkata")

MENU_TOPIC_DEFAULTS = {
    "SONDESH": "Sondesh", "SAVOURY": "Savoury", "BENTO_MEAL": "Bento / Meal",
    "GIFT_EVENT": "Gift / Event", "WEEKLY_SAVOURY": "Weekly Savoury",
    "SUBSCRIPTION": "Subscription", "HOME_TABLE": "Home Table",
}


def _parse_trustworthy_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime.

    Returns None for anything empty or unparseable — callers must treat that
    as "age unknown", never as a proxy for "old" or "young". Accepts ``Z``,
    and explicit ``+00:00``-style offsets. A timezone-naive external timestamp
    is deliberately rejected: the caller has no evidence for which timezone
    Meta meant. Comparison must always happen on the resulting datetime
    instant, never on the raw string, so a non-UTC offset compares correctly
    against a UTC cutoff.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _story_publication_date_kolkata(published_at: Optional[str]) -> Optional[str]:
    """Derive the Asia/Kolkata publication date (YYYY-MM-DD) from a trustworthy
    ``published_at`` timestamp, or None if it cannot be parsed. Never guesses."""
    raw = str(published_at or "").strip()
    if not raw:
        return None
    trustworthy = _parse_trustworthy_datetime(raw)
    if trustworthy is None:
        return None
    return trustworthy.astimezone(_KOLKATA_TZ).strftime("%Y-%m-%d")


def _ordinal_suffix(number: int) -> str:
    if 10 <= (number % 100) <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")


def format_story_sequence_label(number: Optional[int]) -> Optional[str]:
    """Admin-facing ordinal label, e.g. '2nd Story'. Never used for routing."""
    if number is None:
        return None
    try:
        value = int(number)
    except (TypeError, ValueError):
        return None
    return f"{value}{_ordinal_suffix(value)} Story"


def format_story_sequence_date_display(date_value: Optional[str]) -> Optional[str]:
    """Human-facing date, e.g. '17 Sep 2026', from a stored YYYY-MM-DD."""
    raw = str(date_value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return None


class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.DB_PATH
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        self.init_db()

    def _retire_outdated_home_table_knowledge(self) -> int:
        """Archive the retired Bengali Home Table event answers once.

        This is deliberately narrower than a blanket Kolkata cleanup: only
        active learned answers that contain the known event signature are
        retired. Their prior content and aliases remain in version history,
        while the parent status prevents both direct and alias retrieval.
        """
        strong_markers = (
            "4th & 11th October 2026",
            "diamond park",
            "male folk deities of bengal",
            "bengali omakase",
            "durga pujo",
        )
        retired = 0
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM learned_knowledge WHERE status = 'ACTIVE'"
            ).fetchall()
            for row in rows:
                text = " ".join(
                    str(row[key] or "")
                    for key in ("topic", "question_pattern", "verified_answer", "source")
                ).lower()
                has_home_table = "bengali home table" in text or "home table" in text
                marker_count = sum(marker.lower() in text for marker in strong_markers)
                if not has_home_table or marker_count < 1:
                    continue
                self._snapshot_version(
                    conn, int(row["id"]), dict(row),
                    "system:retire_home_table_event",
                    "retired_outdated_home_table_event",
                )
                conn.execute(
                    "UPDATE learned_knowledge SET status = 'ARCHIVED', "
                    "updated_by = ?, updated_at = ? WHERE id = ?",
                    (
                        "system:retire_home_table_event",
                        datetime.utcnow().isoformat(),
                        int(row["id"]),
                    ),
                )
                retired += 1
            if retired:
                conn.commit()
        return retired

    @classmethod
    def _ensure_instagram_outbound_audit_schema(cls, conn: sqlite3.Connection) -> None:
        """Ensure only the additive outbound-audit schema on this connection.

        The long-lived application Database can outlive a test or recovery
        workflow that swaps the SQLite file underneath it. Keeping this small
        helper idempotent lets audit reads/writes heal that file without
        rerunning the full application schema migration on every send.
        """
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS instagram_outbound_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            inbound_message_id INTEGER,
            recipient_id TEXT,
            response_type TEXT NOT NULL DEFAULT 'text',
            parent_send_id TEXT,
            chunk_number INTEGER,
            total_chunks INTEGER,
            character_count INTEGER,
            send_attempted INTEGER NOT NULL DEFAULT 0,
            http_status INTEGER,
            success INTEGER,
            overall_success INTEGER,
            overall_failure_chunk INTEGER,
            meta_message_id TEXT,
            meta_error_code TEXT,
            meta_error_subcode TEXT,
            meta_error_message TEXT,
            meta_trace_id TEXT,
            duration_ms REAL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            delivery_status TEXT,
            exception_class TEXT,
            exception_message TEXT
        )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_ig_outbound_audit_created "
            "ON instagram_outbound_audit(created_at DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_ig_outbound_audit_inbound "
            "ON instagram_outbound_audit(inbound_message_id)"
        )
        for column, declaration in (
            ("parent_send_id", "TEXT"),
            ("chunk_number", "INTEGER"),
            ("total_chunks", "INTEGER"),
            ("character_count", "INTEGER"),
            ("overall_success", "INTEGER"),
            ("overall_failure_chunk", "INTEGER"),
            ("delivery_status", "TEXT"),
        ):
            cls._ensure_column(cursor, "instagram_outbound_audit", column, declaration)

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _ensure_column(cursor, table: str, column: str, decl: str) -> None:
        """Adds a column to an existing table, once, if it is not there yet."""
        existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def init_db(self):
        """Initialize database schema if tables do not exist."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Enable Write-Ahead Logging for high-concurrency read/write operations
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            
            # Sessions table to track ad qualification
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                is_ad_qualified INTEGER DEFAULT 0,
                ad_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, channel)
            );
            """)

            # Admin authentication state is durable so a process restart does
            # not erase brute-force throttles or revive a revoked session.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_login_throttles (
                scope TEXT PRIMARY KEY,
                failures INTEGER NOT NULL DEFAULT 0,
                first_failed_at REAL,
                last_failed_at REAL,
                blocked_until REAL NOT NULL DEFAULT 0
            );
            """)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_sessions (
                session_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                csrf_hash TEXT NOT NULL,
                security_version TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                revoked_at REAL
            );
            """)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_security_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                username TEXT,
                ip_hash TEXT,
                user_agent TEXT,
                occurred_at REAL NOT NULL
            );
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_admin_security_audit_time "
                "ON admin_security_audit(occurred_at DESC)"
            )

            # Conversations log table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                user_message TEXT NOT NULL,
                bot_response TEXT NOT NULL,
                was_matched INTEGER DEFAULT 0,
                template_id TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Phase 3 of the router rollout: the LLM router's silent pick
            # beside the keyword matcher's, for messages nobody replied to
            # differently. Kept out of `conversations` because these rows are a
            # measurement, not a thing any customer was ever sent.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS router_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT,
                user_message TEXT NOT NULL,
                keyword_pick TEXT,
                router_pick TEXT,
                agreed INTEGER DEFAULT 0,
                detail TEXT,
                latency_ms REAL,
                model TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # AI Resolution & Answers, Phase 3 slice 2: the triage model's
            # silent category for every message the deterministic/canonical
            # path could not answer. Same shape as router_shadow above — a
            # measurement table nothing in the reply path reads, kept
            # separate from candidate_templates/escalations/knowledge_gaps
            # because a row here was never acted on, only observed.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS triage_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT,
                user_id TEXT,
                user_message TEXT NOT NULL,
                category TEXT,
                confidence REAL,
                reasoning TEXT,
                ok INTEGER DEFAULT 0,
                detail TEXT,
                matched_knowledge_id INTEGER,
                matched_knowledge_source TEXT,
                matched_knowledge_preview TEXT,
                grounded INTEGER DEFAULT 0,
                grounding_reason TEXT,
                would_auto_resolve INTEGER DEFAULT 0,
                was_matched INTEGER DEFAULT 0,
                template_id TEXT,
                latency_ms REAL,
                model TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Columns added after this table shipped. CREATE TABLE IF NOT
            # EXISTS does nothing to a table that already exists, and the
            # production database lives on a Railway volume full of real rows,
            # so new columns have to be added to it in place.
            self._ensure_column(cursor, "conversations", "message_source", "TEXT")
            self._ensure_column(cursor, "conversations", "ad_id", "TEXT")
            self._ensure_column(cursor, "conversations", "ad_title", "TEXT")
            self._ensure_column(cursor, "sessions", "ad_title", "TEXT")
            self._ensure_column(cursor, "router_shadow", "model", "TEXT")

            # Escalations table for human handoff
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS escalations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                user_message TEXT NOT NULL,
                reason TEXT NOT NULL,
                context_data TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Durable staff notification outbox. The escalation row is the
            # source of truth for the payload; this table tracks one delivery
            # attempt per escalation without putting customer work on an
            # in-memory callback that disappears on restart.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS human_notification_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL DEFAULT 'ESCALATION_CREATED',
                escalation_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                locked_until TIMESTAMP,
                last_error TEXT,
                sent_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Self-Healing Learned Knowledge base. This is the single live,
            # bot-servable answer store — Teach & Resolve, candidate approval,
            # staff/client manual entry and CSV import (Custom Q&A) all write
            # here; nothing forks a second knowledge table for any of them.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS learned_knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                question_pattern TEXT NOT NULL,
                verified_answer TEXT NOT NULL,
                source TEXT DEFAULT 'staff_resolution',
                confidence REAL DEFAULT 1.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Custom Q&A, additive columns on the existing table (existing
            # rows migrate to status=ACTIVE / priority=NORMAL / usage_count=0
            # / last_used_at=NULL via the column defaults below — nothing
            # about topic/question_pattern/verified_answer/source/confidence
            # on an existing row is touched).
            self._ensure_column(cursor, "learned_knowledge", "status", "TEXT DEFAULT 'ACTIVE'")
            self._ensure_column(cursor, "learned_knowledge", "priority", "TEXT DEFAULT 'NORMAL'")
            self._ensure_column(cursor, "learned_knowledge", "usage_count", "INTEGER DEFAULT 0")
            self._ensure_column(cursor, "learned_knowledge", "last_used_at", "TIMESTAMP")
            self._ensure_column(cursor, "learned_knowledge", "created_by", "TEXT")
            self._ensure_column(cursor, "learned_knowledge", "updated_by", "TEXT")
            self._ensure_column(cursor, "learned_knowledge", "normalized_question", "TEXT")

            # One Q&A owns zero or more alternative phrasings of the same
            # question — a real ownership relation, not delimiter-stuffing
            # question_pattern. A row with only question_pattern (every row
            # written before this table existed) remains fully valid with no
            # aliases at all.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS learned_knowledge_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                learned_knowledge_id INTEGER NOT NULL,
                alias_text TEXT NOT NULL,
                normalized_alias TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT
            );
            """)

            # Every edit (and lifecycle change) snapshots the prior state here
            # before applying the new one, so History can show what a Q&A
            # used to say and Restore can bring it back under the same id.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS learned_knowledge_versions (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                learned_knowledge_id INTEGER NOT NULL,
                version_number INTEGER NOT NULL,
                topic TEXT,
                primary_question TEXT,
                answer TEXT,
                priority TEXT,
                status TEXT,
                aliases_snapshot TEXT,
                changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                changed_by TEXT,
                change_reason TEXT
            );
            """)

            # Customer-editable copy for system templates.  This is an
            # override layer, not a duplicate template/knowledge store: the
            # protected qa_templates.json record remains authoritative for
            # triggers and runtime logic, while this table stores only the
            # approved response text an operator has intentionally changed.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS response_template_overrides (
                template_id TEXT PRIMARY KEY,
                response TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_by TEXT
            );
            """)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS response_template_override_versions (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id TEXT NOT NULL,
                response TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                changed_by TEXT,
                change_reason TEXT
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_response_override_versions_template ON response_template_override_versions(template_id, version_id DESC)")

            # Candidate templates table for human approval workflow
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS candidate_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT DEFAULT 'PENDING_APPROVAL',
                user_message TEXT,
                response TEXT NOT NULL,
                source_refs TEXT,
                confidence REAL DEFAULT 0.0,
                created_by TEXT DEFAULT 'rag_fallback',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_at TIMESTAMP,
                approved_by TEXT
            );
            """)

            # One durable row per Instagram Meta send attempt. The recipient
            # is a one-way hash; this table stores no credentials or content.
            self._ensure_instagram_outbound_audit_schema(conn)

            # Human-reviewed assisted-resolution drafts. Separate from the
            # legacy candidate queue so every review action is auditable.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS assisted_resolution_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                escalation_id INTEGER,
                conversation_id INTEGER,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                customer_message TEXT NOT NULL,
                triage_category TEXT NOT NULL,
                detected_intent TEXT,
                confidence REAL DEFAULT 0.0,
                matched_knowledge_id TEXT,
                matched_knowledge_source TEXT,
                grounding_status TEXT NOT NULL DEFAULT 'Not Grounded',
                grounding_details TEXT,
                suggested_answer TEXT,
                original_suggested_answer TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
                risk_flags TEXT NOT NULL DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TIMESTAMP,
                reviewed_by TEXT,
                final_answer TEXT,
                resolution_action TEXT,
                rejection_reason TEXT,
                model TEXT,
                source_routing_id TEXT
            );
            """)
            # Slice 4 evaluation fields are additive so existing review rows
            # remain intact across the production migration.
            self._ensure_column(cursor, "assisted_resolution_suggestions", "edit_score", "REAL")
            self._ensure_column(cursor, "assisted_resolution_suggestions", "edit_severity", "TEXT")
            self._ensure_column(cursor, "assisted_resolution_suggestions", "shadow_category", "TEXT")
            self._ensure_column(cursor, "assisted_resolution_suggestions", "shadow_confidence", "REAL")
            self._ensure_column(cursor, "assisted_resolution_suggestions", "protected_order_flow", "INTEGER DEFAULT 0")
            self._ensure_column(cursor, "assisted_resolution_suggestions", "source_route_present", "INTEGER DEFAULT 0")
            self._ensure_column(cursor, "assisted_resolution_suggestions", "cam_interference", "INTEGER DEFAULT 0")

            # Phase 4: durable Auto Resolve simulation/audit decisions.  These
            # rows are evidence only while the production send flag is off.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS auto_resolve_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assisted_suggestion_id INTEGER,
                conversation_id INTEGER,
                user_id TEXT,
                channel TEXT NOT NULL,
                customer_message TEXT NOT NULL,
                matched_knowledge_id TEXT,
                category TEXT,
                confidence REAL,
                grounding_status TEXT,
                risk_flags TEXT NOT NULL DEFAULT '[]',
                eligibility TEXT NOT NULL,
                eligibility_reasons TEXT NOT NULL DEFAULT '[]',
                simulated INTEGER DEFAULT 1,
                would_auto_resolve INTEGER DEFAULT 0,
                actually_sent INTEGER DEFAULT 0,
                response_text TEXT,
                final_human_outcome TEXT,
                idempotency_key TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            self._ensure_column(cursor, "auto_resolve_decisions", "assisted_suggestion_id", "INTEGER")

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS auto_resolve_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # RAG audit log table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS rag_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_message TEXT NOT NULL,
                matched_template TEXT,
                rag_invoked INTEGER DEFAULT 0,
                retrieved_sources TEXT,
                confidence_score REAL DEFAULT 0.0,
                proposed_template TEXT,
                proposed_response TEXT,
                approval_status TEXT DEFAULT 'PENDING_APPROVAL',
                final_customer_response TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Analytics summary counters table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS analytics (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            );
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS message_route_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                user_message TEXT NOT NULL,
                route TEXT,
                intents TEXT NOT NULL,
                entities TEXT NOT NULL,
                confidence REAL,
                knowledge_ids TEXT NOT NULL,
                actions TEXT NOT NULL,
                llm_called INTEGER DEFAULT 0,
                fallback_used INTEGER DEFAULT 0,
                grounding_result TEXT,
                knowledge_gap INTEGER DEFAULT 0,
                escalation_status TEXT,
                route_source TEXT,
                decision_lane TEXT,
                state_context_used INTEGER DEFAULT 0,
                source_context_influenced INTEGER DEFAULT 0,
                handoff_reason TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            # An existing deployment's table predates route_source; add it
            # without disturbing rows already written.
            cursor.execute("PRAGMA table_info(message_route_audit)")
            route_audit_columns = {row[1] for row in cursor.fetchall()}
            if "route_source" not in route_audit_columns:
                cursor.execute(
                    "ALTER TABLE message_route_audit ADD COLUMN route_source TEXT"
                )
            for column, declaration in (
                ("decision_lane", "TEXT"),
                ("state_context_used", "INTEGER DEFAULT 0"),
                ("source_context_influenced", "INTEGER DEFAULT 0"),
                ("handoff_reason", "TEXT"),
            ):
                if column not in route_audit_columns:
                    cursor.execute(
                        f"ALTER TABLE message_route_audit ADD COLUMN {column} {declaration}"
                    )

            # Instagram -> WhatsApp handoff references. handoff_ref is the
            # opaque customer-facing token ("CAM-48371"); nothing else in this
            # row is ever shown to a customer. One active (non-expired,
            # non-resolved) row per (source_channel, source_user_id) is the
            # dedup key — a repeated "human please" reuses it instead of
            # minting a new reference.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS cross_channel_handoffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                handoff_ref TEXT NOT NULL UNIQUE,
                source_channel TEXT NOT NULL,
                source_user_id TEXT NOT NULL,
                reason TEXT,
                summary TEXT,
                known_entities TEXT,
                customer_username TEXT,
                customer_external_id TEXT,
                unresolved_topic TEXT,
                status TEXT NOT NULL DEFAULT 'CREATED',
                escalation_id INTEGER,
                instagram_username TEXT,
                query_summary TEXT,
                contact_mode TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP
            );
            """)
            # These fields make the generic-callback versus purchase-handoff
            # contract durable.  Add them for existing volumes without
            # rewriting or deleting handoff history.
            cursor.execute("PRAGMA table_info(cross_channel_handoffs)")
            handoff_columns = {row[1] for row in cursor.fetchall()}
            for column, declaration in (
                ("instagram_username", "TEXT"),
                ("query_summary", "TEXT"),
                ("contact_mode", "TEXT"),
            ):
                if column not in handoff_columns:
                    cursor.execute(
                        f"ALTER TABLE cross_channel_handoffs ADD COLUMN {column} {declaration}"
                    )
            self._ensure_column(cursor, "cross_channel_handoffs", "customer_username", "TEXT")
            self._ensure_column(cursor, "cross_channel_handoffs", "customer_external_id", "TEXT")

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS instagram_customer_identities (
                customer_external_id TEXT PRIMARY KEY,
                customer_username TEXT,
                customer_display_name TEXT,
                customer_profile_pic TEXT,
                identity_resolved_at TEXT,
                identity_status TEXT NOT NULL DEFAULT 'UNRESOLVED',
                last_lookup_at TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Admin notification state is deliberately separate from the
            # handoff lifecycle. Reading a notification must never mutate a
            # CAM, and marking it seen must never change WAITING_FOR_HUMAN.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_handoff_views (
                handoff_ref TEXT PRIMARY KEY,
                seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Canonical missing-information queue. Unlike candidate_templates,
            # this records what fact/action is absent rather than teaching one
            # more spelling of the customer's question.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_gaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_key TEXT NOT NULL UNIQUE,
                intents TEXT NOT NULL,
                entities TEXT NOT NULL,
                reason TEXT NOT NULL,
                example_message TEXT NOT NULL,
                occurrences INTEGER DEFAULT 1,
                status TEXT DEFAULT 'OPEN',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS active_conversation_sessions (
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, channel)
            );
            """)

            # The webhook acknowledges only after accepted work is committed.
            # Separate outbound jobs survive a crash or a temporary Meta outage.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS instagram_inbound_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                sender_id TEXT NOT NULL,
                task_name TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                locked_until TIMESTAMP,
                last_error TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS instagram_outbound_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inbound_job_id INTEGER NOT NULL,
                sequence_number INTEGER NOT NULL,
                recipient_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                locked_until TIMESTAMP,
                last_error TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(inbound_job_id, sequence_number)
            )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ig_inbound_jobs_due ON instagram_inbound_jobs(status, next_attempt_at, id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ig_outbound_jobs_due ON instagram_outbound_jobs(status, next_attempt_at, id)")

            # Meta changes ad titles freely, so the stable ad ID is mapped by
            # kitchen staff and kept on the same durable volume as the rest of
            # the bot state. The environment JSON remains a deployment-time
            # fallback for older installations.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS ad_product_routes (
                ad_id TEXT PRIMARY KEY,
                product TEXT NOT NULL CHECK(product IN ('sondesh', 'savoury', 'bento')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            for column, declaration in (
                ("original_draft", "TEXT"),
                ("edited", "INTEGER DEFAULT 0"),
                ("edited_by", "TEXT"),
                ("edited_at", "TIMESTAMP"),
                ("candidate_kind", "TEXT DEFAULT 'knowledge'"),
                ("normalized_question", "TEXT"),
                ("knowledge_key", "TEXT"),
            ):
                self._ensure_column(cursor, "candidate_templates", column, declaration)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS instagram_source_routes (
                routing_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                canonical_url TEXT UNIQUE,
                shortcode TEXT,
                media_id TEXT,
                ad_id TEXT,
                campaign_id TEXT,
                routing_topic TEXT NOT NULL DEFAULT '',
                source_description TEXT NOT NULL DEFAULT '',
                knowledge_scope TEXT NOT NULL DEFAULT '[]',
                escalation_policy TEXT NOT NULL DEFAULT 'human_if_unsupported',
                priority_products TEXT NOT NULL DEFAULT '[]',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                archived_at TIMESTAMP,
                menu_mode TEXT NOT NULL DEFAULT 'INHERIT',
                preview_url TEXT,
                media_type TEXT
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_instagram_routes_ad ON instagram_source_routes(ad_id, active)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_instagram_routes_media ON instagram_source_routes(media_id, active)")
            # A reel shared into a DM is matched by its normalized shortcode.
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_instagram_routes_shortcode ON instagram_source_routes(shortcode, active)")

            # Posts and Reels are an inventory, not routing rows.  Keep this
            # registry separate so discovery can scale without turning every
            # item into a customer-facing source route.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS instagram_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instagram_media_id TEXT UNIQUE,
                media_type TEXT NOT NULL,
                permalink TEXT,
                shortcode TEXT,
                caption TEXT,
                media_url TEXT,
                thumbnail_url TEXT,
                published_at TIMESTAMP,
                route_mode TEXT NOT NULL DEFAULT 'DEFAULT'
                    CHECK(route_mode IN ('DEFAULT', 'CUSTOM', 'DISABLED')),
                topic_id TEXT,
                category TEXT,
                custom_route_id INTEGER,
                sync_status TEXT NOT NULL DEFAULT 'DISCOVERED',
                needs_review INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """)
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_instagram_media_permalink ON instagram_media(permalink) WHERE permalink IS NOT NULL")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_instagram_media_shortcode ON instagram_media(shortcode)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_instagram_media_gallery ON instagram_media(media_type, route_mode, needs_review, published_at DESC)")

            # A Story reply is useful evidence even before an operator maps it
            # to a Topic. Keep this registry separate from routes so an
            # unmapped Story is visible in admin without becoming customer-
            # servable context, and so temporary CDN URLs never become the
            # identity of the Story.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS instagram_story_sources (
                story_media_id TEXT PRIMARY KEY,
                temporary_url TEXT,
                story_status TEXT,
                first_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_sender_id TEXT,
                last_event_metadata TEXT NOT NULL DEFAULT '{}',
                admin_deleted INTEGER NOT NULL DEFAULT 0,
                deleted_at TIMESTAMP,
                deleted_by TEXT,
                restored_at TIMESTAMP,
                restored_by TEXT
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_instagram_story_sources_seen ON instagram_story_sources(last_seen)")
            # Story sync metadata is additive.  The webhook evidence fields
            # above remain the source of truth for existing captured Stories;
            # these fields let the opt-in Meta inventory keep its own safe
            # lifecycle data without changing route identity or mappings.
            for column, declaration in (
                ("media_type", "TEXT"),
                ("temporary_url", "TEXT"),
                ("permalink", "TEXT"),
                ("published_at", "TIMESTAMP"),
                ("source", "TEXT NOT NULL DEFAULT 'WEBHOOK_STORY_REPLY'"),
            ):
                self._ensure_column(cursor, "instagram_story_sources", column, declaration)
            # Legacy tombstone columns remain for migration compatibility;
            # current application behavior never writes or restores them.
            self._ensure_column(cursor, "instagram_story_sources", "admin_deleted", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "instagram_story_sources", "deleted_at", "TIMESTAMP")
            self._ensure_column(cursor, "instagram_story_sources", "deleted_by", "TEXT")
            self._ensure_column(cursor, "instagram_story_sources", "restored_at", "TIMESTAMP")
            self._ensure_column(cursor, "instagram_story_sources", "restored_by", "TEXT")

            # Phase 3C-A.1 lifecycle: distinguishes a Story that naturally
            # expired from one that disappeared from Meta's inventory before
            # its normal lifetime — the latter only after two consecutive
            # COMPLETE successful syncs confirm it absent (see
            # reconcile_instagram_story_lifecycle), then delete it locally.
            self._ensure_column(cursor, "instagram_story_sources", "sync_miss_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(cursor, "instagram_story_sources", "last_confirmed_active_at", "TIMESTAMP")
            self._ensure_column(cursor, "instagram_story_sources", "removed_at", "TIMESTAMP")
            self._ensure_column(cursor, "instagram_story_sources", "removed_reason", "TEXT")
            # Phase 3C-A.1 sequence: an admin-facing ordinal ("2nd Story"),
            # never a routing identity. Assigned once and never renumbered —
            # see backfill_instagram_story_sequence_numbers.
            self._ensure_column(cursor, "instagram_story_sources", "story_sequence_number", "INTEGER")
            self._ensure_column(cursor, "instagram_story_sources", "story_sequence_date", "TEXT")

            # One short-lived lease protects an idempotent sync when more than
            # one process is serving the same durable SQLite volume.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS instagram_story_sync_locks (
                lock_name TEXT PRIMARY KEY,
                owner TEXT,
                locked_until REAL NOT NULL DEFAULT 0
            );
            """)

            # A shared post carries no shortcode/URL Meta will put in a
            # webhook, and its messaging-attachment id has not been shown to
            # correspond to any id the Graph API will confirm — so a post
            # route can only get a verified media_id through an admin
            # deliberately pairing one. One row per pairing attempt; the
            # deliberately short lifetime and one-active-at-a-time rule are
            # enforced by the application, not this table.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS instagram_post_pairings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                routing_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                started_by TEXT,
                bound_media_id TEXT,
                bound_at TIMESTAMP
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_post_pairings_status ON instagram_post_pairings(status, expires_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_post_pairings_routing ON instagram_post_pairings(routing_id, id DESC)")

            # A permanent-delete audit trail. Kept even after the route row
            # itself is gone, and deliberately small — no customer data, just
            # enough to answer "what was this route and who removed it."
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS instagram_route_removals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                routing_id INTEGER NOT NULL,
                canonical_url TEXT,
                source_type TEXT,
                shortcode TEXT,
                routing_topic TEXT,
                removed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                removed_by TEXT
            );
            """)

            for column, declaration in (
                ("source_routing_id", "INTEGER"),
                ("source_match_method", "TEXT"),
                ("source_type", "TEXT"),
                ("shortcode", "TEXT"),
                ("media_id", "TEXT"),
                ("routing_topic", "TEXT"),
                ("source_context_used", "INTEGER DEFAULT 0"),
                ("source_context_available", "INTEGER DEFAULT 0"),
                ("source_referential_intent", "TEXT"),
                ("source_media_present", "INTEGER DEFAULT 0"),
                ("customer_text_accompanied_media", "INTEGER DEFAULT 0"),
            ):
                self._ensure_column(cursor, "conversations", column, declaration)

            # Dynamic menu catalogue: the registry of every menu the Visual
            # Menu Manager and Instagram routing know about — the durable
            # image/version machinery in menu_image_versions already works
            # against any menu_key string, so a new menu needs nothing more
            # than a row here to become real everywhere it is referenced.
            # `is_builtin` marks the five menus that predate this catalogue
            # (their image history already exists under these exact keys);
            # it is informational only and gates nothing.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS menu_catalogue (
                menu_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Other',
                description TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                is_builtin INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            existing_menu_columns = {row[1] for row in cursor.execute("PRAGMA table_info(menu_catalogue)")}
            self._ensure_column(cursor, "menu_catalogue", "customer_topic", "TEXT")
            self._ensure_column(cursor, "menu_catalogue", "display_order", "INTEGER NOT NULL DEFAULT 100")
            self._ensure_column(cursor, "menu_catalogue", "legacy_customer_eligible", "INTEGER NOT NULL DEFAULT 0")

            # Menu categories are operator-managed data, not application
            # constants.  Menus retain their human-readable category string
            # for backwards compatibility with existing routes and exports;
            # category rename and delete/reassign operations update those
            # strings atomically, so no catalogue row can point at a missing
            # category.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS menu_categories (
                category_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                is_system INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_menu_categories_normalized_name "
                "ON menu_categories(name COLLATE NOCASE)"
            )
            if not cursor.execute("SELECT 1 FROM menu_categories LIMIT 1").fetchone():
                for name, is_system in (
                    ("Sondesh", False), ("Weekly Vegetarian", False),
                    ("Weekly Non-Vegetarian", False), ("Bento / Meal", False),
                    ("Subscription", False), ("Event", False), ("Other", True),
                ):
                    cursor.execute(
                        "INSERT OR IGNORE INTO menu_categories (name, active, is_system) VALUES (?, 1, ?)",
                        (name, int(is_system)),
                    )
            cursor.execute(
                "INSERT OR IGNORE INTO menu_categories (name, active, is_system) VALUES ('Other', 1, 1)"
            )
            # Additive taxonomy metadata belongs to DB initialization, never
            # a read of the Admin catalogue. Existing labels and menu rows
            # remain untouched across restarts.
            cursor.execute(
                "INSERT OR IGNORE INTO menu_categories (name, active, is_system) "
                "VALUES ('Savoury Menu', 1, 0)"
            )
            cursor.execute("""CREATE TABLE IF NOT EXISTS menu_customer_topics (
                topic_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                routing_key TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                is_system INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_menu_customer_topics_name "
                "ON menu_customer_topics(name COLLATE NOCASE)"
            )
            for routing_key, name in MENU_TOPIC_DEFAULTS.items():
                cursor.execute(
                    "INSERT OR IGNORE INTO menu_customer_topics "
                    "(topic_id,name,routing_key,active,is_system) VALUES (?,?,?,1,1)",
                    (routing_key, name, routing_key),
                )

            # A hard-deleted built-in must not be recreated by the seed loop
            # below on the next process start. This table intentionally keeps
            # only the stable id and deletion audit fields; menu content,
            # images, drafts, and versions are removed by the delete operation.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS menu_deletion_tombstones (
                menu_id TEXT PRIMARY KEY,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deleted_by TEXT
            );
            """)

            # Instagram Content Routing topics: a reusable label an operator
            # picks once and every future route reuses, instead of retyping
            # the same topic as free text on each route. `priority_products`
            # and `knowledge_scope` here are the *defaults* a route is
            # pre-filled with at creation time, not a live reference read at
            # matching time — the route's own columns remain what the
            # matcher and responder actually act on, so nothing about
            # existing routing behavior changes shape.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS instagram_topics (
                topic_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                default_menu_id TEXT,
                priority_products TEXT NOT NULL DEFAULT '[]',
                knowledge_scope TEXT NOT NULL DEFAULT '[]',
                response_order TEXT NOT NULL DEFAULT 'description_first',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            self._ensure_column(cursor, "instagram_topics", "response_order", "TEXT NOT NULL DEFAULT 'description_first'")
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_topics_normalized_name ON instagram_topics(name COLLATE NOCASE)")

            self._ensure_column(cursor, "instagram_source_routes", "topic_id", "TEXT")
            self._ensure_column(cursor, "instagram_source_routes", "source_description", "TEXT NOT NULL DEFAULT ''")
            # The menu shown for this route. Defaults from the selected
            # Topic's default_menu_id at save time (see validate_route_payload)
            # but is its own column so a route can override it independently —
            # "the client may override it if needed" without changing the Topic.
            self._ensure_column(cursor, "instagram_source_routes", "menu_id", "TEXT")
            self._ensure_column(cursor, "instagram_source_routes", "menu_mode", "TEXT NOT NULL DEFAULT 'INHERIT'")
            self._ensure_column(cursor, "instagram_source_routes", "preview_url", "TEXT")
            self._ensure_column(cursor, "instagram_source_routes", "media_type", "TEXT")

            # Admin-only visual fallback for connected media whose Meta
            # preview is unavailable. This table is deliberately separate
            # from the routing identity and is never read by the matcher.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS instagram_route_previews (
                routing_id INTEGER PRIMARY KEY,
                image_path TEXT NOT NULL,
                content_type TEXT NOT NULL,
                uploaded_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            # Existing rows with a concrete route menu are already configured;
            # retain their behavior. Null-menu legacy rows remain INHERIT until
            # an operator explicitly saves NONE or SELECTED through the admin UI.
            cursor.execute(
                "UPDATE instagram_source_routes SET menu_mode='SELECTED' "
                "WHERE menu_id IS NOT NULL AND (menu_mode IS NULL OR menu_mode='INHERIT')"
            )

            # Menu Manager: one row per unpublished edit in progress. A menu_key
            # ("collections.aamar_mishti", "weekly.vegetarian", ...) has at most
            # one draft — a second save overwrites it rather than accumulating.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS menu_drafts (
                menu_key TEXT PRIMARY KEY,
                draft_json TEXT NOT NULL,
                updated_by TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # Menu Manager: one row per publish/restore, holding the full
            # resulting item list for that menu_key — the lightweight version
            # history the client can review or roll back to.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS menu_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                menu_key TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT 'publish',
                changed_by TEXT,
                changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_menu_versions_key ON menu_versions(menu_key, id DESC);")
            # Additive review metadata: existing menus and versions remain untouched.
            for table, column, declaration in (
                ("menu_drafts", "review_json", "TEXT"),
                ("menu_versions", "approval_json", "TEXT"),
                ("menu_versions", "is_current", "INTEGER NOT NULL DEFAULT 0"),
                ("menu_versions", "valid_from", "TEXT"),
                ("menu_versions", "valid_until", "TEXT"),
                ("menu_versions", "date_provenance", "TEXT"),
                ("menu_versions", "weekly_state", "TEXT"),
                ("menu_versions", "fallback_until_at", "TEXT"),
            ):
                columns = {r[1] for r in cursor.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_menu_versions_weekly_range "
                           "ON menu_versions(menu_key, valid_from, valid_until)")
            cursor.execute("""CREATE TABLE IF NOT EXISTS menu_review_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id TEXT NOT NULL,
                menu_key TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
                details_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")

            # Visual Menu Manager: durable image versions per menu_key. Image
            # bytes live on the same durable volume as menu.json; this table
            # holds only the metadata needed to make one version current.
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS menu_image_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                menu_key TEXT NOT NULL,
                image_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                version INTEGER NOT NULL,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                uploaded_by TEXT,
                published_at TIMESTAMP,
                archived_at TIMESTAMP
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_menu_image_versions_key ON menu_image_versions(menu_key, version DESC)")
            self._ensure_column(cursor, "menu_image_versions", "review_version_id", "INTEGER")

            # Seed counters if empty
            cursor.execute("INSERT OR IGNORE INTO analytics (key, value) VALUES ('total_messages', 0);")
            cursor.execute("INSERT OR IGNORE INTO analytics (key, value) VALUES ('matched_responses', 0);")
            cursor.execute("INSERT OR IGNORE INTO analytics (key, value) VALUES ('escalations_count', 0);")

            # The five menus that predate the dynamic menu catalogue. Seeded
            # here — not from a separate startup call — so every Database
            # instance (production, or an isolated one built fresh in a
            # test) has them without remembering to ask; INSERT OR IGNORE
            # means an operator's later edits to a built-in's description
            # are never overwritten by this running again.
            for menu_id, name, category, description in (
                ("aamar_mishti", "Aamar Mishti Collection", "Sondesh",
                 "Everyday Bengali Sondesh collection, made with local dairy milk."),
                ("signature", "Signature Collection", "Sondesh",
                 "Sondesh for special occasions and gifting, made with A2 cow milk."),
                ("weekly_vegetarian", "Weekly Vegetarian", "Weekly Vegetarian",
                 "This week's fresh vegetarian Bengali dishes."),
                ("weekly_non_vegetarian", "Weekly Non-Vegetarian", "Weekly Non-Vegetarian",
                 "This week's fresh non-vegetarian Bengali dishes."),
                ("bento", "Bento / Meal Menu", "Bento / Meal", "Ready-to-eat Bengali meal boxes."),
            ):
                if not cursor.execute(
                    "SELECT 1 FROM menu_deletion_tombstones WHERE menu_id = ?",
                    (menu_id,),
                ).fetchone():
                    cursor.execute(
                        "INSERT OR IGNORE INTO menu_catalogue (menu_id, name, category, description, active, is_builtin) "
                        "VALUES (?, ?, ?, ?, 1, 1)",
                        (menu_id, name, category, description),
                    )
            if "legacy_customer_eligible" not in existing_menu_columns:
                # Preserve already-reviewed custom menus that reached the
                # customer before explicit topic links existed. New menus do
                # not inherit this exception.
                cursor.execute(
                    "UPDATE menu_catalogue SET legacy_customer_eligible=1 WHERE is_builtin=0 "
                    "AND menu_id IN (SELECT DISTINCT menu_key FROM menu_versions WHERE approval_json IS NOT NULL)"
                )
            if "customer_topic" not in existing_menu_columns:
                # Only the five built-ins have unambiguous, pre-existing
                # customer relationships. A newly added menu stays unlinked.
                for menu_id, topic, position in (
                    ("aamar_mishti", "SONDESH", 10),
                    ("signature", "SONDESH", 20),
                    ("weekly_vegetarian", "WEEKLY_SAVOURY", 10),
                    ("weekly_non_vegetarian", "WEEKLY_SAVOURY", 20),
                    ("bento", "BENTO_MEAL", 10),
                ):
                    cursor.execute(
                        "UPDATE menu_catalogue SET customer_topic=?, display_order=? "
                        "WHERE menu_id=? AND is_builtin=1",
                        (topic, position, menu_id),
                    )

            # A database upgraded from a version that allowed a category not
            # in the initial seed still has a durable, selectable category
            # after migration.  It is intentionally not re-created once an
            # operator has deleted it: all menu rows are reassigned first.
            for row in cursor.execute(
                "SELECT DISTINCT TRIM(category) AS name FROM menu_catalogue WHERE TRIM(category) <> ''"
            ).fetchall():
                cursor.execute(
                    "INSERT OR IGNORE INTO menu_categories (name, active, is_system) VALUES (?, 1, 0)",
                    (row["name"],),
                )

            self._create_indexes(cursor)

            conn.commit()

        # Retire only the explicitly identified, outdated Home Table event
        # answers after the base schema is committed. This keeps startup
        # migration-safe and leaves current Kolkata/business knowledge intact.
        self._retire_outdated_home_table_knowledge()

        # Existing Phase 3C-A rows predate sequence numbering; backfill them
        # deterministically once the schema above is committed. Uses its own
        # connection, so it must run after the migration transaction closes.
        self.backfill_instagram_story_sequence_numbers()

    def set_ad_product_route(self, ad_id: str, product: str) -> Dict[str, Any]:
        """Creates or updates the product shown first for one Meta ad ID."""
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO ad_product_routes (ad_id, product, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ad_id) DO UPDATE SET
                    product = excluded.product,
                    updated_at = excluded.updated_at
                """,
                (ad_id, product, now, now),
            )
            conn.commit()
        return {"ad_id": ad_id, "product": product, "updated_at": now}

    def get_ad_product_route(self, ad_id: str) -> Optional[str]:
        """Returns an admin-managed product route for one ad, if configured."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT product FROM ad_product_routes WHERE ad_id = ?", (ad_id,)
            ).fetchone()
        return str(row["product"]) if row else None

    def list_ad_product_routes(self) -> List[Dict[str, Any]]:
        """Lists ad routes in most-recently-updated order for the portal."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT ad_id, product, created_at, updated_at "
                "FROM ad_product_routes ORDER BY updated_at DESC, ad_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_ad_product_route(self, ad_id: str) -> bool:
        """Deletes one mapping without affecting conversations from that ad."""
        with self.get_connection() as conn:
            cursor = conn.execute("DELETE FROM ad_product_routes WHERE ad_id = ?", (ad_id,))
            conn.commit()
        return cursor.rowcount > 0

    # --- Menu Manager: drafts and version history -------------------------

    def save_menu_draft(self, menu_key: str, draft_json: str, updated_by: Optional[str] = None) -> None:
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO menu_drafts (menu_key, draft_json, updated_by, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(menu_key) DO UPDATE SET
                    draft_json = excluded.draft_json,
                    updated_by = excluded.updated_by,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (menu_key, draft_json, updated_by),
            )
            conn.commit()

    def get_menu_draft(self, menu_key: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM menu_drafts WHERE menu_key = ?", (menu_key,)
            ).fetchone()
            return dict(row) if row else None

    def clear_menu_draft(self, menu_key: str) -> None:
        with self.get_connection() as conn:
            conn.execute("DELETE FROM menu_drafts WHERE menu_key = ?", (menu_key,))
            conn.commit()

    def record_menu_version(
        self, menu_key: str, snapshot_json: str, action: str = "publish",
        changed_by: Optional[str] = None,
    ) -> int:
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO menu_versions (menu_key, snapshot_json, action, changed_by)
                VALUES (?, ?, ?, ?)
                """,
                (menu_key, snapshot_json, action, changed_by),
            )
            conn.commit()
            return cursor.lastrowid

    def get_menu_versions(self, menu_key: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Newest first — the version a client wants to inspect or restore is
        almost always a recent one."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM menu_versions WHERE menu_key = ? ORDER BY id DESC LIMIT ?",
                (menu_key, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_menu_version(self, version_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM menu_versions WHERE id = ?", (version_id,)
            ).fetchone()
            return dict(row) if row else None

    # --- Visual Menu Manager: durable menu image versions ------------------

    def get_menu_image_states(self, menu_keys: list[str]) -> Dict[str, Any]:
        """Read requested image metadata without per-menu queries or writes."""
        keys = list(dict.fromkeys(menu_keys))
        states = {key: {"current": None, "draft": None, "archived": False} for key in keys}
        if not keys:
            return states
        try:
            with self.get_connection() as conn:
                placeholders = ",".join("?" for _ in keys)
                rows = conn.execute(
                    f"SELECT * FROM menu_image_versions WHERE menu_key IN ({placeholders})",
                    keys).fetchall()
            for raw in rows:
                row = dict(raw)
                state = states[row["menu_key"]]
                status = row["status"]
                if status == "archived":
                    state["archived"] = True
                elif status in ("current", "draft"):
                    field = "version" if status == "current" else "id"
                    previous = state[status]
                    if previous is None or row[field] > previous[field]:
                        state[status] = row
        except sqlite3.OperationalError:
            pass
        return states

    def get_menu_image_current(self, menu_key: str) -> Optional[Dict[str, Any]]:
        # Every responder reply checks this, so a table that has not been
        # created yet (a connection racing process startup, or a test
        # database recreated without re-running init_db) must read as "no
        # managed image" rather than take the whole reply down.
        try:
            with self.get_connection() as conn:
                row = conn.execute("SELECT * FROM menu_image_versions WHERE menu_key = ? AND status = 'current' ORDER BY version DESC LIMIT 1", (menu_key,)).fetchone()
                return dict(row) if row else None
        except sqlite3.OperationalError:
            return None

    def get_menu_image_draft(self, menu_key: str) -> Optional[Dict[str, Any]]:
        try:
            with self.get_connection() as conn:
                row = conn.execute("SELECT * FROM menu_image_versions WHERE menu_key = ? AND status = 'draft' ORDER BY id DESC LIMIT 1", (menu_key,)).fetchone()
                return dict(row) if row else None
        except sqlite3.OperationalError:
            return None

    def has_menu_image_archive(self, menu_key: str) -> bool:
        try:
            with self.get_connection() as conn:
                return conn.execute("SELECT 1 FROM menu_image_versions WHERE menu_key = ? AND status = 'archived' LIMIT 1", (menu_key,)).fetchone() is not None
        except sqlite3.OperationalError:
            return False

    def save_menu_image_draft(self, menu_key: str, image_path: str, uploaded_by: str) -> Dict[str, Any]:
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            conn.execute("DELETE FROM menu_image_versions WHERE menu_key = ? AND status = 'draft'", (menu_key,))
            next_version = (conn.execute("SELECT COALESCE(MAX(version), 0) + 1 AS n FROM menu_image_versions WHERE menu_key = ?", (menu_key,)).fetchone()["n"])
            cur = conn.execute("INSERT INTO menu_image_versions (menu_key, image_path, status, version, uploaded_at, uploaded_by) VALUES (?, ?, 'draft', ?, ?, ?)", (menu_key, image_path, next_version, now, uploaded_by))
            conn.commit()
            return dict(conn.execute("SELECT * FROM menu_image_versions WHERE id = ?", (cur.lastrowid,)).fetchone())

    def publish_menu_image(self, menu_key: str) -> Optional[Dict[str, Any]]:
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            draft = conn.execute("SELECT * FROM menu_image_versions WHERE menu_key = ? AND status = 'draft' ORDER BY id DESC LIMIT 1", (menu_key,)).fetchone()
            if not draft:
                return None
            conn.execute("UPDATE menu_image_versions SET status = 'archived', archived_at = ? WHERE menu_key = ? AND status = 'current'", (now, menu_key))
            conn.execute("UPDATE menu_image_versions SET status = 'current', published_at = ? WHERE id = ?", (now, draft["id"]))
            conn.commit()
            return dict(conn.execute("SELECT * FROM menu_image_versions WHERE id = ?", (draft["id"],)).fetchone())

    def archive_menu_image(self, menu_key: str) -> Optional[Dict[str, Any]]:
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM menu_image_versions WHERE menu_key = ? AND status = 'current' ORDER BY version DESC LIMIT 1", (menu_key,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE menu_image_versions SET status = 'archived', archived_at = ? WHERE id = ?", (now, row["id"]))
            conn.commit()
            return dict(conn.execute("SELECT * FROM menu_image_versions WHERE id = ?", (row["id"],)).fetchone())

    def create_menu_image_archive(self, menu_key: str, image_path: str, uploaded_by: str) -> Dict[str, Any]:
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 AS n FROM menu_image_versions WHERE menu_key = ?", (menu_key,)).fetchone()["n"]
            cur = conn.execute("INSERT INTO menu_image_versions (menu_key, image_path, status, version, uploaded_at, uploaded_by, archived_at) VALUES (?, ?, 'archived', ?, ?, ?, ?)", (menu_key, image_path, version, now, uploaded_by, now))
            conn.commit()
            return dict(conn.execute("SELECT * FROM menu_image_versions WHERE id = ?", (cur.lastrowid,)).fetchone())

    def list_menu_image_history(self, menu_key: str) -> list[Dict[str, Any]]:
        with self.get_connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM menu_image_versions WHERE menu_key = ? AND status != 'draft' ORDER BY version DESC", (menu_key,)).fetchall()]

    def restore_menu_image(self, menu_key: str, version_id: int) -> Optional[Dict[str, Any]]:
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            target = conn.execute("SELECT * FROM menu_image_versions WHERE id = ? AND menu_key = ? AND status = 'archived'", (version_id, menu_key)).fetchone()
            if not target:
                return None
            conn.execute("UPDATE menu_image_versions SET status = 'archived', archived_at = ? WHERE menu_key = ? AND status = 'current'", (now, menu_key))
            conn.execute("UPDATE menu_image_versions SET status = 'current', published_at = ?, archived_at = NULL WHERE id = ?", (now, version_id))
            conn.commit()
            return dict(conn.execute("SELECT * FROM menu_image_versions WHERE id = ?", (version_id,)).fetchone())

    def upsert_instagram_media(self, media: Dict[str, Any]) -> Dict[str, Any]:
        """Upsert Graph metadata without changing an operator's route mode."""
        now = datetime.utcnow().isoformat()
        media_id = str(media.get("instagram_media_id") or "").strip()
        if not media_id:
            raise ValueError("instagram_media_id is required")
        with self.get_connection() as conn:
            existing = conn.execute(
                "SELECT * FROM instagram_media WHERE instagram_media_id=?", (media_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE instagram_media SET media_type=?, permalink=?, shortcode=?, caption=?,
                       media_url=?, thumbnail_url=?, published_at=?, sync_status=?, last_seen_at=?, updated_at=?
                       WHERE instagram_media_id=?""",
                    (media.get("media_type") or existing["media_type"], media.get("permalink"),
                     media.get("shortcode"), media.get("caption"), media.get("media_url"),
                     media.get("thumbnail_url"), media.get("published_at"), media.get("sync_status", "SYNCED"),
                     now, now, media_id),
                )
            else:
                conn.execute(
                    """INSERT INTO instagram_media (instagram_media_id, media_type, permalink, shortcode, caption,
                       media_url, thumbnail_url, published_at, route_mode, sync_status, needs_review, active,
                       first_seen_at, last_seen_at, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DEFAULT', ?, 0, 1, ?, ?, ?, ?)""",
                    (media_id, media.get("media_type") or "IMAGE", media.get("permalink"), media.get("shortcode"),
                     media.get("caption"), media.get("media_url"), media.get("thumbnail_url"), media.get("published_at"),
                     media.get("sync_status", "SYNCED"), now, now, now, now),
                )
            conn.commit()
            return dict(conn.execute("SELECT * FROM instagram_media WHERE instagram_media_id=?", (media_id,)).fetchone())

    def list_instagram_media(self, *, page: int = 1, page_size: int = 24, media_type: Optional[str] = None,
                             route_mode: Optional[str] = None, needs_review: Optional[bool] = None,
                             topic_id: Optional[str] = None, search: Optional[str] = None,
                             sort: str = "newest", source_type: Optional[str] = None,
                             mapping_status: Optional[str] = None,
                             context_active: Optional[bool] = None) -> tuple[List[Dict[str, Any]], int]:
        clauses, params = ["1=1"], []
        if source_type == "reel":
            clauses.append("media_type='VIDEO'")
        elif source_type == "post":
            clauses.append("media_type!='VIDEO'")
        if media_type:
            clauses.append("media_type=?"); params.append(media_type.upper())
        if route_mode:
            clauses.append("route_mode=?"); params.append(route_mode.upper())
        if needs_review is not None:
            clauses.append("needs_review=?"); params.append(int(needs_review))
        if topic_id:
            clauses.append("topic_id=?"); params.append(topic_id)
        if mapping_status == "mapped":
            clauses.append("(topic_id IS NOT NULL OR route_mode IN ('CUSTOM','DISABLED') OR EXISTS (SELECT 1 FROM instagram_source_routes r WHERE r.media_id=instagram_media.instagram_media_id AND r.source_type IN ('post','reel') AND r.archived_at IS NULL))")
        elif mapping_status == "unmapped":
            clauses.append("(topic_id IS NULL AND route_mode='DEFAULT' AND NOT EXISTS (SELECT 1 FROM instagram_source_routes r WHERE r.media_id=instagram_media.instagram_media_id AND r.source_type IN ('post','reel') AND r.archived_at IS NULL))")
        if context_active is not None:
            clauses.append("EXISTS (SELECT 1 FROM instagram_source_routes r WHERE r.media_id=instagram_media.instagram_media_id AND r.source_type IN ('post','reel') AND r.archived_at IS NULL AND r.active=?)")
            params.append(int(context_active))
        if search:
            pattern = f"%{search[:200]}%"
            clauses.append("(instagram_media_id LIKE ? OR permalink LIKE ? OR caption LIKE ? OR EXISTS (SELECT 1 FROM instagram_source_routes r WHERE r.media_id=instagram_media.instagram_media_id AND r.source_type IN ('post','reel') AND r.archived_at IS NULL AND (r.routing_topic LIKE ? OR r.source_description LIKE ?)))")
            params.extend([pattern] * 5)
        where = " AND ".join(clauses)
        direction = "ASC" if sort == "oldest" else "DESC"
        order_column = "updated_at" if sort == "recently_edited" else "published_at"
        with self.get_connection() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) AS n FROM instagram_media WHERE {where}", params).fetchone()["n"])
            rows = conn.execute(
                f"SELECT * FROM instagram_media WHERE {where} ORDER BY {order_column} {direction}, id {direction} LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return [dict(row) for row in rows], total

    def get_instagram_media(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM instagram_media WHERE id=?", (item_id,)).fetchone()
            return dict(row) if row else None

    def get_instagram_media_by_meta_id(self, media_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM instagram_media WHERE instagram_media_id=?", (media_id,)).fetchone()
            return dict(row) if row else None

    def update_instagram_media_route(self, item_id: int, *, route_mode: str, topic_id: Optional[str],
                                     custom_route_id: Optional[int], needs_review: bool) -> Optional[Dict[str, Any]]:
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            conn.execute("UPDATE instagram_media SET route_mode=?, topic_id=?, custom_route_id=?, needs_review=?, updated_at=? WHERE id=?",
                         (route_mode, topic_id, custom_route_id, int(needs_review), now, item_id))
            conn.commit()
            row = conn.execute("SELECT * FROM instagram_media WHERE id=?", (item_id,)).fetchone()
            return dict(row) if row else None

    def save_instagram_source_route(self, route: Dict[str, Any], routing_id: Optional[int] = None) -> Dict[str, Any]:
        """Create/update one source route, deduplicating URL and identifiers."""
        now = datetime.utcnow().isoformat()
        menu_mode = str(route.get("menu_mode") or "").strip().upper()
        if menu_mode not in {"NONE", "SELECTED", "INHERIT"}:
            menu_mode = "SELECTED" if route.get("menu_id") else "INHERIT"
        values = (
            route["source_type"], route.get("canonical_url"), route.get("shortcode"),
            route.get("media_id"), route.get("ad_id"), route.get("campaign_id"),
            route.get("routing_topic", ""), route.get("source_description", ""), json.dumps(route.get("knowledge_scope", [])),
            route.get("escalation_policy", "human_if_unsupported"),
            json.dumps(route.get("priority_products", [])), int(bool(route.get("active", True))),
            route.get("topic_id"), route.get("menu_id"), menu_mode,
            route.get("preview_url"), route.get("media_type"), now,
        )
        with self.get_connection() as conn:
            if routing_id is None:
                for field in ("canonical_url", "ad_id", "media_id"):
                    if not route.get(field):
                        continue
                    existing = conn.execute(
                        f"SELECT routing_id FROM instagram_source_routes WHERE {field} = ? AND archived_at IS NULL",
                        (route[field],),
                    ).fetchone()
                    if existing:
                        routing_id = int(existing["routing_id"])
                        break
            if routing_id is None:
                cursor = conn.execute(
                    """INSERT INTO instagram_source_routes
                    (source_type, canonical_url, shortcode, media_id, ad_id, campaign_id,
                     routing_topic, source_description, knowledge_scope, escalation_policy, priority_products,
                    active, topic_id, menu_id, menu_mode, preview_url, media_type, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values[:-1] + (now, now),
                )
                routing_id = int(cursor.lastrowid)
            else:
                cursor = conn.execute(
                    """UPDATE instagram_source_routes SET source_type=?, canonical_url=?, shortcode=?,
                    media_id=?, ad_id=?, campaign_id=?, routing_topic=?, source_description=?, knowledge_scope=?,
                    escalation_policy=?, priority_products=?, active=?, topic_id=?, menu_id=?, menu_mode=?,
                    preview_url=?, media_type=?, updated_at=?, archived_at=NULL
                    WHERE routing_id=?""",
                    values + (routing_id,),
                )
                if cursor.rowcount == 0:
                    raise ValueError("Routing record not found.")
            conn.commit()
            row = conn.execute(
                "SELECT * FROM instagram_source_routes WHERE routing_id = ?", (routing_id,)
            ).fetchone()
        return dict(row)

    def update_instagram_source_route_metadata(
        self, routing_id: int, *, preview_url: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update display-only media metadata without touching routing fields."""
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE instagram_source_routes SET preview_url=?, media_type=?, updated_at=? "
                "WHERE routing_id=?",
                (preview_url, media_type, now, routing_id),
            )
            if not cursor.rowcount:
                return None
            conn.commit()
            row = conn.execute(
                "SELECT * FROM instagram_source_routes WHERE routing_id = ?", (routing_id,)
            ).fetchone()
        return dict(row)

    def get_instagram_route_preview(self, routing_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM instagram_route_previews WHERE routing_id = ?", (routing_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_instagram_route_previews(self) -> Dict[int, Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM instagram_route_previews").fetchall()
        return {int(row["routing_id"]): dict(row) for row in rows}

    def save_instagram_route_preview(
        self, routing_id: int, image_path: str, content_type: str, uploaded_by: str,
    ) -> Optional[Dict[str, Any]]:
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM instagram_source_routes WHERE routing_id = ?", (routing_id,)
            ).fetchone():
                return None
            old = conn.execute(
                "SELECT * FROM instagram_route_previews WHERE routing_id = ?", (routing_id,)
            ).fetchone()
            conn.execute(
                """
                INSERT INTO instagram_route_previews
                    (routing_id, image_path, content_type, uploaded_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(routing_id) DO UPDATE SET
                    image_path=excluded.image_path,
                    content_type=excluded.content_type,
                    uploaded_by=excluded.uploaded_by,
                    updated_at=excluded.updated_at
                """,
                (routing_id, image_path, content_type, uploaded_by, now, now),
            )
            conn.commit()
        return dict(old) if old else None

    def delete_instagram_route_preview(self, routing_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM instagram_route_previews WHERE routing_id = ?", (routing_id,)
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "DELETE FROM instagram_route_previews WHERE routing_id = ?", (routing_id,)
            )
            conn.commit()
        return dict(row)

    def list_instagram_source_routes(self, include_archived: bool = False,
                                     media_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if not include_archived:
            clauses.append("archived_at IS NULL")
        if media_ids is not None:
            if not media_ids:
                return []
            clauses.append(f"media_id IN ({','.join('?' for _ in media_ids)})")
            params.extend(media_ids)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.get_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM instagram_source_routes {where} ORDER BY updated_at DESC, routing_id DESC",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_instagram_source_route(self, routing_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM instagram_source_routes WHERE routing_id = ?", (routing_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_instagram_source_route(
        self, field: str, value: str, source_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        # The column name is interpolated, so the allowlist is the thing
        # keeping this query safe. Widening it is adding a name to the list,
        # never relaxing the check.
        if field not in ("ad_id", "media_id", "shortcode"):
            raise ValueError("Unsupported route identifier.")
        with self.get_connection() as conn:
            where = (
                f"{field} = ? AND active = 1 AND archived_at IS NULL"
                + (" AND source_type = ?" if source_type else "")
                + (
                    " AND NOT EXISTS (SELECT 1 FROM instagram_story_sources "
                    "WHERE story_media_id = instagram_source_routes.media_id "
                    "AND admin_deleted = 1)"
                    if source_type == "story" else ""
                )
            )
            params = (value, source_type) if source_type else (value,)
            row = conn.execute(
                f"SELECT * FROM instagram_source_routes WHERE {where} "
                "ORDER BY updated_at DESC LIMIT 1",
                params,
            ).fetchone()
        return dict(row) if row else None

    def record_instagram_story_source(
        self,
        story_media_id: str,
        *,
        temporary_url: Optional[str] = None,
        story_status: Optional[str] = None,
        sender_id: Optional[str] = None,
        event_metadata: Optional[Dict[str, Any]] = None,
        seen_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Record exact Story evidence without making it a route.

        ``story_media_id`` is the only identity used for a later mapping.
        A supplied URL is display/diagnostic metadata only and is never used
        by ``find_instagram_source_route``.
        """
        media_id = str(story_media_id or "").strip()
        if not media_id:
            return None
        now = seen_at or datetime.now(timezone.utc).isoformat()
        metadata = json.dumps(event_metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO instagram_story_sources
                    (story_media_id, temporary_url, story_status, first_seen,
                     last_seen, last_sender_id, last_event_metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(story_media_id) DO UPDATE SET
                    temporary_url=COALESCE(excluded.temporary_url, instagram_story_sources.temporary_url),
                    story_status=COALESCE(excluded.story_status, instagram_story_sources.story_status),
                    last_seen=excluded.last_seen,
                    last_sender_id=COALESCE(excluded.last_sender_id, instagram_story_sources.last_sender_id),
                    last_event_metadata=excluded.last_event_metadata
                """,
                (media_id, temporary_url, story_status, now, now, sender_id, metadata),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM instagram_story_sources WHERE story_media_id = ?", (media_id,)
            ).fetchone()
        return dict(row) if row else None

    def upsert_instagram_story_sync(
        self,
        story_media_id: str,
        *,
        media_type: Optional[str] = None,
        temporary_url: Optional[str] = None,
        permalink: Optional[str] = None,
        published_at: Optional[str] = None,
        event_metadata: Optional[Dict[str, Any]] = None,
        seen_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Upsert one exact Story returned by Meta's owned-Stories edge.

        A sync can refresh Meta metadata but never creates or edits mapping
        state. Story rows are current operational records, not history.
        """
        media_id = str(story_media_id or "").strip()
        if not media_id:
            return None
        now = seen_at or datetime.now(timezone.utc).isoformat()
        metadata = json.dumps(event_metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO instagram_story_sources
                    (story_media_id, temporary_url, story_status, first_seen,
                     last_seen, last_event_metadata, media_type, permalink,
                     published_at, source, sync_miss_count, last_confirmed_active_at)
                VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, 'META_STORY_SYNC', 0, ?)
                ON CONFLICT(story_media_id) DO UPDATE SET
                    temporary_url=COALESCE(excluded.temporary_url, instagram_story_sources.temporary_url),
                    story_status='ACTIVE',
                    sync_miss_count=0,
                    last_confirmed_active_at=excluded.last_confirmed_active_at,
                    last_seen=excluded.last_seen,
                    last_event_metadata=excluded.last_event_metadata,
                    media_type=COALESCE(excluded.media_type, instagram_story_sources.media_type),
                    permalink=COALESCE(excluded.permalink, instagram_story_sources.permalink),
                    published_at=COALESCE(excluded.published_at, instagram_story_sources.published_at),
                    source='META_STORY_SYNC'
                """,
                (media_id, temporary_url, now, now, metadata,
                 media_type, permalink, published_at, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM instagram_story_sources WHERE story_media_id = ?", (media_id,)
            ).fetchone()
        return dict(row) if row else None

    def reconcile_instagram_story_lifecycle(
        self, active_story_ids: List[str], *, expired_before: str, now: Optional[str] = None,
    ) -> Dict[str, int]:
        """Apply Story lifecycle rules after one COMPLETE successful Meta fetch.

        Only call this once ``active_story_ids`` reflects everything Meta's
        owned-Stories edge returned in a fully successful (non-partial, non-
        failed) fetch — a failure, timeout, rate limit, or malformed/partial
        page must never reach here, since only a complete successful fetch is
        trustworthy evidence that an ACTIVE Story is actually absent.

        Expired Stories are hard-deleted only when a trustworthy publication
        timestamp confirms their lifetime has passed. Other absent Stories
        survive one complete miss and are hard-deleted after the second
        consecutive complete miss. Failed, partial, rate-limited, malformed,
        or busy syncs never call this method. Identity is the exact Meta ID.
        """
        ids = [str(value or "").strip() for value in active_story_ids if str(value or "").strip()]
        expired_before_dt = _parse_trustworthy_datetime(expired_before)
        counts = {"expired": 0, "removed": 0, "miss_pending": 0}
        with self.get_connection() as conn:
            params: List[Any] = []
            where = "COALESCE(story_status, 'ACTIVE')='ACTIVE' AND admin_deleted=0"
            if ids:
                where += f" AND story_media_id NOT IN ({','.join('?' for _ in ids)})"
                params.extend(ids)
            missing_rows = conn.execute(
                f"SELECT story_media_id, published_at, sync_miss_count FROM instagram_story_sources WHERE {where}",
                tuple(params),
            ).fetchall()
            for row in missing_rows:
                story_id = row["story_media_id"]
                published_dt = _parse_trustworthy_datetime(row["published_at"])
                miss_count = int(row["sync_miss_count"] or 0)
                if published_dt is not None and expired_before_dt is not None and published_dt <= expired_before_dt:
                    self._delete_instagram_story_source_in_connection(conn, story_id)
                    counts["expired"] += 1
                elif miss_count >= 1:
                    self._delete_instagram_story_source_in_connection(conn, story_id)
                    counts["removed"] += 1
                else:
                    # One complete absence is insufficient evidence. Unknown
                    # timestamps are not used to infer identity or expiry.
                    conn.execute(
                        "UPDATE instagram_story_sources SET sync_miss_count=sync_miss_count+1 WHERE story_media_id=?",
                        (story_id,),
                    )
                    counts["miss_pending"] += 1
            conn.commit()
        return counts

    @staticmethod
    def _delete_instagram_story_source_in_connection(conn, story_media_id: str) -> Dict[str, int]:
        """Delete a Story and only state owned by its exact-ID Story routes."""
        routes = conn.execute(
            "SELECT routing_id FROM instagram_source_routes WHERE source_type='story' AND media_id=?",
            (story_media_id,),
        ).fetchall()
        routing_ids = [int(row["routing_id"]) for row in routes]
        if routing_ids:
            placeholders = ",".join("?" for _ in routing_ids)
            conn.execute(
                f"DELETE FROM instagram_post_pairings WHERE routing_id IN ({placeholders})",
                routing_ids,
            )
            conn.execute(
                f"DELETE FROM instagram_route_previews WHERE routing_id IN ({placeholders})",
                routing_ids,
            )
            # Do not preserve Story deletion audit content. Other route types
            # keep their existing generic removal audit behavior.
            conn.execute(
                f"DELETE FROM instagram_route_removals WHERE source_type='story' AND routing_id IN ({placeholders})",
                routing_ids,
            )
        conn.execute(
            "DELETE FROM instagram_source_routes WHERE source_type='story' AND media_id=?",
            (story_media_id,),
        )
        deleted_sources = conn.execute(
            "DELETE FROM instagram_story_sources WHERE story_media_id=?",
            (story_media_id,),
        ).rowcount
        return {"sources": max(0, deleted_sources), "routes": len(routing_ids)}

    def cleanup_legacy_instagram_story_history(self) -> Dict[str, int]:
        """Remove legacy Story history once the normal schema is initialized.

        The Admin card displays ``published_at || last_seen``. Match the
        operator-identified card against that exact fallback expression, only
        among current rows, and leave an ambiguous match untouched. The whole
        cleanup is one transaction and is safe to repeat on every startup.
        """
        stale_timestamp = "2026-09-22T08:25:36.439647+00:00"
        legacy_where = "story_status IN ('EXPIRED', 'REMOVED') OR admin_deleted=1"
        counts = {
            "history_rows_found": 0,
            "history_rows_deleted": 0,
            "routes_deleted": 0,
            "story_audit_rows_deleted": 0,
            "stale_matches": 0,
            "stale_deleted": 0,
            "stale_mapping_deleted": 0,
            "noncurrent_remaining": 0,
        }
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            history_rows = conn.execute(
                f"SELECT story_media_id FROM instagram_story_sources WHERE {legacy_where}"
            ).fetchall()
            stale_rows = conn.execute(
                "SELECT story_media_id FROM instagram_story_sources "
                "WHERE COALESCE(story_status, 'ACTIVE')='ACTIVE' AND admin_deleted=0 "
                "AND COALESCE(NULLIF(published_at, ''), last_seen)=?",
                (stale_timestamp,),
            ).fetchall()
            counts["history_rows_found"] = len(history_rows)
            counts["stale_matches"] = len(stale_rows)

            # The generic removal audit has no Story source row to preserve.
            # Retain all Post/Reel/Ad audit records by filtering its type.
            counts["story_audit_rows_deleted"] = conn.execute(
                "DELETE FROM instagram_route_removals WHERE source_type='story'"
            ).rowcount
            for row in history_rows:
                deleted = self._delete_instagram_story_source_in_connection(
                    conn, row["story_media_id"],
                )
                counts["history_rows_deleted"] += deleted["sources"]
                counts["routes_deleted"] += deleted["routes"]
            if len(stale_rows) == 1:
                deleted = self._delete_instagram_story_source_in_connection(
                    conn, stale_rows[0]["story_media_id"],
                )
                counts["stale_deleted"] = deleted["sources"]
                counts["stale_mapping_deleted"] = deleted["routes"]
                counts["routes_deleted"] += deleted["routes"]
            counts["noncurrent_remaining"] = conn.execute(
                f"SELECT COUNT(*) FROM instagram_story_sources WHERE {legacy_where}"
            ).fetchone()[0]
            conn.commit()
        return counts

    def backfill_instagram_story_sequence_numbers(self) -> int:
        """Assign a stable admin-facing sequence number/date to Stories missing one.

        Numbering is derived only from ``published_at`` (Asia/Kolkata
        publication date) and ``story_media_id`` as a deterministic tie-
        breaker — never Meta's array order, the numeric Story ID, first/last
        seen, or any content signal. Already-assigned rows are never
        renumbered: a new row for an existing date is appended after the
        current maximum for that date. Rows without a trustworthy
        ``published_at`` are left unassigned rather than guessed. Touches
        only ``story_sequence_number``/``story_sequence_date`` — never
        mapping, routes, topic, or the Story ID.
        """
        with self.get_connection() as conn:
            unassigned = conn.execute(
                "SELECT story_media_id, published_at FROM instagram_story_sources "
                "WHERE story_sequence_number IS NULL AND published_at IS NOT NULL"
            ).fetchall()
            by_date: Dict[str, List[Tuple[datetime, str]]] = {}
            for row in unassigned:
                pub_date = _story_publication_date_kolkata(row["published_at"])
                if not pub_date:
                    continue
                parsed = _parse_trustworthy_datetime(row["published_at"])
                if parsed is None:
                    continue
                by_date.setdefault(pub_date, []).append((parsed, row["story_media_id"]))
            updated = 0
            for pub_date, items in by_date.items():
                items.sort(key=lambda pair: (pair[0], pair[1]))
                existing_max = conn.execute(
                    "SELECT COALESCE(MAX(story_sequence_number), 0) FROM instagram_story_sources "
                    "WHERE story_sequence_date = ?",
                    (pub_date,),
                ).fetchone()[0]
                next_number = int(existing_max or 0) + 1
                for _, story_media_id in items:
                    conn.execute(
                        "UPDATE instagram_story_sources SET story_sequence_number=?, story_sequence_date=? "
                        "WHERE story_media_id=?",
                        (next_number, pub_date, story_media_id),
                    )
                    next_number += 1
                    updated += 1
            conn.commit()
        return updated

    def try_acquire_instagram_story_sync_lock(
        self, owner: str, lease_seconds: float = 120.0,
    ) -> bool:
        """Atomically acquire the shared sync lease, or report it is busy."""
        now = time.time()
        lease_until = now + max(1.0, float(lease_seconds))
        lock_name = "story_inventory"
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner, locked_until FROM instagram_story_sync_locks WHERE lock_name=?",
                (lock_name,),
            ).fetchone()
            if row and float(row["locked_until"] or 0) > now and row["owner"] != owner:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO instagram_story_sync_locks(lock_name, owner, locked_until) "
                "VALUES (?, ?, ?) ON CONFLICT(lock_name) DO UPDATE SET owner=excluded.owner, locked_until=excluded.locked_until",
                (lock_name, str(owner)[:128], lease_until),
            )
            conn.commit()
        return True

    def release_instagram_story_sync_lock(self, owner: str) -> None:
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE instagram_story_sync_locks SET locked_until=0 WHERE lock_name=? AND owner=?",
                ("story_inventory", str(owner)[:128]),
            )
            conn.commit()

    def list_instagram_story_sources(self, *, page: int = 1,
                                     page_size: Optional[int] = None, search: Optional[str] = None,
                                     mapping_status_filter: Optional[str] = None,
                                     context_active: Optional[bool] = None,
                                     sort: str = "newest", with_total: bool = False):
        """Return only current operational Stories and exact-ID mappings."""
        clauses = [
            "s.admin_deleted=0",
            "UPPER(COALESCE(s.story_status,'ACTIVE'))='ACTIVE'",
        ]
        params: List[Any] = []
        if search:
            pattern = f"%{search[:200]}%"
            clauses.append("(s.story_media_id LIKE ? OR COALESCE(r.routing_topic,'') LIKE ? OR COALESCE(r.source_description,'') LIKE ?)")
            params.extend([pattern] * 3)
        if mapping_status_filter == "MAPPED":
            clauses.append("r.routing_id IS NOT NULL AND r.archived_at IS NULL AND r.active=1")
        elif mapping_status_filter == "DISABLED":
            clauses.append("r.routing_id IS NOT NULL AND r.archived_at IS NULL AND r.active=0")
        elif mapping_status_filter == "UNMAPPED":
            clauses.append("(r.routing_id IS NULL OR r.archived_at IS NOT NULL)")
        if context_active is not None:
            clauses.append("COALESCE(r.active,0)=?")
            params.append(int(context_active))
        where = " AND ".join(clauses) if clauses else "1=1"
        sort_column = "r.updated_at" if sort == "recently_edited" else "s.last_seen"
        direction = "ASC" if sort == "oldest" else "DESC"
        with self.get_connection() as conn:
            join = "LEFT JOIN instagram_source_routes r ON r.routing_id=(SELECT rr.routing_id FROM instagram_source_routes rr WHERE rr.source_type='story' AND rr.media_id=s.story_media_id ORDER BY rr.updated_at DESC, rr.routing_id DESC LIMIT 1)"
            total = int(conn.execute(f"SELECT COUNT(*) AS n FROM instagram_story_sources s {join} WHERE {where}", params).fetchone()["n"])
            sql = f"SELECT s.story_media_id, s.temporary_url, s.story_status, s.first_seen, s.last_seen, s.last_sender_id, s.last_event_metadata, s.media_type, s.permalink, s.published_at, s.source, s.sync_miss_count, s.last_confirmed_active_at, s.story_sequence_number, s.story_sequence_date, r.routing_id, r.topic_id, r.routing_topic, r.source_description, r.menu_id, r.menu_mode, r.active AS route_active, r.archived_at AS route_archived_at FROM instagram_story_sources s {join} WHERE {where} ORDER BY {sort_column} {direction}, s.story_media_id"
            if page_size is not None:
                sql += " LIMIT ? OFFSET ?"
                params = [*params, page_size, (page - 1) * page_size]
            rows = conn.execute(sql, params).fetchall()
            output = []
            for row in rows:
                item = dict(row)
                route_active = item.pop("route_active")
                route_archived_at = item.pop("route_archived_at")
                route = item.get("routing_id")
                if route and route_archived_at:
                    mapping_status = "ARCHIVED"
                elif route and not route_active:
                    mapping_status = "DISABLED"
                elif route:
                    mapping_status = "MAPPED"
                else:
                    mapping_status = "UNMAPPED"
                item["media_id"] = item["story_media_id"]
                item["mapping_status"] = mapping_status
                item["status"] = mapping_status
                item["routing_id"] = route
                item["topic_id"] = item.get("topic_id") if route else None
                item["routing_topic"] = item.get("routing_topic") if route else None
                item["source_description"] = item.get("source_description") or ""
                item["menu_id"] = item.get("menu_id") if route else None
                item["menu_mode"] = item.get("menu_mode") if route else "INHERIT"
                item["active"] = bool(route_active) if route else False
                # Admin-only ordinal convenience label — never a routing
                # identity. story_media_id above remains what any reply/
                # webhook or mapping actually matches against.
                sequence_label = format_story_sequence_label(item.get("story_sequence_number"))
                sequence_date_display = format_story_sequence_date_display(item.get("story_sequence_date"))
                item["story_sequence_label"] = sequence_label
                item["story_sequence_date_display"] = sequence_date_display
                if sequence_label and sequence_date_display:
                    item["story_sequence_display"] = f"{sequence_label} • {sequence_date_display}"
                else:
                    item["story_sequence_display"] = "Story — sequence unavailable"
                output.append(item)
        return (output, total) if with_total else output

    def get_instagram_story_source(self, story_media_id: str) -> Optional[Dict[str, Any]]:
        media_id = str(story_media_id or "").strip()
        if not media_id:
            return None
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM instagram_story_sources WHERE story_media_id=?",
                (media_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_instagram_story_source(self, story_media_id: str) -> bool:
        """Permanently delete local Story and exact Story mapping state only."""
        media_id = str(story_media_id or "").strip()
        if not media_id:
            return False
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute(
                "SELECT story_media_id FROM instagram_story_sources WHERE story_media_id = ?",
                (media_id,),
            ).fetchone()
            if not source:
                conn.rollback()
                return False
            self._delete_instagram_story_source_in_connection(conn, media_id)
            conn.commit()
        return True

    def set_instagram_source_route_active(self, routing_id: int, active: bool) -> bool:
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE instagram_source_routes SET active=?, updated_at=? WHERE routing_id=? AND archived_at IS NULL",
                (int(active), datetime.utcnow().isoformat(), routing_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def set_instagram_source_route_topic(self, routing_id: int, topic_id: str) -> bool:
        """Backfills topic_id only — deliberately narrower than
        save_instagram_source_route, which unconditionally clears archived_at
        on update. Used by the startup migration, which must never un-archive
        a route just because it is backfilling one field."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE instagram_source_routes SET topic_id=? WHERE routing_id=?",
                (topic_id, routing_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def archive_instagram_source_route(self, routing_id: int) -> bool:
        # `active` is left exactly as it was — `archived_at IS NULL` is
        # already what gates matching, so forcing active=0 here would only
        # destroy the one piece of information restore needs: whether this
        # route should resume matching immediately or come back disabled.
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE instagram_source_routes SET archived_at=?, updated_at=? WHERE routing_id=?",
                (now, now, routing_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def restore_instagram_source_route(self, routing_id: int) -> bool:
        """Un-archives a route in place — same row, same id, nothing duplicated.

        Deliberately does not touch `active`: an operator who archived a
        route they had disabled should get back a disabled route, not one
        that starts matching customer traffic again on its own.
        """
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE instagram_source_routes SET archived_at=NULL, updated_at=? "
                "WHERE routing_id=? AND archived_at IS NOT NULL",
                (now, routing_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def delete_instagram_source_route(self, routing_id: int, removed_by: Optional[str] = None) -> bool:
        """Permanently deletes one routing configuration.

        Scope is deliberately narrow: the route row itself, and the
        pairing-attempt rows that exist only to serve that route (they carry
        no meaning once the route they were pairing is gone). Nothing about
        Topics, Menus, product/knowledge catalogues, conversations, or CAM
        handoffs is touched — none of those are owned by a route, and a
        route referencing one of them is not a reason to remove it.

        Non-Story routes retain the generic removal audit. Story mappings are
        deliberately excluded from that history and are removed locally.
        """
        with self.get_connection() as conn:
            route = conn.execute(
                "SELECT * FROM instagram_source_routes WHERE routing_id = ?", (routing_id,)
            ).fetchone()
            if not route:
                return False
            if route["source_type"] == "story":
                conn.execute(
                    "DELETE FROM instagram_route_removals WHERE source_type='story' AND routing_id=?",
                    (routing_id,),
                )
                conn.execute("DELETE FROM instagram_route_previews WHERE routing_id=?", (routing_id,))
            else:
                conn.execute(
                    """
                    INSERT INTO instagram_route_removals
                        (routing_id, canonical_url, source_type, shortcode, routing_topic, removed_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (routing_id, route["canonical_url"], route["source_type"],
                     route["shortcode"], route["routing_topic"], removed_by),
                )
            conn.execute("DELETE FROM instagram_post_pairings WHERE routing_id = ?", (routing_id,))
            conn.execute("DELETE FROM instagram_source_routes WHERE routing_id = ?", (routing_id,))
            conn.commit()
        return True

    # --- Shared-post pairing: admin-verified media_id binding --------------

    def start_post_pairing(self, routing_id: int, ttl_seconds: int, started_by: Optional[str]) -> Dict[str, Any]:
        """Opens a pairing window for one route, superseding any other open one.

        Only one route may be mid-pairing at a time — otherwise the next
        shared post received during the window would be ambiguous about which
        route it was meant to bind. Starting a new pairing is how an admin
        cancels a forgotten one, so this does not refuse; it supersedes.
        """
        now = datetime.utcnow()
        # SQLite's own CURRENT_TIMESTAMP renders as "YYYY-MM-DD HH:MM:SS" —
        # matched here rather than using isoformat()'s "T"-separated,
        # microsecond-bearing string, which would sort before every
        # CURRENT_TIMESTAMP value in a plain string comparison and make
        # get_active_post_pairing()'s expiry check never actually expire
        # anything.
        expires_at = (now + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE instagram_post_pairings SET status='cancelled' WHERE status='pending'"
            )
            cursor = conn.execute(
                """
                INSERT INTO instagram_post_pairings (routing_id, status, started_at, expires_at, started_by)
                VALUES (?, 'pending', ?, ?, ?)
                """,
                (routing_id, now.isoformat(), expires_at, started_by),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM instagram_post_pairings WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row)

    def get_active_post_pairing(self) -> Optional[Dict[str, Any]]:
        """The one pairing currently waiting for a shared post, if its window has not closed."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM instagram_post_pairings WHERE status = 'pending' "
                "AND expires_at > CURRENT_TIMESTAMP ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def get_post_pairing_for_route(self, routing_id: int) -> Optional[Dict[str, Any]]:
        """Newest pairing attempt for this route, of any status — for the admin status view."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM instagram_post_pairings WHERE routing_id = ? ORDER BY id DESC LIMIT 1",
                (routing_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_latest_post_pairings(self) -> Dict[int, Dict[str, Any]]:
        """Return the newest pairing attempt per route in one database query."""
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT p.*
                FROM instagram_post_pairings p
                JOIN (
                    SELECT routing_id, MAX(id) AS id
                    FROM instagram_post_pairings
                    GROUP BY routing_id
                ) latest ON latest.id = p.id
                """
            ).fetchall()
        return {int(row["routing_id"]): dict(row) for row in rows}

    def cancel_post_pairing(self, routing_id: int) -> bool:
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE instagram_post_pairings SET status='cancelled' "
                "WHERE routing_id = ? AND status = 'pending'",
                (routing_id,),
            )
            conn.commit()
        return cursor.rowcount > 0

    def bind_post_pairing(self, pairing_id: int, media_id: str) -> Optional[Dict[str, Any]]:
        """
        Commits one shared post's media id to the route it was paired against.

        Writes both sides atomically-enough for SQLite's single-writer model:
        the pairing row records what was bound and when (for the admin to see
        it happened and to what), and the route itself gets the verified
        media_id, which is what the existing exact-match lookup already
        matches ad-id/story replies against — a paired post needs no matcher
        of its own once this is set.
        """
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            pairing = conn.execute(
                "SELECT * FROM instagram_post_pairings WHERE id = ? AND status = 'pending' "
                "AND expires_at > CURRENT_TIMESTAMP",
                (pairing_id,),
            ).fetchone()
            if not pairing:
                return None
            conn.execute(
                "UPDATE instagram_post_pairings SET status='bound', bound_media_id=?, bound_at=? WHERE id=?",
                (media_id, now, pairing_id),
            )
            conn.execute(
                "UPDATE instagram_source_routes SET media_id=?, updated_at=? WHERE routing_id=?",
                (media_id, now, pairing["routing_id"]),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM instagram_post_pairings WHERE id=?", (pairing_id,)).fetchone()
        return dict(row)

    # --- Dynamic menu catalogue ---------------------------------------------

    def list_menu_categories(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        where = "" if include_inactive else "WHERE c.active = 1"
        with self.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT c.*, COUNT(m.menu_id) AS menu_count
                FROM menu_categories c
                LEFT JOIN menu_catalogue m ON m.category = c.name COLLATE NOCASE
                {where}
                GROUP BY c.category_id
                ORDER BY c.name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_menu_category(self, category_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM menu_categories WHERE category_id = ?", (category_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_menu_category_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM menu_categories WHERE name = ? COLLATE NOCASE", (name.strip(),)
            ).fetchone()
        return dict(row) if row else None

    def create_menu_category(self, name: str) -> Dict[str, Any]:
        with self.get_connection() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO menu_categories (name, active, is_system) VALUES (?, 1, 0)", (name,)
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("A category with that name already exists.") from exc
            conn.commit()
            row = conn.execute(
                "SELECT * FROM menu_categories WHERE category_id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row)

    def rename_menu_category(self, category_id: int, name: str) -> Optional[Dict[str, Any]]:
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            current = conn.execute(
                "SELECT * FROM menu_categories WHERE category_id = ?", (category_id,)
            ).fetchone()
            if not current:
                return None
            duplicate = conn.execute(
                "SELECT category_id FROM menu_categories WHERE name = ? COLLATE NOCASE AND category_id != ?",
                (name, category_id),
            ).fetchone()
            if duplicate:
                raise ValueError("A category with that name already exists.")
            # Preserve every relationship while changing the display value.
            conn.execute(
                "UPDATE menu_catalogue SET category = ?, updated_at = ? WHERE category = ? COLLATE NOCASE",
                (name, now, current["name"]),
            )
            conn.execute(
                "UPDATE menu_categories SET name = ?, updated_at = ? WHERE category_id = ?",
                (name, now, category_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM menu_categories WHERE category_id = ?", (category_id,)
            ).fetchone()
        return dict(row)

    def menu_category_usage(self, category_id: int) -> Optional[Dict[str, Any]]:
        category = self.get_menu_category(category_id)
        if not category:
            return None
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT menu_id, name, active FROM menu_catalogue WHERE category = ? COLLATE NOCASE ORDER BY name COLLATE NOCASE",
                (category["name"],),
            ).fetchall()
        return {"category": category, "menus": [dict(row) for row in rows]}

    def delete_menu_category(self, category_id: int, replacement_category_id: Optional[int] = None) -> Dict[str, Any]:
        with self.get_connection() as conn:
            category = conn.execute(
                "SELECT * FROM menu_categories WHERE category_id = ?", (category_id,)
            ).fetchone()
            if not category:
                raise KeyError(category_id)
            if category["is_system"]:
                raise ValueError("This protected category cannot be deleted.")
            menus = conn.execute(
                "SELECT menu_id, name, active FROM menu_catalogue WHERE category = ? COLLATE NOCASE ORDER BY name COLLATE NOCASE",
                (category["name"],),
            ).fetchall()
            # Reassignment is a separate operator action. A delete must never
            # silently move menus, even if an old client supplies a replacement.
            if menus:
                return {"deleted": False, "category": dict(category), "menus": [dict(row) for row in menus]}
            conn.execute("DELETE FROM menu_categories WHERE category_id = ?", (category_id,))
            conn.commit()
        return {
            "deleted": True, "category": dict(category), "replacement": None,
            "reassigned_menus": 0,
        }

    def list_menu_catalogue(self, include_inactive: bool = True) -> List[Dict[str, Any]]:
        where = "" if include_inactive else "WHERE active = 1"
        with self.get_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM menu_catalogue {where} ORDER BY is_builtin DESC, name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_menu_catalogue_entry(self, menu_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM menu_catalogue WHERE menu_id = ?", (menu_id,)).fetchone()
        return dict(row) if row else None

    def is_menu_deleted(self, menu_id: str) -> bool:
        with self.get_connection() as conn:
            return conn.execute(
                "SELECT 1 FROM menu_deletion_tombstones WHERE menu_id = ?",
                (menu_id,),
            ).fetchone() is not None

    def list_active_menu_references(self, menu_id: str) -> List[Dict[str, Any]]:
        """Return customer-serving route/topic references that block deletion."""
        with self.get_connection() as conn:
            routes = conn.execute(
                """
                SELECT routing_id, source_type, media_id, ad_id, routing_topic
                FROM instagram_source_routes
                WHERE menu_id = ? AND active = 1 AND archived_at IS NULL
                ORDER BY routing_id
                """,
                (menu_id,),
            ).fetchall()
            topics = conn.execute(
                """
                SELECT topic_id, name
                FROM instagram_topics
                WHERE default_menu_id = ? AND active = 1
                ORDER BY topic_id
                """,
                (menu_id,),
            ).fetchall()
        return [
            {
                "type": "instagram_route",
                "routing_id": int(row["routing_id"]),
                "source_type": row["source_type"],
                "media_id": row["media_id"],
                "ad_id": row["ad_id"],
                "routing_topic": row["routing_topic"],
            }
            for row in routes
        ] + [
            {
                "type": "instagram_topic",
                "topic_id": row["topic_id"],
                "name": row["name"],
            }
            for row in topics
        ]

    def permanently_delete_menu(
        self, menu_id: str, *, related_keys: Optional[List[str]] = None,
        deleted_by: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Atomically remove one catalogue menu and all of its local state.

        Active customer-facing references are checked before any mutation. Any
        inactive/archived references are nulled in the same transaction so a
        deleted menu can never leave a dangling route or Topic default.
        """
        keys = tuple(dict.fromkeys([menu_id, *(related_keys or [])]))
        placeholders = ", ".join("?" for _ in keys)
        with self.get_connection() as conn:
            entry = conn.execute(
                "SELECT * FROM menu_catalogue WHERE menu_id = ?", (menu_id,)
            ).fetchone()
            if not entry:
                return None

            routes = conn.execute(
                """
                SELECT routing_id, source_type, media_id, ad_id, routing_topic
                FROM instagram_source_routes
                WHERE menu_id = ? AND active = 1 AND archived_at IS NULL
                ORDER BY routing_id
                """,
                (menu_id,),
            ).fetchall()
            topics = conn.execute(
                """
                SELECT topic_id, name
                FROM instagram_topics
                WHERE default_menu_id = ? AND active = 1
                ORDER BY topic_id
                """,
                (menu_id,),
            ).fetchall()
            references = [
                {
                    "type": "instagram_route",
                    "routing_id": int(row["routing_id"]),
                    "source_type": row["source_type"],
                    "media_id": row["media_id"],
                    "ad_id": row["ad_id"],
                    "routing_topic": row["routing_topic"],
                }
                for row in routes
            ] + [
                {"type": "instagram_topic", "topic_id": row["topic_id"], "name": row["name"]}
                for row in topics
            ]
            if references:
                return {"deleted": False, "entry": dict(entry), "references": references}

            image_paths = [
                row["image_path"]
                for row in conn.execute(
                    f"SELECT image_path FROM menu_image_versions WHERE menu_key IN ({placeholders})",
                    keys,
                ).fetchall()
            ]
            conn.execute(
                f"DELETE FROM menu_image_versions WHERE menu_key IN ({placeholders})", keys
            )
            conn.execute(
                f"DELETE FROM menu_drafts WHERE menu_key IN ({placeholders})", keys
            )
            conn.execute(
                f"DELETE FROM menu_versions WHERE menu_key IN ({placeholders})", keys
            )
            # Archived/disabled records are not a reason to block deletion,
            # but they must not retain a dead menu_id.
            conn.execute(
                """
                UPDATE instagram_source_routes
                SET menu_id = NULL,
                    menu_mode = CASE WHEN menu_mode = 'SELECTED' THEN 'NONE' ELSE menu_mode END,
                    updated_at = ?
                WHERE menu_id = ?
                """,
                (datetime.utcnow().isoformat(), menu_id),
            )
            conn.execute(
                "UPDATE instagram_topics SET default_menu_id = NULL, updated_at = ? WHERE default_menu_id = ?",
                (datetime.utcnow().isoformat(), menu_id),
            )
            conn.execute("DELETE FROM menu_catalogue WHERE menu_id = ?", (menu_id,))
            conn.execute(
                """
                INSERT INTO menu_deletion_tombstones (menu_id, deleted_at, deleted_by)
                VALUES (?, ?, ?)
                ON CONFLICT(menu_id) DO UPDATE SET deleted_at=excluded.deleted_at, deleted_by=excluded.deleted_by
                """,
                (menu_id, datetime.utcnow().isoformat(), deleted_by),
            )
            conn.commit()
        return {
            "deleted": True,
            "entry": dict(entry),
            "image_paths": image_paths,
            "references_cleared": True,
        }

    def find_menu_catalogue_entry_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM menu_catalogue WHERE name = ? COLLATE NOCASE", (name.strip(),)
            ).fetchone()
        return dict(row) if row else None

    def create_menu_catalogue_entry(
        self, menu_id: str, name: str, category: str, description: str,
        active: bool = True, is_builtin: bool = False, customer_topic: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO menu_catalogue (menu_id, name, category, description, active, is_builtin, customer_topic)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (menu_id, name, category, description, int(active), int(is_builtin), customer_topic),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM menu_catalogue WHERE menu_id = ?", (menu_id,)).fetchone()
        return dict(row)

    def update_menu_catalogue_entry(
        self, menu_id: str, *, name: Optional[str] = None, category: Optional[str] = None,
        description: Optional[str] = None, active: Optional[bool] = None,
        customer_topic: Optional[str] = None, update_topic: bool = False,
    ) -> Optional[Dict[str, Any]]:
        fields, values = [], []
        for column, value in (("name", name), ("category", category), ("description", description)):
            if value is not None:
                fields.append(f"{column} = ?")
                values.append(value)
        if active is not None:
            fields.append("active = ?")
            values.append(int(active))
        if update_topic:
            fields.append("legacy_customer_eligible = CASE WHEN COALESCE(customer_topic,'') != COALESCE(?, '') "
                          "THEN 0 ELSE legacy_customer_eligible END")
            values.append(customer_topic)
            fields.append("customer_topic = ?")
            values.append(customer_topic)
        if not fields:
            return self.get_menu_catalogue_entry(menu_id)
        fields.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.append(menu_id)
        with self.get_connection() as conn:
            cursor = conn.execute(f"UPDATE menu_catalogue SET {', '.join(fields)} WHERE menu_id = ?", values)
            conn.commit()
        return self.get_menu_catalogue_entry(menu_id) if cursor.rowcount else None

    def move_menu_in_topic(self, menu_id: str, position: int) -> Optional[List[Dict[str, Any]]]:
        """Persist a stable 10-step presentation rank within one explicit topic."""
        with self.get_connection() as conn:
            menu = conn.execute("SELECT customer_topic FROM menu_catalogue WHERE menu_id=?", (menu_id,)).fetchone()
            if not menu:
                return None
            if not menu["customer_topic"]:
                raise ValueError("Link this menu to a customer topic before setting reply order.")
            rows = conn.execute(
                "SELECT menu_id FROM menu_catalogue WHERE customer_topic=? AND menu_id!=? "
                "ORDER BY display_order, menu_id", (menu["customer_topic"], menu_id),
            ).fetchall()
            ids = [row["menu_id"] for row in rows]
            if position < 1 or position > len(ids) + 1:
                raise ValueError("Reply position is outside this topic.")
            ids.insert(position - 1, menu_id)
            for index, item_id in enumerate(ids, 1):
                conn.execute(
                    "UPDATE menu_catalogue SET display_order=?, updated_at=? WHERE menu_id=?",
                    (index * 10, datetime.utcnow().isoformat(), item_id),
                )
            conn.commit()
            return [dict(row) for row in conn.execute(
                "SELECT * FROM menu_catalogue WHERE customer_topic=? ORDER BY display_order, menu_id",
                (menu["customer_topic"],),
            )]

    # --- Instagram Content Routing topics -----------------------------------

    def list_instagram_topics(self, include_inactive: bool = True) -> List[Dict[str, Any]]:
        where = "" if include_inactive else "WHERE active = 1"
        with self.get_connection() as conn:
            rows = conn.execute(f"SELECT * FROM instagram_topics {where} ORDER BY name COLLATE NOCASE").fetchall()
        return [dict(row) for row in rows]

    def get_instagram_topic(self, topic_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM instagram_topics WHERE topic_id = ?", (topic_id,)).fetchone()
        return dict(row) if row else None

    def find_instagram_topic_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM instagram_topics WHERE name = ? COLLATE NOCASE", (name.strip(),)
            ).fetchone()
        return dict(row) if row else None

    def create_instagram_topic(
        self, topic_id: str, name: str, description: str, default_menu_id: Optional[str],
        priority_products: List[str], knowledge_scope: List[str], active: bool = True,
        response_order: str = "description_first",
    ) -> Dict[str, Any]:
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO instagram_topics
                    (topic_id, name, description, default_menu_id, priority_products, knowledge_scope, response_order, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (topic_id, name, description, default_menu_id,
                 json.dumps(priority_products), json.dumps(knowledge_scope), response_order, int(active)),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM instagram_topics WHERE topic_id = ?", (topic_id,)).fetchone()
        return dict(row)

    def update_instagram_topic(
        self, topic_id: str, *, name: Optional[str] = None, description: Optional[str] = None,
        default_menu_id: Optional[str] = None, priority_products: Optional[List[str]] = None,
        knowledge_scope: Optional[List[str]] = None, active: Optional[bool] = None,
        response_order: Optional[str] = None,
        clear_default_menu: bool = False,
    ) -> Optional[Dict[str, Any]]:
        fields, values = [], []
        if name is not None:
            fields.append("name = ?"); values.append(name)
        if description is not None:
            fields.append("description = ?"); values.append(description)
        if clear_default_menu:
            fields.append("default_menu_id = NULL")
        elif default_menu_id is not None:
            fields.append("default_menu_id = ?"); values.append(default_menu_id)
        if priority_products is not None:
            fields.append("priority_products = ?"); values.append(json.dumps(priority_products))
        if knowledge_scope is not None:
            fields.append("knowledge_scope = ?"); values.append(json.dumps(knowledge_scope))
        if response_order is not None:
            fields.append("response_order = ?"); values.append(response_order)
        if active is not None:
            fields.append("active = ?"); values.append(int(active))
        if not fields:
            return self.get_instagram_topic(topic_id)
        fields.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.append(topic_id)
        with self.get_connection() as conn:
            cursor = conn.execute(f"UPDATE instagram_topics SET {', '.join(fields)} WHERE topic_id = ?", values)
            if cursor.rowcount and name is not None:
                conn.execute("UPDATE instagram_source_routes SET routing_topic=?, updated_at=? WHERE topic_id=?", (name, datetime.utcnow().isoformat(), topic_id))
            conn.commit()
        return self.get_instagram_topic(topic_id) if cursor.rowcount else None

    def instagram_topic_references(self, topic_id: str) -> Dict[str, int]:
        with self.get_connection() as conn:
            return {
                "source_routes": conn.execute("SELECT COUNT(*) AS n FROM instagram_source_routes WHERE topic_id=?", (topic_id,)).fetchone()["n"],
                "media_gallery": conn.execute("SELECT COUNT(*) AS n FROM instagram_media WHERE topic_id=?", (topic_id,)).fetchone()["n"],
            }

    def delete_instagram_topic(self, topic_id: str, replacement_topic_id: Optional[str] = None) -> bool:
        with self.get_connection() as conn:
            if replacement_topic_id:
                replacement = conn.execute("SELECT name FROM instagram_topics WHERE topic_id=?", (replacement_topic_id,)).fetchone()
                if not replacement:
                    return False
                conn.execute("UPDATE instagram_source_routes SET topic_id=?, routing_topic=? WHERE topic_id=?", (replacement_topic_id, replacement["name"], topic_id))
                conn.execute("UPDATE instagram_media SET topic_id=? WHERE topic_id=?", (replacement_topic_id, topic_id))
            else:
                conn.execute("UPDATE instagram_source_routes SET topic_id=NULL, routing_topic='' WHERE topic_id=?", (topic_id,))
                conn.execute("UPDATE instagram_media SET topic_id=NULL WHERE topic_id=?", (topic_id,))
            cursor = conn.execute("DELETE FROM instagram_topics WHERE topic_id=?", (topic_id,))
            conn.commit()
            return bool(cursor.rowcount)

    @staticmethod
    def _create_indexes(cursor) -> None:
        """
        Indexes for the columns actually filtered and sorted on.

        Every table here was previously unindexed apart from the implicit
        primary keys, so each admin query and each retention purge was a full
        scan. That is invisible on a few hundred rows and gets steadily worse:
        `conversations` grows by one row per customer message and is the table
        the dashboard reads most.

        Chosen from the real query shapes in this file rather than added to
        every column — an index that is never used still has to be written on
        every insert, and inserts here sit in the customer's reply path.
        """
        statements = (
            # get_conversations() filters on channel and/or user_id, ordered by
            # id DESC. delete_user_data() and purge_expired_data() filter on
            # user_id and timestamp respectively.
            "CREATE INDEX IF NOT EXISTS idx_conversations_user_channel ON conversations(user_id, channel, id DESC);",
            "CREATE INDEX IF NOT EXISTS idx_conversations_channel_id ON conversations(channel, id DESC);",
            "CREATE INDEX IF NOT EXISTS idx_conversations_timestamp ON conversations(timestamp);",
            # /health reads this table on every poll, so it must not scan it.
            "CREATE INDEX IF NOT EXISTS idx_router_shadow_timestamp ON router_shadow(timestamp);",
            "CREATE INDEX IF NOT EXISTS idx_triage_shadow_timestamp ON triage_shadow(timestamp);",
            "CREATE INDEX IF NOT EXISTS idx_triage_shadow_category ON triage_shadow(category);",
            "CREATE INDEX IF NOT EXISTS idx_lk_aliases_lkid ON learned_knowledge_aliases(learned_knowledge_id);",
            "CREATE INDEX IF NOT EXISTS idx_lk_aliases_normalized ON learned_knowledge_aliases(normalized_alias);",
            "CREATE INDEX IF NOT EXISTS idx_lk_versions_lkid ON learned_knowledge_versions(learned_knowledge_id, version_number DESC);",
            "CREATE INDEX IF NOT EXISTS idx_learned_knowledge_status ON learned_knowledge(status);",
            # get_escalations() filters on status, ordered by id DESC.
            "CREATE INDEX IF NOT EXISTS idx_escalations_status_id ON escalations(status, id DESC);",
            "CREATE INDEX IF NOT EXISTS idx_escalations_user ON escalations(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_escalations_created ON escalations(created_at);",
            "CREATE INDEX IF NOT EXISTS idx_human_notification_due "
            "ON human_notification_events(status, next_attempt_at, id);",
            # sessions already has a UNIQUE(user_id, channel) index, which
            # serves the per-user lookup; this one serves the dashboard's
            # channel filter with its updated_at ordering and the purge.
            "CREATE INDEX IF NOT EXISTS idx_sessions_channel_updated ON sessions(channel, updated_at DESC);",
            "CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);",
            # get_pending_candidates() filters on status, ordered by created_at.
            "CREATE INDEX IF NOT EXISTS idx_candidates_status_created ON candidate_templates(status, created_at DESC);",
            # get_learned_knowledge() reads the newest N; the primary key
            # already orders by id, so only the audit log needs help here.
            "CREATE INDEX IF NOT EXISTS idx_rag_audit_timestamp ON rag_audit_logs(timestamp DESC);",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_gaps_status_updated ON knowledge_gaps(status, updated_at DESC);",
            "CREATE INDEX IF NOT EXISTS idx_active_sessions_updated ON active_conversation_sessions(updated_at);",
            "CREATE INDEX IF NOT EXISTS idx_message_route_time ON message_route_audit(timestamp DESC);",
            "CREATE INDEX IF NOT EXISTS idx_handoffs_ref ON cross_channel_handoffs(handoff_ref);",
            "CREATE INDEX IF NOT EXISTS idx_handoffs_active ON cross_channel_handoffs(source_channel, source_user_id, status);",
        )
        for statement in statements:
            cursor.execute(statement)

    def record_router_shadow(
        self,
        channel: str,
        user_message: str,
        keyword_pick: Optional[str],
        router_pick: Optional[str],
        detail: str,
        latency_ms: float,
        model: Optional[str] = None,
    ) -> None:
        """Stores one silent router pick beside the keyword matcher's pick."""
        with self.get_connection() as conn:
            conn.execute("""
            INSERT INTO router_shadow (
                channel, user_message, keyword_pick, router_pick, agreed, detail,
                latency_ms, model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                channel, user_message, keyword_pick, router_pick,
                1 if keyword_pick == router_pick else 0, detail, round(latency_ms, 1), model,
            ))
            conn.commit()

    def router_shadow_pulse(self, hours: int = 24) -> Dict[str, Any]:
        """
        Just enough to tell whether shadow mode is actually running.

        Flipping ROUTER_SHADOW_ENABLED in the host dashboard is otherwise an
        act of faith: the flag lives in an environment nothing in the repo can
        read, and the comparison table is behind admin auth. This is the cheap
        counterpart that /health can show without credentials.
        """
        with self.get_connection() as conn:
            cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
            row = conn.execute("""
                SELECT COUNT(*) AS n, MAX(timestamp) AS last_at, SUM(agreed) AS agreed
                FROM router_shadow WHERE timestamp >= ?
            """, (cutoff,)).fetchone()
            compared = row["n"] or 0
            return {
                "compared_recently": compared,
                "agreement_rate": round((row["agreed"] or 0) / compared, 3) if compared else None,
                "last_comparison_at": row["last_at"],
                "window_hours": hours,
            }

    def router_shadow_report(self, days: int = 30, limit: int = 40) -> Dict[str, Any]:
        """
        How often the router and the keyword matcher choose the same answer,
        and every case where they did not.

        The disagreements are the point. A bare agreement percentage cannot say
        which side was right, and on the cases that matter — the phrasings the
        trigger list never anticipated — the matcher is the one expected to be
        wrong. These have to be read, not just counted.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

            totals = cursor.execute("""
                SELECT COUNT(*) AS n,
                       SUM(agreed) AS agreed,
                       AVG(latency_ms) AS avg_latency
                FROM router_shadow WHERE timestamp >= ?
            """, (cutoff,)).fetchone()

            compared = totals["n"] or 0
            agreed = totals["agreed"] or 0

            by_model = [
                {
                    "model": row["model"] or "unrecorded",
                    "compared": row["n"],
                    "agreement_rate": round((row["agreed"] or 0) / row["n"], 3) if row["n"] else None,
                    "avg_latency_ms": round(row["avg_latency"], 1) if row["avg_latency"] else None,
                }
                for row in cursor.execute("""
                    SELECT model, COUNT(*) AS n, SUM(agreed) AS agreed, AVG(latency_ms) AS avg_latency
                    FROM router_shadow WHERE timestamp >= ?
                    GROUP BY model ORDER BY n DESC
                """, (cutoff,))
            ]

            by_detail = [
                {"detail": row["detail"], "count": row["n"]}
                for row in cursor.execute("""
                    SELECT detail, COUNT(*) AS n FROM router_shadow
                    WHERE timestamp >= ? GROUP BY detail ORDER BY n DESC
                """, (cutoff,))
            ]

            disagreements = [
                {
                    "message": row["user_message"],
                    "keyword_pick": row["keyword_pick"],
                    "router_pick": row["router_pick"],
                    "detail": row["detail"],
                    "channel": row["channel"],
                    "at": row["timestamp"],
                }
                for row in cursor.execute("""
                    SELECT * FROM router_shadow
                    WHERE timestamp >= ? AND agreed = 0
                    ORDER BY id DESC LIMIT ?
                """, (cutoff, limit))
            ]

            # The cases the trigger list missed entirely and the router placed.
            # This is the number that justifies the whole rollout, so it is
            # counted rather than left to be spotted by eye.
            rescued = sum(
                1 for d in disagreements
                if not d["keyword_pick"] and d["router_pick"]
            )

            return {
                "days": days,
                "compared": compared,
                "agreed": agreed,
                "agreement_rate": round(agreed / compared, 3) if compared else None,
                "avg_router_latency_ms": round(totals["avg_latency"], 1) if totals["avg_latency"] else None,
                "outcomes": by_detail,
                "by_model": by_model,
                "unmatched_but_routed_in_sample": rescued,
                "disagreements": disagreements,
            }

    def record_triage_shadow(
        self,
        channel: str,
        user_id: str,
        user_message: str,
        category: Optional[str],
        confidence: Optional[float],
        reasoning: Optional[str],
        ok: bool,
        detail: str,
        matched_knowledge_id: Optional[int],
        matched_knowledge_source: Optional[str],
        matched_knowledge_preview: Optional[str],
        grounded: bool,
        grounding_reason: Optional[str],
        would_auto_resolve: bool,
        was_matched: bool,
        template_id: Optional[str],
        latency_ms: float,
        model: Optional[str] = None,
    ) -> None:
        """Stores one silent triage classification. Never read by any reply path."""
        with self.get_connection() as conn:
            conn.execute("""
            INSERT INTO triage_shadow (
                channel, user_id, user_message, category, confidence, reasoning,
                ok, detail, matched_knowledge_id, matched_knowledge_source,
                matched_knowledge_preview, grounded, grounding_reason,
                would_auto_resolve, was_matched, template_id, latency_ms, model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                channel, user_id, user_message, category, confidence, reasoning,
                1 if ok else 0, detail, matched_knowledge_id, matched_knowledge_source,
                matched_knowledge_preview, 1 if grounded else 0, grounding_reason,
                1 if would_auto_resolve else 0, 1 if was_matched else 0, template_id,
                round(latency_ms, 1), model,
            ))
            conn.commit()

    def triage_shadow_pulse(self, hours: int = 24) -> Dict[str, Any]:
        """Just enough to tell whether shadow triage is actually running, for /health."""
        with self.get_connection() as conn:
            cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
            row = conn.execute("""
                SELECT COUNT(*) AS n, MAX(timestamp) AS last_at,
                       SUM(would_auto_resolve) AS would_auto_resolve
                FROM triage_shadow WHERE timestamp >= ?
            """, (cutoff,)).fetchone()
            classified = row["n"] or 0
            return {
                "classified_recently": classified,
                "would_auto_resolve_recently": row["would_auto_resolve"] or 0,
                "last_classification_at": row["last_at"],
                "window_hours": hours,
            }

    def triage_shadow_report(self, days: int = 7, limit: int = 40) -> Dict[str, Any]:
        """
        How the triage model would have categorized the messages the bot
        could not answer on its own, for admin review before any auto-resolve
        path exists. Recent rows are the point — an admin has to be able to
        read the model's reasoning against the real message, not just a count.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

            totals = cursor.execute("""
                SELECT COUNT(*) AS n, SUM(ok) AS ok, SUM(would_auto_resolve) AS would_auto_resolve,
                       SUM(grounded) AS grounded, AVG(latency_ms) AS avg_latency
                FROM triage_shadow WHERE timestamp >= ?
            """, (cutoff,)).fetchone()
            classified = totals["n"] or 0

            by_category = [
                {
                    "category": row["category"] or "unclassified",
                    "count": row["n"],
                    "avg_confidence": round(row["avg_confidence"], 3) if row["avg_confidence"] is not None else None,
                }
                for row in cursor.execute("""
                    SELECT category, COUNT(*) AS n, AVG(confidence) AS avg_confidence
                    FROM triage_shadow WHERE timestamp >= ? AND ok = 1
                    GROUP BY category ORDER BY n DESC
                """, (cutoff,))
            ]

            by_detail = [
                {"detail": row["detail"], "count": row["n"]}
                for row in cursor.execute("""
                    SELECT detail, COUNT(*) AS n FROM triage_shadow
                    WHERE timestamp >= ? AND ok = 0 GROUP BY detail ORDER BY n DESC
                """, (cutoff,))
            ]

            recent = [
                dict(row) for row in cursor.execute("""
                    SELECT * FROM triage_shadow WHERE timestamp >= ?
                    ORDER BY id DESC LIMIT ?
                """, (cutoff, limit))
            ]

            return {
                "days": days,
                "classified": classified,
                "ok": totals["ok"] or 0,
                "grounded": totals["grounded"] or 0,
                "would_auto_resolve": totals["would_auto_resolve"] or 0,
                "avg_latency_ms": round(totals["avg_latency"], 1) if totals["avg_latency"] else None,
                "by_category": by_category,
                "failures": by_detail,
                "recent": recent,
            }

    def entry_point_breakdown(self, channel: str = "instagram", days: int = 30) -> Dict[str, Any]:
        """
        How customers on a channel are composing their messages, and which
        questions arrive the same way over and over.

        The repeated-question list is the point: a question fifty different
        people send in identical words, all as taps, is a prompt someone is
        being shown — not fifty people wondering the same thing.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

            by_source = [
                {"source": row["source"], "messages": row["n"], "senders": row["senders"]}
                for row in cursor.execute("""
                    SELECT COALESCE(message_source, 'unrecorded') AS source,
                           COUNT(*) AS n,
                           COUNT(DISTINCT user_id) AS senders
                    FROM conversations
                    WHERE channel = ? AND timestamp >= ?
                    GROUP BY source
                    ORDER BY n DESC
                """, (channel, cutoff))
            ]

            repeated = [
                {
                    "message": row["msg"],
                    "source": row["source"],
                    "messages": row["n"],
                    "senders": row["senders"],
                }
                for row in cursor.execute("""
                    SELECT LOWER(TRIM(user_message)) AS msg,
                           COALESCE(message_source, 'unrecorded') AS source,
                           COUNT(*) AS n,
                           COUNT(DISTINCT user_id) AS senders
                    FROM conversations
                    WHERE channel = ? AND timestamp >= ? AND TRIM(user_message) <> ''
                    GROUP BY msg, source
                    HAVING COUNT(DISTINCT user_id) > 1
                    ORDER BY senders DESC, n DESC
                    LIMIT 25
                """, (channel, cutoff))
            ]

            by_ad = [
                {"ad_id": row["ad_id"], "ad_title": row["ad_title"], "messages": row["n"], "senders": row["senders"]}
                for row in cursor.execute("""
                    SELECT ad_id, ad_title, COUNT(*) AS n, COUNT(DISTINCT user_id) AS senders
                    FROM conversations
                    WHERE channel = ? AND timestamp >= ? AND ad_id IS NOT NULL
                    GROUP BY ad_id, ad_title
                    ORDER BY senders DESC
                    LIMIT 25
                """, (channel, cutoff))
            ]

            return {
                "channel": channel,
                "days": days,
                "by_source": by_source,
                "repeated_questions": repeated,
                "by_ad": by_ad,
                "ad_titles_seen": sum(1 for a in by_ad if a["ad_title"]),
            }

    def set_ad_qualified(self, user_id: str, channel: str, is_ad_qualified: bool = True, ad_id: Optional[str] = None):
        """Mark user session as ad_qualified."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            cursor.execute("""
            INSERT INTO sessions (user_id, channel, is_ad_qualified, ad_id, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, channel) DO UPDATE SET
                is_ad_qualified = excluded.is_ad_qualified,
                ad_id = COALESCE(excluded.ad_id, sessions.ad_id),
                updated_at = excluded.updated_at;
            """, (user_id, channel, 1 if is_ad_qualified else 0, ad_id, now))
            conn.commit()

    def is_ad_qualified(self, user_id: str, channel: str) -> bool:
        """Check if user session is marked as ad_qualified."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_ad_qualified FROM sessions WHERE user_id = ? AND channel = ?", (user_id, channel))
            row = cursor.fetchone()
            if row:
                return bool(row["is_ad_qualified"])
            return False

    def log_conversation(
        self,
        user_id: str,
        channel: str,
        user_message: str,
        bot_response: str,
        was_matched: bool,
        template_id: Optional[str] = None,
        message_source: Optional[str] = None,
        ad_id: Optional[str] = None,
        ad_title: Optional[str] = None,
        source_routing_id: Optional[int] = None,
        source_match_method: Optional[str] = None,
        source_type: Optional[str] = None,
        shortcode: Optional[str] = None,
        media_id: Optional[str] = None,
        routing_topic: Optional[str] = None,
        source_context_used: bool = False,
        source_context_available: bool = False,
        source_referential_intent: Optional[str] = None,
        source_media_present: bool = False,
        customer_text_accompanied_media: bool = False,
    ):
        """Log message exchange and increment analytics counters."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO conversations (
                user_id, channel, user_message, bot_response, was_matched, template_id,
                message_source, ad_id, ad_title, source_routing_id, source_match_method,
                source_type, shortcode, media_id, routing_topic, source_context_used,
                source_context_available, source_referential_intent, source_media_present,
                customer_text_accompanied_media
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id, channel, user_message, bot_response, 1 if was_matched else 0, template_id,
                message_source, ad_id, ad_title, source_routing_id, source_match_method,
                source_type, shortcode, media_id, routing_topic, int(bool(source_context_used)),
                int(bool(source_context_available)), source_referential_intent,
                int(bool(source_media_present)), int(bool(customer_text_accompanied_media)),
            ))
            conversation_id = int(cursor.lastrowid)
            
            cursor.execute("UPDATE analytics SET value = value + 1 WHERE key = 'total_messages';")
            if was_matched:
                cursor.execute("UPDATE analytics SET value = value + 1 WHERE key = 'matched_responses';")
            else:
                cursor.execute("UPDATE analytics SET value = value + 1 WHERE key = 'escalations_count';")
                
            conn.commit()
            return conversation_id

    # --- Instagram outbound delivery audit -------------------------------

    def create_instagram_outbound_audit(
        self,
        *,
        inbound_message_id: Optional[int],
        recipient_id: str,
        response_type: str,
        retry_count: int = 0,
        parent_send_id: Optional[str] = None,
        chunk_number: Optional[int] = None,
        total_chunks: Optional[int] = None,
        character_count: Optional[int] = None,
    ) -> int:
        """Create the pending row immediately before a Meta API call."""
        with self.get_connection() as conn:
            self._ensure_instagram_outbound_audit_schema(conn)
            cursor = conn.execute(
                """
                INSERT INTO instagram_outbound_audit (
                    inbound_message_id, recipient_id, response_type,
                    parent_send_id, chunk_number, total_chunks, character_count,
                    send_attempted, retry_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    inbound_message_id,
                    recipient_id,
                    response_type or "text",
                    parent_send_id,
                    chunk_number,
                    total_chunks,
                    character_count,
                    max(0, int(retry_count or 0)),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def complete_instagram_outbound_audit(
        self,
        audit_id: int,
        *,
        http_status: Optional[int] = None,
        success: bool = False,
        meta_message_id: Optional[str] = None,
        meta_error_code: Optional[Any] = None,
        meta_error_subcode: Optional[Any] = None,
        meta_error_message: Optional[str] = None,
        meta_trace_id: Optional[str] = None,
        duration_ms: Optional[float] = None,
        delivery_status: Optional[str] = None,
        exception_class: Optional[str] = None,
        exception_message: Optional[str] = None,
        overall_success: Optional[bool] = None,
        overall_failure_chunk: Optional[int] = None,
    ) -> None:
        """Fill the pending row with the exact Meta result or safe exception."""
        with self.get_connection() as conn:
            self._ensure_instagram_outbound_audit_schema(conn)
            conn.execute(
                """
                UPDATE instagram_outbound_audit
                SET http_status = ?, success = ?, meta_message_id = ?,
                    meta_error_code = ?, meta_error_subcode = ?,
                    meta_error_message = ?, meta_trace_id = ?, duration_ms = ?,
                    delivery_status = ?,
                    exception_class = ?, exception_message = ?,
                    overall_success = ?, overall_failure_chunk = ?
                WHERE id = ?
                """,
                (
                    http_status,
                    int(bool(success)),
                    str(meta_message_id) if meta_message_id is not None else None,
                    str(meta_error_code) if meta_error_code is not None else None,
                    str(meta_error_subcode) if meta_error_subcode is not None else None,
                    meta_error_message,
                    meta_trace_id,
                    duration_ms,
                    delivery_status,
                    exception_class,
                    exception_message,
                    int(bool(overall_success)) if overall_success is not None else None,
                    overall_failure_chunk,
                    audit_id,
                ),
            )
            conn.commit()

    def finalize_instagram_outbound_audit(
        self,
        parent_send_id: str,
        *,
        overall_success: bool,
        overall_failure_chunk: Optional[int] = None,
    ) -> None:
        """Mark every attempt in one logical send with its aggregate outcome."""
        with self.get_connection() as conn:
            self._ensure_instagram_outbound_audit_schema(conn)
            conn.execute(
                """
                UPDATE instagram_outbound_audit
                SET overall_success = ?, overall_failure_chunk = ?
                WHERE parent_send_id = ?
                """,
                (int(bool(overall_success)), overall_failure_chunk, parent_send_id),
            )
            conn.commit()

    def get_instagram_outbound_audit(
        self,
        *,
        limit: int = 50,
        success: Optional[bool] = None,
        inbound_message_id: Optional[int] = None,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Read recent Instagram send attempts with optional diagnostics filters."""
        clauses = ["1=1"]
        params: List[Any] = []
        if success is not None:
            clauses.append("success = ?")
            params.append(int(bool(success)))
        if inbound_message_id is not None:
            clauses.append("inbound_message_id = ?")
            params.append(int(inbound_message_id))
        if since:
            clauses.append("created_at >= ?")
            params.append(str(since))
        params.append(max(1, min(int(limit or 50), 200)))
        with self.get_connection() as conn:
            self._ensure_instagram_outbound_audit_schema(conn)
            rows = conn.execute(
                "SELECT * FROM instagram_outbound_audit "
                f"WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                if not item.get("delivery_status") and any(
                    item.get(field) is not None
                    for field in (
                        "http_status", "success", "meta_error_code", "meta_error_subcode",
                        "exception_class", "exception_message",
                    )
                ):
                    from camellia.monitoring import classify_instagram_delivery
                    item["delivery_status"] = classify_instagram_delivery(
                        http_status=item.get("http_status"),
                        meta_code=item.get("meta_error_code"),
                        meta_subcode=item.get("meta_error_subcode"),
                        exception_class=item.get("exception_class"),
                        exception_message=item.get("exception_message"),
                        success=bool(item.get("success")),
                    )
                result.append(item)
            return result

    def instagram_outbound_health(self) -> Dict[str, Any]:
        """Summarize send activity without making traffic absence unhealthy."""
        with self.get_connection() as conn:
            self._ensure_instagram_outbound_audit_schema(conn)
            last_attempted = conn.execute(
                "SELECT created_at FROM instagram_outbound_audit "
                "WHERE send_attempted = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last_success = conn.execute(
                "SELECT created_at FROM instagram_outbound_audit "
                "WHERE send_attempted = 1 AND success = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last_failure = conn.execute(
                "SELECT created_at FROM instagram_outbound_audit "
                "WHERE send_attempted = 1 AND success = 0 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            failures_24h = conn.execute(
                "SELECT COUNT(*) AS count FROM instagram_outbound_audit "
                "WHERE send_attempted = 1 AND success = 0 "
                "AND created_at >= datetime('now', '-24 hours')"
            ).fetchone()["count"]
        return {
            "last_attempted": last_attempted["created_at"] if last_attempted else None,
            "last_success": last_success["created_at"] if last_success else None,
            "last_failure": last_failure["created_at"] if last_failure else None,
            "failures_last_24h": int(failures_24h or 0),
        }

    def record_escalation(self, user_id: str, channel: str, user_message: str, reason: str, context_data: Optional[Dict[str, Any]] = None) -> int:
        """Record escalation for human review."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            context_str = json.dumps(context_data) if context_data else "{}"
            cursor.execute("""
            INSERT INTO escalations (user_id, channel, user_message, reason, context_data)
            VALUES (?, ?, ?, ?, ?)
            """, (user_id, channel, user_message, reason, context_str))
            conn.commit()
            return cursor.lastrowid

    def enqueue_human_notification(self, escalation_id: int) -> Dict[str, Any]:
        """Create one durable notification event for an escalation."""
        event_key = f"escalation:{int(escalation_id)}"
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO human_notification_events
                    (event_key, escalation_id)
                VALUES (?, ?)
                """,
                (event_key, int(escalation_id)),
            )
            row = conn.execute(
                "SELECT * FROM human_notification_events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            conn.commit()
            return dict(row)

    def claim_human_notification_events(
        self, *, limit: int = 10, event_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Lease due outbox events so concurrent workers cannot double-send."""
        limit = max(1, min(int(limit or 1), 50))
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            clauses = [
                "status IN ('PENDING', 'RETRY')",
                "next_attempt_at <= CURRENT_TIMESTAMP",
                "(locked_until IS NULL OR locked_until <= CURRENT_TIMESTAMP)",
            ]
            params: List[Any] = []
            if event_id is not None:
                clauses.append("id = ?")
                params.append(int(event_id))
            params.append(limit)
            rows = conn.execute(
                "SELECT * FROM human_notification_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY id LIMIT ?",
                tuple(params),
            ).fetchall()
            claimed = []
            for row in rows:
                conn.execute(
                    """
                    UPDATE human_notification_events
                    SET status = 'SENDING', attempts = attempts + 1,
                        locked_until = datetime('now', '+5 minutes'),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (int(row["id"]),),
                )
                updated = conn.execute(
                    "SELECT * FROM human_notification_events WHERE id = ?",
                    (int(row["id"]),),
                ).fetchone()
                claimed.append(dict(updated))
            conn.commit()
            return claimed

    def complete_human_notification(
        self,
        event_id: int,
        *,
        success: bool,
        skipped: bool = False,
        retryable: bool = False,
        error: Optional[str] = None,
        retry_after_seconds: int = 60,
    ) -> None:
        """Record an outbox result without retaining provider secrets."""
        if skipped:
            status = "SKIPPED"
            modifier = None
        elif success:
            status = "SENT"
            modifier = None
        elif retryable:
            status = "RETRY"
            modifier = f"+{max(1, min(int(retry_after_seconds), 86400))} seconds"
        else:
            status = "FAILED"
            modifier = None
        safe_error = str(error or "")[:500] or None
        with self.get_connection() as conn:
            if modifier:
                conn.execute(
                    """
                    UPDATE human_notification_events
                    SET status = ?, next_attempt_at = datetime('now', ?),
                        locked_until = NULL, last_error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (status, modifier, safe_error, int(event_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE human_notification_events
                    SET status = ?, sent_at = CASE WHEN ? = 'SENT' THEN CURRENT_TIMESTAMP ELSE sent_at END,
                        locked_until = NULL, last_error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (status, status, safe_error, int(event_id)),
                )
            conn.commit()

    def get_human_notification_events(
        self, *, limit: int = 50, status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return safe outbox audit metadata for operators."""
        with self.get_connection() as conn:
            params: List[Any] = []
            query = "SELECT * FROM human_notification_events"
            if status:
                query += " WHERE status = ?"
                params.append(status)
            query += " ORDER BY id DESC LIMIT ?"
            params.append(max(1, min(int(limit or 1), 200)))
            return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]

    def get_human_notification_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM human_notification_events WHERE id = ?",
                (int(event_id),),
            ).fetchone()
            return dict(row) if row else None

    def get_escalations(self, limit: int = 50, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve recorded escalations."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute("SELECT * FROM escalations WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
            else:
                cursor.execute("SELECT * FROM escalations ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    # --- Instagram -> WhatsApp cross-channel handoffs -----------------------

    def create_handoff(
        self,
        *,
        handoff_ref: str,
        source_channel: str,
        source_user_id: str,
        reason: Optional[str],
        summary: Optional[str],
        known_entities: Optional[Dict[str, Any]],
        unresolved_topic: Optional[str],
        escalation_id: Optional[int],
        expires_at: Optional[str],
        instagram_username: Optional[str] = None,
        query_summary: Optional[str] = None,
        contact_mode: Optional[str] = None,
        customer_username: Optional[str] = None,
        customer_external_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Records a new opaque handoff reference. Never overwrites one."""
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO cross_channel_handoffs (
                    handoff_ref, source_channel, source_user_id, reason, summary,
                    known_entities, unresolved_topic, status, escalation_id,
                    instagram_username, query_summary, contact_mode, expires_at,
                    customer_username, customer_external_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff_ref, source_channel, source_user_id, reason, summary,
                    json.dumps(known_entities or {}, ensure_ascii=False), unresolved_topic,
                    escalation_id, instagram_username, query_summary, contact_mode, expires_at,
                    customer_username, customer_external_id,
                ),
            )
            conn.commit()
        return self.get_handoff_by_ref(handoff_ref)

    def get_or_create_handoff(
        self,
        *,
        handoff_ref: str,
        source_channel: str,
        source_user_id: str,
        reason: Optional[str],
        summary: Optional[str],
        known_entities: Optional[Dict[str, Any]],
        unresolved_topic: Optional[str],
        escalation_id: Optional[int],
        expires_at: Optional[str],
        instagram_username: Optional[str] = None,
        query_summary: Optional[str] = None,
        contact_mode: Optional[str] = None,
        customer_username: Optional[str] = None,
        customer_external_id: Optional[str] = None,
    ) -> tuple[Dict[str, Any], bool]:
        """Atomically reuse or create one active handoff for a customer.

        The read and insert must share one write transaction. Otherwise two
        duplicate webhook deliveries handled concurrently can both observe
        no active handoff and mint two customer references before either
        transaction commits.
        """
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM cross_channel_handoffs
                WHERE source_channel = ? AND source_user_id = ?
                  AND status NOT IN ('RESOLVED', 'EXPIRED', 'RESUMED')
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                ORDER BY id DESC LIMIT 1
                """,
                (source_channel, source_user_id),
            ).fetchone()
            if existing:
                if customer_username or customer_external_id:
                    conn.execute(
                        "UPDATE cross_channel_handoffs SET "
                        "customer_username=COALESCE(?, customer_username), "
                        "customer_external_id=COALESCE(?, customer_external_id), "
                        "updated_at=CURRENT_TIMESTAMP WHERE handoff_ref = ?",
                        (customer_username, customer_external_id, existing["handoff_ref"]),
                    )
                    existing = conn.execute(
                        "SELECT * FROM cross_channel_handoffs WHERE handoff_ref = ?",
                        (existing["handoff_ref"],),
                    ).fetchone()
                conn.commit()
                return dict(existing), False

            conn.execute(
                """
                INSERT INTO cross_channel_handoffs (
                    handoff_ref, source_channel, source_user_id, reason, summary,
                    known_entities, unresolved_topic, status, escalation_id,
                    instagram_username, query_summary, contact_mode, expires_at,
                    customer_username, customer_external_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff_ref, source_channel, source_user_id, reason, summary,
                    json.dumps(known_entities or {}, ensure_ascii=False), unresolved_topic,
                    escalation_id, instagram_username, query_summary, contact_mode, expires_at,
                    customer_username, customer_external_id,
                ),
            )
            created = conn.execute(
                "SELECT * FROM cross_channel_handoffs WHERE handoff_ref = ?",
                (handoff_ref,),
            ).fetchone()
            conn.commit()
            return dict(created), True

    def update_handoff_identity(
        self, handoff_ref: str, *, customer_username: Optional[str], customer_external_id: Optional[str],
    ) -> None:
        """Enrich an existing CAM without changing its lifecycle or dedup key."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE cross_channel_handoffs SET "
                "customer_username=COALESCE(?, customer_username), "
                "customer_external_id=COALESCE(?, customer_external_id), "
                "updated_at=CURRENT_TIMESTAMP WHERE handoff_ref = ?",
                (customer_username, customer_external_id, handoff_ref),
            )
            conn.commit()

    def get_instagram_customer_identity(self, external_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM instagram_customer_identities WHERE customer_external_id = ?",
                (external_id,),
            ).fetchone()
            return dict(row) if row else None

    def save_instagram_customer_identity(
        self, external_id: str, *, username: Optional[str] = None,
        display_name: Optional[str] = None, profile_pic: Optional[str] = None,
        lookup_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cache Meta identity without erasing a previously resolved value."""
        resolved_at = lookup_at if (username or display_name) else None
        status = "RESOLVED" if username else "UNRESOLVED"
        with self.get_connection() as conn:
            conn.execute(
                """INSERT INTO instagram_customer_identities
                   (customer_external_id, customer_username, customer_display_name,
                    customer_profile_pic, identity_resolved_at, identity_status, last_lookup_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(customer_external_id) DO UPDATE SET
                    customer_username=COALESCE(excluded.customer_username, customer_username),
                    customer_display_name=COALESCE(excluded.customer_display_name, customer_display_name),
                    customer_profile_pic=COALESCE(excluded.customer_profile_pic, customer_profile_pic),
                    identity_resolved_at=COALESCE(excluded.identity_resolved_at, identity_resolved_at),
                    identity_status=CASE WHEN excluded.identity_status='RESOLVED' OR customer_username IS NOT NULL
                        THEN 'RESOLVED' ELSE 'UNRESOLVED' END,
                    last_lookup_at=COALESCE(excluded.last_lookup_at, last_lookup_at),
                    updated_at=CURRENT_TIMESTAMP""",
                (external_id, username, display_name, profile_pic, resolved_at, status, lookup_at),
            )
            row = conn.execute(
                "SELECT * FROM instagram_customer_identities WHERE customer_external_id = ?",
                (external_id,),
            ).fetchone()
            conn.commit()
            return dict(row)

    def get_handoff_by_ref(self, handoff_ref: str) -> Optional[Dict[str, Any]]:
        """Looks up a handoff by its customer-facing reference."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM cross_channel_handoffs WHERE handoff_ref = ?", (handoff_ref,)
            ).fetchone()
            return dict(row) if row else None

    def get_active_handoff(
        self, source_channel: str, source_user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """The one non-expired, non-resolved handoff for this customer, if any.

        This is the dedup check: a customer repeating "human please" reuses
        this row instead of minting a second reference for the same enquiry.
        """
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM cross_channel_handoffs
                WHERE source_channel = ? AND source_user_id = ?
                  AND status NOT IN ('RESOLVED', 'EXPIRED', 'RESUMED')
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                ORDER BY id DESC LIMIT 1
                """,
                (source_channel, source_user_id),
            ).fetchone()
            return dict(row) if row else None

    def update_handoff_status(self, handoff_ref: str, status: str) -> None:
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE cross_channel_handoffs SET status = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE handoff_ref = ?",
                (status, handoff_ref),
            )
            conn.commit()
        from camellia.monitoring import get_logger
        get_logger("camellia.database").info("handoff.status_updated status=%s", status)

    def resume_handoff(self, handoff_ref: str) -> Optional[Dict[str, Any]]:
        """Close the waiting gate through an explicit operator action."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM cross_channel_handoffs WHERE handoff_ref = ?",
                (handoff_ref,),
            ).fetchone()
            if not row:
                return None
            if row["status"] in {"RESUMED", "RESOLVED", "EXPIRED"}:
                return dict(row)
            conn.execute(
                "UPDATE cross_channel_handoffs SET status = 'RESUMED', "
                "updated_at = CURRENT_TIMESTAMP WHERE handoff_ref = ?",
                (handoff_ref,),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM cross_channel_handoffs WHERE handoff_ref = ?",
                (handoff_ref,),
            ).fetchone()
            from camellia.monitoring import get_logger
            get_logger("camellia.database").info("handoff.resumed")
            return dict(updated)

    def get_handoffs(self, limit: int = 50, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Staff-facing listing, newest first."""
        with self.get_connection() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM cross_channel_handoffs WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cross_channel_handoffs ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(row) for row in rows]

    def get_admin_handoff_overview(self, limit: int, include_history: bool, status: Optional[str] = None) -> Dict[str, Any]:
        """Read-only operator workload; unread notifications are not resolution.

        Match the existing active-handoff expiry boundary without changing any
        stored lifecycle. Filter before pagination and count the complete set.
        """
        actionable = "(status IN ('CREATED', 'WAITING_FOR_HUMAN') AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP))"
        with self.get_connection() as conn:
            counts = conn.execute(
                f"SELECT SUM(CASE WHEN {actionable} THEN 1 ELSE 0 END) AS actionable_count, "
                f"SUM(CASE WHEN {actionable} THEN 0 ELSE 1 END) AS historical_count "
                "FROM cross_channel_handoffs"
            ).fetchone()
            where, params = ("status = ?", [status]) if status else ("1=1" if include_history else actionable, [])
            rows = conn.execute(
                f"SELECT *, {actionable} AS actionable FROM cross_channel_handoffs WHERE {where} ORDER BY id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            unread = conn.execute(
                f"SELECT COUNT(*) FROM cross_channel_handoffs h WHERE {actionable} "
                "AND NOT EXISTS (SELECT 1 FROM admin_handoff_views s WHERE s.handoff_ref = h.handoff_ref)"
            ).fetchone()[0]
            return {"rows": [dict(row) for row in rows], "actionable_count": counts["actionable_count"] or 0,
                    "historical_count": counts["historical_count"] or 0, "unread_count": unread}

    def get_admin_handoff_seen_refs(self, handoff_refs: Optional[List[str]] = None) -> set[str]:
        """Return durable operator-seen state without exposing source IDs."""
        with self.get_connection() as conn:
            if handoff_refs:
                placeholders = ",".join("?" for _ in handoff_refs)
                rows = conn.execute(
                    f"SELECT handoff_ref FROM admin_handoff_views WHERE handoff_ref IN ({placeholders})",
                    tuple(handoff_refs),
                ).fetchall()
            else:
                rows = conn.execute("SELECT handoff_ref FROM admin_handoff_views").fetchall()
            return {str(row["handoff_ref"]) for row in rows}

    def mark_admin_handoff_seen(self, handoff_ref: str) -> bool:
        """Persist that an operator opened a handoff notification."""
        reference = str(handoff_ref or "").strip()
        if not reference:
            return False
        with self.get_connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM cross_channel_handoffs WHERE handoff_ref = ?",
                (reference,),
            ).fetchone()
            if not exists:
                return False
            conn.execute(
                "INSERT INTO admin_handoff_views (handoff_ref, seen_at) VALUES (?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(handoff_ref) DO UPDATE SET seen_at = CURRENT_TIMESTAMP",
                (reference,),
            )
            conn.commit()
            return True

    def get_conversations(self, limit: int = 50, offset: int = 0, channel: Optional[str] = None, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve paginated conversation logs."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM conversations WHERE 1=1"
            params = []
            if channel:
                query += " AND channel = ?"
                params.append(channel)
            if user_id:
                query += " AND user_id = ?"
                params.append(user_id)
            query += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_conversations_count(self, channel: Optional[str] = None, user_id: Optional[str] = None) -> int:
        """Count total logged conversations matching filters."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT COUNT(*) as count FROM conversations WHERE 1=1"
            params = []
            if channel:
                query += " AND channel = ?"
                params.append(channel)
            if user_id:
                query += " AND user_id = ?"
                params.append(user_id)
            cursor.execute(query, tuple(params))
            row = cursor.fetchone()
            return row["count"] if row else 0

    def get_sessions(self, limit: int = 50, offset: int = 0, channel: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve session records."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM sessions WHERE 1=1"
            params = []
            if channel:
                query += " AND channel = ?"
                params.append(channel)
            query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_sessions_count(self, channel: Optional[str] = None) -> int:
        """Count total sessions."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT COUNT(*) as count FROM sessions WHERE 1=1"
            params = []
            if channel:
                query += " AND channel = ?"
                params.append(channel)
            cursor.execute(query, tuple(params))
            row = cursor.fetchone()
            return row["count"] if row else 0

    def get_escalation_by_id(self, escalation_id: int) -> Optional[Dict[str, Any]]:
        """Retrieve single escalation record."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM escalations WHERE id = ?", (escalation_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_escalation_status(self, escalation_id: int, status: str = "resolved") -> bool:
        """Updates the status of an escalation."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE escalations SET status = ? WHERE id = ?", (status, escalation_id))
            conn.commit()
            return cursor.rowcount > 0

    _LEARNED_STOP_WORDS = {"is", "are", "do", "you", "the", "a", "an", "and", "in", "of", "to", "for", "your", "what", "how", "can", "have"}

    def add_learned_knowledge(
        self,
        topic: str,
        question_pattern: str,
        verified_answer: str,
        source: str = "staff_resolution",
        confidence: float = 1.0,
        priority: str = "NORMAL",
        status: str = "ACTIVE",
        created_by: Optional[str] = None,
        aliases: Optional[List[str]] = None,
    ) -> Optional[int]:
        """
        Adds a verified knowledge item to the self-healing knowledge base.

        This is the single write path shared by Teach & Resolve, candidate
        approval, manual staff/client entry (Custom Q&A) and CSV import —
        every one of them ends up as a plain learned_knowledge row here, not
        in a second knowledge table. `priority`/`status`/`created_by`/
        `aliases` are new, optional, and default to the values every
        pre-existing row already migrated to, so every caller written before
        Custom Q&A existed keeps working unchanged.
        """
        topic = (topic or "").strip()
        question_pattern = (question_pattern or "").strip().lower()
        verified_answer = (verified_answer or "").strip()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            cursor.execute("""
            INSERT INTO learned_knowledge (
                topic, question_pattern, verified_answer, source, confidence,
                priority, status, created_by, updated_by, normalized_question,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                topic, question_pattern, verified_answer, source, confidence,
                (priority or "NORMAL").upper(), (status or "ACTIVE").upper(), created_by, created_by,
                normalize_question(question_pattern), now, now,
            ))
            new_id = cursor.lastrowid
            for alias in (aliases or []):
                alias = (alias or "").strip()
                if not alias:
                    continue
                cursor.execute("""
                INSERT INTO learned_knowledge_aliases (learned_knowledge_id, alias_text, normalized_alias, created_at, created_by)
                VALUES (?, ?, ?, ?, ?)
                """, (new_id, alias, normalize_question(alias), now, created_by))
            conn.commit()
            return new_id

    def get_learned_knowledge(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieves learned knowledge entries ordered by most recently created, any status.

        This is an admin/history accessor. Customer-facing code must use
        :meth:`get_active_learned_knowledge` or
        :meth:`search_learned_knowledge`, both of which enforce the ACTIVE
        lifecycle contract.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM learned_knowledge ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_active_learned_knowledge(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return only learned knowledge eligible for customer-facing runtime use."""
        return self._active_learned_knowledge_with_aliases(limit=limit)

    def get_learned_knowledge_aliases(self, learned_knowledge_id: int) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM learned_knowledge_aliases WHERE learned_knowledge_id = ? ORDER BY id ASC",
                (learned_knowledge_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def _active_learned_knowledge_with_aliases(self, limit: int = 150) -> List[Dict[str, Any]]:
        """Only rows eligible for live matching, each carrying its own aliases."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM learned_knowledge WHERE status = 'ACTIVE' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            items = [dict(row) for row in rows]
            if not items:
                return items
            ids = [item["id"] for item in items]
            placeholders = ",".join("?" * len(ids))
            alias_rows = conn.execute(
                f"SELECT learned_knowledge_id, alias_text FROM learned_knowledge_aliases WHERE learned_knowledge_id IN ({placeholders})",
                ids,
            ).fetchall()
            aliases_by_id: Dict[int, List[str]] = {}
            for row in alias_rows:
                aliases_by_id.setdefault(row["learned_knowledge_id"], []).append(row["alias_text"])
            for item in items:
                item["aliases"] = aliases_by_id.get(item["id"], [])
            return items

    @staticmethod
    def _priority_rank(item: Dict[str, Any]) -> int:
        return 1 if str(item.get("priority") or "NORMAL").upper() == "HIGH" else 0

    def _rank_learned_knowledge(self, query_text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str], float]:
        """
        Shared ranking behind search_learned_knowledge and the Custom Q&A
        Test dry-run.

        A single-token primary question or alias ("sondesh", "kanchagolla")
        is too generic to ever match on its own — a specificity floor of two
        meaningful tokens keeps a broad Custom Q&A entry from shadowing a
        deterministic/canonical answer for the same word. Above that floor,
        every candidate is scored by how much of ITS OWN text is satisfied
        by the query (a literal substring containment is a full 1.0; a
        token-overlap match scores the covered fraction), so a specific,
        fully-covered phrase always outranks a short generic one that merely
        happens to appear as a substring too. Priority is only a tie-breaker
        between candidates already equal on (score, specificity).
        """
        q_clean = (query_text or "").lower().strip()
        q_tokens = set(re.findall(r'\w+', q_clean))
        if not q_tokens:
            return None, None, 0.0
        filtered_q = q_tokens - self._LEARNED_STOP_WORDS

        items = self._active_learned_knowledge_with_aliases(limit=150)
        # (score, specificity, priority_rank, updated_at, item, matched_via)
        candidates: List[Tuple[float, int, int, str, Dict[str, Any], str]] = []

        for item in items:
            topic = (item.get("topic") or "").lower()
            pattern = (item.get("question_pattern") or "").lower()
            aliases = item.get("aliases") or []

            texts = [("Primary Question", pattern)] + [("Alias", (a or "").lower()) for a in aliases]
            best_for_item: Optional[Tuple[float, int, str]] = None

            for via, text in texts:
                if not text:
                    continue
                text_tokens = set(re.findall(r'\w+', text))
                filtered_text = text_tokens - self._LEARNED_STOP_WORDS
                specificity = len(filtered_text)
                if specificity < 2:
                    continue

                if text in q_clean or q_clean in text:
                    score = 1.0
                else:
                    overlap = filtered_text & filtered_q
                    if len(overlap) < 2:
                        continue
                    score = len(overlap) / specificity

                if score < 0.4:
                    continue
                if best_for_item is None or (score, specificity) > (best_for_item[0], best_for_item[1]):
                    best_for_item = (score, specificity, via)

            if best_for_item is None and topic:
                topic_tokens = set(re.findall(r'\w+', topic)) - self._LEARNED_STOP_WORDS
                if len(topic_tokens) >= 2 and topic in q_clean:
                    best_for_item = (1.0, len(topic_tokens), "Other")

            if best_for_item:
                score, specificity, via = best_for_item
                candidates.append((score, specificity, self._priority_rank(item), item.get("updated_at") or "", item, via))

        if not candidates:
            return None, None, 0.0

        candidates.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
        score, _specificity, _prio, _updated, winner, via = candidates[-1]
        return winner, via, score

    def search_learned_knowledge(self, query_text: str, count_usage: bool = True) -> Optional[Dict[str, Any]]:
        """
        Searches learned knowledge base (primary questions and aliases,
        ACTIVE only) for the best match to the query. `count_usage=False` is
        for anything that must never move usage_count/last_used_at — the
        Custom Q&A Test dry-run and triage shadow mode; every real serving
        call site (responder, rag_fallback, ai_responder) keeps the default.
        """
        if not query_text:
            return None
        winner, _via, _score = self._rank_learned_knowledge(query_text)
        if winner and count_usage:
            self.record_learned_knowledge_usage(winner["id"])
        return winner

    def record_learned_knowledge_usage(self, knowledge_id: int) -> None:
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE learned_knowledge SET usage_count = COALESCE(usage_count, 0) + 1, last_used_at = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), knowledge_id),
            )
            conn.commit()

    def delete_learned_knowledge(self, knowledge_id: int) -> bool:
        """
        Permanently deletes one learned knowledge entry and only its own
        aliases/version history — never conversations, escalations,
        candidate_templates, Instagram Topics, menus, handoffs, or any other
        learned_knowledge row.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM learned_knowledge_aliases WHERE learned_knowledge_id = ?", (knowledge_id,))
            cursor.execute("DELETE FROM learned_knowledge_versions WHERE learned_knowledge_id = ?", (knowledge_id,))
            cursor.execute("DELETE FROM learned_knowledge WHERE id = ?", (knowledge_id,))
            affected = cursor.rowcount
            conn.commit()
            return affected > 0

    # -- Response Library: safe overrides for protected system templates --

    def get_response_template_override(self, template_id: str) -> Optional[Dict[str, Any]]:
        """Return the active editable copy for one protected template, if any."""
        if not template_id:
            return None
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM response_template_overrides WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_response_template_overrides(self) -> Dict[str, Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM response_template_overrides").fetchall()
            return {str(row["template_id"]): dict(row) for row in rows}

    def save_response_template_override(
        self, template_id: str, response: str, updated_by: Optional[str] = None,
        status: str = "ACTIVE", change_reason: str = "edited", previous_response: Optional[str] = None,
    ) -> bool:
        """Upsert only customer-facing copy while preserving template logic."""
        template_id = (template_id or "").strip()
        response = (response or "").strip()
        status = (status or "ACTIVE").upper()
        if not template_id or not response or status not in {"ACTIVE", "DISABLED", "ARCHIVED"}:
            return False
        with self.get_connection() as conn:
            current = conn.execute(
                "SELECT * FROM response_template_overrides WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            now = datetime.utcnow().isoformat()
            if current:
                current = dict(current)
                conn.execute(
                    "INSERT INTO response_template_override_versions "
                    "(template_id, response, status, changed_at, changed_by, change_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (template_id, current["response"], current.get("status") or "ACTIVE", now, updated_by, change_reason),
                )
                conn.execute(
                    "UPDATE response_template_overrides SET response = ?, status = ?, updated_at = ?, updated_by = ? WHERE template_id = ?",
                    (response, status, now, updated_by, template_id),
                )
            else:
                if previous_response:
                    conn.execute(
                        "INSERT INTO response_template_override_versions "
                        "(template_id, response, status, changed_at, changed_by, change_reason) VALUES (?, ?, ?, ?, ?, ?)",
                        (template_id, previous_response, "ACTIVE", now, updated_by, "initial_override_default"),
                    )
                conn.execute(
                    "INSERT INTO response_template_overrides (template_id, response, status, created_at, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?)",
                    (template_id, response, status, now, now, updated_by),
                )
            conn.commit()
            return True

    def get_response_template_override_versions(self, template_id: str) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM response_template_override_versions WHERE template_id = ? ORDER BY version_id DESC",
                (template_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def restore_response_template_override_version(self, template_id: str, version_id: int, actor: Optional[str] = None) -> bool:
        with self.get_connection() as conn:
            version = conn.execute(
                "SELECT * FROM response_template_override_versions WHERE template_id = ? AND version_id = ?",
                (template_id, version_id),
            ).fetchone()
            current = conn.execute(
                "SELECT * FROM response_template_overrides WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            if not version or not current:
                return False
            now = datetime.utcnow().isoformat()
            current = dict(current)
            conn.execute(
                "INSERT INTO response_template_override_versions "
                "(template_id, response, status, changed_at, changed_by, change_reason) VALUES (?, ?, ?, ?, ?, ?)",
                (template_id, current["response"], current.get("status") or "ACTIVE", now, actor, f"restored_from_version_{version_id}"),
            )
            conn.execute(
                "UPDATE response_template_overrides SET response = ?, status = ?, updated_at = ?, updated_by = ? WHERE template_id = ?",
                (version["response"], version["status"], now, actor, template_id),
            )
            conn.commit()
            return True

    # -- Custom Q&A: full editor on top of the same learned_knowledge store --

    def list_custom_qa(
        self,
        status: Optional[str] = None,
        source: Optional[str] = None,
        priority: Optional[str] = None,
        topic: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """The Custom Q&A tab's list, with each row's alias count attached."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            clauses, params = [], []
            if status and status.upper() != "ALL":
                clauses.append("status = ?")
                params.append(status.upper())
            if source:
                clauses.append("source = ?")
                params.append(source)
            if priority:
                clauses.append("priority = ?")
                params.append(priority.upper())
            if topic:
                clauses.append("topic = ?")
                params.append(topic)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            cursor.execute(f"SELECT * FROM learned_knowledge {where} ORDER BY id DESC LIMIT ?", (*params, limit))
            items = [dict(row) for row in cursor.fetchall()]

            if not items:
                return items
            ids = [item["id"] for item in items]
            placeholders = ",".join("?" * len(ids))
            alias_rows = cursor.execute(
                f"SELECT learned_knowledge_id, alias_text FROM learned_knowledge_aliases WHERE learned_knowledge_id IN ({placeholders})",
                ids,
            ).fetchall()
            aliases_by_id: Dict[int, List[str]] = {}
            for row in alias_rows:
                aliases_by_id.setdefault(row["learned_knowledge_id"], []).append(row["alias_text"])
            for item in items:
                item["aliases"] = aliases_by_id.get(item["id"], [])

            if search:
                needle = search.strip().lower()
                if needle:
                    def matches(it):
                        haystacks = [it.get("topic"), it.get("question_pattern"), it.get("verified_answer")] + it.get("aliases", [])
                        return any(needle in (h or "").lower() for h in haystacks)
                    items = [it for it in items if matches(it)]
            return items

    def get_custom_qa(self, knowledge_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM learned_knowledge WHERE id = ?", (knowledge_id,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["aliases"] = self.get_learned_knowledge_aliases(knowledge_id)
            return item

    def _snapshot_version(self, conn, knowledge_id: int, current: Dict[str, Any], changed_by: Optional[str], reason: str) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(version_number), 0) AS n FROM learned_knowledge_versions WHERE learned_knowledge_id = ?",
            (knowledge_id,),
        ).fetchone()
        next_version = (row["n"] or 0) + 1
        aliases = [a["alias_text"] for a in self.get_learned_knowledge_aliases(knowledge_id)]
        conn.execute("""
        INSERT INTO learned_knowledge_versions (
            learned_knowledge_id, version_number, topic, primary_question, answer,
            priority, status, aliases_snapshot, changed_at, changed_by, change_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            knowledge_id, next_version, current.get("topic"), current.get("question_pattern"),
            current.get("verified_answer"), current.get("priority"), current.get("status"),
            json.dumps(aliases), datetime.utcnow().isoformat(), changed_by, reason,
        ))

    def update_custom_qa(
        self,
        knowledge_id: int,
        topic: str,
        primary_question: str,
        answer: str,
        aliases: List[str],
        priority: str,
        status: str,
        updated_by: Optional[str],
        change_reason: str = "edited",
    ) -> bool:
        """
        Edits the SAME learned_knowledge record — never a new one. The prior
        state (including its alias set) is snapshotted to
        learned_knowledge_versions before anything is overwritten.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            current = cursor.execute("SELECT * FROM learned_knowledge WHERE id = ?", (knowledge_id,)).fetchone()
            if not current:
                return False
            self._snapshot_version(conn, knowledge_id, dict(current), updated_by, change_reason)

            now = datetime.utcnow().isoformat()
            cursor.execute("""
            UPDATE learned_knowledge
            SET topic = ?, question_pattern = ?, verified_answer = ?, priority = ?, status = ?,
                normalized_question = ?, updated_by = ?, updated_at = ?
            WHERE id = ?
            """, (
                (topic or "").strip(), (primary_question or "").strip().lower(), (answer or "").strip(),
                (priority or "NORMAL").upper(), (status or "ACTIVE").upper(),
                normalize_question(primary_question), updated_by, now, knowledge_id,
            ))
            cursor.execute("DELETE FROM learned_knowledge_aliases WHERE learned_knowledge_id = ?", (knowledge_id,))
            for alias in (aliases or []):
                alias = (alias or "").strip()
                if not alias:
                    continue
                cursor.execute("""
                INSERT INTO learned_knowledge_aliases (learned_knowledge_id, alias_text, normalized_alias, created_at, created_by)
                VALUES (?, ?, ?, ?, ?)
                """, (knowledge_id, alias, normalize_question(alias), now, updated_by))
            conn.commit()
            return True

    def set_custom_qa_status(self, knowledge_id: int, status: str, actor: Optional[str], reason: Optional[str] = None) -> bool:
        """Disable/enable/archive/reactivate — a lifecycle change, snapshotted like any other edit."""
        status = (status or "").upper()
        if status not in {"ACTIVE", "DISABLED", "ARCHIVED"}:
            return False
        with self.get_connection() as conn:
            cursor = conn.cursor()
            current = cursor.execute("SELECT * FROM learned_knowledge WHERE id = ?", (knowledge_id,)).fetchone()
            if not current:
                return False
            self._snapshot_version(conn, knowledge_id, dict(current), actor, reason or f"status_changed_to_{status.lower()}")
            cursor.execute(
                "UPDATE learned_knowledge SET status = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                (status, actor, datetime.utcnow().isoformat(), knowledge_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_learned_knowledge_versions(self, knowledge_id: int) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM learned_knowledge_versions WHERE learned_knowledge_id = ? ORDER BY version_number DESC",
                (knowledge_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    item["aliases_snapshot"] = json.loads(item.get("aliases_snapshot") or "[]")
                except Exception:
                    item["aliases_snapshot"] = []
                result.append(item)
            return result

    def restore_learned_knowledge_version(self, knowledge_id: int, version_id: int, actor: Optional[str]) -> bool:
        """
        Restores a historical version's content onto the live row, under the
        exact same id — the current state is preserved as a new version
        first, so restoring is itself just another reviewable edit.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            version = cursor.execute(
                "SELECT * FROM learned_knowledge_versions WHERE version_id = ? AND learned_knowledge_id = ?",
                (version_id, knowledge_id),
            ).fetchone()
            current = cursor.execute("SELECT * FROM learned_knowledge WHERE id = ?", (knowledge_id,)).fetchone()
            if not version or not current:
                return False
            self._snapshot_version(conn, knowledge_id, dict(current), actor, f"restored_from_version_{version['version_number']}")

            now = datetime.utcnow().isoformat()
            cursor.execute("""
            UPDATE learned_knowledge
            SET topic = ?, question_pattern = ?, verified_answer = ?, priority = ?, status = ?,
                normalized_question = ?, updated_by = ?, updated_at = ?
            WHERE id = ?
            """, (
                version["topic"], version["primary_question"], version["answer"],
                version["priority"], version["status"], normalize_question(version["primary_question"] or ""),
                actor, now, knowledge_id,
            ))
            try:
                restored_aliases = json.loads(version["aliases_snapshot"] or "[]")
            except Exception:
                restored_aliases = []
            cursor.execute("DELETE FROM learned_knowledge_aliases WHERE learned_knowledge_id = ?", (knowledge_id,))
            for alias in restored_aliases:
                alias = (alias or "").strip()
                if not alias:
                    continue
                cursor.execute("""
                INSERT INTO learned_knowledge_aliases (learned_knowledge_id, alias_text, normalized_alias, created_at, created_by)
                VALUES (?, ?, ?, ?, ?)
                """, (knowledge_id, alias, normalize_question(alias), now, actor))
            conn.commit()
            return True

    def add_learned_knowledge_alias(self, knowledge_id: int, alias_text: str, created_by: Optional[str] = None) -> Optional[int]:
        alias_text = (alias_text or "").strip()
        if not alias_text:
            return None
        with self.get_connection() as conn:
            cursor = conn.execute("""
            INSERT INTO learned_knowledge_aliases (learned_knowledge_id, alias_text, normalized_alias, created_at, created_by)
            VALUES (?, ?, ?, ?, ?)
            """, (knowledge_id, alias_text, normalize_question(alias_text), datetime.utcnow().isoformat(), created_by))
            conn.commit()
            return cursor.lastrowid

    def remove_learned_knowledge_alias(self, alias_id: int) -> bool:
        """Removes one alias only — the owning Q&A and its other aliases are untouched."""
        with self.get_connection() as conn:
            cursor = conn.execute("DELETE FROM learned_knowledge_aliases WHERE id = ?", (alias_id,))
            conn.commit()
            return cursor.rowcount > 0

    def find_learned_knowledge_duplicates(
        self, primary_question: str, aliases: Optional[List[str]] = None, exclude_id: Optional[int] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Deterministic duplicate detection for Custom Q&A create/edit: an
        exact match on normalized primary/alias text blocks the save, a
        conservative token-overlap match only warns. No LLM decides two
        pieces of business knowledge are equivalent.
        """
        candidates = [primary_question] + list(aliases or [])
        normalized_candidates = {normalize_question(c) for c in candidates if (c or "").strip()}
        if not normalized_candidates:
            return {"exact": [], "similar": []}

        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM learned_knowledge WHERE status != 'ARCHIVED'").fetchall()
            items = [dict(row) for row in rows if exclude_id is None or row["id"] != exclude_id]
            alias_rows = conn.execute("SELECT * FROM learned_knowledge_aliases").fetchall()
            aliases_by_id: Dict[int, List[str]] = {}
            for row in alias_rows:
                aliases_by_id.setdefault(row["learned_knowledge_id"], []).append(row["alias_text"])

        exact, similar = [], []
        seen_ids = set()
        for item in items:
            item_aliases = aliases_by_id.get(item["id"], [])
            item_normalized = {normalize_question(item.get("question_pattern") or "")} | {normalize_question(a) for a in item_aliases}
            item_normalized.discard("")
            if item_normalized & normalized_candidates:
                exact.append({**item, "aliases": item_aliases})
                seen_ids.add(item["id"])

        candidate_tokens = set()
        for c in normalized_candidates:
            candidate_tokens |= set(c.split())
        candidate_tokens -= self._LEARNED_STOP_WORDS

        for item in items:
            if item["id"] in seen_ids or not candidate_tokens:
                continue
            item_aliases = aliases_by_id.get(item["id"], [])
            item_text_tokens = set(normalize_question(item.get("question_pattern") or "").split())
            for a in item_aliases:
                item_text_tokens |= set(normalize_question(a).split())
            item_text_tokens -= self._LEARNED_STOP_WORDS
            if not item_text_tokens:
                continue
            overlap = candidate_tokens & item_text_tokens
            score = len(overlap) / max(len(candidate_tokens | item_text_tokens), 1)
            if score >= 0.5:
                similar.append({**item, "aliases": item_aliases})

        return {"exact": exact, "similar": similar}

    def get_analytics(self) -> Dict[str, Any]:
        """Get summary analytics."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM analytics")
            rows = cursor.fetchall()
            data = {row["key"]: row["value"] for row in rows}
            
            total = data.get("total_messages", 0)
            matched = data.get("matched_responses", 0)
            escalations = data.get("escalations_count", 0)
            match_rate = (matched / total * 100) if total > 0 else 0.0
            
            return {
                "total_messages": total,
                "matched_responses": matched,
                "escalations_count": escalations,
                "match_rate_percent": round(match_rate, 2)
            }

    def purge_expired_data(self, retention_days: int = None) -> Dict[str, int]:
        """
        Deletes conversations, escalations and sessions older than the retention
        window promised in the privacy policy. Safe to call on every startup.
        """
        days = retention_days if retention_days is not None else config.DATA_RETENTION_DAYS
        if days <= 0:
            return {"conversations": 0, "escalations": 0, "sessions": 0}

        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        # Columns hold a mix of SQLite CURRENT_TIMESTAMP ("... 10:00:00") and
        # Python isoformat ("...T10:00:00.123"), so normalise before comparing —
        # a raw string compare would treat every space-form row as older.
        def older_than(column: str) -> str:
            return f"substr(replace({column}, 'T', ' '), 1, 19) < ?"

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM instagram_outbound_jobs WHERE " + older_than("created_at"), (cutoff,)
            )
            cursor.execute(
                "DELETE FROM instagram_inbound_jobs WHERE " + older_than("created_at"), (cutoff,)
            )
            cursor.execute(f"DELETE FROM conversations WHERE {older_than('timestamp')}", (cutoff,))
            removed_conversations = cursor.rowcount
            cursor.execute(
                "DELETE FROM human_notification_events WHERE escalation_id IN "
                "(SELECT id FROM escalations WHERE " + older_than("created_at") + ")",
                (cutoff,),
            )
            cursor.execute(f"DELETE FROM escalations WHERE {older_than('created_at')}", (cutoff,))
            removed_escalations = cursor.rowcount
            cursor.execute(f"DELETE FROM sessions WHERE {older_than('updated_at')}", (cutoff,))
            removed_sessions = cursor.rowcount
            conn.commit()

        return {
            "conversations": max(removed_conversations, 0),
            "escalations": max(removed_escalations, 0),
            "sessions": max(removed_sessions, 0),
        }

    def delete_user_data(self, user_id: str) -> Dict[str, int]:
        """
        Erases everything stored about one customer, across both channels.
        Backs the deletion right stated in the privacy policy.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM instagram_outbound_jobs WHERE recipient_id = ?", (user_id,))
            cursor.execute("DELETE FROM instagram_inbound_jobs WHERE sender_id = ?", (user_id,))
            cursor.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
            removed_conversations = cursor.rowcount
            cursor.execute(
                "DELETE FROM human_notification_events WHERE escalation_id IN "
                "(SELECT id FROM escalations WHERE user_id = ?)",
                (user_id,),
            )
            cursor.execute("DELETE FROM escalations WHERE user_id = ?", (user_id,))
            removed_escalations = cursor.rowcount
            handoff_refs = [
                row["handoff_ref"] for row in cursor.execute(
                    "SELECT handoff_ref FROM cross_channel_handoffs WHERE source_user_id = ?",
                    (user_id,),
                ).fetchall()
            ]
            if handoff_refs:
                placeholders = ",".join("?" for _ in handoff_refs)
                cursor.execute(
                    f"DELETE FROM admin_handoff_views WHERE handoff_ref IN ({placeholders})",
                    tuple(handoff_refs),
                )
            cursor.execute("DELETE FROM cross_channel_handoffs WHERE source_user_id = ?", (user_id,))
            removed_handoffs = cursor.rowcount
            cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            removed_sessions = cursor.rowcount
            conn.commit()

        return {
            "conversations": max(removed_conversations, 0),
            "escalations": max(removed_escalations, 0),
            "handoffs": max(removed_handoffs, 0),
            "sessions": max(removed_sessions, 0),
        }

    def save_candidate_template(
        self,
        name: str,
        response: str,
        user_message: str = None,
        source_refs: List[str] = None,
        confidence: float = 0.0,
        created_by: str = "rag_fallback"
    ) -> int:
        """
        Saves a proposed candidate template with status 'PENDING_APPROVAL'.

        Flowers are stripped here rather than on the way out. A candidate is
        read by staff in the approval queue and, once approved, becomes copy
        the client owns — so it must never carry a flower in the first place.
        """
        response = strip_flower_emoji(response or "")
        eligible, _ = candidate_eligibility(user_message or "", response)
        valid, _ = validate_candidate_answer(response)
        if not eligible or not valid:
            return None
        normalized = normalize_question(user_message or name)
        stable_key = knowledge_key(name, user_message or name)
        sources_str = json.dumps(source_refs or [])
        with self.get_connection() as conn:
            cursor = conn.cursor()
            duplicate_rows = cursor.execute("""
                SELECT id, normalized_question FROM candidate_templates
                WHERE status = 'PENDING_APPROVAL'
                  AND (knowledge_key = ? OR normalized_question = ?)
                ORDER BY id ASC
            """, (stable_key, normalized)).fetchall()
            if not duplicate_rows:
                duplicate_rows = cursor.execute("""
                    SELECT id, normalized_question FROM candidate_templates
                    WHERE status = 'PENDING_APPROVAL' AND candidate_kind = 'knowledge'
                    ORDER BY id ASC
                """).fetchall()
            incoming_words = set(normalized.split())
            for duplicate in duplicate_rows:
                existing_words = set((duplicate["normalized_question"] or "").split())
                union = incoming_words | existing_words
                similarity = len(incoming_words & existing_words) / len(union) if union else 1.0
                if duplicate["normalized_question"] == normalized or similarity >= 0.88:
                    return int(duplicate["id"])
            cursor.execute("""
            INSERT INTO candidate_templates (
                name, status, user_message, response, original_draft, source_refs,
                confidence, created_by, created_at, candidate_kind,
                normalized_question, knowledge_key
            ) VALUES (?, 'PENDING_APPROVAL', ?, ?, ?, ?, ?, ?, ?, 'knowledge', ?, ?)
            """, (
                name, user_message, response, response, sources_str, confidence,
                created_by, datetime.utcnow().isoformat(), normalized, stable_key,
            ))
            candidate_id = cursor.lastrowid
            conn.commit()
            return candidate_id

    def record_knowledge_gap(
        self,
        user_message: str,
        intents: List[str],
        entities: Dict[str, Any],
        reason: str,
    ) -> int:
        """Deduplicates missing knowledge by intent and meaningful entities."""
        normalized_intents = sorted({str(value).strip().lower() for value in intents if str(value).strip()}) or ["unknown"]
        normalized_entities = {
            str(key).strip().lower(): str(value).strip().lower()
            for key, value in (entities or {}).items()
            if value not in (None, "", [], {})
        }
        key_parts = normalized_intents + [
            f"{key}={normalized_entities[key]}" for key in sorted(normalized_entities)
        ]
        canonical_key = "|".join(key_parts)[:300]
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO knowledge_gaps (
                    canonical_key, intents, entities, reason, example_message,
                    occurrences, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, 'OPEN', ?, ?)
                ON CONFLICT(canonical_key) DO UPDATE SET
                    occurrences = knowledge_gaps.occurrences + 1,
                    example_message = excluded.example_message,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
            """, (
                canonical_key,
                json.dumps(normalized_intents),
                json.dumps(normalized_entities, ensure_ascii=False),
                reason,
                user_message,
                now,
                now,
            ))
            row = cursor.execute(
                "SELECT id FROM knowledge_gaps WHERE canonical_key = ?", (canonical_key,)
            ).fetchone()
            conn.commit()
            return int(row["id"])

    def log_message_route(
        self,
        *,
        user_id: str,
        channel: str,
        user_message: str,
        route: Optional[str],
        intents: List[str],
        entities: Dict[str, Any],
        confidence: Optional[float],
        knowledge_ids: List[str],
        actions: List[str],
        llm_called: bool,
        fallback_used: bool,
        grounding_result: Optional[str],
        knowledge_gap: bool,
        escalation_status: Optional[str],
        route_source: Optional[str] = None,
        decision_lane: Optional[str] = None,
        state_context_used: bool = False,
        source_context_influenced: bool = False,
        handoff_reason: Optional[str] = None,
    ) -> int:
        """Records why a customer-facing route was selected.

        route_source is the production metric this migration exists to
        drive down over time: model_nlu, deterministic_semantic_fallback,
        state_handler, explicit_rule, legacy_trigger, or escalation. A
        query grouping by it is "% of traffic still on the compatibility
        floor" without re-deriving it from route/intents after the fact.
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO message_route_audit (
                    user_id, channel, user_message, route, intents, entities,
                    confidence, knowledge_ids, actions, llm_called, fallback_used,
                    grounding_result, knowledge_gap, escalation_status, route_source,
                    decision_lane, state_context_used, source_context_influenced, handoff_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(user_id), str(channel), user_message, route,
                json.dumps(intents or []), json.dumps(entities or {}, ensure_ascii=False),
                confidence, json.dumps(knowledge_ids or []), json.dumps(actions or []),
                int(bool(llm_called)), int(bool(fallback_used)), grounding_result,
                int(bool(knowledge_gap)), escalation_status, route_source,
                decision_lane, int(bool(state_context_used)),
                int(bool(source_context_influenced)), handoff_reason,
            ))
            conn.commit()
            return int(cursor.lastrowid)

    def save_active_session(
        self, user_id: str, channel: str, payload: Dict[str, Any], updated_at: float
    ) -> None:
        """Upserts bounded active conversation state for restart continuity."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO active_conversation_sessions (user_id, channel, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, channel) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
            """, (str(user_id), str(channel), json.dumps(payload, ensure_ascii=False), float(updated_at)))
            conn.commit()

    def enqueue_instagram_inbound_job(
        self, event_key: str, sender_id: str, task_name: str, payload: Dict[str, Any]
    ) -> Tuple[int, bool]:
        """Commit a webhook task before its HTTP acknowledgement."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO instagram_inbound_jobs "
                "(event_key, sender_id, task_name, payload) VALUES (?, ?, ?, ?)",
                (event_key, sender_id, task_name, json.dumps(payload, ensure_ascii=False)),
            )
            created = cursor.rowcount == 1
            row = conn.execute(
                "SELECT id FROM instagram_inbound_jobs WHERE event_key = ?", (event_key,)
            ).fetchone()
            conn.commit()
            return int(row["id"]), created

    def claim_instagram_inbound_jobs(
        self, *, limit: int = 10, job_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Lease oldest due task per sender; expired leases are recoverable."""
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT job.* FROM instagram_inbound_jobs AS job
                WHERE (? IS NULL OR job.id = ?)
                  AND ((job.status IN ('PENDING', 'RETRY') AND job.next_attempt_at <= CURRENT_TIMESTAMP)
                       OR (job.status = 'PROCESSING' AND job.locked_until <= CURRENT_TIMESTAMP))
                  AND NOT EXISTS (
                      SELECT 1 FROM instagram_inbound_jobs AS earlier
                      WHERE earlier.sender_id = job.sender_id AND earlier.id < job.id
                        AND earlier.status NOT IN ('DONE', 'FAILED')
                  )
                ORDER BY job.id LIMIT ?
                """,
                (job_id, job_id, max(1, min(int(limit), 50))),
            ).fetchall()
            claimed = []
            for row in rows:
                conn.execute(
                    "UPDATE instagram_inbound_jobs SET status = 'PROCESSING', "
                    "attempts = attempts + 1, locked_until = datetime('now', '+15 minutes'), "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (row["id"],),
                )
                claimed.append(dict(conn.execute(
                    "SELECT * FROM instagram_inbound_jobs WHERE id = ?", (row["id"],)
                ).fetchone()))
            conn.commit()
            return claimed

    def complete_instagram_inbound_job(self, job_id: int, *, success: bool, error: str = "") -> None:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT attempts FROM instagram_inbound_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                return
            attempts = int(row["attempts"])
            status = "DONE" if success else "FAILED" if attempts >= 6 else "RETRY"
            delay = min(300, 5 * (2 ** max(0, attempts - 1)))
            conn.execute(
                "UPDATE instagram_inbound_jobs SET status = ?, "
                "next_attempt_at = datetime('now', ?), locked_until = NULL, "
                "last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, f"+{delay} seconds", str(error)[:300] or None, job_id),
            )
            conn.commit()

    def enqueue_instagram_outbound_job(
        self, inbound_job_id: int, sequence_number: int, recipient_id: str,
        payload: Dict[str, Any],
    ) -> int:
        with self.get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO instagram_outbound_jobs "
                "(inbound_job_id, sequence_number, recipient_id, payload) "
                "VALUES (?, ?, ?, ?)",
                (inbound_job_id, sequence_number, recipient_id, json.dumps(payload, ensure_ascii=False)),
            )
            row = conn.execute(
                "SELECT id FROM instagram_outbound_jobs WHERE inbound_job_id = ? AND sequence_number = ?",
                (inbound_job_id, sequence_number),
            ).fetchone()
            conn.commit()
            return int(row["id"])

    def claim_instagram_outbound_jobs(
        self, *, limit: int = 10, inbound_job_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Lease due sends, keeping each recipient's reply sequence ordered."""
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT job.* FROM instagram_outbound_jobs AS job
                WHERE (? IS NULL OR job.inbound_job_id = ?)
                  AND ((job.status IN ('PENDING', 'RETRY') AND job.next_attempt_at <= CURRENT_TIMESTAMP)
                       OR (job.status = 'SENDING' AND job.locked_until <= CURRENT_TIMESTAMP))
                  AND NOT EXISTS (
                      SELECT 1 FROM instagram_outbound_jobs AS earlier
                      WHERE earlier.recipient_id = job.recipient_id AND earlier.id < job.id
                        AND (earlier.status NOT IN ('SENT', 'FAILED')
                             OR (earlier.inbound_job_id = job.inbound_job_id AND earlier.status = 'FAILED'))
                  )
                ORDER BY job.id LIMIT ?
                """,
                (inbound_job_id, inbound_job_id, max(1, min(int(limit), 50))),
            ).fetchall()
            claimed = []
            for row in rows:
                conn.execute(
                    "UPDATE instagram_outbound_jobs SET status = 'SENDING', "
                    "attempts = attempts + 1, locked_until = datetime('now', '+15 minutes'), "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (row["id"],),
                )
                claimed.append(dict(conn.execute(
                    "SELECT * FROM instagram_outbound_jobs WHERE id = ?", (row["id"],)
                ).fetchone()))
            conn.commit()
            return claimed

    def complete_instagram_outbound_job(
        self, job_id: int, *, success: bool, error: str = "", retryable: bool = True
    ) -> None:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT attempts FROM instagram_outbound_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                return
            attempts = int(row["attempts"])
            status = "SENT" if success else "RETRY" if retryable else "FAILED"
            delay = min(1800, 5 * (2 ** min(max(0, attempts - 1), 9)))
            conn.execute(
                "UPDATE instagram_outbound_jobs SET status = ?, "
                "next_attempt_at = datetime('now', ?), locked_until = NULL, "
                "last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, f"+{delay} seconds", str(error)[:300] or None, job_id),
            )
            conn.commit()

    def instagram_job_counts(self) -> Dict[str, Dict[str, int]]:
        with self.get_connection() as conn:
            return {
                name: {row["status"]: int(row["count"]) for row in conn.execute(
                    f"SELECT status, COUNT(*) AS count FROM {table} GROUP BY status"
                )}
                for name, table in (
                    ("inbound", "instagram_inbound_jobs"),
                    ("outbound", "instagram_outbound_jobs"),
                )
            }

    def load_active_session(self, user_id: str, channel: str) -> Optional[Dict[str, Any]]:
        """Loads active state; the session layer expires transient fields."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT payload, updated_at FROM active_conversation_sessions WHERE user_id = ? AND channel = ?",
                (str(user_id), str(channel)),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def delete_active_session(self, user_id: str, channel: str) -> None:
        with self.get_connection() as conn:
            conn.execute(
                "DELETE FROM active_conversation_sessions WHERE user_id = ? AND channel = ?",
                (str(user_id), str(channel)),
            )
            conn.commit()

    def purge_active_sessions(self, older_than: float) -> int:
        with self.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM active_conversation_sessions WHERE updated_at < ?", (float(older_than),)
            )
            conn.commit()
        return cursor.rowcount

    def get_knowledge_gaps(self, status: str = "OPEN", limit: int = 100) -> List[Dict[str, Any]]:
        """Returns canonical information gaps for human review."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge_gaps WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["intents"] = json.loads(item.get("intents") or "[]")
            item["entities"] = json.loads(item.get("entities") or "{}")
            result.append(item)
        return result

    def update_knowledge_gap_status(self, gap_id: int, status: str) -> bool:
        """Marks a canonical gap reviewed without publishing any answer."""
        allowed = {"OPEN", "RESOLVED", "DISMISSED"}
        normalized = str(status).strip().upper()
        if normalized not in allowed:
            return False
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE knowledge_gaps SET status = ?, updated_at = ? WHERE id = ?",
                (normalized, datetime.utcnow().isoformat(), gap_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def _hydrate_candidate_row(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Shared shaping for a candidate_templates row before it reaches the
        dashboard: strip flowers left over from before the save-time strip
        existed, fall back original_draft to the current response for a row
        that was never edited, decode source_refs, and normalize `edited`
        to a real bool for the JS layer.
        """
        item["response"] = strip_flower_emoji(item.get("response") or "")
        item["original_draft"] = item.get("original_draft") or item["response"]
        item["edited"] = bool(item.get("edited"))
        try:
            item["source_refs"] = json.loads(item.get("source_refs") or "[]")
        except Exception:
            item["source_refs"] = []
        return item

    def get_pending_candidates(self) -> List[Dict[str, Any]]:
        """Retrieves all candidate templates awaiting human approval."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM candidate_templates WHERE status = 'PENDING_APPROVAL' ORDER BY created_at DESC")
            rows = cursor.fetchall()
            result = []
            for row in rows:
                item = self._hydrate_candidate_row(dict(row))
                eligible, _ = candidate_eligibility(item.get("user_message") or "", item["response"])
                valid, _ = validate_candidate_answer(item["response"])
                if not eligible or not valid:
                    continue
                result.append(item)
            return result

    def get_candidates_by_status(self, status: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Historical candidate rows for review — an approved or rejected
        decision already made. Unlike get_pending_candidates, this does not
        re-run the eligibility/validity gate: that gate decides what belongs
        in the live approval queue, not whether a past decision is worth
        showing in the dashboard's AI Resolution & Answers view.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM candidate_templates WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            )
            return [self._hydrate_candidate_row(dict(row)) for row in cursor.fetchall()]

    def get_candidate_template(self, candidate_id: int) -> Optional[Dict[str, Any]]:
        """Returns one candidate template for the approval workflow."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM candidate_templates WHERE id = ?", (candidate_id,))
            row = cursor.fetchone()
            if not row:
                return None
            item = dict(row)
            item["original_draft"] = item.get("original_draft") or item.get("response") or ""
            item["edited"] = bool(item.get("edited"))
            try:
                item["source_refs"] = json.loads(item.get("source_refs") or "[]")
            except Exception:
                item["source_refs"] = []
            return item

    def edit_candidate_template(
        self, candidate_id: int, response: str, edited_by: str = "admin"
    ) -> bool:
        """Updates the current draft without changing its pending status."""
        answer = strip_flower_emoji(response or "").strip()
        valid, _ = validate_candidate_answer(answer)
        if not valid:
            return False
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            cursor = conn.execute("""
                UPDATE candidate_templates
                SET original_draft = COALESCE(original_draft, response),
                    response = ?, edited = 1, edited_by = ?, edited_at = ?
                WHERE id = ? AND status = 'PENDING_APPROVAL'
            """, (answer, edited_by, now, candidate_id))
            conn.commit()
        return cursor.rowcount > 0

    def approve_candidate_template(self, candidate_id: int, approved_by: str = "admin") -> bool:
        """Marks a candidate template as APPROVED and returns candidate details."""
        now = datetime.utcnow().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE candidate_templates
            SET status = 'APPROVED', approved_at = ?, approved_by = ?
            WHERE id = ? AND status = 'PENDING_APPROVAL'
            """, (now, approved_by, candidate_id))
            affected = cursor.rowcount
            conn.commit()
            return affected > 0

    def reject_candidate_template(self, candidate_id: int) -> bool:
        """Marks a candidate template as REJECTED."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE candidate_templates SET status = 'REJECTED' WHERE id = ?", (candidate_id,))
            affected = cursor.rowcount
            conn.commit()
            return affected > 0

    # --- Human-gated assisted resolution ---------------------------------
    def create_assisted_suggestion(self, **fields: Any) -> int:
        with self.get_connection() as conn:
            existing = conn.execute("SELECT id FROM assisted_resolution_suggestions WHERE user_id=? AND channel=? AND customer_message=? AND status='PENDING_REVIEW' ORDER BY id DESC LIMIT 1", (str(fields.get("user_id")), str(fields.get("channel")), fields.get("customer_message", ""))).fetchone()
            if existing:
                return int(existing["id"])
            columns = ["escalation_id", "conversation_id", "user_id", "channel", "customer_message", "triage_category", "detected_intent", "confidence", "matched_knowledge_id", "matched_knowledge_source", "grounding_status", "grounding_details", "suggested_answer", "original_suggested_answer", "status", "risk_flags", "model", "source_routing_id", "shadow_category", "shadow_confidence", "protected_order_flow", "source_route_present", "cam_interference"]
            values = [fields.get(c) for c in columns]
            values[5] = fields.get("triage_category") or "UNSUPPORTED"; values[7] = float(fields.get("confidence") or 0.0); values[10] = fields.get("grounding_status") or "Not Grounded"; values[14] = fields.get("status") or "PENDING_REVIEW"; values[15] = json.dumps(fields.get("risk_flags") or [], ensure_ascii=False); values[20] = int(bool(fields.get("protected_order_flow"))); values[21] = int(bool(fields.get("source_route_present"))); values[22] = int(bool(fields.get("cam_interference")))
            cur = conn.execute(f"INSERT INTO assisted_resolution_suggestions ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", values); conn.commit(); return int(cur.lastrowid)

    def latest_triage_shadow_for(self, user_id: str, channel: str, user_message: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT category, confidence, reasoning FROM triage_shadow WHERE user_id=? AND channel=? AND user_message=? ORDER BY id DESC LIMIT 1", (str(user_id), str(channel), user_message)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _decode_assisted(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        try: item["risk_flags"] = json.loads(item.get("risk_flags") or "[]")
        except (TypeError, json.JSONDecodeError): item["risk_flags"] = []
        return item

    def get_assisted_suggestion(self, suggestion_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn: row = conn.execute("SELECT * FROM assisted_resolution_suggestions WHERE id=?", (suggestion_id,)).fetchone()
        return self._decode_assisted(row) if row else None

    def list_assisted_suggestions(self, status: Optional[str] = None, category: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        query = "SELECT * FROM assisted_resolution_suggestions WHERE 1=1"; params: List[Any] = []
        if status: query += " AND status=?"; params.append(status)
        if category: query += " AND triage_category=?"; params.append(category)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"; params.append(max(1, min(int(limit), 200)))
        with self.get_connection() as conn: rows = conn.execute(query, tuple(params)).fetchall()
        return [self._decode_assisted(row) for row in rows]

    def review_assisted_suggestion(self, suggestion_id: int, *, status: str, reviewed_by: str, final_answer: Optional[str] = None, resolution_action: Optional[str] = None, rejection_reason: Optional[str] = None) -> bool:
        original = self.get_assisted_suggestion(suggestion_id) or {}
        edit_score = None
        edit_severity = None
        if status == "EDITED_AND_APPROVED":
            before = str(original.get("original_suggested_answer") or "")
            after = str(final_answer or "")
            similarity = SequenceMatcher(None, before, after) if before or after else 1.0
            edit_score = round(1.0 - (similarity.ratio() if hasattr(similarity, "ratio") else similarity), 4)
            edit_severity = "Minor Edit" if edit_score <= 0.10 else ("Moderate Edit" if edit_score <= 0.30 else "Major Edit")
        with self.get_connection() as conn:
            cur = conn.execute("UPDATE assisted_resolution_suggestions SET status=?, reviewed_at=?, reviewed_by=?, final_answer=?, resolution_action=?, rejection_reason=?, edit_score=COALESCE(?, edit_score), edit_severity=COALESCE(?, edit_severity) WHERE id=? AND status='PENDING_REVIEW'", (status, datetime.utcnow().isoformat(), reviewed_by, final_answer, resolution_action, rejection_reason, edit_score, edit_severity, suggestion_id))
            if cur.rowcount:
                # Keep the simulation-vs-human comparison joined by the same
                # durable suggestion id; a pending row can only be reviewed once.
                conn.execute("UPDATE auto_resolve_decisions SET final_human_outcome=? WHERE assisted_suggestion_id=?", (status, suggestion_id))
            conn.commit(); return cur.rowcount > 0

    def record_assisted_edit_metrics(self, suggestion_id: int, original: str, final: str) -> Dict[str, Any]:
        similarity = SequenceMatcher(None, original or "", final or "").ratio()
        score = round(1.0 - similarity, 4)
        severity = "Minor Edit" if score <= 0.10 else ("Moderate Edit" if score <= 0.30 else "Major Edit")
        with self.get_connection() as conn:
            conn.execute("UPDATE assisted_resolution_suggestions SET edit_score=?, edit_severity=? WHERE id=?", (score, severity, suggestion_id))
            conn.commit()
        return {"edit_score": score, "edit_severity": severity}

    def assisted_metrics(self) -> Dict[str, int]:
        with self.get_connection() as conn: rows = conn.execute("SELECT status, COUNT(*) AS count FROM assisted_resolution_suggestions GROUP BY status").fetchall()
        c = {str(r["status"]): int(r["count"]) for r in rows}
        return {"suggestions_reviewed": sum(c.get(k, 0) for k in ("APPROVED", "EDITED_AND_APPROVED", "REJECTED", "SENT_TO_HUMAN", "RESOLVED")), "approved_as_is": c.get("APPROVED", 0), "edited_before_send": c.get("EDITED_AND_APPROVED", 0), "rejected": c.get("REJECTED", 0), "sent_to_human": c.get("SENT_TO_HUMAN", 0), "teach_required": c.get("TEACH_REQUIRED", 0)}

    def assisted_evaluation(self) -> Dict[str, Any]:
        """Read-only quality and safety rollups for the review dashboard."""
        with self.get_connection() as conn:
            rows = [self._decode_assisted(row) for row in conn.execute("SELECT * FROM assisted_resolution_suggestions ORDER BY created_at DESC, id DESC").fetchall()]
            knowledge_rows = {str(row["id"]): dict(row) for row in conn.execute("SELECT id, topic, source, usage_count, last_used_at, updated_at, status FROM learned_knowledge").fetchall()}
        reviewed_statuses = {"APPROVED", "EDITED_AND_APPROVED", "REJECTED", "SENT_TO_HUMAN", "RESOLVED"}
        reviewed = [row for row in rows if row.get("status") in reviewed_statuses]
        count = lambda values, predicate: sum(1 for value in values if predicate(value))
        status_counts = {status: count(rows, lambda row, status=status: row.get("status") == status) for status in ("PENDING_REVIEW", "APPROVED", "EDITED_AND_APPROVED", "REJECTED", "SENT_TO_HUMAN", "TEACH_REQUIRED", "RESOLVED")}
        def rate(n: int) -> float:
            return round(n / len(reviewed), 4) if reviewed else 0.0
        performance = {
            "suggestions_generated": len(rows), "reviewed": len(reviewed),
            "approved_as_is": status_counts["APPROVED"], "edited_and_approved": status_counts["EDITED_AND_APPROVED"],
            "rejected": status_counts["REJECTED"], "sent_to_human": status_counts["SENT_TO_HUMAN"],
            "teach_and_resolve": count(rows, lambda row: row.get("resolution_action") == "TEACH_AND_RESOLVE"),
            "non_actionable_dismissed": count(rows, lambda row: row.get("resolution_action") == "DISMISSED"),
            "approval_rate": rate(status_counts["APPROVED"]), "edit_rate": rate(status_counts["EDITED_AND_APPROVED"]),
            "rejection_rate": rate(status_counts["REJECTED"]), "human_escalation_rate": rate(status_counts["SENT_TO_HUMAN"]),
        }
        buckets = (("0-49%", 0.0, 0.50), ("50-69%", 0.50, 0.70), ("70-84%", 0.70, 0.85), ("85-94%", 0.85, 0.95), ("95-100%", 0.95, 1.01))
        def outcome_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
            return {"total": len(items), "reviewed": count(items, lambda row: row.get("status") in reviewed_statuses), "approved_as_is": count(items, lambda row: row.get("status") == "APPROVED"), "edited": count(items, lambda row: row.get("status") == "EDITED_AND_APPROVED"), "rejected": count(items, lambda row: row.get("status") == "REJECTED"), "human_handoff": count(items, lambda row: row.get("status") == "SENT_TO_HUMAN")}
        confidence_buckets = {label: outcome_counts([row for row in rows if low <= float(row.get("confidence") or 0.0) < high]) for label, low, high in buckets}
        categories = ("AUTO_RESOLVE", "AI_SUGGESTED", "TEACH_REQUIRED", "HUMAN_REQUIRED", "NON_ACTIONABLE", "CONTACT_DETAIL", "UNSUPPORTED")
        category_breakdown = {category: outcome_counts([row for row in rows if row.get("triage_category") == category]) for category in categories}
        grounding_breakdown = {value: outcome_counts([row for row in rows if (row.get("grounding_status") or "Not Grounded") == value]) for value in ("Grounded", "Not Grounded")}
        risk_names = {"price": "Price", "discount": "Discount", "delivery_commitment": "Delivery", "availability": "Availability", "payment": "Payment", "event_date": "Event Date", "hiring": "Hiring", "allergen": "Ingredient / Allergen", "custom_order": "Custom Order", "order_change": "Order Change"}
        risk_breakdown = {label: outcome_counts([row for row in rows if key in (row.get("risk_flags") or [])]) for key, label in risk_names.items()}
        def severity(row: Dict[str, Any]) -> str:
            if row.get("edit_severity"): return str(row["edit_severity"])
            before, after = str(row.get("original_suggested_answer") or ""), str(row.get("final_answer") or "")
            if not before or not after: return "Major Edit"
            score = 1.0 - SequenceMatcher(None, before, after).ratio()
            return "Minor Edit" if score <= 0.10 else ("Moderate Edit" if score <= 0.30 else "Major Edit")
        high_confidence = [row for row in rows if float(row.get("confidence") or 0.0) >= 0.85 and (row.get("status") == "REJECTED" or row.get("status") == "SENT_TO_HUMAN" or (row.get("status") == "EDITED_AND_APPROVED" and severity(row) != "Minor Edit"))]
        shadow_comparison = [{"id": row.get("id"), "shadow_prediction": row.get("shadow_category"), "assisted_prediction": row.get("triage_category"), "human_final_decision": row.get("status"), "confidence": row.get("confidence")} for row in rows if row.get("shadow_category")]
        knowledge_signals = []
        for key, group in __import__("itertools").groupby(sorted([row for row in rows if str(row.get("matched_knowledge_id") or "").startswith("learned_knowledge#")], key=lambda row: str(row.get("matched_knowledge_id"))), key=lambda row: str(row.get("matched_knowledge_id"))):
            items = list(group); numeric_id = key.rsplit("#", 1)[-1]; knowledge = knowledge_rows.get(numeric_id, {})
            signals = outcome_counts(items)
            repeated_rejections = signals["rejected"] >= 2
            major_edits = count(items, lambda row: row.get("edit_severity") == "Major Edit")
            stale = not knowledge.get("last_used_at") and bool(knowledge.get("updated_at"))
            knowledge_signals.append({"knowledge_id": key, "topic": knowledge.get("topic"), "source": knowledge.get("source") or items[0].get("matched_knowledge_source"), "usage": knowledge.get("usage_count", 0), "last_used": knowledge.get("last_used_at"), "last_updated": knowledge.get("updated_at"), "assisted_approvals": signals["approved_as_is"], "edits": signals["edited"], "major_edits": major_edits, "rejections": signals["rejected"], "simulation_eligible": any(row.get("would_auto_resolve") for row in rows if row.get("matched_knowledge_id") == key), "health": "Not Eligible for Auto Resolve" if repeated_rejections or major_edits >= 2 or knowledge.get("status") != "ACTIVE" else ("Review Recommended" if stale else "Healthy"), **signals, "knowledge_review_recommended": repeated_rejections})
        safety = {"source_routing_overrides": count(rows, lambda row: bool(row.get("source_route_present"))), "whatsapp_assisted_activity": count(rows, lambda row: str(row.get("channel")).lower() == "whatsapp"), "protected_order_flow": count(rows, lambda row: bool(row.get("protected_order_flow"))), "cam_interference": count(rows, lambda row: bool(row.get("cam_interference")))}
        safety["order_flow_interference"] = safety["protected_order_flow"] + safety["cam_interference"]
        unsafe = count(rows, lambda row: bool(row.get("risk_flags")) and row.get("status") in {"REJECTED", "SENT_TO_HUMAN"})
        commitment_errors = count(rows, lambda row: any(flag in (row.get("risk_flags") or []) for flag in ("price", "discount", "delivery_commitment")) and row.get("status") in {"REJECTED", "SENT_TO_HUMAN", "EDITED_AND_APPROVED"})
        grounded_reviewed = len([row for row in reviewed if row.get("grounding_status") == "Grounded" and row.get("triage_category") in {"AUTO_RESOLVE", "AI_SUGGESTED"}])
        readiness = {"reviewed_grounded_suggestions": {"value": grounded_reviewed, "target": 100}, "unsafe_factual_suggestions": unsafe, "high_confidence_rejected": len(high_confidence), "price_discount_delivery_errors": commitment_errors, "whatsapp_ai_activity": safety["whatsapp_assisted_activity"], "source_routing_overrides": safety["source_routing_overrides"], "order_flow_interference": safety["protected_order_flow"] + safety["cam_interference"], "target_gate": {"grounded_reviewed": ">= 100", "unsafe_factual": 0, "price_discount_delivery": 0, "whatsapp_activity": 0, "source_routing_overrides": 0, "order_flow_interference": 0, "high_confidence_false_decisions": "near 0"}}
        auto_eval = self.auto_resolve_evaluation()
        safety.update({
            "auto_resolve_source_routing_overrides": auto_eval["source_routing_overrides"],
            "auto_resolve_whatsapp_activity": auto_eval["whatsapp_ai_activity"],
            "auto_resolve_order_flow_interference": auto_eval["order_cam_interference"],
        })
        readiness.update({
            "simulated_eligible_cases": auto_eval["simulated_eligible_cases"],
            "simulation_false_positives": auto_eval["simulation_false_positives"],
            "human_handoffs": performance["sent_to_human"],
            "major_edits": count(rows, lambda row: row.get("edit_severity") == "Major Edit"),
            "risk_violations": auto_eval["risk_violations"],
            "readiness_status": auto_eval["readiness_status"],
        })
        return {"performance": performance, "confidence_buckets": confidence_buckets, "category_breakdown": category_breakdown, "grounding_breakdown": grounding_breakdown, "risk_breakdown": risk_breakdown, "high_confidence_problems": [{key: row.get(key) for key in ("id", "customer_message", "confidence", "triage_category", "grounding_status", "matched_knowledge_source", "suggested_answer", "final_answer", "status", "rejection_reason", "edit_severity")} for row in high_confidence], "shadow_vs_assisted": shadow_comparison, "knowledge_signals": knowledge_signals, "safety": safety, "readiness": readiness, "auto_resolve": auto_eval}

    @staticmethod
    def _decode_auto_resolve(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("risk_flags", "eligibility_reasons"):
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except (TypeError, json.JSONDecodeError):
                item[key] = []
        for key in ("simulated", "would_auto_resolve", "actually_sent"):
            item[key] = bool(item.get(key))
        return item

    def record_auto_resolve_decision(self, **fields: Any) -> Dict[str, Any]:
        """Insert one decision, returning the existing row on webhook replay."""
        key = str(fields.get("idempotency_key") or "")[:300]
        columns = [
            "assisted_suggestion_id", "conversation_id", "user_id", "channel",
            "customer_message", "matched_knowledge_id", "category", "confidence",
            "grounding_status", "risk_flags", "eligibility", "eligibility_reasons",
            "simulated", "would_auto_resolve", "actually_sent", "response_text", "idempotency_key",
        ]
        values = [fields.get(c) for c in columns]
        values[9] = json.dumps(fields.get("risk_flags") or [], ensure_ascii=False)
        values[11] = json.dumps(fields.get("eligibility_reasons") or [], ensure_ascii=False)
        values[12:15] = [int(bool(fields.get("simulated", True))), int(bool(fields.get("would_auto_resolve"))), int(bool(fields.get("actually_sent")))]
        with self.get_connection() as conn:
            existing = conn.execute("SELECT * FROM auto_resolve_decisions WHERE idempotency_key=?", (key,)).fetchone() if key else None
            if existing:
                return self._decode_auto_resolve(existing)
            try:
                cur = conn.execute(f"INSERT INTO auto_resolve_decisions ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", values)
            except sqlite3.IntegrityError:
                existing = conn.execute("SELECT * FROM auto_resolve_decisions WHERE idempotency_key=?", (key,)).fetchone()
                if existing:
                    return self._decode_auto_resolve(existing)
                raise
            conn.commit()
            return self._decode_auto_resolve(conn.execute("SELECT * FROM auto_resolve_decisions WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_auto_resolve_decisions(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM auto_resolve_decisions ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),)).fetchall()
        return [self._decode_auto_resolve(row) for row in rows]

    def auto_resolve_evaluation(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            rows = [self._decode_auto_resolve(row) for row in conn.execute("SELECT * FROM auto_resolve_decisions ORDER BY id DESC").fetchall()]
        simulated = [row for row in rows if row.get("simulated")]
        eligible = [row for row in simulated if row.get("would_auto_resolve")]
        false_positive_statuses = {"REJECTED", "SENT_TO_HUMAN", "TEACH_REQUIRED"}
        false_positives = [row for row in eligible if row.get("final_human_outcome") in false_positive_statuses or (row.get("final_human_outcome") == "EDITED_AND_APPROVED" and "Major" in str(row.get("edit_severity") or ""))]
        source_overrides = sum(1 for row in rows if "source_route_precedence" not in (row.get("eligibility_reasons") or []) and str(row.get("channel")).lower() == "instagram" and row.get("would_auto_resolve") and row.get("matched_knowledge_id") and row.get("category") in {"AUTO_RESOLVE", "GENERAL_FAQ"} and row.get("eligibility_reasons") == [])
        whatsapp_activity = sum(1 for row in rows if str(row.get("channel")).lower() == "whatsapp" and (row.get("actually_sent") or row.get("would_auto_resolve")))
        order_interference = sum(1 for row in rows if any(reason in (row.get("eligibility_reasons") or []) for reason in ("protected_order_flow", "cam_handoff_conflict")) and row.get("actually_sent"))
        risk_violations = sum(1 for row in rows if row.get("would_auto_resolve") and set(row.get("risk_flags") or set()) )
        reviewed_grounded = sum(1 for row in rows if row.get("final_human_outcome") and str(row.get("grounding_status")).lower() == "grounded")
        if reviewed_grounded >= 100 and len(eligible) >= 50 and not false_positives and not source_overrides and not whatsapp_activity and not order_interference and not risk_violations:
            readiness_status = "PILOT ELIGIBLE"
        elif rows:
            readiness_status = "OBSERVING"
        else:
            readiness_status = "NOT READY"
        return {
            "decisions": len(rows), "simulated_eligible_cases": len(eligible),
            "simulation_false_positives": len(false_positives), "source_routing_overrides": source_overrides,
            "whatsapp_ai_activity": whatsapp_activity, "order_cam_interference": order_interference,
            "risk_violations": risk_violations, "reviewed_grounded_cases": reviewed_grounded,
            "approved_as_is": sum(1 for row in rows if row.get("final_human_outcome") == "APPROVED"),
            "edited_cases": sum(1 for row in rows if row.get("final_human_outcome") == "EDITED_AND_APPROVED"),
            "rejected_cases": sum(1 for row in rows if row.get("final_human_outcome") == "REJECTED"),
            "human_handoffs": sum(1 for row in rows if row.get("final_human_outcome") == "SENT_TO_HUMAN"),
            "readiness_status": readiness_status, "false_positive_rows": false_positives,
            "false_positive_count": len(false_positives),
            "simulation_accuracy": round((len(eligible) - len(false_positives)) / len(eligible), 4) if eligible else 0.0,
            "alerts": [
                *(["WhatsApp AI activity > 0"] if whatsapp_activity else []),
                *(["Source-routing override > 0"] if source_overrides else []),
                *(["Order/CAM interference > 0"] if order_interference else []),
                *(["Unsafe risk candidate marked eligible"] if risk_violations else []),
                *(["Simulation false positive"] if false_positives else []),
            ],
        }

    def knowledge_resolution_problems(self, knowledge_id: str) -> Dict[str, int]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT status, edit_severity FROM assisted_resolution_suggestions WHERE matched_knowledge_id=?", (knowledge_id,)).fetchall()
        return {
            "repeated_rejections": sum(1 for row in rows if row["status"] == "REJECTED") >= 2,
            "major_edits": sum(1 for row in rows if row["edit_severity"] == "Major Edit") >= 2,
        }

    def knowledge_health(self, knowledge_id: int) -> Dict[str, Any]:
        """Operational health for one Custom Q&A without changing its state."""
        key = f"learned_knowledge#{int(knowledge_id)}"
        with self.get_connection() as conn:
            rows = [dict(row) for row in conn.execute("SELECT status, edit_severity FROM assisted_resolution_suggestions WHERE matched_knowledge_id=?", (key,)).fetchall()]
            decisions = [self._decode_auto_resolve(row) for row in conn.execute("SELECT * FROM auto_resolve_decisions WHERE matched_knowledge_id=?", (key,)).fetchall()]
        approvals = sum(1 for row in rows if row.get("status") == "APPROVED")
        edits = sum(1 for row in rows if row.get("status") == "EDITED_AND_APPROVED")
        major_edits = sum(1 for row in rows if row.get("edit_severity") == "Major Edit")
        rejections = sum(1 for row in rows if row.get("status") == "REJECTED")
        not_eligible = rejections >= 2 or major_edits >= 2
        return {
            "usage_count": len(rows), "assisted_approvals": approvals, "edits": edits,
            "major_edits": major_edits, "rejections": rejections,
            "simulation_eligibility": any(row.get("would_auto_resolve") for row in decisions),
            "last_used": max((row.get("created_at") for row in decisions), default=None),
            "health": "Not Eligible for Auto Resolve" if not_eligible else ("Review Recommended" if edits or rejections else "Healthy"),
        }

    def get_auto_resolve_settings(self) -> Dict[str, Any]:
        default_categories = {"GENERAL_FAQ": False, "PRODUCT_DESCRIPTION": False, "MENU_EXPLANATION": False, "GENERAL_BRAND_INFORMATION": False, "AUTO_RESOLVE": False}
        defaults = {"kill_switch": True, "category_controls": default_categories.copy(), "category_settings": {key: {"simulation_enabled": True, "actual_enabled": False, "min_confidence": config.LLM_AUTO_RESOLVE_MIN_CONFIDENCE} for key in default_categories}}
        with self.get_connection() as conn:
            rows = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM auto_resolve_settings").fetchall()}
        try: defaults["kill_switch"] = json.loads(rows.get("kill_switch", "true"))
        except Exception: pass
        try: defaults["category_controls"].update(json.loads(rows.get("category_controls", "{}")))
        except Exception: pass
        try:
            for key, value in json.loads(rows.get("category_settings", "{}") or "{}").items():
                if key in defaults["category_settings"] and isinstance(value, dict):
                    defaults["category_settings"][key].update(value)
        except Exception: pass
        defaults.update({"simulation_enabled": bool(config.LLM_AUTO_RESOLVE_SIMULATION_ENABLED), "actual_enabled": bool(config.LLM_AUTO_RESOLVE_ENABLED and defaults["kill_switch"]), "min_confidence": config.LLM_AUTO_RESOLVE_MIN_CONFIDENCE})
        return defaults

    def set_auto_resolve_setting(self, key: str, value: Any) -> None:
        with self.get_connection() as conn:
            conn.execute("INSERT INTO auto_resolve_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (key, json.dumps(value), datetime.utcnow().isoformat()))
            conn.commit()

    def increment_learned_knowledge_usage(self, knowledge_id: int) -> bool:
        with self.get_connection() as conn:
            cur = conn.execute("UPDATE learned_knowledge SET usage_count=COALESCE(usage_count,0)+1, last_used_at=? WHERE id=?", (datetime.utcnow().isoformat(), knowledge_id)); conn.commit(); return cur.rowcount > 0

    def log_rag_audit(
        self,
        user_message: str,
        matched_template: Optional[str],
        rag_invoked: bool,
        retrieved_sources: List[str],
        confidence_score: float,
        proposed_template: Optional[str],
        proposed_response: Optional[str],
        approval_status: str,
        final_customer_response: str
    ):
        """Logs an audit record for every RAG fallback invocation."""
        sources_str = json.dumps(retrieved_sources or [])
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO rag_audit_logs (
                user_message, matched_template, rag_invoked, retrieved_sources,
                confidence_score, proposed_template, proposed_response,
                approval_status, final_customer_response, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_message, matched_template, 1 if rag_invoked else 0, sources_str,
                confidence_score, proposed_template, proposed_response,
                approval_status, final_customer_response, datetime.utcnow().isoformat()
            ))
            conn.commit()

    def admin_login_rate_status(self, scopes: List[str], now: float, window_seconds: float = 300.0) -> int:
        """Return the remaining cooldown, if any, across login throttle scopes."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT scope, last_failed_at, blocked_until FROM admin_login_throttles "
                f"WHERE scope IN ({','.join('?' for _ in scopes)})",
                tuple(scopes),
            ).fetchall() if scopes else []
        retry_after = 0
        for row in rows:
            blocked_until = float(row["blocked_until"] or 0)
            last_failed_at = row["last_failed_at"]
            if blocked_until > now:
                retry_after = max(retry_after, int(blocked_until - now) + 1)
            elif last_failed_at is not None and now - float(last_failed_at) > window_seconds:
                continue
        return retry_after

    def record_admin_login_failure(
        self,
        scopes: List[Tuple[str, int, float]],
        now: float,
        window_seconds: float = 300.0,
    ) -> int:
        """Atomically increment persistent throttle scopes and return cooldown."""
        retry_after = 0
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for scope, threshold, cooldown in scopes:
                row = conn.execute(
                    "SELECT failures, first_failed_at, last_failed_at, blocked_until "
                    "FROM admin_login_throttles WHERE scope=?",
                    (scope,),
                ).fetchone()
                failures = int(row["failures"] or 0) if row else 0
                last_failed_at = float(row["last_failed_at"]) if row and row["last_failed_at"] is not None else None
                if last_failed_at is None or now - last_failed_at > window_seconds:
                    failures = 0
                failures += 1
                blocked_until = float(row["blocked_until"] or 0) if row else 0
                if failures >= threshold:
                    blocked_until = max(blocked_until, now + cooldown)
                conn.execute(
                    "INSERT INTO admin_login_throttles "
                    "(scope, failures, first_failed_at, last_failed_at, blocked_until) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(scope) DO UPDATE SET failures=excluded.failures, "
                    "first_failed_at=excluded.first_failed_at, last_failed_at=excluded.last_failed_at, "
                    "blocked_until=excluded.blocked_until",
                    (
                        scope,
                        failures,
                        now if not row or last_failed_at is None or now - last_failed_at > window_seconds else row["first_failed_at"],
                        now,
                        blocked_until,
                    ),
                )
                if blocked_until > now:
                    retry_after = max(retry_after, int(blocked_until - now) + 1)
            conn.commit()
        return retry_after

    def reset_admin_login_scopes(self, scopes: List[str]) -> None:
        if not scopes:
            return
        with self.get_connection() as conn:
            conn.executemany(
                "DELETE FROM admin_login_throttles WHERE scope=?",
                ((scope,) for scope in scopes),
            )
            conn.commit()

    def create_admin_session(
        self,
        session_hash: str,
        username: str,
        csrf_hash: str,
        security_version: str,
        created_at: float,
        expires_at: float,
    ) -> None:
        with self.get_connection() as conn:
            conn.execute(
                "INSERT INTO admin_sessions "
                "(session_hash, username, csrf_hash, security_version, created_at, last_seen_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_hash, username, csrf_hash, security_version, created_at, created_at, expires_at),
            )
            conn.commit()

    def get_admin_session(
        self,
        session_hash: str,
        now: float,
        idle_seconds: float,
        security_version: str,
    ) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM admin_sessions WHERE session_hash=? AND revoked_at IS NULL",
                (session_hash,),
            ).fetchone()
            if not row:
                return None
            if (
                float(row["expires_at"]) <= now
                or now - float(row["last_seen_at"]) > idle_seconds
                or row["security_version"] != security_version
            ):
                conn.execute("UPDATE admin_sessions SET revoked_at=? WHERE session_hash=?", (now, session_hash))
                conn.commit()
                return None
            conn.execute("UPDATE admin_sessions SET last_seen_at=? WHERE session_hash=?", (now, session_hash))
            conn.commit()
            return dict(row)

    def revoke_admin_session(self, session_hash: str, now: float) -> None:
        with self.get_connection() as conn:
            conn.execute("UPDATE admin_sessions SET revoked_at=? WHERE session_hash=? AND revoked_at IS NULL", (now, session_hash))
            conn.commit()

    def revoke_admin_sessions_for_user(self, username: str, now: float) -> None:
        with self.get_connection() as conn:
            conn.execute("UPDATE admin_sessions SET revoked_at=? WHERE username=? AND revoked_at IS NULL", (now, username))
            conn.commit()

    def record_admin_security_event(
        self,
        event_type: str,
        username: Optional[str],
        ip_hash: Optional[str],
        user_agent: Optional[str],
        occurred_at: float,
    ) -> None:
        with self.get_connection() as conn:
            conn.execute(
                "INSERT INTO admin_security_audit "
                "(event_type, username, ip_hash, user_agent, occurred_at) VALUES (?, ?, ?, ?, ?)",
                (str(event_type)[:80], str(username or "")[:200] or None, str(ip_hash or "")[:128] or None, str(user_agent or "")[:300] or None, occurred_at),
            )
            conn.commit()

    def list_admin_security_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, event_type, username, ip_hash, user_agent, occurred_at "
                "FROM admin_security_audit ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

db = Database()
