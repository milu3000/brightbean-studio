/* Actual base.html + built Tailwind. Synthetic DB responses, no live network. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {Browser, withCleanup} = require('./browser/cdp.cjs');
const {createPage} = require('./inbox_canonical_browser.cjs');
const args = process.argv.slice(2);
const value = key => args[args.indexOf(key) + 1];
const fixture = JSON.parse(fs.readFileSync(value('--fixtures'), 'utf8'));

// Check every clipping ancestor, not merely the outer viewport: the previous
// negative margins passed a viewport-only test but lost text inside base.html.
const clipped = `(() => {
    const failures=[];
    for (const selector of ['.canonical-shell > header','[data-inbox-search]','[data-inbox-platform]',
        '[data-inbox-account]','[data-inbox-open-message]']) {
        const element=document.querySelector(selector);
        if (!element || !element.getClientRects().length) {
            failures.push({selector,reason:'Required list control is missing or hidden'});
            continue;
        }
        if (['hidden','collapse'].includes(getComputedStyle(element).visibility)) {
            failures.push({selector,reason:'Required list control is not visible'});
            continue;
        }
        const box=element.getBoundingClientRect();
        if (box.width<=0 || box.height<=0) {
            failures.push({selector,reason:'Required list control has no visible size'});
            continue;
        }
        for (let parent=element.parentElement;parent;parent=parent.parentElement) {
            const style=getComputedStyle(parent),bounds=parent.getBoundingClientRect();
            const clipX=/hidden|auto|scroll|clip/.test(style.overflowX);
            const clipY=/hidden|auto|scroll|clip/.test(style.overflowY);
            if ((clipX && (box.left<bounds.left-1 || box.right>bounds.right+1)) ||
                (clipY && (box.top<bounds.top-1 || box.bottom>bounds.bottom+1))) {
                failures.push({selector,parent:parent.className,box:{left:box.left,top:box.top,right:box.right,bottom:box.bottom},bounds:{left:bounds.left,top:bounds.top,right:bounds.right,bottom:bounds.bottom}});
                break;
            }
        }
    }
    return failures;
})()`;

async function screenshot(page, name) {
    const result=await page.command('Page.captureScreenshot',{format:'png'});
    fs.mkdirSync(fixture.screenshotDirectory,{recursive:true});
    fs.writeFileSync(path.join(fixture.screenshotDirectory,name+'.png'),Buffer.from(result.data,'base64'));
}

async function oldLayoutNegativeControl(page) {
    // Exact shell rules from base commit 8c20d79b. Reintroduce the reported
    // clipping inside the real base wrapper, then prove normal layout recovers.
    const oldCss='.canonical-shell{display:flex;flex-direction:column;min-height:0;height:calc(100% + 1.5rem);margin:-.75rem;overflow:hidden}\n'+
        '@media(min-width:640px){.canonical-shell{height:calc(100% + 2rem);margin:-1rem}}\n'+
        '@media(min-width:1024px){.canonical-shell{height:calc(100% + 3rem);margin:-1.5rem}}';
    try {
        await page.evaluate(`(() => {const style=document.createElement('style');style.id='old-layout-negative-control';style.textContent=${JSON.stringify(oldCss)};document.head.append(style);})()`);
        await page.settle();
        await screenshot(page,'regression-negative-margins');
        const failures=await page.evaluate(clipped);
        assert(failures.some(item=>item.parent),'The old negative margins must fail real ancestor geometry');
    } finally {
        await page.evaluate(`document.querySelector('#old-layout-negative-control')?.remove()`);
        await page.settle();
    }
    assert.deepEqual(await page.evaluate(clipped),[],'Restoring candidate styles returns to valid geometry');
    for (const mode of ['hidden','missing','invisible']) {
        const failures=await page.evaluate(`(() => {
            const element=document.querySelector('.canonical-shell > header'),parent=element.parentElement,next=element.nextSibling;
            const saved=element.getAttribute('style');
            try {
                if (${JSON.stringify(mode)}==='missing') element.remove();
                else if (${JSON.stringify(mode)}==='invisible') element.style.visibility='hidden';
                else element.style.display='none';
                return ${clipped};
            } finally {
                if (!element.isConnected) parent.insertBefore(element,next);
                if (saved===null) element.removeAttribute('style');else element.setAttribute('style',saved);
            }
        })()`);
        assert(failures.some(item=>item.selector==='.canonical-shell > header'),`${mode} required header must fail`);
    }
    assert.deepEqual(await page.evaluate(clipped),[],'Negative controls leave the real page intact');
    process.stdout.write('PASS real DOM negative controls: old margins, missing and hidden header fail; candidate recovers\n');
}

const unifiedFailures = `(() => {
    const failures=[];
    if (document.querySelectorAll('[data-unified-shell]').length!==1) failures.push('split-shell');
    if (document.querySelectorAll('[data-inbox-account]').length!==1 || document.querySelector('[data-canonical-account-switcher]') || /Switch account/.test(document.querySelector('main').innerText)) failures.push('duplicate-account-navigation');
    for (const row of document.querySelectorAll('[data-unified-sender]')) {
        if (/^@?\\d{5,}$/.test(row.textContent.trim())) failures.push('numeric-sender');
    }
    if (/@\\d{5,}/.test(document.querySelector('main').innerText)) failures.push('numeric-handle');
    return failures;
})()`;

async function openLegacy(page,thread) {
    await page.click(`[data-inbox-open-message="${thread.id}"]`);
    await page.wait(`document.querySelector('[data-inbox-panel]')?.dataset.selectedMessageId===${JSON.stringify(thread.id)}`,'legacy detail');
    await page.settle();
}
async function chooseType(page,domain) {
    await page.click(`[data-unified-domain="${domain}"]`);
    await page.wait(`document.querySelector('[data-unified-domain][aria-current="page"]')?.dataset.unifiedDomain===${JSON.stringify(domain)}`,'selected type');
    await page.settle();
    assert.equal(await page.evaluate('document.querySelector("[data-unified-shell]")===window.originalUnifiedShell'),true,'Type switching preserves the exact outer shell node');
    assert.equal(await page.evaluate('document.querySelector("#inbox-detail-panel")===window.originalDetailPane'),true,'Type switching preserves the exact detail target');
    assert.deepEqual(await page.evaluate(unifiedFailures),[]);
}
async function selectFilter(page,name,value) {
    await page.evaluate(`(() => { const control=document.querySelector('#inbox-filters [name="${name}"]');control.value=${JSON.stringify(value)};control.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await page.wait(`new URL(location.href).searchParams.get(${JSON.stringify(name)})===${JSON.stringify(value)}`,'filter URL');
    await page.settle();
}

async function scenario(browser,width,height,collapsed) {
    const page=await createPage(browser,{...fixture,sidebarCollapsed:collapsed},width,height);
    try {
        await page.settle();
        await page.evaluate('window.originalUnifiedShell=document.querySelector("[data-unified-shell]");window.originalDetailPane=document.querySelector("#inbox-detail-panel")');
        await screenshot(page,`unified-shell-${width}-${collapsed?'collapsed':'expanded'}`);
        assert.deepEqual(await page.evaluate(clipped),[], 'Controls and first row fit every clipping ancestor');
        assert.deepEqual(await page.evaluate(unifiedFailures),[]);
        assert.equal(await page.evaluate(`document.querySelector('[data-unified-domain][aria-current="page"]').dataset.unifiedDomain`),'all');
        assert.deepEqual(await page.evaluate(`Array.from(new Set(Array.from(document.querySelectorAll('[data-unified-row]'),row=>row.dataset.messageType))).sort()`),['comment','dm','mention','review']);
        if (width===1365 && !collapsed) await oldLayoutNegativeControl(page);
        assert(await page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'),'No document horizontal overflow');
        assert(await page.evaluate(`Array.from(document.querySelectorAll('#inbox-filters select')).every(select=>{
            const style=getComputedStyle(select),canvas=document.createElement('canvas'),context=canvas.getContext('2d');
            context.font=style.fontWeight+' '+style.fontSize+' '+style.fontFamily;
            return context.measureText(select.selectedOptions[0].textContent).width + parseFloat(style.paddingLeft) + parseFloat(style.paddingRight) + 16 <= select.clientWidth;
        })`),'Default filter labels fit beside their dropdown arrow');
        assert(await page.evaluate(`document.querySelector('#inbox-message-list').getBoundingClientRect().height > 180`),'Filters leave room for messages');
        const ids=await page.evaluate(`Array.from(document.querySelector('[data-inbox-account]').options,option=>option.value).filter(Boolean)`);
        assert.deepEqual(ids.sort(),[...fixture.legacyAccountIds,fixture.canonicalAccount].sort(),'One selector contains every eligible/historical account');
        assert(await page.evaluate(`Array.from(document.querySelector('[data-inbox-account]').options).filter(option=>option.value).every(option=>/Facebook|Instagram|threads/.test(option.textContent))`),'Repeated account names include platform labels');
        await page.open(fixture.threads[0]);
        assert(await page.evaluate(`(() => {const c=document.querySelector('#inbox-canonical-composer').getBoundingClientRect(), h=document.querySelector('#inbox-canonical-header').getBoundingClientRect();return c.bottom<=innerHeight+1&&c.height>40&&h.top>=0;})()`),'Canonical header and composer fit');
        assert.equal(await page.evaluate(`!!document.querySelector('.canonical-list-pane').getClientRects().length`),width>=768);
        if (width<768) await page.click('[data-canonical-back]');
        await openLegacy(page,fixture.legacyThreads.find(thread=>thread.kind==='dm'));
        assert(await page.evaluate(`(() => {const p=document.querySelector('[data-inbox-panel]').getBoundingClientRect(),c=document.querySelector('[data-inbox-reply-form]').getBoundingClientRect();return p.top>=0&&c.bottom<=innerHeight+1&&c.height>40;})()`),'Legacy DM uses the same bounded detail pane');
        if (width<768) {
            await page.click('[data-inbox-back]');
            assert.equal(await page.evaluate(`document.querySelector('[data-unified-shell]').dataset.activePanel`),'list');
            assert.deepEqual(await page.evaluate(clipped),[]);
        }
        await chooseType(page,'comment');
        await openLegacy(page,fixture.legacyThreads.find(thread=>thread.kind==='comment'));
        assert(await page.evaluate(`!document.querySelector('[data-inbox-panel]').innerText.includes('@9911223344556677')`),'Legacy detail never renders numeric provider ID as a handle');
        await screenshot(page,`unified-comment-${width}-${collapsed?'collapsed':'expanded'}`);
        if (width<768) {await page.click('[data-inbox-back]');assert.deepEqual(await page.evaluate(clipped),[]);}
        await page.clean();
        process.stdout.write(`PASS actual unified shell ${width}x${height}, ${collapsed?'collapsed':'expanded'} sidebar; canonical/legacy DM/comment and mobile back\n`);
    } finally {await page.close();}
}

async function mixedInteractionScenario(browser) {
    const page=await createPage(browser,fixture,1365,900);
    try {
        await page.evaluate('window.originalUnifiedShell=document.querySelector("[data-unified-shell]");window.originalDetailPane=document.querySelector("#inbox-detail-panel")');
        const comment=fixture.legacyThreads.find(thread=>thread.kind==='comment');
        await chooseType(page,'comment');
        await openLegacy(page,comment);
        await page.type('Unsaved legacy reply');
        await chooseType(page,'dm');
        await page.click(`[data-inbox-open-message="${fixture.threads[0].id}"]`);
        await page.settle();
        assert.equal(page.dialogs.length,1,'Cross-source selection asks before discarding legacy draft');
        assert.equal(await page.evaluate(`document.querySelector('[data-inbox-reply-form] textarea').value`),'Unsaved legacy reply');
        page.acceptDialogs=true;
        await page.open(fixture.threads[0]);
        await page.type('Unsaved canonical reply');
        await chooseType(page,'comment');
        page.acceptDialogs=false;
        await page.click(`[data-inbox-open-message="${comment.id}"]`);
        await page.settle();
        assert.equal(await page.evaluate(`document.querySelector('[data-inbox-reply-form] textarea').value`),'Unsaved canonical reply');
        page.acceptDialogs=true;
        await openLegacy(page,comment);
        assert.equal(page.posts.filter(post=>/reply|send/.test(post.route)).length,0,'No real or intercepted send attempted');
        await chooseType(page,'mention');
        assert.deepEqual(await page.evaluate(`Array.from(document.querySelectorAll('[data-unified-row]'),row=>row.dataset.messageType)`),['mention']);
        await chooseType(page,'review');
        assert(await page.evaluate(`document.querySelector('.unified-notice')?.innerText.includes('Review ingestion is not connected')`),'Review view states its actual ingestion limit');
        assert.equal(await page.evaluate(`document.querySelector('[data-inbox-account]').options.length`),2,'Review account choices retain only saved review account plus All');
        await page.clean();
        process.stdout.write('PASS real mixed type switching, honest Review notice, cross-source unsaved drafts, same shell and detail nodes\n');
    } finally {await page.close();}
}

async function filteringScenario(browser) {
    const page=await createPage(browser,fixture,1365,900);
    try {
        await page.evaluate('window.originalUnifiedShell=document.querySelector("[data-unified-shell]");window.originalDetailPane=document.querySelector("#inbox-detail-panel")');
        assert.deepEqual(await page.evaluate(`Array.from(document.querySelectorAll('[data-unified-row]'),row=>row.dataset.inboxOpenMessage)`),fixture.firstRows);
        await page.click('[data-unified-next]');
        await page.wait(`new URL(location.href).searchParams.has('cursor')`,'pagination cursor');
        await page.settle();
        assert.deepEqual(await page.evaluate(`Array.from(document.querySelectorAll('[data-unified-row]'),row=>row.dataset.inboxOpenMessage)`),fixture.nextRows,'Actual signed page has no repeats/omissions');
        await page.evaluate(`(() => {const input=document.querySelector('[data-inbox-search]');input.value='synthetic.visitor';input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
        await page.wait(`new URL(location.href).searchParams.get('q')==='synthetic.visitor'`,'search from later page');
        assert.equal(await page.evaluate(`new URL(location.href).searchParams.has('cursor')`),false,'Search resets stale pagination');
        await page.click('[data-inbox-clear-search]');
        await page.wait(`!new URL(location.href).searchParams.has('q')`,'clear query');
        await chooseType(page,'comment');
        await page.evaluate(`document.querySelector('[name="status"]').value='unread'`);
        await selectFilter(page,'account',fixture.commentAccount);
        assert.equal(await page.evaluate(`document.querySelector('[name="status"]').value`),'','Changing account clears source-specific status');
        await page.evaluate(`(() => {const input=document.querySelector('[data-inbox-search]');input.value='synthetic.visitor';input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
        await page.wait(`new URL(location.href).searchParams.get('q')==='synthetic.visitor'`,'scoped search');
        await page.click('[data-inbox-clear-search]');
        await page.wait(`!new URL(location.href).searchParams.has('q')`,'scoped clear');
        assert.equal(await page.evaluate(`document.querySelector('[data-inbox-account]').value`),fixture.commentAccount,'Clear preserves selected account');
        assert.equal(await page.evaluate(`document.querySelector('[data-unified-domain][aria-current]').dataset.unifiedDomain`),'comment','Clear preserves type');
        await page.clean();
        process.stdout.write('PASS actual mixed keyset pagination, search reset, one scoped account selector and search clearing\n');
    } finally {await page.close();}
}

async function mixedRaceScenario(browser) {
    for (const delayed of [fixture.threads[0],fixture.legacyThreads.find(thread=>thread.kind==='dm')]) {
        const page=await createPage(browser,fixture,1365,900);
        try {
            const newer=fixture.legacyThreads.find(thread=>thread.kind==='comment');
            page.delays.set(delayed.detail,450);
            await page.click(`[data-inbox-open-message="${delayed.id}"]`);
            await page.wait(`true`,'request started');
            await page.evaluate(`window.seenPanels=[];window.panelObserver=new MutationObserver(()=>{const panel=document.querySelector('[data-inbox-panel]');if(panel)window.seenPanels.push(panel.dataset.conversationId||panel.dataset.selectedMessageId);});window.panelObserver.observe(document.querySelector('#inbox-detail-panel'),{childList:true,subtree:true});`);
            await openLegacy(page,newer);
            assert.equal(await page.evaluate(`window.seenPanels.includes(${JSON.stringify(delayed.id)})`),false,'Delayed superseded source never flashes into detail');
            assert.equal(page.posts.some(post=>post.route===fixture.threads[0].read),false,'A stale canonical detail is never acknowledged');
            await page.clean();
        } finally {await page.close();}
    }
    const page=await createPage(browser,fixture,1365,900);
    try {
        page.delays.set(fixture.feed+'?domain=comment',450);
        await page.click('[data-unified-domain="comment"]');
        await page.evaluate(`window.seenTypes=[];window.typeObserver=new MutationObserver(()=>window.seenTypes.push(document.querySelector('[data-unified-domain][aria-current]')?.dataset.unifiedDomain));window.typeObserver.observe(document.querySelector('#inbox-list-content'),{childList:true,subtree:true});`);
        await page.click('[data-unified-domain="mention"]');
        await page.evaluate(`htmx.trigger(document.querySelector('[data-unified-filters]'),'inbox:refresh-list')`);
        await page.wait(`document.querySelector('[data-unified-domain][aria-current]')?.dataset.unifiedDomain==='mention'`,'latest type after old automatic refresh');
        await page.settle();
        assert.equal(await page.evaluate(`window.seenTypes.includes('comment')`),false,'Stale type list never flashes');
        assert.equal(await page.evaluate(`document.querySelector('[data-unified-domain][aria-current]').dataset.unifiedDomain`),'mention','Old read refresh cannot override the user type');
        await page.clean();
    } finally {await page.close();}
    process.stdout.write('PASS stale canonical/legacy detail and list responses cannot replace newer selection\n');
}

async function reportedBugNegativeControls(browser) {
    for (const [index,route] of fixture.negativeRoutes.entries()) {
        const page=await createPage(browser,{...fixture,feed:route},1365,900);
        try {
            await page.settle();
            const failures=await page.evaluate(unifiedFailures);
            assert(failures.includes(index===0?'split-shell':'duplicate-account-navigation'),'Exact old legacy split/canonical duplicate layout is rejected');
            await screenshot(page,index===0?'regression-legacy-split-shell':'regression-duplicate-account-selector');
            await page.clean();
        } finally {await page.close();}
    }
    const page=await createPage(browser,fixture,1365,900);
    try {
        assert.deepEqual(await page.evaluate(unifiedFailures),[]);
        await page.evaluate(`document.querySelector('[data-unified-sender]').textContent='9911887766554433';const handle=document.createElement('span');handle.textContent='@9911223344556677';document.querySelector('[data-unified-row]').append(handle)`);
        const failures=await page.evaluate(unifiedFailures);
        assert(failures.includes('numeric-sender')&&failures.includes('numeric-handle'),'Reported raw provider name and @numeric handle regressions are caught');
        await screenshot(page,'regression-raw-provider-identity');
        await page.clean();
    } finally {await page.close();}
    process.stdout.write('PASS genuine old split and duplicate markup plus reported raw identity negative controls\n');
}

async function main() {
    const profile=fs.mkdtempSync(path.join(os.tmpdir(),'brightbean-shell-chromium-'));
    const browser=new Browser(value('--browser'),profile);
    await withCleanup(async()=>{try {
        await browser.command('Browser.getVersion',{},undefined,30000);
        for (const [width,height,collapsed] of [[1365,900,false],[1052,1344,true],[1024,768,false],[768,900,false],[390,844,false]]) {
            await scenario(browser,width,height,collapsed);
        }
        await mixedInteractionScenario(browser);
        await filteringScenario(browser);
        await mixedRaceScenario(browser);
        await reportedBugNegativeControls(browser);
        for (const width of [1365,390]) {
            const standalone={...fixture,feed:fixture.threads[0].detail+'?standalone=1'};
            const page=await createPage(browser,standalone,width,844);
            try {
                assert.equal(await page.evaluate(`Array.from(document.querySelectorAll('a')).filter(link=>link.textContent.trim()==='Back to inbox'&&link.getClientRects().length&&getComputedStyle(link).display!=='none'&&!['hidden','collapse'].includes(getComputedStyle(link).visibility)).length`),1,'Direct conversation has one visible route back to the inbox');
                assert(await page.evaluate(`(() => {const p=document.querySelector('.canonical-panel').getBoundingClientRect(), c=document.querySelector('#inbox-canonical-composer').getBoundingClientRect();return p.top>=0&&p.left>=0&&p.right<=innerWidth+1&&c.bottom<=innerHeight+1&&c.height>40;})()`),'Direct conversation URL keeps its composer on screen');
                await screenshot(page,`standalone-${width}`);
                await page.clean();
            } finally {await page.close();}
        }
        assert.deepEqual(browser.errors,[]);
        process.stdout.write('ACTUAL SHELL CHROMIUM PASSED\n');
    } catch(error) {throw new Error(`${error.stack}\nChromium stderr:\n${browser.stderr}`);}
    },async()=>{await browser.close();fs.rmSync(profile,{recursive:true,force:true,maxRetries:5,retryDelay:100});});
}
main().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
