# Phase 4 security acceptance — 2026-09-29

Baseline: `4c3290ba688a2d5c1b48d9fd68f1a5e2d19085f6` (post-Phase-3 hotfix). This audit used temporary local SQLite, fixture keys, read-only Railway configuration/log views, public health probes, and authenticated read-only Admin views. It did not send customer messages, publish menus, alter canonical facts, or change Railway variables.

## Route inventory and authorization

The application declares 161 HTTP routes in `camellia/app.py`: 144 under `/admin` and 17 non-Admin routes. Public-required routes include `/`, `/favicon.ico`, `/privacy`, `/terms`, `/health`, and the Instagram OAuth callback (state-validated). `/webhook/instagram` GET requires the configured verification token; POST requires a valid Meta signature by default and fails closed if the secret is missing. `/auth/instagram/start` requires Admin authentication. Admin login/shell/compatibility entry points expose no protected records; other Admin APIs require a server-side session or a separate machine API key. The compatibility PIN is not an authenticator. `/chat`, `/demo`, `/test`, `/web`, `/tester`, `/bot`, `/api/test-chat`, and `/api/reset-session` are disabled when the web tester is off. WhatsApp automation routes are not registered. FastAPI-generated OpenAPI/docs route metadata may be publicly discoverable; this is route enumeration, not an authenticated data or mutation bypass.

## Acceptance evidence

| Area | Evidence and result |
| --- | --- |
| Admin | Argon2id verification, durable cooldown, random server-side session, idle/absolute expiry, logout revocation, Secure/HttpOnly/SameSite Strict session cookie, and distinct machine key: `tests/test_admin_security.py`. New negative probes: `tests/test_phase4_security_acceptance.py`. |
| CSRF | Server-bound CSRF required for authenticated Admin mutations; missing, wrong, and cross-session tokens rejected. The CSRF cookie is readable by same-origin JavaScript for the double-submit pattern, while the session cookie is HttpOnly. |
| Meta and channels | Signature/token, exact identity/source, and human-only WhatsApp checks: `tests/test_admin_portal.py`, `tests/test_instagram_identity.py`, `tests/test_instagram_source_referential.py`, `tests/test_channel_policy.py`. |
| Business authority | Draft/pending/expired menu, approved RAG, grounded-answer, candidate, waiting-for-human and explicit-resume checks: `tests/test_phase2c_menu_authority.py`, `tests/test_phase2b_pending_menu_handoff.py`, `tests/test_menu_approved_rag.py`, `tests/test_grounded_answer_lanes.py`, `tests/test_candidate_approval_editing.py`, `tests/test_waiting_for_human.py`, `tests/test_phase2e_admin_operator_experience.py`. No OCR draft is approved automatically. |
| Upload and storage | MIME, decoder, size, pixel/PDF, private source path, stale revision, atomic publication and empty-review checks: `tests/test_menu_review_approval.py`, `tests/test_phase2c_review_workflow.py`, `tests/test_post_phase3_menu_vision_hotfix.py`, and Phase 4 negative probes. |
| XSS and SQL | Admin escaping is exercised with an HTML/attribute payload; candidate SQL-injection text remains pending data and does not alter schema. Current active UI uses escaping/text content. No reachable script execution or SQL injection was demonstrated. |
| Backups and logs | WAL-inclusive online backup, disposable recovery, bounded read-only operations, and secret/error redaction: `tests/test_phase2f_observability_operations.py`, `tests/test_db_backup_endpoint.py`, `tests/test_access_log_security.py`. No live recovery was attempted. |
| Headers | Live production `/health`: CSP, frame denial, `nosniff`, referrer policy, and one-year HSTS. Admin replies are no-store. |
| Dependency integrity | An isolated disposable environment installed from the existing `requirements.txt` and passed `python -m pip check`. The developer's global Python environment has unrelated third-party conflicts; production container `pip check` was not directly available because Railway SSH requires an unregistered key. No vulnerability scanner was installed, so this is **not** a zero-CVE claim. |

## Finding and disposition

**LOW, fixed:** `/admin/assisted-resolutions/{id}/edit-resolve` validated an empty body before authorizing the caller. No edit or customer send could occur without authorization because the delegated approve handler checked it, but unauthenticated callers received an input-validation response. Authorization now runs first; `test_assisted_edit_validates_authorization_before_request_body` proves the 403.

**LOW, deferred:** The Admin CSP permits inline script/style because the current Admin is implemented with inline code. Other CSP/frame protections remain active. Removing inline execution needs a separately reviewed UI refactor and is not warranted for this narrow release.

**INFO, deferred:** Retired compatibility JavaScript includes legacy `innerHTML` branches. The current server-side draft/status contracts do not reach the old publication branches in this audit. This is not a claim that every historical string sink is formally proven safe; no reachable XSS was reproduced. Review if those compatibility paths are re-enabled.

**INFO, deferred:** `GEMINI_*` and `ADMIN_PIN` variable names remain in Railway configuration but are not used as runtime Gemini or PIN authentication. Do not delete ambiguous variables without separate ownership review. The duplicate staging-named resource in the production environment also remains untouched.

**Limits:** This is a code, local integration, and bounded runtime acceptance, not a penetration test or comprehensive secret/PII scan. A targeted tracked-file/commit-path check found no tracked `.env` or known-format key in scanned application/test/docs paths; it did not inspect secret values or prove that arbitrary PII can never appear in logs. Staging has no Groq key, so live staging vision extraction was not exercised. Railway production source, domain, volume, builder, replicas, variable names, and deployment SHA must be rechecked after release.
