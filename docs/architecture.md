# Architecture

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

Retrieval-grounded generation runs only when the retrieved excerpts address the
question and no detected intent is one the catalogue knows it cannot support.

## Provider failure

Every failure shape — rate limit, HTTP error, connection exception, empty
choices, malformed JSON, schema violation, low confidence — is treated
identically: understanding produced no usable meaning, so no second LLM router
runs for that turn. `STRUCTURED_NLU_FALLBACK_MODEL`, when the operator sets it,
is tried exactly once and only for capacity-shaped failures; with no configured
model the bot fails closed to the deterministic path.

## Benchmarks

* `scripts/conversation_benchmark.py` — 220-turn regression net over known wordings.
* `scripts/generalization_benchmark.py` — adversarial set: paraphrases, unsupported
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
