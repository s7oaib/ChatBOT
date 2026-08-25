import os
import json
import pytest
from fastapi.testclient import TestClient

from camellia.config import config
import camellia.database as db_module
from camellia.app import app
from camellia.responder import responder

# Ensure database is initialized for test client
db_module.db.init_db()

client = TestClient(app)

TEST_ADMIN_PIN = "test_pin_98765"
config.ADMIN_PIN = TEST_ADMIN_PIN
ADMIN_HEADERS = {"x-admin-pin": TEST_ADMIN_PIN}

@pytest.fixture(autouse=True)
def ensure_db():
    db_module.db.init_db()
    config.ADMIN_PIN = TEST_ADMIN_PIN


def test_admin_dashboard_page():
    """Verifies that the /admin and /admin/dashboard pages return HTML successfully."""
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Kitchen & Menu Admin Portal" in resp.text
    assert "Kitchen Admin Portal" in resp.text and "Unlock Portal" in resp.text

    resp_dash = client.get("/admin/dashboard")
    assert resp_dash.status_code == 200
    assert resp_dash.text == resp.text

def test_verify_pin_endpoint():
    """Tests the PIN validation API endpoint."""
    # Correct PIN
    resp = client.post("/admin/verify-pin", json={"pin": TEST_ADMIN_PIN})
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("valid") is True

    # Incorrect PIN
    resp_bad = client.post("/admin/verify-pin", json={"pin": "wrong_pin_00000"})
    assert resp_bad.status_code == 401
    assert "detail" in resp_bad.json()

def test_admin_auth_fails_closed_when_unconfigured():
    """Ensures /admin endpoints return 503 Service Unavailable if no ADMIN_PIN or ADMIN_API_KEY is configured in env."""
    orig_pin = config.ADMIN_PIN
    orig_key = config.ADMIN_API_KEY
    try:
        config.ADMIN_PIN = ""
        config.ADMIN_API_KEY = ""
        
        # Protected route fails closed with 503
        resp = client.get("/admin/menu/active", headers={"x-admin-pin": "any_pin"})
        assert resp.status_code == 503
        assert "disabled" in resp.json().get("detail", "").lower()
        
        # Verify PIN endpoint also fails closed with 503
        resp_pin = client.post("/admin/verify-pin", json={"pin": "any_pin"})
        assert resp_pin.status_code == 503
        assert "disabled" in resp_pin.json().get("detail", "").lower()
    finally:
        config.ADMIN_PIN = orig_pin
        config.ADMIN_API_KEY = orig_key

def test_admin_auth_protection():
    """Ensures protected endpoints reject unauthenticated requests with 403 Forbidden."""
    # Without headers
    resp = client.get("/admin/menu/active")
    assert resp.status_code == 403

    # With invalid PIN header
    resp = client.get("/admin/menu/active", headers={"x-admin-pin": "wrongpin"})
    assert resp.status_code == 403

    # With valid PIN header
    resp = client.get("/admin/menu/active", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json().get("status") == "success"

def test_parse_menu_card_text():
    """Tests raw text parsing and hot-reloading through the admin API."""
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        original_menu = f.read()

    try:
        test_text = """
        Weekend Special:
        * Ilish Machher Jhol - ₹520 (350g)
        * Lau Ghonto with Bori - ₹260 (250g)
        """
        resp = client.post("/admin/parse-menu-card", json={"raw_text": test_text}, headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "success"
        assert data.get("updated_items_count", 0) >= 2
    finally:
        with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
            f.write(original_menu)
        responder.reload_templates()

def test_reset_menu_defaults():
    """Tests resetting menu items back to default Bengal cuisine baseline."""
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        original_menu = f.read()

    try:
        resp = client.post("/admin/menu/reset-defaults", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "success"

        # Verify active menu shows defaults
        resp_active = client.get("/admin/menu/active", headers=ADMIN_HEADERS)
        assert resp_active.status_code == 200
        menu = resp_active.json().get("menu", {})
        last_week = menu.get("weekly_menus", [])[-1]
        veg_names = [item["item"] for item in last_week.get("vegetarian", [])]
        nonveg_names = [item["item"] for item in last_week.get("non_vegetarian", [])]

        assert "Chholar Daal" in veg_names
        assert "Mangsho (Mutton Kosha)" in nonveg_names
    finally:
        with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
            f.write(original_menu)
        responder.reload_templates()

def test_admin_analytics_and_escalations():
    """Tests the analytics summary and escalations list endpoints."""
    # Analytics
    resp_stats = client.get("/admin/analytics", headers=ADMIN_HEADERS)
    assert resp_stats.status_code == 200
    stats = resp_stats.json()
    assert "total_messages" in stats
    assert "matched_responses" in stats
    assert "match_rate_percent" in stats

    # Escalations
    resp_esc = client.get("/admin/escalations", headers=ADMIN_HEADERS)
    assert resp_esc.status_code == 200
    esc_data = resp_esc.json()
    assert "escalations" in esc_data
    assert isinstance(esc_data["escalations"], list)

def test_menu_and_qa_schema_validity():
    """Validates structural integrity of menu.json and qa_templates.json."""
    from scripts.validate_schemas import validate_menu_schema, validate_qa_templates_schema
    # No argument: both validators default to the same resolved paths the bot
    # loads, so this cannot drift from where the files actually live.
    menu_ok, menu_errs = validate_menu_schema()
    assert menu_ok, f"menu.json schema errors: {menu_errs}"

    qa_ok, qa_errs = validate_qa_templates_schema()
    assert qa_ok, f"qa_templates.json schema errors: {qa_errs}"


def test_admin_conversations_endpoint():
    """Verifies that the /admin/conversations endpoint retrieves logged conversations."""
    # Log a test interaction
    db_module.db.log_conversation(
        user_id="test_user_conv_123",
        channel="instagram",
        user_message="Do you deliver Sondesh to Salt Lake?",
        bot_response="Yes! We deliver across Kolkata including Salt Lake.",
        was_matched=True
    )

    resp = client.get("/admin/conversations?limit=10", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert "conversations" in data
    assert len(data["conversations"]) > 0

    # Test filtering by channel
    resp_insta = client.get("/admin/conversations?channel=instagram", headers=ADMIN_HEADERS)
    assert resp_insta.status_code == 200
    assert "conversations" in resp_insta.json()


def test_admin_learned_knowledge_crud_and_live_bot_matching():
    """Tests creating, querying, matching, and deleting learned knowledge."""
    # 1. Create a learned knowledge item
    payload = {
        "topic": "Corporate Diwali Hampers",
        "question_pattern": "diwali bulk gifting hampers",
        "verified_answer": "We offer customized artisanal Bengal sweet hampers for Diwali with personalized branding.",
        "source": "admin_test"
    }
    resp_create = client.post("/admin/learned-knowledge", json=payload, headers=ADMIN_HEADERS)
    assert resp_create.status_code == 200
    create_data = resp_create.json()
    assert create_data.get("status") == "success"
    knowledge_id = create_data.get("id")
    assert knowledge_id is not None

    # 2. List learned knowledge
    resp_list = client.get("/admin/learned-knowledge", headers=ADMIN_HEADERS)
    assert resp_list.status_code == 200
    items = resp_list.json().get("learned_knowledge", [])
    assert any(k.get("topic") == "Corporate Diwali Hampers" for k in items)

    # 3. Verify bot matches the question via responder
    from camellia.responder import responder
    bot_resp = responder.respond("Do you have diwali bulk gifting hampers?")
    assert "customized artisanal Bengal sweet hampers" in bot_resp.text

    # 4. Delete learned knowledge item
    resp_del = client.delete(f"/admin/learned-knowledge/{knowledge_id}", headers=ADMIN_HEADERS)
    assert resp_del.status_code == 200
    assert resp_del.json().get("status") == "deleted"


def test_admin_escalation_resolve_and_learn_flow():
    """Tests resolving an escalation and automatically converting it into learned knowledge."""
    # 1. Log an escalation
    esc_id = db_module.db.record_escalation(
        user_id="user_esc_789",
        channel="whatsapp",
        user_message="Can you pack sweets in tin boxes for flight travel to London?",
        reason="unmatched_flight_packing"
    )

    # Get latest escalations
    resp_esc = client.get("/admin/escalations", headers=ADMIN_HEADERS)
    assert resp_esc.status_code == 200
    escalations = resp_esc.json().get("escalations", [])
    assert len(escalations) > 0

    # 2. Resolve and teach bot
    resolve_payload = {
        "verified_answer": "Yes, we provide airtight hermetically sealed tin containers ideal for international flights.",
        "status": "resolved"
    }
    resp_resolve = client.post(f"/admin/escalations/{esc_id}/resolve", json=resolve_payload, headers=ADMIN_HEADERS)
    assert resp_resolve.status_code == 200
    assert resp_resolve.json().get("status") == "resolved"

    # 3. Verify escalation is now marked resolved
    resp_esc_after = client.get("/admin/escalations", headers=ADMIN_HEADERS)
    resolved_esc = next((e for e in resp_esc_after.json()["escalations"] if e["id"] == esc_id), None)
    assert resolved_esc is not None
    assert resolved_esc["status"] == "resolved"




def test_health_reports_healthy_when_the_bot_can_answer():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["answering"]["ready"] is True
    assert body["answering"]["templates_loaded"] > 10
    assert body["answering"]["problems"] == []


def test_health_fails_when_no_templates_are_loaded():
    """
    A deployment once reported healthy while holding zero templates, so every
    customer got the fallback and nothing noticed. Health must fail on that.
    """
    from camellia.responder import responder

    saved = responder.qa_data
    responder.qa_data = {}
    try:
        resp = client.get("/health")
        assert resp.status_code == 503, "A bot with no templates must not report healthy"
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["answering"]["ready"] is False
        assert any("template" in p.lower() for p in body["answering"]["problems"])
    finally:
        responder.qa_data = saved

    # And recovers once the templates are back.
    assert client.get("/health").status_code == 200


def test_health_fails_when_the_menu_is_empty():
    from camellia.responder import responder

    saved = responder.menu_data
    responder.menu_data = {}
    try:
        resp = client.get("/health")
        assert resp.status_code == 503
        assert any("menu" in p.lower() for p in resp.json()["answering"]["problems"])
    finally:
        responder.menu_data = saved

    assert client.get("/health").status_code == 200
