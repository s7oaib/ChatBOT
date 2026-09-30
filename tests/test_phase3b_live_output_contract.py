"""End-to-end Instagram output contract tests for the Phase 3B remediation."""

import uuid
from unittest.mock import patch

import pytest

from camellia import app as app_module
from camellia import handoff as handoff_module
from camellia.ai_responder import ai_responder
from camellia.config import config
from camellia.content_integrity import (
    CLIENT_CONFIRMED_EXACT_RESPONSES,
    quarantine_template_response,
)
from camellia.database import Database
from camellia import responder as responder_module
from camellia.session_memory import session_memory


def _fixtures(tmp_path):
    return Database(str(tmp_path / "phase3b-live-output.db"))


def _run_instagram(monkeypatch, database, message, *, facts=None):
    """Run the production webhook worker and capture its outbound payloads."""
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(responder_module, "db", database)
    monkeypatch.setattr(handoff_module, "db", database)
    user_id = f"phase3b-output-{uuid.uuid4().hex}"
    session_memory.clear_session(user_id, "instagram")
    if facts:
        session_memory.get_facts(user_id, "instagram").update(facts)
    sent = []
    with patch.object(
        app_module,
        "send_instagram_reply",
        side_effect=lambda recipient, text, image_url=None, **kwargs: sent.append(
            {"recipient": recipient, "text": text, "image_url": image_url, **kwargs}
        ),
    ), patch.object(
        app_module.escalator, "handle_escalation", return_value={"escalation_id": 1}
    ) as escalated, patch.object(config, "WHATSAPP_BUSINESS_NUMBER", "919800000000"):
        app_module._process_instagram_event(user_id, message, "phase3b-output-ad")
    return sent, escalated


def _template_response(template_id):
    template = next(
        item for item in responder_module.responder.qa_data["templates"]
        if item["id"] == template_id
    )
    return template["response"]


def test_subscription_delivery_uses_ordered_approved_instagram_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    sent, escalated = _run_instagram(
        monkeypatch, _fixtures(tmp_path), "subscription delivery time"
    )

    # The approved QA answer intentionally sends text, its chart, then its
    # approved manager next-steps block as separate ordered Meta payloads.
    assert len(sent) == 3
    assert not escalated.called
    from camellia.client_qa import REGISTRY_BY_ID
    text_blocks = [item["text"] for item in sent if item["text"]]
    assert "\n\n".join(text_blocks) == REGISTRY_BY_ID["subscription_details_client"].answer
    assert sent[0]["image_url"] is None
    assert sent[1]["text"] == ""
    assert sent[1]["image_url"] == ["/images/Everyday%20Meal%20Subscription%20Rate%20Chart.jpeg"]
    assert "Manager of Kitchens" in sent[2]["text"]
    assert "45 minutes" not in " ".join(item["text"] for item in sent)
    assert "à la carte" not in " ".join(item["text"] for item in sent).lower()


def test_sondesh_dispatch_uses_exact_confirmed_copy_and_modified_copy_stays_quarantined(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    sent, escalated = _run_instagram(
        monkeypatch, _fixtures(tmp_path), "how long to deliver sondesh"
    )

    assert len(sent) == 1
    assert not escalated.called
    from camellia.client_qa import REGISTRY_BY_ID
    assert sent[0]["text"] == REGISTRY_BY_ID["sondesh_delivery_time"].answer
    assert sent[0]["image_url"] is None

    approved = CLIENT_CONFIRMED_EXACT_RESPONSES["sondesh_dispatch_time"]
    modified = approved + " Trusted dairies."
    safe = quarantine_template_response("sondesh_dispatch_time", modified)
    assert safe == approved
    assert "Trusted dairies" not in safe


@pytest.mark.parametrize(
    ("message", "template_id"),
    [
        ("Tell me about the Bengali Home Table", "bengali_home_table_details"),
        ("How much per person for the Bengali home lunch?", "bengali_home_table_pricing"),
        ("How do I pay for the Home Table?", "bengali_home_table_payment"),
    ],
)
def test_home_table_routes_reach_instagram_without_handoff(tmp_path, monkeypatch, message, template_id):
    sent, escalated = _run_instagram(monkeypatch, _fixtures(tmp_path), message)

    assert not escalated.called
    from camellia.client_qa import REGISTRY_BY_ID, HOME_TABLE_REEL_URLS, HOME_TABLE_VIDEO_ANCHOR
    source_id = {
        "bengali_home_table_details": "home_table_details",
        "bengali_home_table_pricing": "home_table_price",
        "bengali_home_table_payment": "home_table_payment",
    }[template_id]
    if source_id == "home_table_payment":
        assert len(sent) == 1
        assert sent[0]["text"] == REGISTRY_BY_ID[source_id].answer
    else:
        assert len(sent) == 6
        assert tuple(item["text"] for item in sent[1:5]) == HOME_TABLE_REEL_URLS
        assert sent[0]["text"].endswith(HOME_TABLE_VIDEO_ANCHOR)
        assert sent[0]["text"] + sent[5]["text"] == REGISTRY_BY_ID[source_id].answer
    assert all(item["image_url"] is None for item in sent)


def test_stale_always_human_source_policy_cannot_replace_new_informational_answer(
    tmp_path, monkeypatch
):
    stale_source = {
        "routing_id": 19,
        "source_type": "story",
        "active": True,
        "escalation_policy": "always_human",
    }
    for message in (
        "subscription delivery time",
        "how long to deliver sondesh",
        "Tell me about the Bengali Home Table",
        "How much per person for the Bengali home lunch?",
        "How do I pay for the Home Table?",
        "How can I contact your manager?",
    ):
        sent, escalated = _run_instagram(
            monkeypatch, _fixtures(tmp_path), message, facts={"source_route": stale_source}
        )
        if "contact your manager" in message.lower():
            assert escalated.called, message
            assert any("Manager of Kitchens" in item["text"] for item in sent), message
        else:
            assert sent, message
            assert not escalated.called, message
        assert all("wa.me" not in item["text"] for item in sent), message


@pytest.mark.parametrize("message", [
    "who can i speak to directly",
    "How can I contact your manager?",
])
def test_manager_contact_request_uses_human_handoff(tmp_path, monkeypatch, message):
    database = _fixtures(tmp_path)
    sent, escalated = _run_instagram(monkeypatch, database, message)
    assert len(sent) == 1
    assert escalated.called
    assert "Manager of Kitchens" in sent[0]["text"]
    assert "camellia.artisanal.gurgaon@gmail.com" not in sent[0]["text"]
    assert "wa.me" not in sent[0]["text"]


def test_ready_to_order_still_hands_off_after_informational_contact_fix(tmp_path, monkeypatch):
    database = _fixtures(tmp_path)
    sent, escalated = _run_instagram(monkeypatch, database, "finalise it")
    assert len(sent) == 2
    assert escalated.called
    assert any("wa.me" in payload["text"] for payload in sent)


def test_home_table_booking_remains_a_manager_handoff(tmp_path, monkeypatch):
    sent, escalated = _run_instagram(
        monkeypatch, _fixtures(tmp_path), "I want to book the Home Table"
    )
    assert len(sent) == 2
    assert escalated.called
    assert any("wa.me" in payload["text"] for payload in sent)


@pytest.mark.parametrize("message", [
    "what is this?",
    "tell me about this",
    "is this available?",
    "what does it contain?",
])
def test_vague_referential_instagram_messages_never_reuse_stale_media(tmp_path, monkeypatch, message):
    database = _fixtures(tmp_path)
    sent, escalated = _run_instagram(
        monkeypatch, database, message, facts={"clarify_sent": True, "last_topic": "sondesh"}
    )

    assert len(sent) == 1
    assert escalated.called
    assert sent[0]["image_url"] is None
    assert "Manager of Kitchens" in sent[0]["text"]
    assert "wa.me" not in sent[0]["text"]


def test_deterministic_instagram_output_does_not_require_groq(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    monkeypatch.setattr(config, "GROQ_ENABLED", True)
    monkeypatch.setattr(config, "GROQ_API_KEY", "429-test-key")
    monkeypatch.setattr(ai_responder, "get_active_provider", lambda: "none")
    database = _fixtures(tmp_path)

    for message in (
        "subscription delivery time",
        "how long to deliver sondesh",
        "Tell me about the Bengali Home Table",
        "How much per person for the Bengali home lunch?",
        "How do I pay for the Home Table?",
        "How can I contact your manager?",
    ):
        sent, escalated = _run_instagram(monkeypatch, database, message)
        assert sent, message
        if "contact your manager" in message.lower():
            assert escalated.called, message
        else:
            assert not escalated.called, message
        assert sent[0]["text"]
