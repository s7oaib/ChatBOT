import os
import re
import sqlite3
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from camellia.config import config
from camellia.text_rules import strip_flower_emoji

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

            # Seed counters if empty
            cursor.execute("INSERT OR IGNORE INTO analytics (key, value) VALUES ('total_messages', 0);")
            cursor.execute("INSERT OR IGNORE INTO analytics (key, value) VALUES ('matched_responses', 0);")
            cursor.execute("INSERT OR IGNORE INTO analytics (key, value) VALUES ('escalations_count', 0);")

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
    ):
        """Log message exchange and increment analytics counters."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO conversations (
                user_id, channel, user_message, bot_response, was_matched, template_id,
                message_source, ad_id, ad_title
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id, channel, user_message, bot_response, 1 if was_matched else 0, template_id,
                message_source, ad_id, ad_title,
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
    ) -> int:
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
        sources_str = json.dumps(source_refs or [])
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO candidate_templates (name, status, user_message, response, source_refs, confidence, created_by, created_at)
            VALUES (?, 'PENDING_APPROVAL', ?, ?, ?, ?, ?, ?)
            """, (name, user_message, response, sources_str, confidence, created_by, datetime.utcnow().isoformat()))
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
            try:
                item["source_refs"] = json.loads(item.get("source_refs") or "[]")
            except Exception:
                item["source_refs"] = []
            return item

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
