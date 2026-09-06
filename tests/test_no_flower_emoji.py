"""
The client does not want a flower anywhere a person can see one.

An earlier pass stripped one emoji, U+1F338, from outbound replies at send
time. It left the source text full of them, ignored every other flower, and
put a double space wherever it cut one out of the middle of a sentence. These
tests hold the whole rule: none in the shipped text, none in the reply, and no
gap left behind.
"""
import json
import re
from pathlib import Path

import pytest

from camellia.responder import _strip_flower_emoji, responder

# Cherry blossom, hibiscus, sunflower, blossom, rose, tulip, bouquet, rosette.
FLOWERS = re.compile(
    "[\U0001F338\U0001F33A\U0001F33B\U0001F33C\U0001F339\U0001F337"
    "\U0001F490\U0001F3F5]"
)

ROOT = Path(__file__).resolve().parent.parent

# Source the client or a customer can end up reading. Tests are excluded on
# purpose: two of them feed a flower in deliberately to prove it gets removed.
SHIPPED = [
    "camellia/responder.py",
    "camellia/app.py",
    "camellia/whatsapp_adapter.py",
    "camellia/weekly_flyer.py",
    "camellia/order_calculator.py",
    "data/qa_templates.json",
    "data/menu.json",
    "README.md",
    "scripts/simulate_bot.py",
]


@pytest.mark.parametrize("rel", SHIPPED)
def test_no_flower_emoji_in_shipped_source(rel):
    """A literal flower in the source is one the strip has to keep catching."""
    path = ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")
    found = FLOWERS.findall(path.read_text(encoding="utf-8"))
    assert not found, f"{rel} still contains {len(found)} flower emoji: {set(found)}"


def test_no_template_response_carries_a_flower():
    """The answers customers actually receive, read straight off the seed."""
    data = json.loads((ROOT / "data/qa_templates.json").read_text(encoding="utf-8"))
    offenders = []

    def walk(node, trail):
        if isinstance(node, str):
            if FLOWERS.search(node):
                offenders.append(trail)
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{trail}[{index}]")

    walk(data, "qa_templates")
    assert not offenders, f"flower emoji still in templates: {offenders}"


@pytest.mark.parametrize(
    "given,expected",
    [
        ("\U0001F338 See Menu", "See Menu"),
        ("Both! \U0001F338 Our weekly meals", "Both! Our weekly meals"),
        ("updated just for you! \U0001F338", "updated just for you!"),
        ("a \U0001F490 b", "a b"),
        ("\U0001F3F5️ Rosette", "Rosette"),
        ("nothing to remove", "nothing to remove"),
    ],
)
def test_the_strip_takes_the_space_with_it(given, expected):
    """
    Removing the emoji alone left "Both!  Our weekly meals" with a double
    space in every sentence it touched.
    """
    assert _strip_flower_emoji(given) == expected


def test_the_strip_leaves_price_tables_alone():
    """
    Several replies carry ASCII tables whose columns are runs of spaces. A
    blanket whitespace collapse would tear them apart, so only the space
    beside a flower may be touched.
    """
    table = (
        "Our Rose Sondesh \U0001F338 is made fresh:\n```\n"
        "+-----------+-------+\n"
        "| Pack Size | 250g  |\n"
        "| Price     | 450   |\n"
        "+-----------+-------+\n```"
    )
    out = _strip_flower_emoji(table)
    assert "| Pack Size | 250g  |" in out
    assert "| Price     | 450   |" in out
    assert "Our Rose Sondesh is made fresh:" in out


def test_the_strip_reaches_into_captions_and_button_lists():
    """Cards and quick replies are the places a flower survived longest."""
    payload = {
        "text": "Menu \U0001F338",
        "buttons": ["\U0001F338 See Menu", "Order"],
        "cards": [{"url": "/images/a.jpeg", "caption": "Fresh \U0001F33A today"}],
    }
    cleaned = _strip_flower_emoji(payload)
    assert cleaned["text"] == "Menu"
    assert cleaned["buttons"] == ["See Menu", "Order"]
    assert cleaned["cards"][0]["caption"] == "Fresh today"
    assert cleaned["cards"][0]["url"] == "/images/a.jpeg"


@pytest.mark.parametrize(
    "message",
    [
        "hi",
        "show me the menu",
        "sondesh price",
        "do you deliver to gurgaon",
        "what is camellia",
    ],
)
def test_a_real_reply_carries_no_flower(message):
    """End to end, through the matcher a customer actually hits."""
    text, _, _, images = responder.match_and_respond(
        message, user_id=f"flower_{abs(hash(message))}", channel="whatsapp"
    )
    assert not FLOWERS.search(text or ""), f"{message!r} replied with a flower"
    for image in images or []:
        caption = image.get("caption", "") if isinstance(image, dict) else ""
        assert not FLOWERS.search(caption), f"{message!r} sent a flower caption"


def test_the_duplicate_guard_works_now_that_both_sides_match():
    """
    A side effect worth pinning. The guard compares a candidate reply against
    the last one sent, but the stored copy had been flower-stripped and the
    candidate had not, so for any template whose text carried a flower the two
    never compared equal and the guard silently never fired. With the flowers
    gone from the source both sides match, and asking the same thing twice
    gets the "shared just above" note instead of the identical answer again.
    """
    uid = "flower_dupe_guard"
    first_text, _, first_tmpl, first_images = responder.match_and_respond(
        "can i get a menu for today", user_id=uid, channel="whatsapp"
    )
    second_text, _, second_tmpl, second_images = responder.match_and_respond(
        "menu for today", user_id=uid, channel="whatsapp"
    )

    assert first_tmpl == second_tmpl == "know_more"
    assert first_images, "the first ask should still send the menu cards"
    assert second_text != first_text, "the identical reply was sent twice"
    assert "just above" in second_text
    # Documented intent: re-sending the same cards under a "shared above" note
    # would contradict the note.
    assert not second_images


def test_removing_the_flowers_left_no_sentence_running_into_the_next():
    """
    Some flowers were doing punctuation's job. "Hello from Camellia [flower]
    Are you in the mood..." became one run-on sentence once the emoji went,
    because the strip only knows about spaces and cannot tell a decorative
    flower from a separating one. Any future strip has the same blind spot.
    """
    data = json.loads((ROOT / "data/qa_templates.json").read_text(encoding="utf-8"))
    offenders = []

    def walk(node, trail):
        if isinstance(node, str):
            # A lowercase word, a single space, then a capitalised word, with
            # no punctuation between them and no line break to separate them.
            for match in re.finditer(r"[a-z] [A-Z][a-z]+ [a-z]", node):
                fragment = node[max(0, match.start() - 24): match.end() + 12]
                if any(word in fragment for word in ("Camellia Are", "Artisanal What")):
                    offenders.append(f"{trail}: ...{fragment}...")
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{trail}[{index}]")

    walk(data, "qa_templates")
    assert not offenders, f"a sentence runs into the next: {offenders}"


def test_every_greeting_names_the_brand_in_full():
    """
    Nine of the ten said "Camellia Artisanal" and one said "Camellia". Because
    the bank is picked at random, that one made the greeting test fail about
    one run in ten — a flake that looked like an intermittent bug for weeks.
    """
    data = json.loads((ROOT / "data/qa_templates.json").read_text(encoding="utf-8"))
    greeting = next(t for t in data["templates"] if t.get("id") == "greeting")
    responses = greeting.get("responses") or []
    assert len(responses) >= 5, "the greeting bank shrank unexpectedly"

    missing = [i for i, r in enumerate(responses) if "Camellia Artisanal" not in r]
    assert not missing, (
        f"greeting responses {missing} do not name the brand in full; whichever "
        "customer draws one gets a different introduction from everyone else"
    )
