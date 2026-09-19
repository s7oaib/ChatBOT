"""Persistent-session contract tests for the final Phase 3B remediation."""

from contextlib import contextmanager
from unittest.mock import patch

from camellia import app as app_module
from camellia import handoff as handoff_module
from camellia.ai_responder import ai_responder
from camellia.config import config
from camellia.content_integrity import CLIENT_CONFIRMED_EXACT_RESPONSES
from camellia.database import Database
from camellia import responder as responder_module
from camellia.session_memory import session_memory


@contextmanager
def _run_same_user(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "phase3b-persistent.db"))
    user_id = "phase3b-persistent-fixed-user"
    session_memory.clear_session(user_id, "instagram")
    sent = []
    escalated = []

    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(responder_module, "db", database)
    monkeypatch.setattr(handoff_module, "db", database)

    def capture(recipient, text, image_url=None, **kwargs):
        sent.append({"recipient": recipient, "text": text, "image_url": image_url, **kwargs})

    with patch.object(app_module, "send_instagram_reply", side_effect=capture), \
        patch.object(
            app_module.escalator,
            "handle_escalation",
            side_effect=lambda **kwargs: escalated.append(kwargs),
        ), \
        patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        yield database, user_id, sent, escalated


def _send(database, user_id, message):
    before = database.get_conversations_count(user_id=user_id, channel="instagram")
    app_module._process_instagram_event(user_id, message, "phase3b-persistent-ad")
    after = database.get_conversations_count(user_id=user_id, channel="instagram")
    assert after == before + 1
    rows = database.get_conversations(limit=1, user_id=user_id, channel="instagram")
    return rows[0]


def test_same_user_phase3b_sequence_preserves_explicit_subject_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    with _run_same_user(tmp_path, monkeypatch) as (database, user_id, sent, escalated):
        cases = [
            ("Tell me about the Bengali Home Table", "bengali_home_table_details"),
            ("subscription delivery time", "subscription_details"),
            ("how long to deliver sondesh", "sondesh_dispatch_time"),
            ("who can i speak to directly", "GUARD_staff_names_manager_contact"),
            ("Tell me about the Bengali Home Table", "bengali_home_table_details"),
            ("How much per person for the Bengali home lunch?", "bengali_home_table_pricing"),
            ("How do I pay for the Home Table?", "bengali_home_table_payment"),
        ]
        for message, expected in cases:
            row = _send(database, user_id, message)
            assert row["template_id"] == expected, message
            assert len(sent) == len(database.get_conversations(user_id=user_id, channel="instagram"))

        row = _send(database, user_id, "I want to book the Home Table")
        assert row["template_id"] == "instagram_whatsapp_handoff"
        assert len(escalated) == 1
        assert len(sent) == 9

        row = _send(database, user_id, "Tell me about the subscription")
        assert row["template_id"] == "waiting_for_human"
        assert len(sent) == 9
        waiting = database.get_active_handoff("instagram", user_id)
        assert waiting is not None
        database.resume_handoff(waiting["handoff_ref"])

        row = _send(database, user_id, "Tell me about the subscription")
        assert row["template_id"] == "subscription_details"
        row = _send(database, user_id, "Actually I want Bengali Bento")
        assert row["template_id"] in {"bento_details", "combo_bento"}
        bento_media_count = len(sent[-1].get("image_url") or []) if isinstance(sent[-1].get("image_url"), list) else int(bool(sent[-1].get("image_url")))
        assert bento_media_count >= 1

        row = _send(database, user_id, "price?")
        assert row["template_id"] in {"bento_details", "combo_bento", "bento_price"}
        assert "₹" in row["bot_response"]
        assert sent[-1]["image_url"]

        for message in ("what is this?", "tell me about this"):
            row = _send(database, user_id, message)
            assert row["template_id"] == "clarify_request"
            assert sent[-1]["image_url"] is None

        row = _send(database, user_id, "What is the sondesh price?")
        assert "sondesh" in row["template_id"]
        row = _send(database, user_id, "Do you also cook meals?")
        assert row["template_id"] == "sondesh_crosssell_savoury"
        assert sent[-1]["text"] == CLIENT_CONFIRMED_EXACT_RESPONSES["sondesh_crosssell_savoury"]
        assert sent[-1]["image_url"]

        before = len(sent)
        row = _send(database, user_id, "what meals do you cook?")
        assert len(sent) == before + 1
        assert CLIENT_CONFIRMED_EXACT_RESPONSES["sondesh_crosssell_savoury"] not in sent[-1]["text"]
        assert all(payload.get("response_type") != "duplicate" for payload in sent)


def test_stale_always_human_context_cannot_override_canonical_subjects(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    with _run_same_user(tmp_path, monkeypatch) as (database, user_id, sent, escalated):
        facts = session_memory.get_facts(user_id, "instagram")
        facts["source_route"] = {"active": True, "escalation_policy": "always_human"}
        facts["home_table_context"] = True
        for message, expected in (
            ("subscription delivery time", "subscription_details"),
            ("how long to deliver sondesh", "sondesh_dispatch_time"),
            ("Tell me about the Bengali Home Table", "bengali_home_table_details"),
            ("How much per person for the Bengali home lunch?", "bengali_home_table_pricing"),
            ("How do I pay for the Home Table?", "bengali_home_table_payment"),
            ("who can i speak to directly", "GUARD_staff_names_manager_contact"),
        ):
            row = _send(database, user_id, message)
            assert row["template_id"] == expected
            assert "wa.me" not in sent[-1]["text"]
        assert not escalated


def test_confirmed_template_is_canonicalized_before_instagram_last_mile(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    with _run_same_user(tmp_path, monkeypatch) as (database, user_id, sent, escalated):
        database.save_response_template_override(
            "sondesh_dispatch_time",
            CLIENT_CONFIRMED_EXACT_RESPONSES["sondesh_dispatch_time"] + " Trusted dairies.",
            updated_by="test",
            change_reason="unsafe-test-copy",
        )
        row = _send(database, user_id, "how long to deliver sondesh")
        assert row["template_id"] == "sondesh_dispatch_time"
        assert sent[-1]["text"] == CLIENT_CONFIRMED_EXACT_RESPONSES["sondesh_dispatch_time"]
        assert "Trusted dairies" not in sent[-1]["text"]
        assert not escalated


def test_phase3b_deterministic_transitions_do_not_require_groq(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GROQ_ENABLED", True)
    monkeypatch.setattr(config, "GROQ_API_KEY", "429-test-key")
    monkeypatch.setattr(ai_responder, "get_active_provider", lambda: "none")
    with _run_same_user(tmp_path, monkeypatch) as (database, user_id, sent, escalated):
        for message, expected in (
            ("Tell me about the Bengali Home Table", "bengali_home_table_details"),
            ("subscription delivery time", "subscription_details"),
            ("how long to deliver sondesh", "sondesh_dispatch_time"),
            ("who can i speak to directly", "GUARD_staff_names_manager_contact"),
        ):
            row = _send(database, user_id, message)
            assert row["template_id"] == expected
            assert sent[-1]["text"]
        assert not escalated
