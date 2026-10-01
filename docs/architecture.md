# Architecture

## Channel policy: Instagram AI, WhatsApp manual continuation

Instagram is the only automated customer channel. When Instagram cannot
answer, or a customer asks for a person, `camellia/handoff.py` creates a
durable opaque CAM reference. Generic support handoffs do not include a
WhatsApp link. Booking, order, payment, and purchase-related handoffs may
include the separately approved manual `wa.me` continuation. WhatsApp is the
human team's continuation surface: this service has no WhatsApp webhook, bot,
Cloud API sender, AI/RAG/template path, or inbound message processing.

```
Instagram DM -> approved AI response
            -> CAM handoff + deduplicated WAITING_FOR_HUMAN state
            -> human resumes the conversation

Booking/order/payment handoff -> optional manual wa.me continuation
```

`camellia/handoff.py` owns the opaque reference, deduplication, the optional
`WHATSAPP_BUSINESS_NUMBER` link target for purchase-related handoffs, and the
staff-facing summary built from Instagram's existing structured state. The
durable escalation outbox records the handoff for admin visibility and marks
external notification as `SKIPPED`; no provider credential or network send is
involved. Explicit admin resume remains idempotent and restores Instagram AI
only after the handoff is closed.

`INSTAGRAM_CHATBOT_ENABLED` controls Instagram channel capability. Durable Admin
LIVE / TEST_ONLY independently controls customer access; accepted production
is TEST_ONLY. Exact allowlisted Meta sender IDs alone may reach business
processing and customer sending in TEST_ONLY. See [CLIENT_HANDOVER.md](CLIENT_HANDOVER.md)
for the current Approved Q&A, Weekly Savoury and source-mapping authority. The
WhatsApp business number is retained solely for the manual `wa.me` link.

## Runtime boundaries

| Area | Responsibility |
| --- | --- |
| `main.py` | Process entry point used by Railway, Docker and Procfile. |
| `camellia/` | FastAPI application and domain logic. |
| `data/` | Versioned seed menu and approved Q&A templates. |
| `images/` | Versioned static menu cards served at `/images`. |
| `tests/` | Automated regression and integration tests. |
| `scripts/` | Local operator and evaluation utilities. |
| `docs/` | Human-facing operational documentation. |

## Application package

`camellia/app.py` owns HTTP/webhook endpoints and the admin portal.
`responder.py` owns safe customer reply selection; it reads factual menu and
template data but does not author new business facts. Channel adapters,
escalation, persistence, session state, analytics, menu parsing, and provider
integration remain separate modules in the same package so imports stay stable
for Railway and the test suite.

## Customer-message path

1. `session_memory.py` restores active state from SQLite and applies the 24-hour TTL.
2. Safety and active-flow guards handle replies such as delivery-area follow-ups.
   An open slot ("veg", "Chatra Marg", "yes i want to book") is answered by its
   own deterministic handler: the state, not the wording, carries the meaning.
3. `understanding.py` asks Groq for validated intent/entity JSON. It emits no customer copy.
4. `knowledge.py` resolves those intents and entities to approved knowledge records.
5. `responder.py` executes the record's allowlisted actions and renders approved copy.
6. Grounded generation/refinement is accepted only when its numeric, protected and
   content claims exist in the approved context it was shown.
7. Unsupported facts create a canonical `knowledge_gaps` record and use human handoff copy.

## Knowledge, not phrasing

`camellia/knowledge.py` holds the canonical catalogue. A record is addressed by
meaning — an intent plus the entities that narrow it — and points at the
approved copy in its existing source; it never stores a customer's wording and
never restates a price or policy. `SOURCE_MAP` in the same module names the one
authoritative source per business domain (weekly menu, prices, products,
flavours, pack sizes, bento, delivery, delivery time, storage, packaging,
subscriptions, event/bulk, contact, allergens, availability, payment,
ordering), so a fact that appears in more than one place has a documented
precedence instead of a silent winner.

Subscriptions show the intended split: the plans, the booking steps and the
five-hour change notice are canonical knowledge, while which turn of the
booking conversation a customer is on stays deterministic state.

## Reading meaning without the model

`camellia/semantic_fallback.py` is what runs when the understanding provider is
unreachable. It is concept-based, not phrase-based: lexical families for menu,
price, delivery, ordering, storage, packaging, dietary, allergens, payment,
contact, subscriptions and pack sizes, in the English and Hinglish customers
actually write, plus entity extraction and clause-by-clause reading. A compound
question keeps both halves; a clause with a subject and no concept becomes
`unknown_business_query`, a typed gap rather than silence; and any word the
reader cannot account for makes it defer to the older deterministic handlers
rather than claim a message it only partly read.

`last_route_source` records which layer decided each turn — `model_nlu`,
`deterministic_semantic_fallback`, `state_handler`, `explicit_rule`,
`legacy_trigger` or `escalation` — so trigger dependency is measurable rather
than assumed.

## Relevance and sufficiency

Two rules keep an approved answer from being sent to the wrong question:

* **Relevance** — a selected answer must be about something the customer
  actually mentioned. Word similarity is evidence of a typo, not of subject
  ("parking" against the "packing" trigger). Questions made only of words the
  business has never written about become knowledge gaps, with no list of
  unsupported topics to maintain.
* **Sufficiency** — every factual reply records which knowledge answered it and
  a per-intent sufficiency decision. Replies that assert nothing (a
  clarification, an acknowledgement, a handover) are exempt by design.
* **Subtopic sufficiency** — a domain match does not imply every corner of that
  domain. "Do you gift wrap?" is a packaging question, and the approved
  packaging copy is about food-safe delivery boxes, so it is a gap. The
  evidence is the approved copy itself: a subtopic counts as covered when the
  copy mentions it, which means nothing has to be maintained twice.

Retrieval-grounded generation runs only when the retrieved excerpts address the
question and no detected intent is one the catalogue knows it cannot support.

## Provider failure

Every failure shape — rate limit, HTTP error, connection exception, empty
choices, malformed JSON, schema violation, low confidence — is treated
identically: understanding produced no usable meaning, so no second LLM router
runs for that turn. `STRUCTURED_NLU_FALLBACK_MODEL`, when the operator sets it,
is tried exactly once and only for capacity-shaped failures; with no configured
model the bot fails closed to the deterministic path.

## Production observability and metrics

Every reply writes one row to `message_route_audit` (`camellia/database.py`):
route, intents, entities, confidence, knowledge ids, actions, grounding
result, knowledge-gap flag, escalation status, and `route_source` — which
layer actually decided the turn (`model_nlu`, `deterministic_semantic_fallback`,
`state_handler`, `explicit_rule`, `legacy_trigger`, or `escalation`).
`route_source` is the metric this migration exists to drive toward zero for
`legacy_trigger`: grouping the table by it and dividing by the day's row count
gives % model NLU, % semantic fallback, % state-owned, % legacy-trigger and
% escalation without re-deriving anything from the route name after the fact.
The same grouping over `knowledge_gap` and `grounding_result` gives % knowledge
gap and % grounding rejection; grouping over `entities.topic` for gapped rows
surfaces the top unknown topics customers actually ask about — the input to
the canonical-learning queue below, never raw sentences.

## Candidate learning stays fact-shaped, not sentence-shaped

`db.record_knowledge_gap` records what is missing (an intent plus its
entities/topic), not a new spelling of a question already answered. Ten
different ways of asking about gift wrapping should produce one gap entry for
"packaging: gift wrapping", reviewed and approved by staff once, not ten QA
templates. The model never gets to decide a gap is answered — only an
approved template or catalogue record does that, and this migration's
sufficiency rules mean nothing ships until one exists.

## Benchmarks

* `scripts/conversation_benchmark.py` — 220-turn regression net over known wordings.
* `scripts/generalization_benchmark_v3.py` — 296 turns built to be difficult
  for a semantic system by construction: lexical/entity ambiguity, negation,
  correction, topic switching, ellipsis, pronouns, mixed-language
  code-switching, spelling errors, an unsupported clause riding on a
  supported one, implied requests, misleading vocabulary overlap,
  hallucination bait, false premises, 3+ intent compounds, and stateful
  chains. Every turn is checked for a stated wrong or invented fact
  regardless of category; ambiguous categories are graded on safety
  (a clean escalation passes) rather than one fixed wording.
* `scripts/generalization_benchmark_v2.py` — 208 turns: unseen paraphrases,
  Hinglish, unsupported topics, multi-intent, mixed supported/unsupported,
  state interruptions, hallucination probes and vague questions, with
  route-source counts.
* `scripts/generalization_benchmark.py` — the earlier 102-turn set: paraphrases, unsupported
  restaurant topics, compound asks, interruptions to an open flow, Hinglish, and
  probes that invite an invented fact. Both run offline and block network access.

`STRUCTURED_NLU_ROLLOUT_PERCENT` assigns a stable SHA-256 customer cohort from
0 to 100. The production target is 100 after the initial cohort validation;
set it to 0 for immediate rollback. Active
sessions use the existing `DB_PATH` volume when `SESSION_PERSISTENCE_ENABLED=true`.

## Runtime data rules

`data/menu.json` and `data/qa_templates.json` are seed files. Production can
mount persistent runtime state separately; do not place SQLite databases,
uploaded flyers, `.env`, benchmark output, or customer exports in version
control. Those paths are ignored by `.gitignore`.
