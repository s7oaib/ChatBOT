"""Independent, outcome-focused conversation evaluation.

This script runs the existing deterministic benchmark twice (baseline and
conversation-improved), then adds point-wise JSON judging and position-bias
controlled pairwise comparison. It never sends Meta messages or staff alerts.

Default mode is offline and deterministic. Add --judge only after configuring
a separate OpenAI-compatible judge endpoint and model:

  $env:JUDGE_API_KEY = "..."
  $env:JUDGE_BASE_URL = "https://api.openai.com/v1"
  $env:JUDGE_MODEL = "your-independent-judge-model"
  python scripts/judge_conversation_benchmark.py --judge --limit 220 \
      --output scratch/judge-benchmark.json

Human calibration labels are JSONL records with case, turn and overall (0-5).
They must contain anonymized conversations only.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
from typing import Any, Dict, Iterable, List, Optional

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from conversation_benchmark import evaluate, scenarios  # noqa: E402


POINTWISE_PROMPT = """You are an independent quality judge for a food-business chatbot.
Judge ONE answer only against the supplied approved facts and expected outcome.
Do not reward plausible invented menu items, prices, delivery areas, policies, or
claims that a human was contacted when that is not supported. A required handoff
must be safe and clear. Score each field from 0 to 5.

Return ONLY this JSON object:
{"correctness":0,"relevance":0,"tone":0,"safety":0,"overall":0,"critical_failures":[],"reason":""}

Scoring: 5 = fully meets the criterion; 3 = partly meets it; 0 = fails it.
Overall is not an average by default: a factual or safety failure must keep it
at 2 or below.
"""

PAIRWISE_PROMPT = """You are an independent quality judge for a food-business chatbot.
Compare Answer A and Answer B against the approved facts and expected outcome.
Prefer factual correctness and safe handoff behavior, then relevance, then brief
friendly tone. Do not prefer an answer merely because it appears first.
Return ONLY this JSON object:
{"winner":"A","confidence":0,"reason":""}
winner must be exactly "A", "B", or "tie". confidence is an integer 0-5.
"""


def _json_object(text: str) -> Dict[str, Any]:
    """Parse a JSON object, tolerating a provider that wraps it in a fence."""
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else ""
        value = value.rsplit("```", 1)[0].strip()
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Judge returned no JSON object")
    parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Judge JSON was not an object")
    return parsed


def _score(value: Any, field: str) -> int:
    if not isinstance(value, int) or not 0 <= value <= 5:
        raise ValueError(f"Judge field {field!r} must be an integer from 0 to 5")
    return value


class IndependentJudge:
    def __init__(self, model: str, base_url: str, api_key: str):
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def ask(self, system: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        with httpx.Client(timeout=45.0) as client:
            response = client.post(self.url, headers=self.headers, json=request)
            response.raise_for_status()
        body = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Judge response did not contain choices[0].message.content") from exc
        return _json_object(content)


def _reference(record: Dict[str, Any]) -> Dict[str, Any]:
    expected = record.get("expected") or {}
    return {
        "customer_message": record["message"],
        "allowed_template_ids": expected.get("ids", []),
        "must_include": expected.get("contains", []),
        "should_handoff_to_human": bool(expected.get("handoff")),
        "maximum_words": expected.get("max_words"),
        "deterministic_checks": record.get("errors", []),
    }


def deterministic_pointwise(record: Dict[str, Any]) -> Dict[str, Any]:
    """Hard checks supplement the LLM judge for facts and escalation routing."""
    errors = record.get("errors", [])
    factual = not any(e.startswith(("missing_content", "missing_menu_fact", "missing_delivery_refusal")) for e in errors)
    routed = "wrong_route" not in errors
    safe = "missed_handoff" not in errors
    concise = "too_long" not in errors
    scores = {
        "correctness": 5 if factual else 0,
        "relevance": 5 if routed else 1,
        "tone": 5 if concise else 2,
        "safety": 5 if safe else 0,
    }
    overall = round(sum(scores.values()) / len(scores))
    return {**scores, "overall": overall, "critical_failures": errors, "reason": "deterministic route/content checks"}


def pointwise_judgment(judge: IndependentJudge, record: Dict[str, Any]) -> Dict[str, Any]:
    result = judge.ask(POINTWISE_PROMPT, {"reference": _reference(record), "assistant_answer": record["reply"]})
    for field in ("correctness", "relevance", "tone", "safety", "overall"):
        result[field] = _score(result.get(field), field)
    if not isinstance(result.get("critical_failures"), list) or not isinstance(result.get("reason"), str):
        raise ValueError("Judge result requires critical_failures array and reason string")
    return result


def pairwise_judgment(judge: IndependentJudge, baseline: Dict[str, Any], candidate: Dict[str, Any], swapped: bool) -> Dict[str, Any]:
    answers = (candidate, baseline) if swapped else (baseline, candidate)
    result = judge.ask(PAIRWISE_PROMPT, {
        "reference": _reference(candidate),
        "answer_a": answers[0]["reply"],
        "answer_b": answers[1]["reply"],
    })
    winner = result.get("winner")
    if winner not in {"A", "B", "tie"}:
        raise ValueError("Pairwise winner must be A, B or tie")
    result["confidence"] = _score(result.get("confidence"), "confidence")
    result["candidate_winner"] = "tie" if winner == "tie" else (
        "candidate" if (winner == "A") == swapped else "baseline"
    )
    return result


def _pairs(baseline: Iterable[Dict[str, Any]], candidate: Iterable[Dict[str, Any]]) -> Iterable[tuple[Dict[str, Any], Dict[str, Any]]]:
    indexed = {(row["case"], row["turn"]): row for row in baseline}
    for row in candidate:
        before = indexed.get((row["case"], row["turn"]))
        if before:
            yield before, row


def load_human_labels(path: Optional[Path]) -> Dict[tuple[str, int], int]:
    if not path:
        return {}
    labels = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        labels[(item["case"], int(item["turn"]))] = _score(item["overall"], "overall")
    return labels


def calibration_report(judgments: List[Dict[str, Any]], labels: Dict[tuple[str, int], int]) -> Dict[str, Any]:
    pairs = [(j["overall"], labels[(j["case"], j["turn"])]) for j in judgments if (j["case"], j["turn"]) in labels]
    if not pairs:
        return {"labelled_cases": 0, "status": "not_calibrated"}
    gaps = [abs(judge - human) for judge, human in pairs]
    return {
        "labelled_cases": len(pairs),
        "mean_absolute_error": round(statistics.mean(gaps), 3),
        "within_one_point_rate": round(sum(gap <= 1 for gap in gaps) / len(gaps), 3),
        "status": "review_prompt_if_mae_exceeds_1",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--judge", action="store_true", help="Call an independent OpenAI-compatible judge model.")
    parser.add_argument("--judge-model", default=os.getenv("JUDGE_MODEL", ""))
    parser.add_argument("--judge-base-url", default=os.getenv("JUDGE_BASE_URL", ""))
    parser.add_argument("--judge-api-key", default=os.getenv("JUDGE_API_KEY", ""))
    parser.add_argument("--limit", type=int, default=25, help="Turns to judge; set 220 for the full synthetic benchmark.")
    parser.add_argument("--human-labels", type=Path, help="Anonymized JSONL labels for judge calibration.")
    args = parser.parse_args()

    cases = scenarios()
    baseline = evaluate(cases, improved=False)["records"]
    candidate = evaluate(cases, improved=True)["records"]
    if args.limit > 0:
        baseline, candidate = baseline[:args.limit], candidate[:args.limit]

    report: Dict[str, Any] = {
        "dataset": "synthetic regression cases; replace or supplement with anonymized, human-labelled production samples",
        "turns": len(candidate),
        "deterministic_baseline": [dict(case=b["case"], turn=b["turn"], judgment=deterministic_pointwise(b)) for b in baseline],
        "deterministic_candidate": [dict(case=c["case"], turn=c["turn"], judgment=deterministic_pointwise(c)) for c in candidate],
        "judge": {"enabled": args.judge, "model": args.judge_model or None},
    }

    if args.judge:
        if not all((args.judge_model, args.judge_base_url, args.judge_api_key)):
            parser.error("--judge requires JUDGE_MODEL, JUDGE_BASE_URL and JUDGE_API_KEY")
        from camellia.config import config
        if args.judge_model in {config.GROQ_MODEL, config.GROQ_ROUTER_MODEL}:
            parser.error("Choose a judge model different from the bot and router models")
        judge = IndependentJudge(args.judge_model, args.judge_base_url, args.judge_api_key)
        judgments = []
        comparisons = []
        for before, after in _pairs(baseline, candidate):
            item = {"case": after["case"], "turn": after["turn"], "baseline": pointwise_judgment(judge, before), "candidate": pointwise_judgment(judge, after)}
            judgments.append({"case": after["case"], "turn": after["turn"], **item["candidate"]})
            item["pairwise_normal"] = pairwise_judgment(judge, before, after, swapped=False)
            item["pairwise_swapped"] = pairwise_judgment(judge, before, after, swapped=True)
            comparisons.append(item)
        labels = load_human_labels(args.human_labels)
        report["judge"]["pointwise_and_pairwise"] = comparisons
        report["judge"]["calibration"] = calibration_report(judgments, labels)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"turns": report["turns"], "judge": report["judge"]}, indent=2))


if __name__ == "__main__":
    main()
