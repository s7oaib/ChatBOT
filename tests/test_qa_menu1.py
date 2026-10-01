"""Reviewed menu authority outranks stale conversational state, never review."""
import copy
import io
import json
import uuid
from datetime import date

import pytest
from PIL import Image

from camellia import menu_dates, menu_images, menu_review, order_intake, weekly_savoury
from camellia.config import config
from camellia.database import db
from camellia.rag_fallback import rag_fallback_engine
from camellia.responder import responder
from camellia.session_memory import session_memory
from test_weekly_savoury_lifecycle import weekly_env, _draft, _publish


@pytest.fixture
def current_menu(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, "today_ist", lambda: date(2026, 9, 27))
    monkeypatch.setattr(config, "BYPASS_AD_GATE", True)
    return _publish(_draft("22 Sep - 28 Sep 2026", 100, "Dhokar Dalna"))


def respond(message, uid=None):
    return responder.match_and_respond(message, user_id=uid or uuid.uuid4().hex, channel="instagram")


@pytest.mark.parametrize("query", [
    "What is the price of Dhokar Dalna?", "How much is Dhokar Dalna?",
    "Dhokar Dalna price?", "What is the price of Dhokar Dolna?",
    "How much is Dhokar Dolna?", "Dhokar Dolna price?",
    "DHOKAR   DALNA price?", "Dhokar-Dalna price?",
])
def test_grounded_generic_price_and_safe_typo(current_menu, query):
    result = rag_fallback_engine.retrieve_approved_menu(query)
    assert result.auto_response == "Dhokar Dalna 500g is ₹100."
    assert result.candidate_id is None
    assert f"version:{current_menu['version_id']}" in result.source_refs[0]
    assert rag_fallback_engine.menu_grounding_report(result.auto_response, result.source_chunks)[0]
    answer = respond(query)
    assert answer[0] == result.auto_response
    assert answer[2] == "approved_menu"


BROWSE = ["give me weekly menu", "give me savoury menu", "show me the menu",
          "show menu", "menu please", "give me the menu", "I want to see the menu",
          "I want to know about the menu", "what's on the menu?",
          "what is on this week's menu?"]


@pytest.mark.parametrize("query", BROWSE)
def test_menu_browsing_preserves_order_draft_and_ignores_old_topic(current_menu, query):
    uid = uuid.uuid4().hex
    facts = session_memory.get_facts(uid, "instagram")
    draft = order_intake.new_order_draft()
    order_intake.update_order_draft(draft, "2 boxes Sondesh Gurgaon Friday dinner")
    facts.update(order_intake_draft=draft, last_topic="bengali_home_table", active_topic="subscription")
    before = copy.deepcopy(draft)
    answer = respond(query, uid)
    assert answer[2] is None  # A text-only approval has no flyer to send.
    assert "₹" not in answer[0]
    assert "unchanged" not in answer[0].lower()
    assert facts["order_intake_draft"] == before
    assert f"version:{current_menu['version_id']}" in facts["last_knowledge_ids"][0]
    assert respond("show me my order", uid)[2] == "order_intake_draft"
    assert facts["order_intake_draft"] == before


def test_pending_replacement_then_publish_hot_reload(current_menu):
    old = current_menu['version_id']
    draft = _draft("22 Sep - 28 Sep 2026", 200, "Dhokar Dalna")
    assert weekly_savoury.WEEKLY_KEY in menu_review.pending_review_keys()
    queries = ["Dhokar Dalna price?", "Dhokar Dolna price?", *BROWSE]
    for query in queries:
        assert not rag_fallback_engine.retrieve_approved_menu(query).auto_response
        answer = respond(query)
        assert "₹100" not in answer[0] and "₹200" not in answer[0]
        assert answer[2] == "pending_menu_handoff"
    new = _publish(draft, replace_version_id=old)
    assert not menu_review.pending_review_keys()
    assert weekly_savoury.current_version()["id"] == new["version_id"]
    assert [v['id'] for v in menu_review.current_versions()] == [new['version_id']]
    assert [v['id'] for v in menu_review.customer_versions()] == [new['version_id']]
    for query in queries:
        answer = respond(query)
        if query in BROWSE:
            assert "₹" not in answer[0]
        else:
            assert "₹200" in answer[0] and "₹100" not in answer[0]


def test_unique_stored_alias_bypasses_unknown_price_gate(current_menu):
    draft = _draft("22 Sep - 28 Sep 2026", 100, "Dhokar Dalna")
    rows = copy.deepcopy(draft['rows'])
    rows[0]['aliases'] = ['Kitchen Special']
    draft = menu_review.edit(weekly_savoury.WEEKLY_KEY, draft['draft_id'], draft['revision'], rows, 'admin')
    _publish(draft, replace_version_id=current_menu['version_id'])
    assert respond('What is the price of Kitchen Special?')[0] == 'Dhokar Dalna 500g is ₹100.'


def test_two_near_matches_fail_closed(current_menu):
    draft = _draft("22 Sep - 28 Sep 2026", 100, "Dhokar Dalna")
    rows = copy.deepcopy(draft['rows'])
    rows.append({**copy.deepcopy(rows[0]), 'item': 'Dhokar Dulna', 'row_id': 'second', 'price_inr': 200})
    draft = menu_review.edit(weekly_savoury.WEEKLY_KEY, draft['draft_id'], draft['revision'], rows, 'admin')
    _publish(draft, replace_version_id=current_menu['version_id'])
    result = rag_fallback_engine.retrieve_approved_menu('Dhokar Dolna price?')
    assert result.has_conflict and not result.auto_response
    assert '₹' not in respond('Dhokar Dolna price?')[0]


@pytest.mark.parametrize('today', [date(2026, 9, 21), date(2026, 9, 29)])
def test_upcoming_or_expired_cannot_serve_prices_or_flyer(current_menu, monkeypatch, today):
    monkeypatch.setattr(menu_dates, 'today_ist', lambda: today)
    assert weekly_savoury.current_version() is None
    assert menu_review.customer_versions() == []
    for query in ['Dhokar Dalna price?', 'Dhokar Dolna price?', *BROWSE]:
        assert '₹100' not in respond(query)[0]
    assert menu_images.current_weekly_url() is None


def test_draft_only_never_authorizes_price(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, 'today_ist', lambda: date(2026, 9, 27))
    _draft('22 Sep - 28 Sep 2026', 200, 'Dhokar Dalna')
    for query in ['Dhokar Dalna price?', 'Dhokar Dolna price?']:
        assert not rag_fallback_engine.retrieve_approved_menu(query).auto_response
        assert '₹200' not in respond(query)[0]


def test_current_flyer_replacement_and_static_fallback(weekly_env, monkeypatch):
    monkeypatch.setattr(menu_dates, 'today_ist', lambda: date(2026, 9, 27))
    price = [100]
    monkeypatch.setattr(menu_review.menu_parser, 'parse_image_and_update_menu', lambda *_: {
        'status': 'success', 'week_of': '22 Sep - 28 Sep 2026', 'parsed': {
            'vegetarian': [{'item': 'Dhokar Dalna', 'quantity': '500g', 'price_inr': price[0], 'description': ''}]}})
    def photo(color):
        buffer = io.BytesIO()
        Image.new('RGB', (120, 160), color).save(buffer, format='PNG')
        return buffer.getvalue()
    def publish_photo(color, **options):
        draft = menu_review.extract(weekly_savoury.WEEKLY_KEY, 'admin', data=photo(color), mime='image/png', weekly=True)
        return _publish(draft, **options)
    first = publish_photo('red')
    old_image = json.loads(db.get_menu_version(first['version_id'])['approval_json'])['image_path']
    assert respond('show me the menu')[3] == [old_image]
    price[0] = 200
    second = publish_photo('blue', replace_version_id=first['version_id'])
    new_image = json.loads(db.get_menu_version(second['version_id'])['approval_json'])['image_path']
    assert new_image != old_image
    for query in BROWSE:
        uid = uuid.uuid4().hex
        answer = respond(query, uid)
        assert answer[3] == [new_image]
        assert answer[0] == ''
        assert answer[2] == 'approved_menu'
        assert respond('What is the price of Dhokar Dalna?', uid)[0] == 'Dhokar Dalna 500g is ₹200.'
    assert menu_images.current_weekly_url() == new_image
    assert menu_review.canonical_menu()['weekly_menus'][-1]['vegetarian'][0]['price_inr'] == 200
