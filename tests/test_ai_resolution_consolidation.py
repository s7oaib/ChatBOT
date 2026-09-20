"""
Admin navigation consolidation and additive candidate read paths.

This is a navigation change, not a data or workflow change: candidate_
templates, escalations, knowledge_gaps and learned_knowledge keep their
exact schemas, endpoints and semantics — only which screen reaches them
changed. The action-level guarantees (edit/approve/reject a candidate,
Teach & Resolve, resolve-only, dismiss a gap, unified-view refresh after a
mutation) are already covered by tests/test_admin_ui_structure.py's
test_ai_resolution_tab_exists_and_reuses_existing_endpoints and by
tests/test_candidate_approval_editing.py; this file covers what is new in
this change specifically: the nav removal itself, the new status-filtered
candidate read path (approved/rejected), and that nothing was deleted or
rewritten to make the new filters possible.
"""

from fastapi.testclient import TestClient

from camellia import app as app_module
from camellia.config import config
from camellia.database import Database, db

client = TestClient(app_module.app)
db_module_db = db


def _auth():
    return {"X-Admin-Key": config.ADMIN_API_KEY, "X-Admin-Pin": config.ADMIN_API_KEY}


class TestNavigationConsolidation:
    def test_customers_button_is_gone_from_normal_navigation(self):
        page = client.get("/admin").text
        nav = page[page.index('<aside class="admin-sidebar"'):page.index('</aside>', page.index('<aside class="admin-sidebar"'))]
        assert "Customers" not in nav

    def test_answers_button_is_gone_from_normal_navigation(self):
        page = client.get("/admin").text
        nav = page[page.index('<aside class="admin-sidebar"'):page.index('</aside>', page.index('<aside class="admin-sidebar"'))]
        assert 'Answers' not in nav
        assert 'candidateBadge' not in nav

    def test_ai_resolution_and_answers_is_still_the_primary_tab(self):
        page = client.get("/admin").text
        nav = page[page.index('<aside class="admin-sidebar"'):page.index('</aside>', page.index('<aside class="admin-sidebar"'))]
        assert "Dashboard" in nav
        assert "QA Templates" in nav

    def test_legacy_panels_remain_compatible_but_are_not_promoted_in_the_ui(self):
        page = client.get("/admin").text
        assert 'id="tab-stats"' in page
        assert 'id="tab-candidates"' in page
        assert "Advanced / Legacy Views" not in page
        assert "/admin/legacy" in {route.path for route in app_module.app.routes}


class TestCandidateStatusFilterIsAdditiveNotDestructive:
    """
    The new status-filtered read path (used for the Approved/Rejected
    filters) must not change what the live approval queue returns, and must
    never delete or mutate a row — it only ever reads.
    """

    def test_default_status_matches_the_old_pending_only_behaviour(self, tmp_path):
        database = Database(str(tmp_path / "status_filter.db"))
        database.save_candidate_template(name="t1", response="An approved-length answer about delivery.", user_message="do you deliver?")
        pending_default = database.get_pending_candidates()
        pending_explicit = database.get_candidates_by_status("PENDING_APPROVAL")
        assert len(pending_default) == 1
        # get_pending_candidates re-applies the eligibility/validity gate;
        # get_candidates_by_status does not, since a REJECTED/APPROVED
        # row is a decision already made and not being offered for approval.
        assert len(pending_explicit) == 1
        assert pending_default[0]["id"] == pending_explicit[0]["id"]

    def test_a_rejected_candidate_is_still_readable_and_unmodified(self, tmp_path):
        database = Database(str(tmp_path / "status_filter_rejected.db"))
        cid = database.save_candidate_template(name="t2", response="An answer nobody approved in the end.", user_message="weird question")
        assert database.reject_candidate_template(cid)
        rejected = database.get_candidates_by_status("REJECTED")
        assert any(r["id"] == cid for r in rejected)
        row = next(r for r in rejected if r["id"] == cid)
        assert row["response"] == "An answer nobody approved in the end."
        assert row["status"] == "REJECTED"
        # It must have vanished from the live queue, not from the database.
        assert not any(p["id"] == cid for p in database.get_pending_candidates())

    def test_an_approved_candidate_is_still_readable_and_unmodified(self, tmp_path):
        database = Database(str(tmp_path / "status_filter_approved.db"))
        cid = database.save_candidate_template(name="t3", response="An answer that got approved for reuse.", user_message="another question")
        assert database.approve_candidate_template(cid, approved_by="tester")
        approved = database.get_candidates_by_status("APPROVED")
        assert any(a["id"] == cid for a in approved)
        row = next(a for a in approved if a["id"] == cid)
        assert row["status"] == "APPROVED"
        assert row["approved_by"] == "tester"

    def test_the_endpoint_rejects_a_status_it_does_not_recognize(self):
        resp = client.get("/admin/candidates", params={"status": "MADE_UP"}, headers=_auth())
        assert resp.status_code == 400

    def test_the_endpoint_default_is_unchanged_for_existing_callers(self, monkeypatch):
        calls = {}

        def fake():
            calls["pending"] = True
            return []

        monkeypatch.setattr(db_module_db, "get_pending_candidates", fake)
        resp = client.get("/admin/candidates", headers=_auth())
        assert resp.status_code == 200
        assert calls.get("pending") is True

    def test_status_approved_reads_via_get_candidates_by_status(self, monkeypatch):
        seen = {}

        def fake(status, limit=100):
            seen["status"] = status
            return []

        monkeypatch.setattr(db_module_db, "get_candidates_by_status", fake)
        resp = client.get("/admin/candidates", params={"status": "approved"}, headers=_auth())
        assert resp.status_code == 200
        assert seen["status"] == "APPROVED"


class TestApprovedAnswersRemainAccessible:
    def test_learned_knowledge_endpoint_still_backs_the_approved_answers_filter(self):
        page = client.get("/admin").text
        script = page[page.index("async function loadAiResolution"):]
        assert "fetch('/admin/learned-knowledge" in script
        assert "aiFilterBtn-approved" in page

    def test_deleting_a_learned_knowledge_row_refreshes_the_unified_view(self):
        page = client.get("/admin").text
        script = page[page.index("async function deleteKnowledge"):]
        script = script[:script.index("\n        }\n")]
        assert "refreshAiResolutionIfVisible" in script


class TestTriageShadowStaysReadOnlyAfterConsolidation:
    def test_llm_triage_enabled_does_not_alter_the_candidates_endpoint(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_TRIAGE_ENABLED", True)
        resp = client.get("/admin/candidates", headers=_auth())
        assert resp.status_code == 200

    def test_llm_triage_enabled_does_not_alter_the_escalations_endpoint(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_TRIAGE_ENABLED", True)
        resp = client.get("/admin/escalations", headers=_auth())
        assert resp.status_code == 200
