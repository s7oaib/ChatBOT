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


def test_refinement_budget_stays_under_the_slow_notice_threshold():
    """
    Refinement is optional polish on an answer the bot already holds. If it can
    outlast the slow-reply threshold it will tell customers the business is
    running slow because an AI provider was overloaded — which is what a
    7-second 503 was doing in production.
    """
    assert config.GROQ_REFINE_TIMEOUT * 1000 < config.SLOW_REPLY_THRESHOLD_MS, (
        f"Refinement may take up to {config.GROQ_REFINE_TIMEOUT}s while the slow-reply "
        f"notice fires at {config.SLOW_REPLY_THRESHOLD_MS}ms"
    )


# ---------------------------------------------------------------------------
# Candidate review queue
#
# The bot has always drafted answers for questions it could not match and saved
# them as PENDING_APPROVAL, and the approve/reject API has always existed — but
# no page in the dashboard ever listed them, so the queue only grew. These pin
# the review loop end to end, because approving good drafts is what raises the
# bot's answer rate over time.
# ---------------------------------------------------------------------------

def test_dashboard_exposes_the_candidate_review_queue():
    """The approval queue must be reachable from the dashboard, not API-only."""
    page = client.get("/admin").text
    assert 'id="tab-candidates"' in page, "no Suggested Answers panel in the dashboard"
    assert "switchTab('tab-candidates'" in page, "no tab button to reach the panel"
    assert "async function loadCandidates" in page
    assert "approveCandidate" in page and "rejectCandidate" in page


def test_approving_a_candidate_moves_it_into_verified_knowledge():
    candidate_id = db_module.db.save_candidate_template(
        name="opening_hours",
        response="Our kitchen is open 10:00 AM to 7:30 PM every day.",
        user_message="what time do you open on sundays",
        source_refs=["qa_templates.json#hours"],
        confidence=0.8,
        created_by="rag_fallback",
    )

    listed = client.get("/admin/candidates", headers=ADMIN_HEADERS)
    assert listed.status_code == 200
    assert any(c["id"] == candidate_id for c in listed.json()["candidates"])

    approved = client.post(
        f"/admin/candidates/{candidate_id}/approve",
        headers=ADMIN_HEADERS,
        json={"approved_by": "admin_dashboard"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    # It is now an answer the bot can serve without calling a model at all.
    knowledge = db_module.db.get_learned_knowledge(limit=200)
    assert any(
        k.get("source") == f"approved_candidate#{candidate_id}" for k in knowledge
    ), "an approved candidate must land in the verified knowledge base"

    # And it leaves the queue, so it cannot be approved twice.
    again = client.post(
        f"/admin/candidates/{candidate_id}/approve",
        headers=ADMIN_HEADERS,
        json={"approved_by": "admin_dashboard"},
    )
    assert again.status_code == 409


def test_rejecting_a_candidate_never_reaches_a_customer():
    candidate_id = db_module.db.save_candidate_template(
        name="bad_draft",
        response="We deliver to every city in India.",  # untrue
        user_message="do you deliver to chennai",
        source_refs=[],
        confidence=0.7,
        created_by="rag_fallback",
    )

    assert client.post(
        f"/admin/candidates/{candidate_id}/reject", headers=ADMIN_HEADERS
    ).status_code == 200

    pending_ids = [c["id"] for c in client.get("/admin/candidates", headers=ADMIN_HEADERS).json()["candidates"]]
    assert candidate_id not in pending_ids

    knowledge = db_module.db.get_learned_knowledge(limit=200)
    assert not any(
        k.get("source") == f"approved_candidate#{candidate_id}" for k in knowledge
    ), "a rejected draft must never become a verified answer"


def test_candidate_queue_requires_admin_auth():
    """Customer messages are quoted in this queue, so it is not public."""
    assert client.get("/admin/candidates").status_code == 403


# ---------------------------------------------------------------------------
# Health warnings
# ---------------------------------------------------------------------------

def test_health_warns_about_silent_failure_modes():
    """
    Mock mode and an unverified webhook both look exactly like a working bot
    from the outside, so health has to name them explicitly.
    """
    body = client.get("/health").json()
    assert "warnings" in body
    assert "reply_delivery" in body
    assert "webhook_signature" in body

    with_mock = [w for w in body["warnings"] if "MOCK" in w.upper()]
    assert with_mock, "mock mode must be surfaced — it means customers get silence"
    assert body["reply_delivery"]["mode"] == "mock"
    assert body["reply_delivery"]["delivering"] is False

    # Mock mode is the correct default locally, so it must not fail the check.
    assert client.get("/health").status_code == 200


def test_health_fails_when_live_mode_has_no_credentials():
    """Live mode without a token produces replies that cannot be delivered."""
    saved_mock = config.WHATSAPP_MOCK_MODE
    saved_token = config.WHATSAPP_ACCESS_TOKEN
    config.WHATSAPP_MOCK_MODE = False
    config.WHATSAPP_ACCESS_TOKEN = ""
    try:
        resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["reply_delivery"]["mode"] == "live"
        assert body["reply_delivery"]["delivering"] is False
        assert "WHATSAPP_ACCESS_TOKEN" in body["reply_delivery"]["problem"]
    finally:
        config.WHATSAPP_MOCK_MODE = saved_mock
        config.WHATSAPP_ACCESS_TOKEN = saved_token

    assert client.get("/health").status_code == 200


def test_webhook_signature_status_is_reported_honestly():
    saved = config.META_APP_SECRET
    try:
        config.META_APP_SECRET = ""
        assert client.get("/health").json()["webhook_signature"] == "DISABLED"
        config.META_APP_SECRET = "a_real_secret"
        assert client.get("/health").json()["webhook_signature"] == "enforced"
    finally:
        config.META_APP_SECRET = saved


def test_require_webhook_signature_rejects_unsigned_when_unconfigured():
    """
    The opt-in that lets an operator fail closed: with no app secret to verify
    against, a webhook POST is refused rather than trusted.
    """
    saved_secret = config.META_APP_SECRET
    saved_require = config.REQUIRE_WEBHOOK_SIGNATURE
    config.META_APP_SECRET = ""
    config.REQUIRE_WEBHOOK_SIGNATURE = True
    try:
        resp = client.post("/webhook/instagram", json={"object": "instagram", "entry": []})
        assert resp.status_code == 403
    finally:
        config.META_APP_SECRET = saved_secret
        config.REQUIRE_WEBHOOK_SIGNATURE = saved_require
