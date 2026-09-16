from camellia.content_integrity import (
    CLIENT_CONFIRMED_EXACT_RESPONSES,
    canonical_override_responses,
    contains_unconfirmed_claim,
    quarantine_template_response,
    quarantine_unconfirmed_claims,
)
import json
from camellia.responder import DeterministicResponder
from camellia.session_memory import session_memory


def test_unconfirmed_marketing_and_sourcing_sentences_are_removed():
    text = (
        "Collection 2 uses desi A2 cow milk. "
        "It is made with small-batch artisanal farmers and trusted dairies. "
        "Gifting is offered."
    )
    result = quarantine_unconfirmed_claims(text)
    assert result == "Collection 2 uses desi A2 cow milk.\nGifting is offered."
    assert "farmers" not in result and "dairies" not in result


def test_operational_timing_is_quarantined_but_preorder_guidance_remains():
    text = (
        "We usually dispatch within 15 minutes. "
        "We recommend pre-ordering so the team can confirm your request."
    )
    result = quarantine_unconfirmed_claims(text)
    assert "15 minutes" not in result
    assert "recommend pre-ordering" in result


def test_wholly_unconfirmed_domains_use_safe_fallbacks():
    assert quarantine_template_response(
        "GUARD_dietary_suitability", "Sondesh is eggless and gluten-free."
    ).startswith("Please tell us the exact dietary requirement")
    assert quarantine_template_response(
        "delivery_out_of_area", "We use a cold chain outside Delhi NCR."
    ) == CLIENT_CONFIRMED_EXACT_RESPONSES["delivery_out_of_area"]


def test_confirmed_copy_is_not_quarantined():
    text = "Signature Collection uses desi A2 cow milk. Porter delivery charges are on actuals."
    assert not contains_unconfirmed_claim(text)
    assert quarantine_unconfirmed_claims(text) == text


def test_public_resolver_applies_quarantine_to_customer_copy(monkeypatch):
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    responder = DeterministicResponder()
    user_id = "quarantine-runtime"
    session_memory.clear_session(user_id, "whatsapp")
    text, _matched, template_id, _images = responder.match_and_respond(
        "different from other sweet shops", user_id=user_id, channel="whatsapp"
    )
    assert template_id == "artisanal_differentiation"
    assert "sourced directly" not in text.lower()
    assert "partially mechanised" not in text.lower()


def test_all_managed_quarantine_copies_are_free_of_unconfirmed_claims():
    with open("data/menu.json", encoding="utf-8") as handle:
        menu = json.load(handle)
    with open("data/qa_templates.json", encoding="utf-8") as handle:
        templates = json.load(handle)["templates"]
    managed = canonical_override_responses(menu, templates)
    assert managed
    for template_id, text in managed.items():
        if template_id in CLIENT_CONFIRMED_EXACT_RESPONSES:
            assert text == CLIENT_CONFIRMED_EXACT_RESPONSES[template_id]
        else:
            assert not contains_unconfirmed_claim(text)


def test_client_confirmed_dispatch_template_emits_exact_timing(monkeypatch):
    monkeypatch.setenv("CONTENT_INTEGRITY_REMEDIATION", "true")
    responder = DeterministicResponder()
    user_id = "quarantine-dispatch"
    session_memory.clear_session(user_id, "whatsapp")
    text, _matched, _template_id, _images = responder.match_and_respond(
        "how long to deliver sondesh", user_id=user_id, channel="whatsapp"
    )
    assert text == CLIENT_CONFIRMED_EXACT_RESPONSES["sondesh_dispatch_time"]
    modified = text + " Trusted dairies."
    safe = quarantine_template_response("sondesh_dispatch_time", modified)
    assert safe == text
    assert "Trusted dairies" not in safe
