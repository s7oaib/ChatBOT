import os
import re
import sqlite3
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from camellia.config import config
from camellia.text_rules import strip_flower_emoji
from camellia.candidate_safety import (
    candidate_eligibility,
    knowledge_key,
    normalize_question,
    validate_candidate_answer,
)

class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.DB_PATH
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        self.init_db()

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

            # Self-Healing Learned Knowledge base
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
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            # An existing deployment's table predates route_source; add it
            # without disturbing rows already written.
            cursor.execute("PRAGMA table_info(message_route_audit)")
            if "route_source" not in {row[1] for row in cursor.fetchall()}:
                cursor.execute(
                    "ALTER TABLE message_route_audit ADD COLUMN route_source TEXT"
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
                unresolved_topic TEXT,
                status TEXT NOT NULL DEFAULT 'CREATED',
                escalation_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP
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
                knowledge_scope TEXT NOT NULL DEFAULT '[]',
                escalation_policy TEXT NOT NULL DEFAULT 'human_if_unsupported',
                priority_products TEXT NOT NULL DEFAULT '[]',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                archived_at TIMESTAMP
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_instagram_routes_ad ON instagram_source_routes(ad_id, active)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_instagram_routes_media ON instagram_source_routes(media_id, active)")
            # A reel shared into a DM is matched by its normalized shortcode.
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_instagram_routes_shortcode ON instagram_source_routes(shortcode, active)")

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
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_topics_normalized_name ON instagram_topics(name COLLATE NOCASE)")

            self._ensure_column(cursor, "instagram_source_routes", "topic_id", "TEXT")
            # The menu shown for this route. Defaults from the selected
            # Topic's default_menu_id at save time (see validate_route_payload)
            # but is its own column so a route can override it independently —
            # "the client may override it if needed" without changing the Topic.
            self._ensure_column(cursor, "instagram_source_routes", "menu_id", "TEXT")

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
                cursor.execute(
                    "INSERT OR IGNORE INTO menu_catalogue (menu_id, name, category, description, active, is_builtin) "
                    "VALUES (?, ?, ?, ?, 1, 1)",
                    (menu_id, name, category, description),
                )

            self._create_indexes(cursor)

            conn.commit()

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

    def save_instagram_source_route(self, route: Dict[str, Any], routing_id: Optional[int] = None) -> Dict[str, Any]:
        """Create/update one source route, deduplicating URL and identifiers."""
        now = datetime.utcnow().isoformat()
        values = (
            route["source_type"], route.get("canonical_url"), route.get("shortcode"),
            route.get("media_id"), route.get("ad_id"), route.get("campaign_id"),
            route.get("routing_topic", ""), json.dumps(route.get("knowledge_scope", [])),
            route.get("escalation_policy", "human_if_unsupported"),
            json.dumps(route.get("priority_products", [])), int(bool(route.get("active", True))),
            route.get("topic_id"), route.get("menu_id"), now,
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
                     routing_topic, knowledge_scope, escalation_policy, priority_products,
                     active, topic_id, menu_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values[:-1] + (now, now),
                )
                routing_id = int(cursor.lastrowid)
            else:
                cursor = conn.execute(
                    """UPDATE instagram_source_routes SET source_type=?, canonical_url=?, shortcode=?,
                    media_id=?, ad_id=?, campaign_id=?, routing_topic=?, knowledge_scope=?,
                    escalation_policy=?, priority_products=?, active=?, topic_id=?, menu_id=?, updated_at=?, archived_at=NULL
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

    def list_instagram_source_routes(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self.get_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM instagram_source_routes {where} ORDER BY updated_at DESC, routing_id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_instagram_source_route(self, routing_id: int) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM instagram_source_routes WHERE routing_id = ?", (routing_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_instagram_source_route(self, field: str, value: str) -> Optional[Dict[str, Any]]:
        # The column name is interpolated, so the allowlist is the thing
        # keeping this query safe. Widening it is adding a name to the list,
        # never relaxing the check.
        if field not in ("ad_id", "media_id", "shortcode"):
            raise ValueError("Unsupported route identifier.")
        with self.get_connection() as conn:
            row = conn.execute(
                f"SELECT * FROM instagram_source_routes WHERE {field} = ? AND active = 1 "
                "AND archived_at IS NULL ORDER BY updated_at DESC LIMIT 1",
                (value,),
            ).fetchone()
        return dict(row) if row else None

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

        A row is written to instagram_route_removals before the delete, so
        what this route was and who removed it survives the delete itself.
        """
        with self.get_connection() as conn:
            route = conn.execute(
                "SELECT * FROM instagram_source_routes WHERE routing_id = ?", (routing_id,)
            ).fetchone()
            if not route:
                return False
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

    def find_menu_catalogue_entry_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM menu_catalogue WHERE name = ? COLLATE NOCASE", (name.strip(),)
            ).fetchone()
        return dict(row) if row else None

    def create_menu_catalogue_entry(
        self, menu_id: str, name: str, category: str, description: str,
        active: bool = True, is_builtin: bool = False,
    ) -> Dict[str, Any]:
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO menu_catalogue (menu_id, name, category, description, active, is_builtin)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (menu_id, name, category, description, int(active), int(is_builtin)),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM menu_catalogue WHERE menu_id = ?", (menu_id,)).fetchone()
        return dict(row)

    def update_menu_catalogue_entry(
        self, menu_id: str, *, name: Optional[str] = None, category: Optional[str] = None,
        description: Optional[str] = None, active: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        fields, values = [], []
        for column, value in (("name", name), ("category", category), ("description", description)):
            if value is not None:
                fields.append(f"{column} = ?")
                values.append(value)
        if active is not None:
            fields.append("active = ?")
            values.append(int(active))
        if not fields:
            return self.get_menu_catalogue_entry(menu_id)
        fields.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.append(menu_id)
        with self.get_connection() as conn:
            cursor = conn.execute(f"UPDATE menu_catalogue SET {', '.join(fields)} WHERE menu_id = ?", values)
            conn.commit()
        return self.get_menu_catalogue_entry(menu_id) if cursor.rowcount else None

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
    ) -> Dict[str, Any]:
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO instagram_topics
                    (topic_id, name, description, default_menu_id, priority_products, knowledge_scope, active)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (topic_id, name, description, default_menu_id,
                 json.dumps(priority_products), json.dumps(knowledge_scope), int(active)),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM instagram_topics WHERE topic_id = ?", (topic_id,)).fetchone()
        return dict(row)

    def update_instagram_topic(
        self, topic_id: str, *, name: Optional[str] = None, description: Optional[str] = None,
        default_menu_id: Optional[str] = None, priority_products: Optional[List[str]] = None,
        knowledge_scope: Optional[List[str]] = None, active: Optional[bool] = None,
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
        if active is not None:
            fields.append("active = ?"); values.append(int(active))
        if not fields:
            return self.get_instagram_topic(topic_id)
        fields.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())
        values.append(topic_id)
        with self.get_connection() as conn:
            cursor = conn.execute(f"UPDATE instagram_topics SET {', '.join(fields)} WHERE topic_id = ?", values)
            conn.commit()
        return self.get_instagram_topic(topic_id) if cursor.rowcount else None

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
            # get_escalations() filters on status, ordered by id DESC.
            "CREATE INDEX IF NOT EXISTS idx_escalations_status_id ON escalations(status, id DESC);",
            "CREATE INDEX IF NOT EXISTS idx_escalations_user ON escalations(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_escalations_created ON escalations(created_at);",
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
    ):
        """Log message exchange and increment analytics counters."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO conversations (
                user_id, channel, user_message, bot_response, was_matched, template_id,
                message_source, ad_id, ad_title, source_routing_id, source_match_method,
                source_type, shortcode, media_id, routing_topic, source_context_used
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id, channel, user_message, bot_response, 1 if was_matched else 0, template_id,
                message_source, ad_id, ad_title, source_routing_id, source_match_method,
                source_type, shortcode, media_id, routing_topic, int(bool(source_context_used)),
            ))
            
            cursor.execute("UPDATE analytics SET value = value + 1 WHERE key = 'total_messages';")
            if was_matched:
                cursor.execute("UPDATE analytics SET value = value + 1 WHERE key = 'matched_responses';")
            else:
                cursor.execute("UPDATE analytics SET value = value + 1 WHERE key = 'escalations_count';")
                
            conn.commit()

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
    ) -> Dict[str, Any]:
        """Records a new opaque handoff reference. Never overwrites one."""
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO cross_channel_handoffs (
                    handoff_ref, source_channel, source_user_id, reason, summary,
                    known_entities, unresolved_topic, status, escalation_id, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?)
                """,
                (
                    handoff_ref, source_channel, source_user_id, reason, summary,
                    json.dumps(known_entities or {}, ensure_ascii=False), unresolved_topic,
                    escalation_id, expires_at,
                ),
            )
            conn.commit()
        return self.get_handoff_by_ref(handoff_ref)

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
                  AND status NOT IN ('RESOLVED', 'EXPIRED')
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

    def add_learned_knowledge(
        self,
        topic: str,
        question_pattern: str,
        verified_answer: str,
        source: str = "staff_resolution",
        confidence: float = 1.0
    ) -> Optional[int]:
        """Adds or updates a verified knowledge item in the self-healing knowledge base."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            cursor.execute("""
            INSERT INTO learned_knowledge (topic, question_pattern, verified_answer, source, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (topic.strip(), question_pattern.strip().lower(), verified_answer.strip(), source, confidence, now, now))
            conn.commit()
            return cursor.lastrowid

    def get_learned_knowledge(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieves active learned knowledge entries ordered by most recently updated."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM learned_knowledge ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def search_learned_knowledge(self, query_text: str) -> Optional[Dict[str, Any]]:
        """
        Searches learned knowledge base for relevant facts matching the query.
        Returns the highest-scoring matching item if relevance threshold is met.
        """
        if not query_text:
            return None

        q_clean = query_text.lower().strip()
        q_tokens = set(re.findall(r'\w+', q_clean))
        if not q_tokens:
            return None

        items = self.get_learned_knowledge(limit=150)
        best_match = None
        best_score = 0.0

        for item in items:
            topic = item.get("topic", "").lower()
            pattern = item.get("question_pattern", "").lower()
            
            # Direct phrase/substring match
            if pattern in q_clean or q_clean in pattern:
                return item
            if topic in q_clean:
                return item

            # Token overlap scoring
            pattern_tokens = set(re.findall(r'\w+', pattern)) | set(re.findall(r'\w+', topic))
            # Remove generic stop words
            stop_words = {"is", "are", "do", "you", "the", "a", "an", "and", "in", "of", "to", "for", "your", "what", "how", "can", "have"}
            filtered_patterns = pattern_tokens - stop_words
            filtered_q = q_tokens - stop_words

            if filtered_patterns and filtered_q:
                overlap = filtered_patterns.intersection(filtered_q)
                score = len(overlap) / max(len(filtered_patterns), 1)
                if len(overlap) >= 2 or (len(overlap) == 1 and len(filtered_patterns) == 1):
                    if score > best_score:
                        best_score = score
                        best_match = item

        if best_match and best_score >= 0.4:
            return best_match
        return None

    def delete_learned_knowledge(self, knowledge_id: int) -> bool:
        """Deletes a learned knowledge entry by id."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM learned_knowledge WHERE id = ?", (knowledge_id,))
            conn.commit()
            return cursor.rowcount > 0

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
            cursor.execute(f"DELETE FROM conversations WHERE {older_than('timestamp')}", (cutoff,))
            removed_conversations = cursor.rowcount
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
            cursor.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
            removed_conversations = cursor.rowcount
            cursor.execute("DELETE FROM escalations WHERE user_id = ?", (user_id,))
            removed_escalations = cursor.rowcount
            cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            removed_sessions = cursor.rowcount
            conn.commit()

        return {
            "conversations": max(removed_conversations, 0),
            "escalations": max(removed_escalations, 0),
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
                    grounding_result, knowledge_gap, escalation_status, route_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(user_id), str(channel), user_message, route,
                json.dumps(intents or []), json.dumps(entities or {}, ensure_ascii=False),
                confidence, json.dumps(knowledge_ids or []), json.dumps(actions or []),
                int(bool(llm_called)), int(bool(fallback_used)), grounding_result,
                int(bool(knowledge_gap)), escalation_status, route_source,
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

    def get_pending_candidates(self) -> List[Dict[str, Any]]:
        """Retrieves all candidate templates awaiting human approval."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM candidate_templates WHERE status = 'PENDING_APPROVAL' ORDER BY created_at DESC")
            rows = cursor.fetchall()
            result = []
            for row in rows:
                item = dict(row)
                # Rows written before the save-time strip existed still carry
                # flowers, so clean them on the way to the dashboard too.
                item["response"] = strip_flower_emoji(item.get("response") or "")
                eligible, _ = candidate_eligibility(item.get("user_message") or "", item["response"])
                valid, _ = validate_candidate_answer(item["response"])
                if not eligible or not valid:
                    continue
                item["original_draft"] = item.get("original_draft") or item["response"]
                item["edited"] = bool(item.get("edited"))
                try:
                    item["source_refs"] = json.loads(item.get("source_refs") or "[]")
                except Exception:
                    item["source_refs"] = []
                result.append(item)
            return result

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

db = Database()
