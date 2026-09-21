"""Production regressions reported for the Instagram routing boundary."""

from camellia import client_qa
from camellia import app as app_module
from camellia.config import config
from camellia.responder import responder
from camellia.session_memory import session_memory


BEST_TIME_REPLY = (
    "We always recommend pre-ordering. It helps us ensure that the dishes you want are available, exactly as you’d like them.\n\n"
    "Since we cook everything in small batches, some dishes may sell out if you order at the last minute. Pre-ordering is the best way to avoid disappointment."
)


def _respond(message, user_id, seed=None):
    config.BYPASS_AD_GATE = True
    session_memory.clear_session(user_id, "instagram")
    if seed:
        session_memory.get_facts(user_id, "instagram").update(seed)
    return responder.match_and_respond(message, user_id=user_id, channel="instagram")


def test_best_time_to_book_exact_phrase_beats_stale_finalized_order_state():
    text, matched, template_id, images = _respond(
        "whats the BEST TIME TO BOOK",
        "prod-regression-best-time-stale",
        {"order_finalized": True, "order_status": "finalized", "active_transaction": True},
    )

    assert matched is True
    assert template_id == "savoury_best_time_to_book"
    assert text == BEST_TIME_REPLY
    assert images is None
    assert "Manager of Kitchens" not in text
    assert "invoice" not in text.lower()


def test_best_time_to_book_case_and_punctuation_variants_are_deterministic():
    for index, message in enumerate((
        "What's the best time to book?",
        "WHATS THE BEST TIME TO BOOK!!!",
        "what’s the best time to book",
    )):
        text, matched, template_id, _ = _respond(message, f"prod-regression-best-time-{index}")
        assert matched is True
        assert template_id == "savoury_best_time_to_book"
        assert text == BEST_TIME_REPLY


def test_exact_sondesh_price_phrase_uses_one_approved_long_answer():
    text, matched, template_id, images = _respond(
        "Whats the price of sondesh?",
        "prod-regression-sondesh-price",
    )

    assert matched is True
    assert template_id == "sondesh_price_detailed"
    assert text == client_qa.REGISTRY_BY_CANONICAL_ID["SONDESH_DETAILS"].answer
    assert text.startswith("Hi thanks for getting in touch.")
    assert "INSERT " not in text
    assert "Matched:" not in text
    assert "Sondesh unfamiliar" not in text
    assert images


def test_exact_sondesh_price_phrase_does_not_emit_debug_or_second_route():
    text, matched, template_id, _ = _respond(
        "Whats the price of sondesh?",
        "prod-regression-sondesh-single",
    )

    assert matched is True
    assert template_id == "sondesh_price_detailed"
    assert text.count("Hi thanks for getting in touch.") == 1
    assert "Matched:" not in text
    assert "Sondesh unfamiliar" not in text


def test_webhook_dispatch_sends_one_customer_response_without_placeholders(monkeypatch):
    calls = []

    monkeypatch.setattr(app_module.db, "get_active_handoff", lambda *_args: None)
    monkeypatch.setattr(app_module.db, "log_conversation", lambda *_args, **_kwargs: 12345)
    monkeypatch.setattr(
        app_module,
        "send_instagram_reply",
        lambda recipient, text, **kwargs: calls.append((recipient, text, kwargs)),
    )

    app_module._process_instagram_event(
        "prod-regression-webhook-sondesh",
        "Whats the price of sondesh?",
        None,
    )

    assert len(calls) == 1
    sent_text = calls[0][1]
    assert sent_text.startswith("Hi thanks for getting in touch.")
    assert "INSERT " not in sent_text
    assert "Matched:" not in sent_text
    assert "Sondesh unfamiliar" not in sent_text


def test_configured_sondesh_assets_are_returned_as_actions_not_literal_markers():
    match = client_qa.match("Tell me more about sondesh", {})
    assert match and match.entry
    assert match.entry.canonical_id == "SONDESH_DETAILS"
    assert match.asset_keys == ("aamar_menu", "signature_menu", "weekly_menu")
    assert all(marker not in match.entry.answer for marker in (
        "INSERT AAMAR MENU HERE",
        "INSERT SIGNATURE MENU HERE",
        "INSERT LATEST WEEKLY LUNCH/ DINNER MENU HERE",
    ))


def test_true_finalized_order_message_keeps_manager_invoice_route():
    text, matched, template_id, _ = _respond(
        "I have placed the order",
        "prod-regression-finalized-order",
    )
    assert matched is False
    assert template_id == "order_next_steps"
    assert "Manager of Kitchens" in text
    assert "invoice" in text.lower()


def test_true_unfamiliar_sondesh_message_keeps_unfamiliar_route():
    text, matched, template_id, _ = _respond(
        "I am new to Sondesh",
        "prod-regression-sondesh-unfamiliar",
    )
    assert matched is True
    assert template_id == "sondesh_unfamiliar"
    assert "first time" in text.lower()


def test_sondesh_price_and_details_variants_remain_single_deterministic_routes():
    cases = (
        ("what does Sondesh cost", "sondesh_price_detailed"),
        ("Tell me more about Sondesh", "sondesh_details"),
    )
    for index, (message, expected_template) in enumerate(cases):
        text, matched, template_id, _ = _respond(message, f"prod-regression-sondesh-variant-{index}")
        assert matched is True
        assert template_id == expected_template
        assert text
        assert "Matched:" not in text
        assert "Sondesh unfamiliar" not in text
