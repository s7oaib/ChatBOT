"""
AI Resolution & Answers, Phase 3 slice 2: the LLM triage classifier running
in shadow beside the existing candidate/escalation/knowledge-gap pipeline.

Modelled directly on tests/test_router_shadow.py — the safety properties
matter more than the categorizing here. Shadow mode runs on live customer
traffic, so it must be incapable of changing a reply, delaying one, or
breaking one when the provider misbehaves, and it must never reach
WhatsApp regardless of what flags are set.
"""

import json
from unittest.mock import MagicMock, patch

from camellia import app as app_module

from camellia.config import config
from camellia.database import db
from camellia.responder import responder
from camellia.session_memory import session_memory
from camellia.triage import CATEGORIES, TriageClassifier, triage_classifier


class TestClassify:
    def _classifier_replying(self, text):
        c = TriageClassifier()
        fake = MagicMock()
        fake.get_active_provider.return_value = "groq"
        fake._query_groq.return_value = (text, True, "groq_rag")
        return c, fake

    def test_a_valid_category_is_returned(self):
        c, fake = self._classifier_replying(
            json.dumps({"category": "teach_required", "confidence": 0.4, "reasoning": "no matching answer"})
        )
        with patch("camellia.ai_responder.ai_responder", fake):
            result = c.classify("do you cater weddings?")
        assert result == {"ok": True, "category": "TEACH_REQUIRED", "confidence": 0.4, "reasoning": "no matching answer"}

    def test_every_documented_category_is_accepted(self):
        for category in CATEGORIES:
            c, fake = self._classifier_replying(json.dumps({"category": category, "confidence": 0.5, "reasoning": "x"}))
            with patch("camellia.ai_responder.ai_responder", fake):
                result = c.classify("anything")
            assert result["ok"] is True and result["category"] == category

    def test_confidence_is_clamped_to_zero_one(self):
        c, fake = self._classifier_replying(json.dumps({"category": "NON_ACTIONABLE", "confidence": 4.2, "reasoning": "x"}))
        with patch("camellia.ai_responder.ai_responder", fake):
            result = c.classify("thanks!")
        assert result["confidence"] == 1.0

    def test_an_invented_category_is_rejected_rather_than_coerced(self):
        c, fake = self._classifier_replying(json.dumps({"category": "MAYBE_LATER", "confidence": 0.9, "reasoning": "x"}))
        with patch("camellia.ai_responder.ai_responder", fake):
            result = c.classify("anything")
        assert result["ok"] is False
        assert result["reason"].startswith("invalid_category:")

    def test_unparseable_json_is_reported_not_raised(self):
        c, fake = self._classifier_replying("not json at all")
        with patch("camellia.ai_responder.ai_responder", fake):
            result = c.classify("anything")
        assert result == {"ok": False, "reason": "unparseable_response"}

    def test_a_provider_failure_is_reported_not_raised(self):
        c = TriageClassifier()
        fake = MagicMock()
        fake.get_active_provider.return_value = "groq"
        fake._query_groq.return_value = ("", False, "groq_http_429")
        with patch("camellia.ai_responder.ai_responder", fake):
            result = c.classify("anything")
        assert result["ok"] is False
        assert "429" in result["reason"]

    def test_nothing_is_asked_when_no_provider_is_available(self):
        c = TriageClassifier()
        fake = MagicMock()
        fake.get_active_provider.return_value = "none"
        with patch("camellia.ai_responder.ai_responder", fake):
            result = c.classify("hi")
        assert result == {"ok": False, "reason": "provider_unavailable"}
        assert not fake._query_groq.called

    def test_nothing_is_asked_for_an_empty_message(self):
        c = TriageClassifier()
        fake = MagicMock()
        with patch("camellia.ai_responder.ai_responder", fake):
            result = c.classify("   ")
        assert result == {"ok": False, "reason": "empty_message"}
        assert not fake.get_active_provider.called

    def test_the_classifier_uses_its_own_model_not_the_reply_model(self):
        c, fake = self._classifier_replying(json.dumps({"category": "NON_ACTIONABLE", "confidence": 0.9, "reasoning": "x"}))
        with patch("camellia.ai_responder.ai_responder", fake), \
             patch.object(config, "LLM_TRIAGE_MODEL", "test/triage-model"):
            c.classify("thanks!")
        assert fake._query_groq.call_args.kwargs["model"] == "test/triage-model"


class TestGrounding:
    def test_no_matched_knowledge_is_reported_plainly(self):
        c = TriageClassifier()
        with patch.object(db, "search_learned_knowledge", return_value=None):
            grounding = c._grounding_for("do you cater weddings?")
        assert grounding == {
            "matched_knowledge_id": None,
            "matched_knowledge_source": None,
            "matched_knowledge_preview": None,
            "grounded": False,
            "grounding_reason": "no_matched_knowledge",
        }

    def test_a_matched_answer_is_run_through_the_real_grounding_gate(self):
        c = TriageClassifier()
        match = {"id": 7, "source": "staff_resolution", "verified_answer": "We deliver across Delhi NCR."}
        with patch.object(db, "search_learned_knowledge", return_value=match), \
             patch("camellia.rag_fallback.rag_fallback_engine.grounding_report", return_value=(True, "ok")) as gr:
            grounding = c._grounding_for("do you deliver to noida?")
        assert grounding["matched_knowledge_id"] == 7
        assert grounding["grounded"] is True
        gr.assert_called_once()


class TestShadowIsHarmless:
    def test_nothing_is_queued_while_the_flag_is_off(self):
        c = TriageClassifier()
        with patch.object(config, "LLM_TRIAGE_ENABLED", False), \
             patch.object(c, "_pool_or_none") as pool:
            c.record_shadow("hi", "instagram", "u1", False, None)
        assert not pool.called

    def test_a_broken_pool_never_reaches_the_caller(self):
        c = TriageClassifier()
        with patch.object(config, "LLM_TRIAGE_ENABLED", True), \
             patch.object(c, "_pool_or_none", side_effect=RuntimeError("no threads")):
            c.record_shadow("hi", "instagram", "u1", False, None)  # must not raise

    def test_a_classify_exception_is_still_recorded_rather_than_lost(self):
        c = TriageClassifier()
        with patch.object(c, "classify", side_effect=ValueError("boom")), \
             patch.object(c, "_grounding_for", return_value={}), \
             patch.object(db, "record_triage_shadow") as record:
            c._run_triage("hi", "instagram", "u1", False, None)
        assert record.call_args.kwargs["detail"].startswith("exception:")
        assert record.call_args.kwargs["category"] is None
        assert record.call_args.kwargs["would_auto_resolve"] is False

    def test_the_queue_is_bounded_so_a_slow_provider_cannot_pile_up(self):
        c = TriageClassifier()
        c._pending = 64
        with patch.object(config, "LLM_TRIAGE_ENABLED", True), \
             patch.object(c, "_pool_or_none") as pool:
            c.record_shadow("hi", "instagram", "u1", False, None)
        assert not pool.called

    def test_would_auto_resolve_requires_category_confidence_and_grounding_together(self):
        c = TriageClassifier()
        with patch.object(c, "classify", return_value={"ok": True, "category": "AUTO_RESOLVE", "confidence": 0.9, "reasoning": "x"}), \
             patch.object(c, "_grounding_for", return_value={"grounded": True}), \
             patch.object(config, "LLM_TRIAGE_CONFIDENCE_THRESHOLD", 0.85), \
             patch.object(db, "record_triage_shadow") as record:
            c._run_triage("free delivery?", "instagram", "u1", False, None)
        assert record.call_args.kwargs["would_auto_resolve"] is True

    def test_would_auto_resolve_is_false_when_not_grounded_even_at_high_confidence(self):
        c = TriageClassifier()
        with patch.object(c, "classify", return_value={"ok": True, "category": "AUTO_RESOLVE", "confidence": 0.99, "reasoning": "x"}), \
             patch.object(c, "_grounding_for", return_value={"grounded": False}), \
             patch.object(db, "record_triage_shadow") as record:
            c._run_triage("free delivery?", "instagram", "u1", False, None)
        assert record.call_args.kwargs["would_auto_resolve"] is False

    def test_the_reply_is_unchanged_and_the_classifier_is_consulted_off_the_path(self):
        user_id = "triage_shadow_reply"
        session_memory.clear_session(user_id, "instagram")

        with patch.object(config, "LLM_TRIAGE_ENABLED", False), \
             patch.object(config, "BYPASS_AD_GATE", True):
            plain = responder.match_and_respond("asdkfjhqwoiuerlaksjdf nonsense query", user_id=user_id, channel="instagram")

        session_memory.clear_session(user_id, "instagram")
        with patch.object(config, "LLM_TRIAGE_ENABLED", True), \
             patch.object(config, "BYPASS_AD_GATE", True), \
             patch.object(triage_classifier, "record_shadow") as record:
            shadowed = responder.match_and_respond("asdkfjhqwoiuerlaksjdf nonsense query", user_id=user_id, channel="instagram")

        assert shadowed[0] == plain[0]
        assert shadowed[2] == plain[2]
        assert record.called

    def test_triage_never_fires_on_whatsapp_even_if_the_flag_is_on(self):
        user_id = "triage_shadow_whatsapp"
        session_memory.clear_session(user_id, "whatsapp")
        with patch.object(config, "LLM_TRIAGE_ENABLED", True), \
             patch.object(triage_classifier, "record_shadow") as record:
            responder.match_and_respond("asdkfjhqwoiuerlaksjdf nonsense query", user_id=user_id, channel="whatsapp")
        assert not record.called

    def test_triage_never_fires_for_a_message_that_was_already_matched(self):
        user_id = "triage_shadow_matched"
        session_memory.clear_session(user_id, "instagram")
        with patch.object(config, "LLM_TRIAGE_ENABLED", True), \
             patch.object(config, "BYPASS_AD_GATE", True), \
             patch.object(triage_classifier, "record_shadow") as record:
            responder.match_and_respond("do you deliver to delhi ncr?", user_id=user_id, channel="instagram")
        assert not record.called

    def test_triage_never_fires_when_the_ad_gate_blocks_the_message(self):
        user_id = "triage_shadow_ad_gate"
        session_memory.clear_session(user_id, "instagram")
        with patch.object(config, "LLM_TRIAGE_ENABLED", True), \
             patch.object(config, "BYPASS_AD_GATE", False), \
             patch.object(triage_classifier, "record_shadow") as record:
            responder.match_and_respond("anything at all", user_id=user_id, channel="instagram")
        assert not record.called


class TestReport:
    def test_by_category_and_recent_rows_are_both_reported(self):
        db.record_triage_shadow(
            channel="instagram", user_id="u1", user_message="do you cater weddings?",
            category="TEACH_REQUIRED", confidence=0.4, reasoning="no matching answer",
            ok=True, detail="ok", matched_knowledge_id=None, matched_knowledge_source=None,
            matched_knowledge_preview=None, grounded=False, grounding_reason="no_matched_knowledge",
            would_auto_resolve=False, was_matched=False, template_id=None, latency_ms=500.0,
            model="test/triage-model",
        )
        report = db.triage_shadow_report(days=3650, limit=50)
        assert report["classified"] >= 1
        assert any(c["category"] == "TEACH_REQUIRED" for c in report["by_category"])
        assert any(r["user_message"] == "do you cater weddings?" for r in report["recent"])

    def test_would_auto_resolve_is_counted_separately(self):
        db.record_triage_shadow(
            channel="instagram", user_id="u2", user_message="do you deliver to noida?",
            category="AUTO_RESOLVE", confidence=0.95, reasoning="exact verified match",
            ok=True, detail="ok", matched_knowledge_id=3, matched_knowledge_source="staff_resolution",
            matched_knowledge_preview="We deliver across Delhi NCR.", grounded=True, grounding_reason="ok",
            would_auto_resolve=True, was_matched=False, template_id=None, latency_ms=450.0,
            model="test/triage-model",
        )
        report = db.triage_shadow_report(days=3650, limit=50)
        assert report["would_auto_resolve"] >= 1

    def test_failures_are_reported_separately_from_categories(self):
        db.record_triage_shadow(
            channel="instagram", user_id="u3", user_message="garbled input",
            category=None, confidence=None, reasoning=None,
            ok=False, detail="provider_error:groq_http_429", matched_knowledge_id=None,
            matched_knowledge_source=None, matched_knowledge_preview=None, grounded=False,
            grounding_reason="no_matched_knowledge", would_auto_resolve=False, was_matched=False,
            template_id=None, latency_ms=300.0, model="test/triage-model",
        )
        report = db.triage_shadow_report(days=3650, limit=50)
        assert any(f["detail"].startswith("provider_error:") for f in report["failures"])


class TestHealthReportsShadowState:
    def test_health_shows_the_flag_is_off(self):
        with patch.object(config, "LLM_TRIAGE_ENABLED", False):
            payload = json.loads(app_module.health_check().body)
        assert payload["llm_triage"]["enabled"] is False
        assert payload["llm_triage"]["model"] is None

    def test_health_shows_the_model_and_a_live_count_when_on(self):
        with patch.object(config, "LLM_TRIAGE_ENABLED", True), \
             patch.object(config, "LLM_TRIAGE_MODEL", "test/triage-model"), \
             patch.object(db, "triage_shadow_pulse", return_value={
                 "classified_recently": 9, "would_auto_resolve_recently": 2,
                 "last_classification_at": "2026-09-12 02:00:00", "window_hours": 24,
             }):
            payload = json.loads(app_module.health_check().body)

        shadow = payload["llm_triage"]
        assert shadow["enabled"] is True
        assert shadow["model"] == "test/triage-model"
        assert shadow["classified_recently"] == 9

    def test_a_broken_pulse_query_never_takes_health_down_with_it(self):
        with patch.object(config, "LLM_TRIAGE_ENABLED", True), \
             patch.object(db, "triage_shadow_pulse", side_effect=RuntimeError("locked")):
            resp = app_module.health_check()
        payload = json.loads(resp.body)
        assert resp.status_code == 200
        assert payload["llm_triage"]["error"] == "RuntimeError"


class TestAdminEndpoint:
    def test_endpoint_reports_disabled_state_with_a_note(self):
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        with patch.object(config, "LLM_TRIAGE_ENABLED", False), \
             patch.object(db, "triage_shadow_report", return_value={
                 "days": 7, "classified": 0, "ok": 0, "grounded": 0, "would_auto_resolve": 0,
                 "avg_latency_ms": None, "by_category": [], "failures": [], "recent": [],
             }):
            resp = client.get(
                "/admin/triage-shadow",
                headers={"X-Admin-Key": config.ADMIN_API_KEY, "X-Admin-Pin": config.ADMIN_API_KEY},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["shadow_enabled"] is False
        assert "note" in data
