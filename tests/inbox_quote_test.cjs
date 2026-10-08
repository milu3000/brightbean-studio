const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/inbox-quote.js'), 'utf8');
function setup(supported = true) {
    const listeners = new Map(); let active, changes = 0;
    const field = { value:'', defaultValue:'', dataset:{inboxQuoteInitial:''} }, sender = {textContent:''}, body = {textContent:''};
    const preview = {hidden:true, querySelector: selector => selector.endsWith('sender]') ? sender : body};
    const textarea = {value:'Unsent body',focus(){this.focused=true;}};
    const form = {isConnected:true,dataset:{inboxQuoteSupported:String(supported)},
        closest: selector => selector === '[data-canonical-panel]' ? panel : form,
        contains: node => node === cancel,
        querySelector: selector => ({'[data-inbox-quote-id]':field,'[data-inbox-quote-preview]':preview,'[data-inbox-quote-cancel]':cancel,'textarea':textarea}[selector])};
    const bubble = {dataset:{canonicalMessage:'message-a'},textContent:'PRIVATE RETAINED SECRET',
        querySelector: selector => selector === '[data-inbox-quote-body]' ? {textContent:'Visible ordinary text'} : {textContent:'Visible sender'}};
    const panel = {isConnected:true,contains: node => node === bubble,
        querySelector: selector => selector === '[data-inbox-reply-form]' ? form : null,
        querySelectorAll: () => [select]};
    function button(isCancel) { const result = {disabled:false,hidden:true,dataset:isCancel?{}:{inboxQuoteTarget:'message-a'},
        setAttribute(key,value){this[key]=value;}, matches: () => isCancel,
        closest: selector => selector === '[data-canonical-panel]' ? panel : selector === '[data-canonical-message]' ? bubble : result};return result; }
    const select = button(false), cancel = button(true); active = panel;
    const document = {readyState:'complete',querySelector:()=>active,addEventListener(name,fn){listeners.set(name,[...(listeners.get(name)||[]),fn]);}};
    const context={document,window:{},WeakSet};vm.runInNewContext(source,context);
    const emit=(name,event={})=>(listeners.get(name)||[]).forEach(fn=>fn(event));
    return {field,form,panel,preview,body,sender,textarea,select,cancel,bubble,context,emit,
        click(button){emit('click',{target:button,preventDefault(){changes++;}});},changes:()=>changes,
        detach(){panel.isConnected=false;active={querySelector:()=>null,isConnected:true,querySelectorAll:()=>[]};}};
}
test('quote is empty by default; explicit selection uses only visible slots and cancel clears it',()=>{
    const app=setup();assert.equal(app.field.value,'');assert.equal(app.preview.hidden,true);
    app.click(app.select);assert.equal(app.field.value,'message-a');assert.equal(app.field.dataset.inboxQuoteInitial,'');
    assert.equal(app.body.textContent,'Visible ordinary text');assert(!app.body.textContent.includes('PRIVATE'));
    assert.equal(app.textarea.value,'Unsent body');assert.equal(app.select['aria-pressed'],'true');
    app.click(app.cancel);assert.equal(app.field.value,'');assert.equal(app.preview.hidden,true);assert.equal(app.body.textContent,'');
});
test('unsupported or busy composer cannot change targets',()=>{
    const unsupported=setup(false);unsupported.click(unsupported.select);assert.equal(unsupported.field.value,'');assert.equal(unsupported.select.hidden,true);
    const app=setup();app.click(app.select);app.emit('htmx:beforeRequest',{detail:{elt:app.form}});app.click(app.cancel);assert.equal(app.field.value,'message-a');
    app.emit('htmx:afterRequest',{detail:{elt:app.form}});app.click(app.cancel);assert.equal(app.field.value,'');
});
test('detached or mismatched bubbles cannot update a new conversation',()=>{
    const app=setup();app.select.dataset.inboxQuoteTarget='foreign';app.click(app.select);assert.equal(app.field.value,'');
    app.select.dataset.inboxQuoteTarget='message-a';app.detach();app.click(app.select);assert.equal(app.field.value,'');
});
test('history processing enables current controls and installing again adds no duplicate handlers',()=>{
    const app=setup();app.select.hidden=true;app.emit('inbox:history-loaded');assert.equal(app.select.hidden,false);
    vm.runInNewContext(source,app.context);app.click(app.select);assert.equal(app.changes(),1);
});
