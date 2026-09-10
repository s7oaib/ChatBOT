"""
The one bento substitution the client confirmed must be answered, not escalated.

"instead of" is an explicit escalation keyword, and that check runs before any
template matching — so it outranks every answer the bot has. That is right for
substitutions nobody has ruled on, but the confirmed bento copy answers one
outright: "Also available with Chholar Dal (Pooja style) instead of Aloo Dom."
It was being handed to a human purely because of the words it contained.
"""
import itertools

from camellia.responder import responder
from camellia.session_memory import session_memory

_ids = itertools.count()


def _ask(message):
    uid = f"subst_{next(_ids)}"
    session_memory.clear_session(uid, "whatsapp")
    text, matched, tmpl, _ = responder.match_and_respond(
        message, user_id=uid, channel="whatsapp"
    )
    return text, matched, tmpl


def _escalated(text):
    return "assist you personally on whatsapp" in (text or "").lower()


def test_the_confirmed_substitution_is_answered():
    for phrasing in (
        "can I get chholar dal instead of aloo dom in the veg bento",
        "chholar dal instead of aloo dom",
        "chholar daal instead of aloo dom",
        "can i have chholar daal in the bento",
    ):
        text, matched, tmpl = _ask(phrasing)
        assert matched, f"{phrasing!r} dead-ended into a handover"
        assert tmpl == "bento_details", f"{phrasing!r} routed to {tmpl}"
        assert not _escalated(text)
        assert "Chholar Dal" in text, "the reply does not mention the substitution"


def test_flavour_mixing_is_still_locked():
    """Never confirmed, and the exemption must not have widened to cover it."""
    text, matched, tmpl = _ask("can i mix flavours in the bento")

    assert tmpl == "GUARD_customization_flavour_mixing"
    assert not matched
    assert _escalated(text)


def test_unconfirmed_substitutions_still_go_to_a_human():
    """The exemption is one dish pair, not a licence to guess at swaps."""
    for phrasing in (
        "can I get paneer instead of aloo dom in the veg bento",
        "can i get chicken instead of mutton",
        "can i have fish instead of chicken in the bento",
    ):
        text, _, _ = _ask(phrasing)
        assert _escalated(text), f"{phrasing!r} was answered without confirmation"


def test_ordinary_escalation_keywords_are_untouched():
    text, _, tmpl = _ask("I want a refund")
    assert tmpl == "explicit_escalation"
    assert _escalated(text)
