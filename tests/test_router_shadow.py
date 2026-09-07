"""
Phase 3: the LLM router picking in silence beside the keyword matcher.

The safety properties matter more than the picking here. Shadow mode runs on
live customer traffic, so it must be incapable of changing a reply, delaying
one, or breaking one when the provider misbehaves.
"""

import json
from unittest.mock import MagicMock, patch

from camellia import app as app_module

from camellia.config import config
from camellia.database import db
from camellia.responder import responder
from camellia.router import ShadowRouter, shadow_router
from camellia.session_memory import session_memory

TEMPLATES = [
    {"id": "know_more", "response": "Which would you like — Sondesh or Meals?",
     "router_hint": "The general answer when someone asks to see the menu."},
    {"id": "delivery_area", "response": "We deliver right across Delhi NCR."},
    {"id": "greeting", "responses": ["Hi! Welcome to Camellia Artisanal."]},
]


class TestCatalog:
    def test_every_template_appears_with_its_id(self):
        catalog, ids = ShadowRouter().build_catalog(TEMPLATES)
        assert set(ids) == {"know_more", "delivery_area", "greeting"}
        for tmpl_id in ids:
            assert tmpl_id in catalog

    def test_a_hint_replaces_the_derived_gist_where_one_is_written(self):
        catalog, _ = ShadowRouter().build_catalog(TEMPLATES)
        assert "The general answer when someone asks to see the menu." in catalog
        assert "Which would you like — Sondesh or Meals?" not in catalog

    def test_a_template_with_only_a_response_bank_still_gets_a_line(self):
        catalog, _ = ShadowRouter().build_catalog(TEMPLATES)
        assert "greeting: Hi! Welcome to Camellia Artisanal." in catalog


class TestClassify:
    def _router_replying(self, text):
        r = ShadowRouter()
        fake = MagicMock()
        fake.get_active_provider.return_value = "groq"
        fake._query_groq.return_value = (text, True, "groq_rag")
        return r, fake

    def test_a_valid_id_is_returned(self):
        r, fake = self._router_replying("delivery_area")
        with patch("camellia.ai_responder.ai_responder", fake):
            assert r.classify("do you ship to noida", TEMPLATES) == ("delivery_area", "ok")

    def test_untidy_formatting_around_the_id_is_tolerated(self):
        r, fake = self._router_replying("`delivery_area`.\n")
        with patch("camellia.ai_responder.ai_responder", fake):
            assert r.classify("do you ship to noida", TEMPLATES)[0] == "delivery_area"

    def test_none_is_a_real_answer_not_a_failure(self):
        # Nothing covering the question is exactly what escalation is for.
        r, fake = self._router_replying("NONE")
        with patch("camellia.ai_responder.ai_responder", fake):
            assert r.classify("who is the president", TEMPLATES) == (None, "router_says_none")

    def test_an_invented_id_is_rejected_rather_than_coerced(self):
        r, fake = self._router_replying("sondesh_gift_hampers")
        with patch("camellia.ai_responder.ai_responder", fake):
            pick, detail = r.classify("gift hampers?", TEMPLATES)
        assert pick is None
        assert detail.startswith("invalid_id:")

    def test_a_provider_failure_is_reported_not_raised(self):
        r = ShadowRouter()
        fake = MagicMock()
        fake.get_active_provider.return_value = "groq"
        fake._query_groq.return_value = ("", False, "groq_http_429")
        with patch("camellia.ai_responder.ai_responder", fake):
            pick, detail = r.classify("anything", TEMPLATES)
        assert pick is None
        assert "429" in detail

    def test_nothing_is_asked_when_no_provider_is_available(self):
        r = ShadowRouter()
        fake = MagicMock()
        fake.get_active_provider.return_value = "none"
        with patch("camellia.ai_responder.ai_responder", fake):
            assert r.classify("hi", TEMPLATES) == (None, "provider_unavailable")
        assert not fake._query_groq.called


class TestShadowIsHarmless:
    def test_nothing_is_queued_while_the_flag_is_off(self):
        r = ShadowRouter()
        with patch.object(config, "ROUTER_SHADOW_ENABLED", False), \
             patch.object(r, "_pool_or_none") as pool:
            r.record_shadow("hi", "whatsapp", "greeting", TEMPLATES)
        assert not pool.called

    def test_a_broken_pool_never_reaches_the_caller(self):
        # This runs on the live reply path. It may fail; it may not raise.
        r = ShadowRouter()
        with patch.object(config, "ROUTER_SHADOW_ENABLED", True), \
             patch.object(r, "_pool_or_none", side_effect=RuntimeError("no threads")):
            r.record_shadow("hi", "whatsapp", "greeting", TEMPLATES)  # must not raise

    def test_a_classify_exception_is_still_recorded_rather_than_lost(self):
        r = ShadowRouter()
        with patch.object(r, "classify", side_effect=ValueError("boom")), \
             patch.object(db, "record_router_shadow") as record:
            r._run_comparison("hi", "whatsapp", "greeting", TEMPLATES, None)
        assert record.call_args.kwargs["detail"].startswith("exception:")
        assert record.call_args.kwargs["router_pick"] is None

    def test_the_queue_is_bounded_so_a_slow_provider_cannot_pile_up(self):
        r = ShadowRouter()
        r._pending = 64
        with patch.object(config, "ROUTER_SHADOW_ENABLED", True), \
             patch.object(r, "_pool_or_none") as pool:
            r.record_shadow("hi", "whatsapp", "greeting", TEMPLATES)
        assert not pool.called

    def test_the_reply_is_unchanged_and_the_router_is_consulted_off_the_path(self):
        user_id = "router_shadow_reply"
        session_memory.clear_session(user_id, "whatsapp")

        with patch.object(config, "ROUTER_SHADOW_ENABLED", False):
            plain = responder.match_and_respond("Where do you deliver?", user_id=user_id, channel="whatsapp")

        session_memory.clear_session(user_id, "whatsapp")
        with patch.object(config, "ROUTER_SHADOW_ENABLED", True), \
             patch.object(shadow_router, "record_shadow") as record:
            shadowed = responder.match_and_respond("Where do you deliver?", user_id=user_id, channel="whatsapp")

        assert shadowed[0] == plain[0]
        assert shadowed[2] == plain[2]
        assert record.called
        assert record.call_args.kwargs["keyword_pick"] == plain[2]


class TestReport:
    def test_agreement_and_disagreements_are_both_reported(self):
        db.record_router_shadow("whatsapp", "where do you deliver", "delivery_area", "delivery_area", "ok", 700.0)
        db.record_router_shadow("whatsapp", "im furious my order is late", "name_introduction", "order_status_tracking", "ok", 800.0)

        report = db.router_shadow_report(days=3650, limit=50)

        assert report["compared"] >= 2
        assert report["agreement_rate"] is not None
        disagreed = [d["message"] for d in report["disagreements"]]
        assert "im furious my order is late" in disagreed

    def test_messages_the_keyword_matcher_missed_entirely_are_counted(self):
        # The number that justifies the rollout: the router placed something
        # the trigger list could not.
        db.record_router_shadow("instagram", "any sweets for a diwali hamper", None, "product_overview", "ok", 650.0)

        report = db.router_shadow_report(days=3650, limit=50)
        assert report["unmatched_but_routed_in_sample"] >= 1


class TestRouterModelIsolation:
    """The router runs on its own model, and must not disturb the reply path."""

    def test_the_router_uses_its_own_model_not_the_reply_model(self):
        r = ShadowRouter()
        fake = MagicMock()
        fake.get_active_provider.return_value = "groq"
        fake._query_groq.return_value = ("delivery_area", True, "groq_rag")
        with patch("camellia.ai_responder.ai_responder", fake), \
             patch.object(config, "GROQ_ROUTER_MODEL", "groq/compound-mini"):
            r.classify("do you ship to noida", TEMPLATES)
        assert fake._query_groq.call_args.kwargs["model"] == "groq/compound-mini"

    def test_a_router_rate_limit_does_not_pause_the_model_that_writes_replies(self):
        # Rate limits on Groq are per model. A 429 on the router's model must
        # not stop refinement and grounded answers, which customers depend on.
        from camellia.ai_responder import AIResponder

        responder_ai = AIResponder()
        resp = MagicMock(status_code=429, text="rate limited")
        resp.headers = {}
        client = MagicMock()
        client.__enter__.return_value.post.return_value = resp

        with patch.object(config, "GROQ_API_KEY", "k"), \
             patch.object(config, "GROQ_MODEL", "openai/gpt-oss-20b"), \
             patch("camellia.ai_responder.httpx.Client", return_value=client):
            responder_ai._query_groq("hi", model="groq/compound-mini")

        assert responder_ai._groq_cooldown_until == 0.0
        assert responder_ai._last_call_ok is None

    def test_a_failure_on_the_reply_model_still_trips_its_cooldown(self):
        from camellia.ai_responder import AIResponder

        responder_ai = AIResponder()
        resp = MagicMock(status_code=429, text="rate limited")
        resp.headers = {}
        client = MagicMock()
        client.__enter__.return_value.post.return_value = resp

        with patch.object(config, "GROQ_API_KEY", "k"), \
             patch.object(config, "GROQ_MODEL", "openai/gpt-oss-20b"), \
             patch("camellia.ai_responder.httpx.Client", return_value=client):
            responder_ai._query_groq("hi")

        assert responder_ai._groq_cooldown_until > 0.0
        assert responder_ai._last_call_ok is False

    def test_the_model_behind_each_pick_is_recorded_for_comparison(self):
        db.record_router_shadow(
            "whatsapp", "model attribution", "know_more", "know_more", "ok", 600.0,
            model="groq/compound-mini",
        )
        report = db.router_shadow_report(days=3650)
        models = {m["model"] for m in report["by_model"]}
        assert "groq/compound-mini" in models


class TestHealthReportsShadowState:
    """Flipping the flag in the host dashboard has to be verifiable."""

    def test_health_shows_the_flag_is_off(self):
        with patch.object(config, "ROUTER_SHADOW_ENABLED", False):
            payload = json.loads(app_module.health_check().body)
        assert payload["router_shadow"]["enabled"] is False
        assert payload["router_shadow"]["model"] is None

    def test_health_shows_the_model_and_a_live_count_when_on(self):
        with patch.object(config, "ROUTER_SHADOW_ENABLED", True), \
             patch.object(config, "GROQ_ROUTER_MODEL", "groq/compound-mini"), \
             patch.object(db, "router_shadow_pulse", return_value={
                 "compared_recently": 12, "agreement_rate": 0.75,
                 "last_comparison_at": "2026-09-08 02:00:00", "window_hours": 24,
             }):
            payload = json.loads(app_module.health_check().body)

        shadow = payload["router_shadow"]
        assert shadow["enabled"] is True
        assert shadow["model"] == "groq/compound-mini"
        assert shadow["compared_recently"] == 12

    def test_a_broken_pulse_query_never_takes_health_down_with_it(self):
        with patch.object(config, "ROUTER_SHADOW_ENABLED", True), \
             patch.object(db, "router_shadow_pulse", side_effect=RuntimeError("locked")):
            resp = app_module.health_check()
        payload = json.loads(resp.body)
        assert resp.status_code == 200
        assert payload["router_shadow"]["error"] == "RuntimeError"
