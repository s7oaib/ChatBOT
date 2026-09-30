"""Dedicated approved authority UI and guarded dry-run/publication routes."""
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from camellia import approved_qa


def create_router(require_admin, actor):
    router = APIRouter(prefix="/admin/approved-qa")

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "Approved Q&A or version not found") from exc
        except Exception as exc:
            logging.getLogger(__name__).exception("Approved Q&A operation failed")
            raise HTTPException(503, "Approved Q&A storage unavailable; published content was preserved") from exc

    async def body(request):
        try:
            value = await request.json()
        except Exception as exc:
            raise HTTPException(400, "Invalid JSON body") from exc
        if not isinstance(value, dict):
            raise HTTPException(400, "Expected a JSON object")
        return value

    @router.get("")
    def listing(x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        return call(approved_qa.listing)

    @router.post("/test")
    async def test(request: Request, x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        data = await body(request)
        message = data.get("message")
        if not isinstance(message, str) or len(message) > 4000:
            raise HTTPException(400, "Enter a test question")
        return call(approved_qa.test, message)

    @router.post("/preview")
    async def preview(request: Request, x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        return call(approved_qa.preview, await body(request))

    @router.post("/publish")
    async def publish(request: Request, x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        return call(approved_qa.publish, await body(request), actor())

    @router.get("/{qa_id}")
    def detail(qa_id: str, x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        return call(approved_qa.detail, qa_id)

    @router.post("/{qa_id}/archive")
    def archive(qa_id: str, x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        return call(approved_qa.archive_or_restore, qa_id, actor())

    @router.post("/{qa_id}/restore")
    def restore(qa_id: str, x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        return call(approved_qa.archive_or_restore, qa_id, actor(), restore=True)

    @router.post("/{qa_id}/restore/{version_id}")
    def restore_version(qa_id: str, version_id: int, x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        return call(approved_qa.archive_or_restore, qa_id, actor(), restore=True, version_id=version_id)

    return router


HTML = r'''
<div id="tab-approved-qa" class="panel-card" data-group="approved_qa" style="display:none;">
 <div class="admin-page-heading"><div><h2>Approved Q&amp;A</h2><p>Only client-approved answers are managed here. Preview your changes before publishing.</p></div><button class="btn-secondary" onclick="aqEdit()">+ Add Approved Q&amp;A</button></div>
 <div id="aqMessage" role="status" class="result-box" style="display:none;"></div>
 <div id="aqCounts" class="admin-kpi-grid"></div>
 <div style="display:flex;gap:10px;flex-wrap:wrap;margin:16px 0;">
  <input id="aqSearch" aria-label="Search approved questions and answers" placeholder="Search Approved Q&A" oninput="aqRender()" class="field-control">
  <button class="btn-secondary" onclick="aqFilter('ACTIVE')">Active</button><button class="btn-secondary" onclick="aqFilter('SCHEDULED')">Scheduled</button><button class="btn-secondary" onclick="aqFilter('EXPIRING_SOON')">Expiring Soon</button><button class="btn-secondary" onclick="aqFilter('ARCHIVED')">Archived / Expired</button>
 </div>
 <p id="aqFilterLabel">Active</p><div id="aqList"></div>
 <details style="margin-top:18px"><summary>Test a customer question (nothing is sent)</summary><label for="aqTestQuestion">Customer wording</label><input id="aqTestQuestion" class="field-control"><button class="btn-secondary" onclick="aqTest()">Test</button><pre id="aqTestResult" style="white-space:pre-wrap"></pre></details>
 <dialog id="aqDialog" style="width:min(800px,95vw);max-height:90vh;overflow:auto;border:1px solid #ddd;border-radius:12px;padding:24px;">
  <button class="btn-secondary" style="float:right" onclick="document.getElementById('aqDialog').close()">Close</button><div id="aqDialogContent"></div>
 </dialog>
</div>
'''

SCRIPT = r'''
<script>
let aqRecords = [], aqStatus = 'ACTIVE', aqDraft = null, aqToken = null, aqCreating = false;
function aqMessage(text, error=false) { const el=document.getElementById('aqMessage');el.style.display='block';el.className='result-box '+(error?'error':'success');el.textContent=text; }
async function aqApi(path='', method='GET', payload) {
 const response=await fetch('/admin/approved-qa'+path,{method,headers:{...getAuthHeaders(),'Content-Type':'application/json'},...(payload?{body:JSON.stringify(payload)}:{}),cache:'no-store'});
 const data=await response.json();if(!response.ok)throw new Error(data.detail||'Approved Q&A request failed');return data;
}
function aqDate(value) { return value?new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',hour12:true}).format(new Date(value))+' IST':'No limit'; }
function aqLocal(value) { if(!value)return '';const date=new Date(value);return new Date(date.getTime()+330*60000).toISOString().slice(0,19); }
async function loadApprovedQa() { if(!adminAuthenticated)return;try { const data=await aqApi();aqRecords=data.records;document.getElementById('aqCounts').innerHTML=Object.entries(data.counts).map(([key,count])=>`<div class="admin-kpi"><div class="admin-kpi-label">${escapeHtml(key.replaceAll('_',' '))}</div><div class="admin-kpi-value">${count}</div></div>`).join('');aqRender(); }catch(e){aqMessage(e.message,true);} }
function aqFilter(value) { aqStatus=value;document.getElementById('aqFilterLabel').textContent=value==='ARCHIVED'?'Archived / Expired':value.replaceAll('_',' ');aqRender(); }
function aqRender() {
 const search=document.getElementById('aqSearch').value.toLowerCase();
 const rows=aqRecords.filter(r=>(aqStatus==='EXPIRING_SOON'?r.expiring_soon:aqStatus==='ARCHIVED'?['ARCHIVED','EXPIRED'].includes(r.status):r.status===aqStatus)&&[r.question,r.answer,r.context,...r.aliases].join(' ').toLowerCase().includes(search));
 document.getElementById('aqList').innerHTML=rows.map(r=>`<article style="border:1px solid var(--border);padding:18px;border-radius:10px;margin-bottom:12px"><h3>${escapeHtml(r.question)}</h3><p style="white-space:pre-wrap">${escapeHtml(r.answer.slice(0,240))}${r.answer.length>240?'…':''}</p><p>${escapeHtml(r.context)} · ${escapeHtml(r.status)} · ${r.variation_count} question variations</p><p>Valid from: ${aqDate(r.valid_from)}<br>Valid until: ${aqDate(r.valid_until)}</p><button class="btn-secondary" data-aq-action="view" data-aq-id="${escapeHtml(r.qa_id)}">View</button> <button class="btn-secondary" data-aq-action="edit" data-aq-id="${escapeHtml(r.qa_id)}">Edit</button> <button class="btn-secondary" data-aq-action="archive" data-aq-id="${escapeHtml(r.qa_id)}">Delete</button>${r.status==='ARCHIVED'?` <button class="btn-secondary" data-aq-action="restore" data-aq-id="${escapeHtml(r.qa_id)}">Restore</button>`:''}</article>`).join('')||'<p>No approved Q&A in this view.</p>';
 document.querySelectorAll('[data-aq-action]').forEach(button=>button.onclick=()=>{const id=button.dataset.aqId;({view:aqView,edit:aqEdit,archive:aqArchive,restore:aqRestore})[button.dataset.aqAction](id);});
}
async function aqView(id) { try { const r=await aqApi('/'+encodeURIComponent(id));document.getElementById('aqDialogContent').innerHTML=`<h3>${escapeHtml(r.question)}</h3><pre style="white-space:pre-wrap">${escapeHtml(r.answer)}</pre><p>Context: ${escapeHtml(r.context)} · ${escapeHtml(r.status)}</p><p>From ${aqDate(r.valid_from)}<br>Until ${aqDate(r.valid_until)}</p><p>Variations: ${escapeHtml([...r.source_aliases,...r.aliases].join(' / ')||'None')}</p><details><summary>Read-only assets &amp; metadata</summary><pre style="white-space:pre-wrap">${escapeHtml(JSON.stringify({qa_id:r.qa_id,assets:r.asset_keys,created_at:r.created_at,created_by:r.created_by,updated_at:r.updated_at,updated_by:r.updated_by},null,2))}</pre></details><h4>Version history</h4>${r.versions.map(v=>`<details><summary>Version ${v.id} · ${aqDate(v.changed_at)} · ${escapeHtml(v.changed_by)} · ${escapeHtml(v.change_reason)}</summary><pre style="white-space:pre-wrap">${escapeHtml(JSON.stringify(v.effective_snapshot,null,2))}</pre><button class="btn-secondary" data-aq-version="${v.id}">Restore this version</button></details>`).join('')||'<p>Canonical source; no Admin changes.</p>'}`;document.querySelectorAll('[data-aq-version]').forEach(b=>b.onclick=()=>aqRestore(id,b.dataset.aqVersion));document.getElementById('aqDialog').showModal(); }catch(e){aqMessage(e.message,true);} }
function aqEdit(id) {
 aqDraft=id?aqRecords.find(r=>r.qa_id===id):null;aqToken=null;aqCreating=!id;
 document.getElementById('aqDialogContent').innerHTML='<h3>'+ (id?'Edit':'Add')+' Approved Q&amp;A</h3><form id="aqForm" onsubmit="event.preventDefault();aqPreview()"><label for="aqQuestion">Question *</label><textarea id="aqQuestion" required class="field-control"></textarea><label for="aqAnswer">Approved answer *</label><textarea id="aqAnswer" required rows="9" class="field-control"></textarea><label for="aqContext">Category / context</label><input id="aqContext" class="field-control"><label for="aqAliases">Question variations (one per line)</label><textarea id="aqAliases" rows="3" class="field-control"></textarea><label for="aqFrom">Valid From (Asia/Kolkata)</label><input id="aqFrom" type="datetime-local" step="1" class="field-control"><label for="aqUntil">Valid Until (Asia/Kolkata)</label><input id="aqUntil" type="datetime-local" step="1" class="field-control"><p>Asset references stay read-only.</p><button class="btn-secondary" type="submit">Preview / Test</button> <button id="aqPublishButton" class="btn-secondary" type="button" disabled onclick="aqPublish()">Publish Approved Q&amp;A</button><div id="aqPreviewResult" role="status"></div></form>';
 const r=aqDraft||{};document.getElementById('aqQuestion').value=r.question||'';document.getElementById('aqAnswer').value=r.answer||'';document.getElementById('aqContext').value=r.context||'general';document.getElementById('aqAliases').value=(r.aliases||[]).join('\n');document.getElementById('aqFrom').value=aqLocal(r.valid_from);document.getElementById('aqUntil').value=aqLocal(r.valid_until);
 document.getElementById('aqForm').oninput=()=>{aqToken=null;document.getElementById('aqPublishButton').disabled=true;};document.getElementById('aqDialog').showModal();
}
function aqPayload() { return {create:aqCreating,...(aqDraft?.qa_id?{qa_id:aqDraft.qa_id}:{}),question:document.getElementById('aqQuestion').value,answer:document.getElementById('aqAnswer').value,context:document.getElementById('aqContext').value,aliases:document.getElementById('aqAliases').value.split('\n').map(v=>v.trim()).filter(Boolean),valid_from:document.getElementById('aqFrom').value||null,valid_until:document.getElementById('aqUntil').value||null}; }
async function aqPreview() { try { const data=await aqApi('/preview','POST',aqPayload());aqDraft={...(aqDraft||{}),qa_id:data.qa_id};aqToken=data.preview_token;document.getElementById('aqPublishButton').disabled=false;document.getElementById('aqPreviewResult').innerHTML=`<h4>Current approved answer</h4><pre style="white-space:pre-wrap">${escapeHtml(data.current?.answer||'New Q&A')}</pre><h4>Proposed approved answer</h4><pre style="white-space:pre-wrap">${escapeHtml(data.proposed.answer)}</pre><p>${escapeHtml(data.proposed.status)} · From ${aqDate(data.proposed.valid_from)} · Until ${aqDate(data.proposed.valid_until)}</p><h4>Test result</h4><pre style="white-space:pre-wrap">${escapeHtml(JSON.stringify(data.test,null,2))}</pre>`; }catch(e){aqToken=null;document.getElementById('aqPublishButton').disabled=true;document.getElementById('aqPreviewResult').textContent=e.message;} }
async function aqPublish() { if(!aqToken)return;try {await aqApi('/publish','POST',{...aqPayload(),preview_token:aqToken});document.getElementById('aqDialog').close();aqMessage('Approved Q&A published.');await loadApprovedQa();}catch(e){document.getElementById('aqPreviewResult').textContent=e.message;aqToken=null;document.getElementById('aqPublishButton').disabled=true;} }
async function aqArchive(id) { if(!confirm('Delete this approved Q&A from customer matching? It will be archived with history preserved.'))return;try{await aqApi('/'+encodeURIComponent(id)+'/archive','POST');aqMessage('Q&A archived. Restore is available in history.');await loadApprovedQa();}catch(e){aqMessage(e.message,true);} }
async function aqRestore(id,version) { if(!confirm('Restore this approved Q&A? Its validity and conflicts will be checked.'))return;try{await aqApi('/'+encodeURIComponent(id)+'/restore'+(version?'/'+version:''),'POST');document.getElementById('aqDialog').close();aqMessage('Approved Q&A restored.');await loadApprovedQa();}catch(e){aqMessage(e.message,true);} }
async function aqTest() { try{const result=await aqApi('/test','POST',{message:document.getElementById('aqTestQuestion').value});document.getElementById('aqTestResult').textContent=JSON.stringify(result,null,2);}catch(e){document.getElementById('aqTestResult').textContent=e.message;} }
</script>
'''
