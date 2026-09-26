"""Authenticated Menu Manager review routes and its small table editor."""
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from camellia import menu_review

router = APIRouter(prefix="/admin/menu-manager/menus/{menu_key}/review")


def reviewer(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    from camellia.app import require_admin, _admin_context
    require_admin(x_admin_key, x_admin_pin)
    context = _admin_context.get() or {}
    # Never store the API credential as an audit identity.
    return str(context.get("username") or "admin-api")[:120]


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except menu_review.ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "Menu, draft or version not found.") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
def get_review(menu_key: str, actor=Depends(reviewer)):
    return {"draft": call(menu_review.get_draft, menu_key),
            "categories": call(menu_review.categories, menu_key)}


@router.post("/extract-text")
def extract_text(menu_key: str, body: dict = Body(...), actor=Depends(reviewer)):
    return {"draft": call(menu_review.extract, menu_key, actor, text=body.get("text"))}


@router.post("/extract-file")
async def extract_file(menu_key: str, file: UploadFile = File(...), actor=Depends(reviewer)):
    data = await file.read(menu_review.MAX_BYTES + 1)
    if len(data) > menu_review.MAX_BYTES:
        raise HTTPException(413, "Menu files must be at most 10MB.")
    return {"draft": await run_in_threadpool(call, menu_review.extract, menu_key, actor,
            data=data, mime=file.content_type, filename=file.filename or "")}


@router.put("")
def save_review(menu_key: str, body: dict = Body(...), actor=Depends(reviewer)):
    return {"draft": call(menu_review.edit, menu_key, body.get("draft_id"), body.get("revision"), body.get("rows"), actor, body.get("menu_metadata"))}


@router.post("/reject")
def reject_review(menu_key: str, body: dict = Body(...), actor=Depends(reviewer)):
    return {"draft": call(menu_review.reject, menu_key, body.get("draft_id"), body.get("revision"), actor)}


@router.post("/approve-publish")
def approve_review(menu_key: str, body: dict = Body(...), actor=Depends(reviewer)):
    return {"draft": call(menu_review.publish_approved_menu, menu_key, body.get("draft_id"), body.get("revision"), actor)}


@router.get("/source/{draft_id}")
def review_source(menu_key: str, draft_id: str, actor=Depends(reviewer)):
    path = call(menu_review.source_file, menu_key, draft_id)
    # PDF preview uses the inert rendered flyer, not an active PDF document.
    if path.suffix == ".pdf":
        draft = menu_review.get_draft(menu_key)
        path = path.parent / draft["flyer_asset"]
    return FileResponse(path, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/versions")
def review_versions(menu_key: str, actor=Depends(reviewer)):
    return {"versions": call(menu_review.version_history, menu_key)}


@router.post("/versions/{version_id}/restore")
def restore_review(menu_key: str, version_id: int, actor=Depends(reviewer)):
    return {"draft": call(menu_review.restore_as_draft, menu_key, version_id, actor)}


REVIEW_HTML = r'''
<style>
  #menuReviewPanel{max-width:850px;margin:0 auto}
  #menuReviewPanel [hidden]{display:none!important}
  #menuReviewPanel .mr-header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:20px}
  #menuReviewPanel .mr-header h2{margin:0;color:var(--primary)}
  #menuReviewPanel .mr-header p{margin:5px 0 0;color:var(--text-muted)}
  #menuReviewPanel .mr-catalogue-list{display:grid;gap:12px}
  #menuReviewPanel .mr-card{padding:18px;background:#fff;border:1px solid var(--border);border-radius:12px}
  #menuReviewPanel .mr-card h3{margin:0 0 5px;color:var(--primary)}
  #menuReviewPanel .mr-card p{margin:0 0 14px;color:var(--text-muted)}
  #menuReviewPanel .mr-card-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  #menuReviewPanel .mr-card-actions details{position:relative}
  #menuReviewPanel .mr-card-actions summary{list-style:none;cursor:pointer;min-width:44px;min-height:44px;display:grid;place-items:center;border:1px solid var(--border);border-radius:8px}
  #menuReviewPanel .mr-card-actions summary::-webkit-details-marker{display:none}
  #menuReviewPanel .mr-menu-actions{position:absolute;left:0;z-index:4;min-width:145px;padding:8px;background:#fff;border:1px solid var(--border);border-radius:8px;box-shadow:0 4px 12px #0002}
  #menuReviewPanel .mr-menu-actions button{display:block;width:100%;min-height:44px;text-align:left}
  #menuReviewPanel .mr-inactive{font-size:.8rem;color:var(--text-muted);border:1px solid var(--border);border-radius:100px;padding:2px 9px}
  #menuReviewPanel .mr-tabs{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0;border-bottom:1px solid var(--border);padding-bottom:10px}
  #menuReviewPanel .mr-tabs button{min-height:44px}
  #menuReviewPanel .mr-tabs button[aria-selected="true"]{background:var(--primary);color:#fff}
  #menuReviewPanel .mr-current-row{display:grid;grid-template-columns:minmax(0,1fr) 90px 90px;gap:12px;padding:10px 0;border-bottom:1px solid var(--border)}
  #menuReviewPanel .mr-current-row span:last-child{text-align:right}
  #menuReviewPanel .mr-input-tabs{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
  #menuReviewPanel .mr-input-tabs button{min-height:44px}
  #menuReviewPanel .mr-drop{display:block;padding:22px;border:2px dashed var(--border);border-radius:10px;background:#fcfaf7}
  #menuReviewPanel .mr-drop input{display:block;margin-top:12px;max-width:100%}
  #menuReviewPanel .mr-review-table{overflow-x:auto}
  #menuReviewPanel .mr-review-table table{width:100%}
  #menuReviewPanel .mr-review-table input,#menuReviewPanel .mr-review-table select{max-width:100%}
  #menuReviewPanel .mr-version{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 0;border-bottom:1px solid var(--border)}
  #menuReviewPanel dialog{max-width:min(520px,calc(100vw - 32px));width:100%;border:1px solid #888;border-radius:8px;padding:24px}
  #menuReviewPanel dialog::backdrop{background:#0008}
  #menuReviewPanel dialog .field-control{width:100%;margin-bottom:12px}
  #menuReviewPanel .mr-dialog-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
  #menuReviewPanel .mr-mobile-actions{display:none}
  @media(max-width:600px){#menuReviewPanel .mr-card-actions details{display:none}#menuReviewPanel .mr-mobile-actions{display:flex;gap:8px;flex-wrap:wrap}#menuReviewPanel .mr-mobile-actions button{min-height:44px}#menuReviewPanel .mr-current-row{grid-template-columns:minmax(0,1fr) 65px 65px;font-size:.9rem}#menuReviewPanel .mr-review-table{overflow:visible}#menuReviewPanel .mr-review-table thead{display:none}#menuReviewPanel .mr-review-table tr{display:block;border:1px solid var(--border);border-radius:10px;padding:10px;margin-bottom:12px}#menuReviewPanel .mr-review-table td{display:block;border:0;padding:6px}#menuReviewPanel .mr-review-table td:before{content:attr(data-label);display:block;font-size:.75rem;color:var(--text-muted)}#menuReviewPanel .mr-review-table input,#menuReviewPanel .mr-review-table select{width:100%!important}}
</style>
<section id="menuReviewPanel" aria-labelledby="menuReviewHeading">
  <select id="mrMenu" hidden aria-hidden="true"></select>
  <div id="mrCatalogueView">
    <div class="mr-header"><div><h2 id="menuReviewHeading">Menu Catalogue</h2><p>Manage your menus.</p></div>
      <button type="button" class="btn-primary" onclick="mrOpenMenuForm('add')">+ Add Menu</button></div>
    <div id="mrCatalogueList" class="mr-catalogue-list"></div>
    <div id="mrEmpty" hidden><p>No menus yet. Create your first menu to get started.</p>
      <button type="button" class="btn-primary" onclick="mrOpenMenuForm('add')">+ Add Menu</button></div>
  </div>
  <div id="mrSelectedView" hidden>
    <button type="button" class="btn-secondary" onclick="mrShowCatalogue()">← Menu Catalogue</button>
    <div class="mr-header" style="margin-top:16px"><div><h2 id="mrSelectedName"></h2><p id="mrSelectedDescription"></p></div><span id="mrSelectedInactive" class="mr-inactive" hidden>Inactive</span></div>
    <nav class="mr-tabs" aria-label="Menu sections">
      <button type="button" class="btn-secondary" data-mr-pane="current" aria-selected="true" onclick="mrShowPane('current')">Menu</button>
      <button type="button" class="btn-secondary" data-mr-pane="upload" aria-selected="false" onclick="mrShowPane('upload')">Upload Menu</button>
      <button type="button" class="btn-secondary" data-mr-pane="versions" aria-selected="false" onclick="mrShowPane('versions')">Versions</button>
    </nav>
    <div id="mrCurrentPane"><h3>Current Menu</h3><div id="mrCurrentRows"></div>
      <p id="mrLastPublished"></p><div id="mrCurrentEmpty" hidden><p>No menu published yet. Upload a menu to get started.</p>
        <button type="button" class="btn-primary" onclick="mrShowPane('upload')">Upload Menu</button></div></div>
    <div id="mrUploadPane" hidden>
      <h3>Upload Menu</h3>
      <div id="mrUploadInput">
        <div class="mr-input-tabs"><button type="button" class="btn-secondary" id="mrFileTab" onclick="mrInputMode('file')">Upload File</button>
          <button type="button" class="btn-secondary" id="mrTextTab" onclick="mrInputMode('text')">Paste Text</button></div>
        <div id="mrFilePane"><label id="mrDrop" class="mr-drop" for="mrFile"><strong>Drop your menu here</strong><br>JPG, PNG, WEBP or PDF · Maximum 10 MB
          <input id="mrFile" type="file" accept="image/jpeg,image/png,image/webp,application/pdf"></label></div>
        <div id="mrTextPane" hidden><label for="mrText">Paste menu text</label><textarea id="mrText" class="field-control" rows="7" maxlength="50000"></textarea></div>
        <button type="button" class="btn-primary" onclick="mrExtract()" style="margin-top:16px">Continue to Review</button>
      </div>
      <div id="mrReview" hidden>
        <h3>Review Menu</h3><p id="mrReviewCount"></p><p>Check everything before publishing.</p><p id="mrStatus"></p>
        <label for="mrWeek">Printed date / week (blank if not stated)</label><input id="mrWeek" class="field-control" maxlength="160">
        <label for="mrWindow">Printed service window (blank if not stated)</label><input id="mrWindow" class="field-control" maxlength="160">
        <div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start">
          <div style="flex:1;min-width:0"><img id="mrSource" alt="Original menu preview (all PDF pages)" style="max-width:100%" hidden><pre id="mrOriginal" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre></div>
          <div class="mr-review-table" style="flex:2;min-width:0"><table><thead><tr><th>Item</th><th>Category</th><th>Quantity</th><th>Price (₹)</th><th>Description</th><th>Source / warnings</th><th>Action</th></tr></thead><tbody id="mrRows"></tbody></table></div>
        </div>
        <div class="action-bar" id="mrActions">
          <button type="button" class="btn-secondary" onclick="mrAdd()">Add item</button>
          <button type="button" class="btn-secondary" onclick="mrSave()">Save Draft</button>
          <button type="button" class="btn-danger" onclick="mrReject()">Reject Draft</button>
          <button type="button" class="btn-primary" onclick="mrApprove()">Approve &amp; Publish</button>
        </div>
        <p>Leave details blank when they are not stated. Publishing text without a flyer removes the old flyer.</p>
      </div>
    </div>
    <div id="mrVersionsPane" hidden><h3>Versions</h3><div id="mrHistory"></div></div>
  </div>
  <div id="mrMessage" role="status" aria-live="polite"></div>
  <dialog id="mrMenuDialog" aria-labelledby="mrMenuDialogTitle">
    <h3 id="mrMenuDialogTitle">Add New Menu</h3>
    <form id="mrMenuForm" onsubmit="mrSaveMenu(event)">
      <label for="mrMenuName">Menu name *</label><input id="mrMenuName" class="field-control" maxlength="120" required>
      <label for="mrMenuCategory">Category *</label><select id="mrMenuCategory" class="field-control" required></select>
      <label for="mrMenuDescription">Description *</label><textarea id="mrMenuDescription" class="field-control" maxlength="1000" required></textarea>
      <label><input id="mrMenuActive" type="checkbox" checked> Active</label>
      <div class="mr-dialog-actions"><button type="button" class="btn-secondary" onclick="document.getElementById('mrMenuDialog').close()">Cancel</button><button id="mrMenuSubmit" type="submit" class="btn-primary">Add Menu</button></div>
    </form>
    <p id="mrMenuFormError" role="alert"></p>
  </dialog>
</section>
'''

REVIEW_SCRIPT = r'''
<script>
let mrDraft=null, mrCategories=[], mrMenuCategories=[], mrCatalogue=[], mrPreviewUrl=null, mrBusy=false, mrEditingId=null, mrInput='file';
function mrBase(){return '/admin/menu-manager/menus/'+encodeURIComponent(document.getElementById('mrMenu').value)+'/review';}
function mrMessage(value){document.getElementById('mrMessage').textContent=value;}
async function mrRequest(path,method='GET',body){const options={method,headers:getAuthHeaders()};if(body instanceof FormData){options.body=body;}else if(body!==undefined){options.headers={...options.headers,'Content-Type':'application/json'};options.body=JSON.stringify(body);}const response=await fetch(path,options);const data=await response.json();if(!response.ok){const error=Error(typeof data.detail==='string'?data.detail:'Request could not be completed.');error.status=response.status;throw error;}return data;}
async function mrMenus(preferredId){
  try{
    const data=await mrRequest('/admin/menu-catalogue');
    mrCatalogue=data.menus||[];mrMenuCategories=data.categories||[];
    const picker=document.getElementById('mrMenu');picker.replaceChildren();
    const list=document.getElementById('mrCatalogueList');list.replaceChildren();
    mrCatalogue.forEach(menu=>{
      const option=document.createElement('option');option.value=menu.menu_id;option.textContent=menu.name;picker.appendChild(option);
      const card=document.createElement('article');card.className='mr-card';
      const title=document.createElement('h3');title.textContent=menu.name;card.appendChild(title);
      const description=document.createElement('p');description.textContent=menu.description||'';card.appendChild(description);
      if(!menu.active){const badge=document.createElement('span');badge.className='mr-inactive';badge.textContent='Inactive';card.appendChild(badge);}
      const actions=document.createElement('div');actions.className='mr-card-actions';
      const open=document.createElement('button');open.type='button';open.className='btn-primary';open.textContent='Manage Menu';open.onclick=()=>mrOpenMenu(menu.menu_id);actions.appendChild(open);
      const more=document.createElement('details');const summary=document.createElement('summary');summary.textContent='⋯';summary.setAttribute('aria-label','Actions for '+menu.name);more.appendChild(summary);
      const menuActions=document.createElement('div');menuActions.className='mr-menu-actions';
      const mobile=document.createElement('div');mobile.className='mr-mobile-actions';
      for(const container of [menuActions,mobile]){
        const edit=document.createElement('button');edit.type='button';edit.className='btn-secondary';edit.textContent='Edit Menu';edit.onclick=()=>mrOpenMenuForm('edit',menu.menu_id);container.appendChild(edit);
        const remove=document.createElement('button');remove.type='button';remove.className='btn-danger';remove.textContent='Delete Menu';remove.onclick=()=>mrDeleteMenu(menu.menu_id);container.appendChild(remove);
      }
      more.appendChild(menuActions);actions.appendChild(more);card.appendChild(actions);card.appendChild(mobile);list.appendChild(card);
    });
    document.getElementById('mrEmpty').hidden=!!mrCatalogue.length;
    if(preferredId && mrCatalogue.some(menu=>menu.menu_id===preferredId))await mrOpenMenu(preferredId);
    else mrShowCatalogue();
    return true;
  }catch(e){mrMessage(e.message);return false;}
}
function mrShowCatalogue(){document.getElementById('mrCatalogueView').hidden=false;document.getElementById('mrSelectedView').hidden=true;document.getElementById('mrMenu').value='';}
async function mrOpenMenu(id,pane='current'){
  const menu=mrCatalogue.find(item=>item.menu_id===id);if(!menu)return;
  document.getElementById('mrMenu').value=id;
  document.getElementById('mrSelectedName').textContent=menu.name;
  document.getElementById('mrSelectedDescription').textContent=menu.description||'';
  document.getElementById('mrSelectedInactive').hidden=!!menu.active;
  document.getElementById('mrCatalogueView').hidden=true;document.getElementById('mrSelectedView').hidden=false;
  mrDraft=null;mrRender();await mrLoad(id);await mrShowPane(pane);
}
async function mrShowPane(pane){
  for(const [name,id] of [['current','mrCurrentPane'],['upload','mrUploadPane'],['versions','mrVersionsPane']])document.getElementById(id).hidden=name!==pane;
  document.querySelectorAll('#menuReviewPanel [data-mr-pane]').forEach(button=>button.setAttribute('aria-selected',String(button.dataset.mrPane===pane)));
  if(pane==='current')await mrLoadCurrent();if(pane==='versions')await mrHistory();if(pane==='upload')mrInputMode(mrInput);
}
function mrInputMode(mode){mrInput=mode;document.getElementById('mrFilePane').hidden=mode!=='file';document.getElementById('mrTextPane').hidden=mode!=='text';document.getElementById('mrFileTab').setAttribute('aria-selected',String(mode==='file'));document.getElementById('mrTextTab').setAttribute('aria-selected',String(mode==='text'));}
const mrDrop=document.getElementById('mrDrop');
mrDrop.addEventListener('dragover',event=>{event.preventDefault();});
mrDrop.addEventListener('drop',event=>{event.preventDefault();if(event.dataTransfer?.files?.length){document.getElementById('mrFile').files=event.dataTransfer.files;mrInputMode('file');}});
async function mrLoadCurrent(){
  const id=document.getElementById('mrMenu').value;if(!id)return;
  try{
    const data=await mrRequest(mrBase()+'/versions');if(document.getElementById('mrMenu').value!==id)return;
    const current=(data.versions||[]).find(version=>version.is_current&&version.approval_json);
    const rows=document.getElementById('mrCurrentRows');rows.replaceChildren();
    document.getElementById('mrCurrentEmpty').hidden=!!current;
    document.getElementById('mrLastPublished').textContent=current?'Last published: '+(current.changed_at||'Date unavailable'):'';
    if(!current)return;
    const approval=JSON.parse(current.approval_json);
    (approval.rows||[]).forEach(item=>{
      const row=document.createElement('div');row.className='mr-current-row';
      for(const value of [item.item||'',item.quantity||'',item.price_inr===null||item.price_inr===undefined?'':'₹'+item.price_inr]){
        const span=document.createElement('span');span.textContent=value;row.appendChild(span);
      }
      rows.appendChild(row);
    });
  }catch(e){mrMessage(e.message);}
}
async function mrLoad(id=document.getElementById('mrMenu').value){if(!id)return;try{const data=await mrRequest('/admin/menu-manager/menus/'+encodeURIComponent(id)+'/review');if(document.getElementById('mrMenu').value!==id)return;mrDraft=data.draft;mrCategories=data.categories;mrRender();}catch(e){mrMessage(e.message);}}
function mrOpenMenuForm(mode,id){const selected=mrCatalogue.find(m=>m.menu_id===(id||document.getElementById('mrMenu').value));if(mode==='edit'&&!selected)return;mrEditingId=mode==='edit'?selected.menu_id:null;document.getElementById('mrMenuDialogTitle').textContent=mrEditingId?'Edit Menu':'Add New Menu';document.getElementById('mrMenuSubmit').textContent=mrEditingId?'Save Changes':'Add Menu';document.getElementById('mrMenuName').value=selected&&mrEditingId?selected.name:'';document.getElementById('mrMenuDescription').value=selected&&mrEditingId?selected.description:'';document.getElementById('mrMenuActive').checked=selected&&mrEditingId?!!selected.active:true;document.getElementById('mrMenuFormError').textContent='';const category=document.getElementById('mrMenuCategory');category.replaceChildren();[...new Set([...mrMenuCategories,...(selected&&mrEditingId?[selected.category]:[])])].forEach(name=>{const option=document.createElement('option');option.value=name;option.textContent=name;category.appendChild(option);});category.value=selected&&mrEditingId?selected.category:mrMenuCategories[0]||'';document.getElementById('mrMenuDialog').showModal();document.getElementById('mrMenuName').focus();}
async function mrSaveMenu(event){event.preventDefault();if(mrBusy)return;const form=document.getElementById('mrMenuForm');if(!form.reportValidity())return;const payload={name:document.getElementById('mrMenuName').value.trim(),category:document.getElementById('mrMenuCategory').value,description:document.getElementById('mrMenuDescription').value.trim(),active:document.getElementById('mrMenuActive').checked};if(!payload.name||!payload.category||!payload.description){document.getElementById('mrMenuFormError').textContent='Complete all required fields.';return;}mrBusy=true;document.getElementById('mrMenuSubmit').disabled=true;try{let result;const editing=!!mrEditingId;if(editing){result=await mrRequest('/admin/menu-catalogue/'+encodeURIComponent(mrEditingId),'PATCH',payload);}else{const body=new FormData();Object.entries(payload).forEach(([key,value])=>body.append(key,String(value)));result=await mrRequest('/admin/menu-catalogue','POST',body);}document.getElementById('mrMenuDialog').close();await mrMenus(result.menu.menu_id);if(!editing)await mrShowPane('upload');mrMessage(editing?'Menu updated.':'Menu added. Upload or paste its content to begin review.');}catch(e){document.getElementById('mrMenuFormError').textContent=e.message;}finally{mrBusy=false;document.getElementById('mrMenuSubmit').disabled=false;}}
async function mrDeleteMenu(id){const menu=mrCatalogue.find(m=>m.menu_id===(id||document.getElementById('mrMenu').value));if(!menu)return;if(!confirm('Delete “'+menu.name+'”?\n\nThis permanently removes this menu and its local menu assets/history. It cannot be undone.'))return;try{await mrRequest('/admin/menu-catalogue/'+encodeURIComponent(menu.menu_id)+'/permanent','DELETE');await mrMenus();mrMessage('Menu deleted.');}catch(e){mrMessage(e.status===409?'This menu is currently being used. Remove or change the connected Instagram source first, then try again.':e.message);}}
async function mrWork(task){if(mrBusy)return;mrBusy=true;document.getElementById('mrMenu').disabled=true;try{await task();}catch(e){mrMessage(e.message);}finally{mrBusy=false;document.getElementById('mrMenu').disabled=false;}}
async function mrExtract(){return mrWork(async()=>{let data;mrMessage('Extracting menu details. The published menu is unchanged.');if(mrInput==='file'){const file=document.getElementById('mrFile').files[0];if(!file)throw Error('Choose a menu file first.');const form=new FormData();form.append('file',file);data=await mrRequest(mrBase()+'/extract-file','POST',form);}else{const value=document.getElementById('mrText').value.trim();if(!value)throw Error('Paste menu text first.');data=await mrRequest(mrBase()+'/extract-text','POST',{text:value});}mrDraft=data.draft;mrRender();});}
function mrRender(){
  const reviewing=!!mrDraft&&!['PUBLISHED','REJECTED'].includes(mrDraft.status);
  document.getElementById('mrReview').hidden=!reviewing;document.getElementById('mrUploadInput').hidden=reviewing;
  if(!reviewing)return;
  const done=mrDraft.status==='EXTRACTING';
  document.getElementById('mrReviewCount').textContent='We found '+mrDraft.rows.length+' items.';
  document.getElementById('mrStatus').textContent=mrDraft.status+' · Revision '+mrDraft.revision;
  document.getElementById('mrActions').hidden=done;
  document.getElementById('mrOriginal').textContent=mrDraft.raw_text||'';
  document.getElementById('mrWeek').value=mrDraft.menu_metadata?.week_of||'';
  document.getElementById('mrWindow').value=mrDraft.menu_metadata?.service_window||'';
  mrMessage((mrDraft.warnings||[]).join('; '));
  const body=document.getElementById('mrRows');body.replaceChildren();
  mrDraft.rows.forEach((row,index)=>{
    const tr=document.createElement('tr');
    for(const field of ['item','category','quantity','price_inr','description']){
      const td=document.createElement('td');td.dataset.label=field==='price_inr'?'Price (₹)':field;
      const control=document.createElement(field==='category'?'select':'input');control.setAttribute('aria-label',field+' row '+(index+1));
      if(field==='category')for(const category of ['',...mrCategories]){const option=document.createElement('option');option.value=category;option.textContent=category||'Choose category';control.appendChild(option);}
      control.value=row[field]??'';control.disabled=done;control.style.width=field==='description'?'220px':'125px';
      control.oninput=()=>{row[field]=control.value;};td.appendChild(control);
      const small=document.createElement('small');small.textContent=' '+(row.provenance?.[field]||'OPERATOR_EDITED');td.appendChild(small);tr.appendChild(td);
    }
    const source=document.createElement('td');source.dataset.label='Source / warnings';source.textContent=(row.source_page?'Page '+row.source_page+'. ':'')+(row.warnings||[]).join('; ');tr.appendChild(source);
    const action=document.createElement('td');action.dataset.label='Action';const button=document.createElement('button');button.type='button';button.textContent='Delete Row';button.disabled=done;button.onclick=()=>{mrDraft.rows.splice(index,1);mrRender();};action.appendChild(button);tr.appendChild(action);body.appendChild(tr);
  });
  const preview=document.getElementById('mrSource');preview.hidden=true;if(mrPreviewUrl){URL.revokeObjectURL(mrPreviewUrl);mrPreviewUrl=null;}
  if(mrDraft.source_asset){const id=mrDraft.draft_id;fetch(mrBase()+'/source/'+id,{headers:getAuthHeaders()}).then(r=>{if(!r.ok)throw Error('Preview unavailable');return r.blob();}).then(blob=>{if(mrDraft?.draft_id!==id)return;mrPreviewUrl=URL.createObjectURL(blob);preview.src=mrPreviewUrl;preview.hidden=false;}).catch(e=>mrMessage(e.message));}
}
function mrAdd(){mrDraft.rows.push({item:'',category:'',quantity:'',price_inr:'',description:''});mrRender();}
async function mrSaveInternal(){const data=await mrRequest(mrBase(),'PUT',{draft_id:mrDraft.draft_id,revision:mrDraft.revision,rows:mrDraft.rows,menu_metadata:{week_of:document.getElementById('mrWeek').value,service_window:document.getElementById('mrWindow').value}});mrDraft=data.draft;mrRender();return data;}
async function mrSave(){return mrWork(async()=>{await mrSaveInternal();mrMessage('Draft saved. Published menu unchanged.');});}
async function mrReject(){return mrWork(async()=>{if(!confirm('Reject this draft? The published menu will not change.'))return;const data=await mrRequest(mrBase()+'/reject','POST',{draft_id:mrDraft.draft_id,revision:mrDraft.revision});mrDraft=data.draft;mrRender();});}
async function mrApprove(){return mrWork(async()=>{await mrSaveInternal();const menu=mrCatalogue.find(item=>item.menu_id===document.getElementById('mrMenu').value);if(!confirm('Publish this menu?\n\n'+mrDraft.rows.length+' approved items will replace the current published version of '+(menu?.name||'this menu')+'.'))return;const data=await mrRequest(mrBase()+'/approve-publish','POST',{draft_id:mrDraft.draft_id,revision:mrDraft.revision});mrDraft=data.draft;mrRender();await mrShowPane('current');mrMessage('Approved and published. Menu knowledge is live.');});}
async function mrHistory(){const id=document.getElementById('mrMenu').value;if(!id)return;try{const data=await mrRequest(mrBase()+'/versions');if(document.getElementById('mrMenu').value!==id)return;const box=document.getElementById('mrHistory');box.replaceChildren();const versions=(data.versions||[]).filter(v=>v.approval_json);if(!versions.length){box.textContent='No published versions yet.';return;}versions.forEach(v=>{const line=document.createElement('div');line.className='mr-version';const label=document.createElement('span');label.textContent='Version '+v.id+' · '+v.changed_at+(v.is_current?' · Current':'');line.appendChild(label);const button=document.createElement('button');button.type='button';button.className='btn-secondary';button.textContent='Restore to review draft';button.onclick=()=>mrWork(async()=>{const data=await mrRequest(mrBase()+'/versions/'+v.id+'/restore','POST');mrDraft=data.draft;mrRender();await mrShowPane('upload');mrMessage('Version restored to a review draft. Published menu unchanged.');});line.appendChild(button);box.appendChild(line);});}catch(e){mrMessage(e.message);}}
// All visible menu input actions converge on the review screen.
async function mmReplaceImage(key){switchTab('tab-image',null);await mrMenus();const aliases={'collections.aamar_mishti':'aamar_mishti','collections.signature_collection':'signature','weekly.vegetarian':'weekly_vegetarian','weekly.non_vegetarian':'weekly_non_vegetarian','weekly.bengali_bento':'bento'};await mrOpenMenu(aliases[key]||key,'upload');mrInputMode('file');document.getElementById('mrFile').click();}
async function submitMenuImage(){return mrExtract();}
async function mmPublishImage(){switchTab('tab-image',null);await mrMenus();mrMessage('Review the extracted facts, then use Approve & Publish.');}
</script>
'''
