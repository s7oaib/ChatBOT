"""The client-facing Menu UI uses the existing catalogue and approval APIs."""

import json
import shutil
import subprocess

import pytest

from camellia.admin_menu_review import REVIEW_HTML, REVIEW_SCRIPT


def test_menu_catalogue_and_selected_menu_are_the_only_primary_views():
    assert '<div id="mrCatalogueView">' in REVIEW_HTML
    assert '<div id="mrSelectedView" hidden>' in REVIEW_HTML
    assert 'Menu Catalogue' in REVIEW_HTML
    assert '+ Add Menu' in REVIEW_HTML
    assert 'Manage Menu' in REVIEW_SCRIPT
    for label in ('data-mr-pane="current"', 'data-mr-pane="upload"', 'data-mr-pane="versions"'):
        assert label in REVIEW_HTML
    assert 'Continue to Review' in REVIEW_HTML
    assert 'Approve &amp; Publish' in REVIEW_HTML
    assert '<dialog id="mrMenuDialog"' in REVIEW_HTML
    assert 'id="mrMenuCategory"' in REVIEW_HTML
    assert 'id="mrCurrentRows"' in REVIEW_HTML
    assert 'id="mrHistory"' in REVIEW_HTML
    assert 'id="mrEmpty"' in REVIEW_HTML
    assert 'mr-mobile-actions' in REVIEW_HTML
    assert 'Scan Flyer & Update Menu' not in REVIEW_HTML
    assert "Upload this week's menu" not in REVIEW_HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for the Admin JavaScript test")
def test_catalogue_crud_current_approval_and_input_modes_without_refresh():
    source = REVIEW_SCRIPT.split("<script>", 1)[1].split("</script>", 1)[0]
    scenario = r"""
const vm = require('vm');
const elements = {};
function node(tag='div') {
  return {
    tag, value:'', textContent:'', hidden:false, disabled:false, checked:false, open:false,
    children:[], dataset:{}, style:{}, files:[],
    replaceChildren(){this.children=[];this.value='';},
    appendChild(child){this.children.push(child);if(this.tag==='select'&&!this.value)this.value=child.value;},
    setAttribute(name,value){this[name]=value;},addEventListener(){},
    showModal(){this.open=true;},close(){this.open=false;},focus(){},reportValidity(){return true;},
  };
}
function element(id){if(!elements[id])elements[id]=node(id==='mrMenu'||id==='mrMenuCategory'?'select':'div');return elements[id];}
global.document={getElementById:element,createElement:node,querySelectorAll:()=>[]};
global.getAuthHeaders=()=>({'x-admin-key':'test'});
global.FormData=class {constructor(){this.fields=[];}append(key,value){this.fields.push([key,value]);}entries(){return this.fields[Symbol.iterator]();}};
let menus=[
  {menu_id:'m1',name:'Aamar Mishti Collection',category:'Sweets',description:'Classic sweets',active:1},
  {menu_id:'m2',name:'Weekly Menu',category:'Other',description:'Weekly menu',active:1},
];
let blocked=false,requests=[],confirmations=[];
global.confirm=message=>{confirmations.push(message);return confirmations.length!==1;};
global.fetch=async(path,options={})=>{
  const method=options.method||'GET';requests.push([method,path]);let status=200,data={};
  if(path==='/admin/menu-catalogue'&&method==='GET')data={menus,categories:['Sweets','Other']};
  else if(path.endsWith('/versions')&&method==='GET')data={versions:path.includes('/m1/')?[{id:7,is_current:1,changed_at:'2026-09-26',approval_json:JSON.stringify({rows:[{item:'Kanchagolla',quantity:'250g',price_inr:650}]})}]:[]};
  else if(path.endsWith('/review')&&method==='GET')data={draft:null,categories:['Dish']};
  else if(path==='/admin/menu-catalogue'&&method==='POST'){
    const fields=Object.fromEntries(options.body.entries());
    const menu={menu_id:'m3',name:fields.name,category:fields.category,description:fields.description,active:fields.active==='true'?1:0};
    menus.push(menu);data={menu,published:false};
  }else if(method==='PATCH'){
    const id=path.split('/').pop(),menu=menus.find(m=>m.menu_id===id);
    Object.assign(menu,JSON.parse(options.body));data={menu};
  }else if(method==='DELETE'){
    if(blocked){status=409;data={detail:{message:'internal routing id 42'}};}
    else{menus=menus.filter(m=>m.menu_id!==path.split('/')[3]);data={status:'deleted'};}
  }else{status=404;data={detail:'Unexpected request'};}
  return{ok:status===200,status,json:async()=>data};
};
vm.runInThisContext(SOURCE);
(async()=>{
  await mrMenus();
  const initial={catalogue:!element('mrCatalogueView').hidden,selected:element('mrSelectedView').hidden,names:element('mrCatalogueList').children.map(card=>card.children[0].textContent)};
  await mrOpenMenu('m1');
  const current={selected:element('mrSelectedName').textContent,rows:element('mrCurrentRows').children.map(row=>row.children.map(cell=>cell.textContent)),empty:element('mrCurrentEmpty').hidden};
  mrInputMode('text');const textMode={text:!element('mrTextPane').hidden,file:element('mrFilePane').hidden};
  mrInputMode('file');const fileMode={file:!element('mrFilePane').hidden,text:element('mrTextPane').hidden};
  mrShowCatalogue();mrOpenMenuForm('add');
  const formCategories=element('mrMenuCategory').children.map(option=>option.textContent);
  element('mrMenuName').value='Durga Puja Specials';element('mrMenuCategory').value='Sweets';element('mrMenuDescription').value='Festive Bengali sweets';
  await mrSaveMenu({preventDefault(){}});
  const created={selected:element('mrMenu').value,name:element('mrSelectedName').textContent,upload:!element('mrUploadPane').hidden,open:element('mrMenuDialog').open};
  mrOpenMenuForm('edit','m3');
  const prefilling={name:element('mrMenuName').value,description:element('mrMenuDescription').value};
  element('mrMenuName').value='Renamed Specials';element('mrMenuDescription').value='Updated description';element('mrMenuActive').checked=false;
  await mrSaveMenu({preventDefault(){}});
  const edited={selected:element('mrMenu').value,name:element('mrSelectedName').textContent,menu:menus.find(m=>m.menu_id==='m3')};
  const beforeCancel=requests.filter(request=>request[0]==='DELETE').length;
  await mrDeleteMenu('m3');
  const afterCancel=requests.filter(request=>request[0]==='DELETE').length;
  blocked=true;await mrDeleteMenu('m3');
  const blockedState={message:element('mrMessage').textContent,stillExists:menus.some(m=>m.menu_id==='m3')};
  blocked=false;await mrDeleteMenu('m3');
  const deleted={catalogue:!element('mrCatalogueView').hidden,names:element('mrCatalogueList').children.map(card=>card.children[0].textContent)};
  menus=[];await mrMenus();const empty={shown:!element('mrEmpty').hidden,catalogue:!element('mrCatalogueView').hidden};
  console.log(JSON.stringify({initial,current,textMode,fileMode,formCategories,created,prefilling,edited,beforeCancel,afterCancel,blockedState,deleted,empty,confirmations,requests}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    completed = subprocess.run(
        [shutil.which("node")], input="const SOURCE=" + json.dumps(source) + ";\n" + scenario,
        text=True, encoding="utf-8", capture_output=True, timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())
    assert result["initial"] == {"catalogue": True, "selected": True, "names": ["Aamar Mishti Collection", "Weekly Menu"]}
    assert result["current"] == {"selected": "Aamar Mishti Collection", "rows": [["Kanchagolla", "250g", "₹650"]], "empty": True}
    assert result["textMode"] == {"text": True, "file": True}
    assert result["fileMode"] == {"file": True, "text": True}
    assert result["formCategories"] == ["Sweets", "Other"]
    assert result["created"] == {"selected": "m3", "name": "Durga Puja Specials", "upload": True, "open": False}
    assert result["prefilling"] == {"name": "Durga Puja Specials", "description": "Festive Bengali sweets"}
    assert result["edited"]["selected"] == "m3"
    assert result["edited"]["name"] == "Renamed Specials"
    assert result["edited"]["menu"]["description"] == "Updated description"
    assert result["edited"]["menu"]["active"] is False
    assert result["beforeCancel"] == result["afterCancel"]
    assert result["blockedState"] == {"message": "This menu is currently being used. Remove or change the connected Instagram source first, then try again.", "stillExists": True}
    assert result["deleted"] == {"catalogue": True, "names": ["Aamar Mishti Collection", "Weekly Menu"]}
    assert result["empty"] == {"shown": True, "catalogue": True}
    assert len(result["confirmations"]) == 3
    assert all(request[1].endswith("/m3/permanent") for request in result["requests"] if request[0] == "DELETE")
