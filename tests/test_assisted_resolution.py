from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import camellia.app as app_module
import camellia.assisted_resolution as assisted
from camellia.config import config
from camellia.database import Database


class _ApprovedRag:
    def retrieve(self, message, *, persist_candidate=True):
        assert persist_candidate is False
        return SimpleNamespace(
            proposed_response="Jolbhora is available in our approved menu.",
            source_refs=["qa_templates.json#jolbhora"],
            source_chunks=["Jolbhora is available in our approved menu."],
            confidence_score=0.9,
            proposed_intent="jolbhora",
        )

    def grounding_report(self, answer, chunks):
        return True, "grounded"


@pytest.fixture
def isolated_assisted(monkeypatch, tmp_path):
    database = Database(str(tmp_path / "assisted.db"))
    monkeypatch.setattr(assisted, "db", database)
    monkeypatch.setattr(assisted, "rag_fallback_engine", _ApprovedRag())
    monkeypatch.setattr(config, "LLM_ASSISTED_RESOLUTION_ENABLED", True)
    return database


def test_assisted_resolution_is_disabled_by_default_and_whatsapp_is_blocked(monkeypatch):
    monkeypatch.setattr(config, "LLM_ASSISTED_RESOLUTION_ENABLED", False)
    assert assisted.create_suggestion(user_id="u", channel="instagram", customer_message="hello") is None
    monkeypatch.setattr(config, "LLM_ASSISTED_RESOLUTION_ENABLED", True)
    assert assisted.create_suggestion(user_id="u", channel="whatsapp", customer_message="hello") is None
    assert assisted.is_whatsapp_allowed("whatsapp") is False


def test_grounded_instagram_suggestion_is_deduplicated(isolated_assisted):
    first = assisted.create_suggestion(user_id="u", channel="instagram", customer_message="Tell me about Jolbhora")
    second = assisted.create_suggestion(user_id="u", channel="instagram", customer_message="Tell me about Jolbhora")
    assert first["id"] == second["id"]
    assert first["triage_category"] == "AUTO_RESOLVE"
    assert first["grounding_status"] == "Grounded"
    assert first["status"] == "PENDING_REVIEW"


def test_risky_request_remains_human_gated(isolated_assisted):
    item = assisted.create_suggestion(
        user_id="u2", channel="instagram",
        customer_message="I need 50 boxes for my wedding in Delhi",
    )
    assert item["triage_category"] == "AI_SUGGESTED"
    assert "event_date" in item["risk_flags"]


def test_authenticated_approval_is_the_only_send_path(monkeypatch, tmp_path):
    database = Database(str(tmp_path / "api.db"))
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(config, "ADMIN_PIN", "assisted-test-pin")
    sent = []
    monkeypatch.setattr(app_module, "send_instagram_reply", lambda user_id, text, image_url=None: sent.append((user_id, text)))
    suggestion_id = database.create_assisted_suggestion(
        user_id="u3", channel="instagram", customer_message="What is Jolbhora?",
        triage_category="AUTO_RESOLVE", suggested_answer="Jolbhora is a sweet.",
        original_suggested_answer="Jolbhora is a sweet.", grounding_status="Grounded",
    )
    client = TestClient(app_module.app)
    assert client.post(f"/admin/assisted-resolutions/{suggestion_id}/approve", json={}).status_code in {401, 403}
    response = client.post(
        f"/admin/assisted-resolutions/{suggestion_id}/approve",
        headers={"x-admin-pin": "assisted-test-pin"}, json={},
    )
    assert response.status_code == 200
    assert sent == [("u3", "Jolbhora is a sweet.")]
    assert database.get_assisted_suggestion(suggestion_id)["status"] == "APPROVED"
