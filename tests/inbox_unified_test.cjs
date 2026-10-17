/* Event contract tests; actual geometry and navigation are gated in Chromium. */
'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/js/inbox-unified.js'),'utf8');
function setup(present=true) {
    let currentForm=null;
    const dispatched=[],listeners=new Map(),shell={dataset:{activePanel:'detail'}};
    const rows=['canonical-id','legacy-id'].map(id=>({dataset:{inboxOpenMessage:id},attributes:{},setAttribute(key,value){this.attributes[key]=value;},removeAttribute(key){delete this.attributes[key];}}));
    const add=(name,handler)=>listeners.set(name,[...(listeners.get(name)||[]),handler]);
    const context={URL,URLSearchParams,CustomEvent:class{constructor(type,options){this.type=type;this.detail=options?.detail;}},FormData:class{constructor(form){return form.entries;}},window:{location:{href:'https://fixture.test/inbox/'},addEventListener:add},document:{addEventListener:add,dispatchEvent:event=>dispatched.push(event),querySelector:selector=>selector==='[data-unified-filters]'?currentForm:present?shell:null,querySelectorAll:()=>rows}};
    vm.runInNewContext(source,context);
    const emit=(name,event)=>{for(const handler of listeners.get(name)||[])handler(event);};
    return {context,shell,rows,emit,dispatched,setForm(form){currentForm=form;}};
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
    const element=refresh?form:{dataset:{},closest:()=>null,matches:()=>false,getAttribute:()=>'/inbox/?'+new URLSearchParams(form.entries)};
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

test('successful paginated refresh rebases its scope so the next mutation refresh is accepted',()=>{
    const app=setup(),older=listEvent('comment',false,{cursor:'page-two'}),refresh=listEvent('comment',true,{cursor:'page-two'});
    app.emit('htmx:confirm',older);
    app.emit('htmx:confirm',refresh);assert.equal(refresh.prevented,undefined);
    app.emit('htmx:beforeRequest',refresh);
    const firstPage=listEvent('comment',true);
    app.setForm(firstPage.detail.elt);
    app.emit('htmx:afterSwap',{detail:{xhr:refresh.detail.xhr,target:{id:'inbox-list-content'}}});
    app.emit('htmx:confirm',firstPage);assert.equal(firstPage.prevented,undefined);
});
test('failed paginated refresh keeps the old visible scope retryable',()=>{
    const app=setup(),older=listEvent('comment',false,{cursor:'page-two'});
    app.emit('htmx:confirm',older);
    for(let attempt=0;attempt<2;attempt++) {
        const refresh=listEvent('comment',true,{cursor:'page-two'});
        app.emit('htmx:confirm',refresh);assert.equal(refresh.prevented,undefined);
        app.emit('htmx:beforeRequest',refresh);
        app.emit('htmx:afterRequest',{detail:{xhr:refresh.detail.xhr,failed:true,successful:false}});
    }
});
test('detail swaps cannot prematurely rebase a tracked list refresh',()=>{
    const app=setup(),older=listEvent('comment',false,{cursor:'page-two'}),refresh=listEvent('comment',true,{cursor:'page-two'});
    app.emit('htmx:confirm',older);app.emit('htmx:confirm',refresh);app.emit('htmx:beforeRequest',refresh);
    app.setForm(listEvent('comment',true).detail.elt);
    app.emit('htmx:afterSwap',{detail:{xhr:refresh.detail.xhr,target:{id:'inbox-detail-panel'}}});
    const retry=listEvent('comment',true,{cursor:'page-two'});
    app.emit('htmx:confirm',retry);assert.equal(retry.prevented,undefined);
});
test('old paginated refresh cannot overwrite or rebase newer filter, page or history intent',()=>{
    for(const mode of ['filter','page','history']) {
        const app=setup(),older=listEvent('comment',false,{cursor:'page-two'}),refresh=listEvent('comment',true,{cursor:'page-two'});
        app.emit('htmx:confirm',older);app.emit('htmx:confirm',refresh);app.emit('htmx:beforeRequest',refresh);
        const newer=mode==='page'?listEvent('comment',false,{cursor:'page-three'}):listEvent('mention');
        if(mode==='history') {app.setForm(listEvent('mention',true).detail.elt);app.emit('htmx:historyRestore',{});}
        else app.emit('htmx:confirm',newer);
        const stale=swap(refresh.detail.xhr);app.emit('htmx:beforeSwap',stale);assert.equal(stale.detail.shouldSwap,false,mode);
        // A stale/foreign completion cannot rebase wantedList even if an event is delivered late.
        app.setForm(listEvent('comment',true).detail.elt);
        app.emit('htmx:afterSwap',{detail:{xhr:refresh.detail.xhr,target:{id:'inbox-list-content'}}});
        const expected=mode==='page'?listEvent('comment',true,{cursor:'page-three'}):listEvent('mention',true);
        app.emit('htmx:confirm',expected);assert.equal(expected.prevented,undefined,mode);
    }
});

test('account and platform changes clear source-specific status before HTMX collects values',()=>{
    for(const name of ['account','platform']) {
        const app=setup(),fields=[{value:'unread'},{value:'needs_action'},{value:'mine'}];
        const target={matches:()=>true,closest:()=>({querySelectorAll:()=>fields})};
        app.emit('change',{target});
        assert.deepEqual(fields.map(field=>field.value),['','',''],name);
    }
});

test('history restoration resets filter scope while preserving selected detail',()=>{
    const app=setup(),old=listEvent('dm'),restored=listEvent('comment',true);
    app.emit('inbox:selection-approved',{detail:{messageId:'canonical-id'}});
    app.emit('htmx:confirm',old);app.emit('htmx:beforeRequest',old);
    app.setForm(restored.detail.elt);app.emit('htmx:historyRestore',{});
    const stale=swap(old.detail.xhr);app.emit('htmx:beforeSwap',stale);
    assert.equal(stale.detail.shouldSwap,false);
    app.emit('htmx:confirm',restored);assert.equal(restored.prevented,undefined);
    assert.equal(app.rows[0].attributes['aria-current'],'true');
});

test('old current-workspace body cache is bypassed through scoped HTMX without rewriting cached drafts',async()=>{
    const app=setup(),calls=[],nodes=[],cache='[{"url":"http://[","content":"Malformed URL entry retains input"},{"url":"/inbox/?domain=comment","content":"<p>Unsaved cached reply</p>"},{"url":"/calendar/","content":"Other page"}]';
    app.setForm({action:'/inbox/'});
    app.context.window.location.href='https://fixture.test/inbox/?domain=comment';
    app.context.window.localStorage={getItem:()=>cache,setItem(){throw new Error('Cache must not be rewritten');},removeItem(){throw new Error('Cache must not be deleted');}};
    app.context.window.htmx={ajax(...args){calls.push(args);return Promise.resolve();}};
    app.shell.appendChild=node=>nodes.push(node);
    app.context.document.createElement=()=>({dataset:{},setAttribute(){},remove(){this.removed=true;}});
    const event={state:{htmx:true},stopImmediatePropagation(){this.stopped=true;}};
    app.emit('popstate',event);
    assert.equal(event.stopped,true);assert.equal(calls.length,1);
    assert.equal(app.dispatched[0].type,'inbox:history-navigation','Existing native observation cleanup still runs');
    assert.equal(calls[0][0],'GET');assert.equal(calls[0][1],'https://fixture.test/inbox/?domain=comment');
    assert.equal(calls[0][2].swap,'innerHTML settle:0ms');assert.equal(calls[0][2].select,'#inbox-list-content > *');assert.equal(calls[0][2].target,'#inbox-list-content');
    assert.equal(calls[0][2].headers['HX-History-Restore-Request'],'true');assert.equal(calls[0][2].values,undefined);
    assert.equal(calls[0][2].source,nodes[0]);assert.equal(nodes[0].hidden,true);
    assert.equal(app.context.window.localStorage.getItem(),cache);
});
test('history bypass leaves other pages, workspaces and origins entirely to normal navigation',()=>{
    for(const location of ['https://fixture.test/other-workspace/inbox/','https://fixture.test/calendar/','https://elsewhere.test/inbox/']) {
        const app=setup();app.setForm({action:'https://fixture.test/inbox/'});app.context.window.location.href=location;
        app.context.window.localStorage={getItem(){throw new Error('Unrelated history cache should not even be inspected');}};
        app.context.window.htmx={ajax(){throw new Error('Unrelated navigation must not be intercepted');}};
        const event={state:{htmx:true},stopImmediatePropagation(){this.stopped=true;}};app.emit('popstate',event);
        assert.equal(event.stopped,undefined);
    }
});

test('malformed or unrelated cache and non-HTMX popstate never activate the bypass',()=>{
    for(const [cache,state] of [
        ['not valid JSON',{htmx:true}],
        [JSON.stringify([{url:'/calendar/',content:'Other page'}]),{htmx:true}],
        [JSON.stringify([{url:'/inbox/',content:'Old draft'}]),{}],
        [JSON.stringify([{url:'/inbox/',content:'Old draft'}]),null]
    ]) {
        const app=setup();app.setForm({action:'/inbox/'});
        app.context.window.localStorage={getItem:()=>cache,setItem(){throw new Error('No cache writes');},removeItem(){throw new Error('No cache deletion');}};
        app.context.window.htmx={ajax(){throw new Error('Unexpected bypass');}};
        const event={state,stopImmediatePropagation(){this.stopped=true;}};app.emit('popstate',event);
        assert.equal(event.stopped,undefined);
    }
});
