import os
import json
import hmac
import hashlib
import sqlite3
import time
import unittest
from unittest.mock import patch, MagicMock, PropertyMock
from fastapi.testclient import TestClient

# Use temp database for testing
TEST_DB_PATH = "test_camellia.db"
os.environ["DB_PATH"] = TEST_DB_PATH

from camellia.config import config
from camellia.database import db, Database
from camellia.ad_gate import ad_gate
from camellia.responder import responder
from camellia.whatsapp_adapter import whatsapp_adapter
from camellia.escalator import escalator
from camellia.app import app

class TestCamelliaBot(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Override DB path to test_camellia.db
        db.db_path = TEST_DB_PATH
        db.init_db()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except Exception:
                pass

    def setUp(self):
        # Ensure Ad-Gate and AI testing are isolated from .env overrides
        config.BYPASS_AD_GATE = False
        config.GROQ_ENABLED = False
        from camellia.gemini_responder import gemini_responder
        gemini_responder._groq_cooldown_until = 0.0
        from camellia.session_memory import session_memory
        session_memory._sessions.clear()
        responder.qa_data = responder._load_json(responder.qa_path)
        # Clear tables between tests
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions;")
            cursor.execute("DELETE FROM conversations;")
            cursor.execute("DELETE FROM escalations;")
            cursor.execute("UPDATE analytics SET value = 0;")
            conn.commit()

    def _post_signed(self, path: str, payload: dict):
        """POSTs a webhook payload carrying a valid X-Hub-Signature-256, as Meta does."""
        raw = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if config.META_APP_SECRET:
            headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(
                config.META_APP_SECRET.encode("utf-8"), raw, hashlib.sha256
            ).hexdigest()
        return self.client.post(path, content=raw, headers=headers)

    def test_ad_gate_organic_dm_ignored(self):
        """PRD Requirement #4: Non-ad organic DMs must be IGNORED by the bot."""
        organic_event = {
            "sender": {"id": "user_organic_101"},
            "message": {"text": "Hi, do you have Sondesh?"}
            # Note: No referral object present!
        }
        is_ad, sender, text, ad_id = ad_gate.inspect_and_gate_message(organic_event)
        self.assertFalse(is_ad, "Organic DM must NOT be ad-qualified.")
        self.assertEqual(sender, "user_organic_101")
        self.assertFalse(db.is_ad_qualified("user_organic_101", "instagram"))

    def test_ad_gate_ad_dm_qualified(self):
        """PRD Requirement #4: Ad-originated DMs (referral.source == 'ADS') are qualified."""
        ad_event = {
            "sender": {"id": "user_ad_202"},
            "message": {
                "text": "Hi, what are the Sondesh prices?",
                "referral": {
                    "source": "ADS",
                    "type": "OPEN_THREAD",
                    "ad_id": "1204040404"
                }
            }
        }
        is_ad, sender, text, ad_id = ad_gate.inspect_and_gate_message(ad_event)
        self.assertTrue(is_ad, "Ad-originated DM must be ad-qualified.")
        self.assertEqual(sender, "user_ad_202")
        self.assertEqual(ad_id, "1204040404")
        self.assertTrue(db.is_ad_qualified("user_ad_202", "instagram"))

    def test_subsequent_messages_in_ad_session(self):
        """Subsequent messages from an already ad-qualified session remain qualified."""
        # 1. First message from ad
        ad_event = {
            "sender": {"id": "user_ad_303"},
            "message": {
                "text": "Hi",
                "referral": {"source": "ADS", "ad_id": "999"}
            }
        }
        ad_gate.inspect_and_gate_message(ad_event)
        
        # 2. Second message without referral payload
        next_event = {
            "sender": {"id": "user_ad_303"},
            "message": {"text": "How much for Nolen Gur?"}
        }
        is_ad, sender, text, _ = ad_gate.inspect_and_gate_message(next_event)
        self.assertTrue(is_ad, "Subsequent message in ad-qualified session must be allowed.")

    @patch("camellia.ad_gate.httpx.Client")
    def test_ad_gate_message_edit_fallback(self, mock_client_cls):
        """Test fallback Graph API lookup for message_edit events lacking a sender field."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "from": {"id": "user_fallback_999"},
            "to": {"id": "page_123"},
            "message": "Edited message text from fallback"
        }
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

        event_without_sender = {
            "timestamp": 1700000000,
            "message_edit": {
                "mid": "m_test_mid_fallback_123"
            }
        }
        old_token = config.INSTAGRAM_ACCESS_TOKEN
        old_bypass = config.BYPASS_AD_GATE
        try:
            config.INSTAGRAM_ACCESS_TOKEN = "EAAB_test_mock_token_for_fallback_test"
            config.BYPASS_AD_GATE = True
            is_ad, sender, text, _ = ad_gate.inspect_and_gate_message(event_without_sender)
            self.assertTrue(is_ad)
            self.assertEqual(sender, "user_fallback_999")
            self.assertEqual(text, "Edited message text from fallback")
        finally:
            config.INSTAGRAM_ACCESS_TOKEN = old_token
            config.BYPASS_AD_GATE = old_bypass

    def test_responder_sondesh_pricing(self):
        """Test pricing query returns correct predefined template and photo card."""
        response, was_matched, tmpl_id, img_url = responder.match_and_respond("How much is Sondesh?")
        self.assertTrue(was_matched)
        self.assertEqual(tmpl_id, "sondesh_price_detailed")
        self.assertIn("₹450", response)
        self.assertIsNotNone(img_url)

    def test_responder_dynamic_weekly_menu(self):
        """Test menu query returns menu photo card URL."""
        response, was_matched, tmpl_id, img_url = responder.match_and_respond("Show me the veg menu this week")
        self.assertTrue(was_matched)
        self.assertEqual(tmpl_id, "weekly_menu_veg")
        self.assertIn("Camellia Artisanal", response)
        self.assertIsNotNone(img_url)

    def test_responder_delivery_location_matching(self):
        """Delivery in Delhi NCR matches delivery_area; Bangalore/out-of-area matches delivery_out_of_area."""
        # 1. Delhi NCR query
        resp_delhi, matched_delhi, tmpl_delhi, _ = responder.match_and_respond("do you deliver in delhi")
        self.assertTrue(matched_delhi)
        self.assertEqual(tmpl_delhi, "delivery_area")
        self.assertIn("Delhi NCR", resp_delhi)

        # 2. Bangalore location reply / query
        resp_blr, matched_blr, tmpl_blr, _ = responder.match_and_respond("bangalore")
        self.assertTrue(matched_blr)
        self.assertEqual(tmpl_blr, "delivery_out_of_area")
        self.assertIn("do not deliver to Bangalore", resp_blr)

        # 3. Explicit "do you deliver to Bangalore" query
        resp_blr2, matched_blr2, tmpl_blr2, _ = responder.match_and_respond("do you deliver to bangalore")
        self.assertTrue(matched_blr2)
        self.assertEqual(tmpl_blr2, "delivery_out_of_area")

    def test_responder_typo_fuzzy_matching(self):
        """Spelling typos like 'delivere' or 'banglore' are matched using fuzzy similarity."""
        # 1. Typo in delivere & delhi
        resp1, matched1, tmpl1, _ = responder.match_and_respond("do you delivere in delhi")
        self.assertTrue(matched1)
        self.assertEqual(tmpl1, "delivery_area")

        # 2. Typo in delivere & banglore
        resp2, matched2, tmpl2, _ = responder.match_and_respond("do you delivere in banglore")
        self.assertTrue(matched2)
        self.assertEqual(tmpl2, "delivery_out_of_area")

    def test_responder_explicit_escalation(self):
        """Sensitive queries (refund/complaint) must trigger explicit escalation."""
        response, was_matched, tmpl_id, img_url = responder.match_and_respond("I want a refund for my order")
        self.assertFalse(was_matched)
        self.assertEqual(tmpl_id, "explicit_escalation")

    def test_responder_unmatched_out_of_syllabus(self):
        """Out-of-syllabus query must escalate without guessing when AI is disabled."""
        with patch.object(config, "GROQ_ENABLED", False):
            response, was_matched, tmpl_id, img_url = responder.match_and_respond("What is the weather forecast in Tokyo today?")
            self.assertFalse(was_matched)
            self.assertIsNone(tmpl_id)
            self.assertTrue("notified our team" in response or "connect you with our team" in response)

    def test_product_overview_queries(self):
        """Product inquiries must match product_overview and return 3 menu cards."""
        test_queries = [
            "tell me about your product",
            "tell me about your products",
            "what products do you have",
            "what are your products",
            "what do you sell",
            "what items do you have",
            "about your products"
        ]
        for query in test_queries:
            resp, matched, tmpl_id, img_urls = responder.match_and_respond(query)
            self.assertTrue(matched, f"Query '{query}' should match product_overview")
            self.assertEqual(tmpl_id, "product_overview", f"Query '{query}' should map to product_overview")
            self.assertIsInstance(img_urls, list)
            self.assertEqual(len(img_urls), 3)

            # Each card now carries its own description as a caption, so the
            # detail the customer reads is spread across the captions rather
            # than sitting in one block of text. Assert on everything actually
            # delivered, not just the closing message.
            delivered = resp + " ".join(
                img.get("caption", "") for img in img_urls if isinstance(img, dict)
            )
            self.assertRegex(delivered, r"(?i)sondesh")
            self.assertRegex(delivered, r"(?i)meal")
            self.assertIn("₹450", delivered)
            # Every card should be paired with a caption, not left unexplained.
            self.assertTrue(
                all(isinstance(img, dict) and img.get("caption") for img in img_urls),
                "Each menu card should arrive with its own description",
            )

    def test_bot_capabilities_queries(self):
        """Assistance inquiries must match bot_capabilities with was_matched=True (zero escalation)."""
        test_queries = [
            "how can you assist me",
            "how can you help me",
            "how do you assist me",
            "what can you do",
            "what can u do",
            "how can you help",
            "how can you assist",
            "help me",
            "can you assist me",
            "what can you help me with"
        ]
        for query in test_queries:
            resp, matched, tmpl_id, _ = responder.match_and_respond(query)
            self.assertTrue(matched, f"Query '{query}' should match bot_capabilities")
            self.assertEqual(tmpl_id, "bot_capabilities", f"Query '{query}' should map to bot_capabilities")
            self.assertIn("Handcrafted Sondesh", resp)
            self.assertIn("Fresh Bengali Meals", resp)
            self.assertIn("Check Delivery & Timings", resp)
            self.assertIn("Calculate & Place Orders", resp)

    def test_bot_identity_queries(self):
        """Brand/bot introduction questions must map cleanly to bot_identity."""
        test_queries = [
            "who are you",
            "who are u",
            "are you a bot",
            "are you human",
            "tell me about yourself",
            "what is camellia"
        ]
        for query in test_queries:
            resp, matched, tmpl_id, _ = responder.match_and_respond(query)
            self.assertTrue(matched, f"Query '{query}' should match bot_identity")
            self.assertEqual(tmpl_id, "bot_identity", f"Query '{query}' should map to bot_identity")
            self.assertIn("Camellia Artisanal virtual assistant", resp)

    def test_strict_out_of_context_and_escalation(self):
        """In-domain queries are answered directly; out-of-domain and explicit triggers escalate."""
        # 1. In-domain product queries -> NOT escalated
        _, matched1, _, _ = responder.match_and_respond("tell me about your product")
        self.assertTrue(matched1)

        # 2. In-domain capability queries -> NOT escalated
        _, matched2, _, _ = responder.match_and_respond("how can you assist me")
        self.assertTrue(matched2)

        # 3. Explicit escalation triggers -> Escalated
        _, matched3, tmpl3, _ = responder.match_and_respond("I want to speak with a human agent")
        self.assertFalse(matched3)
        self.assertEqual(tmpl3, "explicit_escalation")

        # 3b. Manager complaint/demand framing ("I want to speak to your manager NOW") -> Escalated, NOT matched to info template
        _, matched3b, tmpl3b, _ = responder.match_and_respond("I want to speak to your manager NOW")
        self.assertFalse(matched3b, "Manager complaint framing must escalate to human, not match info template")
        self.assertEqual(tmpl3b, "explicit_escalation", "Manager complaint framing must map to explicit_escalation")

        # 4. Out-of-context query with AI disabled -> Escalated to deterministic fallback response
        with patch.object(config, "GROQ_ENABLED", False):
            resp4, matched4, tmpl4, _ = responder.match_and_respond("What is the capital of France?")
            self.assertFalse(matched4)
            self.assertIsNone(tmpl4)
            expected_fallback = responder.qa_data.get("escalation_triggers", {}).get("fallback_response")
            self.assertEqual(resp4, expected_fallback, "Deterministic fallback message must match exact fallback_response text")

    def test_whatsapp_adapter_flow(self):
        """PRD Requirement #2: WhatsApp integration handling."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "from": "919876543210",
                            "id": "wamid.HBgLMTIzNDU2Nzg5MA==",
                            "type": "text",
                            "text": {"body": "Hi, what is the price of Sondesh?"}
                        }]
                    }
                }]
            }]
        }
        res = whatsapp_adapter.parse_webhook_message(payload)
        self.assertIsNotNone(res)
        sender, msg_id, body = res
        self.assertEqual(sender, "919876543210")
        self.assertEqual(msg_id, "wamid.HBgLMTIzNDU2Nzg5MA==")
        self.assertEqual(body, "Hi, what is the price of Sondesh?")

        # Process through adapter
        with patch.object(whatsapp_adapter, "send_message"):
            result = whatsapp_adapter.process_incoming_message(sender, body)
        self.assertEqual(result["status"], "processed")
        self.assertTrue(result["was_matched"])
        self.assertEqual(result["template_id"], "sondesh_price_detailed")

        initial = db.get_analytics()
        # Log 1 matched and 1 escalated
        db.log_conversation("user_test_1", "instagram", "sondesh price", "response", True, "sondesh_price_detailed")
        db.log_conversation("user_test_2", "instagram", "random question", "fallback", False, None)

        analytics = db.get_analytics()
        self.assertEqual(analytics["total_messages"] - initial["total_messages"], 2)
        self.assertEqual(analytics["matched_responses"] - initial["matched_responses"], 1)
        self.assertEqual(analytics["escalations_count"] - initial["escalations_count"], 1)

    def test_fastapi_instagram_webhook_endpoints(self):
        """FastAPI Webhook Verification & POST Event Handler Tests."""
        # 1. GET Verify
        res = self.client.get(
            "/webhook/instagram",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": config.META_VERIFY_TOKEN,
                "hub.challenge": "test_challenge_123"
            }
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.text, "test_challenge_123")

        # 2. POST Event - Organic DM (Ignored)
        payload_organic = {
            "object": "instagram",
            "entry": [{
                "messaging": [{
                    "sender": {"id": "ig_organic_user"},
                    "message": {"text": "Hello, sondesh price?"}
                }]
            }]
        }
        res_post = self._post_signed("/webhook/instagram", payload_organic)
        self.assertEqual(res_post.status_code, 200)
        data = res_post.json()
        self.assertEqual(data["results"][0]["action"], "ignored_organic_dm")

        # 3. POST Event - Ad DM (accepted and queued)
        #
        # The webhook acknowledges the delivery and builds the reply off the
        # request path, so the response reports the routing decision rather
        # than the finished reply. What matters is that the customer still gets
        # answered — asserted below on the background task's actual effects.
        payload_ad = {
            "object": "instagram",
            "entry": [{
                "messaging": [{
                    "sender": {"id": "ig_ad_user"},
                    "message": {
                        "text": "Hello, sondesh price?",
                        "referral": {"source": "ADS", "ad_id": "ad_888"}
                    }
                }]
            }]
        }
        with patch("camellia.app.send_instagram_reply") as mock_send:
            res_post_ad = self._post_signed("/webhook/instagram", payload_ad)
        self.assertEqual(res_post_ad.status_code, 200)
        data_ad = res_post_ad.json()
        self.assertEqual(data_ad["results"][0]["action"], "queued")
        self.assertEqual(data_ad["results"][0]["ad_id"], "ad_888")

        # The reply was actually built and dispatched to the customer.
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][0], "ig_ad_user")
        self.assertTrue(mock_send.call_args[0][1].strip(), "customer must receive non-empty text")

        # And the exchange reached the conversation log.
        logged = db.get_conversations(limit=5, user_id="ig_ad_user")
        self.assertTrue(logged, "the queued message must still be logged")

    def test_instagram_webhook_failure_does_not_break_the_ack(self):
        """
        A crash while building the reply is logged, not swallowed silently, and
        never turns into a non-200 that would make Meta redeliver the message.
        """
        payload_ad = {
            "object": "instagram",
            "entry": [{
                "messaging": [{
                    "sender": {"id": "ig_boom_user"},
                    "message": {
                        "text": "sondesh price",
                        "referral": {"source": "ADS", "ad_id": "ad_boom"}
                    }
                }]
            }]
        }
        with patch("camellia.app.responder.match_and_respond", side_effect=RuntimeError("boom")), \
             patch("camellia.app.log.exception") as mock_log:
            res = self._post_signed("/webhook/instagram", payload_ad)

        self.assertEqual(res.status_code, 200)
        mock_log.assert_called_once()

    def test_webhook_rejects_unsigned_payload(self):
        """An unsigned POST must be refused whenever META_APP_SECRET is configured."""
        with patch.object(config, "META_APP_SECRET", "mock_meta_app_secret_123"):
            res = self.client.post("/webhook/instagram", json={"object": "instagram", "entry": []})
            self.assertEqual(res.status_code, 403)

    @patch("httpx.Client.post")
    def test_groq_in_context_query(self, mock_post):
        """Test Groq RAG responds accurately for in-syllabus queries when deterministic match score is low."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {"content": "We offer artisanal Nolen Gur Sondesh made with authentic date palm jaggery delivered fresh across Delhi NCR!"}
            }]
        }
        mock_post.return_value = mock_response

        with patch.object(config, "GROQ_ENABLED", True), patch.object(config, "GROQ_API_KEY", "test_key"):
            resp_text, was_matched, tmpl_id, img_url = responder.match_and_respond("Can you provide a historical overview of traditional dairy techniques in ancient India?")
        # Controlled Architecture: Unmatched queries NEVER send raw AI responses to customers; customer gets human escalation fallback while candidate is logged
        self.assertFalse(was_matched)
        self.assertIn("notified our team", resp_text.lower())

    @patch("httpx.Client.post")
    def test_groq_out_of_context_refusal(self, mock_post):
        """Test Groq RAG refuses out-of-syllabus query with OUT_OF_CONTEXT and triggers human escalation."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {"content": "OUT_OF_CONTEXT"}
            }]
        }
        mock_post.return_value = mock_response

        with patch.object(config, "GROQ_ENABLED", True), patch.object(config, "GROQ_API_KEY", "test_key"):
            resp_text, was_matched, tmpl_id, img_url = responder.match_and_respond("What is the capital of France?")
        self.assertFalse(was_matched)
        self.assertTrue("notified our team" in resp_text.lower() or "connect you with our team" in resp_text.lower())

    @patch("httpx.Client.post")
    def test_groq_offline_fallback(self, mock_post):
        """Test graceful fallback to human escalation if Groq API call fails."""
        mock_post.side_effect = Exception("API connection error")

        with patch.object(config, "GROQ_ENABLED", True), patch.object(config, "GROQ_API_KEY", "test_key"):
            resp_text, was_matched, tmpl_id, img_url = responder.match_and_respond("Can you send a violin player for a wedding?")
        self.assertFalse(was_matched)
        self.assertTrue(tmpl_id is None or tmpl_id.startswith("groq_"))

    def test_session_memory_fact_extraction(self):
        """Test extraction of customer name in Session Memory."""
        from camellia.session_memory import session_memory
        session_memory.clear_session("test_mem_user", "whatsapp")

        facts = session_memory.extract_and_update_facts(
            "test_mem_user", "whatsapp", "Hi, my name is Shoaib"
        )
        self.assertEqual(facts["name"], "Shoaib")

    def test_conversational_name_flow_and_recall(self):
        """Test full multi-turn conversational name flow: ask name -> reply with single word -> recall name."""
        from camellia.session_memory import session_memory
        user_id = "test_conv_user"
        channel = "whatsapp"
        session_memory.clear_session(user_id, channel)
        responder.reload_templates()

        # Step 1: User asks name before introducing
        resp1, matched1, tmpl1, _ = responder.match_and_respond("whats my name", user_id=user_id, channel=channel)
        self.assertTrue(matched1)
        self.assertEqual(tmpl1, "recall_name")
        self.assertIn("What should I call you", resp1)

        # Step 2: User responds with just their name (e.g. 'bhadwa')
        resp2, matched2, tmpl2, _ = responder.match_and_respond("bhadwa", user_id=user_id, channel=channel)
        self.assertTrue(matched2)
        self.assertEqual(tmpl2, "name_introduction")
        self.assertIn("Bhadwa", resp2)

        # Step 3: User asks 'whats my name' again
        resp3, matched3, tmpl3, _ = responder.match_and_respond("whats my name", user_id=user_id, channel=channel)
        self.assertTrue(matched3)
        self.assertEqual(tmpl3, "recall_name")
        self.assertIn("Your name is Bhadwa", resp3)

    def test_direct_name_introduction_patterns(self):
        """Test various direct name introduction phrases."""
        from camellia.session_memory import session_memory
        patterns = [
            ("My name is Shoaib", "Shoaib"),
            ("Call me Rahul", "Rahul"),
            ("I am Alex", "Alex"),
            ("Myself Priya Sen", "Priya Sen"),
            ("It's Ananya", "Ananya"),
        ]
        responder.reload_templates()
        for idx, (phrase, expected_name) in enumerate(patterns):
            uid = f"user_pat_{idx}"
            session_memory.clear_session(uid, "whatsapp")
            resp, matched, tmpl, _ = responder.match_and_respond(phrase, user_id=uid, channel="whatsapp")
            self.assertTrue(matched, f"Failed for {phrase}")
            self.assertEqual(tmpl, "name_introduction", f"Failed for {phrase}")
            self.assertIn(expected_name, resp)

    def test_bot_identity_queries(self):
        """Test queries asking about bot identity ('who are you', 'are you a bot', etc.)."""
        queries = [
            "who are you",
            "who are you?",
            "who r u",
            "what are you",
            "who is this",
            "are you a bot",
            "are you human",
            "what is camellia artisanal",
            "tell me about yourself",
        ]
        responder.reload_templates()
        for q in queries:
            resp, matched, tmpl, _ = responder.match_and_respond(q, user_id="test_bot_id_user", channel="whatsapp")
            self.assertTrue(matched, f"Failed for '{q}'")
            self.assertEqual(tmpl, "bot_identity", f"Failed for '{q}'")
            self.assertIn("Camellia Artisanal", resp)

    def test_escalation_handoff_brief_generation(self):
        """Test escalation handoff brief includes customer name and recent chat history."""
        from camellia.session_memory import session_memory
        user_id = "user_handoff_test"
        channel = "instagram"
        session_memory.clear_session(user_id, channel)

        # 1. Establish session memory turns
        responder.match_and_respond("Call me Shoaib", user_id=user_id, channel=channel, referral_source="ADS")

        # 2. Trigger unmatched escalation
        resp_text, was_matched, tmpl_id, img_url = responder.match_and_respond(
            "Can you send a violin player for a wedding?", user_id=user_id, channel=channel, referral_source="ADS"
        )
        with patch.object(escalator, "send_human_alert", return_value={"status": "sent", "mock": True}):
            escalator.handle_escalation(user_id, channel, "Can you send a violin player for a wedding?", "Unmatched Query", {})

        escalation_record = db.get_escalations(limit=1)[0]

        context_data = json.loads(escalation_record["context_data"])
        self.assertIn("handoff_brief", context_data)
        brief = context_data["handoff_brief"]
        self.assertEqual(brief["customer_name"], "Shoaib")
        self.assertTrue(len(brief["recent_chat_history"]) > 0)

    def test_reset_session_endpoint(self):
        """Test /api/reset-session clears session memory correctly."""
        from camellia.session_memory import session_memory
        user_id = "user_reset_test"
        channel = "whatsapp"

        # 1. Set fact
        session_memory.extract_and_update_facts(user_id, channel, "My name is Shoaib")
        self.assertEqual(session_memory.get_session(user_id, channel)["facts"]["name"], "Shoaib")

        # 2. Call reset API endpoint
        with patch.object(config, "ENABLE_WEB_TESTER", True):
            res = self.client.post("/api/reset-session", json={"user_id": user_id, "channel": channel})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "cleared")

        # 3. Verify memory is empty
        self.assertIsNone(session_memory.get_session(user_id, channel)["facts"]["name"])

    def test_web_tester_hidden_when_disabled(self):
        """The browser tester writes real rows and pages the owner, so it must be
        invisible unless ENABLE_WEB_TESTER is explicitly switched on."""
        with patch.object(config, "ENABLE_WEB_TESTER", False):
            self.assertEqual(self.client.get("/chat").status_code, 404)
            self.assertEqual(self.client.post("/api/test-chat", json={"message": "hi"}).status_code, 404)
            self.assertEqual(self.client.post("/api/reset-session", json={"user_id": "x"}).status_code, 404)

        with patch.object(config, "ENABLE_WEB_TESTER", True):
            self.assertEqual(self.client.get("/chat").status_code, 200)

    def test_legal_pages_are_public(self):
        """Meta requires a reachable Privacy Policy URL before an app can go Live,
        and both pages must stay open — no admin key, no tester flag."""
        for path in ("/privacy", "/terms"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, path)
            self.assertIn("text/html", res.headers["content-type"])
        self.assertIn(str(config.DATA_RETENTION_DAYS), self.client.get("/privacy").text)

    def test_retention_purge_removes_only_expired_rows(self):
        """Rows past DATA_RETENTION_DAYS go; anything inside the window stays."""
        from datetime import datetime, timedelta

        db.log_conversation("purge_recent", "whatsapp", "hi", "hello", True, "greeting")
        db.log_conversation("purge_old", "whatsapp", "hi", "hello", True, "greeting")

        stale = (datetime.utcnow() - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE conversations SET timestamp = ? WHERE user_id = 'purge_old'", (stale,)
            )
            conn.commit()

        removed = db.purge_expired_data(retention_days=90)
        self.assertEqual(removed["conversations"], 1)

        with db.get_connection() as conn:
            rows = conn.execute("SELECT user_id FROM conversations").fetchall()
        remaining = {r["user_id"] for r in rows}
        self.assertIn("purge_recent", remaining)
        self.assertNotIn("purge_old", remaining)

    def test_retention_purge_disabled_when_zero(self):
        """DATA_RETENTION_DAYS=0 means keep forever, not delete everything."""
        db.log_conversation("purge_keep", "whatsapp", "hi", "hello", True, "greeting")
        removed = db.purge_expired_data(retention_days=0)
        self.assertEqual(removed["conversations"], 0)
        with db.get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM conversations WHERE user_id = 'purge_keep'").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_delete_user_data_removes_every_trace(self):
        """Backs the DPDP deletion right promised on /privacy."""
        db.log_conversation("dpdp_user", "whatsapp", "hi", "hello", True, "greeting")
        db.log_conversation("other_user", "whatsapp", "hi", "hello", True, "greeting")
        db.record_escalation("dpdp_user", "whatsapp", "refund please", "Refund request", {})

        removed = db.delete_user_data("dpdp_user")
        self.assertEqual(removed["conversations"], 1)
        self.assertEqual(removed["escalations"], 1)

        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT user_id FROM conversations WHERE user_id = 'dpdp_user'"
            ).fetchall()
        self.assertEqual(rows, [])

        with db.get_connection() as conn:
            survivors = conn.execute(
                "SELECT COUNT(*) AS c FROM conversations WHERE user_id = 'other_user'"
            ).fetchone()["c"]
        self.assertEqual(survivors, 1)

    def test_delivery_monitor_alerts_only_after_repeated_failures(self):
        """One network blip must not page the owner; a dead token must."""
        from camellia.monitoring import DeliveryHealthMonitor

        monitor = DeliveryHealthMonitor(failure_threshold=3)
        with patch.object(monitor, "_send_owner_alert") as alert:
            monitor.record_failure("whatsapp", "timeout")
            monitor.record_failure("whatsapp", "timeout")
            self.assertFalse(alert.called)
            self.assertTrue(monitor.snapshot()["healthy"])

            monitor.record_failure("whatsapp", "401 invalid token")
            self.assertEqual(alert.call_count, 1)
            self.assertFalse(monitor.snapshot()["healthy"])

            # Still failing: alert once, do not spam.
            monitor.record_failure("whatsapp", "401 invalid token")
            self.assertEqual(alert.call_count, 1)

            monitor.record_success("whatsapp")
            self.assertTrue(monitor.snapshot()["healthy"])

    def test_freshness_ingredients_query(self):
        """Verify 'how fresh are the ingredients' matches freshness_ingredients template and not delivery_time."""
        resp_text, matched, tmpl_id, img_url = responder.match_and_respond(
            "how fresh are the ingredients", "test_user_fresh", "whatsapp"
        )
        self.assertTrue(matched)
        self.assertEqual(tmpl_id, "freshness_ingredients")
        self.assertIn("freshness and quality", resp_text.lower())
        self.assertNotIn("sushant lok", resp_text.lower())

    def test_aamar_mishti_250g_query_and_repeat_image_delivery(self):
        """Verify 'aamar mishti 250g' matches collection template and attaches photo cards on every request."""
        from camellia.session_memory import session_memory
        session_memory._sessions.clear()

        # Turn 1: User asks for menu -> gets text + menu photo cards
        text1, matched1, tmpl1, img1 = responder.match_and_respond(
            "send the menu", "user_test_menu_suppression", "whatsapp"
        )
        self.assertTrue(matched1)
        self.assertIsNotNone(img1, "First menu query should attach photo cards.")

        # Turn 2: User asks "aamar mishti 250g" -> gets exact price breakdown and photo cards
        text2, matched2, tmpl2, img2 = responder.match_and_respond(
            "aamar mishti 250g", "user_test_menu_suppression", "whatsapp"
        )
        self.assertTrue(matched2)
        self.assertEqual(tmpl2, "aamar_mishti_collection")
        self.assertIn("Collection 1 — Aamar Mishti", text2)
        self.assertIn("Classic / Rose", text2)
        self.assertIn("₹450", text2)
        self.assertIsNotNone(img2, "Menu/collection query should attach photo cards as requested.")

    def test_order_confirmation_escalation_and_verification(self):
        """Verify order confirmation triggers staff alert with verification status."""
        from camellia.escalator import escalator
        from camellia.whatsapp_adapter import whatsapp_adapter

        # 1. Test missing address/phone -> flags PENDING VERIFICATION
        res1 = escalator._evaluate_order_verification("I want 2 boxes of Nolen Gur", "no previous history")
        self.assertIn("PENDING VERIFICATION", res1)

        # 2. Test valid address & phone -> flags READY FOR VERIFICATION & INVOICING
        res2 = escalator._evaluate_order_verification(
            "Here is my order: 250g Nolen Gur. Address: Flat 402, Sector 56 Gurgaon, Phone: 9876543210", ""
        )
        self.assertIn("READY FOR VERIFICATION & INVOICING", res2)

        # 3. Test WhatsApp order_next_steps triggers escalation alert
        with patch.object(escalator, "handle_escalation") as mock_esc, patch.object(whatsapp_adapter, "send_message"):
            whatsapp_adapter.process_incoming_message("919999888877", "confirm my order here is my address Sector 43 Gurgaon")
            self.assertTrue(mock_esc.called)

    def test_order_calculator_integration(self):
        """Verify OrderCalculator correctly parses quantities and generates itemized draft receipts."""
        from camellia.order_calculator import order_calculator
        msg = "I want to order 2 packs of 250g Nolen Gur sondesh"
        calc_res = order_calculator.parse_and_calculate(msg)
        self.assertIsNotNone(calc_res)
        self.assertIn("ORDER DRAFT RECEIPT", calc_res["formatted_receipt"])
        self.assertIn("Nolen Gur", calc_res["formatted_receipt"])
        self.assertEqual(calc_res["quantity"], 2)
        self.assertEqual(calc_res["total_price"], 1200)

    def test_multi_turn_order_slot_filling(self):
        """PRD Requirement: Multi-turn order slot memory carries preferences across turns without repeating menus."""
        from camellia.responder import responder
        from camellia.session_memory import session_memory
        session_memory.clear_session("test_slot_user", "whatsapp")

        # Turn 1: User says "rose"
        resp1, match1, tmpl1, _ = responder.match_and_respond("rose", user_id="test_slot_user", channel="whatsapp")
        self.assertTrue(match1)
        self.assertEqual(tmpl1, "sondesh_classic_rose")
        self.assertIn("Which collection and pack size would you like", resp1)

        # Turn 2: User says "aamar mishti 500g" (combines Rose + Aamar Mishti + 500g)
        resp2, match2, tmpl2, _ = responder.match_and_respond("aamar mishti 500g", user_id="test_slot_user", channel="whatsapp")
        self.assertTrue(match2)
        self.assertEqual(tmpl2, "order_draft_receipt")
        self.assertIn("CAMELLIA ARTISANAL — ORDER DRAFT RECEIPT", resp2)
        self.assertIn("Rose Sondesh", resp2)
        self.assertIn("500g", resp2)
        self.assertIn("₹800", resp2)

    def test_multi_item_cart_receipt(self):
        """PRD Requirement: Ordering multiple items in a single message or adding to cart calculates itemized receipt with grand total."""
        from camellia.responder import responder
        from camellia.session_memory import session_memory
        session_memory.clear_session("test_multi_cart_user", "whatsapp")

        # Test single message with multiple items: "add rose 250g and jolbhora nolen gur 500g both"
        msg = "add rose 250g and jolbhora nolen gur 500g both"
        resp, match, tmpl, _ = responder.match_and_respond(msg, user_id="test_multi_cart_user", channel="whatsapp")
        self.assertTrue(match)
        self.assertEqual(tmpl, "order_draft_receipt")
        self.assertIn("CAMELLIA ARTISANAL — ORDER DRAFT RECEIPT", resp)
        self.assertIn("Rose Sondesh", resp)
        self.assertIn("Jolbhora Sondesh", resp)
        self.assertIn("₹450", resp)
        self.assertIn("₹1,400", resp)
        self.assertIn("₹1,850", resp)

    def test_cart_item_removal(self):
        """PRD Requirement: Removing an item from cart recalculates receipt for remaining items."""
        from camellia.responder import responder
        from camellia.session_memory import session_memory
        session_memory.clear_session("test_removal_user", "whatsapp")

        # Step 1: Add 3 items
        msg = "add rose 250g, jolbhora 500g, and mango 250g"
        resp1, match1, tmpl1, _ = responder.match_and_respond(msg, user_id="test_removal_user", channel="whatsapp")
        self.assertTrue(match1)
        self.assertEqual(tmpl1, "order_draft_receipt")
        self.assertIn("Rose Sondesh", resp1)
        self.assertIn("Jolbhora Sondesh", resp1)
        self.assertIn("Mango Sondesh", resp1)

        # Step 2: User says "remove rose"
        resp2, match2, tmpl2, _ = responder.match_and_respond("remove rose", user_id="test_removal_user", channel="whatsapp")
        self.assertTrue(match2)
        self.assertEqual(tmpl2, "order_draft_receipt")
        self.assertIn("Rose Sondesh* has been removed", resp2)
        self.assertNotIn("Rose Sondesh (Aamar", resp2)
        self.assertIn("Jolbhora Sondesh", resp2)
        self.assertIn("Mango Sondesh", resp2)
        self.assertIn("₹1,950", resp2)

    def test_out_of_context_question_does_not_trigger_receipt(self):
        """Out-of-context questions like 'Who is the President of US?' must NOT trigger an order receipt even if draft slots exist."""
        from camellia.responder import responder
        from camellia.session_memory import session_memory
        session_memory.clear_session("test_ooc_user", "whatsapp")

        # Step 1: User says "rose 250g" -> generates order receipt
        resp1, match1, tmpl1, _ = responder.match_and_respond("rose 250g", user_id="test_ooc_user", channel="whatsapp")
        self.assertTrue(match1)
        self.assertEqual(tmpl1, "order_draft_receipt")

        # Step 2: User asks "Who is the President of US?" -> MUST NOT return order_draft_receipt!
        resp2, match2, tmpl2, _ = responder.match_and_respond("Who is the President of US?", user_id="test_ooc_user", channel="whatsapp")
        self.assertNotEqual(tmpl2, "order_draft_receipt")
        self.assertNotIn("ORDER DRAFT RECEIPT", resp2)

    def test_whatsapp_quick_reply_buttons(self):
        """Verify WhatsAppAdapter includes quick reply interactive buttons for greetings and menu requests."""
        from camellia.whatsapp_adapter import whatsapp_adapter
        with patch.object(whatsapp_adapter, "send_message") as mock_send:
            whatsapp_adapter.process_incoming_message("919876543210", "Hi")
            self.assertTrue(mock_send.called)
            _, kwargs = mock_send.call_args
            buttons = kwargs.get("buttons")
            self.assertIsNotNone(buttons)
            self.assertIn("🌸 See Menu", buttons)
            self.assertIn("🛍️ Place Order", buttons)

    def test_parse_raw_menu_text(self):
        """Test MenuParser regex extraction of items, quantities, prices, and categories from unstructured text."""
        from camellia.menu_parser import menu_parser
        raw = """
        Bengal Sweet Special:
        • Rose Sondesh 250g - ₹450
        • Jolbhora Nolen Gur 500g - ₹1400
        
        Non-Veg Items:
        • Mutton Kosha Bento 1 box - ₹650
        • Chicken Cutlet 2pcs - ₹350
        """
        parsed = menu_parser.parse_menu_text(raw)
        self.assertTrue(len(parsed["vegetarian"]) >= 2)
        self.assertTrue(len(parsed["non_vegetarian"]) >= 2)
        
        veg_names = [item["item"] for item in parsed["vegetarian"]]
        self.assertTrue(any("Rose" in name for name in veg_names))
        
        nonveg_names = [item["item"] for item in parsed["non_vegetarian"]]
        self.assertTrue(any("Mutton" in name for name in nonveg_names))

    def test_parse_menu_card_endpoint(self):
        """Test POST /admin/parse-menu-card endpoint."""
        with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
            original_menu = f.read()

        try:
            headers = {"X-Admin-Key": config.ADMIN_API_KEY} if config.ADMIN_API_KEY else {}
            raw_text = "Rose Sondesh 250g - Rs 450\nKosha Mangsho Bento ₹650 (non veg)"
            response = self.client.post("/admin/parse-menu-card", json={"raw_text": raw_text}, headers=headers)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "success")
            self.assertTrue(data["updated_items_count"] >= 2)
        finally:
            with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
                f.write(original_menu)
            responder.reload_templates()

    def test_collection_1_and_2_matching(self):
        """Verify that typing 'collection 1' or 'collection 2' matches specific collection templates and delivers only their respective menu card."""
        from camellia.responder import responder
        
        resp1, match1, tmpl1, imgs1 = responder.match_and_respond("collection 1", user_id="test_col1", channel="whatsapp")
        self.assertTrue(match1)
        self.assertEqual(tmpl1, "aamar_mishti_collection")
        self.assertIn("Collection 1 — Aamar Mishti", resp1)
        self.assertEqual(imgs1, ["/images/AAMAR%20MISHTI.jpeg"])

        resp1_menu, match1_menu, tmpl1_menu, imgs1_menu = responder.match_and_respond("collection 1 menu", user_id="test_col1_menu", channel="whatsapp")
        self.assertTrue(match1_menu)
        self.assertEqual(tmpl1_menu, "aamar_mishti_collection")
        self.assertEqual(imgs1_menu, ["/images/AAMAR%20MISHTI.jpeg"])

        resp2, match2, tmpl2, imgs2 = responder.match_and_respond("collection 2", user_id="test_col2", channel="whatsapp")
        self.assertTrue(match2)
        self.assertEqual(tmpl2, "signature_collection")
        self.assertIn("Collection 2 — Signature Collection", resp2)
        self.assertEqual(imgs2, ["/images/Signature%20Sondesh.jpeg"])

        resp2_menu, match2_menu, tmpl2_menu, imgs2_menu = responder.match_and_respond("collection 2 menu", user_id="test_col2_menu", channel="whatsapp")
        self.assertTrue(match2_menu)
        self.assertEqual(tmpl2_menu, "signature_collection")
        self.assertEqual(imgs2_menu, ["/images/Signature%20Sondesh.jpeg"])

    @patch("camellia.gemini_responder.httpx.Client")
    def test_groq_template_refinement(self, mock_httpx_cls):
        """Verify Groq AI refines a template that carries no prices or tables."""
        from camellia.responder import responder
        from camellia.gemini_responder import gemini_responder

        refined = "Hello Shoaib! We deliver fresh across Delhi NCR every day."
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": refined}}]
        }
        mock_httpx_cls.return_value.__enter__.return_value.post.return_value = mock_response

        gemini_responder._groq_cooldown_until = 0.0
        gemini_responder._refinement_cache.clear()
        with patch.object(config, "GROQ_ENABLED", True), patch.object(config, "GROQ_API_KEY", "fake_key"):
            resp, match, tmpl, _ = responder.match_and_respond(
                "which areas do you deliver to", user_id="test_ai_refine", channel="whatsapp"
            )
            self.assertTrue(match)
            self.assertIn("Hello Shoaib!", resp)

    @patch("camellia.gemini_responder.httpx.Client")
    def test_price_table_template_is_never_refined(self, mock_httpx_cls):
        """
        A template stating prices or drawing a monospace table is sent exactly as
        written, without going near the model.

        Refinement is cosmetic — it rewords copy a human already approved. Asked
        to reword "₹600 / 250g" a model can reformat the number, reflow the
        table, or drop a row, and that reaches a paying customer as a wrong
        price. Skipping the call also removes a network round trip from the
        longest replies the bot sends.
        """
        from camellia.responder import responder
        from camellia.gemini_responder import gemini_responder

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "REWORDED — prices mangled"}}]
        }
        mock_httpx_cls.return_value.__enter__.return_value.post.return_value = mock_response

        gemini_responder._groq_cooldown_until = 0.0
        gemini_responder._refinement_cache.clear()
        with patch.object(config, "GROQ_ENABLED", True), patch.object(config, "GROQ_API_KEY", "fake_key"):
            resp, match, tmpl, _ = responder.match_and_respond(
                "collection 2", user_id="test_price_no_refine", channel="whatsapp"
            )
            self.assertTrue(match)
            self.assertEqual(tmpl, "signature_collection")
            self.assertNotIn("REWORDED", resp)
            self.assertIn("Collection 2 — Signature Collection", resp)
            mock_httpx_cls.return_value.__enter__.return_value.post.assert_not_called()

    @patch("camellia.gemini_responder.httpx.Client")
    def test_repeat_question_reuses_cached_refinement(self, mock_httpx_cls):
        """
        The same question against the same template refines once, then serves
        from cache — the provider is not called again.
        """
        from camellia.gemini_responder import gemini_responder

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Refined once."}}]
        }
        post = mock_httpx_cls.return_value.__enter__.return_value.post
        post.return_value = mock_response

        gemini_responder._groq_cooldown_until = 0.0
        gemini_responder._refinement_cache.clear()
        with patch.object(config, "GROQ_ENABLED", True), patch.object(config, "GROQ_API_KEY", "fake_key"):
            first, ok1 = gemini_responder.refine_template_response(
                "delivery_area", "We deliver across Delhi NCR.", "where do you deliver"
            )
            # Same question, different spacing and case — still one call.
            second, ok2 = gemini_responder.refine_template_response(
                "delivery_area", "We deliver across Delhi NCR.", "  Where  Do You Deliver  "
            )

        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertEqual(first, "Refined once.")
        self.assertEqual(second, "Refined once.")
        self.assertEqual(post.call_count, 1)

    @patch("camellia.gemini_responder.httpx.Client")
    def test_rag_prompt_stays_under_provider_token_budget(self, mock_httpx_cls):
        """
        The full-RAG prompt sent for an unmatched question must fit inside the
        provider's per-minute token ceiling.

        The prompt used to carry the entire knowledge base — 41,288 chars
        (~13.4k tokens) — which Groq's free-tier token-per-minute limit
        (~6-8k) rejects with HTTP 413 on every single unmatched question.
        The context is now summarised and budgeted; this pins the actual
        request payload (not just the context string) so a bigger menu or
        template set cannot silently push it back over the ceiling.
        """
        from camellia.gemini_responder import gemini_responder

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Drafted candidate answer."}}]
        }
        post = mock_httpx_cls.return_value.__enter__.return_value.post
        post.return_value = mock_response

        gemini_responder._groq_cooldown_until = 0.0
        with patch.object(config, "GROQ_ENABLED", True), patch.object(config, "GROQ_API_KEY", "fake_key"):
            resp, ok, reason = gemini_responder.generate_response(
                "Is the Nolen Gur real date palm jaggery or just flavoring?"
            )

        self.assertTrue(ok)
        payload = post.call_args.kwargs["json"]
        sent_prompt = payload["messages"][0]["content"]
        # Groq's tokenizer measured ~3.07 chars/token over this corpus; using
        # a conservative 3.0 keeps the estimate honest. 6000 tokens is the
        # tightest per-minute ceiling observed across Groq free-tier plans.
        estimated_tokens = len(sent_prompt) / 3.0
        self.assertLess(
            estimated_tokens, 6000,
            f"RAG prompt is {len(sent_prompt)} chars (~{int(estimated_tokens)} tokens) — "
            "over the provider's free-tier TPM ceiling; it will be rejected with HTTP 413."
        )

    @patch("camellia.gemini_responder.httpx.Client")
    def test_oversized_payload_gets_config_error_cooldown(self, mock_httpx_cls):
        """
        HTTP 413 must trip the long config-error cooldown, not be retried.

        On a TPM-capped plan an over-limit prompt can never succeed, so without
        the cooldown every unmatched customer message paid a full round trip to
        a guaranteed 413 before falling back to the template.
        """
        from camellia.gemini_responder import gemini_responder, CONFIG_ERROR_COOLDOWN_SECONDS

        mock_response = MagicMock()
        mock_response.status_code = 413
        mock_response.text = '{"error":{"message":"Request too large"}}'
        mock_httpx_cls.return_value.__enter__.return_value.post.return_value = mock_response

        gemini_responder._groq_cooldown_until = 0.0
        with patch.object(config, "GROQ_ENABLED", True), patch.object(config, "GROQ_API_KEY", "fake_key"):
            _resp, ok, reason = gemini_responder._query_groq("test prompt")

        self.assertFalse(ok)
        self.assertEqual(reason, "groq_http_413")
        self.assertGreater(gemini_responder._groq_cooldown_until, 0)
        self.assertAlmostEqual(
            gemini_responder._groq_cooldown_until - __import__("time").time(),
            CONFIG_ERROR_COOLDOWN_SECONDS, delta=5,
        )

    @patch("camellia.gemini_responder.httpx.Client")
    def test_refinement_cache_does_not_leak_customer_names(self, mock_httpx_cls):
        """
        Two customers asking the identical question must not share a cached
        refinement, because a refinement can address the customer by name.
        """
        from camellia.gemini_responder import gemini_responder

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Hi Asha, we deliver across Delhi NCR."}}]
        }
        post = mock_httpx_cls.return_value.__enter__.return_value.post
        post.return_value = mock_response

        gemini_responder._groq_cooldown_until = 0.0
        gemini_responder._refinement_cache.clear()
        with patch.object(config, "GROQ_ENABLED", True), patch.object(config, "GROQ_API_KEY", "fake_key"):
            gemini_responder.refine_template_response(
                "delivery_area", "We deliver across Delhi NCR.", "where do you deliver",
                user_context="Customer Name: Asha",
            )
            gemini_responder.refine_template_response(
                "delivery_area", "We deliver across Delhi NCR.", "where do you deliver",
                user_context="Customer Name: Bikram",
            )

        # A shared key would have served Asha's greeting to Bikram on one call.
        self.assertEqual(post.call_count, 2)

    @patch("camellia.gemini_responder.httpx.Client")
    def test_bad_api_key_pauses_provider_instead_of_retrying_forever(self, mock_httpx_cls):
        """
        An HTTP 400 (bad key / unserved model) stops the bot calling the
        provider at all.

        Previously only 429/500/502/503 tripped the cooldown, so a
        misconfigured key kept reporting itself available and every single
        customer message paid a full failed round trip before falling back to
        the template — measured at ~145ms per message against a live invalid
        key. Retrying a config error changes nothing; the deployment has to.
        """
        from camellia.gemini_responder import gemini_responder

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = '{"error": {"message": "Invalid API Key"}}'
        post = mock_httpx_cls.return_value.__enter__.return_value.post
        post.return_value = mock_response

        gemini_responder._groq_cooldown_until = 0.0
        with patch.object(config, "GROQ_ENABLED", True), \
             patch.object(config, "GROQ_API_KEY", "bad_key"):
            _, ok, reason = gemini_responder._query_groq("hello")
            self.assertFalse(ok)
            self.assertEqual(reason, "groq_http_400")

            # The provider is now parked, so nothing calls it again.
            self.assertEqual(gemini_responder.get_active_provider(), "none")
            self.assertGreater(gemini_responder._groq_cooldown_until, time.time() + 60)

            # And /health says so rather than reporting a healthy AI layer.
            status = gemini_responder.status()
            self.assertIs(status["working"], False)
            self.assertIn("groq", status["paused_seconds"])

        gemini_responder._groq_cooldown_until = 0.0

    @patch("camellia.gemini_responder.httpx.Client")
    def test_transient_error_uses_the_short_cooldown(self, mock_httpx_cls):
        """A 503 is the provider being busy, so it is retried within the minute."""
        from camellia.gemini_responder import gemini_responder

        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.text = "overloaded"
        mock_httpx_cls.return_value.__enter__.return_value.post.return_value = mock_response

        gemini_responder._groq_cooldown_until = 0.0
        with patch.object(config, "GROQ_ENABLED", True), patch.object(config, "GROQ_API_KEY", "k"):
            gemini_responder._query_groq("hello")

        pause = gemini_responder._groq_cooldown_until - time.time()
        self.assertGreater(pause, 0)
        self.assertLessEqual(pause, 60.0)
        gemini_responder._groq_cooldown_until = 0.0

    def test_how_much_is_sondesh_price_inquiry(self):
        """Verify 'How much is Sondesh?' returns price info template/RAG instead of order receipt draft."""
        from camellia.responder import responder
        resp, match, tmpl, _ = responder.match_and_respond("How much is Sondesh?", user_id="test_price_inquiry", channel="whatsapp")
        self.assertTrue(match)
        self.assertNotEqual(tmpl, "order_draft_receipt")
        self.assertEqual(tmpl, "sondesh_price_detailed")

    def test_admin_update_menu_endpoint(self):
        """Verify POST /admin/update-menu updates menu rotation and reloads responder."""
        with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
            original_menu = f.read()

        try:
            headers = {"X-Admin-Key": config.ADMIN_API_KEY} if config.ADMIN_API_KEY else {}
            res = self.client.post(
                "/admin/update-menu",
                headers=headers,
                json={
                    "week": "Test Rotation Week",
                    "vegetarian": [{"item": "Test Sondesh Special", "price_inr": 499}]
                }
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["status"], "success")
        finally:
            with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
                f.write(original_menu)
            responder.reload_templates()

    def test_collection_2_and_collection_1_full_precision_menu_details(self):
        """Verify Collection 2, Collection 1, and Kanchagolla responses list all flavours, items, Kanchagolla & Jolbhora explicitly."""
        from camellia.responder import responder

        # Collection 2 check
        resp2, match2, tmpl2, _ = responder.match_and_respond("collection 2 options", user_id="test_col2_user", channel="whatsapp")
        self.assertTrue(match2)
        self.assertEqual(tmpl2, "signature_collection")
        self.assertIn("Classic / Rose", resp2)
        self.assertTrue("Mango/Kesar" in resp2 or "Vanilla" in resp2)
        self.assertTrue("NolenGur" in resp2 or "Thandai" in resp2 or "Dates" in resp2)
        self.assertIn("Mahua Sondesh", resp2)
        self.assertIn("Jolbhora", resp2)
        self.assertIn("Kanchagolla", resp2)

        # Collection 1 check
        resp1, match1, tmpl1, _ = responder.match_and_respond("collection 1 options", user_id="test_col1_user", channel="whatsapp")
        self.assertTrue(match1)
        self.assertEqual(tmpl1, "aamar_mishti_collection")
        self.assertTrue("Ada" in resp1 or "Aam Ada" in resp1)
        self.assertIn("Jolbhora", resp1)
        self.assertIn("Kanchagolla", resp1)

        # Kanchagolla check
        resp_k, match_k, tmpl_k, _ = responder.match_and_respond("kanchagolla price", user_id="test_k_user", channel="whatsapp")
        self.assertTrue(match_k)
        self.assertEqual(tmpl_k, "kanchagolla")
        self.assertIn("Aamar", resp_k)
        self.assertIn("Signature A2", resp_k)
        self.assertIn("₹450", resp_k)
        self.assertIn("₹600", resp_k)
        self.assertIn("₹700", resp_k)

    def test_individual_flavour_and_pack_size_queries(self):
        """Verify queries for individual flavours, Mahua special, and pack size breakdowns."""
        from camellia.responder import responder

        # Mahua price check
        resp, match, tmpl, imgs = responder.match_and_respond("what are the price of mahua", user_id="test_mahua_user", channel="whatsapp")
        self.assertTrue(match)
        self.assertEqual(tmpl, "mahua_signature_special")
        self.assertIn("₹900", resp)
        self.assertTrue("₹1,600" in resp or "₹1600" in resp)
        self.assertTrue("₹2,900" in resp or "₹2900" in resp)
        self.assertEqual(imgs, ["/images/Signature%20Sondesh.jpeg"])

        # Classic & Rose price check
        resp, match, tmpl, imgs = responder.match_and_respond("how much is rose sondesh", user_id="test_rose_user", channel="whatsapp")
        self.assertTrue(match)
        self.assertEqual(tmpl, "sondesh_classic_rose")
        self.assertIn("Rose Sondesh", resp)
        self.assertIn("₹450", resp)
        self.assertIn("₹600", resp)
        self.assertIn("Collection 1 — Aamar Mishti", resp)
        self.assertIn("Collection 2 — Signature", resp)
        self.assertEqual(len(imgs), 2)

        # Rose Sondesh specifically in Collection 1
        resp_c1, match_c1, tmpl_c1, imgs_c1 = responder.match_and_respond("how much is rose sondesh in collection 1", user_id="test_rose_c1", channel="whatsapp")
        self.assertTrue(match_c1)
        self.assertEqual(tmpl_c1, "sondesh_classic_rose")
        self.assertIn("Collection 1 — Aamar Mishti (Rose Sondesh)", resp_c1)
        self.assertIn("₹450", resp_c1)
        self.assertEqual(imgs_c1, ["/images/AAMAR%20MISHTI.jpeg"])

        # Rose Sondesh specifically in Collection 2
        resp_c2, match_c2, tmpl_c2, imgs_c2 = responder.match_and_respond("how much is rose sondesh in collection 2", user_id="test_rose_c2", channel="whatsapp")
        self.assertTrue(match_c2)
        self.assertEqual(tmpl_c2, "sondesh_classic_rose")
        self.assertIn("Collection 2 — Signature A2 (Rose Sondesh)", resp_c2)
        self.assertIn("₹600", resp_c2)
        self.assertEqual(imgs_c2, ["/images/Signature%20Sondesh.jpeg"])

        # Vanilla / Mango / Cinnamon check
        resp, match, tmpl, imgs = responder.match_and_respond("real vanilla price", user_id="test_vanilla_user", channel="whatsapp")
        self.assertTrue(match)
        self.assertEqual(tmpl, "sondesh_mango_kesar")
        self.assertIn("₹550", resp)
        self.assertIn("₹650", resp)

        # Thandai / Dates / Nolen Gur check
        resp, match, tmpl, imgs = responder.match_and_respond("thandai price", user_id="test_thandai_user", channel="whatsapp")
        self.assertTrue(match)
        self.assertEqual(tmpl, "sondesh_nolen_gur")
        self.assertIn("₹600", resp)
        self.assertIn("₹700", resp)

        # Jolbhora check
        resp, match, tmpl, imgs = responder.match_and_respond("price of jolbhora", user_id="test_jol_user", channel="whatsapp")
        self.assertTrue(match)
        self.assertEqual(tmpl, "jolbhora")
        self.assertIn("pending client confirmation", resp)

        # Pack sizes (250g, 500g, 1kg)
        resp250, match250, tmpl250, _ = responder.match_and_respond("250g box options", user_id="test_250_user", channel="whatsapp")
        self.assertTrue(match250)
        self.assertEqual(tmpl250, "pack_size_250g")
        self.assertIn("Classic / Rose", resp250)
        self.assertIn("₹450", resp250)
        self.assertIn("₹900", resp250)

        resp500, match500, tmpl500, _ = responder.match_and_respond("500g box options", user_id="test_500_user", channel="whatsapp")
        self.assertTrue(match500)
        self.assertEqual(tmpl500, "pack_size_500g")
        self.assertIn("Classic / Rose", resp500)
        self.assertIn("₹800", resp500)
        self.assertTrue("₹1,600" in resp500 or "₹1600" in resp500)

        resp1k, match1k, tmpl1k, _ = responder.match_and_respond("1kg box options", user_id="test_1k_user", channel="whatsapp")
        self.assertTrue(match1k)
        self.assertEqual(tmpl1k, "pack_size_1kg")
        self.assertIn("Classic / Rose", resp1k)
        self.assertTrue("₹1,500" in resp1k or "₹1500" in resp1k)
        self.assertTrue("₹2,900" in resp1k or "₹2900" in resp1k)

    def test_weekly_meals_and_bento_order_calculation(self):
        """Verify order calculation for weekly bentos, combos, and curries."""
        from camellia.responder import responder

        # Single meal order
        resp, match, tmpl, _ = responder.match_and_respond("I want to order 2 veg bento", user_id="test_bento_u1", channel="whatsapp")
        self.assertTrue(match)
        self.assertEqual(tmpl, "order_draft_receipt")
        self.assertIn("Veg Bento Box", resp)
        self.assertIn("2 boxes", resp)
        self.assertIn("₹800", resp)

        # Multi-item meal + sweet order
        resp2, match2, tmpl2, _ = responder.match_and_respond(
            "Please deliver 1 non veg bento and 1 classic sondesh 500g",
            user_id="test_bento_u2",
            channel="whatsapp"
        )
        self.assertTrue(match2)
        self.assertEqual(tmpl2, "order_draft_receipt")
        self.assertIn("Non-Veg Bento Box", resp2)
        self.assertIn("Classic Sondesh", resp2)
        self.assertIn("₹1,500", resp2)

    def test_whatsapp_webhook_parsing(self):
        """Tests parsing of WhatsApp webhook payloads for text and interactive replies."""
        from camellia.whatsapp_adapter import whatsapp_adapter

        # 1. Text payload
        text_payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "from": "919876543210",
                            "id": "wamid.HBgL",
                            "type": "text",
                            "text": {"body": "Hi, what are your sondesh flavours?"}
                        }]
                    }
                }]
            }]
        }
        sender, msg_id, text = whatsapp_adapter.parse_webhook_message(text_payload)
        self.assertEqual(sender, "919876543210")
        self.assertEqual(msg_id, "wamid.HBgL")
        self.assertEqual(text, "Hi, what are your sondesh flavours?")

        # 2. Interactive button reply payload
        btn_payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "from": "919876543210",
                            "id": "wamid.BTN1",
                            "type": "interactive",
                            "interactive": {
                                "button_reply": {"id": "quick_btn_1", "title": "🌸 See Menu"}
                            }
                        }]
                    }
                }]
            }]
        }
        sender_btn, msg_id_btn, text_btn = whatsapp_adapter.parse_webhook_message(btn_payload)
        self.assertEqual(sender_btn, "919876543210")
        self.assertEqual(text_btn, "🌸 See Menu")

        # 3. Invalid/empty payload
        self.assertIsNone(whatsapp_adapter.parse_webhook_message({}))

    @patch("camellia.whatsapp_adapter.WhatsAppAdapter.send_message")
    def test_whatsapp_process_incoming_message(self, mock_send):
        """Tests end-to-end processing of WhatsApp message and contextual quick replies."""
        mock_send.return_value = {"mock": True, "status": "sent"}
        from camellia.whatsapp_adapter import whatsapp_adapter

        # Greeting
        result = whatsapp_adapter.process_incoming_message("919876543210", "Hello")
        self.assertEqual(result["status"], "processed")
        self.assertTrue(result["was_matched"])
        self.assertEqual(result["template_id"], "greeting")
        self.assertIn("🌸 See Menu", result["buttons"])

        # Pricing query
        pricing_res = whatsapp_adapter.process_incoming_message("919876543210", "How much is nolen gur sondesh?")
        self.assertTrue(pricing_res["was_matched"])
        self.assertIn("🛍️ Place Order", pricing_res["buttons"])

    def test_greeting_variations_do_not_repeat_immediately(self):
        """Greeting replies rotate randomly without leaving the approved response bank."""
        from camellia.session_memory import session_memory

        user_id = "greeting_variation_user"
        channel = "whatsapp"
        session_memory.clear_session(user_id, channel)
        approved = responder.qa_data["templates"][0]["responses"]

        replies = []
        for message in ["HI", "Hi", "hi", "HELLO", "Hey", "hello"]:
            reply, matched, template_id, _ = responder.match_and_respond(
                message, user_id=user_id, channel=channel
            )
            self.assertTrue(matched)
            self.assertEqual(template_id, "greeting")
            self.assertIn(reply, approved)
            replies.append(reply)

        self.assertEqual(len(replies), 6)
        self.assertTrue(all(previous != current for previous, current in zip(replies, replies[1:])))
        last_index = session_memory.get_facts(user_id, channel)["last_greeting_index"]
        self.assertEqual(replies[-1], approved[last_index])

    def test_qa_rag_escalates_unknown_questions_without_guessing(self):
        """RAG may use approved QA sources, but unknown business questions remain escalated."""
        from camellia.rag_fallback import rag_fallback_engine

        result = rag_fallback_engine.retrieve("what discount do you offer")
        self.assertFalse(result.auto_response)
        self.assertEqual(result.proposed_intent, "unknown_intent")

        response, matched, template_id, _ = responder.match_and_respond(
            "what discount do you offer",
            user_id="qa_rag_unknown_user",
            channel="whatsapp",
        )
        self.assertFalse(matched)
        self.assertTrue(template_id is None or template_id == "explicit_escalation")
        self.assertIn("notified our team", response.lower())

    def test_approved_learned_answer_is_used_by_rag(self):
        """A staff-approved learned answer is retrievable and safe to activate."""
        from camellia.rag_fallback import rag_fallback_engine

        question = "camellia approved lookup 8472"
        answer = "This is an approved Camellia answer for testing."
        knowledge_id = db.add_learned_knowledge(
            "Test approved topic", question, answer, source="test", confidence=1.0
        )
        try:
            result = rag_fallback_engine.retrieve(question)
            self.assertEqual(result.auto_response, answer)
            self.assertEqual(result.approval_status, "APPROVED")
            response, matched, template_id, _ = responder.match_and_respond(
                question, user_id="qa_rag_learned_user", channel="whatsapp"
            )
            self.assertTrue(matched)
            self.assertEqual(template_id, f"learned_{knowledge_id}")
            self.assertEqual(response, answer)
        finally:
            db.delete_learned_knowledge(knowledge_id)

    @patch("camellia.whatsapp_adapter.WhatsAppAdapter.access_token", new_callable=PropertyMock)
    def test_whatsapp_mock_outbound_and_buttons(self, mock_token):
        """Tests mock WhatsApp outbound message dispatch with image and buttons."""
        mock_token.return_value = ""
        from camellia.whatsapp_adapter import whatsapp_adapter

        send_res = whatsapp_adapter.send_message(
            recipient_phone="919876543210",
            text_body="Here is our signature menu 🌸",
            image_url="/images/signature_card.jpeg",
            buttons=["🌸 See Menu", "🛍️ Place Order"]
        )
        self.assertTrue(send_res.get("mock"))
        self.assertEqual(send_res.get("status"), "sent")
        self.assertEqual(send_res.get("recipient"), "919876543210")
        self.assertEqual(len(send_res.get("buttons", [])), 2)

    def test_whatsapp_mock_mode_never_calls_meta(self):
        """Mock mode remains safe even if placeholder credentials are present."""
        from camellia.whatsapp_adapter import whatsapp_adapter

        with patch.object(config, "WHATSAPP_MOCK_MODE", True), \
             patch.object(config, "WHATSAPP_ACCESS_TOKEN", "placeholder-token"), \
             patch.object(config, "WHATSAPP_PHONE_NUMBER_ID", "placeholder-number"), \
             patch("camellia.whatsapp_adapter.httpx.Client") as mock_client:
            result = whatsapp_adapter.send_message("919876543210", "Mock delivery test")

        self.assertTrue(result["mock"])
        self.assertEqual(result["mode"], "mock")
        mock_client.assert_not_called()

    def test_whatsapp_live_mode_requires_credentials_before_http(self):
        """Live mode fails closed when credentials are not configured."""
        from camellia.whatsapp_adapter import whatsapp_adapter

        with patch.object(config, "WHATSAPP_MOCK_MODE", False), \
             patch.object(config, "WHATSAPP_ACCESS_TOKEN", ""), \
             patch.object(config, "WHATSAPP_PHONE_NUMBER_ID", ""), \
             patch("camellia.whatsapp_adapter.httpx.Client") as mock_client:
            result = whatsapp_adapter.send_message("919876543210", "Should not send")

        self.assertEqual(result["status"], "not_configured")
        mock_client.assert_not_called()

    def test_tiered_fuzzy_matching_3letter_words(self):
        """Tests that 3-letter food terms with minor typos (e.g. gurr) match correctly while unrelated noise is rejected."""
        # 1. 3-letter typo: 'nolen gurr' -> nolen_gur template
        resp, matched, tmpl_id, _ = responder.match_and_respond("Tell me about nolen gurr sondesh", user_id="test_fuzzy_u1", channel="test")
        self.assertTrue(matched)
        self.assertIn("nolen_gur", tmpl_id)

        # 2. General typo on 4+ letter term: 'sandesh' -> menu or pricing
        resp2, matched2, tmpl_id2, _ = responder.match_and_respond("how much is sandesh?", user_id="test_fuzzy_u2", channel="test")
        self.assertTrue(matched2)

    def test_gemini_responder_provider_selection(self):
        """Tests get_active_provider logic for the Groq configuration."""
        from camellia.gemini_responder import GeminiResponder

        gr = GeminiResponder()
        orig_key = config.GROQ_API_KEY
        orig_en = config.GROQ_ENABLED

        try:
            # 1. Enabled with a key present -> groq active
            config.GROQ_ENABLED = True
            config.GROQ_API_KEY = "test-groq-key"
            self.assertEqual(gr.get_active_provider(), "groq")

            # 2. No key -> none
            config.GROQ_API_KEY = ""
            self.assertEqual(gr.get_active_provider(), "none")

            # 3. AI disabled entirely -> none, even with a key
            config.GROQ_API_KEY = "test-groq-key"
            config.GROQ_ENABLED = False
            self.assertEqual(gr.get_active_provider(), "none")

        finally:
            config.GROQ_API_KEY = orig_key
            config.GROQ_ENABLED = orig_en

    @patch("camellia.gemini_responder.httpx.Client")
    def test_gemini_responder_groq_query(self, mock_httpx_cls):
        """Tests that querying Groq formats the OpenAI-compatible chat payload correctly."""
        from camellia.gemini_responder import GeminiResponder

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "Our Nolen Gur Sondesh is crafted with fresh chhena and pure date palm jaggery. A box of 6 is ₹450."
                    }
                }
            ]
        }
        mock_post = MagicMock(return_value=mock_resp)
        mock_httpx_cls.return_value.__enter__.return_value.post = mock_post

        gr = GeminiResponder()
        orig_key = config.GROQ_API_KEY
        orig_model = config.GROQ_MODEL
        orig_en = config.GROQ_ENABLED

        try:
            config.GROQ_ENABLED = True
            config.GROQ_API_KEY = "gsk-mock-key-12345"
            config.GROQ_MODEL = "openai/gpt-oss-20b"

            ans, grounded, reason = gr.generate_response("Tell me about Nolen Gur Sondesh")
            self.assertTrue(grounded)
            self.assertEqual(reason, "groq_rag")
            self.assertIn("Nolen Gur Sondesh", ans)

            # Verify the HTTP payload sent to Groq
            mock_post.assert_called_once()
            called_args, called_kwargs = mock_post.call_args
            self.assertEqual(called_args[0], "https://api.groq.com/openai/v1/chat/completions")

            payload = called_kwargs.get("json", {})
            self.assertEqual(payload.get("model"), "openai/gpt-oss-20b")
            self.assertIn("messages", payload)

            headers = called_kwargs.get("headers", {})
            self.assertEqual(headers.get("Authorization"), "Bearer gsk-mock-key-12345")

        finally:
            config.GROQ_API_KEY = orig_key
            config.GROQ_MODEL = orig_model
            config.GROQ_ENABLED = orig_en

    @patch("camellia.gemini_responder.httpx.Client")
    def test_gemini_responder_groq_out_of_context(self, mock_httpx_cls):
        """Tests that OUT_OF_CONTEXT from Groq is safely caught and triggers escalation fallback."""
        from camellia.gemini_responder import GeminiResponder

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "OUT_OF_CONTEXT"
                    }
                }
            ]
        }
        mock_httpx_cls.return_value.__enter__.return_value.post.return_value = mock_resp

        gr = GeminiResponder()
        orig_key = config.GROQ_API_KEY
        orig_en = config.GROQ_ENABLED

        try:
            config.GROQ_ENABLED = True
            config.GROQ_API_KEY = "gsk-mock-key-12345"

            ans, grounded, reason = gr.generate_response("Can you help me write a Python script for machine learning?")
            self.assertFalse(grounded)
            self.assertEqual(reason, "out_of_context")
            self.assertTrue("team" in ans.lower() or "camellia" in ans.lower())

        finally:
            config.GROQ_API_KEY = orig_key
            config.GROQ_ENABLED = orig_en

    @patch("camellia.gemini_responder.httpx.Client")
    def test_gemini_responder_refine_template_groq(self, mock_httpx_cls):
        """Tests template refinement using Groq."""
        from camellia.gemini_responder import GeminiResponder

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "Hello Rahul! 🌸 Here are the pricing details for our Sondesh:\n• Box of 6: ₹450\n• Box of 9: ₹600"
                    }
                }
            ]
        }
        mock_httpx_cls.return_value.__enter__.return_value.post.return_value = mock_resp

        gr = GeminiResponder()
        orig_key = config.GROQ_API_KEY
        orig_en = config.GROQ_ENABLED

        try:
            config.GROQ_ENABLED = True
            config.GROQ_API_KEY = "gsk-mock-key-12345"

            refined, was_refined = gr.refine_template_response(
                template_id="sondesh_pricing",
                template_text="Box of 6 is 450, Box of 9 is 600",
                user_message="what are prices?",
                user_context="Customer Name: Rahul"
            )
            self.assertTrue(was_refined)
            self.assertIn("Rahul", refined)
            self.assertIn("₹450", refined)

        finally:
            config.GROQ_API_KEY = orig_key
            config.GROQ_ENABLED = orig_en

    def test_human_simulation_scenarios_and_new_qa(self):
        """Test the real-world edge case scenarios identified by human simulation agents."""
        from camellia.session_memory import session_memory
        responder.reload_templates()

        # Scenario 1: Compound name introduction with gifting/difference inquiry (Confirmed collection difference)
        uid1 = "sim_user_priya_1"
        session_memory.clear_session(uid1, "whatsapp")
        msg1 = "Hi! I'm Priya. I want to gift sweet boxes for my parents' anniversary. What is the difference between Collection 1 and Collection 2?"
        resp1, matched1, tmpl1, imgs1 = responder.match_and_respond(msg1, user_id=uid1, channel="whatsapp")
        self.assertTrue(matched1)
        self.assertEqual(tmpl1, "GUARD_collection_differentiation")
        self.assertIn("Aamar Mishti", resp1)
        self.assertIn("Signature Sondesh", resp1)
        facts1 = session_memory.get_facts(uid1, "whatsapp")
        self.assertEqual(facts1.get("name"), "Priya")

        # Scenario 2: Customization and assorted box inquiries (Escalated per custom box policy)
        uid2 = "sim_user_custom_2"
        session_memory.clear_session(uid2, "whatsapp")
        msg2 = "Can I customize the box with a mix of rose and classic norom pak sondesh?"
        resp2, matched2, tmpl2, imgs2 = responder.match_and_respond(msg2, user_id=uid2, channel="whatsapp")
        self.assertFalse(matched2)
        self.assertEqual(tmpl2, "GUARD_customization_flavour_mixing")
        self.assertIn("+91 87965 51990", resp2)

        # Scenario 3: Dietary, Halal & Cooking oil inquiries (Confirmed: Mutton is Halal, Mustard oil from farmer)
        uid3 = "sim_user_dietary_3"
        session_memory.clear_session(uid3, "whatsapp")
        msg3 = "Is your food halal certified and what oil do you cook your curries in?"
        resp3, matched3, tmpl3, imgs3 = responder.match_and_respond(msg3, user_id=uid3, channel="whatsapp")
        self.assertTrue(matched3)
        self.assertEqual(tmpl3, "GUARD_halal_ingredients_oil")
        self.assertIn("Halal", resp3)
        self.assertIn("mustard oil", resp3)

        # Scenario 4: Payment methods and COD clarification (Confirmed: UPI/bank transfer only, No COD)
        uid4 = "sim_user_payment_4"
        session_memory.clear_session(uid4, "whatsapp")
        msg4 = "What payment methods do you accept, do you take COD?"
        resp4, matched4, tmpl4, imgs4 = responder.match_and_respond(msg4, user_id=uid4, channel="whatsapp")
        self.assertTrue(matched4)
        self.assertEqual(tmpl4, "GUARD_payment_method")
        self.assertIn("UPI / bank transfer only", resp4)
        self.assertIn("No COD", resp4)

        # Scenario 5: Transaction failure escalation
        uid5 = "sim_user_failed_tx_5"
        session_memory.clear_session(uid5, "whatsapp")
        msg5 = "My UPI transaction failed and money got deducted, please check"
        resp5, matched5, tmpl5, imgs5 = responder.match_and_respond(msg5, user_id=uid5, channel="whatsapp")
        self.assertFalse(matched5)
        self.assertEqual(tmpl5, "explicit_escalation")
        self.assertIn("+91 87965 51990", resp5)

    def test_phase_b_savoury_food_intro_and_image(self):
        """Phase B: Test savoury_food_intro returns 3 ways to order with Gurgaon only restriction, No minimum order, and attaches weekly menu image."""
        from camellia.session_memory import session_memory
        session_memory.clear_session("test_savoury_intro_user", "whatsapp")
        msg = "What is your lunch menu and savoury food options?"
        resp, matched, tmpl, imgs = responder.match_and_respond(msg, user_id="test_savoury_intro_user", channel="whatsapp")
        self.assertTrue(matched)
        self.assertEqual(tmpl, "savoury_food_intro")
        self.assertIn("3 ways to order", resp)
        self.assertIn("No minimum order", resp)
        self.assertIn("Bengali Bento", resp)
        self.assertIn("Everyday Meal Subscription (Gurgaon only)", resp)
        self.assertIn("All prices include GST, packaging, and portal charges", resp)
        self.assertIn("Porter", resp)
        # Image attachment must be weekly menu card
        self.assertIsNotNone(imgs)
        self.assertTrue("Thursday" in imgs or "Friday" in imgs)
        # First savoury message gets savoury cross-sell nudge
        self.assertIn("We also handcraft Bengali mishti", resp)
        self.assertIn("subtle natural flavours", resp)

    def test_phase_b_bento_details_escalates_safely(self):
        """Phase B: Test bento_details has response: null and escalate: true and safely escalates to human team."""
        from camellia.session_memory import session_memory
        session_memory.clear_session("test_bento_user", "whatsapp")
        msg = "What are the bento meal box details and prices?"
        resp, matched, tmpl, imgs = responder.match_and_respond(msg, user_id="test_bento_user", channel="whatsapp")
        self.assertFalse(matched)
        self.assertEqual(tmpl, "bento_details")
        self.assertIn("+91 87965 51990", resp)

    def test_phase_b_subscription_details_and_booking_steps_sequencing(self):
        """Phase B: Test subscription_details (Gurgaon only) followed by 'how do I book' sequences to 4-step subscription_booking_steps."""
        from camellia.session_memory import session_memory
        uid = "test_sub_seq_user"
        session_memory.clear_session(uid, "whatsapp")

        # Step 1: Inquire about subscription
        msg1 = "Tell me about your everyday meal subscription plans"
        resp1, matched1, tmpl1, _ = responder.match_and_respond(msg1, user_id=uid, channel="whatsapp")
        self.assertTrue(matched1)
        self.assertEqual(tmpl1, "subscription_details")
        self.assertIn("Gurgaon only", resp1)
        self.assertIn("₹1,500", resp1)
        self.assertIn("₹1,750", resp1)
        self.assertIn("₹2,000", resp1)
        self.assertIn("Polao / Luchi", resp1)
        self.assertIn("11th meal is completely FREE", resp1)
        # Verify state flag set
        facts1 = session_memory.get_facts(uid, "whatsapp")
        self.assertTrue(facts1.get("awaiting_subscription_booking"))

        # Step 2: Follow-up asking how to book
        msg2 = "How do I book this subscription?"
        resp2, matched2, tmpl2, _ = responder.match_and_respond(msg2, user_id=uid, channel="whatsapp")
        self.assertTrue(matched2)
        self.assertEqual(tmpl2, "subscription_booking_steps")
        self.assertIn("Next Steps", resp2)
        self.assertIn("Choose your plan", resp2)
        self.assertIn("Pay", resp2)
        self.assertIn("Tell us your preferred dates", resp2)
        self.assertIn("Done — we deliver on those dates without you following up", resp2)
        self.assertIn("5 hours advance notice", resp2)
        # Verify state flag cleared
        facts2 = session_memory.get_facts(uid, "whatsapp")
        self.assertFalse(facts2.get("awaiting_subscription_booking"))

    def test_phase_b_alacarte_prebooking_steps(self):
        """Phase B: Test alacarte_prebooking_steps returns 5-point capture list."""
        from camellia.session_memory import session_memory
        session_memory.clear_session("test_alacarte_user", "whatsapp")
        msg = "How to order item from menu and pre-book savoury dishes?"
        resp, matched, tmpl, _ = responder.match_and_respond(msg, user_id="test_alacarte_user", channel="whatsapp")
        self.assertTrue(matched)
        self.assertEqual(tmpl, "alacarte_prebooking_steps")
        self.assertIn("Items & Quantity", resp)
        self.assertIn("Delivery Address", resp)
        self.assertIn("Receiver's Phone Number", resp)
        self.assertIn("Preferred Delivery Slot / Day", resp)
        self.assertIn("Google Location Pin", resp)
        self.assertIn("8796551990", resp)

    def test_phase_b_sondesh_dispatch_time(self):
        """Phase B: Test sondesh_dispatch_time returns standard dispatch timings."""
        from camellia.session_memory import session_memory
        session_memory.clear_session("test_dispatch_user", "whatsapp")
        msg = "How long to dispatch Sondesh and what is your prep time?"
        resp, matched, tmpl, _ = responder.match_and_respond(msg, user_id="test_dispatch_user", channel="whatsapp")
        self.assertTrue(matched)
        self.assertEqual(tmpl, "sondesh_dispatch_time")
        self.assertIn("15 minutes", resp)
        self.assertIn("30 minutes", resp)
        self.assertIn("an hour", resp)
        self.assertIn("time of day", resp)
        self.assertIn("10:00 AM – 7:30 PM", resp)

    def test_phase_b_sondesh_price_detailed_sourcing_and_images(self):
        """Phase B: Test sondesh_price_detailed returns client artisanal paragraph and images."""
        from camellia.session_memory import session_memory
        session_memory.clear_session("test_price_det_user", "whatsapp")
        msg = "What is the price of Sondesh?"
        resp, matched, tmpl, imgs = responder.match_and_respond(msg, user_id="test_price_det_user", channel="whatsapp")
        self.assertTrue(matched)
        self.assertEqual(tmpl, "sondesh_price_detailed")
        self.assertIn("₹450", resp)
        self.assertIn("₹900", resp)
        self.assertIn("An all-women team", resp)
        self.assertIn("partially mechanised, made in-house", resp)
        self.assertIn("pure desi A2 cow milk", resp)
        self.assertIsNotNone(imgs)

    def test_phase_b_savoury_crosssell_nudge_fires_once_per_session(self):
        """Phase B: Test savoury cross-sell nudge fires on first savoury query and not on subsequent queries."""
        from camellia.session_memory import session_memory
        uid = "test_crosssell_user"
        session_memory.clear_session(uid, "whatsapp")

        # Turn 1: Savoury query -> cross-sell nudge attached
        msg1 = "Show me the lunch options"
        resp1, matched1, tmpl1, _ = responder.match_and_respond(msg1, user_id=uid, channel="whatsapp")
        self.assertTrue(matched1)
        self.assertIn("We also handcraft Bengali mishti", resp1)
        self.assertIn("subtle natural flavours", resp1)
        self.assertTrue(session_memory.get_facts(uid, "whatsapp").get("savoury_crosssell_sent"))

        # Turn 2: Second savoury query -> NO duplicate cross-sell nudge
        msg2 = "How to order item from menu?"
        resp2, matched2, tmpl2, _ = responder.match_and_respond(msg2, user_id=uid, channel="whatsapp")
        self.assertTrue(matched2)
        self.assertEqual(tmpl2, "alacarte_prebooking_steps")
        self.assertNotIn("P.S.", resp2)
        self.assertNotIn("We also make & send Bengali mishti", resp2)

        # Direct inquiry for cross-sell template
        msg3 = "Do you have sweets along with meal?"
        resp3, matched3, tmpl3, _ = responder.match_and_respond(msg3, user_id=uid, channel="whatsapp")
        self.assertTrue(matched3)
        self.assertEqual(tmpl3, "savoury_crosssell_sondesh")
        self.assertIn("We also make & send Bengali mishti", resp3)
        self.assertIn("subtle natural flavours", resp3)

    def test_phase_e_sondesh_crosssell_savoury_nudge_and_template(self):
        """Phase E: Test sondesh -> savoury cross-sell nudge, once-per-session cap, and menu image attachment."""
        from camellia.session_memory import session_memory
        uid = "test_sondesh_crosssell_user"
        session_memory.clear_session(uid, "whatsapp")

        # Turn 1: Sondesh query -> should append reverse cross-sell nudge
        msg1 = "What is the sondesh price?"
        resp1, matched1, tmpl1, img1 = responder.match_and_respond(msg1, user_id=uid, channel="whatsapp")
        self.assertTrue(matched1)
        self.assertEqual(tmpl1, "sondesh_price_detailed")
        self.assertIn("We also cook fresh Bengali meals for Delhi NCR", resp1)
        self.assertTrue(session_memory.get_facts(uid, "whatsapp").get("sondesh_crosssell_sent"))

        # Turn 2: Second sondesh query -> NO duplicate cross-sell nudge
        msg2 = "How long to dispatch sondesh?"
        resp2, matched2, tmpl2, _ = responder.match_and_respond(msg2, user_id=uid, channel="whatsapp")
        self.assertTrue(matched2)
        self.assertEqual(tmpl2, "sondesh_dispatch_time")
        self.assertNotIn("We also cook fresh Bengali meals", resp2)

        # Direct inquiry for sondesh_crosssell_savoury template
        msg3 = "Do you also cook meals?"
        resp3, matched3, tmpl3, img3 = responder.match_and_respond(msg3, user_id=uid, channel="whatsapp")
        self.assertTrue(matched3)
        self.assertEqual(tmpl3, "sondesh_crosssell_savoury")
        self.assertIn("We also prepare and deliver freshly cooked Bengali meals", resp3)
        self.assertIsNotNone(img3)

    def test_phase_e_savoury_best_time_to_book(self):
        """Phase E: Test savoury_best_time_to_book template response."""
        msg = "What is the best time to order food?"
        resp, matched, tmpl, _ = responder.match_and_respond(msg)
        self.assertTrue(matched)
        self.assertEqual(tmpl, "savoury_best_time_to_book")
        self.assertIn("We always recommend pre-ordering", resp)
        self.assertIn("avoid disappointment", resp)

    def test_phase_e_savoury_delivery_time(self):
        """Phase E: Test savoury_delivery_time template response with 45 min figure."""
        msg = "What is the delivery time for food?"
        resp, matched, tmpl, _ = responder.match_and_respond(msg)
        self.assertTrue(matched)
        self.assertEqual(tmpl, "savoury_delivery_time")
        self.assertIn("within 45 minutes", resp)
        self.assertIn("8796551990", resp)

    def test_phase_e_kolkata_expansion_info(self):
        """Phase E: Test kolkata_expansion_info template response."""
        msg = "Are you moving to Kolkata?"
        resp, matched, tmpl, _ = responder.match_and_respond(msg)
        self.assertTrue(matched)
        self.assertEqual(tmpl, "kolkata_expansion_info")
        self.assertIn("expanding our operations to Kolkata!", resp)
        self.assertIn("Mr. Guru Dayal Singh", resp)
        self.assertIn("nothing changes for our guests in Delhi NCR", resp)

    def test_phase_e_manager_contact_email_updated(self):
        """Phase E: Test GUARD_staff_names_manager_contact updated with real email / conflict status."""
        msg = "What is the kitchen manager email address?"
        resp, matched, tmpl, _ = responder.match_and_respond(msg)
        self.assertTrue(matched)
        self.assertEqual(tmpl, "GUARD_staff_names_manager_contact")
        self.assertIn("CONFLICTED", resp)
        self.assertIn("Mr. Guru Dayal Singh", resp)

    def test_phase_c_subscription_booking_nudge(self):
        """Phase C: Test subscription_details response explicitly ends with 'Would you like to book?'."""
        from camellia.session_memory import session_memory
        session_memory.clear_session("test_sub_nudge_user", "whatsapp")
        msg = "Tell me about your everyday meal subscription"
        resp, matched, tmpl, _ = responder.match_and_respond(msg, user_id="test_sub_nudge_user", channel="whatsapp")
        self.assertTrue(matched)
        self.assertEqual(tmpl, "subscription_details")
        self.assertIn("Would you like to book?", resp)
        # Verify it's at the end of the subscription response body
        self.assertTrue(resp.strip().endswith("Would you like to book?") or "*P.S.*" in resp)

    def test_phase_c_first_time_vs_returning_customer_detection(self):
        """Phase C: Test first-time vs returning customer detection on savoury_food_intro."""
        from camellia.session_memory import session_memory
        from camellia.database import db

        first_time_uid = "test_phase_c_first_time_user"
        session_memory.clear_session(first_time_uid, "whatsapp")

        # Path 1: First-time customer (No turns in session memory, No records in DB)
        msg1 = "What is your lunch menu and savoury food options?"
        resp1, matched1, tmpl1, _ = responder.match_and_respond(msg1, user_id=first_time_uid, channel="whatsapp")
        self.assertTrue(matched1)
        self.assertEqual(tmpl1, "savoury_food_intro")
        self.assertIn("If this is your first Camellia meal and the menu feels unfamiliar, I'll be happy to help you choose!", resp1)

        # Path 2: Same user in subsequent turn (prior turns exist in session memory)
        msg2 = "Show me the savoury food options again"
        resp2, matched2, tmpl2, _ = responder.match_and_respond(msg2, user_id=first_time_uid, channel="whatsapp")
        self.assertTrue(matched2)
        self.assertEqual(tmpl2, "savoury_food_intro")
        self.assertNotIn("If this is your first Camellia meal and the menu feels unfamiliar, I'll be happy to help you choose!", resp2)

        # Path 3: Returning customer from a previous session (in-memory cleared, but record exists in DB)
        returning_db_uid = "test_phase_c_returning_db_user"
        session_memory.clear_session(returning_db_uid, "whatsapp")
        # Log a prior conversation into DB
        db.log_conversation(returning_db_uid, "whatsapp", "Hello yesterday", "Hi there", True, "greeting")
        self.assertTrue(session_memory.has_prior_history(returning_db_uid, "whatsapp"))

        msg3 = "What is your savoury lunch menu and food options?"
        resp3, matched3, tmpl3, _ = responder.match_and_respond(msg3, user_id=returning_db_uid, channel="whatsapp")
        self.assertTrue(matched3)
        self.assertEqual(tmpl3, "savoury_food_intro")
        # Since history exists in DB, first-time greeting must NOT appear
        self.assertNotIn("If this is your first Camellia meal and the menu feels unfamiliar, I'll be happy to help you choose!", resp3)

    def test_phase_c_24h_hybrid_expiry_model(self):
        """Phase C: Test 24-hour hybrid expiry model (transient facts reset after 24h, name/history retained)."""
        import time
        from camellia.session_memory import session_memory

        expiry_uid = "test_24h_expiry_user"
        session_memory.clear_session(expiry_uid, "whatsapp")

        # Step 1: User introduces name and creates transient state (order draft, flags)
        sess = session_memory.get_session(expiry_uid, "whatsapp")
        session_memory.extract_and_update_facts(expiry_uid, "whatsapp", "My name is Ananya")
        sess["facts"]["order_draft"] = {"flavour": "jolbhora", "collection": "signature", "size": "500g", "quantity": 2}
        sess["facts"]["cart"] = [{"item": "Jolbhora 500g"}]
        sess["facts"]["savoury_crosssell_sent"] = True
        sess["facts"]["awaiting_subscription_booking"] = True
        sess["facts"]["image_card_sent"] = True
        sess["facts"]["last_greeting_index"] = 4

        self.assertEqual(sess["facts"]["name"], "Ananya")
        self.assertEqual(sess["facts"]["order_draft"]["flavour"], "jolbhora")
        self.assertTrue(sess["facts"]["savoury_crosssell_sent"])

        # Step 2: Return within 24h (e.g. 23 hours / 82800 seconds later)
        now = time.time()
        sess["last_updated"] = now - 82800
        sess_23h = session_memory.get_session(expiry_uid, "whatsapp")

        # Verify facts & order draft retained
        self.assertEqual(sess_23h["facts"]["name"], "Ananya")
        self.assertEqual(sess_23h["facts"]["order_draft"]["flavour"], "jolbhora")
        self.assertEqual(len(sess_23h["facts"]["cart"]), 1)
        self.assertTrue(sess_23h["facts"]["savoury_crosssell_sent"])
        self.assertEqual(sess_23h["facts"]["last_greeting_index"], 4)

        # Step 3: Return after >24h (e.g. 25 hours / 90000 seconds later)
        sess["last_updated"] = now - 90000
        sess_25h = session_memory.get_session(expiry_uid, "whatsapp")

        # Verify transient facts reset while customer name is retained
        self.assertEqual(sess_25h["facts"]["name"], "Ananya")
        self.assertIsNone(sess_25h["facts"]["order_draft"]["flavour"])
        self.assertEqual(len(sess_25h["facts"]["cart"]), 0)
        self.assertFalse(sess_25h["facts"]["savoury_crosssell_sent"])
        self.assertFalse(sess_25h["facts"]["awaiting_subscription_booking"])
        self.assertFalse(sess_25h["facts"]["image_card_sent"])
        self.assertIsNone(sess_25h["facts"]["last_greeting_index"])

    def test_phase_d_ad_gate_savoury_subscription_regression(self):
        """Phase D: Confirm Ad-Gate blocks organic Instagram DMs and allows Ad-Click / WhatsApp DMs for savoury/subscription templates."""
        savoury_queries = [
            "What is your savoury lunch menu?",
            "Do you have weekly veg menu?",
            "Tell me about your everyday meal subscription",
            "How do I book subscription?",
        ]

        for q in savoury_queries:
            # 1. Organic Instagram DM -> BLOCKED by Ad-Gate (left for manual reply)
            resp_org, matched_org, tmpl_org, _ = responder.match_and_respond(q, user_id="ig_org_user", channel="instagram", referral_source=None)
            self.assertFalse(matched_org, f"Organic IG DM should be blocked for '{q}'")
            self.assertEqual(tmpl_org, "ad_gate_blocked")
            self.assertEqual(resp_org, "")

            # 2. Ad-Click Instagram DM (referral_source="ADS") -> ALLOWED

            resp_ad, matched_ad, tmpl_ad, _ = responder.match_and_respond(q, user_id="ig_ad_user", channel="instagram", referral_source="ADS")
            self.assertTrue(matched_ad, f"Ad IG DM should pass for '{q}'")
            self.assertIsNotNone(tmpl_ad)

            # 3. WhatsApp DM -> ALLOWED
            resp_wa, matched_wa, tmpl_wa, _ = responder.match_and_respond(q, user_id="wa_user", channel="whatsapp")
            self.assertTrue(matched_wa, f"WhatsApp DM should pass for '{q}'")
            self.assertIsNotNone(tmpl_wa)


class TestAnswerAccuracy(unittest.TestCase):
    """
    Regressions found by auditing 71 real customer phrasings against the
    category each one belongs to. Each case here was once answered with the
    wrong thing, or handed to a human when the bot had the answer.
    """

    def _tmpl(self, msg, uid=None):
        from camellia.session_memory import session_memory
        uid = uid or f"acc_{abs(hash(msg))}"
        session_memory.clear_session(uid, "whatsapp")
        _, matched, tmpl, _ = responder.match_and_respond(msg, user_id=uid, channel="whatsapp")
        return matched, tmpl

    def test_condition_statements_are_not_taken_as_names(self):
        """"I am diabetic" once stored the name "Diabetic" and greeted them by it."""
        from camellia.session_memory import session_memory
        for msg in ("i am vegan", "i am a vegan", "i am diabetic",
                    "i am allergic to nuts", "i am lactose intolerant", "im pregnant"):
            session_memory.clear_session("nm", "whatsapp")
            facts = session_memory.extract_and_update_facts("nm", "whatsapp", msg)
            self.assertIsNone(facts.get("name"), f"{msg!r} was stored as a name")

    def test_real_introductions_still_capture_the_name(self):
        from camellia.session_memory import session_memory
        for msg, expected in (("i am Priya", "Priya"), ("my name is Sourav Das", "Sourav Das"),
                              ("Hi, I am Ananya", "Ananya"), ("call me Rahul", "Rahul")):
            session_memory.clear_session("nm2", "whatsapp")
            facts = session_memory.extract_and_update_facts("nm2", "whatsapp", msg)
            self.assertEqual(facts.get("name"), expected)

    def test_dietary_questions_get_a_dietary_answer(self):
        for msg in ("is it vegan", "i am vegan", "is it eggless", "is it vegetarian",
                    "is it gluten free", "i am lactose intolerant", "do you have vegan options"):
            matched, tmpl = self._tmpl(msg)
            self.assertTrue(matched, f"{msg!r} fell through to a human")
            self.assertEqual(tmpl, "GUARD_dietary_suitability", f"{msg!r} -> {tmpl}")

    def test_dietary_answer_is_factually_correct(self):
        """Sondesh is milk-based, so it is vegetarian but explicitly not vegan."""
        from camellia.session_memory import session_memory
        session_memory.clear_session("diet", "whatsapp")
        text, _, _, _ = responder.match_and_respond("is it vegan", user_id="diet", channel="whatsapp")
        lowered = text.lower()
        self.assertIn("not suitable for a vegan", lowered,
                      "Must state plainly that dairy-based Sondesh is not vegan")
        self.assertIn("milk", lowered, "Should say why — it is milk-based")
        self.assertIn("vegetarian", lowered, "Should still confirm it is vegetarian")

    def test_price_questions_never_answer_with_packaging(self):
        """A bare 'box' trigger meant price questions got the eco-packaging answer."""
        for msg in ("what does a box cost", "what is the cheapest box",
                    "how much for a box", "price of 500g box"):
            matched, tmpl = self._tmpl(msg)
            self.assertTrue(matched, f"{msg!r} fell through to a human")
            self.assertNotEqual(tmpl, "packaging", f"{msg!r} answered with packaging info")

    def test_packaging_questions_still_reach_packaging(self):
        for msg in ("packaging", "is it eco friendly", "do you use plastic", "how is it packed"):
            matched, tmpl = self._tmpl(msg)
            self.assertTrue(matched, f"{msg!r} fell through")
            self.assertEqual(tmpl, "packaging", f"{msg!r} -> {tmpl}")

    def test_asking_for_kitchen_address_gets_the_address(self):
        for msg in ("what is your address", "whats your address", "where are you located",
                    "where is your kitchen", "kitchen location"):
            matched, tmpl = self._tmpl(msg)
            self.assertTrue(matched, f"{msg!r} fell through to a human")
            self.assertEqual(tmpl, "GUARD_pickup_address_hours", f"{msg!r} -> {tmpl}")

    def test_customer_giving_their_address_is_not_told_the_kitchen_address(self):
        """"my address is ..." is someone ordering, not asking where we are."""
        for msg in ("my address is 123 sector 45", "here is my address, sector 45 gurgaon"):
            _, tmpl = self._tmpl(msg)
            self.assertNotEqual(tmpl, "GUARD_pickup_address_hours", f"{msg!r} -> {tmpl}")

    def test_qualified_weekly_menu_beats_the_generic_intro(self):
        matched, tmpl = self._tmpl("Show me the veg menu this week")
        self.assertTrue(matched)
        self.assertEqual(tmpl, "weekly_menu_veg")
        matched, tmpl = self._tmpl("show me the non veg menu this week")
        self.assertTrue(matched)
        self.assertEqual(tmpl, "weekly_menu_nonveg")

    def test_previously_unanswered_phrasings_now_answer(self):
        """Each of these used to reach a human despite the bot knowing the answer."""
        for msg in ("how much for a kg", "kitna hai sondesh", "most expensive sondesh",
                    "which areas do you cover", "can i get it today",
                    "do you accept cards", "what curries do you have"):
            matched, tmpl = self._tmpl(msg)
            self.assertTrue(matched, f"{msg!r} still falls through to a human")
            self.assertIsNotNone(tmpl)


class TestClarifyBeforeEscalating(unittest.TestCase):
    """
    A message too vague to place gets one clarifying question rather than the
    "I've notified our team" notice, which in that situation was not even true.
    """

    def _reply(self, uid, msg):
        return responder.match_and_respond(msg, user_id=uid, channel="whatsapp")

    def test_vague_message_asks_what_they_meant(self):
        from camellia.session_memory import session_memory
        for msg in ("india", "is it good", "anything else", "what about that one", "ok tell me"):
            uid = f"vague_{abs(hash(msg))}"
            session_memory.clear_session(uid, "whatsapp")
            text, matched, tmpl, _ = self._reply(uid, msg)
            self.assertEqual(tmpl, "clarify_request", f"{msg!r} -> {tmpl}")
            self.assertTrue(matched, "A clarifying question is not an escalation")
            self.assertNotIn("notified our team", text.lower())

    def test_specific_unanswerable_message_still_reaches_a_human(self):
        """Detail we cannot answer is a real gap — a menu of options would insult it."""
        from camellia.session_memory import session_memory
        for msg in ("my order is late and wrong", "i need a gst invoice"):
            uid = f"spec_{abs(hash(msg))}"
            session_memory.clear_session(uid, "whatsapp")
            _, matched, tmpl, _ = self._reply(uid, msg)
            self.assertFalse(matched, f"{msg!r} should still escalate")
            self.assertNotEqual(tmpl, "clarify_request")

    def test_clarifies_only_once_then_escalates(self):
        from camellia.session_memory import session_memory
        session_memory.clear_session("twice", "whatsapp")
        _, _, first, _ = self._reply("twice", "india")
        self.assertEqual(first, "clarify_request")
        _, matched, second, _ = self._reply("twice", "blah")
        self.assertNotEqual(second, "clarify_request", "Asked to clarify twice in one chat")
        self.assertFalse(matched)

    def test_off_domain_questions_still_escalate(self):
        """
        Out-of-syllabus questions name something definite, so they keep going to
        a human rather than being redirected to a menu of our own options.
        """
        from camellia.session_memory import session_memory
        for msg in ("What is the capital of France?", "Do you serve steamed bhapa pitha?"):
            uid = f"off_{abs(hash(msg))}"
            session_memory.clear_session(uid, "whatsapp")
            _, matched, tmpl, _ = self._reply(uid, msg)
            self.assertFalse(matched, f"{msg!r} should escalate")
            self.assertNotEqual(tmpl, "clarify_request")

    def test_clarifier_buttons_match_the_options_offered(self):
        from camellia.whatsapp_adapter import whatsapp_adapter
        buttons = whatsapp_adapter._get_contextual_buttons("clarify_request", True)
        self.assertEqual(buttons, ["🌸 Sondesh", "🍱 Meals", "🚚 Delivery"])


class TestInteractiveMenuFlow(unittest.TestCase):
    """
    A menu question shows the card with a short teaser and invites a choice,
    then each choice leads somewhere real rather than to a human handover.
    """

    def _reply(self, uid, msg):
        from camellia.session_memory import session_memory  # noqa: F401
        return responder.match_and_respond(msg, user_id=uid, channel="whatsapp")

    def test_menu_reply_is_short_and_offers_a_choice(self):
        from camellia.session_memory import session_memory
        for msg in ("menu", "what do you have"):
            session_memory.clear_session(f"menu_{msg}", "whatsapp")
            text, matched, tmpl, images = self._reply(f"menu_{msg}", msg)
            self.assertTrue(matched)
            self.assertIn(tmpl, ("know_more", "product_overview"))
            # The point of the change: a teaser, not a wall of text.
            self.assertLess(len(text), 500, f"'{msg}' reply is still long: {len(text)} chars")
            self.assertTrue(images, "Menu reply should still send the photo card")
            self.assertRegex(text, r"(?i)sondesh")
            self.assertRegex(text, r"(?i)meal")

    def test_menu_buttons_continue_the_choice_offered(self):
        from camellia.whatsapp_adapter import whatsapp_adapter
        for tmpl in ("know_more", "product_overview"):
            buttons = whatsapp_adapter._get_contextual_buttons(tmpl, True)
            self.assertNotIn("🌸 See Menu", buttons, "Offers the menu to someone who just got it")
            self.assertIn("🌸 Sondesh", buttons)
            self.assertIn("🍱 Meals", buttons)

    def test_every_menu_button_leads_somewhere_real(self):
        from camellia.session_memory import session_memory
        for label in ("🌸 Sondesh", "🍱 Meals"):
            session_memory.clear_session(f"btn_{label}", "whatsapp")
            text, matched, tmpl, _ = self._reply(f"btn_{label}", label)
            self.assertTrue(matched, f"Button '{label}' dead-ends into a handover")
            self.assertIsNotNone(tmpl)

    def test_meal_price_question_asks_which_menu_instead_of_escalating(self):
        """
        Bento tier pricing is withheld by the bento_details guard, so a bare
        "how much?" about meals must ask which menu rather than hand off.
        """
        from camellia.session_memory import session_memory
        session_memory.clear_session("meal_price_user", "whatsapp")
        self._reply("meal_price_user", "🍱 Meals")
        text, matched, tmpl, _ = self._reply("meal_price_user", "how much?")
        self.assertTrue(matched, "Meal price question fell through to a human handover")
        self.assertEqual(tmpl, "context_meal_price_followup")
        self.assertNotIn("notified our team", text.lower())
        self.assertRegex(text, r"(?i)veg")

    def test_one_word_veg_and_nonveg_answers_resolve(self):
        """The bot asks "Veg or Non-Veg?", so a one-word reply has to work."""
        from camellia.session_memory import session_memory
        for answer, expected in (("veg", "weekly_menu_veg"), ("non veg", "weekly_menu_nonveg")):
            uid = f"choice_{answer}"
            session_memory.clear_session(uid, "whatsapp")
            self._reply(uid, "🍱 Meals")
            self._reply(uid, "how much?")
            text, matched, tmpl, _ = self._reply(uid, answer)
            self.assertTrue(matched, f"One-word '{answer}' dead-ended")
            self.assertEqual(tmpl, expected, f"'{answer}' routed to {tmpl}")
            self.assertIn("₹", text, f"'{answer}' menu came back without prices")

    def test_bento_pricing_guard_still_holds(self):
        """The pricing-conflict guard must not be weakened by the meals flow."""
        from camellia.session_memory import session_memory
        for msg in ("bento details", "bento prices", "bento tiers"):
            session_memory.clear_session(f"guard_{msg}", "whatsapp")
            _, matched, tmpl, _ = self._reply(f"guard_{msg}", msg)
            self.assertFalse(matched, f"'{msg}' bypassed the bento pricing guard")
            self.assertEqual(tmpl, "bento_details")

    def test_sondesh_side_still_answers_bare_price(self):
        from camellia.session_memory import session_memory
        session_memory.clear_session("sondesh_user", "whatsapp")
        self._reply("sondesh_user", "🌸 Sondesh")
        text, matched, tmpl, _ = self._reply("sondesh_user", "how much?")
        self.assertTrue(matched)
        self.assertIn("₹", text)


class TestSlowReplyNotice(unittest.TestCase):
    """
    The slow-reply notice tells a customer once, when the bot has actually gone
    slow, that replies are delayed and gives them the manager's direct line.
    """

    NOTICE_START = "Hi thanks so much for connecting"

    def setUp(self):
        from camellia.monitoring import latency_monitor
        self.latency_monitor = latency_monitor
        self._orig_threshold = latency_monitor.slow_threshold_ms
        self._orig_until = config.SLOW_REPLY_NOTICE_UNTIL
        self._orig_enabled = config.SLOW_REPLY_NOTICE_ENABLED
        config.SLOW_REPLY_NOTICE_ENABLED = True
        config.SLOW_REPLY_NOTICE_UNTIL = ""

    def tearDown(self):
        self.latency_monitor.slow_threshold_ms = self._orig_threshold
        self.latency_monitor._samples.clear()
        self.latency_monitor._last_ms = 0.0
        config.SLOW_REPLY_NOTICE_UNTIL = self._orig_until
        config.SLOW_REPLY_NOTICE_ENABLED = self._orig_enabled

    def _force_slow(self):
        """Make any real reply count as slow, exercising the true timing path."""
        self.latency_monitor.slow_threshold_ms = 0.0001
        self.latency_monitor._samples.clear()
        self.latency_monitor._last_ms = 0.0

    def _force_fast(self):
        self.latency_monitor.slow_threshold_ms = 10_000_000.0
        self.latency_monitor._samples.clear()
        self.latency_monitor._last_ms = 0.0

    def test_slow_reply_sends_notice_once_per_session(self):
        from camellia.session_memory import session_memory
        self._force_slow()
        session_memory.clear_session("slow_once_user", "whatsapp")

        first, _, _, _ = responder.match_and_respond("hi", user_id="slow_once_user", channel="whatsapp")
        self.assertTrue(first.startswith(self.NOTICE_START))
        # The customer's actual answer must still be delivered below the notice.
        self.assertGreater(len(first), len(config.SLOW_REPLY_NOTICE_TEXT))

        for follow_up in ["what do you have?", "how much?"]:
            text, _, _, _ = responder.match_and_respond(follow_up, user_id="slow_once_user", channel="whatsapp")
            self.assertFalse(
                text.startswith(self.NOTICE_START),
                f"Notice repeated on follow-up '{follow_up}'",
            )

    def test_notice_is_per_session_not_global(self):
        from camellia.session_memory import session_memory
        self._force_slow()
        for uid in ("slow_a", "slow_b"):
            session_memory.clear_session(uid, "whatsapp")
            text, _, _, _ = responder.match_and_respond("hi", user_id=uid, channel="whatsapp")
            self.assertTrue(text.startswith(self.NOTICE_START), f"{uid} did not get its own notice")

    def test_fast_reply_sends_no_notice(self):
        from camellia.session_memory import session_memory
        self._force_fast()
        session_memory.clear_session("fast_user", "whatsapp")
        text, _, _, _ = responder.match_and_respond("hi", user_id="fast_user", channel="whatsapp")
        self.assertFalse(text.startswith(self.NOTICE_START))

    def test_notice_never_fills_a_blocked_reply(self):
        """An ad-gate-blocked DM must stay empty, not become a notice-only message."""
        from camellia.session_memory import session_memory
        self._force_slow()
        session_memory.clear_session("ig_blocked_user", "instagram")
        with patch.object(config, "BYPASS_AD_GATE", False):
            text, matched, tmpl, _ = responder.match_and_respond(
                "hi", user_id="ig_blocked_user", channel="instagram", referral_source=None
            )
        self.assertEqual(tmpl, "ad_gate_blocked")
        self.assertEqual(text, "")

    def test_expired_notice_is_not_sent(self):
        from camellia.session_memory import session_memory
        self._force_slow()
        config.SLOW_REPLY_NOTICE_UNTIL = "2020-01-01"
        self.assertFalse(config.is_slow_reply_notice_active)
        session_memory.clear_session("expired_user", "whatsapp")
        text, _, _, _ = responder.match_and_respond("hi", user_id="expired_user", channel="whatsapp")
        self.assertFalse(text.startswith(self.NOTICE_START))

    def test_future_expiry_keeps_notice_active(self):
        config.SLOW_REPLY_NOTICE_UNTIL = "2099-01-01"
        self.assertTrue(config.is_slow_reply_notice_active)

    def test_malformed_expiry_leaves_notice_active(self):
        """A typo in the date must not silently switch off a notice the client turned on."""
        config.SLOW_REPLY_NOTICE_UNTIL = "next-tuesday"
        self.assertTrue(config.is_slow_reply_notice_active)

    def test_disabled_flag_overrides_everything(self):
        from camellia.session_memory import session_memory
        self._force_slow()
        config.SLOW_REPLY_NOTICE_ENABLED = False
        self.assertFalse(config.is_slow_reply_notice_active)
        session_memory.clear_session("disabled_user", "whatsapp")
        text, _, _, _ = responder.match_and_respond("hi", user_id="disabled_user", channel="whatsapp")
        self.assertFalse(text.startswith(self.NOTICE_START))


if __name__ == "__main__":
    unittest.main()









