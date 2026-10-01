"""Authenticated Admin API and always-visible UI for Instagram chatbot mode."""
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from camellia import instagram_chatbot_control as control

log = logging.getLogger(__name__)


def create_router(require_admin, actor):
    router = APIRouter(prefix="/admin/chatbot-control")
    existing_actor = actor

    def actor():
        from camellia.app import _admin_context
        return (_admin_context.get() or {}).get("username") or existing_actor()

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "Test account not found") from exc
        except Exception as exc:
            log.exception("Instagram chatbot control operation failed")
            raise HTTPException(503, "Instagram chatbot control storage is unavailable") from exc

    async def body(request: Request):
        try:
            value = await request.json()
        except Exception as exc:
            raise HTTPException(400, "Invalid JSON body") from exc
        if not isinstance(value, dict):
            raise HTTPException(400, "Expected a JSON object")
        return value

    @router.get("")
    def summary(x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        return call(control.admin_summary)

    @router.post("/mode")
    async def mode(request: Request, x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        data = await body(request)
        return call(control.set_mode, data.get("mode"), actor())

    @router.get("/test-accounts")
    def test_accounts(x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        state = call(control.get_state, strict=True)
        return {"items": state["test_accounts"], "count": state["tester_count"]}

    @router.post("/test-accounts")
    async def add_test_account(request: Request, x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        data = await body(request)
        return call(
            control.add_test_account,
            sender_id=data.get("sender_id"),
            username=data.get("username"),
            display_name=data.get("display_name"),
            actor=actor(),
        )

    @router.post("/test-accounts/remove")
    async def remove_test_account(request: Request, x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        data = await body(request)
        return call(control.remove_test_account, data.get("sender_id"), actor())

    @router.get("/recent-accounts")
    def recent_accounts(x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        return {"items": call(control.recent_accounts)}

    @router.get("/audit")
    def audit(x_admin_key: Optional[str] = Header(None)):
        require_admin(x_admin_key)
        return {"items": call(control.audit_log)}

    return router


SCRIPT = r'''
<script>
let ccState = null, ccWasAuthenticated = false;
function ccMask(value) {
 const v=String(value||''); if(v.length<=8)return v; return v.slice(0,4)+'…'+v.slice(-4);
}
function ccDate(value) {
 if(!value)return '—';
 try{return new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',hour12:true}).format(new Date(value))+' IST';}
 catch(_e){return value;}
}
function ensureChatbotControlCard() {
 if(document.getElementById('instagramChatbotControlCard'))return;
 const header=document.querySelector('.admin-header'); if(!header)return;
 const card=document.createElement('section'); card.id='instagramChatbotControlCard';
 card.setAttribute('aria-label','Instagram Chatbot control'); card.style.display='none';
 card.style.margin='14px 24px 0'; card.style.padding='16px 18px'; card.style.border='1px solid var(--border)';
 card.style.borderRadius='12px'; card.style.background='var(--card-bg, #fff)'; card.style.boxShadow='0 4px 16px rgba(0,0,0,.05)';
 card.innerHTML=`
  <div style="display:flex;gap:16px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap">
   <div><div style="font-size:.78rem;text-transform:uppercase;letter-spacing:.08em;opacity:.68">Instagram Chatbot</div>
    <div id="ccMode" style="font-size:1.2rem;font-weight:700;margin-top:4px">Loading…</div>
    <div id="ccDescription" style="margin-top:4px;opacity:.8"></div>
    <div id="ccMeta" style="font-size:.88rem;margin-top:8px;opacity:.68"></div>
    <div id="ccWarning" style="display:none;margin-top:10px;padding:9px 11px;border:1px solid #d6a100;border-radius:8px">No test accounts are allowed. The chatbot will remain silent to all Instagram customers until you add a tester.</div>
   </div>
   <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <button id="ccToggle" class="btn-secondary" type="button" onclick="ccToggleMode()">Change Mode</button>
    <button class="btn-secondary" type="button" onclick="ccOpenManager()">Manage Test Accounts</button>
   </div>
  </div>
  <dialog id="ccDialog" style="width:min(860px,95vw);max-height:88vh;overflow:auto;border:1px solid #ddd;border-radius:12px;padding:24px">
   <button class="btn-secondary" style="float:right" onclick="document.getElementById('ccDialog').close()">Close</button>
   <h3>Instagram Chatbot — Test Accounts</h3>
   <p>Test access is bound to the exact Meta sender identity. After changing the connected Instagram business account, send a new DM from the tester and verify/re-add the tester if necessary.</p>
   <div id="ccManagerMessage" role="status"></div>
   <h4>Allowed Test Accounts</h4><div id="ccAllowed"></div>
   <h4 style="margin-top:20px">Add manually</h4>
   <div style="display:grid;grid-template-columns:1fr 1fr auto;gap:8px;align-items:end">
    <label>Exact Meta Sender ID<input id="ccManualId" class="field-control" placeholder="Exact sender ID"></label>
    <label>Username / label (optional)<input id="ccManualUsername" class="field-control" placeholder="@username"></label>
    <button class="btn-secondary" type="button" onclick="ccAddManual()">Add</button>
   </div>
   <p style="font-size:.88rem;opacity:.7">Username-only authorization is never trusted unless it resolves uniquely to an existing Recent Instagram Account.</p>
   <h4 style="margin-top:20px">Recent Instagram Accounts</h4><div id="ccRecent">Loading…</div>
   <details style="margin-top:20px"><summary>Control audit</summary><div id="ccAudit">Loading…</div></details>
  </dialog>`;
 header.insertAdjacentElement('afterend',card);
}
async function ccApi(path='',method='GET',payload) {
 const response=await fetch('/admin/chatbot-control'+path,{method,headers:{...getAuthHeaders(),'Content-Type':'application/json'},...(payload?{body:JSON.stringify(payload)}:{}),cache:'no-store'});
 let data={}; try{data=await response.json();}catch(_e){}
 if(!response.ok)throw new Error(data.detail||'Instagram chatbot control request failed'); return data;
}
function ccRender() {
 ensureChatbotControlCard(); const card=document.getElementById('instagramChatbotControlCard'); if(!card||!ccState)return;
 const live=ccState.mode==='LIVE';
 card.style.borderColor=live?'#2e7d32':'#b45309'; card.style.background=live?'#f0f8f1':'#fff7e8';
 document.getElementById('ccMode').style.color=live?'#216329':'#984706';
 document.getElementById('ccMode').textContent=live?'● ON — LIVE':'● OFF — TEST ACCOUNTS ONLY';
 document.getElementById('ccDescription').textContent=live?'Chatbot can respond to all Instagram customers.':'Only explicitly allowed test accounts can receive chatbot replies. Other Instagram customers receive no bot response.';
 document.getElementById('ccMeta').textContent=`Allowed testers: ${ccState.tester_count||0} · Last changed: ${ccDate(ccState.updated_at)} · By: ${ccState.updated_by||'—'}`;
 document.getElementById('ccToggle').textContent=live?'Switch to OFF / Test Only':'Turn ON / Go Live';
 document.getElementById('ccWarning').style.display=(!live && !ccState.tester_count)?'block':'none';
}
async function loadChatbotControl() {
 ensureChatbotControlCard();
 if(typeof adminAuthenticated==='undefined'||!adminAuthenticated)return;
 const card=document.getElementById('instagramChatbotControlCard'); if(card)card.style.display='block';
 try{ccState=await ccApi();ccRender();}catch(e){if(card){card.style.display='block';card.querySelector('#ccMode').textContent='Control unavailable';card.querySelector('#ccDescription').textContent=e.message;}}
}
async function ccToggleMode() {
 if(!ccState)return; const next=ccState.mode==='LIVE'?'TEST_ONLY':'LIVE';
 const question=next==='LIVE'?'Turn the Instagram chatbot ON for all customers?\n\nAll eligible Instagram customer DMs will enter the production chatbot.':'Switch to Test Accounts Only?\n\nOnly explicitly allowed Instagram test accounts will receive chatbot replies. All other customer DMs will remain unanswered by the bot.';
 if(!confirm(question))return;
 try{ccState=await ccApi('/mode','POST',{mode:next});ccRender();}catch(e){alert(e.message);}
}
async function ccOpenManager() {
 const dialog=document.getElementById('ccDialog'); dialog.showModal(); await ccLoadManager();
}
async function ccLoadManager() {
 const msg=document.getElementById('ccManagerMessage'); msg.textContent='';
 try{
  const [accounts,recent,audit]=await Promise.all([ccApi('/test-accounts'),ccApi('/recent-accounts'),ccApi('/audit')]);
  document.getElementById('ccAllowed').innerHTML=accounts.items.length?accounts.items.map(a=>`<div style="border:1px solid var(--border);border-radius:8px;padding:10px;margin:6px 0;display:flex;justify-content:space-between;gap:12px"><div><strong>${escapeHtml(a.username?'@'+a.username:(a.display_name||'Test account'))}</strong><br><small>Meta sender ID: ${escapeHtml(ccMask(a.sender_id))} · Added ${escapeHtml(ccDate(a.created_at))}</small></div><button class="btn-secondary" type="button" data-cc-remove="${escapeHtml(a.sender_id)}">Remove</button></div>`).join(''):'<p>No test accounts allowed.</p>';
  document.querySelectorAll('[data-cc-remove]').forEach(b=>b.onclick=()=>ccRemove(b.dataset.ccRemove));
  document.getElementById('ccRecent').innerHTML=recent.items.length?recent.items.map(a=>`<div style="border-bottom:1px solid var(--border);padding:9px 0;display:flex;justify-content:space-between;gap:12px"><div><strong>${escapeHtml(a.username?'@'+a.username:(a.display_name||'Instagram account'))}</strong>${a.username&&a.display_name?`<br><small>${escapeHtml(a.display_name)}</small>`:''}<br><small>${escapeHtml(ccMask(a.sender_id))} · Last seen ${escapeHtml(ccDate(a.last_seen_at))}</small></div>${a.allowed?'<span>Allowed ✓</span>':`<button class="btn-secondary" type="button" data-cc-allow="${escapeHtml(a.sender_id)}" data-cc-user="${escapeHtml(a.username||'')}">Allow for Testing</button>`}</div>`).join(''):'<p>No recent Instagram accounts yet. Send a DM to the connected business account; in Test Only mode the bot stays silent while the sender identity can appear here.</p>';
  document.querySelectorAll('[data-cc-allow]').forEach(b=>b.onclick=()=>ccAdd(b.dataset.ccAllow,b.dataset.ccUser));
  document.getElementById('ccAudit').innerHTML=audit.items.length?audit.items.map(a=>`<p><strong>${escapeHtml(a.action)}</strong> · ${escapeHtml(ccDate(a.created_at))} · ${escapeHtml(a.actor||'Admin')}${a.previous_value||a.new_value?`<br><small>${escapeHtml(a.previous_value||'')} → ${escapeHtml(a.new_value||'')}</small>`:''}${a.subject_label?`<br><small>@${escapeHtml(a.subject_label)}</small>`:''}</p>`).join(''):'<p>No control changes yet.</p>';
 }catch(e){msg.textContent=e.message;}
}
async function ccAdd(senderId,username='') {try{ccState=await ccApi('/test-accounts','POST',{sender_id:senderId,username});ccRender();await ccLoadManager();}catch(e){document.getElementById('ccManagerMessage').textContent=e.message;}}
async function ccAddManual() {const id=document.getElementById('ccManualId').value.trim(),username=document.getElementById('ccManualUsername').value.trim();if(!id&&!username){document.getElementById('ccManagerMessage').textContent='Enter an exact Meta sender ID or a username visible in Recent Instagram Accounts.';return;}try{ccState=await ccApi('/test-accounts','POST',{sender_id:id||null,username:username||null});document.getElementById('ccManualId').value='';document.getElementById('ccManualUsername').value='';ccRender();await ccLoadManager();}catch(e){document.getElementById('ccManagerMessage').textContent=e.message;}}
async function ccRemove(senderId) {if(!confirm('Remove this account from Test Only access?'))return;try{ccState=await ccApi('/test-accounts/remove','POST',{sender_id:senderId});ccRender();await ccLoadManager();}catch(e){document.getElementById('ccManagerMessage').textContent=e.message;}}
function ccSyncAuth() {
 ensureChatbotControlCard(); const card=document.getElementById('instagramChatbotControlCard');
 const authenticated=(typeof adminAuthenticated!=='undefined'&&adminAuthenticated);
 if(card)card.style.display=authenticated?'block':'none';
 if(authenticated&&!ccWasAuthenticated)loadChatbotControl();
 if(!authenticated){ccState=null;document.getElementById('ccDialog')?.close();}
 ccWasAuthenticated=authenticated;
}
ensureChatbotControlCard(); ccSyncAuth(); setInterval(ccSyncAuth,1000);
</script>
'''
