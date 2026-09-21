import uuid
from dataclasses import replace
from unittest.mock import MagicMock, patch

from camellia import app as app_module
from camellia import client_qa
from camellia.config import config
from camellia.responder import responder
from camellia.session_memory import session_memory


QUESTION = "What is the price of SUBSCRIPTION - HOME FOOD AWAY FROM HOME"
CHART = "/images/Everyday%20Meal%20Subscription%20Rate%20Chart.jpeg"


def _reply(message=QUESTION):
    user_id = f"subscription-sequence-{uuid.uuid4().hex}"
    session_memory.clear_session(user_id, "instagram")
    with patch.object(config, "BYPASS_AD_GATE", True):
        return user_id, responder.match_and_respond(
            message, user_id=user_id, channel="instagram", referral_source="ADS",
        )


def _send_sequence(text, images):
    sent = []

    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    def post(_url, headers=None, json=None):
        message = json["message"]
        if "text" in message:
            sent.append(("text", message["text"]))
        else:
            sent.append(("asset", message["attachment"]["payload"]["url"]))
        return Response()

    client = MagicMock()
    client.__enter__.return_value.post = post
    with patch.object(config, "INSTAGRAM_ACCESS_TOKEN", "IGAAtest-token"), \
         patch.object(config, "PUBLIC_BASE_URL", "https://example.test"), \
         patch.object(app_module.httpx, "Client", return_value=client):
        app_module.send_instagram_reply("controlled-test-user", text, image_url=images)
    return sent


def test_exact_question_routes_to_subscription_price_canonical_id():
    user_id, (text, matched, template_id, _images) = _reply()
    facts = session_memory.get_facts(user_id, "instagram")

    assert matched is True
    assert template_id == "subscription_details"  # stable public route ID
    assert facts["client_qa_canonical_id"] == "SUBSCRIPTION_PRICE"
    assert facts["client_qa_id"] == "subscription_price"
    assert facts["last_route_source"] == "client_approved_qa"
    assert text.startswith("Hi! Thank you for asking about our Everyday Meal Subscription.")


def test_subscription_price_paraphrases_keep_the_same_canonical_route():
    for index, message in enumerate((
        "How much is the meal subscription?",
        "What are the everyday meal plan rates?",
        "Subscription cost please",
    )):
        user_id, (_text, matched, _template_id, _images) = _reply(message)
        facts = session_memory.get_facts(user_id, "instagram")
        assert matched is True, message
        assert facts["client_qa_canonical_id"] == "SUBSCRIPTION_PRICE", (index, message)


def test_subscription_price_sends_text_chart_manager_in_order():
    _user_id, (text, _matched, _template_id, images) = _reply()

    sent = _send_sequence(text, images)
    assert [kind for kind, _value in sent] == ["text", "asset", "text"]
    assert sent[0][1].startswith("Hi! Thank you for asking about our Everyday Meal Subscription.")
    assert sent[1][1].endswith(CHART)
    assert sent[2][1].startswith("You may also connect directly with Our Manager of Kitchens")


def test_rate_chart_is_not_last_when_manager_paragraph_follows():
    _user_id, (text, _matched, _template_id, images) = _reply()
    sent = _send_sequence(text, images)

    chart_index = next(index for index, item in enumerate(sent) if item == ("asset", "https://example.test" + CHART))
    assert chart_index < len(sent) - 1
    assert sent[chart_index + 1][0] == "text"


def test_subscription_payload_has_no_literal_placeholder_or_duplicate_chart():
    _user_id, (text, _matched, _template_id, images) = _reply()
    sent = _send_sequence(text, images)
    all_text = "\n".join(value for kind, value in sent if kind == "text")
    assets = [value for kind, value in sent if kind == "asset"]

    assert "ATTACH RATE CHART" not in all_text
    assert assets.count("https://example.test" + CHART) == 1


def test_subscription_payload_contains_no_unrelated_follow_up_content():
    _user_id, (text, _matched, _template_id, images) = _reply()
    sent = _send_sequence(text, images)
    all_text = "\n".join(value for kind, value in sent if kind == "text")

    assert "Would be good to hear" not in all_text
    assert "à la carte menu" not in all_text
    assert "Sondesh" not in all_text


def test_asset_missing_uses_safe_existing_handoff_without_fabricated_rates(monkeypatch):
    entry = client_qa.REGISTRY_BY_ID["subscription_price"]
    missing = replace(entry, asset_keys=("missing_rate_chart",))
    monkeypatch.setattr(
        client_qa,
        "match",
        lambda _message, _facts: client_qa.ApprovedMatch(missing, "exact", missing.asset_keys),
    )

    _user_id, (text, matched, template_id, images) = _reply()

    assert matched is False
    assert template_id == "client_approved_asset_missing"
    assert images is None
    assert "ATTACH RATE CHART" not in text
    assert "₹" not in text


def test_existing_text_asset_text_transport_contract_remains_ordered():
    sent = _send_sequence(
        "lead",
        [
            {"url": "/images/one.jpeg", "caption": "middle", "text_first": True},
            {"url": "/images/two.jpeg", "caption": "tail"},
        ],
    )

    assert sent == [
        ("text", "lead"),
        ("asset", "https://example.test/images/one.jpeg"),
        ("text", "middle"),
        ("asset", "https://example.test/images/two.jpeg"),
        ("text", "tail"),
    ]


def test_manager_whatsapp_mention_is_customer_copy_only_not_a_server_side_send():
    _user_id, (text, _matched, _template_id, images) = _reply()
    sent = _send_sequence(text, images)

    assert any("WhatsApp" in value for kind, value in sent if kind == "text")
    assert all("whatsapp" not in value.lower() for kind, value in sent if kind == "asset")
