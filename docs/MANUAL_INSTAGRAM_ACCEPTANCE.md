# Manual Instagram acceptance checklist

This checklist is intentionally manual. The acceptance agent must not log in to Instagram, send messages, or share posts/reels.

## Reel route

- [ ] An admin opens the configured reel containing `DdAnO6MTok1` from the real Instagram account.
- [ ] Send `Tell me more` from the customer account and capture the reply, route, and timestamp.
- [ ] Send `What about kanchagolla?` and capture the reply, route, and timestamp.
- [ ] Confirm the reply is grounded in the intended reel route and that no source-routing override is recorded.

## Post route

- [ ] An admin confirms the post containing `Dc2PmrQE3Y4` is paired through the dashboard and the pairing window is active.
- [ ] Share the post during the pairing window, send `Tell me more`, and capture the reply, route, and timestamp.
- [ ] Confirm the post route is used and no unrelated route takes precedence.

## Safety checks after the manual run

- [ ] Auto Resolve remains simulation-only (`LLM_AUTO_RESOLVE_ENABLED=false`).
- [ ] `/admin/auto-resolve/evaluation` shows zero WhatsApp AI activity, zero order/CAM interference, and zero source-routing overrides.
- [ ] Any unexpected answer, route, or metric is captured as a screenshot and escalated before further customer testing.

No Instagram action is performed by the automated acceptance run.
