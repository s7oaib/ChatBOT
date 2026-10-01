"""Small client-facing Weekly Savoury panel over the existing review timeline."""

WEEKLY_HTML = r'''
<style>
  #weeklySavouryPanel{max-width:850px;margin:0 auto 34px;padding:0 0 30px;border-bottom:1px solid var(--border)}
  #weeklySavouryPanel [hidden]{display:none!important}
  #weeklySavouryPanel h2{margin:0;color:var(--primary)}
  #weeklySavouryPanel .ws-muted{color:var(--text-muted)}
  #weeklySavouryPanel .ws-status{padding:15px 18px;margin:18px 0;background:#fff;border:1px solid var(--border);border-radius:10px}
  #weeklySavouryPanel .ws-status p{margin:4px 0}
  #weeklySavouryPanel .ws-drop{position:relative;display:block;text-align:center;padding:32px 18px;border:2px dashed var(--border);border-radius:12px;background:#fcfaf7;min-height:150px}
  #weeklySavouryPanel .ws-drop:hover,#weeklySavouryPanel .ws-drop:focus-within{border-color:var(--primary)}
  #weeklySavouryPanel .ws-drop.is-dragging{border-color:var(--primary);background:#f5eef0}
  #weeklySavouryPanel .ws-drop:focus-within{outline:3px solid var(--primary);outline-offset:3px}
  #weeklySavouryPanel .ws-visually-hidden{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}
  #weeklySavouryPanel .ws-drop-actions{display:flex;justify-content:center;margin-top:14px}
  #weeklySavouryPanel .ws-drop-actions button{min-height:44px}
  #weeklySavouryPanel .ws-actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:14px 0}
  #weeklySavouryPanel .ws-actions button{min-height:44px}
  #weeklySavouryPanel .ws-dates{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
  #weeklySavouryPanel .ws-dates input{width:100%}
  #weeklySavouryPanel .ws-changes{background:#fff;border:1px solid var(--border);border-radius:10px;padding:15px;margin:16px 0}
  #weeklySavouryPanel .ws-changes p{margin:5px 0}
  #weeklySavouryPanel .ws-item{padding:7px 0;border-bottom:1px solid var(--border)}
  #weeklySavouryPanel .ws-warning{color:#7b3f00;background:#fff3e3;padding:12px;border-radius:8px}
  #weeklySavouryPanel .ws-review-table{overflow-x:auto}
  #weeklySavouryPanel .ws-review-table table{width:100%}
  #weeklySavouryPanel .ws-review-table input,#weeklySavouryPanel .ws-review-table select{max-width:100%;min-width:85px}
  #weeklySavouryPanel .ws-version{padding:10px 0;border-bottom:1px solid var(--border)}
  #weeklySavouryPanel .ws-version button{min-height:44px}
  #weeklySavouryPanel .ws-preview{padding:14px;background:#fff;border:1px solid var(--border);border-radius:8px;white-space:pre-wrap}
  #weeklySavouryPanel .ws-issues{padding:14px 16px;margin:14px 0;background:#fff3e3;border:1px solid #d7aa77;border-radius:8px}
  #weeklySavouryPanel .ws-issues h4{margin:0 0 8px}
  #weeklySavouryPanel .ws-issues ul{margin:0;padding-left:20px}
  #weeklySavouryPanel .ws-issues li{margin:5px 0}
  #weeklySavouryPanel .ws-success{padding:12px 16px;margin:12px 0;background:#edf6ee;border:1px solid #86af8a;border-radius:8px}
  #weeklySavouryPanel .ws-publish-dialog{width:min(480px,calc(100vw - 28px));max-height:calc(100vh - 32px);overflow-y:auto;margin:auto;padding:24px;border:1px solid var(--border);border-radius:10px;color:var(--text)}
  #weeklySavouryPanel .ws-publish-dialog::backdrop{background:#0008}
  #weeklySavouryPanel .ws-publish-dialog h3{margin:0 0 12px;color:var(--primary)}
  #weeklySavouryPanel .ws-publish-dialog ul{padding-left:20px}
  #weeklySavouryPanel .ws-publish-dialog-actions{display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap;margin-top:20px}
  #weeklySavouryPanel .ws-publish-dialog-actions button{min-height:44px}
  @media(max-width:600px){#weeklySavouryPanel .ws-dates{grid-template-columns:1fr}#weeklySavouryPanel .ws-review-table{overflow:visible}#weeklySavouryPanel .ws-review-table thead{display:none}#weeklySavouryPanel .ws-review-table tr{display:block;border:1px solid var(--border);border-radius:10px;padding:10px;margin-bottom:10px}#weeklySavouryPanel .ws-review-table td{display:block;border:0;padding:6px}#weeklySavouryPanel .ws-review-table td:before{content:attr(data-label);display:block;font-size:.75rem;color:var(--text-muted)}#weeklySavouryPanel .ws-review-table input,#weeklySavouryPanel .ws-review-table select{width:100%}}
</style>
<section id="weeklySavouryPanel" aria-labelledby="wsHeading">
  <h2 id="wsHeading">Weekly Savoury Menu</h2>
  <p class="ws-muted">Update this week's menu or prepare the next one.</p>
  <div id="wsSuccess" class="ws-success" role="status" hidden></div>
  <nav class="ws-actions" aria-label="Weekly menu timeline"><button type="button" class="btn-secondary" data-ws-pane="current" aria-selected="true" onclick="wsShowTimeline('current')">Current</button><button type="button" class="btn-secondary" data-ws-pane="upcoming" aria-selected="false" onclick="wsShowTimeline('upcoming')">Upcoming</button><button type="button" class="btn-secondary" data-ws-pane="history" aria-selected="false" onclick="wsShowTimeline('history')">History</button></nav>
  <div id="wsTimelineCurrent"><div id="wsState" class="ws-status" aria-live="polite">Loading weekly menu…</div></div>
  <div id="wsTimelineUpcoming" hidden><h3>Scheduled menus</h3><div id="wsUpcomingVersions"></div></div>
  <div id="wsReminder" class="ws-warning" hidden></div>
  <div id="wsDrop" class="ws-drop"><strong>Drop this week's menu here</strong><br>
    Drag &amp; Drop, Paste image, or choose a file<br><span class="ws-muted">JPG, PNG, WEBP or PDF · Maximum 10 MB</span>
    <div class="ws-drop-actions"><button id="wsChooseFile" type="button" class="btn-secondary" onclick="document.getElementById('wsFile').click()">Choose Menu File</button></div>
    <label id="wsFileLabel" class="ws-visually-hidden" for="wsFile">Weekly menu file to extract</label>
    <input id="wsFile" class="ws-visually-hidden" type="file" accept="image/jpeg,image/png,image/webp,application/pdf" aria-labelledby="wsFileLabel" onchange="wsFileSelected()"></div>
  <div class="ws-actions"><button type="button" class="btn-secondary" onclick="wsTogglePaste()">Paste Menu Text</button></div>
  <div id="wsPastePane" hidden><label for="wsText">Paste menu text</label>
    <textarea id="wsText" class="field-control" rows="6" maxlength="50000"></textarea>
    <button type="button" class="btn-primary" onclick="wsExtractText()">Extract pasted menu</button></div>
  <div id="wsReview" hidden>
    <p id="wsNotLive" class="ws-warning" role="status"></p>
    <h3 id="wsReviewHeading">Confirm Weekly Menu</h3>
    <p id="wsReviewCount"></p>
    <img id="wsSource" alt="Uploaded weekly menu preview" style="max-width:100%" hidden>
    <h4>Menu dates</h4>
    <div class="ws-dates"><label>Valid from<input id="wsFrom" class="field-control" type="date" onchange="wsRefreshAnalysis()"></label>
      <label>Valid until<input id="wsUntil" class="field-control" type="date" onchange="wsRefreshAnalysis()"></label></div>
    <p id="wsNoDate" class="ws-warning" hidden>Confirm dates before publishing, or explicitly choose the 7-day fallback below.</p>
    <label><input id="wsUndated" type="checkbox" onchange="wsRefreshAnalysis()"> Use 7-day fallback without printed dates</label>
    <div id="wsValidation" class="ws-issues" role="alert" tabindex="-1" hidden></div>
    <div id="wsChanges" class="ws-changes"></div>
    <h4>Extracted items</h4><p class="ws-muted">Check names, quantities and prices against the source before confirming.</p>
    <div id="wsExtractedRows"></div>
    <div id="wsWarnings" class="ws-warning" hidden></div>
    <div id="wsNeedsReview" hidden><h4>Needs Review</h4><p>Correct unclear details before publishing. Leave facts blank if not stated.</p>
      <div class="ws-review-table"><table><thead><tr><th>Item</th><th>Section</th><th>Quantity</th><th>Price (₹)</th><th>Description</th><th>Aliases</th><th>Action</th></tr></thead><tbody id="wsRows"></tbody></table></div>
      <div class="ws-actions"><button type="button" class="btn-secondary" onclick="wsAddRow()">Add item</button>
        <button type="button" class="btn-secondary" onclick="wsSaveCorrections()">Save Draft</button>
        <button type="button" class="btn-secondary" onclick="wsRetryExtraction()">Retry Extraction</button></div></div>
    <h4>Preview customer response</h4><p class="ws-muted">Preview only — not sent to customers.</p>
    <input id="wsQuestion" class="field-control" value="What's on the Savoury menu this week?" aria-label="Preview question">
    <div class="ws-actions"><button type="button" class="btn-secondary" onclick="wsPreview()">Preview response</button></div>
    <div id="wsPreviewError" class="ws-issues" role="alert" tabindex="-1" hidden></div>
    <div id="wsPreviewResult" class="ws-preview" hidden></div>
    <div id="wsSafetyChecks">
      <label id="wsSuspiciousLabel" hidden><input id="wsConfirmSuspicious" type="checkbox"> I checked the unusual price or quantity against the source.</label>
      <label id="wsOverlapLabel" hidden><input id="wsConfirmOverlap" type="checkbox"> Replace the overlapping weekly menu shown above.</label>
    </div>
    <div class="ws-actions"><button id="wsPublishButton" type="button" class="btn-primary" onclick="wsPublish()">Update Weekly Menu</button>
      <button type="button" class="btn-secondary" onclick="wsReject()">Discard draft</button></div>
    <div id="wsPublishError" class="ws-issues" role="alert" tabindex="-1" hidden></div>
  </div>
  <div id="wsFailed" class="ws-warning" hidden>We couldn't read this menu reliably. The published menu is unchanged.
    <button type="button" class="btn-secondary" onclick="wsReject()">Discard failed draft</button></div>
  <div id="wsMessage" role="status" aria-live="polite"></div>
  <div id="wsTimelineHistory"><div id="wsHistory"><h3>Previous weekly menus</h3><div id="wsVersions"></div></div></div>
  <dialog id="wsPublishDialog" class="ws-publish-dialog" aria-labelledby="wsPublishDialogTitle">
    <h3 id="wsPublishDialogTitle">Update Weekly Savoury Menu</h3>
    <p id="wsPublishSummary"></p>
    <ul><li>Menu reviewed</li><li>Dates confirmed</li><li>Flyer and facts will be published together</li></ul>
    <div id="wsModalError" class="ws-issues" role="alert" tabindex="-1" hidden></div>
    <div class="ws-publish-dialog-actions"><button type="button" class="btn-secondary" onclick="wsCancelPublish()">Cancel</button>
      <button id="wsConfirmPublishButton" type="button" class="btn-primary" onclick="wsConfirmPublish()">Update Menu</button></div>
  </dialog>
</section>
'''

WEEKLY_SCRIPT = r'''
<script>
let wsDraft=null,wsAnalysis=null,wsToday='',wsBusy=false,wsDirty=false,wsPreviewUrl=null,wsPublishOptions=null,wsPublishOpener=null;
function wsMessage(message){document.getElementById('wsMessage').textContent=message;}
function wsIssue(boxId,title,reasons,focus=false){const box=document.getElementById(boxId);box.replaceChildren();box.hidden=!reasons.length;if(!reasons.length)return;
  const heading=document.createElement('h4');heading.textContent=title;box.appendChild(heading);
  const list=document.createElement('ul');for(const reason of reasons){const item=document.createElement('li');item.textContent=reason;list.appendChild(item);}box.appendChild(list);
  if(focus){box.scrollIntoView({block:'center'});box.focus();}}
function wsBlockingReasons(analysis,options,preview=false){const errors=[...(analysis?.errors||[])];
  const reasons=preview?errors.filter(error=>!error.startsWith('Unusual price change')&&!error.startsWith('Unusual quantity change')):
    errors.filter(error=>!options.confirm_suspicious||!error.startsWith('Unusual price change')&&!error.startsWith('Unusual quantity change'));
  if(!analysis?.comparison_available)reasons.push('Enter at least one valid menu item before continuing.');
  if(!preview){if(wsDraft?.warnings?.length)reasons.push(...wsDraft.warnings);
    if(analysis?.overlaps?.length&&(!options.replace_version_id||analysis.overlaps.length!==1))reasons.push('Confirm the overlapping weekly menu replacement first.');
    if(options.replace_version_id&&!analysis?.overlaps?.length)reasons.push('The overlapping weekly version changed. Reload before publishing.');}
  return [...new Set(reasons)];}
document.getElementById('wsPublishDialog').addEventListener('close',()=>{wsPublishOptions=null;const opener=wsPublishOpener;wsPublishOpener=null;if(opener?.isConnected)opener.focus();});
document.getElementById('wsPublishDialog').addEventListener('cancel',event=>{if(wsBusy)event.preventDefault();});
function wsOptions(){return {draft_id:wsDraft?.draft_id,revision:wsDraft?.revision,
  valid_from:document.getElementById('wsFrom').value,valid_until:document.getElementById('wsUntil').value,
  accept_undated:document.getElementById('wsUndated').checked,
  confirm_suspicious:document.getElementById('wsConfirmSuspicious').checked,
  replace_version_id:document.getElementById('wsConfirmOverlap').checked?wsAnalysis?.overlaps?.[0]?.id:null};}
async function wsLoad(){
  try{const data=await mrRequest('/admin/weekly-savoury');wsToday=data.today;
    const state=document.getElementById('wsState');state.replaceChildren();
    const addLine=(value)=>{const p=document.createElement('p');p.textContent=value;state.appendChild(p);};
    addLine(data.current?'LIVE / CURRENT · '+(data.current.display_date_range||'Current menu')+' · '+data.current.item_count+' items':'No current Weekly Savoury menu');
    if(data.upcoming.length)addLine('PUBLISHED — UPCOMING · '+data.upcoming.map(item=>item.display_date_range).join(', '));
    else if(!data.current)addLine('Upload this week’s menu to begin.');
    const reminder=document.getElementById('wsReminder');reminder.hidden=!data.reminder;reminder.textContent=data.reminder||'';
    const history=document.getElementById('wsVersions');history.replaceChildren();
    const upcoming=document.getElementById('wsUpcomingVersions');upcoming.replaceChildren();
    for(const version of data.versions){
      const line=document.createElement('div');line.className='ws-version';
      const label=document.createElement('p');label.textContent=(version.display_date_range||'Undated')+' · '+version.status+' · '+version.item_count+' items';line.appendChild(label);
      const detail=document.createElement('small');detail.textContent='Version '+version.version_id+' · Approved by '+(version.approved_by||'Admin')+' · '+version.changed_at+' · '+(version.source_type==='text'?'Pasted text':'Menu flyer');line.appendChild(detail);
      if(version.has_source){const source=document.createElement('button');source.type='button';source.className='btn-secondary';source.textContent='Source flyer';source.onclick=()=>mrPreviewVersionSource(version.menu_key,version.version_id,source);line.appendChild(source);}
      if(version.status!=='CURRENT'){const restore=document.createElement('button');restore.type='button';restore.className='btn-secondary';restore.textContent='Restore previous weekly menu';restore.onclick=()=>wsRestore(version);line.appendChild(restore);}
      if(version.status==='UPCOMING')upcoming.appendChild(line);
      else if(version.status!=='CURRENT')history.appendChild(line);
    }
    if(!data.versions.some(version=>!['CURRENT','UPCOMING'].includes(version.status)))history.textContent='No previous weekly menus.';
    if(!data.upcoming.length)upcoming.textContent='No scheduled menus.';
    if(data.draft&&!['PUBLISHED','REJECTED'].includes(data.draft.status)){
      wsDraft=data.draft;wsAnalysis=data.draft.status==='FAILED'?null:(await mrRequest('/admin/weekly-savoury/analyse','POST',
        {draft_id:wsDraft.draft_id,revision:wsDraft.revision,...(wsDraft.weekly_dates||{})})).analysis;wsRenderReview();
    }else{wsDraft=null;wsAnalysis=null;document.getElementById('wsReview').hidden=true;document.getElementById('wsFailed').hidden=true;}
  }catch(error){wsMessage(error.message);}
}
function wsShowTimeline(pane){for(const [name,id] of [['current','wsTimelineCurrent'],['upcoming','wsTimelineUpcoming'],['history','wsTimelineHistory']])document.getElementById(id).hidden=name!==pane;document.querySelectorAll('[data-ws-pane]').forEach(button=>button.setAttribute('aria-selected',String(button.dataset.wsPane===pane)));}
function wsTogglePaste(){const pane=document.getElementById('wsPastePane');pane.hidden=!pane.hidden;if(!pane.hidden)document.getElementById('wsText').focus();}
async function wsFileSelected(file=document.getElementById('wsFile').files?.[0]){if(!file)return;const issue=mrUploadFileError(file);if(issue){wsMessage(issue);document.getElementById('wsFile').value='';return;}await wsExtractFile(file);}
async function wsExtractFile(file){if(wsBusy)return;const issue=mrUploadFileError(file);if(issue){wsMessage(issue);return;}wsBusy=true;try{const form=new FormData();form.append('file',file);wsMessage('Extracting '+file.name+' ('+mrFormatFileSize(file.size)+'). Your published menu is unchanged.');const data=await mrRequest('/admin/weekly-savoury/extract-file','POST',form);wsDraft=data.draft;wsAnalysis=data.analysis;wsDirty=false;wsRenderReview();}catch(error){wsMessage(error.message||'We could not read that file. Try a JPG, PNG, WEBP, or PDF under 10 MB.');}finally{wsBusy=false;document.getElementById('wsFile').value='';}}
async function wsExtractText(){if(wsBusy)return;const value=document.getElementById('wsText').value.trim();if(!value){wsMessage('Paste menu text first.');return;}wsBusy=true;try{wsMessage('Extracting menu details. Your published menu is unchanged.');const data=await mrRequest('/admin/weekly-savoury/extract-text','POST',{text:value});wsDraft=data.draft;wsAnalysis=data.analysis;wsDirty=false;wsRenderReview();}catch(error){wsMessage(error.message);}finally{wsBusy=false;}}
async function wsRetryExtraction(){if(!wsDraft||wsBusy||!confirm('Read the original source again? Corrections will be replaced, not published.'))return;wsBusy=true;try{const data=await mrRequest('/admin/weekly-savoury/retry-extraction','POST',{draft_id:wsDraft.draft_id,revision:wsDraft.revision});wsDraft=data.draft;wsAnalysis=data.analysis;wsDirty=false;wsRenderReview();wsMessage('Review the source again before approving.');}catch(error){wsMessage(error.message);}finally{wsBusy=false;}}
function wsRenderReview(options){
  const failed=!!wsDraft&&wsDraft.status==='FAILED';document.getElementById('wsFailed').hidden=!failed;
  const review=!!wsDraft&&!failed;document.getElementById('wsReview').hidden=!review;if(!review)return;
  document.getElementById('wsNotLive').textContent='NOT LIVE — '+(wsDraft.status==='READY_TO_APPROVE'?'READY TO APPROVE':'NEEDS REVIEW')+'. Uploading a menu does not publish it to customers. Review the extracted items, prices and dates, then publish.';
  document.getElementById('wsReviewCount').textContent=wsDraft.rows.length?'We found '+wsDraft.rows.length+' items.':"Extraction unsuccessful. We couldn't extract any menu items from this flyer. Your published menu is unchanged. You can retry extraction or enter the items manually.";
  document.getElementById('wsFrom').value=options?.valid_from??wsDraft.weekly_dates?.valid_from??'';
  document.getElementById('wsUntil').value=options?.valid_until??wsDraft.weekly_dates?.valid_until??'';
  document.getElementById('wsUndated').checked=!!options?.accept_undated;
  document.getElementById('wsNoDate').hidden=!!(document.getElementById('wsFrom').value&&document.getElementById('wsUntil').value);
  const preview=document.getElementById('wsSource');preview.hidden=true;
  if(wsPreviewUrl){URL.revokeObjectURL(wsPreviewUrl);wsPreviewUrl=null;}
  if(wsDraft.source_asset){const id=wsDraft.draft_id;fetch('/admin/menu-manager/menus/weekly_vegetarian/review/source/'+id,
    {headers:getAuthHeaders()}).then(response=>{if(!response.ok)throw Error('Flyer preview unavailable.');return response.blob();})
    .then(blob=>{if(wsDraft?.draft_id!==id)return;wsPreviewUrl=URL.createObjectURL(blob);preview.src=wsPreviewUrl;preview.hidden=false;}).catch(error=>wsMessage(error.message));}
  wsRenderAnalysis();
}
function wsPriceLabel(value){return value===null||value===undefined||value===''?'Price unclear':'₹'+value;}
function wsRenderAnalysis(){
  if(!wsAnalysis)return;
  const changes=wsAnalysis.changes;const box=document.getElementById('wsChanges');box.replaceChildren();
  const heading=document.createElement('strong');heading.textContent="What's changed?";box.appendChild(heading);
  if(!wsAnalysis.comparison_available){const note=document.createElement('p');note.textContent='Comparison unavailable until at least one valid menu item is entered.';box.appendChild(note);}
  else {
  const summary=document.createElement('p');summary.textContent=[['unchanged','unchanged'],['new','new'],['removed','removed'],['price_changed','price changes'],['quantity_changed','quantity changes']]
    .map(([key,label])=>changes[key].length+' '+label).join(' · ');box.appendChild(summary);
  for(const [key,label] of [['unchanged','UNCHANGED'],['new','NEW'],['removed','REMOVED'],['price_changed','PRICE CHANGED'],['quantity_changed','QUANTITY CHANGED'],['section_changed','SECTION CHANGED'],['suspicious_price','SUSPICIOUS PRICE'],['suspicious_quantity','SUSPICIOUS QUANTITY']]){
    for(const item of changes[key]||[]){const line=document.createElement('p');const value=key.includes('quantity')?item.before.quantity+' → '+item.after.quantity:key==='section_changed'?item.before.category+' → '+item.after.category:item.before?wsPriceLabel(item.before.price_inr)+' → '+wsPriceLabel(item.after.price_inr):wsPriceLabel(item.price_inr);line.textContent=label+' · '+(item.after?.item||item.item)+' · '+value;box.appendChild(line);}
  }
  }
  const warnings=document.getElementById('wsWarnings');warnings.hidden=true;warnings.textContent='';
  wsIssue('wsValidation','Needs attention before publishing',wsBlockingReasons(wsAnalysis,wsOptions()));
  const extracted=document.getElementById('wsExtractedRows');extracted.replaceChildren();
  for(const row of wsAnalysis.rows){const line=document.createElement('div');line.className='ws-item';line.textContent=(row.item||'Item unclear')+' · '+(row.quantity||'Quantity not stated')+' · '+
    (row.price_inr===null||row.price_inr===undefined||row.price_inr===''?'Price unclear':'₹'+row.price_inr);extracted.appendChild(line);}
  const detailed=!!wsAnalysis.needs_review;document.getElementById('wsNeedsReview').hidden=false;
  document.getElementById('wsReviewHeading').textContent=detailed?'Needs Review':'Confirm Weekly Menu';
  document.getElementById('wsSuspiciousLabel').hidden=!(changes.suspicious_price.length||changes.suspicious_quantity.length);
  document.getElementById('wsOverlapLabel').hidden=!wsAnalysis.overlaps.length;
  if(wsAnalysis.overlaps.length)document.getElementById('wsOverlapLabel').lastChild.textContent=' Replace existing schedule: '+wsAnalysis.overlaps.map(item=>item.date_range).join(', ');
  document.getElementById('wsPublishButton').textContent=wsAnalysis.valid_from&&wsAnalysis.valid_from>wsToday?'Schedule Weekly Menu':'Update Weekly Menu';
  document.getElementById('wsPublishButton').disabled=!wsAnalysis.comparison_available;
  wsRenderRows();
}
function wsRenderRows(){const body=document.getElementById('wsRows');body.replaceChildren();if(!wsDraft)return;
  wsDraft.rows.forEach((row,index)=>{const tr=document.createElement('tr');
    for(const field of ['item','category','quantity','price_inr','description','aliases']){
      const td=document.createElement('td');td.dataset.label=field==='price_inr'?'Price (₹)':field;
      const control=document.createElement(field==='category'?'select':'input');control.setAttribute('aria-label',field+' row '+(index+1));
      if(field==='category')for(const category of ['vegetarian','non_vegetarian','norom_pak_sondesh']){
        const option=document.createElement('option');option.value=category;option.textContent=category.replaceAll('_',' ');control.appendChild(option);}
      control.value=field==='aliases'?(row.aliases||[]).join(', '):row[field]??'';
      control.oninput=()=>{row[field]=field==='aliases'?control.value.split(',').map(item=>item.trim()).filter(Boolean):control.value;wsDirty=true;};
      td.appendChild(control);tr.appendChild(td);
    }
    const action=document.createElement('td');action.dataset.label='Action';const remove=document.createElement('button');remove.type='button';remove.className='btn-secondary';remove.textContent='Delete item';remove.onclick=()=>{wsDraft.rows.splice(index,1);wsDirty=true;wsRenderRows();};action.appendChild(remove);tr.appendChild(action);body.appendChild(tr);
  });
}
function wsAddRow(){if(!wsDraft)return;wsDraft.rows.push({item:'',category:'vegetarian',quantity:'',price_inr:'',description:'',aliases:[]});wsDirty=true;wsRenderRows();}
async function wsRefreshAnalysis(){if(!wsDraft||wsBusy)return;try{const result=await mrRequest('/admin/weekly-savoury/analyse','POST',wsOptions());wsAnalysis=result.analysis;document.getElementById('wsNoDate').hidden=!!(wsOptions().valid_from&&wsOptions().valid_until);wsRenderAnalysis();}catch(error){wsIssue('wsValidation','Needs attention before publishing',[error.message],true);}}
async function wsSaveCorrections(){if(!wsDraft||wsBusy)return;wsBusy=true;const options=wsOptions();try{const result=await mrRequest('/admin/weekly-savoury/draft','PUT',{...options,rows:wsDraft.rows});wsDraft=result.draft;wsAnalysis=result.analysis;wsDirty=false;wsRenderReview(options);wsMessage('Corrections saved. Published menu unchanged.');}catch(error){wsMessage(error.message);}finally{wsBusy=false;}}
async function wsPreview(){if(!wsDraft||wsBusy)return;
  if(wsDirty){wsIssue('wsPreviewError','Preview unavailable',['Save corrections before previewing.'],true);return;}
  try{const options=wsOptions();const result=await mrRequest('/admin/weekly-savoury/analyse','POST',options);wsAnalysis=result.analysis;wsRenderAnalysis();
    const reasons=wsBlockingReasons(wsAnalysis,options,true);if(reasons.length){wsIssue('wsPreviewError','Preview unavailable',reasons,true);return;}
    const preview=await mrRequest('/admin/weekly-savoury/preview','POST',{...options,question:document.getElementById('wsQuestion').value});
    wsIssue('wsPreviewError','Preview unavailable',[]);const box=document.getElementById('wsPreviewResult');box.textContent=preview.label+(preview.schedule_note?'\n'+preview.schedule_note:'')+'\n\n'+preview.answer;box.hidden=false;
  }catch(error){wsIssue('wsPreviewError','Preview unavailable',[error.message],true);}}
async function wsPublish(){if(!wsDraft||wsBusy)return;
  if(wsDirty){wsIssue('wsPublishError','Menu was not updated',['Save corrections before publishing.'],true);return;}
  const options=wsOptions();try{const result=await mrRequest('/admin/weekly-savoury/analyse','POST',options);wsAnalysis=result.analysis;wsRenderAnalysis();
    const reasons=wsBlockingReasons(wsAnalysis,options);if(reasons.length){wsIssue('wsPublishError','Needs attention before publishing',reasons,true);return;}
    wsIssue('wsPublishError','Needs attention before publishing',[]);wsPublishOptions=options;wsPublishOpener=document.getElementById('wsPublishButton');
    const scheduled=!!(wsAnalysis.valid_from&&wsAnalysis.valid_from>wsToday);document.getElementById('wsPublishDialogTitle').textContent=scheduled?'Schedule Weekly Savoury Menu':'Update Weekly Savoury Menu';
    document.getElementById('wsPublishSummary').textContent=wsAnalysis.rows.length+' items · '+(wsAnalysis.display_date_range||'7-day undated fallback');
    document.getElementById('wsConfirmPublishButton').textContent=scheduled?'Schedule Menu':'Update Menu';
    wsIssue('wsModalError','Menu was not updated',[]);document.getElementById('wsPublishDialog').showModal();document.getElementById('wsConfirmPublishButton').focus();
  }catch(error){wsIssue('wsPublishError','Needs attention before publishing',[error.message],true);}}
function wsCancelPublish(){if(!wsBusy)document.getElementById('wsPublishDialog').close();}
async function wsConfirmPublish(){if(wsBusy||!wsPublishOptions||!document.getElementById('wsPublishDialog').open)return;
  wsBusy=true;const button=document.getElementById('wsConfirmPublishButton');button.disabled=true;
  const scheduled=!!(wsAnalysis.valid_from&&wsAnalysis.valid_from>wsToday);
  try{await mrRequest('/admin/weekly-savoury/publish','POST',wsPublishOptions);document.getElementById('wsPublishDialog').close();
    await wsLoad();wsShowTimeline(scheduled?'upcoming':'current');
    const success=document.getElementById('wsSuccess');success.textContent=scheduled?'Weekly Savoury menu scheduled':'Weekly Savoury menu updated';success.hidden=false;success.scrollIntoView({block:'center'});
  }catch(error){const reasons=[error.message,'Your currently published menu has not changed.'];wsIssue('wsModalError','Menu was not updated',reasons,true);wsIssue('wsPublishError','Menu was not updated',reasons);}
  finally{wsBusy=false;button.disabled=false;}}
async function wsReject(){if(wsBusy||!wsDraft||!confirm('Discard Weekly Savoury draft #'+wsDraft.draft_id+'? The published menu will not change.'))return;wsBusy=true;wsMessage('Discarding draft…');try{await mrRequest('/admin/weekly-savoury/reject','POST',{draft_id:wsDraft.draft_id,revision:wsDraft.revision});wsDraft=null;wsAnalysis=null;await wsLoad();wsMessage('Draft discarded.');}catch(error){wsMessage(error.message);}finally{wsBusy=false;}}
async function wsRestore(version){if(!confirm('Restore previous weekly menu?\n\n'+(version.display_date_range||'Undated')+'\nIt will become a review draft, not live immediately.'))return;
  if(version.dates_expired&&!confirm('This menu has already expired. Confirm new valid dates before publishing it again. Continue to review?'))return;
  try{const result=await mrRequest('/admin/weekly-savoury/versions/'+version.version_id+'/restore','POST');wsDraft=result.draft;
    wsAnalysis=(await mrRequest('/admin/weekly-savoury/analyse','POST',{draft_id:wsDraft.draft_id,revision:wsDraft.revision})).analysis;
    wsRenderReview();wsMessage('Previous menu restored to a draft. Review and confirm before publishing.');}
  catch(error){wsMessage(error.message);}}
const wsDrop=document.getElementById('wsDrop');
['dragenter','dragover'].forEach(type=>wsDrop.addEventListener(type,event=>{event.preventDefault();wsDrop.classList.add('is-dragging');}));
['dragleave','dragend'].forEach(type=>wsDrop.addEventListener(type,event=>{event.preventDefault();wsDrop.classList.remove('is-dragging');}));
wsDrop.addEventListener('drop',event=>{event.preventDefault();wsDrop.classList.remove('is-dragging');if(event.dataTransfer?.files?.length)wsFileSelected(event.dataTransfer.files[0]);});
document.addEventListener('paste',event=>{const panel=document.getElementById('tab-image');if(!panel||panel.style.display==='none')return;
  const workspace=document.getElementById('mrWeeklyView');if(workspace?.hidden)return;
  if(event.target?.closest?.('input,textarea,[contenteditable]'))return;
  for(const item of event.clipboardData?.items||[]){if(item.type?.startsWith('image/')){const file=item.getAsFile();if(file){event.preventDefault();wsExtractFile(file);}break;}}
});
</script>
'''
