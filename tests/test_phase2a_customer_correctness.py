"""Phase 2A transport and current-week safety regressions."""

from dataclasses import replace
from datetime import date
from unittest.mock import patch

import pytest

from camellia import client_qa, menu_dates, menu_images, weekly_flyer
from camellia.config import config
from camellia.database import Database
from camellia.responder import _approved_continuation, responder
from camellia.session_memory import SessionMemoryManager, session_memory


@pytest.fixture
def customer(monkeypatch, eligible_weekly_asset):
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    user = "phase2a-customer"
    session_memory.clear_session(user, "instagram")

    def reply(message):
        result = responder.match_and_respond(message, user_id=user, channel="instagram")
        return result, session_memory.get_facts(user, "instagram")

    return reply


def test_home_table_continuation_and_fresh_price(customer):
    details = client_qa.REGISTRY_BY_ID["home_table_details"].answer
    price = client_qa.REGISTRY_BY_ID["home_table_price"].answer
    fresh, _ = customer("What is the Bengali Home Table price?")
    assert fresh[2] == "bengali_home_table_pricing"
    assert fresh[0] == price
    customer("Tell me about the Bengali Home Table")
    continued, facts = customer("What's the price?")
    assert continued[2] == "bengali_home_table_pricing"
    assert continued[0] == price[len(details):].lstrip()
    assert facts["ordered_response_blocks"] == [{"kind": "TEXT", "text": continued[0]}]
    assert client_qa.REGISTRY_BY_ID["home_table_payment"].answer not in continued[0]
    payment, _ = customer("How do I pay?")
    assert payment[2] == "bengali_home_table_payment"


@pytest.mark.parametrize("representation", ["crlf", "cr", "outer_whitespace", "repeated_blank_lines"])
def test_home_table_normalized_previous_assistant_turn_continues_price(customer, representation):
    details = client_qa.REGISTRY_BY_ID["home_table_details"].answer
    price = client_qa.REGISTRY_BY_ID["home_table_price"].answer
    customer("Tell me about the Bengali Home Table")
    session = session_memory.get_session("phase2a-customer", "instagram")
    assert session["facts"]["last_answer_id"] == "bengali_home_table_details"
    assert session["turns"][-1]["role"] == "assistant"
    assert "\n\n" in details
    representations = {
        "crlf": details.replace("\n", "\r\n"),
        "cr": details.replace("\n", "\r"),
        "outer_whitespace": f"  \n{details}\n  ",
        "repeated_blank_lines": details.replace("\n\n", "\n\n\n", 1),
    }
    previous_text = representations[representation]
    assert previous_text != details
    session["turns"][-1]["content"] = previous_text

    reply, facts = customer("What's the price?")

    expected = price[len(details):].lstrip()
    assert reply[2] == "bengali_home_table_pricing"
    assert reply[0] == expected
    assert facts["ordered_response_blocks"] == [{"kind": "TEXT", "text": expected}]


def test_prefix_mismatch_fails_closed():
    assert _approved_continuation("Different introduction\n\nPricing details", "Approved introduction") == (
        "Different introduction\n\nPricing details"
    )
    assert _approved_continuation("Approved introduction\r\n\r\nPricing details", "Approved introduction") == "Pricing details"
    assert _approved_continuation("Approved  introduction\n\nPricing details", "Approved introduction") == (
        "Approved  introduction\n\nPricing details"
    )


def test_home_table_prefix_mismatch_keeps_entire_selected_answer(customer, monkeypatch):
    customer("Tell me about the Bengali Home Table")
    original = client_qa.REGISTRY_BY_ID["home_table_price"]
    changed = "Different approved fixture opening\n\n" + original.answer
    monkeypatch.setitem(
        client_qa.REGISTRY_BY_ID, "home_table_price",
        replace(original, answer=changed, blocks=(client_qa.ResponseBlock("TEXT", changed),)),
    )
    reply, facts = customer("What's the price?")
    assert reply[0] == changed
    assert facts["ordered_response_blocks"] == [{"kind": "TEXT", "text": changed}]


def test_home_table_matching_text_with_wrong_previous_answer_id_fails_closed(customer):
    customer("Tell me about the Bengali Home Table")
    facts = session_memory.get_facts("phase2a-customer", "instagram")
    facts["last_answer_id"] = "some_other_approved_answer"
    canonical = client_qa.REGISTRY_BY_ID["home_table_price"].answer

    reply, _ = customer("What's the price?")

    assert reply[0] == canonical


def test_home_table_details_id_with_wrong_previous_assistant_text_fails_closed(customer):
    customer("Tell me about the Bengali Home Table")
    session = session_memory.get_session("phase2a-customer", "instagram")
    session["turns"][-1]["content"] = "Different assistant response"
    canonical = client_qa.REGISTRY_BY_ID["home_table_price"].answer

    reply, _ = customer("What's the price?")

    assert reply[0] == canonical


@pytest.mark.parametrize("week,eligible", [
    ("28 Aug - 3 Sep 2026", False),
    ("18 Sep - 24 Sep 2026", False),
    ("25 Sep - 30 Sep 2026", True),
    ("29 Sep - 5 Oct 2026", False),
])
def test_printed_week_must_include_today(monkeypatch, week, eligible):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    assert weekly_flyer.is_current({"week_of": week, "uploaded_at": 1}) is eligible


def test_static_weekly_fallback_is_not_current(monkeypatch):
    monkeypatch.setattr(menu_images, "current_weekly", lambda: None)
    monkeypatch.setattr(menu_images, "has_weekly_archive", lambda: False)
    monkeypatch.setattr(menu_images.weekly_flyer, "get", lambda: None)
    assert menu_images.current_weekly_url("/images/From%20Friday.jpeg") is None
    assert menu_images.current_weekly_url("/images/Till%20Thursday.jpeg") is None
    assert responder._current_weekly_image(4) == (None, None)


@pytest.mark.parametrize("week,expected", [
    ("28 Aug - 3 Sep 2026", None),
    ("18 Sep - 24 Sep 2026", None),
    ("25 Sep - 30 Sep 2026", "/uploads/test-dated-weekly.jpeg"),
])
def test_uploaded_dated_weekly_asset_uses_shared_eligibility(monkeypatch, week, expected):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    monkeypatch.setattr(menu_images, "current_weekly", lambda: None)
    monkeypatch.setattr(menu_images, "has_weekly_archive", lambda: False)
    monkeypatch.setattr(menu_images.weekly_flyer, "get", lambda: {
        "url": "/uploads/test-dated-weekly.jpeg", "week_of": week, "uploaded_at": 1,
    })
    assert menu_images.current_weekly_url("/images/From%20Friday.jpeg") == expected
    assert menu_images.current_url("weekly_vegetarian", "/images/Till%20Thursday.jpeg") == expected


def test_weekly_routes_have_no_stale_asset_or_attachment_claim(customer, monkeypatch):
    monkeypatch.setattr("camellia.responder.current_weekly_url", lambda: None)
    for message in ("What's the price?", "Tell me about Sondesh", "Show this week's Savoury menu"):
        result, facts = customer(message)
        text, _matched, _template, images = result
        assert images is None or not any("Friday" in str(image) or "Thursday" in str(image) for image in images)
        assert "attached this week’s menu" not in text
        assert "full menu card with price" not in text
        assert not any(
            block.get("asset_key") == "weekly_menu"
            for block in facts.get("ordered_response_blocks") or []
        )
        if "Sondesh" in message:
            assert [block["asset_key"] for block in facts["ordered_response_blocks"] if block["kind"] != "TEXT"] == [
                "aamar_menu", "signature_menu",
            ]
        else:
            assert "finalising this week's menu" in text


def test_sondesh_topic_owns_bare_price_after_home_table(customer, monkeypatch):
    monkeypatch.setattr("camellia.responder.current_weekly_url", lambda: None)
    customer("Tell me about the Bengali Home Table")
    details, facts = customer("Tell me about Sondesh")
    assert details[2] == "sondesh_tell_me_more"
    assert facts["active_topic"] == "sondesh_general"
    price, facts = customer("What's the price?")
    assert price[2] == "sondesh_price_detailed"
    assert facts["client_qa_id"] == "sondesh_price_client"
    subscription, _ = customer("What's the subscription price?")
    assert subscription[2] == "subscription_details"


@pytest.mark.parametrize("followup,eligible_url", [
    ("Show this week's Savoury menu", None),
    ("Show this week's Savoury menu", "/uploads/current-weekly.jpeg"),
])
def test_sondesh_topic_does_not_capture_explicit_weekly_menu(customer, monkeypatch, followup, eligible_url):
    monkeypatch.setattr("camellia.responder.current_weekly_url", lambda: eligible_url)
    customer("Tell me about Sondesh")

    reply, _facts = customer(followup)

    assert reply[2] == "asset_request_weekly_menu"
    if eligible_url:
        assert reply[3] == eligible_url
    else:
        assert "finalising this week's menu" in reply[0]
        assert reply[3] is None


def test_sondesh_topic_does_not_capture_explicit_kanchagolla_price(customer, monkeypatch):
    monkeypatch.setattr("camellia.responder.current_weekly_url", lambda: None)
    customer("Tell me about Sondesh")

    reply, facts = customer("What's the price of Kanchagolla 250g?")

    assert reply[2] not in {"sondesh_price_detailed", "bengali_home_table_pricing"}
    assert facts.get("client_qa_id") != "sondesh_price_client"
    assert facts.get("decision_lane") == "HUMAN_HANDOFF"
    assert reply[1] is False
    assert "₹" not in reply[0]


@pytest.mark.parametrize("opening,qa_id", [
    ("Tell me about Sondesh", "sondesh_details_client"),
    ("Tell me about Sondesh", "sondesh_price_client"),
])
def test_sondesh_without_weekly_menu_preserves_all_other_approved_text(
    customer, monkeypatch, opening, qa_id,
):
    monkeypatch.setattr("camellia.responder.current_weekly_url", lambda: None)
    customer(opening)
    if qa_id == "sondesh_price_client":
        result, facts = customer("What's the price?")
    else:
        result, facts = customer("Tell me about Sondesh")
    entry = client_qa.REGISTRY_BY_ID[qa_id]
    attachment_sentence = "I\u2019ve attached this week\u2019s menu in case you\u2019d like to have a look."
    expected_text_blocks = [
        block.value.replace(attachment_sentence + " ", "", 1)
        for block in entry.blocks
        if block.kind == "TEXT"
    ]
    actual_text_blocks = [
        block["text"] for block in facts["ordered_response_blocks"] if block["kind"] == "TEXT"
    ]

    assert facts["client_qa_id"] == qa_id
    assert actual_text_blocks == expected_text_blocks
    assert result[0] == "\n\n".join(expected_text_blocks)
    assert "I’ve attached this week’s menu" not in result[0]
    assert "Porter charges are on actuals." in result[0]
    assert "We also prepare and deliver freshly cooked Bengali meals across Delhi NCR." in result[0]
    assert "Everything is cooked fresh in small batches" in result[0]
    assert "recommendation based on whether you prefer vegetarian, fish or meat" in result[0]
    assert [
        block["asset_key"] for block in facts["ordered_response_blocks"] if block["kind"] != "TEXT"
    ] == ["aamar_menu", "signature_menu"]
    assert result[3] == ["/images/AAMAR%20MISHTI.jpeg", "/images/Signature%20Sondesh.jpeg"]


def test_sondesh_topic_survives_reload_but_not_global_ttl(tmp_path, monkeypatch):
    import time
    import camellia.responder as responder_module

    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(responder_module, "current_weekly_url", lambda: None)
    database = Database(str(tmp_path / "sondesh-session.db"))
    first = SessionMemoryManager(database=database, persistence_enabled=True)
    monkeypatch.setattr(responder_module, "session_memory", first)
    assert responder.match_and_respond("Tell me about Sondesh", user_id="sondesh-ttl", channel="instagram")[2] == "sondesh_tell_me_more"
    reloaded = SessionMemoryManager(database=database, persistence_enabled=True)
    monkeypatch.setattr(responder_module, "session_memory", reloaded)
    assert responder.match_and_respond("What's the price?", user_id="sondesh-ttl", channel="instagram")[2] == "sondesh_price_detailed"
    with patch("camellia.session_memory.time.time", return_value=time.time() + config.SESSION_TTL_SECONDS + 1):
        assert responder.match_and_respond("What's the price?", user_id="sondesh-ttl", channel="instagram")[2] != "sondesh_price_detailed"
