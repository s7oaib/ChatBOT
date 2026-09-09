# Conversation benchmark

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts/conversation_benchmark.py --output scratch/conversation-benchmark.json
```

The suite contains **180 synthetic scenarios / 220 customer turns**: 40 ordinary
questions, 30 menu-and-delivery requests, 20 conversations resolving ambiguous
prices, 20 human-handoff cases, 10 acknowledgement/sign-off conversations,
40 product-price plus delivery requests, and 20 event/bulk requests.
These are developer-authored regression scenarios with systematic paraphrases,
not anonymized customer traffic or an independent accuracy evaluation.

Each invocation compares the same scenarios with
`CONVERSATION_IMPROVEMENTS_ENABLED=false` and `true`. The default application
setting is true. Set it false to roll back this conversation layer independently
of the existing semantic router. Deployment is still required for local changes
to reach customers.

The candidate run enables structured intent/entity understanding and checks its
deterministic action plan. The baseline disables it. The suite checks compound
price/delivery requests, event handoffs, extracted product/location, knowledge
sufficiency, price clarification, acknowledgements and existing approved copy.

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

## Independent judge evaluation

`scripts/judge_conversation_benchmark.py` adds an evaluation layer on top of
the regression benchmark. It always runs deterministic checks first for menu
facts, delivery refusals, routing and handoff safety. This keeps factual
business rules out of subjective LLM scoring.

Without an external judge it produces those deterministic point-wise scores:

```powershell
.\.venv\Scripts\python.exe scripts/judge_conversation_benchmark.py --output scratch/judge-benchmark.json
```

To enable LLM judging, configure an **independent** OpenAI-compatible model.
Do not use the same `GROQ_MODEL` or router model that writes customer replies.
The judge returns strict JSON scores for correctness, relevance, tone and
safety. It also compares baseline and candidate answers twice, swapping their
positions so a preference for the first answer does not decide the result.

```powershell
$env:JUDGE_API_KEY = "your-judge-key"
$env:JUDGE_BASE_URL = "https://your-provider.example/v1"
$env:JUDGE_MODEL = "your-independent-judge-model"
.\.venv\Scripts\python.exe scripts/judge_conversation_benchmark.py --judge --limit 220 --output scratch/judge-benchmark.json
```

`--limit 220` evaluates the full synthetic set. Start with the default 25
turns while tuning the judge prompt, because each turn makes two point-wise and
two pairwise judge calls.

For calibration, provide a JSONL file of **anonymized** human-scored examples:

```json
{"case":"core-000","turn":0,"overall":5}
```

Then pass `--human-labels scratch/human-labels.jsonl`. The report includes mean
absolute error and the proportion of judge scores within one point of the human
score. Review the rubric/prompt before relying on a judge whose MAE exceeds 1.

## Real-conversation review sample

Export a local review-only sample with customer IDs excluded and phone numbers
and emails replaced. Review it before it leaves the business, then let staff
fill in `human_overall` and `human_notes`:

```powershell
python scripts/export_anonymized_conversations.py --output scratch/review.jsonl --limit 200
```

## Stronger-model trial

This changes no Railway setting and cannot send Instagram or WhatsApp messages.
It compares the named Groq model on the fixed regression benchmark. Compare
pass rate, reply length, latency and token use before changing `GROQ_MODEL` in
Railway:

```powershell
python scripts/conversation_benchmark.py --live --model openai/gpt-oss-120b --output scratch/gpt-oss-120b.json
```
