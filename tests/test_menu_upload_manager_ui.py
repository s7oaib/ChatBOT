"""The simplified upload screen manages menu destinations without a refresh."""

import json
import shutil
import subprocess

import pytest

from camellia.admin_menu_review import REVIEW_HTML, REVIEW_SCRIPT


def test_menu_manager_controls_are_outside_the_selector():
    assert '<select id="mrMenu"' in REVIEW_HTML
    assert '<details id="mrMenuActions">' in REVIEW_HTML
    assert 'aria-label="Menu actions"' in REVIEW_HTML
    assert '+ Add new menu' in REVIEW_HTML
    assert '<dialog id="mrMenuDialog"' in REVIEW_HTML
    assert 'id="mrMenuCategory"' in REVIEW_HTML
    assert 'id="mrDestination"' in REVIEW_HTML
    assert 'id="mrEmpty"' in REVIEW_HTML
    assert 'Load menus / refresh' not in REVIEW_HTML
    assert 'max-width:600px' in REVIEW_HTML and 'mr-mobile-actions' in REVIEW_HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for the Admin JavaScript test")
def test_menu_manager_add_edit_delete_and_empty_state_without_page_refresh():
    source = REVIEW_SCRIPT.split("<script>", 1)[1].split("</script>", 1)[0]
    scenario = r"""
const vm = require('vm');
const elements = {};
function element(id) {
  if (!elements[id]) elements[id] = {
    value: '', textContent: '', hidden: false, disabled: false, checked: false, open: false,
    options: [], replaceChildren() { this.options = []; this.value = ''; },
    appendChild(node) { this.options.push(node); if (!this.value) this.value = node.value; },
    showModal() { this.open = true; }, close() { this.open = false; },
    focus() {}, reportValidity() { return true; },
  };
  return elements[id];
}
global.document = {getElementById: element, createElement: () => ({value:'', textContent:''})};
global.getAuthHeaders = () => ({'x-admin-key': 'test'});
let menus = [
  {menu_id:'m1', name:'Aamar Mishti Collection', category:'Sweets', description:'Classic sweets', active:1},
  {menu_id:'m2', name:'Weekly Menu', category:'Other', description:'Weekly menu', active:1},
];
let blocked = false, requests = [], confirmations = [];
global.confirm = (message) => { confirmations.push(message); return confirmations.length !== 1; };
global.fetch = async (path, options={}) => {
  const method = options.method || 'GET'; requests.push([method, path]);
  let status=200, data={};
  if (path === '/admin/menu-catalogue' && method === 'GET') data={menus, categories:['Sweets','Other']};
  else if (path.includes('/review') && method === 'GET') data={draft:null,categories:['Dish']};
  else if (path === '/admin/menu-catalogue' && method === 'POST') {
    const fields=Object.fromEntries(options.body.entries());
    const menu={menu_id:'m3',name:fields.name,category:fields.category,description:fields.description,active:fields.active==='true'?1:0};
    menus.push(menu); data={menu,published:false};
  } else if (method === 'PATCH') {
    const id=path.split('/').pop(), menu=menus.find(m=>m.menu_id===id);
    Object.assign(menu,JSON.parse(options.body)); data={menu};
  } else if (method === 'DELETE') {
    if (blocked) {status=409;data={detail:{message:'internal routing id 42'}};}
    else {menus=menus.filter(m=>m.menu_id!==path.split('/')[3]);data={status:'deleted'};}
  } else {status=404;data={detail:'Unexpected request'};}
  return {ok:status===200,status,json:async()=>data};
};
vm.runInThisContext(SOURCE);
(async () => {
  await mrMenus();
  const initial={options:element('mrMenu').options.map(o=>o.textContent),destination:element('mrDestination').textContent};
  mrOpenMenuForm('add');
  const formCategories=element('mrMenuCategory').options.map(o=>o.textContent);
  element('mrMenuName').value='Durga Puja Specials';element('mrMenuCategory').value='Sweets';
  element('mrMenuDescription').value='Festive Bengali sweets';
  await mrSaveMenu({preventDefault(){}});
  const created={selected:element('mrMenu').value,destination:element('mrDestination').textContent,open:element('mrMenuDialog').open};
  mrOpenMenuForm('edit');
  const prefilling={name:element('mrMenuName').value,description:element('mrMenuDescription').value};
  element('mrMenuName').value='Renamed Specials';element('mrMenuDescription').value='Updated description';
  element('mrMenuActive').checked=false;
  await mrSaveMenu({preventDefault(){}});
  const edited={selected:element('mrMenu').value,destination:element('mrDestination').textContent,menu:menus.find(m=>m.menu_id==='m3')};
  const beforeCancel=requests.filter(r=>r[0]==='DELETE').length;
  await mrDeleteMenu();
  const afterCancel=requests.filter(r=>r[0]==='DELETE').length;
  blocked=true;await mrDeleteMenu();
  const blockedState={selected:element('mrMenu').value,message:element('mrMessage').textContent,stillExists:menus.some(m=>m.menu_id==='m3')};
  blocked=false;await mrDeleteMenu();
  const deleted={selected:element('mrMenu').value,options:element('mrMenu').options.map(o=>o.textContent)};
  menus=[];await mrMenus('');
  const empty={shown:!element('mrEmpty').hidden,flowHidden:element('mrFlow').hidden,pickerHidden:element('mrPickerRow').hidden};
  mrOpenMenuForm('add');
  element('mrMenuName').value='First Menu';element('mrMenuCategory').value='Other';
  element('mrMenuDescription').value='First collection';
  await mrSaveMenu({preventDefault(){}});
  const firstFromEmpty={selected:element('mrMenu').value,emptyHidden:element('mrEmpty').hidden,flowShown:!element('mrFlow').hidden};
  console.log(JSON.stringify({initial,formCategories,created,prefilling,edited,beforeCancel,afterCancel,blockedState,deleted,empty,firstFromEmpty,confirmations,requests}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    completed = subprocess.run(
        [shutil.which("node")], input="const SOURCE=" + json.dumps(source) + ";\n" + scenario,
        text=True, encoding="utf-8", capture_output=True, timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())
    assert result["initial"] == {
        "options": ["Aamar Mishti Collection", "Weekly Menu"],
        "destination": "Aamar Mishti Collection",
    }
    assert result["formCategories"] == ["Sweets", "Other"]
    assert result["created"] == {"selected": "m3", "destination": "Durga Puja Specials", "open": False}
    assert result["prefilling"] == {"name": "Durga Puja Specials", "description": "Festive Bengali sweets"}
    assert result["edited"]["selected"] == "m3"
    assert result["edited"]["destination"] == "Renamed Specials"
    assert result["edited"]["menu"]["description"] == "Updated description"
    assert result["edited"]["menu"]["active"] is False
    assert result["beforeCancel"] == result["afterCancel"]
    assert result["blockedState"] == {
        "selected": "m3", "message": "This menu is currently being used. Remove or change the connected Instagram source first, then try again.",
        "stillExists": True,
    }
    assert result["deleted"] == {"selected": "m2", "options": ["Aamar Mishti Collection", "Weekly Menu"]}
    assert result["empty"] == {"shown": True, "flowHidden": True, "pickerHidden": True}
    assert result["firstFromEmpty"] == {"selected": "m3", "emptyHidden": True, "flowShown": True}
    assert len(result["confirmations"]) == 3
    assert all(request[1].endswith("/m3/permanent") for request in result["requests"] if request[0] == "DELETE")
