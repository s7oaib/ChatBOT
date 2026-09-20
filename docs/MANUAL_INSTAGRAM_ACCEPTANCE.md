# Manual Instagram acceptance checklist

This checklist is intentionally manual. The acceptance agent must not log in to Instagram, send messages, or share posts/reels. An account owner should run the following exact sequence from the real customer account against the configured source route.

## Required sequence

1. Send `hi`.
2. Send `Jolbhora price`.
3. Share the configured reel.
4. Send `Tell me more`.
5. Send `hi`.
6. Send `Tell me more`.
7. Send `What about Kanchagolla?`.
8. Send `I want to talk to a human`.

Capture the customer-visible reply, timestamp, and route/evidence for each step.

## Expected results

- Greeting is the approved greeting copy.
- Jolbhora prices match the canonical menu.
- The configured reel context is used by the first `Tell me more`.
- The greeting does not consume or replace source context.
- The source context remains available for the subsequent `Tell me more` when the existing session rules allow it.
- `What about Kanchagolla?` uses explicit Kanchagolla intent rather than the reel priority.
- The final request follows the safe human-handoff flow.

## Optional route evidence

- [ ] Confirm the configured reel contains `DdAnO6MTok1` and capture its route ID.
- [ ] If post routing is also being signed off, confirm the configured post contains `Dc2PmrQE3Y4`, share it during the pairing window, and capture the `Tell me more` reply.

## Safety checks after the manual run

- [ ] Auto Resolve remains simulation-only (`LLM_AUTO_RESOLVE_ENABLED=false`).
- [ ] `/health` reports `whatsapp.automation=removed` and
      `manual_handoff=available`; `/admin/auto-resolve/evaluation` still shows
      zero order/CAM interference and zero source-routing overrides.
- [ ] Any unexpected answer, route, or metric is captured as a screenshot and escalated before further customer testing.

No Instagram action is performed by the automated acceptance run.
