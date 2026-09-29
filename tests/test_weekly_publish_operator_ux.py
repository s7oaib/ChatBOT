"""Weekly publication explains invalid drafts before any approval request."""

import copy
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from camellia import menu_dates, menu_review, weekly_savoury
from camellia.admin_menu_review import REVIEW_HTML, REVIEW_SCRIPT
from camellia.admin_weekly_savoury import WEEKLY_HTML, WEEKLY_SCRIPT
from camellia.app import app
from camellia.config import config
from camellia.database import db


@pytest.fixture
def isolated_weekly(tmp_path, monkeypatch):
    menu = tmp_path / "menu.json"
    shutil.copy(config.MENU_JSON_PATH, menu)
    monkeypatch.setattr(config, "MENU_JSON_PATH", str(menu))
    monkeypatch.setattr(db, "db_path", str(tmp_path / "weekly.db"))
    monkeypatch.setattr(menu_dates, "today_ist", lambda: menu_dates.date(2026, 9, 27))
    db.init_db()
    return menu


def test_thirteen_row_bento_draft_is_rejected_without_changing_published_menu(isolated_weekly):
    first = menu_review.extract(weekly_savoury.WEEKLY_KEY, "admin",
        text="25 Sep - 30 Sep 2026\nVeg:\nApproved fixture dish 250g ₹100", weekly=True)
    published = menu_review.publish_approved_menu(weekly_savoury.WEEKLY_KEY,
        first["draft_id"], first["revision"], "admin", weekly_options={})
    version_before = weekly_savoury.current_version()["id"]
    assert version_before == published["version_id"]
    draft = menu_review.extract(weekly_savoury.WEEKLY_KEY, "admin",
        text="25 Sep - 30 Sep 2026\nVeg:\nFixture dish 250g ₹100", weekly=True)
    rows = []
    for index in range(13):
        row = copy.deepcopy(draft["rows"][0])
        row.pop("row_id", None)
        row.update(item=f"Fixture item {index + 1}", quantity="250g", price_inr=100 + index,
                   category="bengali_bento" if index >= 9 else "vegetarian", aliases=[])
        rows.append(row)
    draft = menu_review.edit(weekly_savoury.WEEKLY_KEY, draft["draft_id"],
                             draft["revision"], rows, "admin")
    options = {"draft_id": draft["draft_id"], "revision": draft["revision"],
               "valid_from": "2026-09-25", "valid_until": "2026-09-30",
               "replace_version_id": version_before}
    analysis = menu_review.weekly_analysis(draft, options)
    assert len(analysis["rows"]) == 13
    assert sum(row["category"] == "bengali_bento" for row in analysis["rows"]) == 4
    assert "Bengali Bento belongs in Menu Catalogue, not Weekly Savoury." in analysis["errors"]
    client = TestClient(app)
    headers = {"X-Admin-Key": config.ADMIN_API_KEY}
    preview = client.post("/admin/weekly-savoury/preview", json=options, headers=headers)
    publish = client.post("/admin/weekly-savoury/publish", json=options, headers=headers)
    assert preview.status_code == 400
    assert "Bengali Bento belongs in Menu Catalogue" in preview.json()["detail"]
    assert publish.status_code == 400
    assert "Bengali Bento belongs in Menu Catalogue" in publish.json()["detail"]
    assert weekly_savoury.current_version()["id"] == version_before
    assert [version["id"] for version in weekly_savoury.lifecycle_versions()] == [version_before]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for Admin JavaScript tests")
def test_weekly_publish_flow_blocks_invalid_draft_and_uses_safe_modal():
    source = WEEKLY_SCRIPT.split("<script>", 1)[1].split("</script>", 1)[0]
    scenario = r"""
const vm=require('vm');const elements={};let requests=[],render=0,loads=0,panes=[],nativeConfirms=0;
function node(tag='div') {return {tag,value:'',textContent:'',hidden:false,disabled:false,checked:false,open:false,
  children:[],listeners:{},isConnected:true,focusCount:0,scrollCount:0,
  replaceChildren(...parts){this.children=parts;},appendChild(part){this.children.push(part);},
  addEventListener(name,fn){this.listeners[name]=fn;},focus(){this.focusCount++;},
  scrollIntoView(){this.scrollCount++;},showModal(){this.open=true;},
  close(){this.open=false;this.listeners.close?.();}};}
function element(id){if(!elements[id])elements[id]=node();return elements[id];}
global.document={getElementById:element,createElement:node,addEventListener(){},querySelectorAll(){return[];}};
global.confirm=()=>{nativeConfirms++;throw Error('native confirm used');};
vm.runInThisContext(SOURCE);
wsDraft={draft_id:7,revision:3,rows:Array.from({length:13},(_,i)=>({item:'Fixture '+i})),warnings:[]};
wsToday='2026-09-27';element('wsFrom').value='2026-09-25';element('wsUntil').value='2026-09-30';
element('wsQuestion').value='What is on the weekly menu?';
wsRenderAnalysis=()=>{render++;};wsLoad=async()=>{loads++;};wsShowTimeline=pane=>{panes.push(pane);};
let analysis={errors:['Bengali Bento belongs in Menu Catalogue, not Weekly Savoury.'],comparison_available:true,
  overlaps:[],rows:wsDraft.rows,valid_from:'2026-09-25',display_date_range:'25 Sep - 30 Sep 2026'};
let publishError='',previewError='',pending=null;
mrRequest=async(path)=>{requests.push(path);if(path.endsWith('/analyse'))return{analysis};
  if(path.endsWith('/preview')){if(previewError)throw Error(previewError);return{label:'Preview only',answer:'Fixture answer'};}
  if(path.endsWith('/publish')){if(publishError)throw Error(publishError);if(pending)return await pending;return {};}
  throw Error('unexpected path');};
function box(id){return element(id).children.map(n=>n.textContent||n.children.map(x=>x.textContent).join('|')).join('|');}
(async()=>{
  await wsPublish();const invalid={dialog:element('wsPublishDialog').open,
    reason:box('wsPublishError'),focus:element('wsPublishError').focusCount,
    publishRequests:requests.filter(p=>p.endsWith('/publish')).length};
  await wsPreview();const previewBlocked={reason:box('wsPreviewError'),requests:requests.filter(p=>p.endsWith('/preview')).length};
  analysis={...analysis,errors:[]};previewError='Review the printed menu dates.';await wsPreview();
  const previewServer={reason:box('wsPreviewError'),requests:requests.filter(p=>p.endsWith('/preview')).length};
  await wsPublish();const opened={open:element('wsPublishDialog').open,
    title:element('wsPublishDialogTitle').textContent,summary:element('wsPublishSummary').textContent,
    button:element('wsConfirmPublishButton').textContent,publishRequests:requests.filter(p=>p.endsWith('/publish')).length};
  wsCancelPublish();const cancelled={open:element('wsPublishDialog').open,
    publishRequests:requests.filter(p=>p.endsWith('/publish')).length,focus:element('wsPublishButton').focusCount};
  await wsPublish();publishError='Bengali Bento belongs in Menu Catalogue, not Weekly Savoury.';
  await wsConfirmPublish();const failed={open:element('wsPublishDialog').open,reason:box('wsModalError'),
    nearButton:box('wsPublishError'),enabled:!element('wsConfirmPublishButton').disabled};
  publishError='';wsCancelPublish();await wsPublish();
  pending=new Promise(resolve=>{global.finishPublish=resolve;});
  const one=wsConfirmPublish(),two=wsConfirmPublish();
  const whilePending={disabled:element('wsConfirmPublishButton').disabled,
    publishRequests:requests.filter(p=>p.endsWith('/publish')).length};
  finishPublish({});await Promise.all([one,two]);
  const success={open:element('wsPublishDialog').open,loads,panes,banner:element('wsSuccess').textContent,
    publishRequests:requests.filter(p=>p.endsWith('/publish')).length};
  analysis={...analysis,valid_from:'2026-10-01',display_date_range:'1 Oct - 7 Oct 2026'};
  await wsPublish();const future={title:element('wsPublishDialogTitle').textContent,button:element('wsConfirmPublishButton').textContent};
  console.log(JSON.stringify({invalid,previewBlocked,previewServer,opened,cancelled,failed,whilePending,success,future,nativeConfirms}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    done = subprocess.run([shutil.which("node")], input="const SOURCE=" + json.dumps(source) + ";\n" + scenario,
                          text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip())
    assert result["invalid"] == {"dialog": False, "reason": "Needs attention before publishing|Bengali Bento belongs in Menu Catalogue, not Weekly Savoury.", "focus": 1, "publishRequests": 0}
    assert "Bengali Bento belongs" in result["previewBlocked"]["reason"]
    assert result["previewBlocked"]["requests"] == 0
    assert "Review the printed menu dates." in result["previewServer"]["reason"]
    assert result["previewServer"]["requests"] == 1
    assert result["opened"] == {"open": True, "title": "Update Weekly Savoury Menu", "summary": "13 items · 25 Sep - 30 Sep 2026", "button": "Update Menu", "publishRequests": 0}
    assert result["cancelled"] == {"open": False, "publishRequests": 0, "focus": 1}
    assert result["failed"]["open"] is True and result["failed"]["enabled"] is True
    assert "Bengali Bento belongs" in result["failed"]["reason"]
    assert "currently published menu has not changed" in result["failed"]["nearButton"]
    assert result["whilePending"] == {"disabled": True, "publishRequests": 2}
    assert result["success"] == {"open": False, "loads": 1, "panes": ["current"],
                                 "banner": "Weekly Savoury menu updated", "publishRequests": 2}
    assert result["future"] == {"title": "Schedule Weekly Savoury Menu", "button": "Schedule Menu"}
    assert result["nativeConfirms"] == 0


def test_menu_editor_and_publish_modal_accessibility_contract():
    assert '<dialog id="wsPublishDialog"' in WEEKLY_HTML
    assert 'aria-labelledby="wsPublishDialogTitle"' in WEEKLY_HTML
    assert 'onclick="wsCancelPublish()"' in WEEKLY_HTML
    assert 'width:min(480px,calc(100vw - 28px))' in WEEKLY_HTML
    assert '<p id="mrMenuTopicHelp" class="mr-menu-form-help mr-topic-help">' in REVIEW_HTML
    assert 'id="mrMenuPositionHelp"' in REVIEW_HTML
    assert 'id="mrMenuFile" class="mr-visually-hidden" type="file"' in REVIEW_HTML
    assert 'JPG, PNG, WEBP or PDF · Maximum 10 MB' in REVIEW_HTML
    assert "position.disabled=!topic" in REVIEW_SCRIPT


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for Admin JavaScript tests")
def test_menu_editor_position_and_upload_controls_follow_topic_and_file_selection():
    source = REVIEW_SCRIPT.split("<script>", 1)[1].split("</script>", 1)[0]
    scenario = r"""
const vm=require('vm'),elements={};
function node(){return{value:'',textContent:'',hidden:false,disabled:false,children:[],files:[],
  replaceChildren(){this.children=[];},appendChild(child){this.children.push(child);if(!this.value)this.value=child.value;},
  addEventListener(){}};}
function element(id){return elements[id]??(elements[id]=node());}
global.document={getElementById:element,createElement:node,querySelectorAll:()=>[],addEventListener(){}};
vm.runInThisContext(SOURCE);
mrAllMenus=[];mrEditingId=null;
mrRefreshPosition();const unlinked={disabled:element('mrMenuPosition').disabled,
  label:element('mrMenuPosition').children[0].textContent,help:!element('mrMenuPositionHelp').hidden};
element('mrMenuTopic').value='weekly_savoury';mrRefreshPosition();
const linked={disabled:element('mrMenuPosition').disabled,help:element('mrMenuPositionHelp').hidden,
  option:element('mrMenuPosition').children[0].textContent};
element('mrMenuFile').files=[{name:'menu.png',type:'image/png',size:2048}];mrMenuFileSelected();
const chosen={name:element('mrMenuFileStatus').textContent,button:element('mrMenuFileChoose').textContent,
  removeVisible:!element('mrMenuFileRemove').hidden};mrClearMenuFile();
const removed={status:element('mrMenuFileStatus').textContent,button:element('mrMenuFileChoose').textContent,
  removeHidden:element('mrMenuFileRemove').hidden};
console.log(JSON.stringify({unlinked,linked,chosen,removed}));
"""
    done = subprocess.run([shutil.which("node")], input="const SOURCE=" + json.dumps(source) + ";\n" + scenario,
                          text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip())
    assert result["unlinked"] == {"disabled": True,
        "label": "Choose a customer message topic to set reply order.", "help": True}
    assert result["linked"] == {"disabled": False, "help": True, "option": "Last"}
    assert result["chosen"] == {"name": "menu.png (2.0 KB)", "button": "Change", "removeVisible": True}
    assert result["removed"] == {"status": "No file selected.", "button": "Upload Menu File", "removeHidden": True}
