# Conversation benchmark

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts/conversation_benchmark.py --output scratch/conversation-benchmark.json
```

The suite contains **120 synthetic scenarios / 160 customer turns**: 40 ordinary
questions, 30 menu-and-delivery requests, 20 conversations resolving ambiguous
prices, 20 human-handoff cases, and 10 acknowledgement/sign-off conversations.
These are developer-authored regression scenarios with systematic paraphrases,
not anonymized customer traffic or an independent accuracy evaluation.

Each invocation compares the same scenarios with
`CONVERSATION_IMPROVEMENTS_ENABLED=false` and `true`. The default application
setting is true. Set it false to roll back this conversation layer independently
of the existing semantic router. Deployment is still required for local changes
to reach customers.

The implementation currently supports menu + delivery coverage as its compound
intent pair. Unsupported combinations stay in the existing pipeline. It also
remembers the answer to a product-price clarification, shortens acknowledgements
and closings, and routes complaints/cancellation/human requests before sales
matching. Existing approved menu and policy copy is retained.

## What the report measures

- Incorrect route/content checks and overly long acknowledgement replies.
- Exact repeated questions within each scenario (not semantic repetition).
- Correct handoff decisions. **Actual staff notification/delivery is not tested.**
- Median/p95 response latency and mean reply word count.
- Provider requests, input/output tokens, and cost when supplied model rates are
  available. Cost is null if rates or usage data are missing.

Offline mode disables AI and blocks HTTP POSTs. Its $0 provider cost and latency
are local measurements, not estimates of production Groq performance. Temporary
menu, FAQ, database and sessions keep all benchmark activity out of customer
records. The menu's date is fixed in that temporary fixture so expiry does not
change results between runs; expiry behavior has a separate regression test.

## Optional provider evaluation

Add `--live` to exercise the real configured Groq models. This consumes quota
and can incur charges; Meta messages and staff notifications are never sent.
Add `--rates model-rates.json` to estimate cost from returned usage. The rate
file uses this shape, with your verified USD prices per million tokens:

```json
{"your-model-id": {"input": 0.0, "output": 0.0}}
```

Those zeros are placeholders, not provider pricing. Per-model pricing may need
extra accounting for provider tools; the report only estimates token charges.

Next evaluation: have the client label anonymized real conversations, reserve a
held-out set before tuning, and measure answer correctness and completed staff
handoffs there. Passing this deliberately small regression suite does not prove
general multilingual comprehension or production conversion gains.
