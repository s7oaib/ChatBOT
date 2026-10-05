"""Exact-ID Instagram Story routing and safe Story event handling."""

import uuid
from unittest.mock import patch

import pytest

import camellia.app as app_module
import camellia.responder as responder_module
from camellia import client_qa
from camellia.ad_gate import EntryPoint, describe_entry_point
from camellia.config import config
from camellia.database import Database
from camellia.instagram_routing import route_for_inbound, validate_route_payload
from camellia.responder import responder
from camellia.session_memory import session_memory


@pytest.fixture
def story_db(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "instagram-story.db"))
    monkeypatch.setattr("camellia.database.db", database)
    monkeypatch.setattr(app_module, "db", database)
    monkeypatch.setattr(responder_module, "db", database)
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    monkeypatch.setattr(config, "GROQ_ENABLED", False)
    monkeypatch.setattr(config, "SEMANTIC_ROUTER_ENABLED", False)
    monkeypatch.setattr(config, "STRUCTURED_NLU_ENABLED", False)
    return database


def _story_route(media_id="story-jolbhora"):
    return {
        "source_type": "story",
        "canonical_url": None,
        "shortcode": None,
        "media_id": media_id,
        "ad_id": None,
        "campaign_id": None,
        "routing_topic": "Jolbhora",
        "knowledge_scope": ["products", "prices"],
        "escalation_policy": "human_if_unsupported",
        "priority_products": ["jolbhora"],
        "active": True,
        "topic_id": None,
        "menu_id": None,
        "menu_mode": "INHERIT",
        "preview_url": None,
        "media_type": None,
    }


def _mapped_media_route(source_type, *, shortcode=None, media_id=None):
    """Build a deterministic Jolbhora route for each supported share type."""
    return {
        "source_type": source_type,
        "canonical_url": (
            f"https://www.instagram.com/reel/{shortcode}/"
            if shortcode else None
        ),
        "shortcode": shortcode,
        "media_id": media_id,
        "ad_id": None,
        "campaign_id": None,
        "routing_topic": "Jolbhora",
        "knowledge_scope": ["products", "prices"],
        "escalation_policy": "human_if_unsupported",
        "priority_products": ["jolbhora"],
        "active": True,
        "topic_id": None,
        "menu_id": None,
        "menu_mode": "INHERIT",
        "preview_url": None,
        "media_type": None,
    }


def _with_story_context(user_id, *, product="jolbhora"):
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {
        **_story_route(),
        "routing_id": 41,
        "priority_products": [product],
    }
    return facts


def _reply(user_id, text):
    return responder.match_and_respond(
        text, user_id=user_id, channel="instagram", referral_source="ADS",
    )


def test_story_reply_detection_captures_exact_meta_id():
    event = {
        "sender": {"id": "customer-1"},
        "message": {
            "mid": "m-1",
            "text": "price?",
            "reply_to": {"story": {"id": "17890000000000123"}},
        },
    }
    entry = describe_entry_point(event)
    assert entry.source == "story_reply"
    assert entry.media_id == "17890000000000123"


def test_story_routes_require_exact_media_id_and_no_url(story_db):
    route = validate_route_payload({
        "source_type": "story", "media_id": "17890000000000123",
        "routing_topic": "Jolbhora", "priority_products": ["jolbhora"],
    })
    assert route["source_type"] == "story"
    assert route["media_id"] == "17890000000000123"
    with pytest.raises(ValueError, match="Story media ID"):
        validate_route_payload({"source_type": "story", "url": "https://instagram.com/p/not-authoritative/"})
    with pytest.raises(ValueError, match="requires a Story media ID"):
        validate_route_payload({"source_type": "story", "routing_topic": "Jolbhora"})


def test_mapped_story_does_not_authorize_unregistered_jolbhora_price(story_db):
    saved = story_db.save_instagram_source_route(_story_route("story-price"))
    uid = "story-price-" + uuid.uuid4().hex[:8]
    with patch.object(app_module, "send_instagram_reply"), patch.object(app_module.escalator, "handle_escalation"):
        app_module._process_instagram_event(
            uid, "price?", None,
            EntryPoint(source="story_reply", media_id="story-price"),
        )
    row = story_db.get_connection()
    with row as conn:
        logged = conn.execute(
            "SELECT * FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)
        ).fetchone()
    assert logged["template_id"] == "instagram_support_handoff"
    assert "Manager of Kitchens" in logged["bot_response"]
    assert logged["source_context_used"] == 0
    assert logged["source_type"] == "story"
    assert logged["media_id"] == "story-price"
    assert logged["source_routing_id"] == saved["routing_id"]


@pytest.mark.parametrize("text, expected", [
    ("Where is your kitchen?", "GUARD_pickup_address_hours"),
    ("Can I pay cash on delivery?", "GUARD_payment_method"),
    ("How should I store the sondesh?", "GUARD_shelf_life_storage"),
])
def test_story_context_never_overrides_protected_guards(story_db, text, expected):
    uid = "story-guard-result-" + uuid.uuid4().hex[:8]
    _with_story_context(uid)
    _response, matched, template_id, _images = _reply(uid, text)
    assert matched is False
    assert template_id is None
    assert "Manager of Kitchens" in _response
    assert session_memory.get_facts(uid, "instagram")["last_route_source"] == "HUMAN_HANDOFF"


def test_explicit_product_beats_mapped_story_and_switches_followup(story_db, eligible_weekly_asset):
    uid = "story-switch-" + uuid.uuid4().hex[:8]
    facts = _with_story_context(uid)
    _response, matched, template_id, _ = _reply(uid, "tell me more")
    assert matched is False and template_id is None
    assert "Manager of Kitchens" in _response
    _response, matched, template_id, _ = _reply(uid, "Actually tell me about Kanchagolla")
    assert matched is False and template_id is None
    assert facts["pending_clarification"] is None
    _response, matched, template_id, _ = _reply(uid, "500g price?")
    assert matched is True and template_id == "savoury_food_intro"
    assert _response == client_qa.REGISTRY_BY_ID["general_price"].answer
    assert facts["source_context_used"] is False


def test_unmapped_story_is_recorded_but_does_not_guess(story_db):
    story_db.record_instagram_story_source(
        "unknown-story", temporary_url="https://cdn.example/temporary.jpg",
    )
    route, method = route_for_inbound(
        ad_id=None, media_id="unknown-story", source_type="story_reply",
    )
    assert route is None and method is None
    source = story_db.get_instagram_story_source("unknown-story")
    assert story_db.list_instagram_story_sources()[0]["mapping_status"] == "UNMAPPED"
    assert source["temporary_url"] == "https://cdn.example/temporary.jpg"

    uid = "story-unmapped-" + uuid.uuid4().hex[:8]
    session_memory.clear_session(uid, "instagram")
    _response, matched, template_id, _ = _reply(uid, "price?")
    assert matched is False
    assert template_id is None
    assert "Manager of Kitchens" in _response


def test_caption_and_similar_story_ids_never_route(story_db):
    story_db.save_instagram_source_route(_story_route("known-story"))
    assert route_for_inbound(
        ad_id=None, media_id="different-story", source_type="story_reply",
    ) == (None, None)
    assert route_for_inbound(
        ad_id=None, media_id="Jolbhora", source_type="story_reply",
    ) == (None, None)


def test_story_mentions_and_reactions_are_observed_without_product_claims(story_db):
    from starlette.background import BackgroundTasks

    payload = {
        "object": "instagram",
        "entry": [{"messaging": [
            {"sender": {"id": "mention-user"}, "message": {"story_mention": {"id": "their-story"}, "text": "hi"}},
            {"sender": {"id": "reaction-user"}, "message": {"reply_to": {"story": {"id": "story-r"}}, "text": "🔥"}},
        ]}],
    }
    result = app_module._dispatch_instagram_payload(payload, BackgroundTasks())
    assert [item["action"] for item in result["results"]] == [
        "ignored_story_mention", "ignored_story_reaction",
    ]
    assert story_db.get_instagram_story_source("story-r") is not None


@pytest.mark.parametrize("source_type", ["reel", "post", "story"])
def test_mapped_media_with_same_event_text_uses_the_source_route(story_db, source_type):
    identifiers = {
        "reel": {"shortcode": "reel-jolbhora"},
        "post": {"media_id": "post-jolbhora"},
        "story": {"media_id": "story-jolbhora-same-event"},
    }[source_type]
    route = story_db.save_instagram_source_route(
        _mapped_media_route(source_type, **identifiers)
    )
    uid = f"same-event-{source_type}-" + uuid.uuid4().hex[:8]
    entry = EntryPoint(
        source="story_reply" if source_type == "story" else "shared_media",
        media_id=identifiers.get("media_id"),
    )
    sent = []
    with patch.object(app_module, "send_instagram_reply", lambda *args, **kwargs: sent.append((args, kwargs))):
        app_module._process_instagram_event(
            uid,
            "Tell me more",
            None,
            entry,
            {"shortcode": identifiers["shortcode"]} if source_type == "reel" else None,
            identifiers.get("media_id") if source_type == "post" else None,
            source_type if source_type != "story" else None,
        )

    assert len(sent) == 1
    assert "can't open photos" not in sent[0][0][1].lower()
    assert "voice notes" not in sent[0][0][1].lower()
    with story_db.get_connection() as conn:
        logged = conn.execute(
            "SELECT * FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)
        ).fetchone()
    assert logged["source_type"] == source_type
    assert logged["source_routing_id"] == route["routing_id"]
    assert logged["source_context_available"] == 1
    assert logged["source_context_used"] == 0
    assert logged["customer_text_accompanied_media"] == 1


@pytest.mark.parametrize("source_type", ["reel", "post", "story"])
def test_mapped_media_without_text_uses_one_configured_source_response(story_db, source_type):
    identifiers = {
        "reel": {"shortcode": "reel-jolbhora-no-text"},
        "post": {"media_id": "post-jolbhora-no-text"},
        "story": {"media_id": "story-jolbhora-no-text"},
    }[source_type]
    story_db.save_instagram_source_route(
        _mapped_media_route(source_type, **identifiers)
    )
    uid = f"no-text-{source_type}-" + uuid.uuid4().hex[:8]
    entry = EntryPoint(
        source="story_reply" if source_type == "story" else "shared_media",
        media_id=identifiers.get("media_id"),
    )
    sent = []
    with patch.object(app_module, "send_instagram_reply", lambda *args, **kwargs: sent.append((args, kwargs))):
        app_module._process_instagram_event(
            uid,
            "",
            None,
            entry,
            {"shortcode": identifiers["shortcode"]} if source_type == "reel" else None,
            identifiers.get("media_id") if source_type == "post" else None,
            source_type if source_type != "story" else None,
        )

    assert sent == []  # media-only shares stay silent
    assert session_memory.get_facts(uid, "instagram")["source_route"]["active"] is True


def test_mapped_media_explicit_product_and_guard_override_the_source(story_db):
    story_db.save_instagram_source_route(
        _mapped_media_route("reel", shortcode="reel-jolbhora-override")
    )
    uid = "source-overrides-" + uuid.uuid4().hex[:8]
    sent = []
    with patch.object(app_module, "send_instagram_reply", lambda *args, **kwargs: sent.append(args[1])):
        app_module._process_instagram_event(
            uid, "What is the Kanchagolla price?", None,
            EntryPoint(source="shared_media"),
            {"shortcode": "reel-jolbhora-override"}, None, "reel",
        )
        app_module._process_instagram_event(
            uid, "Where is your kitchen?", None,
            EntryPoint(source="shared_media"),
        )
    assert len(sent) == 2
    assert all("Manager of Kitchens" in text for text in sent)


def test_unknown_media_hands_off_vague_share_without_claiming_product_access(story_db):
    explicit_uid = "unknown-explicit-" + uuid.uuid4().hex[:8]
    vague_uid = "unknown-vague-" + uuid.uuid4().hex[:8]
    sent = []
    with patch.object(app_module, "send_instagram_reply", lambda *args, **kwargs: sent.append(args[1])):
        app_module._process_instagram_event(
            explicit_uid, "Kanchagolla price?", None,
            EntryPoint(source="shared_media"),
            {"shortcode": "unmapped-reel"}, None, "reel",
        )
        app_module._process_instagram_event(
            vague_uid, "Tell me more", None,
            EntryPoint(source="shared_media"),
            {"shortcode": "another-unmapped-reel"}, None, "reel",
        )
    assert "Manager of Kitchens" in sent[0]
    assert "Manager of Kitchens" in sent[1]
    assert "jolbhora" not in sent[1].lower()


def test_standalone_tell_me_more_and_unsupported_attachment_keep_safe_fallbacks(story_db):
    uid = "no-source-" + uuid.uuid4().hex[:8]
    response = responder.match_and_respond(
        "Tell me more", user_id=uid, channel="instagram",
    )
    assert response[2] is None
    assert "Manager of Kitchens" in response[0]

    fallback = []
    with patch.object(app_module, "_reply_to_unreadable_attachment", lambda sender, **_kwargs: fallback.append(sender)):
        app_module._handle_shared_attachment("unsupported-attachment", None, None)
    assert fallback == ["unsupported-attachment"]


def test_story_temporary_url_is_sanitized_and_not_exposed_by_admin(story_db):
    event = {
        "id": "event-story-secret",
        "timestamp": 123,
        "message": {
            "mid": "message-story-secret",
            "reply_to": {"story": {
                "id": "story-secret",
                "url": "https://cdn.example/story.jpg?token=SECRET&access_token=ABC&sig=XYZ&expires=123&arbitrary=value#fragment",
            }},
        },
    }
    app_module._record_story_reply(event, "story-customer", "story-secret")
    saved = story_db.get_instagram_story_source("story-secret")
    assert saved["source"] == "WEBHOOK_STORY_REPLY"
    assert saved["temporary_url"] == "https://cdn.example/story.jpg"
    assert all(secret not in saved["temporary_url"] for secret in ("SECRET", "ABC", "XYZ", "123", "value"))

    story_db.save_instagram_source_route(_story_route("story-secret"))
    route, method = route_for_inbound(
        ad_id=None, media_id="story-secret", source_type="story_reply",
    )
    assert route["routing_topic"] == "Jolbhora"
    assert method == "story_media_id"
    with patch.object(app_module, "require_admin", return_value=None):
        admin_payload = app_module.list_instagram_story_sources()
    assert "SECRET" not in str(admin_payload)
    assert "access_token" not in str(admin_payload)
    assert "fragment" not in str(admin_payload)


def test_deleting_story_hard_deletes_local_source_and_exact_route(story_db):
    story_db.record_instagram_story_source("story-delete", seen_at="2026-01-01T00:00:00")
    story_db.record_instagram_story_source("story-keep", seen_at="2026-01-02T00:00:00")
    route = story_db.save_instagram_source_route(_story_route("story-delete"))

    assert story_db.delete_instagram_story_source("story-delete") is True
    remaining = story_db.list_instagram_story_sources()
    assert len(remaining) == 1
    assert remaining[0]["story_media_id"] == "story-keep"
    assert remaining[0]["mapping_status"] == "UNMAPPED"
    assert story_db.get_instagram_story_source("story-delete") is None
    assert story_db.get_instagram_source_route(route["routing_id"]) is None
    assert route_for_inbound(
        ad_id=None, media_id="story-delete", source_type="story_reply",
    ) == (None, None)


def test_story_restore_api_is_removed(story_db):
    story_db.record_instagram_story_source("story-restore", seen_at="2026-01-01T00:00:00")
    route = story_db.save_instagram_source_route(_story_route("story-restore"))
    story_db.delete_instagram_story_source("story-restore")
    assert story_db.get_instagram_story_source("story-restore") is None
    assert story_db.get_instagram_source_route(route["routing_id"]) is None
    assert not hasattr(story_db, "restore_instagram_story_source")
    assert route_for_inbound(
        ad_id=None, media_id="story-restore", source_type="story_reply",
    ) == (None, None)


def test_story_id_rendering_uses_data_not_inline_executable_id(monkeypatch, story_db):
    hostile_ids = ['"', "'", "<", ">", "&", '123" onclick="alert(1)']
    for index, media_id in enumerate(hostile_ids):
        story_db.record_instagram_story_source(media_id, seen_at=f"2026-01-01T00:00:0{index}")

    with patch.object(app_module, "require_admin", return_value=None):
        admin_payload = app_module.list_instagram_story_sources()
    assert {item["story_media_id"] for item in admin_payload["story_sources"]} >= set(hostile_ids)

    html = app_module.get_admin_dashboard().body.decode()
    assert 'useCapturedStory(${JSON.stringify(source.story_media_id)})' not in html
    assert 'data-story-media-id="${escapeHtml(source.story_media_id || \'\')}"' in html
    assert "button.addEventListener('click'" in html


def test_mapped_shared_post_audit_retains_exact_source_media_id(story_db):
    route = story_db.save_instagram_source_route(
        _mapped_media_route("post", media_id="post-audit-id")
    )
    uid = "post-audit-" + uuid.uuid4().hex[:8]
    with patch.object(app_module, "send_instagram_reply"):
        app_module._process_instagram_event(
            uid, "Tell me more", None, EntryPoint(source="shared_media"),
            None, "post-audit-id", "post",
        )
    with story_db.get_connection() as conn:
        logged = conn.execute(
            "SELECT * FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)
        ).fetchone()
    assert logged["source_type"] == "post"
    assert logged["source_routing_id"] == route["routing_id"]
    assert logged["source_match_method"] == "shared_post_media_id"
    assert logged["media_id"] == "post-audit-id"


@pytest.mark.parametrize("source_type", ["reel", "post", "story"])
def test_unknown_media_without_text_stays_silent_and_never_guesses(story_db, source_type):
    identifiers = {
        "reel": {"shortcode": "unknown-audit-reel"},
        "post": {"media_id": "unknown-audit-post"},
        "story": {"media_id": "unknown-audit-story"},
    }[source_type]
    uid = f"unknown-audit-{source_type}-" + uuid.uuid4().hex[:8]
    sent = []

    def observe_send(*args, **kwargs):
        with story_db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)
            ).fetchone()
        sent.append((args, row))

    with patch.object(app_module, "send_instagram_reply", side_effect=observe_send):
        if source_type == "reel":
            app_module._handle_shared_attachment(uid, identifiers, None)
        elif source_type == "post":
            app_module._handle_shared_attachment(uid, None, identifiers["media_id"])
        else:
            app_module._process_instagram_event(
                uid, "", None,
                EntryPoint(source="story_reply", media_id=identifiers["media_id"]),
            )

    if source_type == "story":
        assert sent == []
    else:
        assert len(sent) == 1
        row = sent[0][1]
        assert row is not None
        assert row["source_type"] == source_type
        assert row["source_routing_id"] is None
        assert row["source_context_available"] == 0
        assert row["source_context_used"] == 0
        assert row["source_media_present"] == 1
        assert row["customer_text_accompanied_media"] == 0
        if source_type == "reel":
            assert row["shortcode"] == identifiers["shortcode"]
            assert row["source_match_method"] == "shared_media_unmapped"
        else:
            assert row["media_id"] == identifiers["media_id"]
            assert row["source_match_method"] == "shared_media_unmapped"
