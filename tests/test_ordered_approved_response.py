"""Positional client-approved delivery, without generated copy or live sends."""

from unittest.mock import patch

import pytest

from camellia import app as app_module, client_qa
from camellia.config import config
from camellia.database import Database
from camellia.responder import responder
from camellia.session_memory import session_memory


def _resolved(question, user_id):
    session_memory.clear_session(user_id, "instagram")
    text, matched, _template, _images = responder.match_and_respond(
        question, user_id=user_id, channel="instagram"
    )
    assert matched
    facts = session_memory.get_facts(user_id, "instagram")
    assert facts["last_route_source"] == "EXACT_APPROVED"
    return text, facts["ordered_response_blocks"]


@pytest.mark.parametrize("qa_id,question", [
    ("sondesh_details_client", "Tell me more about Sondesh"),
    ("sondesh_price_client", "What's the price of Sondesh?"),
])
def test_sondesh_text_and_menus_follow_canonical_position(qa_id, question, eligible_weekly_asset):
    text, blocks = _resolved(question, f"ordered-{qa_id}")
    assert text == client_qa.REGISTRY_BY_ID[qa_id].answer
    timeline = [(block["kind"], block.get("asset_key"), block.get("text", "")) for block in blocks]
    assets = [(i, key) for i, (kind, key, _) in enumerate(timeline) if kind != "TEXT"]
    assert [key for _, key in assets] == ["aamar_menu", "signature_menu", "weekly_menu"]
    aamar, signature, weekly = [i for i, _ in assets]
    assert "Collection 1." in timeline[aamar - 1][2]
    assert "Collection 2." in timeline[signature - 1][2]
    assert "Porter charges are on actuals." in timeline[signature + 1][2]
    assert "recommendation" in timeline[weekly - 1][2]
    assert weekly == len(timeline) - 1


@pytest.mark.parametrize("qa_id,question", [
    ("subscription_price", "What is the subscription price?"),
    ("subscription_details_client", "Tell me more about subscription"),
])
def test_subscription_chart_precedes_manager_next_steps(qa_id, question):
    text, blocks = _resolved(question, f"ordered-{qa_id}")
    assert text == client_qa.REGISTRY_BY_ID[qa_id].answer
    chart = next(i for i, block in enumerate(blocks) if block.get("asset_key") == "subscription_rate_chart")
    assert "Price of 5 / 10 / 30 MEALS" in blocks[chart - 1]["text"]
    assert "Manager of Kitchens" in blocks[chart + 1]["text"]


def test_share_weekly_menu_marker_remains_positional(eligible_weekly_asset):
    _text, blocks = _resolved("What is the price of Chicken Kosha?", "ordered-chicken-kosha")
    assert blocks[-1]["asset_key"] == "weekly_menu"
    assert "rest of the menu" in blocks[-2]["text"]


def test_instagram_sender_sequentially_sends_blocks_without_appending_assets():
    blocks = [
        {"kind": "TEXT", "text": "Aamar introduction"},
        {"kind": "MENU", "asset_key": "aamar_menu", "urls": ["/aamar.jpg"]},
        {"kind": "TEXT", "text": "Signature introduction"},
        {"kind": "MENU", "asset_key": "signature_menu", "urls": ["/signature.jpg"]},
    ]
    with patch.object(app_module, "send_instagram_reply", return_value={"message_id": "ok"}) as send:
        app_module.send_instagram_response_blocks("customer", blocks, inbound_message_id=7)
    assert [call.args[1] for call in send.call_args_list] == ["Aamar introduction", "", "Signature introduction", ""]
    assert send.call_args_list[1].kwargs["image_url"] == ["/aamar.jpg"]
    assert send.call_args_list[3].kwargs["image_url"] == ["/signature.jpg"]


def test_failed_text_block_stops_sequence_without_reordering():
    blocks = [{"kind": "TEXT", "text": "first"}, {"kind": "MENU", "urls": ["/card.jpg"]}]
    with patch.object(app_module, "send_instagram_reply", return_value={"status": "failed"}) as send:
        app_module.send_instagram_response_blocks("customer", blocks)
    assert send.call_count == 1


def test_source_only_event_waits_for_customer_intent(monkeypatch):
    user_id = "source-only-delivery-order"
    session_memory.clear_session(user_id, "instagram")
    route = {"routing_id": 44, "source_type": "post", "media_id": "exact-meta-post", "active": True,
             "routing_topic": "Subscription", "source_description": "Subscription source description"}
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    with patch.object(app_module, "route_for_inbound", return_value=(route, "shared_post_media_id")), patch.object(
        app_module, "send_instagram_reply"
    ) as send:
        app_module._process_instagram_event(user_id, "", None, shared_post_id="exact-meta-post")
    send.assert_not_called()
    facts = session_memory.get_facts(user_id, "instagram")
    assert facts["source_route"]["media_id"] == "exact-meta-post"


def test_unmapped_source_only_event_does_not_reply_or_handoff(monkeypatch):
    user_id = "unmapped-source-only-no-handoff"
    session_memory.clear_session(user_id, "instagram")
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    with patch.object(app_module, "route_for_inbound", return_value=(None, None)), patch.object(
        app_module, "_establish_shared_post_context", return_value=False
    ), patch.object(app_module, "send_instagram_reply") as send, patch.object(
        app_module.escalator, "handle_escalation"
    ) as escalate, patch.object(app_module, "create_suggestion") as suggestion:
        app_module._process_instagram_event(user_id, "", None, shared_post_id="unmapped-meta-post")
    send.assert_not_called()
    escalate.assert_not_called()
    suggestion.assert_not_called()
    assert app_module.db.get_active_handoff("instagram", user_id) is None


def test_explicit_sondesh_after_subscription_source_description_never_replays_it(eligible_weekly_asset):
    user_id = "source-then-explicit-sondesh"
    session_memory.clear_session(user_id, "instagram")
    facts = session_memory.get_facts(user_id, "instagram")
    facts["source_route"] = {"routing_id": 45, "source_type": "post", "media_id": "exact-post", "active": True,
                             "routing_topic": "Subscription", "source_description": "Subscription source description"}
    assert responder.match_and_respond("What is this post about?", user_id=user_id, channel="instagram")[0] == "Subscription source description"
    text, matched, _template, _images = responder.match_and_respond("Tell me about Sondesh", user_id=user_id, channel="instagram")
    assert matched and text == client_qa.REGISTRY_BY_ID["sondesh_details_client"].answer
    assert facts["last_route_source"] == "EXACT_APPROVED"
    assert all("Subscription source description" not in block.get("text", "") for block in facts["ordered_response_blocks"])


def test_instagram_event_from_subscription_post_delivers_sondesh_first(monkeypatch):
    user_id = "ordered-explicit-sondesh-post"
    session_memory.clear_session(user_id, "instagram")
    route = {"routing_id": 46, "source_type": "post", "media_id": "exact-sondesh-entry", "active": True,
             "routing_topic": "Subscription", "source_description": "Subscription source description"}
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    with patch.object(app_module, "route_for_inbound", return_value=(route, "shared_post_media_id")), patch.object(
        app_module, "send_instagram_reply", return_value={"message_id": "ok"}
    ) as send:
        app_module._process_instagram_event(
            user_id, "Tell me about Sondesh", None, shared_post_id="exact-sondesh-entry"
        )
    texts = [call.args[1] for call in send.call_args_list if call.args[1]]
    assert texts and "Sondesh" in texts[0]
    assert all("Subscription source description" not in text for text in texts)
    assert send.call_count > 1


URLS = (
    'https://www.instagram.com/reel/DdJhXQMT4VZ/',
    'https://www.instagram.com/reel/Dcd5bbEzbCo/',
    'https://www.instagram.com/reel/DcLZpuQzCKE/',
    'https://www.instagram.com/reel/DcQYp72zf8D/',
)

def resolve(question, user):
    text, matched, *_ = responder.match_and_respond(question, user_id=user, channel='instagram')
    assert matched
    facts = session_memory.get_facts(user, 'instagram')
    return text, facts, facts['ordered_response_blocks']

@pytest.mark.parametrize('qa_id,question', [
    ('home_table_details', 'Tell me about Bengali Home Table'),
    ('home_table_price', 'What is the price of Bengali Home Table?'),
    ('home_table_details', 'tell me about your kolkata home lunch'),
])
def test_first_home_table_preserves_copy_and_exact_link_order(qa_id, question):
    user = 'reels-first-' + question
    session_memory.clear_session(user, 'instagram')
    text, facts, blocks = resolve(question, user)
    assert text == client_qa.REGISTRY_BY_ID[qa_id].answer
    assert [b['kind'] for b in blocks] == ['TEXT', 'LINK', 'LINK', 'LINK', 'LINK', 'TEXT']
    assert tuple(b['url'] for b in blocks if b['kind'] == 'LINK') == URLS
    assert ''.join(b['text'] for b in blocks if b['kind'] == 'TEXT') == text
    assert blocks[0]['text'].endswith(client_qa.HOME_TABLE_VIDEO_ANCHOR)
    assert not facts.get('home_table_videos_sent')
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id':'ok'}) as send:
        app_module.send_instagram_response_blocks(user, blocks)
    assert [c.args[1] for c in send.call_args_list] == [b.get('text', b.get('url')) for b in blocks]
    assert facts['home_table_videos_sent'] is True
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id':'ok'}) as send:
        app_module.send_instagram_response_blocks(user, blocks)
    assert send.call_count == 2


def test_details_then_price_and_payment_do_not_repeat_links():
    user = 'reels-followup'
    session_memory.clear_session(user, 'instagram')
    _, facts, blocks = resolve('Tell me about Bengali Home Table', user)
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id':'ok'}):
        app_module.send_instagram_response_blocks(user, blocks)
    for question in ['how much?', 'how do I pay?']:
        _, _, blocks = resolve(question, user)
        assert all(b['kind'] != 'LINK' for b in blocks)
    assert facts['home_table_videos_sent']


def test_failed_second_link_stops_and_does_not_mark_sent():
    user = 'reels-failure'
    session_memory.clear_session(user, 'instagram')
    _, facts, blocks = resolve('Tell me about Bengali Home Table', user)
    with patch.object(app_module, 'send_instagram_reply', side_effect=[{'message_id':'text'}, {'message_id':'one'}, {'status':'failed'}]) as send:
        app_module.send_instagram_response_blocks(user, blocks)
    assert [c.args[1] for c in send.call_args_list][1:] == list(URLS[:2])
    assert not facts.get('home_table_videos_sent')
    _, _, retry_blocks = resolve('What is the price of Bengali Home Table?', user)
    assert tuple(b['url'] for b in retry_blocks if b['kind'] == 'LINK') == URLS

@pytest.mark.parametrize('question', ['Tell me about Bento', 'Tell me about Sondesh', 'subscription price', 'chicken kosha price', 'menu prices please', 'How do I pay for Home Table?'])
def test_unrelated_and_payment_responses_have_no_links(question):
    user = 'reels-unrelated-' + question
    session_memory.clear_session(user, 'instagram')
    _, _, blocks = resolve(question, user)
    assert all(b['kind'] != 'LINK' for b in blocks)

@pytest.mark.parametrize('question', ['Tell me about Bento', 'Tell me about Sondesh'])
def test_explicit_switch_from_home_table_never_attaches_links(question):
    user = 'reels-switch-' + question
    session_memory.clear_session(user, 'instagram')
    resolve('Tell me about Bengali Home Table', user)
    _, _, blocks = resolve(question, user)
    assert all(b['kind'] != 'LINK' for b in blocks)


def test_durable_queue_accepts_exact_order_before_marking_sent(tmp_path, monkeypatch):
    database = Database(str(tmp_path / 'links.db'))
    monkeypatch.setattr(app_module, 'db', database)
    user = 'reels-durable'
    session_memory.clear_session(user, 'instagram')
    _, facts, blocks = resolve('Tell me about Bengali Home Table', user)
    job_id, _ = database.enqueue_instagram_inbound_job('reels-event', user, '_process_instagram_event', {})
    token = app_module._instagram_job_context.set({'job_id':job_id, 'sequence':0})
    try:
        with patch.object(app_module, 'send_instagram_reply') as send:
            app_module.send_instagram_response_blocks(user, blocks)
        send.assert_not_called()
    finally:
        app_module._instagram_job_context.reset(token)
    assert facts['home_table_videos_sent']
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id':'ok'}) as send:
        app_module._process_instagram_outbound_jobs(inbound_job_id=job_id)
    assert [c.args[1] for c in send.call_args_list] == [b.get('text', b.get('url')) for b in blocks]



def test_link_delivery_fact_persists_and_expires_with_conversation(tmp_path, monkeypatch):
    import time
    from camellia.session_memory import SessionMemoryManager
    from camellia.config import config
    database = Database(str(tmp_path / 'session.db'))
    memory = SessionMemoryManager(persistence_enabled=True, database=database)
    monkeypatch.setattr(app_module, 'session_memory', memory)
    blocks = [{'kind':'LINK', 'url':url} for url in URLS]
    with patch.object(app_module, 'send_instagram_reply', return_value={'message_id':'ok'}):
        app_module.send_instagram_response_blocks('persistent-reels', blocks)
    restarted = SessionMemoryManager(persistence_enabled=True, database=database)
    assert restarted.get_facts('persistent-reels', 'instagram')['home_table_videos_sent']
    future = time.time() + config.SESSION_TTL_SECONDS + 1
    with patch('camellia.session_memory.time.time', return_value=future):
        assert not restarted.get_facts('persistent-reels', 'instagram')['home_table_videos_sent']
