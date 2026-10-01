"""Click the catalogue projection using the shipped Admin browser scripts."""
import json
import shutil
import subprocess

import pytest

from camellia.admin_menu_review import REVIEW_SCRIPT
from camellia.admin_weekly_savoury import WEEKLY_HTML, WEEKLY_SCRIPT


@pytest.mark.skipif(shutil.which('node') is None, reason='Admin script test requires Node')
def test_projection_click_opens_existing_weekly_workspace_and_returns():
    scripts = '\n'.join(s.split('<script>', 1)[1].split('</script>', 1)[0]
                        for s in (REVIEW_SCRIPT, WEEKLY_SCRIPT))
    scenario = r"""
const vm=require('vm'),elements={},requests=[];
function node(tag='div'){return {tag,children:[],hidden:false,value:'',style:{},dataset:{},
  textContent:'',className:'',listeners:{},classList:{add(){},remove(){}},
  appendChild(x){this.children.push(x);},replaceChildren(...xs){this.children=xs;},
  addEventListener(name,fn){this.listeners[name]=fn;},setAttribute(k,v){this[k]=v;},
  focus(){document.activeElement=this;},scrollIntoView(){this.scrolled=true;}};}
function el(id){return elements[id] ||= node();}
global.document={getElementById:el,createElement:node,querySelectorAll:()=>[],addEventListener(){}};
global.getAuthHeaders=()=>({});global.setTimeout=()=>1;global.clearTimeout=()=>{};
const ordinary={menu_id:'aamar',name:'Aamar Mishti',active:true};
const projection={menu_id:'bento',name:'Savoury Meal Menu',active:true,read_only:true,
  workspace:'menu',derived_from:'__weekly_savoury__',display_status:'CURRENT'};
global.fetch=async(path,opts={})=>{
  requests.push([path,opts.method||'GET']);let data;
  if(path==='/admin/menu-catalogue')data={menus:[ordinary,projection],workspace_rows:[ordinary,projection],categories:[]};
  else if(path==='/admin/weekly-savoury/customer-topics')data={topics:[],routing_keys:[]};
  else if(path==='/admin/weekly-savoury')data={today:'2026-10-02',current:{status:'CURRENT',display_date_range:'2–8 Oct',item_count:13},upcoming:[{status:'UPCOMING',display_date_range:'9–15 Oct',valid_from:'2026-10-09',item_count:2}],draft:null};
  else if(path.endsWith('/versions'))data={versions:[]};
  else if(path.endsWith('/review'))data={draft:null,categories:[]};
  else throw Error('Unexpected '+path);
  return {ok:true,json:async()=>data};
};
vm.runInThisContext(SOURCE);
(async()=>{
  await mrMenus();wsShowTimeline('upcoming');
  const cards=el('mrCatalogueList').children;
  const card=cards.find(c=>c.children[1].children.some(x=>x.textContent==='Savoury Meal Menu'));
  const actions=card.children[1].children.find(x=>x.className==='mr-card-actions');
  const button=actions.children.find(x=>x.textContent==='Open weekly workspace');
  if(!button||el('mrCatalogueView').hidden)throw Error('Catalogue projection missing');
  const beforeLoads=requests.filter(([p])=>p==='/admin/weekly-savoury').length;
  await button.onclick();
  const weekly={catalogueHidden:el('mrCatalogueView').hidden,weeklyVisible:!el('mrWeeklyView').hidden,
    independentEditorHidden:el('mrSelectedView').hidden,selection:el('mrMenu').value,
    currentVisible:!el('wsTimelineCurrent').hidden,upcomingHidden:el('wsTimelineUpcoming').hidden,
    current:el('wsState').children[0].children[0].textContent,
    upcoming:el('wsUpcomingVersions').children[0].children[0].textContent,
    focused:document.activeElement===el('wsHeading'),scrolled:el('wsHeading').scrolled,
    loaded:requests.filter(([p])=>p==='/admin/weekly-savoury').length-beforeLoads,
    noBentoEditor:!requests.some(([p])=>p.includes('/bento/'))};
  mrShowCatalogue();const returned=!el('mrCatalogueView').hidden;
  await cards[0].children[1].children.find(x=>x.className==='mr-card-actions').children[0].onclick();
  const ordinaryOpen=el('mrCatalogueView').hidden&&el('mrWeeklyView').hidden&&!el('mrSelectedView').hidden&&el('mrMenu').value==='aamar';
  console.log(JSON.stringify({weekly,returned,ordinaryOpen,onlyReads:requests.every(([,m])=>m==='GET')}));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    run = subprocess.run([shutil.which('node')], input='const SOURCE='+json.dumps(scripts)+';\n'+scenario,
                         capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result['weekly'] == dict(catalogueHidden=True, weeklyVisible=True,
        independentEditorHidden=True, selection='', currentVisible=True, upcomingHidden=True,
        current='LIVE / CURRENT · 2–8 Oct · 13 items',
        upcoming='PUBLISHED — UPCOMING · 9–15 Oct · 2 items',
        focused=True, scrolled=True, loaded=1, noBentoEditor=True)
    assert result['returned'] and result['ordinaryOpen'] and result['onlyReads']
    assert '<h2 id="wsHeading" tabindex="-1">' in WEEKLY_HTML
