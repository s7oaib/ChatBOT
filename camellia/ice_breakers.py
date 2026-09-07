"""
The prompts Instagram shows before a customer has typed anything.

Defined here rather than in app.py because two very different places need
them: the admin endpoint that writes them to Meta, and the webhook path that
has to understand a tap on one when it comes back. app.py cannot be imported
from ad_gate without a cycle.

Each question was run through the matcher before being written down — an
opener the bot fumbles is worse than no opener, because every customer sees
it. The payloads carry ICE_BREAKER so a tap is recorded as a tap rather than
as someone typing the same words.
"""

from typing import Dict, List

DEFAULT_ICE_BREAKERS: List[Dict[str, str]] = [
    {"question": "Show me the menu", "payload": "ICE_BREAKER_MENU"},
    {"question": "Sondesh flavours & prices", "payload": "ICE_BREAKER_PRICES"},
    {"question": "Do you deliver to my area?", "payload": "ICE_BREAKER_DELIVERY"},
    {"question": "I would like to place an order", "payload": "ICE_BREAKER_ORDER"},
]

# Meta's limits: at most four, and a question capped at 80 characters.
MAX_ICE_BREAKERS = 4
MAX_ICE_BREAKER_CHARS = 80

# What a tapped payload means, so the bot can answer a tap even when the
# webhook carries no title text with it. Instagram is not guaranteed to send
# `title` on a postback the way Messenger does, and without this a tap arrives
# as an event with no text at all and is discarded before it reaches a reply.
PAYLOAD_QUESTIONS: Dict[str, str] = {
    item["payload"]: item["question"] for item in DEFAULT_ICE_BREAKERS
}


def question_for_payload(payload: str) -> str:
    """
    The customer-visible question behind a tapped payload, if we set it.

    Falls back to turning an unknown ICE_BREAKER_STYLE payload into readable
    words, so a prompt written by hand later still produces something the
    matcher can work with rather than an empty message.
    """
    if not payload:
        return ""
    known = PAYLOAD_QUESTIONS.get(payload)
    if known:
        return known
    cleaned = payload.replace("ICE_BREAKER_", "").replace("_", " ").strip()
    return cleaned.lower() if cleaned else ""
