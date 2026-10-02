import os
import unittest
from unittest.mock import patch
from camellia.responder import responder
from camellia.rag_fallback import rag_fallback_engine, RAGFallbackEngine, MEDIUM_CONFIDENCE_THRESHOLD
from camellia.database import db

def _plain(text: str) -> str:
    """
    Flattens typographic spacing so an assertion can be written in ASCII.

    The kitchen hours are written with U+202F narrow no-break spaces so
    "10:00 AM" cannot break across two lines on a phone. That is a choice
    about how the answer looks, not what it says.
    """
    return (
        (text or "")
        .replace("\u202f", " ")
        .replace("\u00a0", " ")
        .replace("\u2011", "-")
        .replace("\u2010", "-")
    )


def _plain(text: str) -> str:
    """
    Flattens typographic spacing so an assertion can be written in ASCII.

    The kitchen hours are written with U+202F narrow no-break spaces so
    "10:00 AM" cannot break across two lines on a phone. That is a choice
    about how the answer looks, not what it says.
    """
    return (
        (text or "")
        .replace("\u202f", " ")
        .replace("\u00a0", " ")
        .replace("\u2011", "-")
        .replace("\u2010", "-")
    )


class TestHybridRAGArchitecture(unittest.TestCase):
    """
    Comprehensive regression test suite for Hybrid Deterministic-RAG Architecture
    covering Scenarios A through J (Section 10).
    """

    def setUp(self):
        db.init_db()
        self.user_id = "test_user_rag_spec"
        self.channel = "whatsapp"

    def test_scenario_a_existing_approved_template(self):
        """Scenario A: Approved template query must return approved text deterministically without RAG."""
        user_msg = "What are your delivery hours?"
        text, was_matched, tmpl_id, img = responder.match_and_respond(user_msg, user_id=self.user_id, channel=self.channel)
        
        self.assertTrue(was_matched)
        self.assertIsNotNone(tmpl_id)
        self.assertIn("10:00 AM", _plain(text))
        self.assertIn("7:30 PM", _plain(text))

    def test_scenario_b_unknown_query_with_relevant_source(self):
        """Scenario B: Unknown query with relevant source context creates candidate template with PENDING_APPROVAL status, does NOT auto-publish."""
        # Query that is not an exact approved trigger but matches menu items in RAG
        user_msg = "Do you serve steamed bhapa pitha?"
        
        # Test RAG retrieval directly
        rag_res = rag_fallback_engine.retrieve(user_msg)
        if rag_res.confidence_score >= 0.60:
            self.assertEqual(rag_res.approval_status, "PENDING_APPROVAL")
            self.assertIsNotNone(rag_res.candidate_id)

        # Test customer-facing responder behavior -> Must escalate safely, NOT auto-publish candidate
        text, was_matched, tmpl_id, img = responder.match_and_respond(user_msg, user_id=self.user_id, channel=self.channel)
        self.assertFalse(was_matched)
        self.assertIn("Manager of Kitchens", text)

    def test_scenario_c_unknown_query_no_relevant_source(self):
        """Scenario C: Unknown query with no relevant source context triggers immediate human escalation."""
        user_msg = "Can I rent a car for my trip to Agra?"
        text, was_matched, tmpl_id, img = responder.match_and_respond(user_msg, user_id=self.user_id, channel=self.channel)
        
        self.assertFalse(was_matched)
        self.assertIn("Manager of Kitchens", text)

    def test_scenario_d_unsupported_price_query(self):
        """Scenario D: Unsupported price query must never invent price, must escalate to human."""
        user_msg = "What is the price for a 50kg custom wedding catering package with customized branding?"
        
        # Verify RAG safety rule flag
        rag_res = rag_fallback_engine.retrieve(user_msg)
        if rag_res.proposed_response:
            self.assertTrue(rag_res.contains_unapproved_facts or rag_res.confidence_score < 0.85)

        # Verify customer response is escalation
        text, was_matched, tmpl_id, img = responder.match_and_respond(user_msg, user_id=self.user_id, channel=self.channel)
        self.assertFalse(was_matched)
        # Bulk/custom enquiries get an intent-specific handover (quote request)
        # rather than the generic notice, but must still hand off to a human
        # and never invent a price for an unsupported request.
        self.assertIn("Manager of Kitchens", text)
        self.assertNotRegex(text, r'₹\s?\d')

    def test_scenario_e_unsupported_shelf_life_query(self):
        """Scenario E: Unsupported shelf life query must never invent shelf life, must escalate to human."""
        user_msg = "Can I keep Sondesh unrefrigerated in my car in June heat for 5 days?"
        text, was_matched, tmpl_id, img = responder.match_and_respond(user_msg, user_id=self.user_id, channel=self.channel)
        
        # Non-standard unapproved storage conditions must return approved facts or escalate
        self.assertTrue("refrigerated" in text.lower() or "Manager of Kitchens" in text)

    def test_scenario_f_staff_manager_contact_query(self):
        """Scenario F: Staff/manager contact query must trigger deterministic manager info or human escalation."""
        user_msg = "Who is the owner and founder of the kitchen and what is their personal email address?"
        text, was_matched, tmpl_id, img = responder.match_and_respond(user_msg, user_id=self.user_id, channel=self.channel)
        
        # Guard template or escalation fallback
        self.assertTrue("guru dayal singh" in text.lower() or "Manager of Kitchens" in text)

    def test_scenario_g_first_contact_messages(self):
        """Scenario G: First-contact messages must be handled deterministically."""
        first_contacts = [
            "Hi",
            "Hello",
            "Hi, I want to know more",
            "What do you sell?",
            "What do you have?",
            "Can you tell me about your products?"
        ]
        for idx, msg in enumerate(first_contacts):
            text, was_matched, tmpl_id, img = responder.match_and_respond(msg, user_id=f"user_g_{idx}", channel=self.channel)
            self.assertTrue(was_matched, f"Failed matching first-contact message: '{msg}'")
            self.assertIsNotNone(tmpl_id)
            self.assertTrue("Sondesh" in text or "Camellia" in text or "Bengal" in text or "menu" in text)

    def test_scenario_h_completely_unknown_message(self):
        """Scenario H: Completely unknown message returns safe human escalation fallback."""
        user_msg = "qwertyuiop asdfghjkl zxcvbnm 12345"
        text, was_matched, tmpl_id, img = responder.match_and_respond(user_msg, user_id=self.user_id, channel=self.channel)
        
        self.assertFalse(was_matched)
        self.assertIn("Manager of Kitchens", text)

    def test_scenario_i_conflicting_rag_sources(self):
        """Scenario I: Conflicting RAG sources flag conflict and escalate to human."""
        user_msg = "Is Sondesh sweet or savory or spicy?"
        rag_res = rag_fallback_engine.retrieve(user_msg)
        
        # If conflict detected, confidence score drops
        if rag_res.has_conflict:
            self.assertLess(rag_res.confidence_score, 0.60)

        text, was_matched, tmpl_id, img = responder.match_and_respond(user_msg, user_id=self.user_id, channel=self.channel)
        self.assertTrue("sondesh" in text.lower() or "Manager of Kitchens" in text)

    def test_scenario_j_qa_templates_vs_rag_conflict(self):
        """A Response Library location claim cannot become business authority."""
        text, matched, template_id, images = responder.match_and_respond("Where is your kitchen located?", user_id=self.user_id, channel=self.channel)
        self.assertFalse(matched)
        self.assertEqual(template_id, "GUARD_pickup_address_hours")
        self.assertIn("Manager of Kitchens", text)
        self.assertNotIn("Sushant Lok", text)
        self.assertIsNone(images)

    def test_forbidden_facts_block_rag_auto_response(self):
        """
        A template response stating a guarded business fact (price, delivery
        area, allergen info, etc.) must never be auto-sent by RAG, even at high
        confidence with an approved qa_templates.json source. This was computed
        as contains_unapproved_facts but never actually checked before setting
        auto_response, so a matched template mentioning a forbidden-fact keyword
        could bypass the human-review gate this class documents for itself.
        """
        engine = RAGFallbackEngine.__new__(RAGFallbackEngine)
        engine.qa_data = {
            "templates": [
                {
                    "id": "unsafe_price_test",
                    "triggers": ["unsafe price test trigger words"],
                    "response": "That costs ₹999 per box.",
                }
            ]
        }
        engine.menu_data = {}

        result = engine.retrieve("unsafe price test trigger words")

        self.assertTrue(result.contains_unapproved_facts)
        self.assertEqual(result.auto_response, "", "A response stating a price must never be auto-sent")
        # Still worth a human's attention — just not sent automatically.
        self.assertGreaterEqual(result.confidence_score, MEDIUM_CONFIDENCE_THRESHOLD)

    def test_fact_free_high_confidence_match_can_still_auto_respond(self):
        """Control for the fix above: a genuinely safe, high-confidence match is unaffected."""
        engine = RAGFallbackEngine.__new__(RAGFallbackEngine)
        engine.qa_data = {
            "templates": [
                {
                    "id": "safe_greeting_test",
                    "triggers": ["safe greeting test trigger words"],
                    "response": "Thanks so much for reaching out, always happy to help!",
                }
            ]
        }
        engine.menu_data = {}

        result = engine.retrieve("safe greeting test trigger words")

        self.assertFalse(result.contains_unapproved_facts)
        self.assertTrue(result.auto_response, "A fact-free high-confidence match should still auto-respond")

    def test_learned_knowledge_zero_confidence_is_not_upgraded(self):
        """
        Staff can explicitly set confidence:0 on a learned-knowledge entry via
        the admin API, meaning "not yet trustworthy." `learned.get('confidence')
        or 1.0` previously treated that 0 as falsy and silently promoted it to
        full confidence, auto-sending an answer staff had explicitly flagged.
        """
        fake_entry = {
            "id": 999999,
            "verified_answer": "Zero-confidence test answer.",
            "confidence": 0,
        }
        with patch("camellia.rag_fallback.db.search_learned_knowledge", return_value=fake_entry):
            result = rag_fallback_engine.retrieve("some zero confidence test query")

        self.assertEqual(result.confidence_score, 0.0)

    def test_learned_knowledge_missing_confidence_still_defaults_to_full_trust(self):
        """A genuinely absent confidence value (not an explicit 0) should still default to 1.0."""
        fake_entry = {
            "id": 999998,
            "verified_answer": "No confidence field test answer.",
        }
        with patch("camellia.rag_fallback.db.search_learned_knowledge", return_value=fake_entry):
            result = rag_fallback_engine.retrieve("some missing confidence test query")

        self.assertEqual(result.confidence_score, 1.0)


if __name__ == "__main__":
    unittest.main()
