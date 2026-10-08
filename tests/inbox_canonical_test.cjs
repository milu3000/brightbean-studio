const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/inbox-canonical.js'), 'utf8');
const camel = value => value.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
function events(target) {
    const map = new Map();
    target.addEventListener = (name, fn) => map.set(name, [...(map.get(name) || []), fn]);
    target.removeEventListener = (name, fn) => map.set(name, (map.get(name) || []).filter(item => item !== fn));
    target.emit = (name, event = {}) => (map.get(name) || []).forEach(fn => fn(event));
    target.dispatchEvent = event => target.emit(event.type, event); return target;
}
class Node {
    constructor(tag, dataset = {}) { this.tag = tag; this.dataset = dataset; this.children = []; this.hidden = false; this.scrollTop = 0; this._text = ''; this.style = {}; events(this); }
    get isConnected() { return this.tag === 'document' || Boolean(this.parent?.isConnected); }
    get scrollHeight() { return this.querySelectorAll('[data-canonical-message]').length * 100; }
    get firstChild() { return this.children[0]; }
    appendChild(child) { child.parent = this; this.children.push(child); return child; }
    insertBefore(child, before) { if (!before) return this.appendChild(child); child.parent = this; this.children.splice(this.children.indexOf(before), 0, child); return child; }
    remove() { if (!this.parent) return; this.parent.children.splice(this.parent.children.indexOf(this),1); this.parent = null; }
    replaceWith(value) { const parent = this.parent, index = parent.children.indexOf(this); value.parent = parent; parent.children.splice(index,1,value); this.parent = null; }
    replaceChildren() { this.children.forEach(child => { child.parent = null; }); this.children = []; }
    set textContent(value) { this._text = value; this.replaceChildren(); }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(' '); }
    getClientRects() { return this.hidden ? [] : [{}]; }
    getAttribute(key) { return this[key]; }
    setAttribute(key,value) { this[key]=value; }
    removeAttribute(key) { delete this[key]; }
    matches(selector) {
        if (selector === 'time[datetime]') return false;
        if (selector.startsWith('#')) return this.id === selector.slice(1);
        const named = selector.match(/^\[name="([^"]+)"\]$/); if (named) return this.name === named[1];
        const data = selector.match(/^\[data-([a-z-]+)(?:="([^"]*)")?\]$/);
        return data ? Object.hasOwn(this.dataset, camel(data[1])) && (data[2] === undefined || this.dataset[camel(data[1])] === data[2]) : this.tag === selector;
    }
    closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector); }
    contains(node) { return this === node || this.children.some(child => child.contains(node)); }
    querySelectorAll(selector) {
        const [first, ...rest] = selector.split(' ');
        if (rest.length) return this.querySelectorAll(first).flatMap(child => child.querySelectorAll(rest.join(' ')));
        return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    cloneNode() { const copy = new Node(this.tag, {...this.dataset}); copy._text = this._text; this.children.forEach(child => copy.appendChild(child.cloneNode())); return copy; }
}
function page(id, names, next = '', token = '') {
    const root = new Node('div', {canonicalPage:'', conversationId:id, olderUrl:next, undatedUrl:'', readAckToken:token, latestUrl:'/head/a/?fragment=history', revision:'1', composerRevision:'0', composerScopeToken:'old-scope', composerObservationToken:'old-observation', sendAllowed:'true'});
    const dated = root.appendChild(new Node('div', {canonicalDated:''})); root.appendChild(new Node('div', {canonicalUndated:''}));
    names.forEach(name => { const item = dated.appendChild(new Node('article', {canonicalMessage:name})); item.textContent = name; }); return root;
}
function setup(token = '') {
    const document = new Node('document'); document.readyState = 'complete'; document.visibilityState = 'visible'; document.body = document;
    document.importNode = row => row.cloneNode(); document.createElement = tag => new Node(tag);
    const window = events({location:{href:'https://fixture.test/inbox/',origin:'https://fixture.test'}, htmx:{process(){},trigger(){}}});
    const requests = [], frames = [], fragments = new Map(); window.requestAnimationFrame = fn => frames.push(fn);
    const panel = document.appendChild(new Node('div', {canonicalPanel:'',conversationId:'a',readAckUrl:'/read/a/'}));
    const readStatus = panel.appendChild(new Node('p',{canonicalReadStatus:''}));
    const readRetry = panel.appendChild(new Node('button',{canonicalReadRetry:''})); readRetry.hidden = true;
    const unread = panel.appendChild(new Node('span',{canonicalUnread:''}));
    const scroller = panel.appendChild(new Node('div',{canonicalScroll:''}));
    const status = scroller.appendChild(new Node('p',{canonicalHistoryStatus:''}));
    const latest = panel.appendChild(new Node('button',{canonicalLatest:''})); latest.hidden = true;
    const refresh = panel.appendChild(new Node('button',{canonicalRefresh:''}));
    scroller.clientHeight = 100;
    const older = scroller.appendChild(new Node('button',{canonicalLoad:'dated'}));
    const history = scroller.appendChild(page('a',['saved-1','saved-2'],'/older/a/',token));
    const form = panel.appendChild(new Node('form',{inboxReplyForm:''}));
    const csrf = form.appendChild(new Node('input')); csrf.name = 'csrfmiddlewaretoken'; csrf.value = 'synthetic';
    const text = form.appendChild(new Node('textarea')); text.value = 'Unsent text';
    const send = form.appendChild(new Node('button',{inboxSendButton:''}));
    const revision = form.appendChild(new Node('input')); revision.name='composer_revision'; revision.value='0';
    const scope = form.appendChild(new Node('input')); scope.name='composer_scope_token'; scope.value='old-scope';
    const observation = form.appendChild(new Node('input')); observation.name='composer_observation_token'; observation.value='old-observation';
    const hold = panel.appendChild(new Node('p')); hold.id='reply-hold-reason';
    const context = {document,window,URL,URLSearchParams,AbortController,Date,Number,Set,Map,
        CustomEvent:class{constructor(type,options={}){this.type=type;this.detail=options.detail;}},
        fetch(url,options){return new Promise(resolve=>requests.push({url,options,resolve}));},
        DOMParser:class{parseFromString(value){const doc=new Node('document');doc.appendChild(fragments.get(value).cloneNode());return doc;}}
    };
    vm.runInNewContext(source,context);
    return {document,window,panel,history,latest,refresh,scroller,status,older,readStatus,readRetry,unread,text,send,revision,scope,observation,hold,requests,
        frame(){frames.splice(0).forEach(fn=>fn());}, click(node){document.emit('click',{target:node,preventDefault(){}});},
        async resolve(index,body,status=200){if(body instanceof Node)fragments.set(String(index),body);requests[index].resolve({ok:status>=200&&status<300,status,redirected:false,text:async()=>String(index),json:async()=>body});await new Promise(resolve=>setImmediate(resolve));}
    };
}
function rowGeometry(app) {
    const scroller=app.scroller;
    scroller.getBoundingClientRect=()=>({top:0,bottom:scroller.clientHeight});
    Object.defineProperty(scroller,'scrollHeight',{get(){return scroller.querySelectorAll('[data-canonical-message]').length*100+(app.status.textContent?24:0);}});
    function decorate(node) {
        const rows=[...(node.matches('[data-canonical-message]')?[node]:[]),...node.querySelectorAll('[data-canonical-message]')];
        rows.forEach(row=>{row.getBoundingClientRect=()=>{
            const index=scroller.querySelectorAll('[data-canonical-message]').indexOf(row);
            const top=(app.status.textContent?24:0)+index*100-scroller.scrollTop;
            return {top,bottom:top+100};
        };});
        return node;
    }
    decorate(app.history);
    const importNode=app.document.importNode;
    app.document.importNode=node=>decorate(importNode(node));
}
test('loading status and a fast prepend preserve the visible row',async()=>{
    const app=setup(); rowGeometry(app); app.frame(); app.scroller.scrollTop=30;
    const row=app.history.querySelector('[data-canonical-message]'), before=row.getBoundingClientRect().top;
    app.click(app.older);
    assert.equal(row.getBoundingClientRect().top,before,'The loading status itself must not move the row');
    await app.resolve(0,page('a',['old-1','old-2'],'/older/a/next/'));
    assert.equal(row.getBoundingClientRect().top,before,'Fast response preserves the pre-click row');
    assert.equal(app.text.value,'Unsent text');
});
test('history completion preserves a new position chosen during the request',async()=>{
    const app=setup(); rowGeometry(app); app.frame(); app.scroller.scrollTop=30;
    app.click(app.older); app.frame();
    app.scroller.scrollTop+=70; app.scroller.emit('scroll');
    const row=app.history.querySelectorAll('[data-canonical-message]')[1], during=row.getBoundingClientRect().top;
    await app.resolve(0,page('a',['old-1','old-2'],'/older/a/next/'));
    assert.equal(row.getBoundingClientRect().top,during,'Do not restore the stale pre-request anchor after user scrolling');
});
test('opening never loads earlier pages; explicit paging keeps draft and scroll anchor',async()=>{
    const app=setup();app.frame();assert.equal(app.requests.length,0);app.scroller.scrollTop=30;app.click(app.older);app.click(app.older);assert.equal(app.requests.length,1);
    await app.resolve(0,page('a',['old-1','old-2','saved-1']));
    assert.equal(app.history.querySelectorAll('[data-canonical-message]').length,4);assert.equal(app.scroller.scrollTop,230);assert.equal(app.text.value,'Unsent text');assert.equal(app.older.hidden,true);
});
test('late history after a conversation switch cannot alter the active DOM',async()=>{
    const app=setup();app.click(app.older);app.document.emit('htmx:beforeRequest',{detail:{elt:{dataset:{inboxOpenMessage:'b'}}}});
    assert.equal(app.requests[0].options.signal.aborted,true);await app.resolve(0,page('a',['late-secret']));assert(!app.history.textContent.includes('late-secret'));assert.equal(app.text.value,'Unsent text');
});
test('foreign fragments and stale cursor stop paging without replacing the composer',async()=>{
    for(const [body,status] of [[page('b',['foreign']),200],[page('a',[]),409]]){
        const app=setup();app.click(app.older);await app.resolve(0,body,status);assert.equal(app.older.hidden,true);assert.equal(app.history.querySelectorAll('[data-canonical-message]').length,2);assert.equal(app.text.value,'Unsent text');assert(app.status.textContent);
    }
});
test('access revocation removes timeline content and disables submission',async()=>{
    const app=setup();app.click(app.older);await app.resolve(0,{},403);assert.equal(app.history.children.length,0);assert.equal(app.send.disabled,true);assert.match(app.status.textContent,/unavailable/);
});
test('read acknowledgement only starts after a visible current panel reaches its animation frame',async()=>{
    const app=setup('rendered-1');assert.equal(app.requests.length,0);app.frame();assert.equal(app.requests.length,1);
    assert.match(app.requests[0].options.body,/rendered-1/);await app.resolve(0,{source:'canonical',conversation_id:'a',read_state:{unread:true}});assert.equal(app.unread.hidden,false);
    const hidden=setup('hidden');hidden.panel.hidden=true;hidden.frame();assert.equal(hidden.requests.length,0);
});
test('read failure preserves unread and retry is explicit; wrong-conversation acknowledgement is ignored',async()=>{
    const app=setup('visible');app.frame();await app.resolve(0,{},503);assert.equal(app.unread.hidden,false);assert.equal(app.readRetry.hidden,false);
    app.click(app.readRetry);app.frame();await app.resolve(1,{source:'canonical',conversation_id:'other',read_state:{unread:false}});assert.equal(app.unread.hidden,false);
    app.click(app.readRetry);app.frame();await app.resolve(2,{source:'canonical',conversation_id:'a',read_state:{unread:false}});assert.equal(app.unread.hidden,true);
});
test('switching before paint suppresses old read acknowledgements and composer-only swaps preserve scroll',()=>{
    const app=setup('visible');app.scroller.scrollTop=50;app.document.emit('htmx:afterRequest',{});assert.equal(app.scroller.scrollTop,50);
    app.document.emit('htmx:beforeRequest',{detail:{elt:{dataset:{inboxOpenMessage:'b'}}}});app.frame();assert.equal(app.requests.length,0);
});

test('history remains bounded while every older page stays reachable and Latest preserves draft', async()=>{
    const app=setup();
    for(let index=0;index<20;index++){
        app.scroller.scrollTop=0; app.click(app.older);
        await app.resolve(index,page('a',Array.from({length:30},(_,n)=>`older-${index*30+n}`),`/older/a/${index+1}`));
        assert(app.panel.querySelectorAll('[data-canonical-message]').length<=500);
    }
    assert(app.panel.textContent.includes('older-599')); assert.equal(app.older.hidden,false); assert.equal(app.latest.hidden,false);
    app.click(app.latest); await app.resolve(20,page('a',['newest-1','newest-2'],'/older/a/'));
    assert.equal(app.panel.querySelectorAll('[data-canonical-message]').length,2);
    assert(app.panel.textContent.includes('newest-1')); assert.equal(app.text.value,'Unsent text'); assert.equal(app.latest.hidden,true);
});
test('refresh while reading history only offers Latest and never acknowledges unseen content', async()=>{
    const app=setup(); app.click(app.older); await app.resolve(0,page('a',['older'],'/older/a/2'));
    app.scroller.scrollTop=10; app.click(app.refresh);
    const newer=page('a',['fresh inbound'],'/older/a/','unseen-read-token'); newer.dataset.revision='2';
    await app.resolve(1,newer); app.frame();
    assert.equal(app.requests.length,2); assert(!app.panel.textContent.includes('fresh inbound'));
    assert.match(app.latest.textContent,/New/); assert.equal(app.scroller.scrollTop,10); assert.equal(app.text.value,'Unsent text');
    app.click(app.latest); await app.resolve(2,newer); app.frame();
    assert(app.panel.textContent.includes('fresh inbound')); assert.match(app.requests[3].options.body,/unseen-read-token/);
});
test('accepted queued selection prevents the previous request swapping after a new intent',()=>{
    const app=setup('old-token'), xhr={};
    app.document.emit('htmx:beforeRequest',{detail:{elt:{dataset:{inboxOpenMessage:'a'}},xhr}});
    app.window.emit('inbox:selection-approved',{detail:{messageId:'b'}});
    const swap={detail:{xhr,shouldSwap:true},preventDefault(){this.prevented=true;}};
    app.document.emit('htmx:beforeSwap',swap); app.frame();
    assert.equal(swap.detail.shouldSwap,false); assert.equal(swap.prevented,true); assert.equal(app.requests.length,0);
});

test('only a visibly applied Latest with unchanged draft revision refreshes send observation and scope',async()=>{
    const app=setup(); app.click(app.older); await app.resolve(0,page('a',['older'],'/older/a/2'));
    const head=page('a',['new incoming']); head.dataset.revision='2'; head.dataset.composerScopeToken='new-scope'; head.dataset.composerObservationToken='new-observation';
    app.click(app.refresh); await app.resolve(1,head); app.frame();
    assert.equal(app.scope.value,'old-scope'); assert.equal(app.observation.value,'old-observation');
    app.click(app.latest); await app.resolve(2,head);
    assert.equal(app.observation.value,'old-observation'); app.frame();
    assert.equal(app.scope.value,'new-scope'); assert.equal(app.observation.value,'new-observation'); assert.equal(app.send.disabled,false); assert.equal(app.text.value,'Unsent text');
});
test('a concurrent saved-draft revision prevents Latest from silently changing its send scope',async()=>{
    const app=setup(), head=page('a',['new incoming']); head.dataset.composerRevision='1'; head.dataset.composerScopeToken='different-draft-scope';
    app.click(app.latest); await app.resolve(0,head); app.frame();
    assert.equal(app.scope.value,'old-scope'); assert.equal(app.send.disabled,true); assert.match(app.hold.textContent,/draft changed/); assert.equal(app.text.value,'Unsent text');
});

test('stale-send response exposes a usable Latest action even before the next poll',()=>{
    const app=setup();
    app.panel.appendChild(new Node('p',{inboxNeedsLatest:''}));
    app.document.emit('htmx:afterSwap',{detail:{target:app.panel}});
    assert.equal(app.latest.hidden,false); assert.equal(app.latest.textContent,'Latest');
});
