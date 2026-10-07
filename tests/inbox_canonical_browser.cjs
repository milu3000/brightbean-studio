/* Real DOM regression gate. All requests are fulfilled from synthetic exports.
 * Run by pytest; no downloaded browser, npm dependency, server, or live account.
 * --validate-fixtures only validates data. It does NOT run or pass browser tests.
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {Browser,withCleanup} = require('./browser/cdp.cjs');
const root = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
const option = name => args[args.indexOf(name) + 1];
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const Q = JSON.stringify;
const selectors = {
    panel: '[data-canonical-panel]', scroll: '[data-canonical-scroll]', rows: '[data-canonical-message]',
    older: '[data-canonical-load="dated"]', input: '[data-inbox-reply-form] textarea',
    quote: '[data-inbox-quote-id]', latest: '[data-canonical-latest]', refresh: '[data-canonical-refresh]'
};

function validateFixture(fixture) {
    assert.equal(fixture.origin, 'https://canonical.test');
    assert.equal(fixture.threads.length, 2);
    assert(fixture.rowCount > 500, 'Fixture must cross the rendered history bound');
    assert(Object.keys(fixture.routes).length > 20, 'Fixture must include full history continuation');
    for (const [url, html] of Object.entries(fixture.routes)) {
        assert(url.startsWith('/') && !url.startsWith('//'), `Not a local fixture route: ${url}`);
        assert.equal(typeof html, 'string');
    }
    const shell = fixture.routes[fixture.feed];
    for (const file of ['htmx.min.js', 'alpine.min.js', 'inbox-canonical.js', 'inbox-composer.js', 'inbox-quote.js', 'inbox-message-details.js']) {
        assert(shell.includes(file), `Missing actual bundled dependency ${file}`);
        assert(fs.statSync(path.join(root, 'static/js', file)).isFile());
    }
    for (const thread of fixture.threads) {
        assert(fixture.routes[thread.detail].includes(`data-conversation-id="${thread.id}"`));
        assert(thread.initialIds.length > 1);
        assert(thread.initialHistory.includes('data-canonical-page'));
    }
    assert(fixture.routes[fixture.freshPath].includes(fixture.freshId));
    assert.equal(fixture.lateImageUrl,'https://synthetic-browser-fixture.fbcdn.net/fixture/late-image.svg');
    assert.deepEqual(fixture.viewerCases.map(item=>item.kind),['body','attachments','retained']);
    const initial=fixture.threads[0].composerFields, fresh=fixture.freshComposerFields, conflict=fixture.conflictComposerFields;
    assert(initial.sendAllowed && fresh.sendAllowed,'Synthetic current owner can send before and after seeing fresh history');
    assert(initial.observation && initial.scope && fresh.observation && fresh.scope);
    assert.notEqual(initial.observation,fresh.observation); assert.notEqual(initial.scope,fresh.scope);
    assert.equal(initial.revision,fresh.revision); assert.notEqual(fresh.revision,conflict.revision);
    for (const item of fixture.viewerCases) {
        const pages=fixture.contentRoutes[item.path][item.kind];
        assert(pages.length>1,'Each content viewer must exercise signed continuation');
        assert.equal(pages.map(page=>page.payload.body||'').join(''),item.body);
        assert.deepEqual(pages.flatMap(page=>(page.payload.items||[]).map(media=>media.title)),item.titles);
    }
}

class Page {
    constructor(browser, fixture, session, target) {
        this.browser = browser; this.fixture = fixture; this.session = session; this.target = target;
        this.requests = []; this.posts = []; this.unexpected = []; this.exceptions = [];
        this.fresh = false; this.conflict = false; this.holdImages = true; this.images = [];
        this.delays = new Map(); this.dialogs = []; this.acceptDialogs = false;
        this.cancelled = new Set(); this.closed = false;
    }
    command(method, params = {}) { return this.browser.command(method, params, this.session); }
    async evaluate(expression) {
        const result = await this.command('Runtime.evaluate', {expression, returnByValue:true, awaitPromise:true});
        assert(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
        return result.result.value;
    }
    async wait(expression, label, timeout = 8000) {
        const end = Date.now() + timeout;
        while (Date.now() < end) {
            if (await this.evaluate(expression)) return;
            if (this.exceptions.length) throw new Error(this.exceptions.join('\n'));
            await delay(25);
        }
        throw new Error(`Timed out: ${label}\n${await this.evaluate('document.body.innerText.slice(-1800)')}`);
    }
    async click(selector) {
        await this.wait(`!!document.querySelector(${Q(selector)})`, `selector ${selector}`);
        await this.evaluate(`document.querySelector(${Q(selector)}).click()`);
    }
    async open(thread) {
        await this.click(`[data-inbox-open-message="${thread.id}"]`);
        await this.wait(`document.querySelector(${Q(selectors.panel)})?.dataset.conversationId === ${Q(thread.id)}`, 'selected conversation');
        await delay(100);
    }
    async type(value) {
        await this.evaluate(`(() => {const field=document.querySelector(${Q(selectors.input)});field.focus();field.value=${Q(value)};field.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    }
    async key(modifiers = 0) {
        await this.command('Input.dispatchKeyEvent', {type:'keyDown', key:'Enter', code:'Enter', windowsVirtualKeyCode:13, nativeVirtualKeyCode:13, modifiers, text:modifiers ? '' : '\r'});
        await this.command('Input.dispatchKeyEvent', {type:'keyUp', key:'Enter', code:'Enter', windowsVirtualKeyCode:13, nativeVirtualKeyCode:13, modifiers});
        await delay(80);
    }
    async settle() { await delay(180); }
    async fulfill(request, body, type = 'text/html', status = 200) {
        if (this.closed || this.cancelled.has(request.networkId)) return;
        try {
            await this.command('Fetch.fulfillRequest', {
                requestId: request.requestId, responseCode:status,
                responseHeaders:[{name:'Content-Type',value:type},{name:'Cache-Control',value:'no-store'}],
                body:Buffer.from(body).toString('base64')
            });
        } catch (error) {
            // A real AbortController may cancel an intentionally delayed Fetch
            // interception. Only CDP-confirmed cancellation is expected here.
            if (!this.closed && !this.cancelled.has(request.networkId)) throw error;
        }
    }
    async paused(event) {
        const url = new URL(event.request.url), route = url.pathname + url.search;
        this.requests.push({url:event.request.url, method:event.request.method, route});
        // This exact synthetic CDN URL exercises the production preview
        // allowlist. CDP returns local SVG bytes before any network connection.
        if (event.request.url === this.fixture.lateImageUrl) {
            if (this.holdImages) { this.images.push(event); return; }
            return this.fulfill(event, '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="320"><rect width="480" height="320" fill="#fed7aa"/></svg>', 'image/svg+xml');
        }
        if (url.origin !== this.fixture.origin) {
            this.unexpected.push(`External request blocked: ${event.request.url}`);
            return this.fulfill(event, 'External requests forbidden', 'text/plain', 403);
        }
        if (this.delays.has(route)) await delay(this.delays.get(route));
        if (url.pathname.startsWith('/static/js/')) {
            const name = url.pathname.slice('/static/js/'.length);
            if (!/^[a-z0-9.-]+\.js$/.test(name)) throw new Error(`Unsafe asset path ${name}`);
            return this.fulfill(event, fs.readFileSync(path.join(root, 'static/js', name)), 'text/javascript');
        }
        if (url.pathname === '/favicon.ico') return this.fulfill(event, '', 'image/x-icon', 204);
        if (event.request.method === 'POST') {
            if (Object.hasOwn(this.fixture.contentRoutes,url.pathname)) {
                const body=new URLSearchParams(event.request.postData||'');
                const pages=this.fixture.contentRoutes[url.pathname][body.get('kind')];
                const content=pages?.find(page=>page.cursor===(body.get('cursor')||''));
                assert(content,'Viewer request must use its exported kind and signed cursor');
                assert(Object.keys(event.request.headers).some(name=>name.toLowerCase()==='x-csrftoken'),'Explicit content fetch includes CSRF header');
                this.posts.push({route,body:event.request.postData||''});
                return this.fulfill(event,JSON.stringify(content.payload),'application/json');
            }
            const thread = this.fixture.threads.find(item => [item.read,item.send,item.save].includes(url.pathname));
            if (!thread) {
                this.unexpected.push(`Unknown POST ${route}`);
                return this.fulfill(event, 'Unexpected POST', 'text/plain', 400);
            }
            this.posts.push({route,body:event.request.postData || ''});
            if (url.pathname === thread.read) return this.fulfill(event, JSON.stringify({source:'canonical',conversation_id:thread.id,read_state:{unread:false},unread_count:0}), 'application/json');
            // Exercise browser submission and HTMX wiring only. Never dispatch
            // an external message, run an app send route, or invent a receipt.
            return this.fulfill(event, '', 'text/plain', 204);
        }
        if (url.pathname === this.fixture.feed && Object.entries(event.request.headers).some(([name,value])=>name.toLowerCase()==='hx-request'&&value==='true')) return this.fulfill(event, this.fixture.listHtml);
        if (url.searchParams.get('fragment') === 'history' && !url.searchParams.get('cursor')) {
            const thread = this.fixture.threads.find(item => item.detail === url.pathname);
            if (thread) return this.fulfill(event, this.conflict && thread === this.fixture.threads[0] ? this.fixture.conflictHistory : this.fresh && thread === this.fixture.threads[0] ? this.fixture.routes[this.fixture.freshPath] : thread.initialHistory);
        }
        if (Object.hasOwn(this.fixture.routes, route)) return this.fulfill(event, this.fixture.routes[route]);
        this.unexpected.push(`Unknown GET ${route}`);
        return this.fulfill(event, 'Unknown fixture request', 'text/plain', 404);
    }
    async releaseImages() {
        this.holdImages = false;
        for (const request of this.images.splice(0)) await this.paused(request);
    }
    async clean() {
        assert.deepEqual(this.unexpected, [], 'Every browser request must stay within the fixture manifest');
        assert.deepEqual(this.exceptions, [], 'Browser runtime errors');
        assert.deepEqual(this.browser.errors, [], 'CDP handler errors');
    }
    async close() { this.closed=true; await this.browser.command('Target.closeTarget', {targetId:this.target}); }
}

async function createPage(browser, fixture, width = 1365, height = 900) {
    const {targetId} = await browser.command('Target.createTarget', {url:'about:blank'});
    const {sessionId} = await browser.command('Target.attachToTarget', {targetId, flatten:true});
    const page = new Page(browser, fixture, sessionId, targetId);
    browser.on('Fetch.requestPaused', (event, session) => session === sessionId ? page.paused(event) : undefined);
    browser.on('Runtime.exceptionThrown', (event, session) => { if (session === sessionId) page.exceptions.push(JSON.stringify(event.exceptionDetails)); });
    browser.on('Network.loadingFailed', (event, session) => { if (session === sessionId && event.canceled) page.cancelled.add(event.requestId); });
    browser.on('Page.javascriptDialogOpening', async (event, session) => {
        if (session !== sessionId) return;
        page.dialogs.push(event.message);
        await page.command('Page.handleJavaScriptDialog', {accept:page.acceptDialogs});
    });
    await page.command('Runtime.enable');
    await page.command('Page.enable');
    await page.command('Network.enable');
    await page.command('Emulation.setDeviceMetricsOverride', {width,height,deviceScaleFactor:1,mobile:width<768});
    await page.command('Emulation.setTimezoneOverride', {timezoneId:'Asia/Taipei'});
    await page.command('Fetch.enable', {patterns:[{urlPattern:'*',requestStage:'Request'}]});
    await page.command('Page.navigate', {url:fixture.origin + fixture.feed});
    await page.wait('window.htmx && window.Alpine && document.querySelector("main").dataset.alpineReady === "true"', 'actual HTMX and Alpine initialized');
    await page.clean();
    return page;
}

const geometry = `(() => {
    const shell=document.querySelector('[data-canonical-shell]'),s=document.querySelector('${selectors.scroll}');
    const r=node=>{const x=node.getBoundingClientRect();return {top:x.top,bottom:x.bottom,left:x.left,right:x.right,width:x.width,height:x.height}};
    return {height:innerHeight,width:innerWidth,bodyHeight:document.documentElement.scrollHeight,
        shell:r(shell),header:r(document.querySelector('#inbox-canonical-header')),composer:r(document.querySelector('#inbox-canonical-composer')),
        scroll:r(s),gap:s.scrollHeight-s.clientHeight-s.scrollTop,scrollTop:s.scrollTop,
        listVisible:!!document.querySelector('.canonical-list-pane').getClientRects().length,
        count:document.querySelectorAll('${selectors.rows}').length};
})()`;
const anchor = `(() => {const s=document.querySelector('${selectors.scroll}'),top=s.getBoundingClientRect().top;
    const row=[...s.querySelectorAll('${selectors.rows}')].find(row=>row.getBoundingClientRect().bottom>top+1);
    return row?{id:row.dataset.canonicalMessage,offset:row.getBoundingClientRect().top-top}:null;})()`;
const anchorOffset = item => `(() => {const s=document.querySelector('${selectors.scroll}'),r=s.querySelector('[data-canonical-message="${item.id}"]');return r?r.getBoundingClientRect().top-s.getBoundingClientRect().top:null;})()`;

async function layoutScenario(browser, fixture, width, height) {
    const page = await createPage(browser, fixture, width, height);
    try {
        await page.open(fixture.threads[0]);
        const initial = await page.evaluate(geometry);
        assert(initial.scroll.height > 100 && initial.count > 1);
        assert(initial.gap <= 3, `Opening starts at latest message (${initial.gap})`);
        assert(initial.composer.bottom <= height + 2 && initial.header.top >= 0, 'Composer and header fit viewport');
        assert.equal(initial.listVisible, width >= 768);
        assert(initial.shell.left >= -2 && initial.shell.right <= width + 2, 'No horizontal overflow');
        await page.evaluate(`document.querySelector('${selectors.scroll}').scrollTop-=150`);
        await page.settle();
        const scrolled = await page.evaluate(geometry);
        assert(Math.abs(scrolled.composer.top-initial.composer.top)<1, 'Composer remains fixed while history scrolls');
        assert(Math.abs(scrolled.header.top-initial.header.top)<1, 'Header remains fixed while history scrolls');
        const time = await page.evaluate(`(() => {const t=document.querySelector('time[datetime]');return {raw:t.dateTime,text:t.textContent,title:t.title,expected:new Date(t.dateTime).toLocaleString(undefined,{timeZoneName:'short'})};})()`);
        assert.equal(time.text,time.expected); assert.equal(time.title,time.expected); assert.notEqual(time.text,time.raw);
        if (width < 768) {
            await page.click('[data-canonical-back]');
            assert.equal(await page.evaluate(`document.querySelector('[data-canonical-shell]').dataset.activePanel`),'list');
            assert(await page.evaluate(`!!document.querySelector('.canonical-list-pane').getClientRects().length`));
            await page.open(fixture.threads[1]);
        }
        await page.clean();
        process.stdout.write(`PASS real DOM layout ${width}x${height}, visible composer, local time, navigation\n`);
    } finally { await page.close(); }
}

async function draftHistoryScenario(browser, fixture) {
    const page = await createPage(browser, fixture);
    try {
        await page.open(fixture.threads[0]);
        const text='Unsent browser draft\nSecond line'; await page.type(text);
        await page.click('[data-inbox-quote-target]');
        const quoted = await page.evaluate(`document.querySelector('${selectors.quote}').value`);
        assert(quoted); assert.equal(await page.evaluate(`document.querySelector('[data-inbox-quote-preview]').hidden`), false);
        await page.click('[data-inbox-quote-cancel]');
        assert.equal(await page.evaluate(`document.querySelector('${selectors.quote}').value`),'');
        await page.click('[data-inbox-quote-target]');
        const before = await page.evaluate(anchor), count = await page.evaluate(`document.querySelectorAll('${selectors.rows}').length`);
        await page.click(selectors.older);
        await page.wait(`document.querySelectorAll('${selectors.rows}').length>${count}`, 'older rows prepended');
        await page.settle();
        const prependedOffset = await page.evaluate(anchorOffset(before));
        assert(Math.abs(prependedOffset-before.offset)<3,
            `Prepend preserves visible row: ${JSON.stringify({before,after:prependedOffset,geometry:await page.evaluate(geometry)})}`);
        // Loading the lazy image is intentional; otherwise an off-screen lazy
        // image would never exercise late decode and size correction.
        await page.evaluate(`document.querySelectorAll('[data-inbox-preview]').forEach(image=>image.loading='eager')`);
        await page.wait(`!!document.querySelector('[data-inbox-preview]')`, 'real attachment image');
        await page.releaseImages();
        await page.wait(`[...document.querySelectorAll('[data-inbox-preview]')].every(image=>image.complete&&image.naturalWidth>0)`, 'late image decoded');
        await page.settle();
        assert(Math.abs(await page.evaluate(anchorOffset(before))-before.offset)<3, 'Late image sizing preserves visible row');
        assert.equal(await page.evaluate(`document.querySelector('${selectors.input}').value`),text);
        assert.equal(await page.evaluate(`document.querySelector('${selectors.quote}').value`),quoted);
        await page.command('Emulation.setDeviceMetricsOverride',{width:1000,height:780,deviceScaleFactor:1,mobile:false});
        await page.settle();
        const resized = await page.evaluate(geometry);
        assert(resized.composer.bottom<=782 && resized.scroll.height>100, 'Viewport resize keeps composer visible');
        await page.click(`[data-inbox-open-message="${fixture.threads[1].id}"]`);
        await page.settle();
        assert(page.dialogs.length>0,'Unsaved text invokes actual confirmation dialog');
        assert.equal(await page.evaluate(`document.querySelector('${selectors.panel}').dataset.conversationId`),fixture.threads[0].id);
        assert.equal(await page.evaluate(`document.querySelector('${selectors.input}').value`),text);
        const cursorAfterCancel=await page.evaluate(`document.querySelector('[data-canonical-page]').dataset.olderUrl`);
        await page.click(selectors.older);
        await page.wait(`document.querySelector('[data-canonical-page]').dataset.olderUrl!==${Q(cursorAfterCancel)}`,'declining navigation keeps the current conversation reader active');
        page.acceptDialogs=true; await page.open(fixture.threads[1]);
        await page.clean();
        process.stdout.write('PASS real DOM drafts, quote select/cancel, unsaved navigation, prepend and late image anchor\n');
    } finally { await page.close(); }
}

async function keyboardScenario(browser, fixture) {
    const page = await createPage(browser, fixture);
    const sendCount = () => page.posts.filter(item=>item.route===fixture.threads[0].send).length;
    try {
        await page.open(fixture.threads[0]);
        assert.equal(await page.evaluate(`document.querySelector('[data-inbox-send-button]').disabled`),false,'Synthetic composer is send-capable');
        await page.type('Plain Enter'); await page.key();
        assert.equal(sendCount(),0); assert((await page.evaluate(`document.querySelector('${selectors.input}').value`)).includes('\n'));
        await page.type('Ctrl Enter'); await page.key(2); assert.equal(sendCount(),1);
        await page.click('[data-inbox-enter-send]'); await page.type('Enter enabled'); await page.key(); assert.equal(sendCount(),2);
        await page.type('Shift newline'); await page.key(8); assert.equal(sendCount(),2);
        await page.type('輸入中文');
        await page.evaluate(`document.querySelector('${selectors.input}').dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:'中'}))`);
        await page.key(); assert.equal(sendCount(),2,'IME active Enter does not submit');
        await page.evaluate(`(() => {const f=document.querySelector('${selectors.input}');f.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'中文'}));f.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,key:'Enter',code:'Enter',keyCode:229}));f.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,key:'Enter',isComposing:true}));})()`);
        assert.equal(sendCount(),2,'IME ending/229/composing Enter does not submit');
        await delay(120); await page.type('After IME'); await page.key(); assert.equal(sendCount(),3);
        const sent = page.posts.filter(item=>item.route===fixture.threads[0].send).map(item=>new URLSearchParams(item.body));
        assert.deepEqual(sent.map(item=>item.get('body')),['Ctrl Enter','Enter enabled','After IME']);
        for (const body of sent) for (const key of ['composer_action_nonce','composer_revision','composer_scope_token']) assert(body.has(key),`Actual HTMX submits ${key}`);
        await page.clean();
        process.stdout.write('PASS real DOM Enter/Ctrl+Enter/Shift+Enter/IME and intercepted HTMX submissions\n');
    } finally { await page.close(); }
}

async function raceScenario(browser, fixture) {
    const page = await createPage(browser, fixture), [a,b]=fixture.threads;
    try {
        await page.evaluate(`(() => {
            window.fixtureAcceptedB=false;window.fixturePanelsAfterB=[];
            window.addEventListener('inbox:selection-approved',event=>{if(event.detail.messageId===${Q(b.id)})window.fixtureAcceptedB=true;});
            new MutationObserver(records=>{if(!window.fixtureAcceptedB)return;for(const record of records)for(const node of record.addedNodes){
                if(node.nodeType!==1)continue;
                const panels=[...(node.matches('${selectors.panel}')?[node]:[]),...node.querySelectorAll('${selectors.panel}')];
                window.fixturePanelsAfterB.push(...panels.map(panel=>panel.dataset.conversationId));
            }}).observe(document.querySelector('#inbox-detail-panel'),{childList:true,subtree:true});
        })()`);
        page.delays.set(a.detail,200);
        await page.click(`[data-inbox-open-message="${a.id}"]`);
        await page.click(`[data-inbox-open-message="${b.id}"]`);
        await page.wait(`document.querySelector('${selectors.panel}')?.dataset.conversationId===${Q(b.id)}`, 'last selected conversation wins');
        await delay(350);
        assert.equal(await page.evaluate(`document.querySelector('${selectors.panel}').dataset.conversationId`),b.id);
        assert.equal(await page.evaluate('window.fixtureAcceptedB'),true,'B selection is accepted');
        assert(!(await page.evaluate('window.fixturePanelsAfterB')).includes(a.id),'Stale A must never flash after accepted B selection');
        assert(!page.posts.some(item=>item.route===a.read),'Stale A must never be read-acknowledged after choosing B');
        page.delays.delete(a.detail); await page.open(a);
        const history = await page.evaluate(`document.querySelector('[data-canonical-page]').dataset.olderUrl`);
        page.delays.set(history,350); await page.click(selectors.older);
        await page.open(b); await delay(450);
        assert.equal(await page.evaluate(`document.querySelector('${selectors.panel}').dataset.conversationId`),b.id);
        const ids=await page.evaluate(`[...document.querySelectorAll('${selectors.rows}')].map(row=>row.dataset.canonicalMessage)`);
        assert.deepEqual(ids,b.initialIds,'Late A history cannot contaminate B');
        assert.equal(await page.evaluate(`document.querySelector('${selectors.input}').value`),'');
        await page.clean();
        process.stdout.write('PASS real DOM fast conversation switching and stale history isolation\n');
    } finally { await page.close(); }
}

async function contentViewerScenario(browser, fixture) {
    const page=await createPage(browser,fixture),[a,b]=fixture.threads;
    const contentPosts=()=>page.posts.filter(item=>Object.hasOwn(fixture.contentRoutes,item.route));
    const view='[data-inbox-detail-view]',slot='[data-inbox-detail-content]';
    try {
        await page.open(a);
        assert.equal(contentPosts().length,0,'Opening a conversation never prefetches body/media/retained details');
        assert(!(await page.evaluate('document.body.textContent')).includes('PRIVATE SYNTHETIC RETAINED'),'Retained contents are absent before explicit reveal');
        for (const item of fixture.viewerCases) {
            const open=`[data-canonical-message="${item.id}"] [data-inbox-detail-open="${item.kind}"]`;
            await page.click(open);
            let output='',titles=[];
            const pages=fixture.contentRoutes[item.path][item.kind];
            for (let index=0;index<pages.length;index++) {
                const value=pages[index].payload;
                await page.wait(`document.querySelector('${slot}')?.firstElementChild?.textContent===${Q(value.body||'')}`,'explicit content chunk rendered');
                output+=await page.evaluate(`document.querySelector('${slot}').firstElementChild.textContent`);
                assert.equal(await page.evaluate(`document.querySelector('${view}').open`),true,'Native dialog is open');
                assert.equal(await page.evaluate(`document.querySelector('${slot}').querySelectorAll('script,img').length`),0,'Viewer uses plain text and links without fetching retained media');
                const links=await page.evaluate(`[...document.querySelectorAll('${slot} a')].map(a=>({title:a.parentElement.firstChild.textContent,rel:a.rel,referrer:a.referrerPolicy}))`);
                titles.push(...links.map(link=>link.title));
                for(const link of links){assert(link.rel.includes('noopener'));assert.equal(link.referrer,'no-referrer');}
                if(index<pages.length-1) await page.click('[data-inbox-detail-more]');
            }
            assert.equal(output,item.body); assert.deepEqual(titles,item.titles);
            assert.equal(await page.evaluate(`document.querySelector('[data-inbox-detail-more]').hidden`),true,'Last bounded chunk stops continuation');
            await page.click('[data-inbox-detail-close]');
            assert.equal(await page.evaluate(`document.querySelector('${view}')`),null,'Close removes the sensitive viewer DOM');
        }
        const retained=fixture.viewerCases.find(item=>item.kind==='retained');
        page.delays.set(retained.path,350);
        await page.click(`[data-canonical-message="${retained.id}"] [data-inbox-detail-open="retained"]`);
        await page.open(b);
        await delay(450);
        assert.equal(await page.evaluate(`document.querySelector('${view}')`),null,'Accepted conversation switch scrubs the viewer');
        assert(!(await page.evaluate('document.body.textContent')).includes('PRIVATE SYNTHETIC RETAINED'),'Late retained response cannot restore content after switching');
        assert.equal(await page.evaluate(`document.querySelector('${selectors.panel}').dataset.conversationId`),b.id);
        await page.clean();
        process.stdout.write('PASS real DOM explicit body/media/retained viewers, bounded Next, no prefetch, Close and late response scrub\n');
    } finally { await page.close(); }
}

async function boundedRefreshScenario(browser, fixture) {
    const page = await createPage(browser, fixture);
    const fields=`(() => {const form=document.querySelector('[data-inbox-reply-form]');return {
        observation:form.querySelector('[name="composer_observation_token"]').value,
        scope:form.querySelector('[name="composer_scope_token"]').value,
        revision:form.querySelector('[name="composer_revision"]').value,
        sendAllowed:!form.querySelector('[data-inbox-send-button]').disabled};})()`;
    try {
        await page.open(fixture.threads[0]); await page.type('Keep draft while browsing the archive');
        await page.click('[data-inbox-quote-target]');
        const quote=await page.evaluate(`document.querySelector('${selectors.quote}').value`);
        const initialFields=await page.evaluate(fields);
        assert.deepEqual(initialFields,fixture.threads[0].composerFields);
        await page.evaluate(`(() => {
            window.fixtureBeforeOwnerPaint=[];
            document.addEventListener('inbox:history-loaded',()=>{
                if(document.querySelector('[data-canonical-message="${fixture.freshId}"]')) window.fixtureBeforeOwnerPaint.push(${fields});
            });
        })()`);
        await page.releaseImages();
        let previous=''; let rounds=0;
        while (await page.evaluate(`!!document.querySelector('[data-canonical-page]').dataset.olderUrl`)) {
            const cursor = await page.evaluate(`document.querySelector('[data-canonical-page]').dataset.olderUrl`);
            assert.notEqual(cursor,previous,'Cursor must advance beyond bounded visible history'); previous=cursor;
            await page.click(selectors.older);
            await page.wait(`document.querySelector('[data-canonical-page]').dataset.olderUrl!==${Q(cursor)}`, 'history continuation advances');
            await page.settle();
            const count=await page.evaluate(`document.querySelectorAll('${selectors.rows}').length`);
            assert(count<=500,`Rendered window stays bounded (${count})`);
            assert(++rounds<=25,'History continuation terminates');
        }
        assert(rounds>=16,'Read beyond the old500-row cutoff');
        assert(await page.evaluate(`document.querySelector('[data-canonical-page]').textContent.includes('Synthetic browser message 0539')`),'Oldest saved row remains reachable');
        await page.evaluate(`document.querySelector('${selectors.scroll}').scrollTop=300`);
        await page.settle(); const oldAnchor=await page.evaluate(anchor);
        page.fresh=true; await page.click(selectors.refresh);
        await page.wait(`!document.querySelector('${selectors.latest}').hidden&&/new/i.test(document.querySelector('${selectors.latest}').textContent)`, 'new saved messages advertised while browsing history');
        assert(Math.abs(await page.evaluate(anchorOffset(oldAnchor))-oldAnchor.offset)<3,'DB-only refresh does not jump old history');
        assert.equal(await page.evaluate(`document.querySelector('${selectors.input}').value`),'Keep draft while browsing the archive');
        assert.deepEqual(await page.evaluate(fields),initialFields,'Offering Latest never advances owner observation or scope');
        assert.equal(await page.evaluate(`document.querySelector('${selectors.quote}').value`),quote,'Background refresh keeps the local quote');
        await page.click(selectors.latest);
        await page.wait(`!!document.querySelector('[data-canonical-message="${fixture.freshId}"]')`, 'Latest displays freshly saved message');
        await page.wait(`document.querySelector('[name="composer_observation_token"]').value===${Q(fixture.freshComposerFields.observation)}`, 'visible Latest advances owner observation on its animation frame');
        assert.deepEqual((await page.evaluate('window.fixtureBeforeOwnerPaint'))[0],initialFields,'Inserted history does not advance owner tokens before the visible animation frame');
        assert.deepEqual(await page.evaluate(fields),fixture.freshComposerFields,'Visible Latest installs its exact owner observation/scope with unchanged composer revision');
        await page.settle();
        assert((await page.evaluate(geometry)).gap<=3,'Latest scrolls to latest');
        assert.equal(await page.evaluate(`document.querySelector('${selectors.input}').value`),'Keep draft while browsing the archive');
        assert.equal(await page.evaluate(`document.querySelector('${selectors.quote}').value`),quote);
        page.conflict=true;
        await page.click(selectors.refresh);
        await page.wait(`document.querySelector('[data-canonical-page]').dataset.composerRevision===${Q(fixture.conflictComposerFields.revision)}&&document.querySelector('[data-inbox-send-button]').disabled`, 'concurrent saved draft revision keeps Send held');
        const held=await page.evaluate(fields);
        assert.deepEqual(held,{...fixture.freshComposerFields,sendAllowed:false},'Conflicting composer revision cannot replace locally held tokens');
        assert.equal(await page.evaluate(`document.querySelector('${selectors.input}').value`),'Keep draft while browsing the archive');
        assert.equal(await page.evaluate(`document.querySelector('${selectors.quote}').value`),quote);
        assert(await page.evaluate(`document.querySelector('#reply-hold-reason').textContent.includes('saved draft changed')`));
        await page.click(selectors.latest); await page.settle();
        assert.equal(await page.evaluate(`document.querySelector('[data-inbox-send-button]').disabled`),true,'Latest cannot release a mismatched composer revision');
        assert(!page.requests.some(item=>/native|provider|sync/.test(item.route)), 'Reading and refresh never invokes a native/provider endpoint');
        await page.clean();
        process.stdout.write('PASS real DOM bounded continuation, Latest, DB-only refresh, visible owner metadata and concurrent draft hold\n');
    } finally { await page.close(); }
}

async function main() {
    const validateOnly = args.includes('--validate-fixtures');
    const fixture = JSON.parse(fs.readFileSync(option(validateOnly?'--validate-fixtures':'--fixtures'),'utf8'));
    validateFixture(fixture);
    if (validateOnly) { process.stdout.write('Synthetic fixture contract valid; browser DOM assertions NOT RUN\n'); return; }
    const binary = option('--browser'); assert(binary && fs.existsSync(binary),'Installed Chrome/Chromium is required');
    const profile=fs.mkdtempSync(path.join(os.tmpdir(),'brightbean-chromium-'));
    const browser=new Browser(binary,profile);
    await withCleanup(async () => { try {
        // Cold browser startup is separate from the 8-second command/DOM budget.
        // A real handshake must still succeed; this never retries or skips it.
        const version=await browser.command('Browser.getVersion',{},undefined,30000);
        assert(/Chrome|Chromium/.test(version.product),`Expected real Chromium, received ${version.product}`);
        process.stdout.write(`Browser ${version.product}, actual bundled HTMX/Alpine, synthetic CDP-only fixtures\n`);
        await layoutScenario(browser,fixture,1365,900);
        await layoutScenario(browser,fixture,390,844);
        await draftHistoryScenario(browser,fixture);
        await keyboardScenario(browser,fixture);
        await raceScenario(browser,fixture);
        await contentViewerScenario(browser,fixture);
        await boundedRefreshScenario(browser,fixture);
        assert.deepEqual(browser.errors,[]);
        process.stdout.write('CHROMIUM DOM REGRESSIONS PASSED\n');
    } catch (error) {
        throw new Error(`${error.stack}\nAsync CDP failures:\n${browser.errors.join('\n')}\nChromium stderr:\n${browser.stderr}`);
    } }, async () => {
        await browser.close();
        // Child processes can finish writing the temporary profile after exit.
        fs.rmSync(profile,{recursive:true,force:true,maxRetries:5,retryDelay:100});
    });
}
main().catch(error=>{process.stderr.write(`${error.stack}\n`);process.exitCode=1;});
