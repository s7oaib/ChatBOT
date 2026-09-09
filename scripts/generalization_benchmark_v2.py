"""Extended adversarial benchmark (~200 turns) with route-source measurement.

Version 1 (`generalization_benchmark.py`) stays untouched as the earlier
reference. This one widens every group, adds vague questions that should be
clarified rather than guessed, and records *which layer* decided each turn —
model understanding, the deterministic semantic reader, a state handler, an
explicit rule, or the legacy trigger matcher. That last number is the one to
watch: it says how much of the bot still thinks in customer phrasings.

Run:  python scripts/generalization_benchmark_v2.py --output scratch/general2.json
      python scripts/generalization_benchmark_v2.py --output scratch/general2.json --mode model
"""
import argparse
from collections import defaultdict
from contextlib import ExitStack
import json
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Fragments that appear only when a topic answer was sent. The handover copy
# lists what the bot can help with, so its own mentions do not count.
OFF_SUBJECT_MARKERS = (
    "cornstarch", "shelf life", "refrigerat", "packed in", "price breakdown",
    "piece count", "we deliver right across",
)

SAFE_CLARIFICATION_ROUTES = {
    "clarify_request", "price_clarify_product", "context_meal_price_followup",
}


def cases():
    out = []

    def add(group, turns):
        out.append({"id": f"{group}-{len(out):03d}", "group": group, "turns": turns})

    def one(group, message, **expect):
        add(group, [{"message": message, **expect}])

    # A. unseen paraphrases (40)
    for message in [
        "What are you selling these days?", "Could you share what you offer?",
        "I'd love to see your list of items", "Could you show me what you sell?",
        "What choices are there?", "What can I get from you?",
        "Which sweets are currently on your menu?", "I want to know what you offer",
        "What Bengali sweets do you make?", "Tell me what mishti you prepare",
        "Which sweets are your speciality?", "Show me your range",
        "What would a box of Jolbhora set me back?", "How expensive is Jolbhora?",
        "What am I looking at price-wise for Aamar Mishti?",
        "What sort of money is Kanchagolla?", "Give me an idea of your prices",
        "How much are your sweets roughly?",
        "Would you be able to bring it to Noida?", "Is Gurgaon within your delivery range?",
        "Can you send an order across to Faridabad?", "Do you come out as far as Dwarka?",
        "Would you ship to Ghaziabad?",
        "What's the procedure for placing an order?", "How would I go about buying from you?",
        "Walk me through ordering please", "What's the process to buy a box?",
        "How does ordering work with you?",
        "How should I keep the sondesh once it arrives?", "Does it need to go in the fridge?",
        "How long will it stay good?", "What's the best way to store it?",
        "What do you pack the sweets in?", "Are the boxes environmentally friendly?",
        "Tell me about your everyday meal plan", "How does the weekly meal package work?",
        "What is in this week's vegetarian selection?", "What's on the non veg menu this week?",
        "Which flavours come in a 250g box?", "How many pieces in a 500g box?",
    ]:
        one("paraphrase", message, expect="answered")

    # B. Hinglish / Indian English (30)
    for message, expectation, route in [
        ("menu bhejo", "answered", None), ("menu bhejo please", "answered", None),
        ("menu dikhao", "answered", None), ("aaj ka menu kya hai", "answered", None),
        ("kitna padega", "route", "price_clarify_product"),
        ("kitne paise", "route", "price_clarify_product"),
        ("kitna hai", "route", "price_clarify_product"),
        ("sondesh ka rate kya hai", "answered", None),
        ("Jolbhora ka rate kya hai", "answered", None),
        ("mishti ka daam batao", "answered", None),
        ("delivery hota hai kya", "answered", None),
        ("Delhi me bhejte ho?", "answered", None),
        ("Noida me delivery hoti hai", "answered", None),
        ("gurgaon me deliver karte ho", "answered", None),
        ("order kaise karu", "answered", None),
        ("order kaise karna hai", "answered", None),
        ("kaise order kare", "answered", None),
        ("mujhe order karna hai", "answered", None),
        ("sweets chahiye", "answered", None),
        ("mishti chahiye", "answered", None),
        ("veg menu bhejo", "answered", None),
        ("non veg menu bhejo", "answered", None),
        ("subscription ke bare me batao", "answered", None),
        ("meal plan kya hai", "answered", None),
        ("payment kaise karu", "answered", None),
        ("sondesh kaise store kare", "answered", None),
        ("packing kaisi hoti hai", "answered", None),
        ("kal delivery ho sakti hai kya", "safe", None),
        ("bulk order karna hai shaadi ke liye", "unknown", None),
        ("aapka number kya hai", "answered", None),
    ]:
        turn = {"message": message, "expect": expectation}
        if route:
            turn["route"] = route
        add("hinglish", [turn])

    # C. unsupported business topics (32)
    for message in [
        "Do you have washrooms?", "Do you have toilets?", "Is there wheelchair access?",
        "Do you have parking?", "Is parking free?", "Do you offer valet?",
        "Can I bring my pets?", "Can pets come inside?", "Do you allow dogs inside?",
        "Do you have seating?", "Is there outdoor seating?", "Can we sit inside?",
        "Do you have air conditioning?", "Is there AC?",
        "Can I reserve a table for four?", "Do you take table bookings for tonight?",
        "Is there a charging point I can use?", "Can I charge my phone?",
        "Do you have Wi-Fi?", "Can I smoke there?", "Do you have a smoking area?",
        "Can I take photographs inside the kitchen?", "Do you allow filming?",
        "Do you have a play area for children?", "Is the building lift accessible?",
        "Do you have a loyalty card?", "Can I hire your chef for an evening?",
        "Do you offer cooking classes?", "Is there a dress code?",
        "Do you validate parking tickets?", "Do you have a waiting area?",
        "Can I use a coupon from another shop?",
    ]:
        one("unsupported_topic", message, expect="unknown")

    # D. multi-intent, both halves supported (30)
    for message in [
        "Show me the menu and tell me if you deliver to Delhi",
        "What is the price of Jolbhora and do you deliver to Gurgaon?",
        "Can I see the veg menu and how do I order?",
        "How much is Aamar Mishti and how should I store it?",
        "What sweets do you have and do you deliver to Noida?",
        "Tell me about the subscription and how do I book it?",
        "Do you deliver to Noida and how do I pay?",
        "What is the veg menu and what is the non veg menu?",
        "How much is a 250g box and what is in it?",
        "Do you have Jolbhora and what does it contain?",
        "Price of Kanchagolla and do you deliver to Delhi?",
        "Show me the menu and how do I place an order?",
        "What is in the bento and how much is it?",
        "Do you deliver to Mumbai and what is the price of sondesh?",
        "How do I order and how do I pay?",
        "What is the price of Mahua and how is it packed?",
        "Menu bhejo and delivery hoti hai kya",
        "Jolbhora ka rate aur delivery Delhi me hoti hai kya",
        "Tell me about Aamar Mishti and the Signature collection",
        "How should I store it and how long does it last?",
        "What is the subscription price and how do I change a delivery date?",
        "Do you have nolen gur and what does it cost?",
        "Which sweets are gluten free and how are they packed?",
        "Do you deliver to Faridabad and what are the charges?",
        "What is this week's veg menu and do you deliver to Dwarka?",
        "Price of a 1kg box and how many pieces is that?",
        "How do I order the bento and what is in it?",
        "What is the difference between Collection 1 and Collection 2 and what do they cost?",
        "Do you have kanchagolla and is it made with milk?",
        "Show me the non veg menu and tell me the delivery time",
    ]:
        one("multi_intent", message, expect="answered")

    # E. mixed supported + unsupported (22)
    for message in [
        "What is the price of Jolbhora and can I get a 30% discount?",
        "How much is Kanchagolla and is it available today?",
        "Do you deliver to Delhi and do you have parking?",
        "Show me the menu and can I get free delivery?",
        "What does Jolbhora cost and can I bring my dog?",
        "I need 100 boxes for a wedding and what is the price?",
        "How do I order and do you have washrooms?",
        "What is in the bento and do you have seating?",
        "Price of Mahua and do you gift wrap?",
        "What's Jolbhora price and do you gift wrap?",
        "Show me the menu and can I reserve a table?",
        "Do you deliver to Noida and can you guarantee 30 minutes?",
        "What is the subscription price and can I cancel anytime?",
        "How much is a 250g box and do you do custom boxes?",
        "Tell me about Aamar Mishti and can I get a bulk discount?",
        "What is this week's menu and do you have wheelchair access?",
        "How do I pay and do you have a loyalty card?",
        "What sweets do you have and is there AC?",
        "Do you deliver to Delhi and can I smoke there?",
        "Price of sondesh and can I charge my phone there?",
        "How is it packed and can you decorate the box?",
        "What is the veg menu and do you allow pets?",
    ]:
        one("mixed_intent", message, expect="partial")

    # F. state interruptions (20 turns across 8 conversations)
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
    add("state_interruption", [
        {"message": "how much", "expect": "route", "route": "price_clarify_product"},
        {"message": "sondesh", "expect": "answered"},
    ])
    add("state_interruption", [
        {"message": "menu bhejo", "expect": "answered"},
        {"message": "veg", "expect": "answered"},
    ])

    # G. hallucination probes (20)
    for message, forbidden in [
        ("Can I get a 30% discount?", ["discount", "30%"]),
        ("Can I get 40% discount?", ["40%", "discount"]),
        ("Do you offer free delivery over 2000?", ["free delivery"]),
        ("Is Jolbhora available today?", ["available today", "in stock"]),
        ("Is Jolbhora available right now?", ["available right now", "in stock"]),
        ("Do you have a sugar-free Jolbhora?", ["sugar-free jolbhora"]),
        ("Does Jolbhora contain almonds and cashews?", ["contains almonds"]),
        ("Can you deliver in 30 minutes to Mumbai?", ["30 minutes", "we deliver to mumbai"]),
        ("Can you guarantee delivery in 30 minutes?", ["guarantee", "30 minutes"]),
        ("What time do you close on Sundays?", ["sunday"]),
        ("Do you have a mango sondesh for 100 rupees?", [r"\b100 rupees\b"]),
        ("Is your kitchen halal certified?", ["halal certified"]),
        ("Can you make it vegan?", ["is vegan", "vegan option", "we can make it vegan"]),
        ("Do you gift wrap?", ["we gift wrap", "yes, we wrap"]),
        ("Do you provide birthday wrapping?", ["birthday wrapping is"]),
        ("Can you decorate the box?", ["we decorate"]),
        ("Do you deliver to Bangalore?", ["yes, we deliver to bangalore"]),
        ("Can I cancel my subscription anytime?", ["cancel anytime", "free cancellation"]),
        ("Do you use organic milk?", ["organic"]),
        ("Is the sondesh preservative free?", ["preservative free", "preservative-free"]),
    ]:
        one("hallucination_probe", message, expect="no_claim", forbidden=forbidden)

    # H. vague questions that should be clarified, not guessed (12)
    for message in [
        "price?", "how much?", "info", "details please", "tell me more",
        "what about the other one?", "and the second one?", "is it good?",
        "can you help?", "I have a question", "quick question", "hello?",
    ]:
        one("vague", message, expect="safe")

    return out


def evaluate(mode):
    from camellia.config import config
    import httpx

    with tempfile.TemporaryDirectory(prefix="camellia-general2-") as tmp, ExitStack() as stack:
        tmp = Path(tmp)
        menu = json.loads(Path(config.MENU_JSON_PATH).read_text(encoding="utf-8"))
        menu["weekly_menus"][-1]["week_of"] = "1st January - 31st December, 2099"
        (tmp / "menu.json").write_text(json.dumps(menu), encoding="utf-8")
        (tmp / "qa.json").write_text(
            Path(config.QA_TEMPLATES_PATH).read_text(encoding="utf-8"), encoding="utf-8")
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
            patch.object(session_memory, "_sessions", type(session_memory._sessions)()))

        def blocked(*args, **kwargs):
            raise AssertionError("The benchmark blocked an external request")

        stack.enter_context(patch.object(httpx.Client, "post", blocked))

        if mode == "model":
            def simulated(message, history_turns=None, state=None):
                result = understanding_engine.deterministic_fallback(message)
                result.reason = "model_nlu_simulated"
                result.available = True
                result.confidence = 0.92
                return result

            stack.enter_context(
                patch.object(understanding_engine, "analyze", side_effect=simulated))
            stack.enter_context(patch.object(
                ai_responder, "compose_verified_response",
                return_value=("", False, "provider_unavailable")))

        records = []
        for case in cases():
            for number, turn in enumerate(case["turns"]):
                reply, matched, route, _images = responder.match_and_respond(
                    turn["message"], user_id=case["id"], channel="whatsapp")
                facts = session_memory.get_facts(case["id"], "whatsapp")
                sufficiency = dict(facts.get("last_knowledge_sufficiency") or {})
                source = facts.get("last_route_source")
                lowered = (reply or "").lower()
                expectation = turn["expect"]
                errors = []

                if expectation == "answered":
                    if not matched:
                        errors.append("not_answered")
                elif expectation == "route":
                    if route != turn["route"]:
                        errors.append(f"wrong_route:{route}")
                elif expectation == "unknown":
                    if matched and route not in SAFE_CLARIFICATION_ROUTES:
                        errors.append(f"answered_anyway:{route}")
                    for marker in OFF_SUBJECT_MARKERS:
                        if marker in lowered:
                            errors.append(f"off_subject_content:{marker}")
                            break
                elif expectation == "partial":
                    if not sufficiency:
                        errors.append("no_sufficiency_decision")
                    elif all(sufficiency.values()):
                        errors.append("unsupported_half_not_flagged")
                elif expectation == "no_claim":
                    for phrase in turn["forbidden"]:
                        pattern = phrase.lower()
                        hit = (
                            re.search(pattern, lowered) if pattern.startswith(r"\b")
                            else pattern in lowered
                        )
                        if hit:
                            errors.append(f"invented:{phrase}")
                elif expectation == "safe":
                    # Anything is fine except stating a business fact we did not
                    # establish: a clarification, a menu prompt or a handover.
                    for marker in ("cornstarch", "shelf life", "refrigerat"):
                        if marker in lowered:
                            errors.append(f"off_subject_content:{marker}")
                            break

                if matched and route and expectation in {"answered", "route"}:
                    exempt = SAFE_CLARIFICATION_ROUTES | {
                        "delivery_area_confirmation", "acknowledgement",
                        "conversation_closing", "greeting", "bot_identity",
                        "bot_capabilities", "product_recommendation",
                    }
                    if not facts.get("last_knowledge_ids") and route not in exempt:
                        errors.append("no_knowledge_source")

                records.append({
                    "case": case["id"], "group": case["group"], "turn": number,
                    "message": turn["message"], "expect": expectation,
                    "route": route, "matched": matched, "route_source": source,
                    "sufficiency": sufficiency,
                    "knowledge_ids": list(facts.get("last_knowledge_ids") or []),
                    "reply": (reply or "")[:300], "errors": errors,
                })

        by_group = defaultdict(lambda: {"turns": 0, "failed": 0})
        by_source = defaultdict(int)
        for record in records:
            bucket = by_group[record["group"]]
            bucket["turns"] += 1
            bucket["failed"] += bool(record["errors"])
            by_source[record["route_source"] or "none"] += 1
        failed = sum(bool(record["errors"]) for record in records)
        return {
            "mode": mode,
            "turns": len(records),
            "failed_turns": failed,
            "pass_rate": round(1 - failed / len(records), 4),
            "by_group": {
                group: {**stats, "pass_rate": round(1 - stats["failed"] / stats["turns"], 4)}
                for group, stats in sorted(by_group.items())
            },
            "route_source_counts": dict(sorted(by_source.items())),
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
                          ("mode", "turns", "failed_turns", "pass_rate",
                           "by_group", "route_source_counts")}, indent=2))
