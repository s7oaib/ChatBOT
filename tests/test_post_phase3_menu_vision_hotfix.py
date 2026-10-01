"""Extraction failures are review drafts, never remove-all publications."""
import ast
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from camellia import menu_review, monitoring
from camellia.config import config
from camellia.database import db
from camellia.menu_parser import menu_parser
from camellia.admin_menu_review import REVIEW_SCRIPT
from camellia.admin_weekly_savoury import WEEKLY_SCRIPT


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    path = tmp_path / 'menu.json'
    path.write_text('{"weekly_menus":[{"vegetarian":[{"item":"Old dish","quantity":"250g","price_inr":100}]}]}')
    monkeypatch.setattr(config, 'MENU_JSON_PATH', str(path))
    monkeypatch.setattr(db, 'db_path', str(tmp_path / 'review.db'))
    monkeypatch.setattr(menu_review, '_private_dir', lambda: tmp_path)
    db.init_db()
    return path


def test_default_model_is_current_without_changing_chat_model():
    source = Path('camellia/config.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    vision = next(n for n in ast.walk(tree) if isinstance(n, ast.AnnAssign)
                  and getattr(n.target, 'id', '') == 'GROQ_VISION_MODEL')
    assert vision.value.args[1].value == 'qwen/qwen3.8-27b'
    assert 'GROQ_MODEL' in source
    assert 'GROQ_VISION_MODEL:-qwen/qwen3.8-27b' in Path('docker-compose.yml').read_text()


def test_model_404_is_not_auth_failure_and_never_retried(monkeypatch):
    events = monitoring.ProviderEvents()
    monkeypatch.setattr(monitoring, 'provider_events', events)
    monkeypatch.setattr(config, 'GROQ_API_KEY', 'fixture-only')
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, *args, **kwargs):
            calls.append(kwargs['json'].copy())
            return httpx.Response(404, json={'error': {'code': 'model_not_found', 'message': 'PRIVATE'}})

    monkeypatch.setattr(httpx, 'Client', Client)
    result = menu_parser._parse_image_with_groq(b'synthetic', 'image/png')
    assert result['diagnostic_codes'] == ['PROVIDER_MODEL_UNAVAILABLE']
    assert 'PRIVATE' not in json.dumps(result)
    assert events.snapshot()['counts'] == {'model_unavailable': 1}
    assert len(calls) == 1
    assert calls[0]['reasoning_effort'] == 'none'
    assert calls[0]['response_format'] == {'type': 'json_object'}
    assert calls[0]['max_tokens'] == 900
    assert calls[0]['messages'][0]['content'][1]['type'] == 'image_url'


@pytest.mark.parametrize('key,weekly', [('weekly_vegetarian', True), ('signature', False)])
@pytest.mark.parametrize('failure', ['model', 'exception', 'empty'])
def test_failed_extraction_retains_source_and_cannot_publish(isolated, monkeypatch, key, weekly, failure):
    before = isolated.read_bytes()
    image = io.BytesIO()
    Image.new('RGB', (4, 4), 'white').save(image, 'PNG')

    def extract(*args):
        if failure == 'exception':
            raise RuntimeError('PRIVATE_PROVIDER_PAYLOAD')
        if failure == 'empty':
            return {'status': 'success', 'parsed': {}}
        return {'status': 'failed', 'diagnostic_codes': ['PROVIDER_MODEL_UNAVAILABLE']}

    monkeypatch.setattr(menu_review.menu_parser, 'parse_image_and_update_menu', extract)
    draft = menu_review.extract(key, 'fixture', data=image.getvalue(), mime='image/png', weekly=weekly)
    assert draft['status'] == 'NEEDS_REVIEW'
    assert draft['rows'] == []
    assert draft['source_asset'] and (isolated.parent / draft['source_asset']).exists()
    assert 'PRIVATE_PROVIDER_PAYLOAD' not in json.dumps(draft)
    with pytest.raises(ValueError):
        menu_review.publish_approved_menu(key, draft['draft_id'], draft['revision'], 'fixture',
                                         weekly_options={'accept_undated': True} if weekly else None)
    assert db.get_menu_versions(key) == []
    assert isolated.read_bytes() == before
    if weekly:
        analysis = menu_review.weekly_analysis(draft)
        assert analysis['comparison_available'] is False
        assert all(not value for value in analysis['changes'].values())
    retried = menu_review.retry_extraction(key, draft['draft_id'], draft['revision'], 'fixture')
    assert retried['status'] == 'NEEDS_REVIEW' and retried['source_asset']


@pytest.mark.parametrize('rows', [[], [{'item': '', 'category': 'vegetarian', 'quantity': '', 'price_inr': None}]])
def test_empty_or_invalid_rows_do_not_compare_as_removals(isolated, rows):
    draft = {'menu_key': 'weekly_vegetarian', 'weekly_mode': True, 'rows': rows}
    analysis = menu_review.weekly_analysis(draft)
    assert analysis['comparison_available'] is False
    assert analysis['changes']['removed'] == []


def test_manual_valid_row_restores_normal_comparison(isolated):
    draft = {'menu_key': 'weekly_vegetarian', 'weekly_mode': True,
             'rows': [{'item': 'New dish', 'category': 'vegetarian', 'quantity': '250g', 'price_inr': 150}]}
    analysis = menu_review.weekly_analysis(draft)
    assert analysis['comparison_available'] is True
    assert analysis['changes']['new'][0]['item'] == 'New dish'
    assert analysis['changes']['removed'][0]['item'] == 'Old dish'


@pytest.mark.parametrize('script_name', ['review', 'weekly'])
def test_failure_copy_and_manual_actions_remain_available(script_name):
    script = REVIEW_SCRIPT if script_name == 'review' else WEEKLY_SCRIPT
    assert 'Extraction unsuccessful' in script
    assert 'Your published menu is unchanged' in script
    assert 'RetryExtraction' in script
    assert 'Save' in script
    assert '.disabled=' in script


def test_weekly_comparison_is_explicitly_unavailable_and_publish_guarded():
    assert 'Comparison unavailable until at least one valid menu item is entered.' in WEEKLY_SCRIPT
    assert 'if(!analysis?.comparison_available)reasons.push(' in WEEKLY_SCRIPT
    assert 'const reasons=wsBlockingReasons(wsAnalysis,options);if(reasons.length)' in WEEKLY_SCRIPT
