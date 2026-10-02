import os
import json
import re
import hashlib
import hmac
import subprocess
import sys
import pytest
from unittest import mock
from fastapi.testclient import TestClient

from camellia.config import config
import camellia.database as db_module
from camellia.app import app
from camellia.responder import responder


def _ordinal(day: int) -> str:
    """'3' -> '3rd', matching how the client's flyers print their dates."""
    if 11 <= day % 100 <= 13:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }"


def _week_label(start_offset: int, end_offset: int) -> str:
    """
    A flyer-style "week_of" positioned relative to today, e.g.
    "28th August - 3rd September, 2026".

    These tests drive the real upload path, where menu_dates.is_expired
    compares the printed end date against today in IST. A hardcoded week is
    therefore a time bomb: it passes until that week ends and fails every run
    after. The dates are built relative to today_ist() — the same clock the
    production check uses — so "this week" stays this week.
    """
    from datetime import timedelta

    from camellia.menu_dates import today_ist

    today = today_ist()
    start = today + timedelta(days=start_offset)
    end = today + timedelta(days=end_offset)
    return (
        f"{_ordinal(start.day)} {start.strftime('%B')} - "
        f"{_ordinal(end.day)} {end.strftime('%B')}, {end.year}"
    )


def _current_week() -> str:
    """A week that is live today: started 2 days ago, ends in 4."""
    return _week_label(-2, 4)


def _next_week() -> str:
    """The week after _current_week(), so it is newer and also unexpired."""
    return _week_label(5, 11)

# Ensure database is initialized for test client
db_module.db.init_db()

client = TestClient(app)

TEST_ADMIN_API_KEY = "test_admin_api_key_98765_long_enough"
config.ADMIN_API_KEY = TEST_ADMIN_API_KEY
ADMIN_HEADERS = {"x-admin-key": TEST_ADMIN_API_KEY}

@pytest.fixture(autouse=True)
def ensure_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module.db, "db_path", str(tmp_path / "admin-portal.db"))
    db_module.db.init_db()
    config.ADMIN_API_KEY = TEST_ADMIN_API_KEY


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

def test_verify_pin_endpoint_is_retired():
    """The legacy PIN endpoint cannot authenticate or bypass sessions."""
    response = client.post("/admin/verify-pin", json={"pin": "anything"})
    assert response.status_code == 410

def test_admin_auth_fails_closed_when_unconfigured():
    """Ensures admin endpoints fail closed if machine auth is unconfigured."""
    orig_key = config.ADMIN_API_KEY
    try:
        config.ADMIN_API_KEY = ""
        
        # Protected route rejects the request; no legacy PIN bypass remains.
        resp = client.get("/admin/menu/active", headers={"x-admin-pin": "any_pin"})
        assert resp.status_code == 403
        
        # The retired endpoint remains unavailable regardless of configuration.
        resp_pin = client.post("/admin/verify-pin", json={"pin": "any_pin"})
        assert resp_pin.status_code == 410
    finally:
        config.ADMIN_API_KEY = orig_key

def test_admin_auth_protection():
    """Ensures protected endpoints reject unauthenticated requests with 403 Forbidden."""
    # Without headers
    resp = client.get("/admin/menu/active")
    assert resp.status_code == 403

    # With invalid PIN header
    resp = client.get("/admin/menu/active", headers={"x-admin-pin": "wrongpin"})
    assert resp.status_code == 403

    # With valid machine API-key header
    resp = client.get("/admin/menu/active", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json().get("status") == "success"


def test_admin_can_create_update_list_and_remove_an_ad_route():
    ad_id = "test-portal-ad-73491"
    db_module.db.delete_ad_product_route(ad_id)
    try:
        created = client.post(
            "/admin/ad-routes",
            headers=ADMIN_HEADERS,
            json={"ad_id": ad_id, "product": "savoury"},
        )
        assert created.status_code == 200
        assert created.json()["route"]["product"] == "savoury"

        updated = client.post(
            "/admin/ad-routes",
            headers=ADMIN_HEADERS,
            json={"ad_id": ad_id, "product": "bento"},
        )
        assert updated.status_code == 200

        listing = client.get("/admin/ad-routes", headers=ADMIN_HEADERS)
        assert listing.status_code == 200
        assert {"ad_id": ad_id, "product": "bento"} in [
            {"ad_id": route["ad_id"], "product": route["product"]}
            for route in listing.json()["routes"]
        ]

        removed = client.delete(f"/admin/ad-routes/{ad_id}", headers=ADMIN_HEADERS)
        assert removed.status_code == 200
        assert db_module.db.get_ad_product_route(ad_id) is None
    finally:
        db_module.db.delete_ad_product_route(ad_id)


def test_ad_route_rejects_unknown_products_and_requires_admin():
    payload = {"ad_id": "test-invalid-product", "product": "cakes"}
    assert client.post("/admin/ad-routes", json=payload).status_code == 403
    response = client.post("/admin/ad-routes", headers=ADMIN_HEADERS, json=payload)
    assert response.status_code == 400
    assert "Sondesh" in response.json()["detail"]

def test_parse_menu_card_text():
    before = open(config.MENU_JSON_PATH, encoding="utf-8").read()
    resp = client.post("/admin/parse-menu-card",
        json={"raw_text": "Veg:\nIlish Machher Jhol ₹520 (350g)\nLau Ghonto ₹260"},
        headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    draft = resp.json()["draft"]
    assert draft["status"] == "NEEDS_REVIEW"
    assert resp.json()["published"] is False
    assert len(draft["rows"]) >= 2
    assert open(config.MENU_JSON_PATH, encoding="utf-8").read() == before

def test_reset_menu_defaults():
    """Tests resetting menu items back to default Bengal cuisine baseline."""
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        original_menu = f.read()

    try:
        resp = client.post("/admin/menu/reset-defaults", headers=ADMIN_HEADERS)
        assert resp.status_code == 409
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
    assert client.delete(f"/admin/learned-knowledge/{knowledge_id}", headers=ADMIN_HEADERS).status_code == 400
    assert client.post(f"/admin/custom-qa/{knowledge_id}/status", json={"status": "ARCHIVED"}, headers=ADMIN_HEADERS).status_code == 200
    resp_del = client.request("DELETE", f"/admin/learned-knowledge/{knowledge_id}", headers=ADMIN_HEADERS,
                              json={"confirm_permanent_delete": True, "confirm_question": payload["question_pattern"]})
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
        assert body["status"] == "not_ready"
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

def test_a_quoted_dashboard_credential_still_works():
    """
    Dashboard machine credentials may be quoted; normalization preserves the
    existing API-key behavior without relying on a retired PIN setting.
    """
    from camellia.config import _env_secret

    # Both quote styles, since a dashboard preserves whichever was typed.
    with mock.patch.dict(os.environ, {"ADMIN_API_KEY": '"4821"'}):
        assert _env_secret("ADMIN_API_KEY") == "4821"
    with mock.patch.dict(os.environ, {"ADMIN_API_KEY": "'a-long-admin-key'"}):
        assert _env_secret("ADMIN_API_KEY") == "a-long-admin-key"

    # A quote on one side only is part of the value, not a wrapper.
    with mock.patch.dict(os.environ, {"ADMIN_API_KEY": '"4821'}):
        assert _env_secret("ADMIN_API_KEY") == '"4821'


def test_an_unquoted_credential_is_untouched():
    from camellia.config import _env_secret

    with mock.patch.dict(os.environ, {"ADMIN_API_KEY": " 4821 "}):
        assert _env_secret("ADMIN_API_KEY") == "4821"
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _env_secret("ADMIN_API_KEY") == ""


def test_dashboard_javascript_string_literals_are_not_broken_by_python():
    """
    The dashboard is a Python string containing JavaScript, so Python eats the
    backslashes before a browser sees them: a JS "\\n" arrives as a real newline
    and "\\'" as a bare quote. Either one terminates a JS string literal early,
    and a single occurrence makes the entire <script> fail to parse — so every
    function in the dashboard, verifyPin included, is never defined and the
    Unlock button does nothing at all, with no error shown to the user.

    This is invisible to any test that only greps the HTML for text, which is
    why it survived: the markup looked perfectly correct.
    """
    import inspect
    from camellia import app as app_module

    # The fix, pinned at its source: a raw string hands every backslash to the
    # browser untouched.
    source = inspect.getsource(app_module)
    assert 'html_content = r"""<!DOCTYPE html>' in source, (
        "the dashboard HTML must be a raw string, or Python consumes the "
        "backslashes in its JavaScript before the browser ever sees them"
    )

    # And the consequence, checked in what is actually served: these arrive as
    # the two-character escapes JavaScript needs, not as an expanded newline
    # and a bare quote that close the string early.
    page = client.get("/admin").text
    assert r"Approve this answer?\n\n" in page
    assert r"the bot\'s verified knowledge" in page

    # The literal must open and close on one line. It previously ran off the
    # end of its line, and that alone stopped the whole script parsing, so no
    # dashboard function was ever defined.
    opening = page.index("confirm('Approve this answer?")
    line = page[opening:page.index("\n", opening)]
    assert line.rstrip().endswith("return;"), (
        f"the confirm() literal does not close on its own line: {line[:120]!r}"
    )


def test_blocked_browser_storage_cannot_prevent_login():
    """
    Storage is a convenience, never part of authentication.

    sessionStorage throws when site data is blocked, in private browsing, and
    inside in-app browsers. The read was at top-level script scope, so one
    SecurityError aborted the whole script and verifyPin was never defined —
    the Unlock button did nothing, with no message. The write then ran before
    the overlay was hidden, so a failure there reported a correct PIN as a
    connection error and left the portal locked.
    """
    page = client.get("/admin").text

    assert "function checkAdminSession" in page
    assert "function loginAdmin" in page
    assert "sessionStorage" not in page
    assert "X-Admin-Key': currentAdminKey" not in page


def test_dashboard_waits_for_session_restore_and_guards_against_stale_checks():
    """A valid server session must not be obscured by the login overlay on refresh."""
    page = client.get("/admin").text

    assert 'id="authRestoring"' in page
    assert 'id="authCard" style="display:none"' in page
    assert "let authRequestGeneration = 0" in page
    assert "const requestGeneration = ++authRequestGeneration" in page
    assert "if (requestGeneration !== authRequestGeneration) return;" in page
    assert "function showAuthChecking" in page
    assert "function hideAuthOverlay" in page


def test_password_login_fields_do_not_expose_or_truncate_credentials():
    """
    The login box must not cap what can be typed into it.

    The browser login uses username/password fields and does not persist a
    password, PIN, or machine API key in browser storage.
    """
    page = client.get("/admin").text
    assert 'id="usernameInput"' in page
    assert 'id="passwordInput"' in page
    assert 'id="pinInput"' not in page


def test_login_distinguishes_rate_limiting_from_a_wrong_pin():
    """
    A rate-limited or unconfigured server must not be reported as a wrong
    password — that sends people hunting for a credential that was never the
    problem.
    """
    page = client.get("/admin").text
    assert "resp.status === 429" in page
    assert "resp.status === 503" in page


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
    Removed WhatsApp automation and an unverified webhook must be visible.
    """
    body = client.get("/health").json()
    assert "warnings" in body
    assert "reply_delivery" in body
    assert "webhook_signature" in body

    assert any("WhatsApp automation removed" in w for w in body["warnings"])
    assert body["reply_delivery"]["mode"] == "instagram_only"
    assert body["reply_delivery"]["manual_handoff"] == "available"

    # Mock mode is the correct default locally, so it must not fail the check.
    assert client.get("/health").status_code == 200


def test_webhook_signature_status_is_reported_honestly(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_WEBHOOK_SIGNATURE", False)
    monkeypatch.setattr(config, "META_APP_SECRET", "")
    assert client.get("/health").json()["webhook_signature"] == "DISABLED"
    monkeypatch.setattr(config, "REQUIRE_WEBHOOK_SIGNATURE", True)
    assert client.get("/health").json()["webhook_signature"] == "required_but_unconfigured"
    assert any(
        "META_APP_SECRET unset while signature enforcement is required" in warning
        for warning in client.get("/health").json()["warnings"]
    )
    monkeypatch.setattr(config, "META_APP_SECRET", "   ")
    assert client.get("/health").json()["webhook_signature"] == "required_but_unconfigured"
    monkeypatch.setattr(config, "META_APP_SECRET", "test-secret")
    assert client.get("/health").json()["webhook_signature"] == "enforced"


def test_secure_webhook_signature_default_when_environment_value_is_absent():
    env = os.environ.copy()
    env.pop("REQUIRE_WEBHOOK_SIGNATURE", None)
    code = (
        "import dotenv; dotenv.load_dotenv = lambda *args, **kwargs: None; "
        "from camellia.config import Config; "
        "print(str(Config.REQUIRE_WEBHOOK_SIGNATURE).lower())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip().endswith("true")


def _instagram_post_with_signature(signature=None):
    raw_body = json.dumps({"object": "instagram", "entry": []}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/webhook/instagram", content=raw_body, headers=headers)


def test_signed_instagram_webhook_passes_signature_gate(monkeypatch):
    secret = "test-secret"
    monkeypatch.setattr(config, "META_APP_SECRET", secret)
    monkeypatch.setattr(config, "REQUIRE_WEBHOOK_SIGNATURE", True)
    raw_body = json.dumps({"object": "instagram", "entry": []}).encode("utf-8")
    signature = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()

    response = client.post(
        "/webhook/instagram", content=raw_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )

    assert response.status_code == 200


@pytest.mark.parametrize("signature", [None, "sha256=incorrect"])
def test_missing_or_invalid_instagram_signature_is_rejected(monkeypatch, caplog, signature):
    monkeypatch.setattr(config, "META_APP_SECRET", "test-secret")
    monkeypatch.setattr(config, "REQUIRE_WEBHOOK_SIGNATURE", True)

    response = _instagram_post_with_signature(signature)

    assert response.status_code == 403
    assert "test-secret" not in response.text
    assert "test-secret" not in caplog.text


def test_missing_secret_with_enforcement_enabled_rejects_instagram_post(monkeypatch):
    monkeypatch.setattr(config, "META_APP_SECRET", "")
    monkeypatch.setattr(config, "REQUIRE_WEBHOOK_SIGNATURE", True)

    response = _instagram_post_with_signature()

    assert response.status_code == 403


def test_missing_secret_is_fail_open_only_after_explicit_local_opt_out(monkeypatch):
    monkeypatch.setattr(config, "META_APP_SECRET", "")
    monkeypatch.setattr(config, "REQUIRE_WEBHOOK_SIGNATURE", False)

    response = _instagram_post_with_signature()

    assert response.status_code == 200


def test_instagram_get_challenge_does_not_require_post_signature(monkeypatch):
    monkeypatch.setattr(config, "META_VERIFY_TOKEN", "unit-test-token")
    monkeypatch.setattr(config, "META_APP_SECRET", "test-secret")
    monkeypatch.setattr(config, "REQUIRE_WEBHOOK_SIGNATURE", True)

    response = client.get("/webhook/instagram", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "unit-test-token",
        "hub.challenge": "challenge-123",
    })

    assert response.status_code == 200
    assert response.text == "challenge-123"


@pytest.mark.parametrize(
    ("configured_token", "supplied_token"),
    [
        ("unit-test-token", "wrong-token"),
        ("", ""),
        ("   ", "   "),
    ],
    ids=["wrong-token", "empty-token", "whitespace-token"],
)
def test_instagram_get_challenge_rejects_untrusted_verify_tokens(
    monkeypatch, configured_token, supplied_token,
):
    monkeypatch.setattr(config, "META_VERIFY_TOKEN", configured_token)

    response = client.get("/webhook/instagram", params={
        "hub.mode": "subscribe",
        "hub.verify_token": supplied_token,
        "hub.challenge": "must-not-be-returned",
    })

    assert response.status_code == 403
    assert response.text == "Forbidden"


def test_meta_verify_token_defaults_to_unconfigured_when_environment_is_absent():
    env = os.environ.copy()
    env.pop("META_VERIFY_TOKEN", None)
    code = (
        "import dotenv; dotenv.load_dotenv = lambda *args, **kwargs: None; "
        "from camellia.config import Config; "
        "print(repr(Config.META_VERIFY_TOKEN))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env, capture_output=True, text=True, check=True,
    )

    assert result.stdout.strip().endswith("''")


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


def test_menu_photo_ocr_uses_the_groq_key_the_bot_already_has():
    """
    Menu photo OCR must not require a second provider account.

    The chat model has no vision, so this path used to demand a second
    provider's key and otherwise accepted the upload and silently extracted
    nothing. Groq serves multimodal models on the same key and endpoint the bot
    already uses for replies.
    """
    from camellia.menu_parser import menu_parser

    captured = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "vegetarian": [{"item": "Shukto", "description": "", "price_inr": 280, "quantity": "250g"}],
                "non_vegetarian": [],
            })}}]}

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["model"] = json["model"]
            captured["auth"] = headers["Authorization"]
            captured["content"] = json["messages"][0]["content"]
            return _Resp()

    from camellia import menu_parser as mp_module
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        original = f.read()
    try:
        with mock.patch.object(config, "GROQ_API_KEY", "gsk-test"), \
             mock.patch.object(config, "GROQ_ENABLED", True), \
             mock.patch.object(mp_module.httpx, "Client", lambda *a, **k: _Client()):
            result = menu_parser.parse_image_and_update_menu(b"\xff\xd8\xff-fake-jpeg", "image/jpeg")
    finally:
        with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
            f.write(original)
        responder.reload_templates()

    assert result.get("status") == "success", result
    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["auth"] == "Bearer gsk-test"
    # The image must travel as a base64 data URI, not a URL Meta-style host.
    image_part = [c for c in captured["content"] if c["type"] == "image_url"][0]
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_no_vision_key_names_groq_first_in_its_guidance():
    """The cheapest fix is the key the operator already has, so say that."""
    from camellia.menu_parser import menu_parser

    with mock.patch.object(config, "GROQ_API_KEY", ""):
        result = menu_parser.parse_image_and_update_menu(b"fake", "image/jpeg")

    assert result["status"] == "warning"
    assert "GROQ_API_KEY" in result["message"]


def test_uploaded_flyer_stays_private_until_review_approval():
    from io import BytesIO
    from PIL import Image
    from camellia import menu_parser, menu_review, menu_images
    original = open(config.MENU_JSON_PATH, encoding="utf-8").read()
    img = BytesIO()
    Image.new("RGB", (12, 12), "white").save(img, format="JPEG")
    payload = {"vegetarian": [{"item":"Shukto","quantity":"250g","price_inr":280}]}
    try:
        with mock.patch.object(menu_parser.menu_parser, "parse_image_and_update_menu",
            return_value={"status":"success","parsed":payload}):
            resp = client.post("/admin/menu/upload-image",
                files={"file":("flyer.jpg",img.getvalue(),"image/jpeg")}, headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        draft = resp.json()["draft"]
        assert draft["status"] == "NEEDS_REVIEW"
        assert db_module.db.get_menu_image_current("weekly_vegetarian") is None
        assert menu_review.current_versions() == []
        assert open(config.MENU_JSON_PATH, encoding="utf-8").read() == original
        assert menu_review.source_file("weekly_vegetarian",draft["draft_id"]).exists()
    finally:
        current = menu_review.get_draft("weekly_vegetarian")
        if current and current["status"] not in ("PUBLISHED","REJECTED"):
            menu_review.reject("weekly_vegetarian",current["draft_id"],current["revision"],"test")


def test_uploaded_flyer_caption_names_no_prices_or_dishes():
    """
    The caption sent with the uploaded flyer must stay generic.

    The vision extraction has not been reviewed by a human at the point this
    caption goes out, and a wrong price or dish name reaching a customer is
    worse than a generic line. The accurate list is sent separately as text,
    built straight from menu.json rather than repeated here.
    """
    from camellia import weekly_flyer

    for _ in range(20):
        state = weekly_flyer.save(b"fake-photo-bytes", "image/jpeg")
        caption = state["caption"].lower()
        assert "₹" not in caption
        assert not re.search(r"\d", caption), f"caption should name no prices: {caption!r}"
    weekly_flyer.clear()


def test_retired_reset_route_cannot_bypass_menu_review():
    """
    Legacy reset endpoint must not bypass the reviewed publication lifecycle.
    """
    from camellia import weekly_flyer

    weekly_flyer.save(b"fake-photo-bytes", "image/jpeg")
    assert weekly_flyer.get() is not None

    resp = client.post("/admin/menu/reset-defaults", headers=ADMIN_HEADERS)
    assert resp.status_code == 409
    assert weekly_flyer.get() is not None
    weekly_flyer.clear()


def test_no_upload_never_sends_baked_in_historical_weekly_image():
    """Without an eligible flyer, never send a baked-in historical week."""
    from camellia import weekly_flyer

    from datetime import date
    weekly_flyer.clear()
    with mock.patch("camellia.menu_dates.today_ist", return_value=date(2026, 6, 21)):
        reply = client.post(
            "/api/test-chat",
            json={"message": "show me the menu", "user_id": "no_flyer_user", "channel": "instagram_ad"},
        )
    images = reply.json().get("image_url") or []
    urls = [i["url"] if isinstance(i, dict) else i for i in images]
    assert not any("Till%20Thursday" in u or "From%20Friday" in u for u in urls)


def test_image_extraction_keeps_unapproved_printed_date_in_draft():
    from io import BytesIO
    from PIL import Image
    from camellia import menu_parser, menu_review
    img=BytesIO(); Image.new("RGB",(12,12),"white").save(img,format="PNG")
    week=_current_week()
    try:
        with mock.patch.object(menu_parser.menu_parser,"parse_image_and_update_menu",
            return_value={"status":"success","week_of":week,"service_window":"Friday to Thursday",
                "parsed":{"vegetarian":[{"item":"Shukto","quantity":"250g","price_inr":280}]}}):
            resp=client.post("/admin/menu/upload-image",
                files={"file":("flyer.png",img.getvalue(),"image/png")},headers=ADMIN_HEADERS)
        draft=resp.json()["draft"]
        assert draft["menu_metadata"]["week_of"] == week
        assert draft["status"] == "NEEDS_REVIEW"
        assert db_module.db.get_menu_image_current("weekly_vegetarian") is None
    finally:
        draft=menu_review.get_draft("weekly_vegetarian")
        if draft and draft["status"] not in ("PUBLISHED","REJECTED"):
            menu_review.reject("weekly_vegetarian",draft["draft_id"],draft["revision"],"test")


def test_failed_ocr_preserves_live_week_and_returns_reviewable_error():
    from io import BytesIO
    from PIL import Image
    from camellia import menu_parser, menu_review
    img=BytesIO(); Image.new("RGB",(12,12),"white").save(img,format="PNG")
    before=open(config.MENU_JSON_PATH,encoding="utf-8").read()
    try:
        with mock.patch.object(menu_parser.menu_parser,"parse_image_and_update_menu",
            return_value={"status":"failed","reason":"provider detail"}):
            resp=client.post("/admin/menu/upload-image",
                files={"file":("menu.png",img.getvalue(),"image/png")},headers=ADMIN_HEADERS)
        draft=resp.json()["draft"]
        assert draft["status"] == "NEEDS_REVIEW"
        assert "provider detail" not in str(draft)
        assert open(config.MENU_JSON_PATH,encoding="utf-8").read()==before
        assert db_module.db.get_menu_image_current("weekly_vegetarian") is None
    finally:
        draft=menu_review.get_draft("weekly_vegetarian")
        if draft and draft["status"] not in ("PUBLISHED","REJECTED"):
            menu_review.reject("weekly_vegetarian",draft["draft_id"],draft["revision"],"test")


def test_upload_does_not_replace_an_unreviewed_menu_draft():
    from io import BytesIO
    from PIL import Image
    from camellia import menu_parser, menu_review
    img=BytesIO(); Image.new("RGB",(12,12),"white").save(img,format="PNG")
    try:
        with mock.patch.object(menu_parser.menu_parser,"parse_image_and_update_menu",
            return_value={"status":"success","parsed":{"vegetarian":[{"item":"Shukto","price_inr":280}]}}):
            first=client.post("/admin/menu/upload-image",files={"file":("first.png",img.getvalue(),"image/png")},headers=ADMIN_HEADERS)
            second=client.post("/admin/menu/upload-image",files={"file":("second.png",img.getvalue(),"image/png")},headers=ADMIN_HEADERS)
        assert first.status_code==200
        assert second.status_code==409
        assert menu_review.current_versions()==[]
        assert db_module.db.get_menu_image_current("weekly_vegetarian") is None
    finally:
        draft=menu_review.get_draft("weekly_vegetarian")
        if draft and draft["status"] not in ("PUBLISHED","REJECTED"):
            menu_review.reject("weekly_vegetarian",draft["draft_id"],draft["revision"],"test")

def _set_current_week(week_of):
    """Rewrites the live week's printed date and hot-reloads the responder."""
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["weekly_menus"][-1]["week_of"] = week_of
    with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    responder.reload_data()


def test_printed_date_ranges_are_read_off_the_real_flyer_formats():
    """The formats the client's own printed flyers actually use."""
    from datetime import date
    from camellia.menu_dates import parse_week_end_date, is_expired

    today = date(2026, 9, 2)
    assert parse_week_end_date("24th July - 30th July, 2026", today=today) == date(2026, 7, 30)
    assert parse_week_end_date("28TH AUGUST - 3RD SEPTEMBER, 2026", today=today) == date(2026, 9, 3)
    # No year printed at all — resolved to the year nearest today.
    assert parse_week_end_date("19, 20, 21 JUNE", today=today) == date(2026, 6, 21)

    # The final day still counts as current: this menu is live all through the 3rd.
    assert is_expired("28th August - 3rd September, 2026", today=date(2026, 9, 3)) is False
    assert is_expired("28th August - 3rd September, 2026", today=date(2026, 9, 4)) is True


def test_a_yearless_flyer_does_not_come_back_to_life_in_january():
    """
    The annual resurrection. A yearless "24th July - 30th July" used to take
    the current year, so on 1 January it read as 30 July of the *new* year —
    seven months in the future, therefore never expired, and July's menu was
    served to customers again all the way to the following summer.
    """
    from datetime import date
    from camellia.menu_dates import is_expired, parse_week_end_date

    july = "24th July - 30th July"
    assert parse_week_end_date(july, today=date(2026, 7, 26)) == date(2026, 7, 30)
    assert is_expired(july, today=date(2026, 7, 26)) is False

    # New Year rolls over. The menu is six months stale and must stay expired.
    for day in (date(2027, 1, 1), date(2027, 3, 15), date(2027, 6, 30)):
        assert parse_week_end_date(july, today=day) == date(2026, 7, 30), day
        assert is_expired(july, today=day) is True, day


def test_a_yearless_flyer_spanning_new_year_is_still_current():
    """
    The other half of the same fix, and why it is not just "use last year":
    a menu printed in late December ends in January. Pinning it to the current
    year would date it to January *past*, expiring a menu that is live today.
    """
    from datetime import date
    from camellia.menu_dates import is_expired, parse_week_end_date

    span = "28th December - 3rd January"
    assert parse_week_end_date(span, today=date(2026, 12, 30)) == date(2027, 1, 3)
    assert is_expired(span, today=date(2026, 12, 30)) is False
    assert is_expired(span, today=date(2027, 1, 3)) is False
    assert is_expired(span, today=date(2027, 1, 4)) is True


def test_a_printed_year_is_always_obeyed_over_the_nearest_guess():
    """
    Nearest-year is a fallback for missing information, never an override. A
    flyer that prints 2026 means 2026 even when 2027 would land closer.
    """
    from datetime import date
    from camellia.menu_dates import parse_week_end_date

    assert parse_week_end_date(
        "24th July - 30th July, 2026", today=date(2027, 6, 1)
    ) == date(2026, 7, 30)


def test_unreadable_dates_never_expire_a_good_menu():
    """
    A parse failure must not hide a perfectly good menu from every customer —
    that is a far worse outcome than showing one slightly too long.
    """
    from camellia.menu_dates import is_expired, parse_week_end_date

    for text in ("Current Week (Friday Lunch - Thursday Dinner)",
                 "Friday Lunch - Thursday Dinner", "Current Rotation", "", None):
        assert parse_week_end_date(text) is None
        assert is_expired(text) is False

    # A day the month does not have means the read was wrong, not a real date.
    assert parse_week_end_date("31st September 2026") is None


def test_expired_menu_is_never_shown_to_a_customer():
    """No stale dishes, no stale prices, no stale photo."""
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        original = f.read()
    try:
        _set_current_week("19, 20, 21 JUNE")  # long past

        reply = client.post(
            "/api/test-chat",
            json={"message": "veg menu", "user_id": "expired_user", "channel": "instagram_ad"},
        ).json()

        text = reply.get("response", "")
        assert "Manager of Kitchens" in text
        assert "₹" not in text, "an expired menu must not quote prices a customer could order against"
        assert not (reply.get("image_url") or []), "an expired menu must not send a photo"
    finally:
        with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
            f.write(original)
        responder.reload_templates()


def test_expired_weekly_menu_does_not_hide_the_sondesh_collections():
    """
    Sondesh is a separate, undated range and stays correct. Browsing must still
    show it — only the weekly card drops out.
    """
    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        original = f.read()
    try:
        _set_current_week("19, 20, 21 JUNE")

        reply = client.post(
            "/api/test-chat",
            json={"message": "show me the menu", "user_id": "expired_browse", "channel": "instagram_ad"},
        ).json()
        urls = [i["url"] if isinstance(i, dict) else i for i in (reply.get("image_url") or [])]

        assert any("AAMAR" in u for u in urls)
        assert any("Signature" in u for u in urls)
        assert not any("Thursday" in u or "Friday" in u or "/uploads/" in u for u in urls)
    finally:
        with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as f:
            f.write(original)
        responder.reload_templates()


def test_new_upload_does_not_republish_an_expired_week_by_itself():
    from io import BytesIO
    from PIL import Image
    from camellia import menu_parser, menu_review
    img=BytesIO(); Image.new("RGB",(12,12),"white").save(img,format="PNG")
    original_menu = open(config.MENU_JSON_PATH, encoding="utf-8").read()
    _set_current_week("19, 20, 21 JUNE")
    before=open(config.MENU_JSON_PATH,encoding="utf-8").read()
    try:
        with mock.patch.object(menu_parser.menu_parser,"parse_image_and_update_menu",
            return_value={"status":"success","week_of":_current_week(),
                          "parsed":{"vegetarian":[{"item":"Shukto","price_inr":300}]}}):
            resp=client.post("/admin/menu/upload-image",
                files={"file":("flyer.png",img.getvalue(),"image/png")},headers=ADMIN_HEADERS)
        assert resp.status_code==200
        assert resp.json()["draft"]["status"]=="NEEDS_REVIEW"
        assert open(config.MENU_JSON_PATH,encoding="utf-8").read()==before
        assert db_module.db.get_menu_image_current("weekly_vegetarian") is None
    finally:
        draft=menu_review.get_draft("weekly_vegetarian")
        if draft and draft["status"] not in ("PUBLISHED","REJECTED"):
            menu_review.reject("weekly_vegetarian",draft["draft_id"],draft["revision"],"test")
        with open(config.MENU_JSON_PATH, "w", encoding="utf-8") as stream:
            stream.write(original_menu)
        responder.reload_data()


def test_expired_weeks_are_pruned_but_the_newest_always_survives():
    """
    Startup cleanup reaches menu.json on the volume, where the stale entries
    already live. It must never empty the array — the newest entry is the slot
    the next upload writes into, even when that entry has itself expired.
    """
    from camellia.menu_dates import prune_expired_weeks

    kept, removed = prune_expired_weeks([
        {"week_of": "24th July - 30th July, 2026"},
        {"week_of": "19, 20, 21 JUNE"},
        {"week_of": "4th September - 10th September, 2026"},
    ])
    assert [w["week_of"] for w in kept] == ["4th September - 10th September, 2026"]
    assert len(removed) == 2

    # Every entry expired: the newest still survives as the upload slot.
    kept, removed = prune_expired_weeks([
        {"week_of": "24th July - 30th July, 2026"},
        {"week_of": "19, 20, 21 JUNE"},
    ])
    assert [w["week_of"] for w in kept] == ["19, 20, 21 JUNE"]

    # A single entry is never removed, whatever its date.
    kept, removed = prune_expired_weeks([{"week_of": "24th July - 30th July, 2026"}])
    assert len(kept) == 1 and removed == []


def test_the_shipped_menu_carries_no_expired_weeks():
    """The outdated July menu is gone from the seed data and must stay gone."""
    from camellia.menu_dates import is_expired

    with open(config.MENU_JSON_PATH, "r", encoding="utf-8") as f:
        weekly = json.load(f)["weekly_menus"]
    stale = [w.get("week_of") for w in weekly[:-1] if is_expired(w.get("week_of"))]
    assert not stale, f"expired weekly menus still shipped in menu.json: {stale}"


def test_only_a_real_date_reaches_the_flyer_caption():
    """
    Found by uploading the client's actual flyer: the seed placeholder is the
    literal string "Current Week (Friday Lunch - Thursday Dinner)", and putting
    it where a date belongs produced "*This Week's Menu* (Current Week (Friday
    Lunch - Thursday Dinner))" — nested brackets, and still no date. A label
    that does not parse as a date is not one.
    """
    from camellia import weekly_flyer

    try:
        placeholder = weekly_flyer.save(
            b"fake", "image/jpeg", week_of="Current Week (Friday Lunch - Thursday Dinner)"
        )
        assert placeholder["caption"] in weekly_flyer.CAPTIONS
        assert "((" not in placeholder["caption"]
        assert "Current Week" not in placeholder["caption"]

        dated = weekly_flyer.save(
            b"fake", "image/jpeg", week_of="28TH AUGUST - 3RD SEPTEMBER, 2026"
        )
        assert "28TH AUGUST - 3RD SEPTEMBER, 2026" in dated["caption"]
    finally:
        weekly_flyer.clear()
