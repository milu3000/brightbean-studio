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

async function scenario(browser,width,height,collapsed) {
    const page=await createPage(browser,{...fixture,sidebarCollapsed:collapsed},width,height);
    try {
        await page.settle();
        const name=`shell-${width}-${collapsed?'collapsed':'expanded'}`;
        await screenshot(page,name);
        assert.deepEqual(await page.evaluate(clipped),[], 'Visible controls and first conversation must fit all clipping ancestors');
        if (width===1365 && !collapsed) await oldLayoutNegativeControl(page);
        assert(await page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'),'No document horizontal overflow');
        assert(await page.evaluate(`(() => {const selects=Array.from(document.querySelectorAll('#inbox-filters select'));return selects.length===3&&selects.every(select=>{
            const style=getComputedStyle(select),canvas=document.createElement('canvas'),context=canvas.getContext('2d');
            context.font=style.fontWeight+' '+style.fontSize+' '+style.fontFamily;
            return context.measureText(select.selectedOptions[0].textContent).width + parseFloat(style.paddingLeft) + parseFloat(style.paddingRight) + 24 <= select.clientWidth;
        });})()`),'Default filter labels remain legible with room for their dropdown arrow');
        assert(await page.evaluate(`document.querySelector('#inbox-message-list').getBoundingClientRect().height > 180`),'Account navigation leaves room for conversations');
        const directory=await page.evaluate(`(() => { const nav=document.querySelector('[aria-label="Other DM accounts"]'),details=nav.closest('details');return {count:nav.querySelectorAll('a').length,visible:details.getBoundingClientRect().height,open:details.open};})()`);
        assert.equal(directory.count,10,'All existing account routes remain reachable');
        const destinations=await page.evaluate(`Array.from(document.querySelectorAll('[aria-label="Other DM accounts"] a'),link=>link.href)`);
        const accounts=destinations.map(href=>{
            const url=new URL(href);
            assert.equal(url.origin,fixture.origin,'Account switch stays on the same application');
            assert.equal(url.pathname,fixture.feed,'Account switch stays in the selected workspace');
            assert.deepEqual([...url.searchParams.keys()].sort(),['account','domain'],'Each switch selects one account and DM');
            assert.equal(url.searchParams.get('domain'),'dm');
            return url.searchParams.get('account');
        });
        assert.deepEqual(accounts.sort(),[...fixture.legacyAccountIds].sort(),'Every account has its own exact destination');
        assert.equal(directory.open,false,'Other accounts start collapsed');
        assert(directory.visible<60,'Other accounts are compact before expansion');
        await page.click('[data-canonical-account-switcher] summary');
        assert(await page.evaluate(`(() => {const nav=document.querySelector('[aria-label="Other DM accounts"]');return nav.clientHeight<=160 && nav.scrollHeight>nav.clientHeight;})()`),'Expanded account menu has its own bounded scroll');
        assert(await page.evaluate(`Array.from(document.querySelectorAll('[aria-label="Other DM accounts"] a')).every(link=>/Facebook|Instagram/.test(link.textContent))`),'Repeated brand names identify their platform');
        await page.click('[data-canonical-account-switcher] summary');
        await page.open(fixture.threads[0]);
        assert(await page.evaluate(`(() => {const c=document.querySelector('#inbox-canonical-composer').getBoundingClientRect(), h=document.querySelector('#inbox-canonical-header').getBoundingClientRect();return c.bottom<=innerHeight+1&&c.height>40&&h.top>=0;})()`),'Opened conversation keeps header and composer visible');
        assert.equal(await page.evaluate(`!!document.querySelector('.canonical-list-pane').getClientRects().length`),width>=768);
        if (width<768) {
            await page.click('[data-canonical-back]');
            assert.equal(await page.evaluate(`document.querySelector('[data-canonical-shell]').dataset.activePanel`),'list');
            assert.deepEqual(await page.evaluate(clipped),[]);
        }
        await page.clean();
        process.stdout.write(`PASS actual application shell ${width}x${height}, ${collapsed?'collapsed':'expanded'} sidebar\n`);
    } finally {await page.close();}
}

async function main() {
    const profile=fs.mkdtempSync(path.join(os.tmpdir(),'brightbean-shell-chromium-'));
    const browser=new Browser(value('--browser'),profile);
    await withCleanup(async()=>{try {
        await browser.command('Browser.getVersion',{},undefined,30000);
        for (const [width,height,collapsed] of [[1365,900,false],[1052,1344,true],[1024,768,false],[768,900,false],[390,844,false]]) {
            await scenario(browser,width,height,collapsed);
        }
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
