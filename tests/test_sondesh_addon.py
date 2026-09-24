"""Client-requested à la carte -> Sondesh add-on acceptance tests."""

import re
import uuid
from unittest.mock import patch

from camellia import app as app_module
from camellia.config import config
from camellia.database import Database
from camellia.responder import responder
from camellia.session_memory import session_memory


ADDON_ASSETS = [
    "/images/sondesh_addon_01.jpeg",
    "/images/sondesh_addon_02.jpeg",
    "/images/sondesh_addon_03.jpeg",
]
ADDON_COPY = (
    "We also make handcrafted Norom Pak Sondesh if you’d like to add something "
    "sweet to your order. Sharing a few pictures below."
)


def _ask(message, user_id=None, channel="instagram"):
    user_id = user_id or f"addon-{uuid.uuid4().hex}"
    kwargs = {"user_id": user_id, "channel": channel}
    if channel == "instagram":
        # The production Instagram gate requires an approved ad/referral; this
        # test uses the controlled approved test path rather than an organic DM.
        kwargs["referral_source"] = "ADS"
    return responder.match_and_respond(message, **kwargs)


def _urls(images):
    return [item.get("url") if isinstance(item, dict) else item for item in (images or [])]


def test_generic_alacarte_question_does_not_trigger_unsolicited_sondesh_addon():
    user_id = f"addon-once-{uuid.uuid4().hex}"
    session_memory.clear_session(user_id, "instagram")

    text, matched, template_id, images = _ask(
        "Can you show me the à la carte menu?", user_id
    )

    assert text
    assert ADDON_COPY not in text
    assert not any(asset in _urls(images) for asset in ADDON_ASSETS)
    assert not session_memory.get_facts(user_id, "instagram")["alacarte_sondesh_addon_sent"]


def test_unapproved_alacarte_paraphrase_hands_off_without_addon():
    user_id = f"addon-client-qa-{uuid.uuid4().hex}"
    session_memory.clear_session(user_id, "instagram")

    text, matched, template_id, images = _ask(
        "What meals do you offer à la carte?", user_id
    )

    assert matched is False
    assert template_id is None
    assert "Manager of Kitchens" in text
    assert ADDON_COPY not in text
    assert not any(asset in _urls(images) for asset in ADDON_ASSETS)


def test_alacarte_question_interrupts_stale_story_without_addon():
    user_id = f"addon-stale-story-{uuid.uuid4().hex}"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["home_table_context"] = True
    facts["source_route"] = {
        "active": True,
        "routing_topic": "Bengali Home Table",
        "topic_id": "bengali_home_table",
        "source_type": "story",
    }

    text, matched, template_id, images = _ask(
        "Can you show me the à la carte menu?", user_id
    )

    assert "Bengali Home Table" not in text
    assert ADDON_COPY not in text
    assert not any(asset in _urls(images) for asset in ADDON_ASSETS)
    assert session_memory.get_facts(user_id, "instagram")["source_context_superseded"]


def test_repeat_alacarte_turn_still_has_no_unsolicited_pitch_or_bundle():
    user_id = f"addon-repeat-{uuid.uuid4().hex}"
    session_memory.clear_session(user_id, "instagram")
    _ask("Can you show me the à la carte menu?", user_id)

    text, _matched, _template_id, images = _ask(
        "what can I order à la carte?", user_id
    )

    assert ADDON_COPY not in text
    assert not any(asset in _urls(images) for asset in ADDON_ASSETS)


def test_historical_addon_state_does_not_authorize_a_new_addon(monkeypatch):
    user_id = f"addon-durable-repeat-{uuid.uuid4().hex}"
    session_memory.clear_session(user_id, "instagram")

    monkeypatch.setattr(
        "camellia.responder.db.get_conversations",
        lambda **kwargs: [{
            "id": 777,
            "user_id": user_id,
            "channel": "instagram",
            "user_message": "What meals do you offer à la carte?",
        }],
    )
    monkeypatch.setattr(
        "camellia.responder.db.get_instagram_outbound_audit",
        lambda **kwargs: [
            {"response_type": "image", "success": 1},
            {"response_type": "image", "success": 1},
            {"response_type": "image", "success": 1},
        ],
    )

    text, _matched, _template_id, images = _ask(
        "What meals do you offer à la carte?", user_id
    )

    assert ADDON_COPY not in text
    assert not any(asset in _urls(images) for asset in ADDON_ASSETS)


def test_non_alacarte_and_direct_sondesh_questions_do_not_trigger_addon():
    for message in (
        "How do I pay?",
        "I want to talk to a person.",
        "What is Sondesh?",
        "Tell me about the Bengali Home Table",
        "Tell me about my subscription",
        "How long is delivery?",
    ):
        text, _matched, _template_id, images = _ask(message)
        assert ADDON_COPY not in text, message
        assert not any(asset in _urls(images) for asset in ADDON_ASSETS), message


def test_decline_does_not_create_an_unsolicited_addon_state():
    user_id = f"addon-decline-{uuid.uuid4().hex}"
    session_memory.clear_session(user_id, "instagram")
    _ask("Can you show me the à la carte menu?", user_id)
    _ask("No thanks", user_id)

    text, _matched, _template_id, images = _ask(
        "Can I order individual dishes?", user_id
    )
    assert ADDON_COPY not in text
    assert not any(asset in _urls(images) for asset in ADDON_ASSETS)
    assert not session_memory.get_facts(user_id, "instagram")["alacarte_sondesh_addon_sent"]


def test_new_session_does_not_receive_unsolicited_addon_after_prior_decline():
    user_id = f"addon-new-flow-{uuid.uuid4().hex}"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["alacarte_sondesh_addon_declined"] = True
    session_memory.clear_session(user_id, "instagram")

    text, _matched, _template_id, images = _ask(
        "what can I order à la carte?", user_id
    )
    assert ADDON_COPY not in text
    assert not any(asset in _urls(images) for asset in ADDON_ASSETS)


def test_unapproved_individual_dish_order_hands_off_without_legacy_menu():
    text, matched, template_id, images = _ask("Can I order individual dishes?")
    assert matched is False
    assert template_id is None
    assert "Manager of Kitchens" in text
    assert not images


def test_no_placeholder_exposed_in_addon_response():
    text, _matched, _template_id, images = _ask("what food do you have?")
    assert "INSERT 3 PICTURES OF SONDESH" not in text
    assert not any("INSERT 3 PICTURES" in str(item) for item in (images or []))


def test_instagram_outbound_sends_text_then_three_ordered_addon_images(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "sondesh-addon.db"))
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(config, "INSTAGRAM_ACCESS_TOKEN", "")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://example.test")
    user_id = f"addon-webhook-{uuid.uuid4().hex}"
    session_memory.clear_session(user_id, "instagram")
    sent = []

    with patch.object(
        app_module,
        "send_instagram_reply",
        side_effect=lambda recipient, text, image_url=None, **kwargs: sent.append(
            {"recipient": recipient, "text": text, "image_url": image_url, **kwargs}
        ),
    ):
        app_module._process_instagram_event(
            user_id, "Can you show me the à la carte menu?", "addon-test-ad"
        )

    assert len(sent) == 1
    assert ADDON_COPY not in sent[0]["text"]
    assert not any(asset in _urls(sent[0]["image_url"]) for asset in ADDON_ASSETS)


def test_asset_filenames_are_stable_and_no_raw_local_paths_are_customer_visible():
    text, _matched, _template_id, images = _ask("what can I order à la carte?")
    assert "C:\\" not in text
    assert not any(re.search(r"(?:AppData|mnt/data|Temp|Users/)", str(item)) for item in (images or []))
