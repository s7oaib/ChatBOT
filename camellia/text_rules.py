"""
Text rules that apply everywhere, not just on the way out to a customer.

The flower strip used to live in responder.py, which meant it only ran on the
reply path. Anything written elsewhere kept its flowers: 28 of the 32 pending
candidate templates in production carried one, because the model that drafted
them was never told not to and nothing removed it before saving. Staff then saw
them in the approval queue, which is the client's own dashboard.

It lives here so database.py can use it too — database.py cannot import
responder.py without a cycle.
"""

import re

# Every flower the client does not want, not just the cherry blossom the first
# pass removed.
FLOWER_RE = re.compile(
    "[\U0001F338\U0001F33A\U0001F33B\U0001F33C\U0001F339\U0001F337"
    "\U0001F490\U0001F3F5]️?"
)


def strip_flower_emoji(value):
    """
    Remove flower emoji from text, card captions and anything nested.

    Takes the space with it. Removing the emoji alone left "Both!  Our weekly
    meals" with a double space in the middle of every sentence it touched.
    Only the space beside the emoji is touched: several replies carry ASCII
    price tables whose columns are built out of runs of spaces, and collapsing
    whitespace generally would tear them apart.
    """
    if isinstance(value, str):
        marked = FLOWER_RE.sub("\x00", value)
        return re.sub(
            " *\x00+ *",
            lambda m: " " if m.group(0).startswith(" ") and m.group(0).endswith(" ") else "",
            marked,
        )
    if isinstance(value, list):
        return [strip_flower_emoji(item) for item in value]
    if isinstance(value, dict):
        return {key: strip_flower_emoji(item) for key, item in value.items()}
    return value
