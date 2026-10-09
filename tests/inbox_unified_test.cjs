/* Event contract tests; actual geometry and navigation are gated in Chromium. */
'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/js/inbox-unified.js'),'utf8');
function setup(present=true) {
    const listeners=new Map(),shell={dataset:{activePanel:'detail'}};
    const rows=['canonical-id','legacy-id'].map(id=>({dataset:{inboxOpenMessage:id},attributes:{},setAttribute(key,value){this.attributes[key]=value;},removeAttribute(key){delete this.attributes[key];}}));
    const add=(name,handler)=>listeners.set(name,[...(listeners.get(name)||[]),handler]);
    const context={URL,URLSearchParams,FormData:class{constructor(form){return form.entries;}},window:{location:{href:'https://fixture.test/inbox/'},addEventListener:add},document:{addEventListener:add,querySelector:()=>present?shell:null,querySelectorAll:()=>rows}};
    vm.runInNewContext(source,context);
    const emit=(name,event)=>{for(const handler of listeners.get(name)||[])handler(event);};
    return {context,shell,rows,emit};
}
function swap(xhr) {return {detail:{xhr,shouldSwap:true},preventDefault(){this.prevented=true;}};}
function request(app,xhr,id,source='legacy') {
    const panel={dataset:source==='legacy'?{selectedMessageId:id}:{conversationId:id}};
    app.emit('htmx:beforeRequest',{detail:{xhr,elt:{dataset:{},closest:()=>panel}}});
}
test('legacy action response cannot replace a newer canonical selection',()=>{
    const app=setup(),xhr={};
    request(app,xhr,'legacy-id');
    app.emit('inbox:selection-approved',{detail:{messageId:'canonical-id'}});
    const event=swap(xhr);app.emit('htmx:beforeSwap',event);
    assert.equal(event.detail.shouldSwap,false);assert.equal(event.prevented,true);
});
test('canonical response cannot replace a newer legacy selection',()=>{
    const app=setup(),xhr={};
    request(app,xhr,'canonical-id','canonical');
    app.emit('inbox:selection-approved',{detail:{messageId:'legacy-id'}});
    const event=swap(xhr);app.emit('htmx:beforeSwap',event);
    assert.equal(event.detail.shouldSwap,false);
});
test('active detail actions and unrelated list requests remain swappable',()=>{
    const app=setup(),xhr={};
    request(app,xhr,'legacy-id');
    app.emit('inbox:selection-approved',{detail:{messageId:'legacy-id'}});
    for(const value of [xhr,{}]) {const event=swap(value);app.emit('htmx:beforeSwap',event);assert.equal(event.detail.shouldSwap,true);}
});
test('selection highlighting follows accepted selection and survives list replacement',()=>{
    const app=setup();app.emit('inbox:selection-approved',{detail:{messageId:'canonical-id'}});
    assert.equal(app.rows[0].attributes['aria-current'],'true');assert.equal(app.rows[1].attributes['aria-current'],undefined);
    app.rows[0].attributes={};app.emit('htmx:afterSwap',{});
    assert.equal(app.rows[0].attributes['aria-current'],'true');
});
test('both detail sources use the shared mobile back behavior',()=>{
    const app=setup(),back={closest:()=>app.shell};
    const event={target:{closest:()=>back},preventDefault(){this.prevented=true;}};
    app.emit('click',event);assert.equal(app.shell.dataset.activePanel,'list');assert.equal(event.prevented,true);
});
test('standalone pages and script reloads do not install extra behavior',()=>{
    const app=setup(false),xhr={};request(app,xhr,'legacy-id');app.emit('inbox:selection-approved',{detail:{messageId:'canonical-id'}});
    const event=swap(xhr);app.emit('htmx:beforeSwap',event);assert.equal(event.detail.shouldSwap,true);
    vm.runInNewContext(source,app.context);
    assert.equal(app.rows[0].attributes['aria-current'],undefined);
});

function listEvent(domain,refresh=false,query={}) {
    const form={action:'/inbox/',entries:[['domain',domain],...Object.entries(query)]};
    form.closest=selector=>selector==='#inbox-filters'?form:null;
    form.matches=()=>refresh;
    const element=refresh?form:{dataset:{},closest:()=>null,matches:()=>false,getAttribute:()=>'/inbox/?domain='+domain};
    element.dataset={};
    return {detail:{elt:element,target:{id:'inbox-list-content'},xhr:{}},preventDefault(){this.prevented=true;}};
}
test('a newer type rejects a stale in-flight list response',()=>{
    const app=setup(),old=listEvent('comment'),newer=listEvent('mention');
    app.emit('htmx:confirm',old);app.emit('htmx:beforeRequest',old);
    app.emit('htmx:confirm',newer);
    const event=swap(old.detail.xhr);app.emit('htmx:beforeSwap',event);
    assert.equal(event.detail.shouldSwap,false);
});
test('automatic read refresh cannot supersede a pending user type choice',()=>{
    const app=setup(),newer=listEvent('mention'),oldRefresh=listEvent('all',true);
    app.emit('htmx:confirm',newer);app.emit('htmx:confirm',oldRefresh);
    assert.equal(oldRefresh.prevented,true);
    const currentRefresh=listEvent('mention',true);app.emit('htmx:confirm',currentRefresh);
    assert.equal(currentRefresh.prevented,undefined);
});
test('same-scope automatic list refresh remains enabled',()=>{
    const app=setup(),first=listEvent('all',true),second=listEvent('all',true);
    app.emit('htmx:confirm',first);app.emit('htmx:beforeRequest',first);app.emit('htmx:confirm',second);
    const event=swap(first.detail.xhr);app.emit('htmx:beforeSwap',event);
    assert.equal(event.detail.shouldSwap,true);
});

test('account and platform changes clear source-specific status before HTMX collects values',()=>{
    for(const name of ['account','platform']) {
        const app=setup(),fields=[{value:'unread'},{value:'needs_action'},{value:'mine'}];
        const target={matches:()=>true,closest:()=>({querySelectorAll:()=>fields})};
        app.emit('change',{target});
        assert.deepEqual(fields.map(field=>field.value),['','',''],name);
    }
});
