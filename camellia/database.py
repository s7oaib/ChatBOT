import os
import re
import sqlite3
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from camellia.config import config

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

            # Seed counters if empty
            cursor.execute("INSERT OR IGNORE INTO analytics (key, value) VALUES ('total_messages', 0);")
            cursor.execute("INSERT OR IGNORE INTO analytics (key, value) VALUES ('matched_responses', 0);")
            cursor.execute("INSERT OR IGNORE INTO analytics (key, value) VALUES ('escalations_count', 0);")

            self._create_indexes(cursor)

            conn.commit()

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
        )
        for statement in statements:
            cursor.execute(statement)

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

    def log_conversation(self, user_id: str, channel: str, user_message: str, bot_response: str, was_matched: bool, template_id: Optional[str] = None):
        """Log message exchange and increment analytics counters."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO conversations (user_id, channel, user_message, bot_response, was_matched, template_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, channel, user_message, bot_response, 1 if was_matched else 0, template_id))
            
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
        """Saves a proposed candidate template with status 'PENDING_APPROVAL'."""
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

    def get_pending_candidates(self) -> List[Dict[str, Any]]:
        """Retrieves all candidate templates awaiting human approval."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM candidate_templates WHERE status = 'PENDING_APPROVAL' ORDER BY created_at DESC")
            rows = cursor.fetchall()
            result = []
            for row in rows:
                item = dict(row)
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
