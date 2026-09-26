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
<section id="menuReviewPanel" aria-labelledby="menuReviewHeading">
  <h2 id="menuReviewHeading">Upload Menu</h2>
  <p>Extract details, review every row, then Approve &amp; Publish. Uploading never changes the current menu.</p>
  <label for="mrMenu">Menu</label><select id="mrMenu" class="field-control" onchange="mrLoad()"></select>
  <button type="button" class="btn-secondary" onclick="mrMenus()">Load menus / refresh</button>
  <label for="mrFile">Upload Menu (JPG, PNG, WEBP or PDF; up to 10MB, 8 pages)</label>
  <input id="mrFile" type="file" accept="image/jpeg,image/png,image/webp,application/pdf">
  <label for="mrText">Or paste menu text</label><textarea id="mrText" class="field-control" rows="4" maxlength="50000"></textarea>
  <button type="button" class="btn-primary" onclick="mrExtract()">Extract Menu Details</button>
  <div id="mrMessage" role="status" aria-live="polite"></div>
  <div id="mrReview" hidden>
    <h3>Review Menu</h3><p id="mrStatus"></p>
    <label for="mrWeek">Printed date / week (blank if not stated)</label><input id="mrWeek" class="field-control" maxlength="160">
    <label for="mrWindow">Printed service window (blank if not stated)</label><input id="mrWindow" class="field-control" maxlength="160">
    <div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start">
      <div style="flex:1;min-width:230px"><img id="mrSource" alt="Original menu preview (all PDF pages)" style="max-width:100%" hidden><pre id="mrOriginal" style="white-space:pre-wrap"></pre></div>
      <div style="flex:2;min-width:280px;overflow:auto"><table><thead><tr><th>Item</th><th>Category</th><th>Quantity</th><th>Price (₹)</th><th>Description</th><th>Source / warnings</th><th>Action</th></tr></thead><tbody id="mrRows"></tbody></table></div>
    </div>
    <div class="action-bar" id="mrActions">
      <button type="button" class="btn-secondary" onclick="mrAdd()">Add Row</button>
      <button type="button" class="btn-secondary" onclick="mrSave()">Save Draft</button>
      <button type="button" class="btn-danger" onclick="mrReject()">Reject Draft</button>
      <button type="button" class="btn-primary" onclick="mrApprove()">Approve &amp; Publish</button>
    </div>
    <p>Missing quantities may stay blank only when not stated. Text-only publication removes the old flyer so customers cannot see outdated prices.</p>
  </div>
  <button type="button" class="btn-secondary" onclick="mrHistory()">Published version history</button><div id="mrHistory"></div>
</section>
'''

REVIEW_SCRIPT = r'''
<script>
let mrDraft=null, mrCategories=[], mrPreviewUrl=null, mrBusy=false;
function mrBase(){return '/admin/menu-manager/menus/'+encodeURIComponent(document.getElementById('mrMenu').value)+'/review';}
function mrMessage(value){document.getElementById('mrMessage').textContent=value;}
async function mrRequest(path,method='GET',body){const options={method,headers:getAuthHeaders()};if(body instanceof FormData){options.body=body;}else if(body!==undefined){options.headers={...options.headers,'Content-Type':'application/json'};options.body=JSON.stringify(body);}const response=await fetch(path,options);const data=await response.json();if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'Request could not be completed.');return data;}
async function mrMenus(){try{const data=await mrRequest('/admin/menu-catalogue');const picker=document.getElementById('mrMenu');const selected=picker.value;picker.replaceChildren();(data.menus||[]).forEach(m=>{const o=document.createElement('option');o.value=m.menu_id;o.textContent=m.name;picker.appendChild(o);});if([...picker.options].some(o=>o.value===selected))picker.value=selected;await mrLoad();}catch(e){mrMessage(e.message);}}
async function mrLoad(){if(!document.getElementById('mrMenu').value)return;try{const data=await mrRequest(mrBase());mrDraft=data.draft;mrCategories=data.categories;mrRender();}catch(e){mrMessage(e.message);}}
async function mrWork(task){if(mrBusy)return;mrBusy=true;document.getElementById('mrMenu').disabled=true;try{await task();}catch(e){mrMessage(e.message);}finally{mrBusy=false;document.getElementById('mrMenu').disabled=false;}}
async function mrExtract(){return mrWork(async()=>{const file=document.getElementById('mrFile').files[0];let data;mrMessage('Extracting menu details. The published menu is unchanged.');if(file){const form=new FormData();form.append('file',file);data=await mrRequest(mrBase()+'/extract-file','POST',form);}else{data=await mrRequest(mrBase()+'/extract-text','POST',{text:document.getElementById('mrText').value});}mrDraft=data.draft;mrRender();});}
function mrRender(){document.getElementById('mrReview').hidden=!mrDraft;if(!mrDraft)return;const done=['PUBLISHED','REJECTED','EXTRACTING'].includes(mrDraft.status);document.getElementById('mrStatus').textContent=mrDraft.status+' · Revision '+mrDraft.revision;document.getElementById('mrActions').hidden=done;document.getElementById('mrOriginal').textContent=mrDraft.raw_text||'';document.getElementById('mrWeek').value=mrDraft.menu_metadata?.week_of||'';document.getElementById('mrWindow').value=mrDraft.menu_metadata?.service_window||'';mrMessage((mrDraft.warnings||[]).join('; '));const body=document.getElementById('mrRows');body.replaceChildren();mrDraft.rows.forEach((row,index)=>{const tr=document.createElement('tr');['item','category','quantity','price_inr','description'].forEach(field=>{const td=document.createElement('td');const control=document.createElement(field==='category'?'select':'input');control.setAttribute('aria-label',field+' row '+(index+1));if(field==='category'){['',...mrCategories].forEach(category=>{const o=document.createElement('option');o.value=category;o.textContent=category||'Choose category';control.appendChild(o);});}control.value=row[field]??'';control.disabled=done;control.style.width=field==='description'?'220px':'125px';control.oninput=()=>{row[field]=control.value;};td.appendChild(control);const small=document.createElement('small');small.textContent=' '+(row.provenance?.[field]||'OPERATOR_EDITED');td.appendChild(small);tr.appendChild(td);});const source=document.createElement('td');source.textContent=(row.source_page?'Page '+row.source_page+'. ':'')+(row.warnings||[]).join('; ');tr.appendChild(source);const action=document.createElement('td');const button=document.createElement('button');button.type='button';button.textContent='Delete Row';button.disabled=done;button.onclick=()=>{mrDraft.rows.splice(index,1);mrRender();};action.appendChild(button);tr.appendChild(action);body.appendChild(tr);});const preview=document.getElementById('mrSource');preview.hidden=true;if(mrPreviewUrl){URL.revokeObjectURL(mrPreviewUrl);mrPreviewUrl=null;}if(mrDraft.source_asset){const id=mrDraft.draft_id;fetch(mrBase()+'/source/'+id,{headers:getAuthHeaders()}).then(r=>{if(!r.ok)throw Error('Preview unavailable');return r.blob();}).then(blob=>{if(mrDraft?.draft_id!==id)return;mrPreviewUrl=URL.createObjectURL(blob);preview.src=mrPreviewUrl;preview.hidden=false;}).catch(e=>mrMessage(e.message));}}
function mrAdd(){mrDraft.rows.push({item:'',category:'',quantity:'',price_inr:'',description:''});mrRender();}
async function mrSaveInternal(){const data=await mrRequest(mrBase(),'PUT',{draft_id:mrDraft.draft_id,revision:mrDraft.revision,rows:mrDraft.rows,menu_metadata:{week_of:document.getElementById('mrWeek').value,service_window:document.getElementById('mrWindow').value}});mrDraft=data.draft;mrRender();return data;}
async function mrSave(){return mrWork(async()=>{await mrSaveInternal();mrMessage('Draft saved. Published menu unchanged.');});}
async function mrReject(){return mrWork(async()=>{if(!confirm('Reject this draft? The published menu will not change.'))return;const data=await mrRequest(mrBase()+'/reject','POST',{draft_id:mrDraft.draft_id,revision:mrDraft.revision});mrDraft=data.draft;mrRender();});}
async function mrApprove(){return mrWork(async()=>{await mrSaveInternal();if(!confirm('Approve every displayed fact and publish this menu and its flyer to customers?'))return;const data=await mrRequest(mrBase()+'/approve-publish','POST',{draft_id:mrDraft.draft_id,revision:mrDraft.revision});mrDraft=data.draft;mrRender();mrMessage('Approved and published. Menu knowledge is live.');});}
async function mrHistory(){try{const data=await mrRequest(mrBase()+'/versions');const box=document.getElementById('mrHistory');box.replaceChildren();data.versions.filter(v=>v.approval_json).forEach(v=>{const p=document.createElement('p');p.textContent='Version '+v.id+' · '+v.changed_at+(v.is_current?' · Current':'');const button=document.createElement('button');button.textContent='Restore to review draft';button.onclick=()=>mrWork(async()=>{const data=await mrRequest(mrBase()+'/versions/'+v.id+'/restore','POST');mrDraft=data.draft;mrRender();});p.appendChild(button);box.appendChild(p);});}catch(e){mrMessage(e.message);}}
// All visible menu input actions converge on the review screen.
async function mmReplaceImage(key){switchTab('tab-image',null);await mrMenus();const aliases={'collections.aamar_mishti':'aamar_mishti','collections.signature_collection':'signature','weekly.vegetarian':'weekly_vegetarian','weekly.non_vegetarian':'weekly_non_vegetarian','weekly.bengali_bento':'bento'};document.getElementById('mrMenu').value=aliases[key]||key;await mrLoad();document.getElementById('mrFile').click();}
async function submitMenuImage(){return mrExtract();}
async function mmPublishImage(){switchTab('tab-image',null);await mrMenus();mrMessage('Review the extracted facts, then use Approve & Publish.');}
</script>
'''
