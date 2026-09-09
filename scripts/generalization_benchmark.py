"""Adversarial benchmark: does the architecture generalize, or only recall?

The 220-turn conversation benchmark is a regression net — it measures wordings
the bot has already met. This one measures the opposite: paraphrases nobody
wrote a trigger for, restaurant questions the business has no answer to,
compound asks, interruptions to an open flow, Hinglish, and probes that invite
the bot to invent a fact.

Nothing here is scored on wording. A case passes when the bot either answers
from approved knowledge that is actually about the question, or says it does
not know and hands over.

Run:  python scripts/generalization_benchmark.py --output scratch/general.json
      python scripts/generalization_benchmark.py --output scratch/general.json --mode model

  --mode outage   understanding is unavailable (default; no provider quota used)
  --mode model    understanding returns a validated result for every message,
                  simulated from the deterministic reading — this exercises the
                  intent/entity path, and is NOT a live-provider measurement.
"""
import argparse
from collections import defaultdict
from contextlib import ExitStack
import json
import re
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Copy fragments that must never answer an off-subject question.
# Fragments that only appear when a *topic answer* was sent. The handover copy
# legitimately lists what the bot can help with, so its mentions of delivery or
# the menu are not evidence that an off-subject question was answered.
OFF_SUBJECT_MARKERS = (
    "cornstarch", "shelf life", "refrigerat", "packed in", "price breakdown",
    "piece count", "we deliver right across",
)


def cases():
    """Every case states what must be true of the reply, never its wording."""
    out = []

    def add(group, turns):
        out.append({"id": f"{group}-{len(out):03d}", "group": group, "turns": turns})

    # --- paraphrases the trigger lists were not written for ----------------
    paraphrases = [
        ("What are you selling these days?", "menu"),
        ("Could you share what you offer?", "menu"),
        ("I'd love to see your list of items", "menu"),
        ("What Bengali sweets do you make?", "product"),
        ("Tell me what mishti you prepare", "product"),
        ("Which sweets are your speciality?", "product"),
        ("What would a box of Jolbhora set me back?", "price"),
        ("How expensive is Jolbhora?", "price"),
        ("What am I looking at price-wise for Aamar Mishti?", "price"),
        ("Would you be able to bring it to Noida?", "delivery"),
        ("Is Gurgaon within your delivery range?", "delivery"),
        ("Can you send an order across to Faridabad?", "delivery"),
        ("What's the procedure for placing an order?", "order"),
        ("How would I go about buying from you?", "order"),
        ("Walk me through ordering please", "order"),
        ("How should I keep the sondesh once it arrives?", "storage"),
        ("Does it need to go in the fridge?", "storage"),
        ("What do you pack the sweets in?", "packaging"),
        ("Are the boxes environmentally friendly?", "packaging"),
        ("Tell me about your everyday meal plan", "subscription"),
        ("How does the weekly meal package work?", "subscription"),
        ("What is in this week's vegetarian selection?", "menu"),
    ]
    for message, topic in paraphrases:
        add("paraphrase", [{"message": message, "expect": "answered", "topic": topic}])

    # --- restaurant-adjacent questions with no approved answer -------------
    unsupported = [
        "Do you have washrooms?",
        "Is there wheelchair access?",
        "Do you have parking?",
        "Do you offer valet?",
        "Can I bring my pets?",
        "Do you allow dogs inside?",
        "Do you have seating?",
        "Is there outdoor seating?",
        "Do you have air conditioning?",
        "Can I reserve a table for four?",
        "Do you take table bookings for tonight?",
        "Is there a charging point I can use?",
        "Do you have Wi-Fi?",
        "Can I smoke there?",
        "Can I take photographs inside the kitchen?",
        "Do you have a play area for children?",
        "Is the building lift accessible?",
        "Do you have a loyalty card?",
        "Can I hire your chef for an evening?",
        "Do you offer cooking classes?",
        "Is there a dress code?",
        "Do you validate parking tickets?",
    ]
    for message in unsupported:
        add("unsupported_topic", [{"message": message, "expect": "unknown"}])

    # --- compound asks ------------------------------------------------------
    multi = [
        ("Show me the menu and tell me if you deliver to Delhi", "answered"),
        ("What is the price of Jolbhora and do you deliver to Gurgaon?", "answered"),
        ("Can I see the veg menu and how do I order?", "answered"),
        ("How much is Aamar Mishti and how should I store it?", "answered"),
        ("What sweets do you have and do you deliver to Noida?", "answered"),
        ("Tell me about the subscription and how do I book it?", "answered"),
        ("What is the price of Jolbhora and can I get a 30% discount?", "partial"),
        ("How much is Kanchagolla and is it available today?", "partial"),
        ("Do you deliver to Delhi and do you have parking?", "partial"),
        ("Show me the menu and can I get free delivery?", "partial"),
        ("What does Jolbhora cost and can I bring my dog?", "partial"),
        ("I need 100 boxes for a wedding and what is the price?", "partial"),
        # Mumbai has an approved answer of its own ("we cannot ship there"),
        # so both halves are supported.
        ("Do you deliver to Mumbai and what is the price of sondesh?", "answered"),
        ("How do I order and do you have washrooms?", "partial"),
        ("What is in the bento and do you have seating?", "partial"),
        ("Price of Mahua and do you gift wrap?", "partial"),
        ("Do you deliver to Noida and how long does it take?", "answered"),
        ("What is the veg menu and what is the non veg menu?", "answered"),
        ("How much is a 250g box and what is in it?", "answered"),
        ("Do you have Jolbhora and what does it contain?", "answered"),
    ]
    for message, expectation in multi:
        add("multi_intent", [{"message": message, "expect": expectation}])

    # --- interruptions to an open flow -------------------------------------
    add("state_interruption", [
        {"message": "Do you deliver?", "expect": "answered"},
        {"message": "what is the price", "expect": "route", "route": "price_clarify_product"},
    ])
    add("state_interruption", [
        {"message": "Do you deliver?", "expect": "answered"},
        {"message": "Chatra Marg", "expect": "route", "route": "delivery_area_confirmation"},
    ])
    add("state_interruption", [
        {"message": "how much", "expect": "route", "route": "price_clarify_product"},
        {"message": "cancel my order", "expect": "unknown"},
    ])
    add("state_interruption", [
        {"message": "show me the menu", "expect": "answered"},
        {"message": "I want to speak to a human", "expect": "unknown"},
    ])
    add("state_interruption", [
        {"message": "tell me about the subscription", "expect": "answered"},
        {"message": "do you deliver to noida", "expect": "answered"},
    ])
    add("state_interruption", [
        {"message": "tell me about the subscription", "expect": "answered"},
        {"message": "yes i want to book", "expect": "route", "route": "subscription_booking_steps"},
        {"message": "what if i need to change a delivery date", "expect": "route",
         "route": "subscription_booking_steps"},
    ])
    add("state_interruption", [
        {"message": "how much", "expect": "route", "route": "price_clarify_product"},
        {"message": "meals", "expect": "route", "route": "context_meal_price_followup"},
        {"message": "veg", "expect": "route", "route": "weekly_menu_veg"},
    ])
    add("state_interruption", [
        {"message": "Do you deliver?", "expect": "answered"},
        {"message": "wrong order delivered", "expect": "unknown"},
    ])

    # --- Indian English / Hinglish -----------------------------------------
    hinglish = [
        ("menu bhejo", "answered"),
        ("menu bhejo please", "answered"),
        ("kitna padega", "route", "price_clarify_product"),
        ("kitne paise", "route", "price_clarify_product"),
        ("delivery hota hai kya", "answered"),
        ("order kaise karu", "answered"),
        ("sondesh ka price kya hai", "answered"),
        ("aaj kya hai menu me", "answered"),
        ("mishti chahiye", "answered"),
        ("khana deliver karte ho noida me", "answered"),
    ]
    for entry in hinglish:
        turn = {"message": entry[0], "expect": entry[1]}
        if len(entry) > 2:
            turn["route"] = entry[2]
        add("hinglish", [turn])

    # --- probes that invite an invented fact --------------------------------
    probes = [
        ("Can I get a 30% discount?", ["discount", "%", "off"]),
        ("Do you offer free delivery over ₹2000?", ["free delivery"]),
        ("Is Jolbhora available today?", ["available today", "in stock"]),
        ("Do you have a sugar-free Jolbhora?", ["sugar-free jolbhora"]),
        ("Does Jolbhora contain almonds and cashews?", ["contains almonds"]),
        ("Can you deliver in 30 minutes to Mumbai?", ["30 minutes to mumbai", "we deliver to mumbai"]),
        ("What time do you close on Sundays?", ["sunday"]),
        ("Do you have a mango sondesh for ₹100?", [r"₹100"]),
        ("Is your kitchen halal certified?", ["halal certified"]),
        ("Can you make it vegan?", ["is vegan", "vegan option", "we can make it vegan"]),
    ]
    for message, forbidden in probes:
        add("hallucination_probe", [{"message": message, "expect": "no_claim", "forbidden": forbidden}])

    return out


def evaluate(mode):
    from camellia.config import config
    import httpx

    with tempfile.TemporaryDirectory(prefix="camellia-general-") as tmp, ExitStack() as stack:
        tmp = Path(tmp)
        menu = json.loads(Path(config.MENU_JSON_PATH).read_text(encoding="utf-8"))
        menu["weekly_menus"][-1]["week_of"] = "1st January - 31st December, 2099"
        (tmp / "menu.json").write_text(json.dumps(menu), encoding="utf-8")
        (tmp / "qa.json").write_text(
            Path(config.QA_TEMPLATES_PATH).read_text(encoding="utf-8"), encoding="utf-8",
        )
        for key, value in {
            "DB_PATH": str(tmp / "test.db"),
            "MENU_JSON_PATH": str(tmp / "menu.json"),
            "QA_TEMPLATES_PATH": str(tmp / "qa.json"),
            "GROQ_ENABLED": False,
            "ROUTER_SHADOW_ENABLED": False,
            "CONVERSATION_IMPROVEMENTS_ENABLED": True,
            "STRUCTURED_NLU_ENABLED": True,
            "STRUCTURED_NLU_ROLLOUT_PERCENT": 100,
            "SLOW_REPLY_NOTICE_ENABLED": False,
        }.items():
            stack.enter_context(patch.object(config, key, value))

        from camellia.database import db
        from camellia.responder import responder
        from camellia.ai_responder import ai_responder
        from camellia.rag_fallback import rag_fallback_engine
        from camellia.session_memory import session_memory
        from camellia.understanding import understanding_engine

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
            engine.reload_data()
        stack.enter_context(
            patch.object(session_memory, "_sessions", type(session_memory._sessions)())
        )

        def blocked(*args, **kwargs):
            raise AssertionError("The generalization benchmark blocked an external request")

        stack.enter_context(patch.object(httpx.Client, "post", blocked))

        if mode == "model":
            def simulated(message, history_turns=None, state=None):
                result = understanding_engine.deterministic_fallback(message)
                result.reason = "model_nlu_simulated"
                result.available = True
                result.confidence = 0.92
                return result

            stack.enter_context(
                patch.object(understanding_engine, "analyze", side_effect=simulated)
            )
            stack.enter_context(patch.object(
                ai_responder, "compose_verified_response",
                return_value=("", False, "provider_unavailable"),
            ))

        records = []
        for case in cases():
            for number, turn in enumerate(case["turns"]):
                reply, matched, route, _images = responder.match_and_respond(
                    turn["message"], user_id=case["id"], channel="whatsapp",
                )
                facts = session_memory.get_facts(case["id"], "whatsapp")
                sufficiency = dict(facts.get("last_knowledge_sufficiency") or {})
                lowered = (reply or "").lower()
                errors = []
                expectation = turn["expect"]

                if expectation == "answered":
                    if not matched:
                        errors.append("not_answered")
                elif expectation == "route":
                    if route != turn["route"]:
                        errors.append(f"wrong_route:{route}")
                elif expectation == "unknown":
                    # Handing over and asking which topic the customer meant are
                    # both safe. Sending an answer about something else is not.
                    if matched and route not in {"clarify_request", "price_clarify_product"}:
                        errors.append(f"answered_anyway:{route}")
                    for marker in OFF_SUBJECT_MARKERS:
                        if marker in lowered:
                            errors.append(f"off_subject_content:{marker}")
                            break
                elif expectation == "partial":
                    if sufficiency and all(sufficiency.values()):
                        errors.append("unsupported_half_not_flagged")
                    if not sufficiency:
                        errors.append("no_sufficiency_decision")
                elif expectation == "no_claim":
                    for phrase in turn["forbidden"]:
                        pattern = phrase.lower()
                        found = (
                            re.search(pattern, lowered) if pattern.endswith(r"")
                            else pattern in lowered
                        )
                        if found:
                            errors.append(f"invented:{phrase}")

                if matched and route and expectation in {"answered", "route"}:
                    if not facts.get("last_knowledge_ids") and route not in {
                        "price_clarify_product", "context_meal_price_followup",
                        "delivery_area_confirmation", "acknowledgement",
                        "conversation_closing", "greeting", "clarify_request",
                        "bot_identity", "bot_capabilities", "product_recommendation",
                    }:
                        errors.append("no_knowledge_source")

                records.append({
                    "case": case["id"], "group": case["group"], "turn": number,
                    "message": turn["message"], "expect": expectation,
                    "route": route, "matched": matched,
                    "sufficiency": sufficiency,
                    "knowledge_ids": list(facts.get("last_knowledge_ids") or []),
                    "reply": (reply or "")[:400],
                    "errors": errors,
                })

        by_group = defaultdict(lambda: {"turns": 0, "failed": 0})
        for record in records:
            bucket = by_group[record["group"]]
            bucket["turns"] += 1
            bucket["failed"] += bool(record["errors"])
        failed = sum(bool(record["errors"]) for record in records)
        return {
            "mode": mode,
            "turns": len(records),
            "failed_turns": failed,
            "pass_rate": round(1 - failed / len(records), 4),
            "by_group": {
                group: {
                    **stats,
                    "pass_rate": round(1 - stats["failed"] / stats["turns"], 4),
                }
                for group, stats in sorted(by_group.items())
            },
            "records": records,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=["outage", "model", "both"], default="both")
    args = parser.parse_args()

    modes = ["outage", "model"] if args.mode == "both" else [args.mode]
    runs = [evaluate(mode) for mode in modes]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"runs": runs}, indent=2), encoding="utf-8")
    for run in runs:
        print(json.dumps({key: run[key] for key in
                          ("mode", "turns", "failed_turns", "pass_rate", "by_group")}, indent=2))
