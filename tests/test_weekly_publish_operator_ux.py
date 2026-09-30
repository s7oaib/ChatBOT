"""Weekly publication and final operator controls stay explicit and actionable."""

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


def test_thirteen_row_bento_draft_is_valid_weekly_savoury_and_can_publish(isolated_weekly):
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
    assert not any("Bengali Bento belongs" in error for error in analysis["errors"])

    client = TestClient(app)
    headers = {"X-Admin-Key": config.ADMIN_API_KEY}
    preview = client.post("/admin/weekly-savoury/preview", json=options, headers=headers)
    publish = client.post("/admin/weekly-savoury/publish", json=options, headers=headers)
    assert preview.status_code == 200
    assert publish.status_code == 200
    current = weekly_savoury.current_version()
    assert current["id"] != version_before
    approval = json.loads(current["approval_json"])
    assert sum(row["category"] == "bengali_bento" for row in approval["rows"]) == 4
    assert json.loads(current["snapshot_json"]) == approval["rows"]


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
let analysis={errors:['Row 4: Missing or invalid price'],comparison_available:true,
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
  await wsPublish();publishError='Review the printed menu dates.';
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
    assert result["invalid"] == {"dialog": False, "reason": "Needs attention before publishing|Row 4: Missing or invalid price", "focus": 1, "publishRequests": 0}
    assert "Missing or invalid price" in result["previewBlocked"]["reason"]
    assert result["previewBlocked"]["requests"] == 0
    assert "Review the printed menu dates." in result["previewServer"]["reason"]
    assert result["previewServer"]["requests"] == 1
    assert result["opened"] == {"open": True, "title": "Update Weekly Savoury Menu", "summary": "13 items · 25 Sep - 30 Sep 2026", "button": "Update Menu", "publishRequests": 0}
    assert result["cancelled"] == {"open": False, "publishRequests": 0, "focus": 1}
    assert result["failed"]["open"] is True and result["failed"]["enabled"] is True
    assert "Review the printed menu dates." in result["failed"]["reason"]
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
    assert 'A menu without a topic is not linked to customer enquiries.' not in REVIEW_HTML
    assert 'onclick="mrManageCategories()">Manage</button>' in REVIEW_HTML
    assert 'onclick="mrManageTopics()">Manage</button>' in REVIEW_HTML
    assert 'id="mrTopicDialog"' in REVIEW_HTML
    assert 'id="mrMenuPositionHelp"' in REVIEW_HTML
    assert 'id="mrMenuFile" class="mr-visually-hidden" type="file"' in REVIEW_HTML
    assert 'JPG, PNG, WEBP or PDF · Maximum 10 MB' in REVIEW_HTML
    assert "position.disabled=!topic" in REVIEW_SCRIPT
    assert "['First','Middle','Last']" in REVIEW_SCRIPT
    assert "option.textContent='Before '" not in REVIEW_SCRIPT


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
element('mrMenuTopic').value='WEEKLY_SAVOURY';mrRefreshPosition();
const linked={disabled:element('mrMenuPosition').disabled,help:element('mrMenuPositionHelp').hidden,
  options:element('mrMenuPosition').children.map(option=>option.textContent),selected:element('mrMenuPosition').value};
mrAllMenus=[{menu_id:'weekly_vegetarian',name:'Weekly Vegetarian',customer_topic:'WEEKLY_SAVOURY',display_order:10},
  {menu_id:'party',name:'Party Menu',customer_topic:'WEEKLY_SAVOURY',display_order:20},
  {menu_id:'event',name:'Event Menu',customer_topic:'WEEKLY_SAVOURY',display_order:30}];
mrRefreshPosition();element('mrMenuPosition').value='middle';
const ordered={options:element('mrMenuPosition').children.map(option=>option.textContent),position:mrPositionNumber()};
const mappings=[];for(let n=0;n<=4;n++){mrAllMenus=Array.from({length:n},(_,i)=>({menu_id:'m'+i,customer_topic:'WEEKLY_SAVOURY',display_order:(i+1)*10}));const values=[];for(const choice of ['first','middle','last']){element('mrMenuPosition').value=choice;values.push(mrPositionNumber());}mappings.push(values);}
mrAllMenus=Array.from({length:3},(_,i)=>({menu_id:'m'+i,customer_topic:'WEEKLY_SAVOURY',display_order:(i+1)*10}));
const preselection=[];for(const id of ['m0','m1','m2']){mrEditingId=id;mrRefreshPosition();preselection.push(element('mrMenuPosition').value);}
mrAllMenus=[mrAllMenus[0]];mrEditingId='m0';mrRefreshPosition();const single=element('mrMenuPosition').value;
mrAllMenus.push({menu_id:'new1',customer_topic:'SONDESH'},{menu_id:'new2',customer_topic:'SONDESH'});element('mrMenuTopic').value='SONDESH';mrRefreshPosition();element('mrMenuPosition').value='last';const newTopic=mrPositionNumber();
element('mrMenuTopic').value='';mrRefreshPosition();const noTopic=mrPositionNumber();
element('mrMenuFile').files=[{name:'menu.png',type:'image/png',size:2048}];mrMenuFileSelected();
const chosen={name:element('mrMenuFileStatus').textContent,button:element('mrMenuFileChoose').textContent,
  removeVisible:!element('mrMenuFileRemove').hidden};mrClearMenuFile();
const removed={status:element('mrMenuFileStatus').textContent,button:element('mrMenuFileChoose').textContent,
  removeHidden:element('mrMenuFileRemove').hidden};
console.log(JSON.stringify({unlinked,linked,ordered,mappings,preselection,single,newTopic,noTopic,chosen,removed}));
"""
    done = subprocess.run([shutil.which("node")], input="const SOURCE=" + json.dumps(source) + ";\n" + scenario,
                          text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip())
    assert result["unlinked"] == {"disabled": True,
        "label": "Choose a customer message topic to set reply order.", "help": True}
    assert result["linked"] == {"disabled": False, "help": True,
        "options": ["First", "Middle", "Last"], "selected": "first"}
    assert result["ordered"] == {"options": ["First", "Middle", "Last"], "position": 2}
    assert result["mappings"] == [[1, 1, 1], [1, 1, 2], [1, 2, 3], [1, 2, 4], [1, 3, 5]]
    assert result["preselection"] == ["first", "middle", "last"]
    assert result["single"] == "first"
    assert result["newTopic"] == 3
    assert result["noTopic"] is None
    assert result["chosen"] == {"name": "menu.png (2.0 KB)", "button": "Change", "removeVisible": True}
    assert result["removed"] == {"status": "No file selected.", "button": "Upload Menu File", "removeHidden": True}
