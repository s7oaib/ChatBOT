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


STORY_FIELDS = r'''<style>#igStoryApprovedQaId[hidden]{display:none!important;}</style><label class="field-label" for="igStoryQaContext">Context</label>
<select class="field-control" id="igStoryQaContext" onchange="changeStoryQaContext()"></select>
<p id="igStoryQaCount" role="status">Selected: 0</p>
<div id="igStoryQaChecklist" style="max-height:40vh;overflow:auto;overflow-wrap:anywhere"></div>
<p>Choose each approved question explicitly. General questions are never automatically selected.</p>'''


def selector_html(prefix):
    assert prefix in {"ig", "igStory", "igMedia"}
    template = SELECTOR_TEMPLATE.replace("__PREFIX__", prefix)
    if prefix == "igStory":
        template = template.replace('<label class="field-label" for="igStoryApprovedQaId">Approved Q&amp;A</label>', '')
        template = template.replace('<select class="field-control" id="igStoryApprovedQaId"', '<select hidden class="field-control" id="igStoryApprovedQaId"')
        template = template.replace('<p class="source-map-help">', STORY_FIELDS + '<p class="source-map-help">')
    else:
        fields = STORY_FIELDS.replace('igStory', prefix).replace('changeStoryQaContext()', "changeStoryQaContext('" + prefix + "')")
        fields = '<div id="' + prefix + 'QaBundleFields" hidden>' + fields + '</div>'
        template = template.replace('<label class="field-label" for="' + prefix + 'ApprovedQaId">', '<label id="' + prefix + 'QaSingleLabel" class="field-label" for="' + prefix + 'ApprovedQaId">')
        template = template.replace('<p class="source-map-help">', fields + '<p class="source-map-help">')
        template = '<style>[id$="QaBundleFields"][hidden],[id$="ApprovedQaId"][hidden],[id$="QaSingleLabel"][hidden]{display:none!important;}</style>' + template
    return template


SCRIPT = r"""<script>
const sourceResponseState = {};
function sourceResponseHtml(prefix) { return prefix==='igStory' ? __STORY_SELECTOR__ : prefix==='igMedia' ? __MEDIA_SELECTOR__ : __POST_SELECTOR__; }
function sourceQaMultiple(prefix){return prefix==='igStory'||prefix==='igMedia'||(prefix==='ig'&&['post','reel'].includes(document.getElementById('igSourceType')?.value||'post'));}
function sourceResponseFields(prefix) {
 const mode=document.querySelector(`input[name="${prefix}ResponseMode"]:checked`)?.value || 'CUSTOM';
 if(sourceQaMultiple(prefix) && mode==='APPROVED_QA'){
  const state=sourceResponseState[prefix];
  return {response_mode:mode,approved_qa_ids:Array.from(state?.selectedIds||[]),[prefix==='igStory'?'story_context':'source_context']:state?.context??document.getElementById(prefix+'QaContext')?.value??'general'};
 }
 return {response_mode:mode, approved_qa_id:mode==='APPROVED_QA' ? document.getElementById(prefix+'ApprovedQaId')?.value || '' : null};
}
function setSourceResponseMode(prefix) {
 if(!sourceResponseState[prefix]){initSourceResponse(prefix,sourceResponseFields(prefix));return;}
 const approved=sourceResponseFields(prefix).response_mode==='APPROVED_QA';
 document.getElementById(prefix+'QaFields').hidden=!approved;
 if(prefix!=='igStory'){const multi=sourceQaMultiple(prefix);document.getElementById(prefix+'QaBundleFields').hidden=!multi;document.getElementById(prefix+'ApprovedQaId').hidden=multi;document.getElementById(prefix+'QaSingleLabel').hidden=multi;}
 const ids={ig:['igTopicPicker','igSourceDescription','igTopicDescription','igMenuPicker'],igStory:['igStoryTopicPicker','igStorySourceDescription','igStoryMenuPicker'],igMedia:['igMediaTopic','igMediaDescription','igMediaMenu']}[prefix];
 for(const id of ids){const element=document.getElementById(id); if(element)element.closest('.source-map-field')?.toggleAttribute('hidden',approved);}
 previewSourceQa(prefix);
}
async function initSourceResponse(prefix, route) {
 const state={selected:route.approved_qa_id || '',records:[],selectedIds:new Set(route.approved_qa_ids || (route.approved_qa_id?[route.approved_qa_id]:[])),context:(prefix==='igStory'?route.story_context:route.source_context)??null}; sourceResponseState[prefix]=state;
 document.querySelector(`input[name="${prefix}ResponseMode"][value="${route.response_mode || 'CUSTOM'}"]`).checked=true;
 document.getElementById(prefix+'QaSearch').value='';
 const select=document.getElementById(prefix+'ApprovedQaId'); select.replaceChildren(new Option('Select Approved Q&A','')); select.disabled=true;
 setSourceResponseMode(prefix);
 try {
  const response=await fetch('/admin/instagram-routes/approved-qa-options',{headers:getAuthHeaders()});
  if(!response.ok)throw new Error('Effective Approved Q&A is unavailable.');
  const data=await response.json(); if(sourceResponseState[prefix]!==state)return;
  state.records=data.records || []; select.disabled=false;
  {
   state.contexts=data.contexts||Array.from(new Set(['general',...state.records.map(r=>r.context)])).map(value=>({value,label:value}));
   if(state.context===null)state.context=state.records.find(r=>state.selectedIds.has(r.qa_id))?.context||'general';
   const context=document.getElementById(prefix+'QaContext');context.replaceChildren(...state.contexts.map(o=>new Option(o.label,o.value)));context.value=state.context;
  }
  filterSourceQa(prefix);
 } catch(error) {
  if(sourceResponseState[prefix]!==state)return;
  select.replaceChildren(new Option('Approved Q&A unavailable',''));
  document.getElementById(prefix+'QaPreview').textContent=error.message;
 }
}
function filterSourceQa(prefix) {
 const state=sourceResponseState[prefix]; if(!state)return;
 if(sourceQaMultiple(prefix)){renderStoryQa(prefix);return;}
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
 if(sourceQaMultiple(prefix)){document.getElementById(prefix+'QaPreview').replaceChildren();return;}
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
function changeStoryQaContext(prefix='igStory'){const state=sourceResponseState[prefix];if(!state)return;state.context=document.getElementById(prefix+'QaContext').value;renderStoryQa(prefix);}
function renderStoryQa(prefix='igStory'){
 const state=sourceResponseState[prefix];if(!state)return;
 const box=document.getElementById(prefix+'QaChecklist');box.replaceChildren();
 document.getElementById(prefix+'QaCount').textContent='Selected: '+state.selectedIds.size;
 const query=document.getElementById(prefix+'QaSearch').value.trim().toLowerCase();
 const eligible=state.records.filter(r=>r.context===state.context||r.context==='general');
 const groups=[['Approved questions for this context',eligible.filter(r=>r.context===state.context)],['General — select explicitly',eligible.filter(r=>r.context==='general'&&state.context!=='general')]];
 for(const [title,records] of groups){
  const group=document.createElement('fieldset');group.style.border='0';group.style.padding='0';
  const legend=document.createElement('legend');legend.textContent=title;group.append(legend);
  for(const record of records){
   const searchable=[record.question,record.qa_id,...(record.aliases||[]),...(record.source_aliases||[])].join(' ').toLowerCase();
   if(query&&!searchable.includes(query))continue;
   const row=document.createElement('div');row.style.marginBottom='12px';
   const label=document.createElement('label');const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.style.width='auto';checkbox.value=record.qa_id;checkbox.checked=state.selectedIds.has(record.qa_id);
   checkbox.onchange=()=>{if(checkbox.checked)state.selectedIds.add(record.qa_id);else state.selectedIds.delete(record.qa_id);document.getElementById(prefix+'QaCount').textContent='Selected: '+state.selectedIds.size;};
   label.append(checkbox,document.createTextNode(' '+record.question+' — '+record.qa_id));row.append(label);
   const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='Read-only approved answer preview';const answer=document.createElement('p');answer.style.whiteSpace='pre-wrap';answer.textContent=record.answer;details.append(summary,answer);row.append(details);group.append(row);
  }box.append(group);
 }
 for(const key of state.selectedIds){if(eligible.some(r=>r.qa_id===key))continue;
  const warning=document.createElement('p');warning.textContent=key+' — unavailable/expired or context changed. Remove it and choose a current Q&A before saving.';
  const remove=document.createElement('button');remove.type='button';remove.textContent='Remove selection';remove.onclick=()=>{state.selectedIds.delete(key);renderStoryQa(prefix);};warning.append(remove);box.append(warning);
 }
}
</script>""".replace("__SELECTOR__", json.dumps(SELECTOR_TEMPLATE)).replace("__STORY_SELECTOR__", json.dumps(selector_html('igStory'))).replace("__MEDIA_SELECTOR__", json.dumps(selector_html('igMedia'))).replace("__POST_SELECTOR__", json.dumps(selector_html('ig')))
