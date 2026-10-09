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
        if (!element || !element.getClientRects().length) continue;
        const box=element.getBoundingClientRect();
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

async function scenario(browser,width,height,collapsed) {
    const page=await createPage(browser,{...fixture,sidebarCollapsed:collapsed},width,height);
    try {
        await page.settle();
        const name=`shell-${width}-${collapsed?'collapsed':'expanded'}`;
        await screenshot(page,name);
        assert.deepEqual(await page.evaluate(clipped),[], 'Visible controls and first conversation must fit all clipping ancestors');
        assert(await page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'),'No document horizontal overflow');
        assert(await page.evaluate(`document.querySelector('#inbox-message-list').getBoundingClientRect().height > 180`),'Account navigation leaves room for conversations');
        const directory=await page.evaluate(`(() => { const nav=document.querySelector('[aria-label="Other DM accounts"]'),details=nav.closest('details');return {count:nav.querySelectorAll('a').length,visible:details.getBoundingClientRect().height,open:details.open};})()`);
        assert.equal(directory.count,10,'All existing account routes remain reachable');
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
