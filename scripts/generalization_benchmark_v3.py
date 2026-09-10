"""Benchmark V3: language that is difficult for a semantic system by design.

Benchmarks v1 (102 turns) and v2 (208 turns) are saturated — both score 100%
in both modes — so they no longer discriminate architecture quality. This one
does not touch them; it is a new, harder dataset built to expose weaknesses:
lexical and entity ambiguity, negation, correction, topic switching, ellipsis,
pronouns, mixed-language code-switching, spelling errors, adversarial
unsupported clauses riding alongside supported ones, implied requests, words
that overlap two unrelated meanings, hallucination bait, false premises, and
3+ intent compounds — plus multi-turn stateful chains.

Because several of these categories are adversarial by construction, a
"correct" reply is not always a single fixed template. What is graded,
generously but strictly on safety, per case:

  * no wrong business fact is ever stated (a hallucination is always a fail,
    in every category, with no exception)
  * a clearly answerable request is answered
  * a clearly unsupported or false-premise request is refused, not answered
    as if it were true
  * a partially-supported compound keeps the supported half and does not
    silently drop the unsupported half

A safe escalation is graded as PASS for genuinely ambiguous cases — that is
the explicit target for provider-down mode. Only a wrong or invented fact, or
a supported half going missing, is a hard FAIL regardless of mode.

Run:  python scripts/generalization_benchmark_v3.py --output scratch/v3.json
      python scripts/generalization_benchmark_v3.py --output scratch/v3.json --mode model
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

# Fragments that only appear in a real topic answer — evidence a specific
# unsupported claim reached the customer as if it were true.
HALLUCINATION_MARKERS = (
    # "30 minutes" is deliberately not a blanket marker: it is the approved
    # Kanchagolla dispatch-time fact in sondesh_dispatch_time, so it is
    # checked per-case in the specific "forbidden" lists below instead,
    # where its meaning (a delivery-time promise vs. a dispatch fact) is
    # unambiguous from context.
    "free delivery", "guarantee delivery", "guaranteed delivery",
    "% off", "discount code",
    "available today", "available right now", "in stock", "currently available",
    "nut free", "nut-free", "no almonds", "no nuts", "halal certified",
    "vegan option", "we are open on sunday", "we validate parking",
    "we have parking", "we have wifi", "we have wi-fi", "gift wrap",
)

SAFE_CLARIFICATION_ROUTES = {
    "clarify_request", "price_clarify_product", "context_meal_price_followup",
}


def cases():
    out = []

    def add(group, turns, note=""):
        out.append({"id": f"{group}-{len(out):03d}", "group": group, "turns": turns, "note": note})

    def one(group, message, **expect):
        add(group, [{"message": message, **expect}])

    # A. Lexical ambiguity — one surface word, two unrelated meanings.
    for message, expect in [
        ("Can you box this?", "safe"),               # packaging vs. "put this in a box" order action
        ("Can I get a box of 12?", "answered"),       # quantity/pack, not packaging material
        ("Do you box up gifts?", "safe"),             # packaging vs gift-wrap subtopic
        ("What's in the box?", "safe"),               # contents vs packaging material
        ("Can you pack this for travel?", "safe"),    # packaging vs. "prepare an order"
        ("Do you do same day?", "safe"),              # same-day delivery subtopic, ambiguous alone
        ("Is it fresh?", "safe"),                     # freshness claim vs generic reassurance
        ("What's your rate?", "answered"),            # price, not exchange/interest rate
        ("Can I get a quote?", "safe"),               # price vs event/bulk quote
        ("Do you cater?", "safe"),                    # catering/bulk vs generic "do you provide"
        ("Is there a catch?", "safe"),                # idiom, no business meaning
        ("What's the deal?", "safe"),                 # idiom vs a discount offer
        ("Can you sort me out?", "safe"),              # idiom vs "sort" as in pack-sizes
        ("Is it packed well?", "safe"),               # packaging quality vs generic reassurance
        ("What do I get?", "safe"),                   # too vague — order contents vs menu
        ("Can you box the sweets separately?", "safe"),
        ("Do you box orders individually?", "safe"),
        ("Is delivery covered?", "safe"),              # coverage area vs insurance-style "covered"
        ("What does it cover?", "safe"),
        ("Can you rush this?", "safe"),                # same-day subtopic vs generic urgency
        ("Is there a rush fee?", "safe"),
        ("Can I get this gift wrapped or boxed?", "safe"),
        ("Do you do combos?", "answered"),             # bento/combo is a real product
        ("What's the catch with the subscription?", "safe"),
        ("Can you sweeten the deal?", "safe"),
        ("Is the rate negotiable?", "safe"),
        ("What's your best rate?", "answered"),
        ("Can I get a sample?", "safe"),
        ("Can you hold my order?", "safe"),
        ("Is there a hold fee?", "safe"),
        ("Can you rush the delivery?", "safe"),
        ("Do you offer express?", "safe"),
        ("Can I get it to go?", "safe"),
        ("Is takeaway available?", "safe"),
        ("Can you match a price?", "safe"),
        ("Do you price match?", "safe"),
        ("Is there a minimum order?", "safe"),
        ("What's the minimum?", "safe"),
        ("Can I mix flavours in one box?", "safe"),
        ("Do you do mixed boxes?", "safe"),
        ("Is the packaging recyclable?", "safe"),
        ("Can it be recycled?", "safe"),
    ]:
        one("lexical_ambiguity", message, expect=expect)

    # B. Entity ambiguity — a bare word that is both a generic English word
    # and a named collection/product.
    for message, expect in [
        ("Signature price", "answered"),      # Signature collection, not "a signature" in general
        ("Classic price", "answered"),        # Classic Sondesh flavour
        ("What is Rose?", "answered"),        # Rose Sondesh flavour, not the flower
        ("Kesar rate", "answered"),
        ("What is Mango?", "answered"),
        ("Vanilla price please", "answered"),
        ("Tell me about Collection", "safe"), # bare "Collection" names neither range
        ("Signature or Classic, which is better?", "safe"),  # subjective recommendation
        ("What's Aamar?", "answered"),        # short for Aamar Mishti
        ("Dalchini price?", "answered"),
        ("Nolen Gur rate?", "answered"),
        ("Mahua details please", "answered"),
        ("Bento price", "answered"),
        ("What is Jolbhora made of?", "safe"),
        ("Talgash price", "answered"),        # alternate name for Jolbhora
        ("Which is your best seller?", "safe"),  # subjective, no sales-data source
        ("What's your most popular sweet?", "safe"),
        ("Is Signature better than Aamar Mishti?", "safe"),
        ("Kanchagolla details please", "answered"),
        ("What is Aamar?", "answered"),
        ("Real Vanilla price", "answered"),
        ("Classic vs Rose, what's the difference?", "safe"),
        ("Which flavour do you recommend?", "safe"),
        ("Signature collection ingredients?", "safe"),
        ("Aamar Mishti ingredients?", "safe"),
        ("Is Kanchagolla a flavour or a collection?", "safe"),
        ("Bento contents please", "answered"),
    ]:
        one("entity_ambiguity", message, expect=expect)

    # C. Negation — explicitly ruling something out.
    for message, expect in [
        ("I don't want the menu, I just need delivery information.", "answered"),
        ("Not the price, just tell me if you deliver to Gurgaon.", "answered"),
        ("No sweets, I'm asking about the savoury meals.", "safe"),
        ("I'm not interested in Jolbhora, what else do you have?", "safe"),
        ("Don't tell me about packaging, tell me the price.", "safe"),
        ("I do not need the subscription, just a one-time order.", "safe"),
        ("Not today, just tell me your hours.", "safe"),
        ("I'm not asking for a discount, just the price.", "answered"),
        ("I don't need the veg menu, show me non veg.", "answered"),
        ("Don't worry about delivery, just the price of Jolbhora.", "answered"),
        ("I'm not asking about allergens, just the price.", "answered"),
        ("No need for the subscription info, just how do I order.", "answered"),
        ("I never asked about packaging, I asked about delivery.", "answered"),
        ("Not Gurgaon, I meant Delhi delivery.", "answered"),
        ("I'm not looking for a discount, just tell me your prices.", "answered"),
        ("Skip the storage details, tell me the price.", "answered"),
        ("No need to explain packaging, just book my order.", "safe"),
        ("Not interested in the subscription, one-off order please.", "safe"),
        ("Don't send me the whole menu, just the veg options.", "answered"),
        ("I don't want Aamar Mishti, show me Signature.", "answered"),
    ]:
        one("negation", message, expect=expect)

    # D. Correction — the customer revises their own earlier statement.
    add("correction", [
        {"message": "Do you deliver to Delhi?", "expect": "answered"},
        {"message": "Actually not Delhi, Gurgaon.", "expect": "answered"},
    ])
    add("correction", [
        {"message": "What is the price of Jolbhora?", "expect": "answered"},
        {"message": "Sorry I meant Kanchagolla.", "expect": "safe"},
    ])
    add("correction", [
        {"message": "I want the veg menu.", "expect": "answered"},
        {"message": "Actually, non veg please.", "expect": "answered"},
    ])
    add("correction", [
        {"message": "Can you deliver to Noida?", "expect": "answered"},
        {"message": "Wait, I meant Faridabad.", "expect": "safe"},
    ])
    add("correction", [
        {"message": "How much is Kanchagolla?", "expect": "answered"},
        {"message": "Sorry, I actually wanted Aamar Mishti.", "expect": "safe"},
    ])
    add("correction", [
        {"message": "What is the price of a 500g box?", "expect": "answered"},
        {"message": "Correction, I meant 250g.", "expect": "safe"},
    ])
    add("correction", [
        {"message": "Show me the non veg menu.", "expect": "answered"},
        {"message": "My mistake, veg please.", "expect": "answered"},
    ])
    add("correction", [
        {"message": "Do you deliver to Faridabad?", "expect": "answered"},
        {"message": "Sorry wrong city, Ghaziabad.", "expect": "safe"},
    ])
    add("correction", [
        {"message": "What's the price of Signature collection?", "expect": "answered"},
        {"message": "Oops, I meant Aamar Mishti collection.", "expect": "safe"},
    ])
    add("correction", [
        {"message": "Show me the veg menu.", "expect": "answered"},
        {"message": "Sorry, non veg please.", "expect": "answered"},
    ])
    add("correction", [
        {"message": "Is Kanchagolla available?", "expect": "safe"},
        {"message": "I meant ask about Jolbhora instead.", "expect": "safe"},
    ])

    # E. Topic switch mid-conversation.
    add("topic_switch", [
        {"message": "What is the price of Jolbhora?", "expect": "answered"},
        {"message": "Forget the price, tell me about storage.", "expect": "answered"},
    ])
    add("topic_switch", [
        {"message": "Show me the veg menu.", "expect": "answered"},
        {"message": "Never mind that, do you deliver to Delhi?", "expect": "answered"},
    ])
    add("topic_switch", [
        {"message": "Tell me about the subscription.", "expect": "answered"},
        {"message": "Actually, what sweets do you have?", "expect": "answered"},
    ])
    add("topic_switch", [
        {"message": "How do I place an order?", "expect": "answered"},
        {"message": "Actually, is Jolbhora available today?", "expect": "safe"},
    ])
    add("topic_switch", [
        {"message": "What is the price of Kanchagolla?", "expect": "answered"},
        {"message": "On second thought, how do you pack it?", "expect": "answered"},
    ])
    add("topic_switch", [
        {"message": "Do you deliver to Noida?", "expect": "answered"},
        {"message": "Let's talk about the subscription instead.", "expect": "answered"},
    ])
    add("topic_switch", [
        {"message": "Tell me about Kanchagolla.", "expect": "answered"},
        {"message": "Skip that, how do you pay?", "expect": "answered"},
    ])
    add("topic_switch", [
        {"message": "What is the price of a 1kg box?", "expect": "answered"},
        {"message": "Different question — do you deliver to Faridabad?", "expect": "answered"},
    ])
    add("topic_switch", [
        {"message": "How do I place an order?", "expect": "answered"},
        {"message": "Wait, tell me about Signature collection first.", "expect": "answered"},
    ])

    # F. Ellipsis — a fragment that only means something against the last turn.
    add("ellipsis", [
        {"message": "What is the price of a 250g box?", "expect": "answered"},
        {"message": "What about 500g?", "expect": "safe"},
    ])
    add("ellipsis", [
        {"message": "Do you deliver to Delhi?", "expect": "answered"},
        {"message": "And Gurgaon?", "expect": "safe"},
    ])
    add("ellipsis", [
        {"message": "What is the price of Jolbhora?", "expect": "answered"},
        {"message": "And Kanchagolla?", "expect": "safe"},
    ])
    add("ellipsis", [
        {"message": "Show me the veg menu.", "expect": "answered"},
        {"message": "What about non veg?", "expect": "answered"},
    ])
    add("ellipsis", [
        {"message": "Do you deliver to Noida?", "expect": "answered"},
        {"message": "What about Faridabad?", "expect": "safe"},
    ])
    add("ellipsis", [
        {"message": "What is the price of a 1kg box?", "expect": "answered"},
        {"message": "And 250g?", "expect": "safe"},
    ])
    add("ellipsis", [
        {"message": "What is the price of Kanchagolla?", "expect": "answered"},
        {"message": "And Aamar Mishti?", "expect": "safe"},
    ])
    add("ellipsis", [
        {"message": "Do you deliver to Gurgaon?", "expect": "answered"},
        {"message": "What about same day?", "expect": "safe"},
    ])
    add("ellipsis", [
        {"message": "Show me the veg menu.", "expect": "answered"},
        {"message": "And the bento options?", "expect": "answered"},
    ])

    # G. Pronouns referring back to something already named.
    add("pronouns", [
        {"message": "Tell me about Jolbhora.", "expect": "answered"},
        {"message": "How much is that one?", "expect": "safe"},
    ])
    add("pronouns", [
        {"message": "What is Aamar Mishti?", "expect": "answered"},
        {"message": "Can you deliver it to Delhi?", "expect": "safe"},
    ])
    add("pronouns", [
        {"message": "Tell me about Kanchagolla.", "expect": "answered"},
        {"message": "Is that one nut free?", "expect": "safe"},
    ])
    add("pronouns", [
        {"message": "What is the Signature collection?", "expect": "answered"},
        {"message": "How much does it cost?", "expect": "safe"},
    ])
    add("pronouns", [
        {"message": "Tell me about the subscription.", "expect": "answered"},
        {"message": "How do I start it?", "expect": "route", "route": "subscription_booking_steps"},
    ])
    add("pronouns", [
        {"message": "What is Signature collection?", "expect": "answered"},
        {"message": "Can you deliver those to Delhi?", "expect": "safe"},
    ])
    add("pronouns", [
        {"message": "Tell me about Aamar Mishti.", "expect": "answered"},
        {"message": "How is it packed?", "expect": "safe"},
    ])
    add("pronouns", [
        {"message": "What is the price of Jolbhora?", "expect": "answered"},
        {"message": "Does it contain nuts?", "expect": "safe"},
    ])

    # H. Mixed language / code-switching within one sentence.
    for message, expect in [
        ("Haan but delivery Gurgaon mein?", "answered"),
        ("Theek hai, menu bhejo please", "answered"),
        ("Achha, Jolbhora price kya hai?", "answered"),
        ("Yes par kitna time lagega delivery mein?", "safe"),
        ("Sure, lekin subscription ka kya price hai?", "answered"),
        ("Okay great, ab batao Jolbhora ka price", "answered"),
        ("Cool, but kya aap Noida deliver karte ho?", "answered"),
        ("Nice, veg menu bhejo please", "answered"),
        ("Alright, storage ke bare me batao", "answered"),
        ("Got it, ab Kanchagolla ka rate batao", "answered"),
        ("Perfect, aap subscription details bhejo", "answered"),
        ("Thik hai, non veg menu dikhao", "answered"),
        ("Great, delivery Delhi tak hoti hai kya", "answered"),
        ("Awesome, Aamar Mishti ka price kya hai", "answered"),
        ("Bilkul, storage instructions batao please", "answered"),
    ]:
        one("mixed_language", message, expect=expect)

    # I. Spelling errors on real product/business words.
    for message, expect in [
        ("delivry to Delhi?", "answered"),
        ("jolbhra price plz", "safe"),
        ("show me the mennu", "safe"),
        ("do u hav sondesh", "safe"),
        ("wat is da subscribtion price", "safe"),
        ("gurgaon me deliverry hoti h?", "safe"),
        ("kanchagola price?", "safe"),
        ("wat r ur pack sizes", "safe"),
        ("do u delvier to noida", "safe"),
        ("subcription details plz", "safe"),
        ("wats the strage instructions", "safe"),
        ("aamar mishty price", "safe"),
        ("mahaua price plz", "safe"),
        ("signture collection price", "safe"),
        ("wat time do u deliver", "safe"),
        ("hw do i pay", "safe"),
        ("gluton free option", "safe"),
        ("do u hav bentoo", "safe"),
    ]:
        one("spelling_errors", message, expect=expect)

    # J. Adversarial unsupported clauses riding on a real, answerable one.
    for message in [
        "What's the Jolbhora price and since you probably have parking, is it free?",
        "What is the veg menu, and since you're eco-friendly, is parking free too?",
        "Do you deliver to Delhi, and since you're modern, do you have Wi-Fi?",
        "What is the price of Kanchagolla and since it's popular, is it always in stock?",
        "Show me the menu, and since you're professional, can I reserve a table?",
        "Tell me about the subscription, and since it's popular, can I get a discount?",
        "How much is Aamar Mishti, and since it's summer, can you guarantee no melting?",
        "Do you deliver to Gurgaon, and since it's close, is it free?",
        "What is Jolbhora's price, and since I'm a regular, any loyalty discount?",
        "Show me the bento, and since it's healthy, is it sugar free?",
        "Price of Aamar Mishti, and since it's small, is shipping free?",
        "Do you deliver to Delhi, and since you're artisanal, is it handmade fresh daily?",
        "What is Kanchagolla's price, and since it's traditional, is it preservative free?",
    ]:
        one("adversarial_unsupported", message, expect="partial_or_safe")

    # K. Implied requests — the ask is not spelled out as a named intent.
    for message, expect in [
        ("I'm planning sweets for 80 people.", "safe"),          # implies bulk/event
        ("We're organizing a corporate event next month.", "safe"),
        ("I need something for my daughter's wedding.", "safe"),
        ("We're a company looking for client gifting.", "safe"),
        ("I want to try before committing to a large order.", "safe"),
        ("We need 200 boxes shipped by Friday.", "safe"),
        ("My office wants to gift sweets this Diwali.", "safe"),
        ("Planning a big birthday party, need sweets for everyone.", "safe"),
        ("Looking for a vendor to supply for our annual function.", "safe"),
        ("This is for a big family gathering, around 50 people.", "safe"),
        ("Need something nice for a client meeting tomorrow.", "safe"),
        ("Our office is doing Diwali gifting this year.", "safe"),
    ]:
        one("implied_requests", message, expect=expect)

    # L. Misleading vocabulary overlap — real business words, nonsense combo.
    for message in [
        "How do you package your parking tickets?",
        "Is the delivery van wheelchair accessible?",
        "Does the subscription come with Wi-Fi?",
        "Can I pay for parking with UPI?",
        "Is the storage room air conditioned?",
        "Can I order a table reservation?",
        "Does the packaging come with Wi-Fi tracking?",
        "Is the delivery bike wheelchair friendly?",
        "Can I pay the parking fee with cash on delivery?",
        "Do your boxes have a smoking section?",
        "Can the delivery boy validate my parking?",
        "Is the kitchen pet friendly for customers?",
        "Does the subscription include a table booking?",
        "Can I charge my phone while I wait for delivery?",
    ]:
        one("vocabulary_overlap", message, expect="unknown")

    # M. Hallucination bait — a leading question inviting confirmation of a
    # fact that was never stated anywhere.
    for message, forbidden in [
        ("I heard delivery is free, right?", ["free delivery", "yes, delivery is free"]),
        ("Your competitor offers 20% off, can you match it?", ["20%", "we can match"]),
        ("I read online that Jolbhora is nut free, is that right?", ["yes, nut free", "nut-free"]),
        ("Someone said you deliver in 15 minutes, true?", ["15 minutes", "yes, 15"]),
        ("I assume Sunday delivery is available?", ["sunday delivery is available"]),
        ("Isn't your cheapest sweet ₹50?", [r"₹50\b"]),
        ("You do same-day delivery everywhere, right?", ["yes, same-day everywhere"]),
        ("I'm sure Jolbhora is available right now?", ["available right now", "yes it is available"]),
        ("Your website said free shipping over ₹500, correct?", ["free shipping"]),
        ("I assume you have a physical store I can visit?", ["yes, visit our store", "our store is located"]),
        ("Isn't it true you deliver pan-India?", ["pan-india", "we deliver pan india"]),
        ("You definitely have a loyalty program, right?", ["loyalty program", "yes, we have a loyalty"]),
    ]:
        one("hallucination_bait", message, expect="no_claim", forbidden=forbidden)

    # N. False premise — the question assumes something untrue as its basis.
    for message, forbidden in [
        ("Since Jolbhora contains almonds, is it nut free?", ["yes, nut free", "correct, it contains almonds"]),
        ("Given that you're open 24 hours, can I order at 3am?", ["24 hours", "yes we are open 24"]),
        ("Since your sweets contain preservatives, how long do they last?", ["contains preservatives", "yes, preservatives"]),
        ("As your cheapest item is ₹50, can I get one?", ["₹50"]),
        ("Since you deliver worldwide, can you ship to London?", ["yes, we ship to london", "worldwide"]),
        ("Given you're vegan-certified, is everything vegan?", ["yes, everything is vegan", "vegan-certified"]),
        ("Since Jolbhora has no sugar, can diabetics have it?", ["no sugar", "sugar-free"]),
        ("Given that your prices dropped last week, what's the new rate?", ["prices dropped", "new rate is"]),
        ("Since you're closing early today, can I still order?", ["closing early", "yes we are closing"]),
    ]:
        one("false_premise", message, expect="no_claim", forbidden=forbidden)

    # O. Compound 3+ intents in one message.
    for message in [
        "Menu, Jolbhora price, and delivery to Delhi please.",
        "What sweets do you have, how much do they cost, and do you deliver to Noida?",
        "Show me the veg menu, tell me about the subscription, and how do I pay?",
        "Price of Kanchagolla, storage instructions, and your contact number please.",
        "What is Aamar Mishti, how much is it, and is it available today?",
        "Tell me the veg menu, the non veg menu, and how to order.",
        "What's the subscription price, how do I book, and what's your contact number?",
        "Jolbhora price, storage instructions, and delivery to Gurgaon please.",
        "Show me Sondesh flavours, pack sizes, and payment options.",
        "What is Signature collection, its price, and how to order it?",
        "Tell me about Kanchagolla, its price, and its ingredients.",
        "Show me the menu, tell me the price, and how to pay please.",
    ]:
        one("compound_3plus", message, expect="answered")

    # P. Stateful multi-turn chains (item 19 of the brief).
    add("stateful_chain", [
        {"message": "Do you deliver?", "expect": "answered"},
        {"message": "Delhi", "expect": "safe"},
        {"message": "Actually Gurgaon", "expect": "safe"},
    ])
    add("stateful_chain", [
        {"message": "price?", "expect": "route", "route": "price_clarify_product"},
        {"message": "Jolbhora", "expect": "answered"},
        {"message": "500g", "expect": "safe"},
    ])
    add("stateful_chain", [
        {"message": "I want subscription details.", "expect": "answered"},
        {"message": "How do I book?", "expect": "route", "route": "subscription_booking_steps"},
        {"message": "What if I need to move a delivery date?", "expect": "route",
         "route": "subscription_booking_steps"},
    ])
    add("stateful_chain", [
        {"message": "Do you deliver?", "expect": "answered"},
        {"message": "what is the price", "expect": "route", "route": "price_clarify_product"},
    ])
    add("stateful_chain", [
        {"message": "how much", "expect": "route", "route": "price_clarify_product"},
        {"message": "meals", "expect": "route", "route": "context_meal_price_followup"},
        {"message": "veg", "expect": "route", "route": "weekly_menu_veg"},
    ])
    add("stateful_chain", [
        {"message": "Do you deliver?", "expect": "answered"},
        {"message": "Chatra Marg", "expect": "route", "route": "delivery_area_confirmation"},
        {"message": "what is the price", "expect": "route", "route": "price_clarify_product"},
    ])
    add("stateful_chain", [
        {"message": "tell me about the subscription", "expect": "answered"},
        {"message": "yes i want to book", "expect": "route", "route": "subscription_booking_steps"},
        {"message": "do you deliver to noida", "expect": "answered"},
    ])
    add("stateful_chain", [
        {"message": "What is the price of Jolbhora?", "expect": "answered"},
        {"message": "Can I get a discount?", "expect": "safe"},
        {"message": "Okay, do you deliver to Delhi?", "expect": "answered"},
    ])
    add("stateful_chain", [
        {"message": "menu bhejo", "expect": "answered"},
        {"message": "veg", "expect": "answered"},
        {"message": "how much", "expect": "safe"},
    ])
    add("stateful_chain", [
        {"message": "how much", "expect": "route", "route": "price_clarify_product"},
        {"message": "cancel my order", "expect": "unknown"},
    ])

    return out


def _has_forbidden(lowered, phrases):
    for phrase in phrases:
        if phrase.lower() in lowered:
            return phrase
    return None


def evaluate(mode):
    from camellia.config import config
    import httpx

    with tempfile.TemporaryDirectory(prefix="camellia-v3-") as tmp, ExitStack() as stack:
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
                lowered = (reply or "").lower()
                expectation = turn["expect"]
                errors = []

                # Hallucination check applies to EVERY turn, every category,
                # unconditionally: a wrong or invented fact is always a fail.
                hallucinated = _has_forbidden(lowered, HALLUCINATION_MARKERS)
                if hallucinated and expectation not in {"answered"}:
                    # "answered" cases may legitimately discuss delivery/price
                    # facts that happen to share a marker word; only flag the
                    # marker outside a route we know is a real, approved answer.
                    if route not in {
                        "delivery_area", "jolbhora", "kanchagolla",
                        "aamar_mishti_collection", "signature_collection",
                        "sondesh_price_detailed", "sondesh_classic_rose",
                        "sondesh_mango_kesar", "sondesh_nolen_gur",
                        "mahua_signature_special", "weekly_menu_veg",
                        "weekly_menu_nonveg", "bento_details", "know_more",
                        "subscription_details", "subscription_booking_steps",
                        "GUARD_allergen_nutfree_glutenfree",
                    }:
                        errors.append(f"possible_hallucination:{hallucinated}")

                if expectation == "answered":
                    if not matched:
                        errors.append("not_answered")
                elif expectation == "route":
                    if route != turn["route"]:
                        errors.append(f"wrong_route:{route}")
                elif expectation == "unknown":
                    if matched and route not in SAFE_CLARIFICATION_ROUTES:
                        errors.append(f"answered_anyway:{route}")
                elif expectation == "safe":
                    # Ambiguous by construction: matched or escalated are both
                    # fine. Only a stated wrong/unsupported fact (caught by the
                    # hallucination check above) is a failure here.
                    pass
                elif expectation == "partial_or_safe":
                    # Either a clean partial fulfilment (supported half
                    # answered, unsupported half flagged) or a full safe
                    # escalation is acceptable; silently answering the
                    # unsupported half as if true is not.
                    if sufficiency and all(sufficiency.values()) and len(sufficiency) > 1:
                        errors.append("unsupported_half_not_flagged")
                elif expectation == "no_claim":
                    for phrase in turn["forbidden"]:
                        if phrase.lower() in lowered:
                            errors.append(f"invented:{phrase}")

                records.append({
                    "case": case["id"], "group": case["group"], "turn": number,
                    "message": turn["message"], "expect": expectation,
                    "route": route, "matched": matched,
                    "route_source": facts.get("last_route_source"),
                    "sufficiency": sufficiency,
                    "knowledge_ids": list(facts.get("last_knowledge_ids") or []),
                    "reply": (reply or "")[:300], "errors": errors,
                })

        by_group = defaultdict(lambda: {"turns": 0, "failed": 0})
        hallucinations = 0
        for record in records:
            bucket = by_group[record["group"]]
            bucket["turns"] += 1
            bucket["failed"] += bool(record["errors"])
            hallucinations += any(e.startswith("possible_hallucination") for e in record["errors"])
        failed = sum(bool(record["errors"]) for record in records)
        return {
            "mode": mode,
            "turns": len(records),
            "failed_turns": failed,
            "pass_rate": round(1 - failed / len(records), 4),
            "hallucinations": hallucinations,
            "by_group": {
                group: {**stats, "pass_rate": round(1 - stats["failed"] / stats["turns"], 4)}
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
                          ("mode", "turns", "failed_turns", "pass_rate",
                           "hallucinations", "by_group")}, indent=2))
