"""Replacement facts and flyer become authoritative together at midnight IST."""
import io
import json
import shutil
import subprocess
from pathlib import Path
from datetime import date, datetime, timezone

import pytest
from PIL import Image

from camellia import admin_menu_review, menu_dates, menu_images, menu_review, weekly_savoury
from camellia.admin_weekly_savoury import WEEKLY_HTML, WEEKLY_SCRIPT
from camellia.menu_dates import today_ist as real_today_ist
from camellia.config import config
from camellia.database import db
from test_qa_menu1 import BROWSE, respond
from test_weekly_savoury_lifecycle import weekly_env, _publish


@pytest.fixture
def replacement_chain(weekly_env, monkeypatch):
    class Clock(datetime):
        instant = datetime(2026, 10, 1, 18, 29, 59, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)

    monkeypatch.setattr(menu_dates, 'datetime', Clock)
    # Other legacy suites mock the entry point; explicitly own both clock hooks.
    monkeypatch.setattr(menu_dates, 'today_ist', real_today_ist)
    monkeypatch.setattr(config, 'BYPASS_AD_GATE', True)
    price = [100]
    monkeypatch.setattr(menu_review.menu_parser, 'parse_image_and_update_menu', lambda *_: {
        'status': 'success', 'week_of': '2 Oct - 8 Oct 2026', 'parsed': {
            'vegetarian': [{'item': 'Dhokar Dalna', 'quantity': '250g', 'price_inr': price[0], 'description': ''}]}})
    versions = []
    for color, value in [('red', 100), ('blue', 120), ('green', 140)]:
        price[0] = value
        buffer = io.BytesIO()
        Image.new('RGB', (120, 160), color).save(buffer, format='PNG')
        draft = menu_review.extract(weekly_savoury.WEEKLY_KEY, 'admin', data=buffer.getvalue(), mime='image/png', weekly=True)
        options = {'replace_version_id': versions[-1]['id']} if versions else {}
        published = _publish(draft, **options)
        versions.append(db.get_menu_version(published['version_id']))
    return Clock, [db.get_menu_version(v['id']) for v in versions]


def test_a_b_c_replacement_and_exact_midnight_without_restart(replacement_chain):
    clock, versions = replacement_chain
    assert [weekly_savoury.version_status(v) for v in versions] == ['SUPERSEDED', 'SUPERSEDED', 'UPCOMING']
    assert menu_dates.today_ist() == date(2026, 10, 1)
    before = admin_menu_review._weekly_view()
    assert before['current'] is None
    assert [v['version_id'] for v in before['upcoming']] == [versions[-1]['id']]
    assert weekly_savoury.current_version() is None
    assert menu_images.current_weekly_url() is None
    for query in [*BROWSE, 'Dhokar Dalna price?', 'what is the price of dhokar dolna']:
        answer = respond(query)
        assert all(f'₹{price}' not in answer[0] for price in [100, 120, 140])
        assert not answer[3]

    clock.instant = datetime(2026, 10, 1, 18, 30, tzinfo=timezone.utc)
    assert menu_dates.today_ist() == date(2026, 10, 2)
    latest = versions[-1]
    flyer = json.loads(latest['approval_json'])['image_path']
    assert weekly_savoury.current_version()['id'] == latest['id']
    after = admin_menu_review._weekly_view()
    assert after['current']['version_id'] == latest['id']
    assert after['current']['image_url'] == flyer
    assert after['upcoming'] == []
    for query in BROWSE:
        answer = respond(query)
        assert '₹140' in answer[0] and '₹100' not in answer[0] and '₹120' not in answer[0]
        assert answer[3] == [flyer]
    for query in ['Dhokar Dalna price?', 'what is the price of dhokar dolna']:
        assert respond(query)[0] == 'Dhokar Dalna 250g is ₹140.'
    assert menu_images.current_weekly_url() == flyer
    assert len(after['versions']) == 3  # Internal history survives UI removal.
    for version in versions:
        assert db.get_menu_version(version['id'])['approval_json'] == version['approval_json']
        assert menu_review.version_source_file(weekly_savoury.WEEKLY_KEY, version['id']).is_file()
        approval = json.loads(version['approval_json'])
        assert (Path(menu_images._dir()) / Path(approval['image_path']).name).is_file()
    clock.instant = datetime(2026, 10, 8, 18, 30, tzinfo=timezone.utc)
    assert weekly_savoury.current_version() is None
    assert menu_images.current_weekly_url() is None
    assert '₹140' not in respond('Dhokar Dolna price?')[0]


def test_ambiguous_approved_versions_fail_closed(replacement_chain):
    clock, versions = replacement_chain
    with db.get_connection() as conn:
        conn.execute('UPDATE menu_versions SET weekly_state=NULL WHERE id=?', (versions[-2]['id'],))
        conn.commit()
    before = admin_menu_review._weekly_view()
    assert before['upcoming_ambiguous'] is True
    assert before['upcoming'] == []
    clock.instant = datetime(2026, 10, 1, 18, 30, tzinfo=timezone.utc)
    assert weekly_savoury.current_version() is None
    assert admin_menu_review._weekly_view()['current'] is None
    assert menu_review.customer_versions() == []
    assert menu_images.current_weekly_url() is None
    assert '₹' not in respond('Dhokar Dolna price?')[0]


def test_normal_weekly_workspace_excludes_history_and_restore():
    for value in ['data-ws-pane="history"', 'Previous weekly menus', 'wsTimelineHistory', 'wsHistory', 'wsVersions']:
        assert value not in WEEKLY_HTML + WEEKLY_SCRIPT
    assert 'wsRestore' not in WEEKLY_SCRIPT
    assert 'Restore previous' not in WEEKLY_HTML + WEEKLY_SCRIPT


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js needed for Admin UI harness')
def test_open_panel_refreshes_at_midnight_without_discarding_draft():
    source = WEEKLY_SCRIPT.split('<script>', 1)[1].split('</script>', 1)[0]
    scenario = r'''
const vm=require('vm'),elements={};let timer,delay,preview;
function node(){return {children:[],style:{},hidden:false,textContent:'',classList:{add(){},remove(){}},
addEventListener(){},replaceChildren(){this.children=[];this.textContent='';},appendChild(n){this.children.push(n);}};}
global.document={hidden:false,addEventListener(){},getElementById(id){return elements[id]||(elements[id]=node());},
createElement:node,querySelectorAll(){return[];}};
global.setTimeout=(fn,ms)=>{timer=fn;delay=ms;return 1;};global.clearTimeout=()=>{};
let now=Date.parse('2026-10-01T23:59:59+05:30');Date.now=()=>now;
vm.runInThisContext(SOURCE);
const latest={version_id:3,status:'UPCOMING',valid_from:'2026-10-02',display_date_range:'2 Oct - 8 Oct',item_count:1,image_url:'/latest-flyer'};
let data={today:'2026-10-01',current:null,upcoming:[latest],versions:[{version_id:1},{version_id:2},latest]};
mrRequest=async()=>data;mrPreviewMenuImage=v=>{preview=v.image_url;};
(async()=>{wsDraft={draft_id:42};wsDirty=true;await wsLoad(true);
const scheduled={count:elements.wsUpcomingVersions.children.length,delay};
elements.wsUpcomingVersions.children[0].children.find(n=>n.textContent==='Preview flyer').onclick();
now=Date.parse('2026-10-02T00:00:00+05:30');data={...data,today:'2026-10-02',current:{...latest,status:'CURRENT'},upcoming:[]};
await timer();await Promise.resolve();
console.log(JSON.stringify({scheduled,preview,current:elements.wsState.children[0].children[0].textContent,
upcoming:elements.wsUpcomingVersions.textContent,draft:wsDraft.draft_id,dirty:wsDirty}));})();
'''
    done = subprocess.run([shutil.which('node')], input='const SOURCE=' + json.dumps(source) + ';\n' + scenario,
                          text=True, encoding='utf-8', capture_output=True, timeout=20)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result['scheduled'] == {'count': 1, 'delay': 1000}
    assert result['preview'] == '/latest-flyer'
    assert result['current'].startswith('LIVE / CURRENT')
    assert result['upcoming'] == 'No scheduled menus.'
    assert result['draft'] == 42 and result['dirty'] is True
