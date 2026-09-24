# Phase 5 production acceptance matrix

This is the automated acceptance index for the frozen Camellia production
policies. Every row points to committed tests; no row sends customer-facing
traffic. Live Meta delivery remains a manual, approval-gated check.

Run the complete matrix with:

```powershell
python -m pytest -q
```

Run the highest-risk subset while investigating a change with:

```powershell
python -m pytest -q `
  tests/test_channel_policy.py `
  tests/test_topic_menu_catalogue.py `
  tests/test_instagram_story_routing.py `
  tests/test_instagram_story_lifecycle.py `
  tests/test_instagram_outbound_audit.py `
  tests/test_human_notifications.py `
  tests/test_waiting_for_human.py `
  tests/test_provider_outage_routing.py `
  tests/test_groq_model_stabilization.py `
  tests/test_privacy_logging.py
```

| Area | Acceptance coverage | Automated evidence |
|---|---|---|
| Instagram deterministic answer | FAQ, menu, product price, delivery and approved knowledge | `tests/test_channel_policy.py`, `tests/test_canonical_knowledge_layer.py` |
| Canonical opening hours | Exact approved opening-hours response; no NLU/Groq dependency | `tests/test_opening_hours_canonical.py` |
| Structured understanding | Structured JSON contract, schema rejection, safe multi-intent behavior | `tests/test_structured_understanding.py`, `tests/test_primary_nlu_architecture.py` |
| Safe AI refinement | Grounded response only; unsupported claims fall back to approved copy | `tests/test_structured_understanding.py`, `tests/test_canonical_knowledge_layer.py` |
| Unsupported facts | Unknown topics do not borrow unrelated answers and route safely | `tests/test_canonical_knowledge_layer.py`, `tests/test_provider_outage_routing.py` |
| Escalation | Direct request, unsupported request, event/order request and handoff references | `tests/test_channel_policy.py`, `tests/test_whatsapp_cleanup.py` |
| Story reply | Exact Meta Story ID routing, unmapped safe fallback, no caption/OCR/similarity routing | `tests/test_instagram_story_routing.py` |
| Post/Reel mapping | Exact source identity, shared media precedence, pairing and route persistence | `tests/test_instagram_source_routing.py`, `tests/test_shared_reel_routing.py`, `tests/test_shared_post_pairing.py`, `tests/test_topic_menu_catalogue.py` |
| Topic catalogue | Create/edit/duplicate protection, active catalogue, selectors, exact mapping, restart persistence and legacy migration | `tests/test_topic_menu_catalogue.py`, `tests/test_admin_ui_structure.py`, `tests/test_instagram_story_runtime.py` |
| Account guard | Intended Instagram account is processed; foreign account is suppressed | `tests/test_instagram_account_guard.py` |
| Webhook signature | Mismatch rejection and honest health reporting | `tests/test_admin_portal.py`, `tests/test_privacy_logging.py` |
| Provider failure | 429/5xx, malformed output, timeout/cooldown and deterministic fallback | `tests/test_provider_outage_routing.py`, `tests/test_groq_model_stabilization.py`, `tests/test_bot.py` |
| Duplicate webhook/idempotency | Duplicate reply guard and stable source/handoff behavior | `tests/test_bot.py`, `tests/test_phase3b_persistent_session.py`, `tests/test_instagram_story_sync.py` |
| Handoff durability | Handoff creation, durable reference, order state and concurrent deduplication | `tests/test_handoff_order_state.py`, `tests/test_channel_policy.py` |
| Notification outbox | Idempotency, bounded retry, terminal failure and recorded delivery state | `tests/test_human_notifications.py` |
| Waiting for human | Customer messages are stored, AI is suppressed, explicit resume is idempotent | `tests/test_waiting_for_human.py` |
| WhatsApp boundary | No inbound bot path or Cloud API sender; CAM/`wa.me` manual continuation remains available | `tests/test_whatsapp_cleanup.py`, `tests/test_channel_policy.py` |
| Privacy | Default logs exclude customer text, tokens, signatures and provider bodies | `tests/test_privacy_logging.py`, `tests/test_instagram_attachment_diagnostics.py` |
| Content integrity | Dispatch-time claims remain quarantined unless confirmed | `tests/test_content_integrity_quarantine.py`, `tests/test_opening_hours_canonical.py` |
| Story lifecycle | Unknown age stays active; partial/failed sync never falsely removes a Story | `tests/test_instagram_story_lifecycle.py`, `tests/test_instagram_story_sync.py` |

## Explicit non-automated gates

- Real customer-facing Meta messages are not sent by this matrix.
- Production Railway deployment, Meta configuration changes, credential
  rotation, and database restore require explicit operator approval.
- A green local suite does not by itself authorize deployment.
