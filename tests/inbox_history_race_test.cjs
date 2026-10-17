/* Exact bundled HTMX history-loader regression; this is not a browser pass. */
'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),test=require('node:test'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const bundle=fs.readFileSync(path.join(root,'static/js/htmx.min.js'),'utf8');
const start=bundle.indexOf('function Gt(o){'),end=bundle.indexOf('function Wt(e){',start);
assert(start>0&&end>start,'Reinspect bundled HTMX history loading if its implementation changes');
const historyLoader=bundle.slice(start,end),controller=fs.readFileSync(path.join(root,'static/js/inbox-unified.js'),'utf8');
test('actual HTMX replaces the refreshed page URL while user filters retain push navigation',()=>{
    const html=fs.readFileSync(path.join(root,'templates/inbox/partials/_unified_filters.html'),'utf8');
    const form={attributes:Object.fromEntries([...html.match(/<form[^>]+>/)[0].matchAll(/(hx-[\w-]+)="([^"]*)"/g)].map(match=>[match[1],match[2]]))};
    const replaced=[];
    const sandbox={R:()=>false,re(element,name){for(let node=element;node;node=node.parent)if(node.attributes[name])return node.attributes[name];},ie:()=>({}),Q:{config:{historyEnabled:true}},history:{replaceState:(state,title,url)=>replaced.push(url)},Bt:'/inbox/?cursor=old'};
    vm.createContext(sandbox);
    const choose=bundle.indexOf('function Nn('),chooseEnd=bundle.indexOf('function In(',choose);
    const replace=bundle.indexOf('function Jt('),replaceEnd=bundle.indexOf('function Kt(',replace);
    assert(choose>0&&chooseEnd>choose&&replace>0&&replaceEnd>replace,'Reinspect bundled HTMX history functions if they change');
    vm.runInContext(bundle.slice(choose,chooseEnd)+bundle.slice(replace,replaceEnd),sandbox);
    const response={xhr:{},pathInfo:{finalRequestPath:'/inbox/?domain=comment&account=synthetic'}};
    const result=sandbox.Nn(form,response);
    assert.equal(result.type,'replace');assert.equal(result.path,response.pathInfo.finalRequestPath);
    sandbox.Jt(result.path);assert.deepEqual(replaced,[result.path]);assert.equal(sandbox.Bt,result.path);
    const input={parent:form,attributes:{'hx-push-url':'true'}};
    assert.equal(sandbox.Nn(input,response).type,'push','Explicit user filter navigation still adds browser history');
    delete form.attributes['hx-replace-url'];
    assert.equal(sandbox.Nn(form,response).type,undefined,'Old markup leaves the stale cursor URL unchanged');
});
function setup(enabled=true) {
    const listeners={},pending=[],body={},shell={},notices=[];let visible='dm';
    const currentForm=()=>({action:'/inbox/',entries:[['domain',visible]],closest(){return this;}});
    const emit=(name,detail)=>{const event={detail,preventDefault(){this.prevented=true;}};for(const fn of listeners[name]||[])fn(event);return event;};
    const sandbox={URL,URLSearchParams,CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail;}},FormData:class{constructor(form){return form.entries;}},
        window:{location:{href:'https://fixture.test/inbox/?domain=dm'},history:{state:{htmx:true},replaceState(state,title,url){sandbox.window.location.href=new URL(url,sandbox.window.location.href).href;}},dispatchEvent(event){notices.push(event);},addEventListener(name,fn){(listeners[name]??=[]).push(fn);}},
        document:{addEventListener(name,fn){(listeners[name]??=[]).push(fn);},querySelector(sel){return sel==='[data-unified-filters]'?currentForm():shell;},querySelectorAll(){return[];}},
        XMLHttpRequest:class{
            constructor(){pending.push(this);this.aborted=false;this.listeners={};}
            open(method,path){this.path=path;}setRequestHeader(){}send(){}addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}finish(){for(const fn of this.listeners.loadend||[])fn();}abort(){this.aborted=true;this.finish();}
        },
        ne:()=>({body,location:sandbox.window.location}),he:(target,name,detail)=>!emit(name,detail).prevented,
        P:domain=>({domain,querySelector(){return this;}}),Ut:()=>({}),xn:()=>({tasks:[]}),kn(){},qe(){},Ve(target,fragment){visible=fragment.domain;},Te(){},Kt(){},fe(){},Bt:''};
    vm.createContext(sandbox);if(enabled)vm.runInContext(controller,sandbox);vm.runInContext(historyLoader,sandbox);
    return {pending,emit,notices,url:()=>sandbox.window.location.href,visible:()=>visible,load(domain){sandbox.window.location.href='https://fixture.test/inbox/?domain='+domain;vm.runInContext(`Gt('/inbox/?domain=${domain}')`,sandbox);},
        deliver(index,domain,status=200){const xhr=pending[index];if(xhr.aborted||!xhr.onload)return false;xhr.status=status;xhr.response=domain;xhr.onload();xhr.finish();return true;},
        intent(domain,event={}){return {target:{id:'inbox-list-content'},elt:{dataset:{},closest:()=>null,matches:()=>false,getAttribute:()=>'/inbox/?domain='+domain},triggeringEvent:event};}};
}
test('negative control: unguarded bundled history loader reproduces delayed Back overwriting Forward',()=>{
    const app=setup(false);app.load('comment');app.load('dm');
    app.deliver(1,'dm');app.deliver(0,'comment');assert.equal(app.visible(),'comment');
});
test('new Forward cancels delayed Back before exact HTMX onload can replace the list',()=>{
    const app=setup();app.load('comment');app.load('dm');
    assert.equal(app.pending[0].aborted,true);assert.equal(app.pending[0].onload,null);
    assert.equal(app.deliver(1,'dm'),true);assert.equal(app.deliver(0,'comment'),false);assert.equal(app.visible(),'dm');
});
test('a new normal filter intent cancels the pending history loader',()=>{
    const app=setup();app.load('comment');app.emit('htmx:confirm',app.intent('mention'));
    assert.equal(app.pending[0].aborted,true);assert.equal(app.pending[0].onload,null);
    assert.equal(app.deliver(0,'comment'),false);assert.equal(app.visible(),'dm');
});
test('history intent invalidates normal requests and their queued original events immediately',()=>{
    const app=setup(),xhr={},trigger={},intent=app.intent('mention',trigger);
    app.emit('htmx:confirm',intent);app.emit('htmx:beforeRequest',{...intent,xhr});
    app.load('comment');
    const stale=app.emit('htmx:beforeSwap',{xhr,shouldSwap:true});assert.equal(stale.detail.shouldSwap,false);
    const replay=app.emit('htmx:confirm',intent);assert.equal(replay.prevented,true,'Queued old intent is not revived');
    assert.equal(app.pending[0].aborted,false,'Rejected queued request cannot cancel the newer history intent');
});
test('automatic refresh cannot supersede an in-progress history restoration',()=>{
    const app=setup();app.load('dm');
    const form={action:'/inbox/',entries:[['domain','dm']],matches:()=>true};form.closest=()=>form;
    assert.equal(app.emit('htmx:confirm',{target:{id:'inbox-list-content'},elt:form}).prevented,true);
    app.deliver(0,'dm');
    assert.equal(app.emit('htmx:confirm',{target:{id:'inbox-list-content'},elt:form}).prevented,undefined);
});

test('HTTP/network history failure releases only its own pending state and restores visible scope',()=>{
    for(const status of [409,404,0]) {
        const app=setup();app.load('comment');app.deliver(0,'unavailable',status);
        assert.equal(app.visible(),'dm');assert.equal(new URL(app.url()).searchParams.get('domain'),'dm');
        const form={action:'/inbox/',entries:[['domain','dm']],matches:()=>true};form.closest=()=>form;
        assert.equal(app.emit('htmx:confirm',{target:{id:'inbox-list-content'},elt:form}).prevented,undefined);
        assert.equal(app.notices.length,1);assert.match(app.notices[0].detail.message,/could not be restored/);
    }
});
test('late loadend from canceled Back cannot clear newer Forward or publish an error',()=>{
    const app=setup();app.load('comment');app.load('dm');app.pending[0].finish();
    assert.equal(app.notices.length,0);
    const form={action:'/inbox/',entries:[['domain','dm']],matches:()=>true};form.closest=()=>form;
    assert.equal(app.emit('htmx:confirm',{target:{id:'inbox-list-content'},elt:form}).prevented,true);
    app.deliver(1,'dm');assert.equal(app.visible(),'dm');assert.equal(app.notices.length,0);
});
