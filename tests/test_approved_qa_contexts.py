"""Context selection is an Admin-only constraint, never a runtime migration."""
import json
import shutil
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from camellia import approved_qa, client_qa, app as app_module
from camellia.admin_approved_qa import SCRIPT
from camellia.database import Database
from camellia.config import config


@pytest.fixture
def store(tmp_path, monkeypatch):
    database = Database(str(tmp_path / 'contexts.db'))
    monkeypatch.setattr(approved_qa, '_database', lambda: database)
    monkeypatch.setattr(app_module, 'db', database)
    return database


@pytest.mark.parametrize('context', list(approved_qa.CANONICAL_CONTEXTS))
def test_new_qa_can_choose_canonical_context_without_copy_changes(store, context):
    body = dict(question='A unique context test?', answer='Exact approved test answer.', context=context,
                aliases=['Unique context variation'])
    draft = approved_qa.preview(body)
    record = approved_qa.publish({**body, 'qa_id': draft['qa_id'], 'preview_token': draft['preview_token']}, 'test')
    assert record['context'] == context
    assert record['question'] == body['question'] and record['answer'] == body['answer']
    assert record['aliases'] == body['aliases'] and record['asset_keys'] == []


def test_existing_and_retained_contexts_remain_exact_and_reads_do_not_write(store):
    key = 'sondesh_price_client'
    with store.get_connection() as conn:
        conn.execute("INSERT INTO approved_qa_versions(qa_id,snapshot_json,changed_at,changed_by,change_reason) VALUES (?,?,'now','test','history')",
                     (key, json.dumps({'context': 'Sandesh <legacy>'})))
    before = approved_qa.registry()
    values = {option['value'] for option in approved_qa.listing()['contexts']}
    assert 'Sandesh <legacy>' in values
    assert {entry.context for entry in client_qa.REGISTRY} <= values
    assert approved_qa.registry() == before
    draft = approved_qa.preview({'qa_id': key, 'context': 'Sandesh <legacy>'})
    assert draft['proposed']['context'] == 'Sandesh <legacy>'
    assert approved_qa.get(key).context == 'sondesh'


def test_arbitrary_context_is_rejected_by_api_before_any_mutation(store, monkeypatch):
    before = approved_qa.overlays(strict=True)
    monkeypatch.setattr(config, 'ADMIN_API_KEY', 'test-context-machine-key')
    response = TestClient(app_module.app, base_url='https://testserver').post(
        '/admin/approved-qa/preview', headers={'X-Admin-Key': 'test-context-machine-key'}, json={
        'question': 'Unique question?', 'answer': 'Exact test copy.', 'context': 'unapproved-context'})
    assert response.status_code == 409
    assert approved_qa.overlays(strict=True) == before


def test_existing_qa_matching_answer_assets_and_validity_unchanged(store):
    before = approved_qa.listing()['records']
    for entry in approved_qa.registry():
        draft = approved_qa.preview({'qa_id': entry.qa_id, 'context': entry.context})
        assert draft['proposed']['answer'] == entry.answer
        assert draft['proposed']['asset_keys'] == list(entry.asset_keys)
    assert approved_qa.listing()['records'] == before


def test_executed_editor_uses_safe_dropdown_and_loads_exact_legacy_selection():
    node = shutil.which('node')
    assert node, 'Node is required to execute the Admin editor'
    script = SCRIPT.split('<script>', 1)[1].split('</script>', 1)[0]
    harness = r'''
const elements = {};
const document = {getElementById(id){return elements[id] ||= {value:'',style:{},replaceChildren(...options){this.options=options},showModal(){}}}};
const Option = function(label,value){this.text=label;this.value=value};
const escapeHtml = value => String(value).replaceAll('<','&lt;').replaceAll('>','&gt;');
'''
    check = r'''
aqContexts=[{value:'general',label:'General'},{value:'Sandesh <legacy>',label:'Sandesh <legacy> (existing)'}];
aqRecords=[{qa_id:'admin_legacy',question:'Question',answer:'Answer',context:'Sandesh <legacy>'}];
aqEdit('admin_legacy');
if(document.getElementById('aqContext').value!=='Sandesh <legacy>')throw Error('legacy changed');
if(!document.getElementById('aqDialogContent').innerHTML.includes('<select id="aqContext"'))throw Error('not dropdown');
if(document.getElementById('aqDialogContent').innerHTML.includes('<input id="aqContext"'))throw Error('free typing');
if(document.getElementById('aqContext').options[1].text!=='Sandesh <legacy> (existing)')throw Error('unsafe option');
aqEdit();if(document.getElementById('aqContext').value!=='general')throw Error('new default');
'''
    result = subprocess.run([node, '-e', harness + script + check], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
