"""Handoffs-tab card for editing the message customers get when handed to a person."""
HTML = r'''
<section class="ig-media-card" id="handoffMessageCard" style="display:block;margin-bottom:16px;">
  <b>Message customers see when the bot hands them to a person</b>
  <p class="source-map-help">Sent whenever the bot cannot answer and passes the chat to your team. Changes apply immediately.</p>
  <textarea class="field-control" id="handoffMessageText" rows="3" maxlength="1000" style="width:100%;box-sizing:border-box;"></textarea>
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px;">
    <button class="btn-primary" type="button" onclick="saveHandoffMessage()">Save message</button>
    <button class="btn-secondary" type="button" id="handoffMessageReset" onclick="resetHandoffMessage()">Use original message</button>
    <span id="handoffMessageStatus" class="ig-media-muted" role="status"></span>
  </div>
</section>
'''

SCRIPT = r'''
<script>
let handoffMessageState = null;
function renderHandoffMessage(state, note) {
    handoffMessageState = state;
    document.getElementById('handoffMessageText').value = state.message || '';
    // .btn-secondary sets display, which overrides the hidden attribute.
    document.getElementById('handoffMessageReset').style.display = state.is_custom ? '' : 'none';
    document.getElementById('handoffMessageStatus').textContent = note || (state.is_custom ? 'Using your edited message.' : 'Using the original message.');
}
async function loadHandoffMessage() {
    if (!adminAuthenticated) return;
    try {
        const response = await fetch('/admin/handoff-message', {headers:getAuthHeaders(), cache:'no-store'});
        if (!response.ok) throw Error();
        renderHandoffMessage(await response.json());
    } catch (_) { document.getElementById('handoffMessageStatus').textContent = 'Could not load the current message.'; }
}
async function saveHandoffMessage() {
    const message = document.getElementById('handoffMessageText').value.trim();
    if (!message) { document.getElementById('handoffMessageStatus').textContent = 'The message cannot be empty.'; return; }
    if (!confirm('Customers will receive this message from now on:\n\n' + message)) return;
    try {
        const response = await fetch('/admin/handoff-message', {method:'PUT', headers:{...getAuthHeaders(),'Content-Type':'application/json'}, body:JSON.stringify({message})});
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : 'Could not save.');
        renderHandoffMessage(data, 'Saved. Customers will now receive this message.');
    } catch (error) { document.getElementById('handoffMessageStatus').textContent = error.message || 'Could not save.'; }
}
async function resetHandoffMessage() {
    if (!handoffMessageState || !confirm('Go back to the original message?\n\n' + handoffMessageState.default_message)) return;
    try {
        const response = await fetch('/admin/handoff-message/reset', {method:'POST', headers:getAuthHeaders()});
        if (!response.ok) throw Error();
        renderHandoffMessage(await response.json(), 'Restored the original message.');
    } catch (_) { document.getElementById('handoffMessageStatus').textContent = 'Could not restore the original message.'; }
}
</script>
'''
