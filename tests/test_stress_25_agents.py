"""
Comprehensive 25-Agent Stress Testing Suite for Camellia Artisanal WhatsApp/Instagram Chatbot.
Simulates 25 diverse real-world customer personas and conversational scenarios.
Evaluates:
  1. Response accuracy and factual correctness against menu.json & qa_templates.json
  2. Ad-Gate enforcement (Organic Instagram blocking vs Ad-Clicking allowance)
  3. Session memory multi-turn context and factual slot preservation
  4. Explicit and soft escalation triggers (Forwarding to our team / +91 87965 51990)
  5. Bengali script, Banglish, typos, and cultural slang comprehension
  6. Dietary, packaging, kitchen pickup, and shelf-life policies
"""

import sys
import os
import json
import unittest
from camellia.responder import responder, DeterministicResponder
from camellia.session_memory import session_memory
from camellia.order_calculator import order_calculator
from camellia.config import config

from camellia.database import db

def _plain(text: str) -> str:
    """
    Flattens typographic spacing so an assertion can be written in ASCII.

    The approved copy uses U+202F (narrow no-break space) and U+2011
    (non-breaking hyphen) to stop "10:00 AM" and "all-women" breaking across
    lines on a phone. That is a deliberate choice about how the message looks,
    not about what it says, so a test asserting the wording should not depend
    on it.
    """
    return (
        (text or "")
        .replace("\u202f", " ")
        .replace("\u00a0", " ")
        .replace("\u2011", "-")
        .replace("\u2010", "-")
    )


class Test25AgentStressScenarios(unittest.TestCase):
    def setUp(self):
        # Reset session memory and initialize DB before each agent test
        db.init_db()
        self.responder = DeterministicResponder()
        session_memory._sessions.clear()

    def test_agent_01_bengali_native_speaker(self):
        """Agent 1: Speaks in Bengali script and Banglish asking for Nolen Gur Sondesh."""
        user_id = "user_bengali_01"
        channel = "whatsapp"
        
        # Turn 1: Bengali script query
        resp1 = self.responder.respond("নলেন গুড়ের সন্দেশ পাওয়া যাবে?", user_id=user_id, channel=channel)
        self.assertTrue(resp1.was_matched)
        self.assertIn("Nolen Gur", resp1.text)
        self.assertIn("₹600", resp1.text)

        # Turn 2: Banglish delivery query
        resp2 = self.responder.respond("Delhi te deliver hoy?", user_id=user_id, channel=channel)
        self.assertTrue(resp2.was_matched)
        self.assertIn("Delhi NCR", resp2.text)

    def test_agent_02_corporate_bulk_buyer_escalation(self):
        """Agent 2: Corporate client wanting 100 boxes with custom branding and price discount."""
        user_id = "user_corp_02"
        channel = "whatsapp"
        
        resp = self.responder.respond(
            "Hi, we need 100 custom boxes of Signature Sondesh for Diwali gifting with our company logo ribbons. Can we get a bulk discount?",
            user_id=user_id,
            channel=channel
        )
        # Should escalate to human manager for bulk customization & discount
        self.assertFalse(resp.was_matched)
        self.assertIn("manager of kitchens", resp.text.lower())

    def test_agent_03_budget_haggler(self):
        """Agent 3: Bargaining customer comparing with commercial shops."""
        user_id = "user_haggler_03"
        channel = "whatsapp"

        # Turn 1: Why expensive / compare with Bhikharam
        resp1 = self.responder.respond("Why is your sondesh more expensive than Bhikharam Chandmal?", user_id=user_id, channel=channel)
        self.assertTrue(resp1.was_matched)
        self.assertIn("pure dairy", resp1.text.lower())
        self.assertIn("desi a2", resp1.text.lower())

        # Turn 2: Attempting to negotiate price
        resp2 = self.responder.respond("Can you do it for less price instead of ₹800?", user_id=user_id, channel=channel)
        self.assertFalse(resp2.was_matched)
        self.assertIn("manager of kitchens", resp2.text.lower())

    def test_agent_04_health_and_dietary_inquirer(self):
        """Agent 4: Inquiring about diabetes, Halal meat, and cooking oils."""
        user_id = "user_health_04"
        channel = "whatsapp"

        # Turn 1: Diabetic / sugar free
        resp1 = self.responder.respond("Do you have sugar free or diabetic friendly options?", user_id=user_id, channel=channel)
        self.assertTrue(resp1.was_matched)
        self.assertNotIn("glycemic", resp1.text.lower())
        self.assertNotIn("diabetic-friendly", resp1.text.lower())

        # Turn 2: Halal & Mustard oil check (Confirmed with client: Mutton is Halal, Mustard oil from farmer)
        resp2 = self.responder.respond("Is your meat halal and which oil do you cook in?", user_id=user_id, channel=channel)
        self.assertTrue(resp2.was_matched)
        self.assertEqual(resp2.template_id, "GUARD_halal_ingredients_oil")
        self.assertIn("mutton is halal", resp2.text)
        self.assertIn("mustard oil", resp2.text)

    def test_agent_05_strict_vegan_inquirer(self):
        """Agent 5: Asking if Sondesh can be made vegan / plant-based."""
        user_id = "user_vegan_05"
        channel = "whatsapp"

        resp = self.responder.respond("Are any of your sondesh vegan or lactose-free?", user_id=user_id, channel=channel)
        # Should answer or escalate gracefully informing pure dairy chhana is core
        self.assertTrue(resp.was_matched or "manager of kitchens" in resp.text.lower())

    def test_agent_06_multi_turn_order_with_session_memory(self):
        """Agent 6: 4-turn order with name introduction and slot memory recall."""
        user_id = "user_memory_06"
        channel = "whatsapp"

        # Turn 1: Introduction
        resp1 = self.responder.respond("Hi, my name is Priya Sen! What are your Rose Sondesh prices?", user_id=user_id, channel=channel)
        self.assertIn("Priya Sen", resp1.text)
        self.assertIn("Rose Sondesh", resp1.text)
        self.assertIn("₹450", resp1.text)

        # Turn 2: Specific flavour choice
        resp2 = self.responder.respond("I want the rose flavour in signature collection", user_id=user_id, channel=channel)
        self.assertTrue(resp2.was_matched)
        self.assertIn("Signature", resp2.text)

        # Turn 3: Inquire sizes
        resp3 = self.responder.respond("What are the 500g box options?", user_id=user_id, channel=channel)
        self.assertTrue(resp3.was_matched)
        self.assertIn("500g", resp3.text)

        # Turn 4: Memory check of earlier choice
        resp4 = self.responder.respond("ok and does that come to my earlier flavour choice or do I need to repeat it", user_id=user_id, channel=channel)
        self.assertTrue(resp4.was_matched)
        self.assertIn("Rose", resp4.text)
        self.assertIn("no need to repeat", resp4.text.lower())

    def test_agent_07_out_of_area_bangalore_inquiry(self):
        """Agent 7: Customer from Bangalore asking for home delivery."""
        user_id = "user_blr_07"
        channel = "whatsapp"

        resp = self.responder.respond("Can you deliver 2 boxes of Nolen Gur Sondesh to Bangalore?", user_id=user_id, channel=channel)
        self.assertTrue(resp.was_matched)
        self.assertIn("Norom Pak Sondesh", resp.text)
        self.assertIn("cold-chain", resp.text)
        self.assertIn("unable to ship our sondesh outside Delhi NCR", resp.text)
        self.assertIn("friends or family travelling", resp.text)

    def test_agent_08_typo_and_slang_comprehension(self):
        """Agent 8: Customer with heavy typos and phonetic Bengali terms."""
        user_id = "user_typo_08"
        channel = "whatsapp"

        resp = self.responder.respond("whast is teh proce of jol bhora and kancha gola?", user_id=user_id, channel=channel)
        self.assertTrue(resp.was_matched)
        self.assertTrue("Jolbhora" in resp.text or "Kanchagolla" in resp.text)

    def test_agent_09_angry_complaint_escalation(self):
        """Agent 9: Customer with missing items or transaction problem."""
        user_id = "user_angry_09"
        channel = "whatsapp"

        resp = self.responder.respond("I paid ₹1400 via UPI 2 hours ago but received no confirmation! Where is my manager?", user_id=user_id, channel=channel)
        self.assertFalse(resp.was_matched)
        self.assertIn("manager of kitchens", resp.text.lower())

    def test_agent_10_instagram_ad_gate_enforcement(self):
        """Agent 10: Instagram Ad-Gate test (Organic blocked vs Ad referral allowed)."""
        user_id = "ig_user_10"
        channel = "instagram"

        orig_bypass = config.BYPASS_AD_GATE
        try:
            config.BYPASS_AD_GATE = False
            # 10a: Organic Instagram DM -> Blocked (No auto-response)
            resp_organic = self.responder.respond("Hi send menu", user_id=user_id, channel=channel, referral_source=None)
            self.assertFalse(resp_organic.was_matched)
            self.assertEqual(resp_organic.template_id, "ad_gate_blocked")
            self.assertEqual(resp_organic.text, "")

            # 10b: Ad Referral Click -> Allowed
            resp_ad = self.responder.respond("Hi send menu", user_id=user_id, channel=channel, referral_source="ADS", referral_ad_id="ad_diwali_123")
            self.assertTrue(resp_ad.was_matched)
            # This test is about the gate: the identical message is silenced
            # organically and answered after an ad click. It used to assert the
            # brand name appeared in the reply, which pinned it to whichever
            # template "Hi send menu" happened to hit — today the clarifier,
            # which asks whether they want Sondesh or Meals and names neither
            # the brand nor a wrong thing. Asserting a real reply came back
            # keeps the gate covered without re-testing template copy.
            self.assertNotEqual(resp_ad.template_id, "ad_gate_blocked")
            self.assertTrue(resp_ad.text.strip(), "an ad-referred DM got no reply")
        finally:
            config.BYPASS_AD_GATE = orig_bypass

    def test_agent_11_delhi_ncr_localities_coverage(self):
        """Agent 11: Asking delivery to various NCR localities."""
        user_id = "user_ncr_11"
        channel = "whatsapp"

        for loc in ["Do you deliver to Dwarka Sector 12?", "Is delivery available in Indirapuram Ghaziabad?", "Can you send to CR Park Delhi?"]:
            resp = self.responder.respond(loc, user_id=user_id, channel=channel)
            self.assertTrue(resp.was_matched, f"Failed on locality: {loc}")
            self.assertIn("Delhi NCR", resp.text)

    def test_agent_12_kitchen_self_pickup(self):
        """Agent 12: Customer wanting to collect directly from kitchen (Confirmed address & hours)."""
        user_id = "user_pickup_12"
        channel = "whatsapp"

        resp = self.responder.respond("Can I pick up my order from your kitchen? What is the address and timings?", user_id=user_id, channel=channel)
        self.assertTrue(resp.was_matched)
        self.assertEqual(resp.template_id, "GUARD_pickup_address_hours")
        self.assertIn("Sushant Arcade", _plain(resp.text))
        self.assertIn("10:00 AM – 7:30 PM", _plain(resp.text))

    def test_agent_13_artisanal_differentiation(self):
        """Agent 13: Customer asking what makes Camellia special."""
        user_id = "user_diff_13"
        channel = "whatsapp"

        resp = self.responder.respond("How are you different from other sweet shops?", user_id=user_id, channel=channel)
        self.assertTrue(resp.was_matched)
        self.assertIn("100% pure dairy", resp.text)
        self.assertIn("Nolen Gur", resp.text)
        self.assertIn("all-women kitchen", _plain(resp.text))

    def test_agent_14_custom_assorted_box(self):
        """Agent 14: Mixing flavours in a 500g box (Escalated per custom box policy)."""
        user_id = "user_custom_14"
        channel = "whatsapp"

        resp = self.responder.respond("Can I mix rose and classic flavours in a 500g box?", user_id=user_id, channel=channel)
        self.assertFalse(resp.was_matched)
        self.assertIsNone(resp.template_id)
        self.assertIn("manager of kitchens", resp.text.lower())

    def test_agent_15_high_value_gifting_signature_a2(self):
        """Agent 15: Ordering Mahua Sondesh and Jolbhora in Signature A2."""
        user_id = "user_gift_15"
        channel = "whatsapp"

        resp1 = self.responder.respond("What is the price of Mahua Sondesh?", user_id=user_id, channel=channel)
        self.assertTrue(resp1.was_matched)
        self.assertIn("Mahua", resp1.text)
        self.assertIn("₹900", resp1.text)

        resp2 = self.responder.respond("Tell me about Jolbhora with liquid filling", user_id=user_id, channel=channel)
        self.assertTrue(resp2.was_matched)
        self.assertIn("Jolbhora", resp2.text)
        self.assertIn("₹750", resp2.text)
        self.assertIn("₹900", resp2.text)

    def test_agent_16_weekly_rotation_dishes(self):
        """Agent 16: Reads only this week's approved Instagram menu data."""
        user_id = "user_foodie_16"
        channel = "instagram"

        resp_veg = self.responder.respond("Show this week's vegetarian menu", user_id=user_id, channel=channel)
        self.assertTrue(resp_veg.was_matched)
        self.assertIn("Vegetarian Menu", resp_veg.text)

        resp_nv = self.responder.respond("Show this week's non-veg menu", user_id=user_id, channel=channel)
        self.assertTrue(resp_nv.was_matched)
        self.assertIn("Bhetki Paturi", resp_nv.text)

    def test_agent_17_payment_methods_and_qr_code(self):
        """Agent 17: Inquiring how to pay, UPI QR, and COD (Confirmed: UPI/bank transfer only, No COD)."""
        user_id = "user_pay_17"
        channel = "whatsapp"

        resp = self.responder.respond("Can I pay via UPI QR code or COD?", user_id=user_id, channel=channel)
        self.assertTrue(resp.was_matched)
        self.assertEqual(resp.template_id, "GUARD_payment_method")
        self.assertIn("UPI at 9625083171", resp.text)
        self.assertIn("Yummraj Culinary Expeditions Pvt Ltd", resp.text)
        self.assertIn("credit card at https://razorpay.me/@camelliaartisanal", resp.text)

    def test_agent_18_shelf_life_and_storage(self):
        """Agent 18: Inquiring how long Sondesh lasts and storage guidelines (Confirmed: 1 week refrigerated)."""
        user_id = "user_storage_18"
        channel = "whatsapp"

        resp = self.responder.respond("How long can I store the sondesh and does it need refrigeration?", user_id=user_id, channel=channel)
        self.assertTrue(resp.was_matched)
        self.assertEqual(resp.template_id, "GUARD_shelf_life_storage")
        self.assertIn("week refrigerated", resp.text)
        self.assertIn("12 seconds", resp.text)

    def test_agent_19_spam_and_gibberish_filter(self):
        """Agent 19: Malicious bot sending spam links and gibberish."""
        user_id = "spam_bot_19"
        channel = "whatsapp"

        resp1 = self.responder.respond("Claim free coins at http://freecoins-crypto.xyz click now!", user_id=user_id, channel=channel)
        self.assertFalse(resp1.was_matched)
        self.assertIn("manager of kitchens", resp1.text.lower())

        resp2 = self.responder.respond("??? !!! 🚀🌸", user_id=user_id, channel=channel)
        self.assertFalse(resp2.was_matched)

    def test_agent_20_order_receipt_calculator(self):
        """Agent 20: Comprehensive order calculation of multiple items."""
        user_id = "user_calc_20"
        channel = "whatsapp"

        # Ordering 2 boxes of 500g Rose Sondesh (Aamar Mishti: 2 * ₹800 = ₹1600) + 1 box 250g Jolbhora (₹750)
        res = order_calculator.parse_and_calculate("I want 2 boxes of 500g Rose Sondesh and 1 box of 250g Jolbhora")
        self.assertIsNotNone(res)
        self.assertIn("ORDER DRAFT RECEIPT", res["formatted_receipt"])
        self.assertIn("₹2,350", res["formatted_receipt"])

    def test_agent_21_bengali_bento_combo(self):
        """Agent 21: Inquiring about Bengali Bento meals."""
        user_id = "user_bento_21"
        channel = "whatsapp"

        resp = self.responder.respond("What is in your Non-Veg Bento box?", user_id=user_id, channel=channel)
        self.assertTrue(resp.was_matched)
        self.assertIn("Bento", resp.text)
        # Client's confirmed copy spells it lowercase "luchi"; match case-insensitively.
        self.assertIn("luchi", resp.text.lower())

    def test_agent_22_allergen_nut_safety(self):
        """Agent 22: Checking nut allergies (Confirmed: milk & sugar only — no nuts)."""
        user_id = "user_allergy_22"
        channel = "whatsapp"

        resp = self.responder.respond("Are your classic and rose sondesh safe for someone with a severe nut allergy?", user_id=user_id, channel=channel)
        self.assertTrue(resp.was_matched)
        self.assertEqual(resp.template_id, "GUARD_allergen_nutfree_glutenfree")
        self.assertIn("Sondesh is made from milk & sugar only — no nuts.", resp.text)

    def test_agent_23_collection_comparison(self):
        """Agent 23: Comparing Collection 1 vs Collection 2 (Confirmed collection differences)."""
        user_id = "user_compare_23"
        channel = "whatsapp"

        resp = self.responder.respond("What is the difference between Collection 1 and Collection 2?", user_id=user_id, channel=channel)
        self.assertTrue(resp.was_matched)
        self.assertEqual(resp.template_id, "GUARD_collection_differentiation")
        self.assertIn("Aamar Mishti", resp.text)
        self.assertIn("Signature Sondesh", resp.text)

    def test_agent_24_packaging_sustainability(self):
        """Agent 24: Asking about eco-friendly packaging and plastic usage."""
        user_id = "user_eco_24"
        channel = "whatsapp"

        resp = self.responder.respond("Is your packaging plastic-free and eco-friendly?", user_id=user_id, channel=channel)
        self.assertTrue(resp.was_matched)
        self.assertIn("plastic-free", resp.text.lower())
        self.assertIn("cornstarch", resp.text.lower())

    def test_agent_25_general_greeting_and_menu_browse(self):
        """Agent 25: First-time visitor greeting and asking for full catalog."""
        user_id = "user_greet_25"
        channel = "whatsapp"

        resp1 = self.responder.respond("Hello!", user_id=user_id, channel=channel)
        self.assertTrue(resp1.was_matched)
        self.assertIn("Camellia Artisanal", resp1.text)

        resp2 = self.responder.respond("Please share your full menu and prices", user_id=user_id, channel=channel)
        self.assertTrue(resp2.was_matched)
        self.assertIn("Aamar Mishti", resp2.text)
        self.assertIn("Signature", resp2.text)

    def test_agent_26_savoury_lunch_dinner_to_bento(self):
        """Agent 26 (Phase D): Savoury food inquiry followed by Bengali Bento specific inquiry (bento_details answers with confirmed tiers)."""
        user_id = "user_savoury_26"
        channel = "whatsapp"

        # Turn 1: General lunch/dinner options
        resp1 = self.responder.respond("Do you have any lunch or dinner options today?", user_id=user_id, channel=channel)
        self.assertTrue(resp1.was_matched)
        self.assertEqual(resp1.template_id, "savoury_food_intro")
        self.assertIn("three ways to order", resp1.text)

        # Turn 2: Specific Bento box details (client-confirmed 2026-09-03 -> answers with the three tiers)
        resp2 = self.responder.respond("Tell me more about the Bengali Bento meal box", user_id=user_id, channel=channel)
        self.assertTrue(resp2.was_matched)
        self.assertEqual(resp2.template_id, "bento_details")
        self.assertIn("₹350", resp2.text)
        self.assertIn("₹400", resp2.text)
        self.assertIn("₹600", resp2.text)

    def test_agent_27_subscription_plan_to_booking_flow(self):
        """Agent 27 (Phase D): Subscription inquiry followed by booking steps in same session."""
        user_id = "user_sub_27"
        channel = "whatsapp"

        # Turn 1: Ask about subscription plan
        resp1 = self.responder.respond("Tell me about your everyday meal subscription", user_id=user_id, channel=channel)
        self.assertTrue(resp1.was_matched)
        self.assertEqual(resp1.template_id, "subscription_details")
        self.assertIn("Price of 5 / 10 / 30 MEALS", resp1.text)

        # Turn 2: Follow up with booking steps
        resp2 = self.responder.respond("How do I book the subscription plan?", user_id=user_id, channel=channel)
        self.assertTrue(resp2.was_matched)
        self.assertEqual(resp2.template_id, "subscription_booking_steps")
        self.assertIn("Choose Option 1, 2 or 3", resp2.text)
        self.assertNotIn("how_to_order", resp2.template_id)

    def test_agent_28_first_time_vs_returning_customer_savoury_intro(self):
        """Agent 28 (Phase D): First-time customer vs returning customer detection on savoury intro."""
        from camellia.database import db

        # Scenario 1: First-time customer (no prior history)
        first_time_uid = "user_first_time_28"
        session_memory.clear_session(first_time_uid, "whatsapp")
        resp1 = self.responder.respond("What is your savoury lunch menu?", user_id=first_time_uid, channel="whatsapp")
        self.assertTrue(resp1.was_matched)
        self.assertEqual(resp1.template_id, "savoury_food_intro")
        self.assertIn("If this is your first Camellia meal and the menu feels unfamiliar, I'll be happy to help you choose!", resp1.text)

        # Scenario 2: Returning customer with seeded SQLite DB history
        returning_uid = "user_returning_28"
        session_memory.clear_session(returning_uid, "whatsapp")
        db.log_conversation(returning_uid, "whatsapp", "Hi prior turn", "Hello response", True, "greeting")

        resp2 = self.responder.respond("What is your savoury lunch menu?", user_id=returning_uid, channel="whatsapp")
        self.assertTrue(resp2.was_matched)
        self.assertEqual(resp2.template_id, "savoury_food_intro")
        self.assertNotIn("If this is your first Camellia meal and the menu feels unfamiliar, I'll be happy to help you choose!", resp2.text)

    def test_agent_29_returning_customer_after_24h_gap_flow(self):
        """Agent 29 (Phase D): Full agent flow simulating >24h gap with name retention & order draft reset."""
        import time

        gap_uid = "user_gap_29"
        channel = "whatsapp"
        session_memory.clear_session(gap_uid, channel)

        # Turn 1: User introduces name and asks about Jolbhora Sondesh (creating order draft & facts)
        resp1 = self.responder.respond("Hi, my name is Priya. What is the price of Jolbhora Sondesh?", user_id=gap_uid, channel=channel)
        self.assertTrue(resp1.was_matched)
        sess = session_memory.get_session(gap_uid, channel)
        self.assertEqual(sess["facts"]["name"], "Priya")
        self.assertEqual(sess["facts"]["order_draft"]["flavour"], "jolbhora")

        # Simulate >24h inactivity gap (e.g. 25 hours = 90000 seconds)
        sess["last_updated"] = time.time() - 90000

        # Turn 2: Customer returns 25 hours later asking about savoury lunch options
        resp2 = self.responder.respond("What savoury lunch options do you have today?", user_id=gap_uid, channel=channel)
        self.assertTrue(resp2.was_matched)
        self.assertEqual(resp2.template_id, "savoury_food_intro")

        # Verify: Customer name is RETAINED in session memory and the order
        # draft is RESET; meal/menu interest gets the one-time add-on.
        sess_after = session_memory.get_session(gap_uid, channel)
        self.assertEqual(sess_after["facts"]["name"], "Priya")
        self.assertIsNone(sess_after["facts"]["order_draft"]["flavour"])
        self.assertIn("handcrafted Norom Pak Sondesh", resp2.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

