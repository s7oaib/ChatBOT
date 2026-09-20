# Routing stabilization contract

The production decision chain is explicit customer intent, valid active
transactional state, exact Meta source context, approved Q&A/paraphrase, and
approved-knowledge grounding; an unresolved or unsafe request becomes a human
handoff. Source context is identified only by exact Meta media identifiers.

Every response records one decision lane: `EXACT_APPROVED`,
`GROUNDED_APPROVED`, or `INSUFFICIENT_KNOWLEDGE`. Grounded comparisons are
computed only from numbers parsed from the approved Bento answer. Unsupported
customisation and severe allergy/medical questions are never routed to a
nearby product answer.

Transient state is bounded by `SESSION_TTL_SECONDS` and explicit subject
changes clear active flow ownership while retaining durable handoff semantics.
Generic Instagram support creates/reuses a `WAITING_FOR_HUMAN` CAM without a
WhatsApp link; booking/order handoff may use the approved manual `wa.me`
continuation. No server-side WhatsApp bot or send path is enabled.

The safe route trace stores intent, approved source IDs, lane, state/source
influence, and a bounded handoff reason. It does not add raw Meta IDs or
customer text to the trace fields.
