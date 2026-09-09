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
3. `understanding.py` asks Groq for validated intent/entity JSON. It emits no customer copy.
4. `responder.py` maps all returned intents to approved templates and deterministic actions.
5. Grounded generation/refinement is accepted only when its numeric and protected claims exist in approved context.
6. Unsupported facts create a canonical `knowledge_gaps` record and use human handoff copy.

`STRUCTURED_NLU_ROLLOUT_PERCENT` assigns a stable SHA-256 customer cohort from
0 to 100. Set it to 10 for an initial production cohort, increase after reviewing
knowledge gaps and handoffs, and set it to 0 for immediate rollback. Active
sessions use the existing `DB_PATH` volume when `SESSION_PERSISTENCE_ENABLED=true`.

## Runtime data rules

`data/menu.json` and `data/qa_templates.json` are seed files. Production can
mount persistent runtime state separately; do not place SQLite databases,
uploaded flyers, `.env`, benchmark output, or customer exports in version
control. Those paths are ignored by `.gitignore`.
