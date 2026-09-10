# Architecture

## Channel policy: Instagram AI, WhatsApp human-only

Instagram is the only AI chatbot channel. WhatsApp is reserved for the
client's own human team — customers reach it only after Instagram creates a
handoff, and no automated reply is ever composed for a WhatsApp message.

```
INSTAGRAM                                  WHATSAPP
   |                                          |
   v                                          v
AI chatbot (full architecture,          human-only
unchanged from prior phases)            (camellia/whatsapp_adapter.py:
   |                                     process_incoming_message does not
   +-- can answer -> answer             call responder/understanding/RAG
   |                                     by default; WHATSAPP_CHATBOT_ENABLED,
   +-- can't / human / order / -->      off by default, is the only escape
       complaint / bulk enquiry          hatch, and it is logged when used)
       -> camellia/handoff.py                |
          creates an opaque reference         v
          ("CAM-48371") and a wa.me      A message naming a handoff reference
          click-to-chat link ->          is recorded and marked
          customer taps it themselves    WHATSAPP_CONNECTED for staff
                                          tracking — still no bot reply.
```

`camellia/handoff.py` owns the opaque reference, deduplication (a repeated
"human please" reuses the active handoff instead of minting a new one), the
click-to-chat link (`WHATSAPP_BUSINESS_NUMBER`, manually configured — see the
manual-configuration report), and the staff-facing summary — built from
whatever Instagram's own structured understanding already extracted this
session (`facts["last_entities"]`, order-draft slots), never re-asked of the
customer. Staff notification continues to go through the existing
`escalator.handle_escalation` mechanism unchanged.

`INSTAGRAM_CHATBOT_ENABLED` (default true), `WHATSAPP_CHATBOT_ENABLED`
(default false), and `WHATSAPP_HUMAN_HANDOFF_ENABLED` (default true) are the
explicit config knobs; the human-only behaviour is enforced in code, not by
configuration defaults alone — `WHATSAPP_CHATBOT_ENABLED` is the one flag
that would restore the old auto-reply behaviour, and the code path it gates
is not reachable any other way.

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

## Backlog

* Before promoting shadow router, require explicit product/entity scope for
  specific product-price routes. Vague price requests must clarify product
  rather than assume one. (Found via `/admin/router-shadow`: a bare "What is
  the price?" with no named product was routed by the shadow model straight to
  a specific product's price instead of asking which product — the kind of
  invented specificity the source-context rules are meant to block. The
  router stays shadow-only/inert on customer replies until this is hardened
  and re-evaluated.)
