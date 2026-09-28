"""Operator semantics remain read-only and independent of notification reads."""
from fastapi.testclient import TestClient
import re
import shutil
import subprocess

import pytest

from camellia import app as app_module
from camellia.config import config
from camellia.database import Database


def test_seen_waiting_records_count_as_actionable_not_history(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "operator.sqlite"))
    database.init_db()
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(config, "ADMIN_API_KEY", "operator-test")
    for index in range(32):
        ref = f"CAM-operator-{index}"
        database.create_handoff(handoff_ref=ref, source_channel="instagram",
                                source_user_id=f"fixture-{index}", reason="Human requested",
                                summary="", known_entities={}, unresolved_topic=None,
                                escalation_id=None, expires_at=None)
        database.update_handoff_status(ref, "WAITING_FOR_HUMAN")
        database.mark_admin_handoff_seen(ref)
    database.resume_handoff("CAM-operator-0")
    database.update_handoff_status("CAM-operator-1", "RESOLVED")
    with database.get_connection() as connection:
        connection.execute("UPDATE cross_channel_handoffs SET expires_at='2000-01-01 00:00:00' WHERE handoff_ref='CAM-operator-2'")
        connection.commit()
    client = TestClient(app_module.app)
    headers = {"x-admin-key": "operator-test"}
    result = client.get("/admin/handoffs?limit=5", headers=headers).json()
    assert result["actionable_count"] == 29
    assert result["historical_count"] == 3
    assert result["unread_count"] == 0
    assert len(result["handoffs"]) == 5
    assert all(item["actionable"] for item in result["handoffs"])
    history = client.get("/admin/handoffs?include_resumed=true", headers=headers).json()
    assert history["actionable_count"] == 29
    assert sum(not item["actionable"] for item in history["handoffs"]) == 3
    assert database.get_handoff_by_ref("CAM-operator-0")["status"] == "RESUMED"
    assert database.get_handoff_by_ref("CAM-operator-2")["status"] == "WAITING_FOR_HUMAN"


@pytest.fixture
def page():
    return TestClient(app_module.app).get("/admin").text


def test_dashboard_and_handoffs_have_same_actionable_semantics(page):
    assert "handoffData.actionable_count" in page
    assert "Needs attention" in page
    assert 'id="handoffHistory"' in page
    assert "historical_count" in page
    assert "seen is not resolved" in page
    assert "active handoff(s)" not in page


def test_timestamp_formats_stored_utc_and_offset_without_fake_recency(page):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    function = re.search(r"function formatAdminTimestamp\(value\) \{.*?\n        \}", page, re.S).group()
    script = function + """
    const assert = require('assert');
    assert.equal(formatAdminTimestamp(null), 'Timestamp unavailable');
    assert.equal(formatAdminTimestamp('not-a-date'), 'Timestamp unavailable');
    const utc = formatAdminTimestamp('2026-09-28 20:32:14');
    assert.ok(utc.includes('2026'));
    assert.equal(utc, formatAdminTimestamp('2026-09-28T20:32:14Z'));
    assert.equal(utc, formatAdminTimestamp('2026-09-29T02:02:14+05:30'));
    """
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    assert "formatAdminTimestamp(e.created_at || e.timestamp)" in page
    assert "Latest Escalations — History" in page
    assert "e.timestamp || 'Recent'" not in page


def test_viewing_handoff_has_no_mutation_and_resume_is_explicit(page):
    opening = page.split("function openHandoffDetails(ref)", 1)[1].split("let adminResumeBusy", 1)[0]
    assert "markHandoffSeen(ref);" not in opening
    assert "fetch(" not in opening
    assert "Mark notification as read" in opening
    assert "Resume AI" in opening
    assert "AI is paused" in opening
    assert "adminResumeBusy || !confirm" in page
    assert "buttons.forEach(button => button.disabled = true)" in page


def test_candidate_history_and_action_guards(page):
    assert "Pending drafts are not live." in page
    assert "approved answers may already have been used for customers" in page
    assert "Nothing on this page has been shown to a customer" not in page
    assert "No records in the selected decision history." in page
    assert 'id="candidateStatusFilter"' in page
    for status in ("PENDING_APPROVAL", "APPROVED", "REJECTED"):
        assert f'value="{status}"' in page
    assert "Read-only decision history" in page
    assert "formatAdminTimestamp(c.created_at)" in page
    assert "Check price, availability, portion and shelf-life facts" in page
    assert "candidateActionsBusy.has(id)" in page
    assert "setCandidateBusy(id, true)" in page
    assert "finally { setCandidateBusy(id, false); }" in page
    assert "Could not approve: " not in page.split("async function approveCandidate(id)", 1)[1].split("function beginCandidateEdit", 1)[0]


def test_classic_menu_authority_and_mobile_structure_remain(page):
    for text in ("Weekly Savoury Menu", "Menu Catalogue", "Approve &amp; Publish", "Restore to review draft", "Current Menu", "Versions"):
        assert text in page
    assert 'id="mrCatalogueTable"' not in page
    assert "@media (max-width: 660px)" in page
    assert "overflow-x: auto" in page
    assert "This cannot be undone" in page


def test_resume_and_candidate_actions_serialize_requests_and_restore_controls(page):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    resume = page.split("let adminResumeBusy", 1)[1].split("function closeContentDetails", 1)[0]
    candidate = page.split("const candidateActionsBusy", 1)[1].split("function beginCandidateEdit", 1)[0]
    script = """
    const assert = require('assert');
    let calls = 0, complete;
    const buttons = [{disabled:false}];
    global.document = {
      querySelectorAll:()=>buttons,
      getElementById:(id)=>({innerText:'',closest:()=>({querySelector:()=>({innerText:'fixture question'})})})
    };
    const confirm=()=>true, alert=()=>{}, getAuthHeaders=()=>({});
    const closeContentDetails=()=>{}, loadHandoffs=async()=>{}, loadDashboardOverview=async()=>{}, loadCandidates=async()=>{};
    const fetch=()=>{calls++; return new Promise(resolve=>complete=resolve);};
    """ + "let adminResumeBusy" + resume + "const candidateActionsBusy" + candidate + """
    (async()=>{
      let first=resumeAdminHandoff('CAM-fixture');
      await resumeAdminHandoff('CAM-fixture');
      assert.equal(calls,1); assert.equal(buttons[0].disabled,true);
      complete({ok:false}); await first;
      assert.equal(buttons[0].disabled,false); assert.equal(adminResumeBusy,false);
      first=approveCandidate(1); await approveCandidate(1);
      assert.equal(calls,2); assert.equal(buttons[0].disabled,true);
      complete({ok:false}); await first;
      assert.equal(buttons[0].disabled,false); assert.equal(candidateActionsBusy.size,0);
    })().catch(error=>{console.error(error);process.exitCode=1;});
    """
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def test_menu_server_errors_are_sanitized_without_bypassing_guards():
    from camellia.admin_menu_review import REVIEW_SCRIPT
    from camellia.admin_weekly_savoury import WEEKLY_SCRIPT
    assert "response.status<500" in REVIEW_SCRIPT
    assert "typeof data.detail==='string'" in REVIEW_SCRIPT
    assert "traceback|sqlite|groq|authorization" in REVIEW_SCRIPT
    assert "async function mrDeleteMenu(id){if(mrBusy)return" in REVIEW_SCRIPT
    assert "async function wsReject(){if(wsBusy||!wsDraft" in WEEKLY_SCRIPT
    assert "finally{wsBusy=false;}" in WEEKLY_SCRIPT
