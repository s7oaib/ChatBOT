"""Synthetic conversation regression benchmark; isolated from customer data.

Run: python scripts/conversation_benchmark.py --output scratch/conversations.json
Use --live to measure Groq too; this consumes provider quota. --rates accepts a
JSON file mapping model ids to input/output USD prices per million tokens.
"""
import argparse
from collections import defaultdict
from contextlib import ExitStack
import json
from pathlib import Path
import random
import re
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def scenarios():
    cases = []
    def add(group, turns):
        cases.append({"id": f"{group}-{len(cases):03d}", "group": group, "turns": turns})
    def turn(message, ids, **checks):
        return {"message": message, "ids": ids.split("|"), **checks}
    core = [
        ("show me the menu", "know_more|product_overview"),
        ("veg menu", "weekly_menu_veg"),
        ("non veg menu", "weekly_menu_nonveg"),
        ("where do you deliver", "delivery_area"),
        ("how to order", "how_to_order"),
        ("how long for food", "savoury_delivery_time"),
        ("sondesh price", "sondesh_price_detailed"),
        ("previous menus", "previous_menus"),
    ]
    for message, ids in core:
        for prefix in ("", "please ", "hi ", "hello, ", "could you please "):
            add("core", [turn(prefix + message, ids)])
    for menu in ("veg menu", "non veg menu", "show me the menu", "menu bhejo", "vegetarian menu"):
        for area in ("gurgaon", "noida", "delhi", "mumbai", "pune", "dwarka"):
            section = "non_vegetarian" if menu.startswith("non") else "vegetarian" if menu.startswith(("veg", "vegetarian")) else None
            add("compound", [turn(f"{menu} and do you deliver to {area}", "*", contains=["Delhi NCR"], menu_section=section, images=True,
                                  outside=area in {"mumbai", "pune"})])
    for price in ("how much", "kitna padega", "what does it cost", "iska price", "kitne paise"):
        for choice in ("meals", "khana", "sondesh", "mithai"):
            meals = choice in ("meals", "khana")
            turns = [turn(price, "price_clarify_product"), turn(choice, "context_meal_price_followup" if meals else "sondesh_price_detailed")]
            if meals:
                turns.append(turn("veg", "weekly_menu_veg"))
            add("ambiguity", turns)
    negative = [
        "refund please", "my food is stale", "cancel my order", "I want to speak to a human",
        "I need 100 boxes for a wedding", "wrong order delivered", "I want a refund",
        "my order never arrived", "the food was bad", "my order is missing",
    ]
    for msg in negative:
        for prefix in ("", "hi "):
            add("handoff", [turn(prefix + msg, "*", handoff=True)])
    for msg in ("okay", "ok", "thanks", "thank you", "shukriya"):
        for opening in ("veg menu", "sondesh price"):
            add("closing", [turn(opening, "weekly_menu_veg|sondesh_price_detailed"), turn(msg, "acknowledgement|conversation_closing", max_words=20)])
    products = ("Jolbhora", "Kanchagolla", "Nolen Gur", "Aamar Mishti")
    areas = ("Delhi", "Gurgaon", "Noida", "Mumbai", "Pune")
    for product in products:
        for area in areas:
            for wording in (
                f"What is the price of {product} and do you deliver to {area}?",
                f"Could you tell me how much {product} costs, and can you ship to {area}?",
            ):
                add("structured", [turn(
                    wording, "*", contains=["Delhi NCR"],
                    intents=["product_price", "delivery_query"],
                    entities={"product": product, "location": area},
                    actions=["SEND_PRICE_SEQUENCE"],
                    outside=area in {"Mumbai", "Pune"},
                )])
    for request in (
        "Bengali sweets for an event",
        "Sondesh for a wedding",
        "Mishti for a party",
        "100 sweet boxes for a function",
        "Festive gifting for our company",
    ):
        for prefix in ("I need ", "Can you arrange ", "Please help with ", "We are looking for "):
            add("structured", [turn(
                prefix + request, "*", handoff=True,
                intents=["event_order|bulk_order"],
                actions=["ESCALATE_TO_TEAM"],
                knowledge_sufficient=False,
            )])
    assert len(cases) == 180
    return cases


def evaluate(cases, improved, live=False, rates=None, model=None):
    # Import config before redirecting its paths, then import all consumers
    # inside the isolated directory. No application lifespan or Meta adapters.
    from camellia.config import config
    import httpx
    with tempfile.TemporaryDirectory(prefix="camellia-benchmark-") as tmp, ExitStack() as stack:
        tmp = Path(tmp)
        menu = json.loads(Path(config.MENU_JSON_PATH).read_text(encoding="utf-8"))
        # Fixed fixture keeps expiration from changing the benchmark next week.
        menu["weekly_menus"][-1]["week_of"] = "1st January - 31st December, 2099"
        (tmp / "menu.json").write_text(json.dumps(menu), encoding="utf-8")
        (tmp / "qa.json").write_text(Path(config.QA_TEMPLATES_PATH).read_text(encoding="utf-8"), encoding="utf-8")
        for key, value in {"DB_PATH": str(tmp / "test.db"), "MENU_JSON_PATH": str(tmp / "menu.json"), "QA_TEMPLATES_PATH": str(tmp / "qa.json"), "GROQ_ENABLED": live, "GROQ_MODEL": model or config.GROQ_MODEL, "ROUTER_SHADOW_ENABLED": False, "CONVERSATION_IMPROVEMENTS_ENABLED": improved, "STRUCTURED_NLU_ENABLED": improved, "STRUCTURED_NLU_ROLLOUT_PERCENT": 100, "SLOW_REPLY_NOTICE_ENABLED": False}.items():
            stack.enter_context(patch.object(config, key, value))
        from camellia.database import db
        from camellia.responder import responder
        from camellia.ai_responder import ai_responder
        from camellia.rag_fallback import rag_fallback_engine
        from camellia.session_memory import session_memory
        stack.enter_context(patch.object(db, "db_path", config.DB_PATH))
        get_connection = db.get_connection
        def connection():
            conn = get_connection()
            stack.callback(conn.close)
            return conn
        stack.enter_context(patch.object(db, "get_connection", connection))
        db.init_db()
        for engine in (responder, ai_responder, rag_fallback_engine):
            stack.enter_context(patch.object(engine, "menu_path", config.MENU_JSON_PATH))
            stack.enter_context(patch.object(engine, "qa_path", config.QA_TEMPLATES_PATH))
        for engine in (responder, ai_responder, rag_fallback_engine):
            engine.reload_data()
        stack.enter_context(patch.object(session_memory, "_sessions", type(session_memory._sessions)()))
        usage = defaultdict(lambda: {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "missing_usage": 0})
        original_post = httpx.Client.post
        def post(client, url, **kwargs):
            if not live or url != "https://api.groq.com/openai/v1/chat/completions":
                raise AssertionError("Benchmark blocked an external request")
            model = kwargs.get("json", {}).get("model", "unknown")
            usage[model]["requests"] += 1
            response = original_post(client, url, **kwargs)
            tokens = response.json().get("usage", {}) if response.status_code == 200 else {}
            usage[model]["missing_usage"] += int(not tokens)
            for key in ("prompt_tokens", "completion_tokens"):
                usage[model][key] += tokens.get(key, 0)
            return response
        stack.enter_context(patch.object(httpx.Client, "post", post))
        random.seed(7)
        records, latencies, lengths = [], [], []
        repeat_count = handoffs_expected = handoffs_routed = failures = 0
        for case in cases:
            previous_questions = set()
            for number, expected in enumerate(case["turns"]):
                start = time.perf_counter()
                reply, matched, tid, images = responder.match_and_respond(expected["message"], user_id=case["id"], channel="whatsapp")
                elapsed = (time.perf_counter() - start) * 1000
                facts = session_memory.get_facts(case["id"], "whatsapp")
                errors = []
                if "*" not in expected["ids"] and tid not in expected["ids"]:
                    errors.append("wrong_route")
                if expected.get("handoff"):
                    handoffs_expected += 1
                    handoffs_routed += int(not matched)
                    if matched:
                        errors.append("missed_handoff")
                for phrase in expected.get("contains", []):
                    if phrase.lower() not in reply.lower():
                        errors.append("missing_content:" + phrase)
                if expected.get("menu_section"):
                    for item in menu["weekly_menus"][-1][expected["menu_section"]]:
                        if item["item"].lower() not in reply.lower() or str(item["price_inr"]) not in reply:
                            errors.append("missing_menu_fact:" + item["item"])
                if expected.get("outside") and "unable to ship" not in reply.lower():
                    errors.append("missing_delivery_refusal")
                if expected.get("images") and not images:
                    errors.append("missing_menu_image")
                expected_intents = expected.get("intents", [])
                for intent_options in expected_intents:
                    if not any(option in facts.get("last_intents", []) for option in intent_options.split("|")):
                        errors.append("missing_intent:" + intent_options)
                for key, value in expected.get("entities", {}).items():
                    if str(facts.get("last_entities", {}).get(key, "")).lower() != str(value).lower():
                        errors.append("wrong_entity:" + key)
                for action in expected.get("actions", []):
                    if action not in facts.get("last_actions", []):
                        errors.append("missing_action:" + action)
                if "knowledge_sufficient" in expected and facts.get("knowledge_sufficient") is not expected["knowledge_sufficient"]:
                    errors.append("wrong_knowledge_sufficiency")
                if len(reply.split()) > expected.get("max_words", float("inf")):
                    errors.append("too_long")
                questions = {q.strip().lower() for q in re.findall(r"[^.!?\n]+\?", reply)}
                repeated = questions & previous_questions
                repeat_count += len(repeated)
                previous_questions.update(questions)
                latencies.append(elapsed)
                lengths.append(len(reply.split()))
                failures += bool(errors)
                records.append({"case": case["id"], "turn": number, "message": expected["message"], "expected": expected, "template": tid, "matched": matched, "reply": reply, "errors": errors, "repeated_questions": sorted(repeated), "latency_ms": round(elapsed, 2)})
        dollars = 0.0
        for model, tokens in usage.items():
            rate = (rates or {}).get(model)
            if not rate or tokens["missing_usage"]:
                dollars = None
                break
            dollars += (tokens["prompt_tokens"] * rate["input"] + tokens["completion_tokens"] * rate["output"]) / 1_000_000
        return {"mode": "live" if live else "offline", "model": model or config.GROQ_MODEL, "improved": improved, "conversations": len(cases), "turns": len(records), "failed_turns": failures, "pass_rate": round(1 - failures / len(records), 4), "repeated_questions": repeat_count, "handoff_routes": {"expected": handoffs_expected, "correct": handoffs_routed, "delivery_verified": False}, "latency_ms": {"p50": round(statistics.median(latencies), 2), "p95": round(sorted(latencies)[int(len(latencies) * .95)], 2)}, "mean_reply_words": round(statistics.mean(lengths), 1), "provider_usage": dict(usage), "provider_cost_usd": dollars, "records": records}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", help="Groq model to benchmark; does not change production configuration.")
    parser.add_argument("--rates", type=Path)
    args = parser.parse_args()
    rates = json.loads(args.rates.read_text()) if args.rates else None
    report = {"dataset": "180 synthetic scenarios / 220 turns; not sampled customer conversations", "limitations": "Route/content/intent/entity/action checks are proxies for answer quality. Repeated questions use exact normalized text. No real handoff delivery is attempted. Offline latency excludes provider/network latency.", "runs": [evaluate(scenarios(), improved, args.live, rates, args.model) for improved in (False, True)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps([{k: v for k, v in run.items() if k != "records"} for run in report["runs"]], indent=2))
