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

## Runtime data rules

`data/menu.json` and `data/qa_templates.json` are seed files. Production can
mount persistent runtime state separately; do not place SQLite databases,
uploaded flyers, `.env`, benchmark output, or customer exports in version
control. Those paths are ignored by `.gitignore`.
