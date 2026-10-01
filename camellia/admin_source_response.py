"""Shared read-only Approved Q&A selector for all Instagram source editors."""
import json

SELECTOR_TEMPLATE = r"""<style>[id$="QaFields"][hidden],.source-map-field[hidden]{display:none!important;} fieldset.source-map-field input[type="radio"]{width:auto;}</style><fieldset class="source-map-field" style="border:0;padding:0;margin:0 0 16px"><legend class="field-label">Response Source</legend>
<label><input type="radio" name="__PREFIX__ResponseMode" value="CUSTOM" checked onchange="setSourceResponseMode('__PREFIX__')"> Custom Response</label>
<label style="margin-left:16px"><input type="radio" name="__PREFIX__ResponseMode" value="APPROVED_QA" onchange="setSourceResponseMode('__PREFIX__')"> Approved Q&amp;A</label>
<div id="__PREFIX__QaFields" hidden style="margin-top:12px">
<label class="field-label" for="__PREFIX__QaSearch">Search Approved Q&amp;A</label>
<input class="field-control" id="__PREFIX__QaSearch" type="search" placeholder="Question, QA ID or accepted variation" oninput="filterSourceQa('__PREFIX__')">
<label class="field-label" for="__PREFIX__ApprovedQaId">Approved Q&amp;A</label>
<select class="field-control" id="__PREFIX__ApprovedQaId" onchange="previewSourceQa('__PREFIX__')"><option value="">Select Approved Q&amp;A</option></select>
<p class="source-map-help">Read-only preview sourced from Approved Q&amp;A. Edit wording in the Approved Q&amp;A page. Custom content is retained when switching modes.</p>
<div id="__PREFIX__QaPreview" style="white-space:pre-wrap;overflow-wrap:anywhere"></div>
</div></fieldset>"""


def selector_html(prefix):
    assert prefix in {"ig", "igStory", "igMedia"}
    return SELECTOR_TEMPLATE.replace("__PREFIX__", prefix)


SCRIPT = r"""<script>
const sourceResponseState = {};
function sourceResponseHtml(prefix) { return __SELECTOR__.replaceAll('__PREFIX__', prefix); }
function sourceResponseFields(prefix) {
 const mode=document.querySelector(`input[name="${prefix}ResponseMode"]:checked`)?.value || 'CUSTOM';
 return {response_mode:mode, approved_qa_id:mode==='APPROVED_QA' ? document.getElementById(prefix+'ApprovedQaId')?.value || '' : null};
}
function setSourceResponseMode(prefix) {
 const approved=sourceResponseFields(prefix).response_mode==='APPROVED_QA';
 document.getElementById(prefix+'QaFields').hidden=!approved;
 const ids={ig:['igTopicPicker','igSourceDescription','igTopicDescription','igMenuPicker'],igStory:['igStoryTopicPicker','igStorySourceDescription','igStoryMenuPicker'],igMedia:['igMediaTopic','igMediaDescription','igMediaMenu']}[prefix];
 for(const id of ids){const element=document.getElementById(id); if(element)element.closest('.source-map-field')?.toggleAttribute('hidden',approved);}
 previewSourceQa(prefix);
}
async function initSourceResponse(prefix, route) {
 const state={selected:route.approved_qa_id || '',records:[]}; sourceResponseState[prefix]=state;
 document.querySelector(`input[name="${prefix}ResponseMode"][value="${route.response_mode || 'CUSTOM'}"]`).checked=true;
 document.getElementById(prefix+'QaSearch').value='';
 const select=document.getElementById(prefix+'ApprovedQaId'); select.replaceChildren(new Option('Select Approved Q&A','')); select.disabled=true;
 setSourceResponseMode(prefix);
 try {
  const response=await fetch('/admin/instagram-routes/approved-qa-options',{headers:getAuthHeaders()});
  if(!response.ok)throw new Error('Effective Approved Q&A is unavailable.');
  const data=await response.json(); if(sourceResponseState[prefix]!==state)return;
  state.records=data.records || []; select.disabled=false; filterSourceQa(prefix);
 } catch(error) {
  if(sourceResponseState[prefix]!==state)return;
  select.replaceChildren(new Option('Approved Q&A unavailable',''));
  document.getElementById(prefix+'QaPreview').textContent=error.message;
 }
}
function filterSourceQa(prefix) {
 const state=sourceResponseState[prefix]; if(!state)return;
 const select=document.getElementById(prefix+'ApprovedQaId');
 const selected=select.value || state.selected;
 const query=document.getElementById(prefix+'QaSearch').value.trim().toLowerCase();
 select.replaceChildren(new Option('Select Approved Q&A',''));
 for(const record of state.records){
  const searchable=[record.question,record.qa_id,...(record.aliases||[]),...(record.source_aliases||[])].join(' ').toLowerCase();
  if(!query || searchable.includes(query) || record.qa_id===selected)select.add(new Option(record.question+' — '+record.qa_id,record.qa_id));
 }
 if(selected && !state.records.some(r=>r.qa_id===selected)){
  const option=new Option(selected+' — unavailable (choose an effective QA)',selected); option.disabled=true;select.add(option);
 }
 select.value=selected;previewSourceQa(prefix);
}
function previewSourceQa(prefix) {
 const state=sourceResponseState[prefix]; if(!state)return;
 const select=document.getElementById(prefix+'ApprovedQaId'); state.selected=select.value || state.selected;
 const box=document.getElementById(prefix+'QaPreview'); box.replaceChildren();
 const record=state.records.find(r=>r.qa_id===select.value);
 if(!record){box.textContent=state.selected ? 'This QA is not currently effective. Select an active Approved Q&A before saving.' : 'Choose an Approved Q&A to preview.';return;}
 const title=document.createElement('strong');title.textContent=record.question+' ('+record.qa_id+')';box.append(title);
 const answer=document.createElement('p');answer.textContent=record.answer;box.append(answer);
 const count=document.createElement('p');count.textContent='Accepted question variations: '+record.variation_count;box.append(count);
 const label=document.createElement('strong');label.textContent='Ordered response assets / links';box.append(label);
 const list=document.createElement('ol');for(const block of record.ordered_blocks || record.blocks || []){
  const item=document.createElement('li');item.textContent=block.kind==='TEXT' ? 'Approved text' : block.kind==='LINK' ? block.url : block.kind+': '+block.value;list.append(item);
 }box.append(list);
}
</script>""".replace("__SELECTOR__", json.dumps(SELECTOR_TEMPLATE))
