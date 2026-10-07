const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/inbox-native-thread.js'), 'utf8');
const camel = name => name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
const dashed = name => name.replace(/[A-Z]/g, letter => '-' + letter.toLowerCase());
function events(target) {
    const listeners = new Map();
    target.addEventListener = (name, callback) => listeners.set(name, [...(listeners.get(name) || []), callback]);
    target.removeEventListener = (name, callback) => listeners.set(name, (listeners.get(name) || []).filter(fn => fn !== callback));
    target.emit = (name, event = {}) => { for (const listener of listeners.get(name) || []) listener(event); };
    return target;
}
class Element {
    constructor(tag, dataset = {}) {
        this.tagName = tag; this.dataset = dataset; this.children = []; this.parent = null;
        this.hidden = false; this.disabled = false; this._text = ''; this.style = {}; this.scrollTop = 0; this.clientHeight = 160;
        events(this);
    }
    appendChild(element) { element.remove(); element.parent = this; this.children.push(element); return element; }
    insertBefore(element, reference) {
        if (!reference) return this.appendChild(element);
        if (element === reference) return element;
        assert.ok(this.children.includes(reference), 'Reference must belong to this parent');
        element.remove(); element.parent = this; this.children.splice(this.children.indexOf(reference), 0, element); return element;
    }
    remove() { if (this.parent) { this.parent.children = this.parent.children.filter(child => child !== this); this.parent = null; } }
    replaceChildren(...elements) { this.children.forEach(child => { child.parent = null; }); this.children = []; this._text = ''; elements.forEach(child => this.appendChild(child)); }
    set textContent(value) { this.replaceChildren(); this._text = value; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(' '); }
    set innerHTML(_) { throw new Error('Untrusted native content must never be interpreted as HTML'); }
    get innerHTML() { return this._text + this.children.map(child => child.outerHTML).join(''); }
    get outerHTML() {
        const attrs = Object.entries(this.dataset).map(([key, value]) => ' data-' + dashed(key) + '="' + value + '"').join('');
        return '<' + this.tagName + attrs + (this.hidden ? ' hidden' : '') + '>' + this.innerHTML + '</' + this.tagName + '>';
    }
    getAttribute(name) { return this[name === 'datetime' ? 'dateTime' : name] || null; }
    get firstChild() { return this.children[0] || null; }
    get parentNode() { return this.parent; }
    replaceChild(replacement, previous) { this.insertBefore(replacement, previous); previous.remove(); }
    get scrollHeight() {
        const height = element => element.hidden ? 0 : (element.dataset.timelineEvent ? 100 : 0) + element.children.reduce((n, child) => n + height(child), 0);
        return height(this);
    }
    cloneNode(deep) {
        const copy = new Element(this.tagName, { ...this.dataset });
        for (const key of ['_text', 'value', 'defaultValue', 'hidden', 'disabled', 'id', 'className', 'href', 'scrollTop', 'clientHeight', 'getBoundingClientRect']) copy[key] = this[key];
        if (deep) this.children.forEach(child => copy.appendChild(child.cloneNode(true)));
        return copy;
    }
    matches(selector) {
        if (selector === '[name=csrfmiddlewaretoken]') return this.name === 'csrfmiddlewaretoken';
        const data = selector.match(/^\[data-([a-z-]+)(?:="([^"]*)")?\]$/);
        if (data) return Object.hasOwn(this.dataset, camel(data[1])) && (data[2] === undefined || this.dataset[camel(data[1])] === data[2]);
        if (selector[0] === '#') return this.id === selector.slice(1);
        return this.tagName === selector;
    }
    closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
    contains(element) { return this === element || this.children.some(child => child.contains(element)); }
    get isConnected() { return this.tagName === 'document' || Boolean(this.parent?.isConnected); }
}
function stored(id, direction, time, body, kind = direction === 'inbound' ? 'incoming' : 'reply') {
    const wrapper = new Element('div', { timelineEvent: 'stored', eventId: kind + ':' + id, platformMessageId: id,
        eventDirection: direction, eventTime: time, eventKind: kind, storedOrder: '0' });
    const label = wrapper.appendChild(new Element('p', { timelineDayLabel: '' })); label.textContent = 'Original saved day';
    const bubble = wrapper.appendChild(new Element('article', { storedEventBubble: '' }));
    bubble.className = direction === 'outbound' ? 'ml-auto' : 'mr-auto';
    const text = bubble.appendChild(new Element('p', { storedEventBody: '' })); text.textContent = body;
    return wrapper;
}
function native(id, body, time = '2026-10-06T09:15:00Z', direction = 'outbound') {
    return { platform_message_id: id, direction, source: 'platform_observed', body, occurred_at: time, attachments: [] };
}
const snapshot = (items = [native('external', 'Native answer')]) => ({
    status: 'observed', reason_code: 'bounded_snapshot', anchor_message_id: 'anchor-a', checked_at: '2026-10-06T10:00:00Z',
    history_complete: false, persisted: false, items, coverage: {}, newer_outbound_observed: true
});
function setup({ auto = true, rows } = {}) {
    const document = new Element('document'); document.readyState = 'loading';
    document.createElement = tag => new Element(tag); document.importNode = (element, deep) => element.cloneNode(deep);
    const window = events({ location: { href: 'https://example.com/inbox/', origin: 'https://example.com' } });
    const requests = []; const fragments = new Map();
    const csrf = document.appendChild(new Element('input')); csrf.name = 'csrfmiddlewaretoken'; csrf.value = 'test-csrf-token';
    const container = document.appendChild(new Element('main'));
    const panel = container.appendChild(new Element('div', { inboxPanel: '', selectedMessageId: 'anchor-a' }));
    const scroller = panel.appendChild(new Element('div', { inboxScroll: '' }));
    const root = scroller.appendChild(new Element('div', { nativeThread: '', anchorId: 'anchor-a', refreshUrl: '/inbox/anchor-a/native-thread/' }));
    const result = root.appendChild(new Element('div', { nativeThreadResult: '' })); result.hidden = true;
    const status = result.appendChild(new Element('p', { nativeThreadStatus: '' }));
    const warning = result.appendChild(new Element('p', { nativeThreadWarning: '' }));
    const button = result.appendChild(new Element('button', { nativeThreadRefresh: '' })); button.hidden = true;
    const dismiss = result.appendChild(new Element('button', { nativeThreadDismiss: '' }));
    const nativeStatus = root.appendChild(new Element('p', { nativeHistoryStatus: '' }));
    const nativeRetry = root.appendChild(new Element('button', { nativeHistoryRetry: '' })); nativeRetry.hidden = true;
    const pageStatus = root.appendChild(new Element('p', { storedHistoryStatus: '' }));
    const pageRetry = root.appendChild(new Element('button', { storedHistoryRetry: '' })); pageRetry.hidden = true;
    const history = scroller.appendChild(new Element('div')); history.id = 'inbox-thread';
    const outside = history.appendChild(new Element('aside')); outside.textContent = 'Selected message outside this page';
    const timeline = history.appendChild(new Element('div', { storedTimelineEvents: '', timelineAnchorId: 'anchor-a',
        timelinePageKey: 'first', olderUrl: '/inbox/anchor-a/?history_before=cursor-1', historyComplete: 'false' }));
    (rows || [stored('in-1', 'inbound', '2026-10-06T08:00:00Z', 'Stored incoming'),
        stored('out-1', 'outbound', '2026-10-06T09:00:00Z', 'Stored sent reply'),
        stored('note-1', 'internal', '2026-10-06T09:30:00Z', 'Stored internal note', 'note')]).forEach((row, index) => {
        row.dataset.storedOrder = String(index); timeline.appendChild(row);
    });
    const footer = history.appendChild(new Element('p')); footer.textContent = 'Saved page footer';
    const composer = panel.appendChild(new Element('textarea')); composer.value = 'My interrupted unsaved reply'; composer.defaultValue = 'Saved draft';
    const context = {
        document, window, AbortController, URL, URLSearchParams, Date, Intl,
        DOMParser: class { parseFromString(html, type) { assert.equal(type, 'text/html'); assert.ok(fragments.has(html)); return fragments.get(html).cloneNode(true); } },
        fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); }
    };
    vm.runInNewContext(source, context);
    const app = { document, window, container, panel, scroller, root, result, status, warning, button, dismiss, nativeStatus, nativeRetry, pageStatus, pageRetry,
        history, timeline, outside, footer, composer, csrf, requests, context,
        click(target) { document.emit('click', { target, preventDefault() {} }); },
        scroll(top) { scroller.scrollTop = top; scroller.emit('scroll'); },
        open() { document.emit('DOMContentLoaded'); },
        page(rows, { anchor = 'anchor-a', key = 'second', older = '', complete = 'true' } = {}) {
            const parsed = new Element('document');
            const page = parsed.appendChild(new Element('div', { storedTimelineEvents: '', timelineAnchorId: anchor, timelinePageKey: key, olderUrl: older, historyComplete: complete }));
            rows.forEach(row => page.appendChild(row));
            const extra = parsed.appendChild(new Element('aside')); extra.textContent = 'Do not import navigation or selected-message aside';
            const token = 'server-fragment-' + fragments.size; fragments.set(token, parsed); return token;
        }
    };
    if (auto) app.open();
    return app;
}
const flush = () => new Promise(resolve => setImmediate(resolve));
async function resolve(app, index, data = snapshot(), ok = true, status = 200) {
    app.requests[index].resolve({ ok, status, json: async () => data, text: async () => data }); await flush();
}
const nativeCards = app => app.timeline.querySelectorAll('[data-native-thread-item]');
const eventOrder = app => app.timeline.children.filter(row => row.dataset.timelineEvent).map(row => row.dataset.eventId || row.textContent);
function bundledHistory(app) {
    const bundled = fs.readFileSync(path.join(__dirname, '../static/js/htmx.min.js'), 'utf8');
    assert.ok(bundled.includes('version:"2.0.4"'), 'Review history regression on HTMX upgrades');
    const begin = bundled.indexOf('function Ut('); const end = bundled.indexOf('function $t(', begin);
    assert.ok(begin > 0 && end > begin);
    const storage = new Map(); const replaced = [];
    const context = {
        ne: () => app.document, B: () => true, U: value => value, S: JSON.parse, se: (values, callback) => values.forEach(callback),
        x: (element, selector) => element.querySelectorAll(selector), G: () => {},
        he: (_, name, detail) => app.document.emit(name, { detail }), fe: (_, name) => { throw new Error(name); },
        Q: { config: { historyEnabled: true, historyCacheSize: 10, requestClass: 'htmx-request' } }, Bt: null,
        location: { pathname: '/inbox/', search: '?status=open' }, window: { scrollY: 0, location: { href: 'https://example.com/inbox/?status=open' } },
        history: { replaceState: (...args) => replaced.push(args) },
        localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }
    };
    app.document.body = app.container; app.document.title = 'Inbox';
    vm.runInNewContext(bundled.slice(begin, end), context);
    return { storage, replaced, save() { context.zt(); } };
}

function savedAction(app, kind = 'status') {
    const element = app.panel.appendChild(new Element(kind === 'status' ? 'button' : 'form'));
    const path = '/inbox/anchor-a/' + (kind === 'draft' ? 'reply/draft/' : kind + '/');
    element['hx-post'] = path;
    return { elt: element, target: app.container, requestConfig: { verb: 'post', path } };
}
function bundledRequest(app, element) {
    // Execute the actual pinned HTMX issuer through beforeRequest, rather
    // than inventing its requestConfig shape. DOM/forms/XHR are offline adapters.
    const bundled = fs.readFileSync(path.join(__dirname, '../static/js/htmx.min.js'), 'utf8');
    assert.ok(bundled.includes('version:"2.0.4"'));
    const begin = bundled.indexOf('function de('); const end = bundled.indexOf('function Nn(', begin);
    assert.ok(begin > 0 && end > begin);
    const data = new WeakMap(); let sent;
    const map = value => value instanceof Map ? value : new Map(Object.entries(value || {}));
    const context = {
        ne: () => app.document, Dn: () => {}, le: node => node.isConnected, Ee: () => app.container,
        ue: value => value, ve: {}, ie: node => { if (!data.has(node)) data.set(node, {}); return data.get(node); },
        ee: (node, name) => node.getAttribute(name), re: () => null, oe: callback => { if (callback) callback(); },
        fn: () => ({}), pn: () => false, cn: () => ({ errors: [], formData: new Map() }),
        qn: map, En: () => ({}), ln: (values, extra) => { extra.forEach((value, key) => values.set(key, value)); return values; },
        hn: value => value, An: values => Object.fromEntries(values), bn: () => ({}), ce: Object.assign, Tn: () => true,
        Cn: (xhr, name, value) => xhr.setRequestHeader(name, value),
        Q: { config: { methodsThatUseUrlParams: ['get'], withCredentials: false, timeout: 0 } },
        XMLHttpRequest: class {
            open(method, path) { this.method = method; this.path = path; }
            overrideMimeType() {}
            setRequestHeader() {}
        },
        he(node, name, detail) {
            // Real he() binds detail.elt before dispatching the browser event.
            detail.elt = node;
            app.document.emit(name, { detail });
            if (name === 'htmx:beforeRequest') { sent = detail; return false; }
            return true;
        },
        fe: (_, name) => { throw new Error(name); }
    };
    vm.runInNewContext(bundled.slice(begin, end), context);
    context.de('post', element.getAttribute('hx-post'), element, null, { targetOverride: app.container }, true);
    assert.equal(sent.xhr.method, 'POST');
    assert.equal(sent.requestConfig.path, element.getAttribute('hx-post'));
    assert.equal(sent.requestConfig.verb, 'post');
    return sent;
}
function measuredTimeline(panel) {
    const scroller = panel.querySelector('[data-inbox-scroll]');
    const timeline = panel.querySelector('[data-stored-timeline-events]');
    scroller.getBoundingClientRect = () => ({ top: 0, bottom: 160 });
    for (const row of timeline.querySelectorAll('[data-timeline-event]')) {
        row.getBoundingClientRect = function () {
            const currentTimeline = this.closest('[data-stored-timeline-events]');
            const currentScroller = this.closest('[data-inbox-scroll]');
            const index = currentTimeline.querySelectorAll('[data-timeline-event]').indexOf(this);
            const top = Number(currentScroller.dataset.headerHeight || 0) + index * 100 - currentScroller.scrollTop;
            return { top, bottom: top + 100 };
        };
    }
    return { scroller, timeline };
}

test('opening reads once, scrolls latest, and repeated lifecycle events never duplicate the read', async () => {
    const app = setup();
    assert.equal(app.requests.length, 1);
    assert.equal(app.scroller.scrollTop, app.scroller.scrollHeight);
    for (const event of ['DOMContentLoaded', 'htmx:afterSwap', 'htmx:afterSettle']) app.document.emit(event);
    vm.runInNewContext(source, app.context);
    app.click(app.button);
    assert.equal(app.requests.length, 1);
    const request = app.requests[0];
    assert.equal(request.url, '/inbox/anchor-a/native-thread/');
    assert.equal(request.options.method, 'POST');
    assert.equal(request.options.headers['X-CSRFToken'], 'test-csrf-token');
    assert.equal(request.options.cache, 'no-store'); assert.equal(request.options.credentials, 'same-origin');
    assert.equal(request.options.body, undefined);
    await resolve(app, 0);
    assert.equal(app.composer.value, 'My interrupted unsaved reply'); assert.equal(app.composer.defaultValue, 'Saved draft');
    assert.equal(app.panel.children.at(-1), app.composer);
    assert.equal(app.scroller.scrollTop, app.scroller.scrollHeight);
    assert.equal(app.root.querySelector('[data-native-thread-items]'), null, 'No parallel message list');
    assert.equal(nativeCards(app).length, 1); assert.equal(app.button.hidden, true);
    assert.match(app.status.textContent, /not saved.*incomplete or already out of date/);
    assert.match(app.status.textContent, /does not establish who sent/);
    assert.match(app.warning.textContent, /newer account-side/);
});

test('one chronological timeline keeps inbound left and account-side right, including newest native beyond old stored page', async () => {
    const app = setup();
    await resolve(app, 0, snapshot([
        native('last', 'Newest platform answer', '2026-10-07T10:00:00Z'),
        native('middle', 'Middle platform question', '2026-10-06T08:30:00Z', 'inbound'),
        native('first', 'Earlier platform answer', '2026-10-05T08:00:00Z')
    ]));
    const order = eventOrder(app);
    assert.match(order[0], /Earlier platform answer/); assert.equal(order[1], 'incoming:in-1');
    assert.match(order[2], /Middle platform question/); assert.equal(order[3], 'reply:out-1');
    assert.equal(order[4], 'note:note-1'); assert.match(order[5], /Newest platform answer/);
    assert.match(nativeCards(app)[1].parent.className, /justify-start/);
    assert.match(nativeCards(app)[2].parent.className, /justify-end/);
    assert.equal(app.outside.parent, app.history); assert.equal(app.footer.parent, app.history);
    assert.match(app.status.textContent, /gaps may remain/);
});

test('unique exact provider ID and direction produce one bubble; richer media supplement does not repeat body', async () => {
    const app = setup();
    const item = native('out-1', 'Stored sent reply', '2026-10-06T09:00:00Z');
    item.attachments = [{ type: 'image', availability: 'available', url: 'https://example.com/photo.jpg', title: 'Observed photo' }];
    await resolve(app, 0, snapshot([item]));
    assert.equal(nativeCards(app).length, 0);
    assert.equal(app.timeline.textContent.split('Stored sent reply').length - 1, 1);
    assert.match(app.timeline.textContent, /Also observed on platform · same message ID/);
    assert.match(app.timeline.textContent, /Additional media observed on platform/);
    assert.equal(app.timeline.querySelector('a').href, 'https://example.com/photo.jpg');
    assert.equal(eventOrder(app).length, 3);
});

test('partial matched response retains full stored text and existing exact attachment URL without duplicate media', async () => {
    const app = setup();
    const saved = app.timeline.children[1].querySelector('[data-stored-event-bubble]');
    const link = saved.appendChild(new Element('a')); link.href = 'https://example.com/photo.jpg'; link.textContent = 'Saved photo';
    const item = native('out-1', '', '2026-10-06T09:00:00Z'); item.content_status = 'fields_unavailable';
    item.attachments = [{ type: 'image', availability: 'available', url: link.href }];
    await resolve(app, 0, snapshot([item]));
    assert.equal(nativeCards(app).length, 0); assert.equal(app.timeline.querySelectorAll('a').length, 1);
    assert.match(app.timeline.textContent, /Stored sent reply/); assert.match(app.timeline.textContent, /observation is partial; saved content is retained/);
});

test('conflicting content retains both source evidence and never overwrites saved text', async () => {
    const app = setup();
    await resolve(app, 0, snapshot([native('out-1', 'Different platform text', '2026-10-06T09:00:00Z')]));
    assert.equal(nativeCards(app).length, 1);
    assert.match(app.timeline.textContent, /Stored sent reply/); assert.match(app.timeline.textContent, /Different platform text/);
    assert.match(app.timeline.textContent, /platform content differs.*Both sources are retained/);
});

test('same body and time never deduplicate different IDs or missing IDs', async () => {
    const app = setup();
    await resolve(app, 0, snapshot([native('different-id', 'Stored sent reply', '2026-10-06T09:00:00Z'), native('', 'Stored sent reply', '2026-10-06T09:00:00Z')]));
    assert.equal(nativeCards(app).length, 2);
    assert.equal(app.timeline.textContent.split('Stored sent reply').length - 1, 3);
    assert.match(app.timeline.textContent, /no reliable platform message ID/);
});

test('duplicate response IDs, opposite directions and duplicate saved IDs stay explicitly unmerged', async () => {
    for (const items of [
        [native('out-1', 'Stored sent reply'), native('out-1', 'Stored sent reply')],
        [native('out-1', 'Stored sent reply', '2026-10-06T09:00:00Z', 'inbound')],
        [native('out-1', 'Stored sent reply'), native('out-1', 'Conflicting side', '2026-10-06T09:00:00Z', 'inbound')]
    ]) {
        const app = setup(); await resolve(app, 0, snapshot(items));
        assert.equal(nativeCards(app).length, items.length); assert.match(app.timeline.textContent, /Not merged:/);
        assert.doesNotMatch(app.timeline.textContent, /Also observed/);
    }
    const app = setup(); app.timeline.appendChild(stored('out-1', 'outbound', '2026-10-06T10:00:00Z', 'Duplicate saved ID'));
    await resolve(app, 0, snapshot([native('out-1', 'Stored sent reply')]));
    assert.equal(nativeCards(app).length, 1); assert.match(app.timeline.textContent, /ID is ambiguous/);
    assert.match(app.timeline.textContent, /Duplicate saved ID/);
});

test('uncertain native or saved timestamps remain unpositioned and preserve original evidence', async () => {
    for (const time of ['', '2026-10-06', '2026-10-06T09:00:00', '2026-02-30T09:00:00Z', 'not a date']) {
        const app = setup(); await resolve(app, 0, snapshot([native('out-1', 'Stored sent reply', time)]));
        assert.equal(nativeCards(app).length, 1);
        assert.ok(nativeCards(app)[0].closest('[data-native-unpositioned]'));
        assert.match(app.timeline.textContent, /position unverified/);
    }
    const app = setup(); app.timeline.children[1].dataset.eventTime = '';
    await resolve(app, 0, snapshot([native('out-1', 'Stored sent reply')]));
    assert.ok(nativeCards(app)[0].closest('[data-native-unpositioned]'));
    assert.match(app.timeline.textContent, /Stored sent reply/);
});

test('dismiss restores exact saved nodes, their day-label visibility and order while retaining scrolling', async () => {
    const app = setup(); const original = app.timeline.children.slice();
    original[0].querySelector('[data-timeline-day-label]').hidden = true;
    const before = app.timeline.outerHTML;
    await resolve(app, 0, snapshot([native('out-1', 'Stored sent reply'), native('new', 'Temporary body')]));
    app.click(app.dismiss);
    assert.equal(app.timeline.outerHTML, before); assert.deepEqual(app.timeline.children, original);
    assert.equal(app.result.hidden, true); assert.equal(app.composer.value, 'My interrupted unsaved reply');
    app.scroll(20); assert.equal(app.requests.length, 2, 'Saved-history scroll remains usable after clearing native data');
});

test('scrolling up while native read is pending never jumps back to bottom', async () => {
    const app = setup(); app.scroll(180);
    await resolve(app, 0);
    assert.equal(app.scroller.scrollTop, 180); assert.equal(app.requests.length, 1);
});

test('upward threshold loads one scoped saved page, prepends only events, deduplicates local IDs and preserves position', async () => {
    const app = setup(); await resolve(app, 0);
    app.scroll(80); app.scroll(70);
    assert.equal(app.requests.length, 2);
    assert.equal(app.requests[1].options.method, 'GET'); assert.equal(app.requests[1].options.headers['HX-Target'], 'inbox-thread');
    const before = app.scroller.scrollHeight;
    const page = app.page([stored('older', 'inbound', '2026-10-05T08:00:00Z', 'Older saved message'), stored('in-1', 'inbound', '2026-10-06T08:00:00Z', 'Duplicate local row')]);
    await resolve(app, 1, page);
    assert.equal(eventOrder(app)[0], 'incoming:older');
    assert.equal(app.scroller.scrollTop, 70 + app.scroller.scrollHeight - before);
    assert.equal(app.timeline.querySelectorAll('[data-timeline-event="stored"]').length, 4);
    assert.doesNotMatch(app.timeline.textContent, /Duplicate local row|Do not import/);
    assert.equal(nativeCards(app).length, 1); assert.equal(app.timeline.dataset.olderUrl, '');
    assert.deepEqual(app.timeline.querySelectorAll('[data-timeline-event="stored"]').map(row => row.dataset.storedOrder), ['0', '1', '2', '3']);
    assert.match(app.pageStatus.textContent, /Beginning of saved history.*Platform history may still be incomplete/);
    app.scroll(0); assert.equal(app.requests.length, 2);
});

test('a newly loaded saved exact match merges an existing native card without duplicating the bubble', async () => {
    const app = setup(); await resolve(app, 0, snapshot([native('older', 'Old platform answer', '2026-10-05T08:00:00Z')]));
    assert.equal(nativeCards(app).length, 1); app.scroll(50);
    await resolve(app, 1, app.page([stored('older', 'outbound', '2026-10-05T08:00:00Z', 'Old platform answer')]));
    assert.equal(nativeCards(app).length, 0); assert.equal(app.timeline.textContent.split('Old platform answer').length - 1, 1);
    assert.match(app.timeline.textContent, /Also observed on platform/);
});

test('paging errors or wrong-anchor fragments retain current view and permit a bounded retry', async () => {
    const app = setup(); await resolve(app, 0); const before = app.timeline.outerHTML;
    app.scroll(50); await resolve(app, 1, app.page([stored('bad', 'inbound', '2026-10-05T08:00:00Z', 'Wrong thread')], { anchor: 'anchor-b' }));
    assert.equal(app.timeline.outerHTML, before); assert.equal(app.pageRetry.hidden, false);
    app.click(app.pageRetry); assert.equal(app.requests.length, 3);
    await resolve(app, 2, app.page([stored('older', 'inbound', '2026-10-05T08:00:00Z', 'Recovered older message')]));
    assert.match(app.timeline.textContent, /Recovered older message/); assert.equal(app.pageRetry.hidden, true);
});

test('unsafe cross-origin history URLs never issue reads', async () => {
    const app = setup(); await resolve(app, 0); app.timeline.dataset.olderUrl = 'https://attacker.example/history';
    app.scroll(50); assert.equal(app.requests.length, 1);
});

test('navigation aborts both native and earlier-page reads; late responses cannot touch drafts or new selection', async () => {
    const app = setup(); app.scroll(30); assert.equal(app.requests.length, 2);
    app.document.emit('htmx:beforeRequest', { detail: { elt: { dataset: { inboxOpenMessage: 'anchor-b' } } } });
    assert.equal(app.requests[0].options.signal.aborted, true); assert.equal(app.requests[1].options.signal.aborted, true);
    await resolve(app, 0, snapshot([native('late', 'Late native')]));
    await resolve(app, 1, app.page([stored('late', 'inbound', '2026-10-05T08:00:00Z', 'Late saved page')]));
    assert.doesNotMatch(app.timeline.textContent, /Late native|Late saved page/);
    assert.equal(app.composer.value, 'My interrupted unsaved reply');
});

test('disconnected, changed anchor or swapped timeline cannot accept late native response', async () => {
    for (const change of [app => app.container.replaceChildren(), app => { app.panel.dataset.selectedMessageId = 'anchor-b'; },
        app => app.document.emit('htmx:beforeSwap', { detail: { target: app.history } })]) {
        const app = setup(); change(app); await resolve(app, 0, snapshot([native('wrong', 'Wrong anchor contents')]));
        assert.equal(nativeCards(app).length, 0);
    }
});

test('dismiss during JSON parsing prevents late content returning, and retry is fenced from the older read', async () => {
    const app = setup(); let release;
    app.requests[0].resolve({ ok: true, json: () => new Promise(resolve => { release = resolve; }) }); await flush();
    app.click(app.dismiss); app.click(app.button);
    await resolve(app, 1, snapshot([native('new', 'Newest reply')]));
    release(snapshot([native('old', 'Discarded older reply')])); await flush();
    assert.match(app.timeline.textContent, /Newest reply/); assert.doesNotMatch(app.timeline.textContent, /Discarded older reply/);
});

test('Back, Forward, browser restore and mobile close discard temporary observations', async () => {
    for (const event of ['popstate', 'pagehide', 'pageshow', 'mobile-back']) {
        const app = setup(); await resolve(app, 0);
        if (event === 'mobile-back') app.click(app.panel.appendChild(new Element('a', { inboxBack: '' })));
        else app.window.emit(event, { persisted: true });
        assert.equal(nativeCards(app).length, 0, event); assert.equal(app.composer.value, 'My interrupted unsaved reply');
    }
});

test('text and media titles remain literal and unsafe links retain a truthful fallback', async () => {
    const app = setup(); const item = native('unsafe', '<img src=x onerror=alert(1)>');
    item.attachments = [{ type: 'image', title: '<script>bad()</script>', availability: 'available', url: 'javascript:alert(1)' },
        { type: 'file', title: 'Real file', availability: 'available', url: 'https://example.com/file.pdf' }];
    item.body_truncated = true;
    await resolve(app, 0, { ...snapshot([item]), more_available: true });
    assert.match(app.timeline.textContent, /<img src=x onerror=alert\(1\)>/); assert.match(app.timeline.textContent, /<script>bad\(\)<\/script>/);
    assert.equal(app.timeline.querySelector('img'), null); assert.equal(app.timeline.querySelector('script'), null);
    assert.match(app.timeline.textContent, /Media unavailable/); assert.match(app.timeline.textContent, /shortened in this view/);
    assert.match(app.status.textContent, /no earlier platform page is available/);
    assert.equal(app.timeline.querySelector('a').rel, 'noopener noreferrer');
});

test('network errors, invalid source and wrong anchor show no content and preserve drafts with retry', async () => {
    const app = setup(); app.requests[0].reject(new Error('PRIVATE RAW ERROR')); await flush();
    assert.match(app.status.textContent, /draft and saved history are still here/); assert.doesNotMatch(app.status.textContent, /PRIVATE RAW/);
    assert.equal(app.button.hidden, false); app.click(app.button);
    await resolve(app, 1, { ...snapshot(), anchor_message_id: 'wrong' }); assert.equal(nativeCards(app).length, 0);
    app.click(app.button); const item = native('bad', 'Unverified contents'); delete item.source;
    await resolve(app, 2, snapshot([item])); assert.equal(nativeCards(app).length, 0);
    assert.equal(app.composer.value, 'My interrupted unsaved reply');
});

test('auth revocation and unavailable results show clear reasons without platform contents', async () => {
    for (const [reason, expected] of [['authorization_revoked', /access changed/], ['missing_native_thread', /no reliable platform/],
        ['unverified_thread', /one-to-one/], ['unsupported_platform', /does not support/]]) {
        const app = setup(); await resolve(app, 0, { ...snapshot([native('bad', 'DO NOT RENDER')]), status: 'unavailable', reason_code: reason }, false);
        assert.match(app.status.textContent, expected); assert.equal(nativeCards(app).length, 0);
    }
});

test('empty result never claims no reply exists, and empty body/media stays unverified', async () => {
    const app = setup(); await resolve(app, 0, { ...snapshot([]), newer_outbound_observed: false });
    assert.match(app.status.textContent, /does not mean no reply exists/); assert.equal(app.warning.hidden, true);
    app.click(app.button); await resolve(app, 1, snapshot([native('empty', '')]));
    assert.match(app.timeline.textContent, /original content has not been verified/);
});

test('missing CSRF never sends; retry can recover after token is available', () => {
    const app = setup({ auto: false }); app.csrf.value = ''; app.open(); assert.equal(app.requests.length, 0);
    app.csrf.value = 'token'; app.click(app.button); assert.equal(app.requests.length, 1);
});

test('real bundled HTMX saves ordinary history with no standalone or matched native content', async () => {
    const app = setup(); const history = bundledHistory(app);
    const matched = native('out-1', 'Stored sent reply'); matched.attachments = [{ title: 'PRIVATE MEDIA TITLE', type: 'file', availability: 'unavailable' }];
    await resolve(app, 0, snapshot([matched, native('private', 'PRIVATE TRANSIENT PLATFORM BODY')]));
    assert.match(app.timeline.textContent, /PRIVATE TRANSIENT/); assert.match(app.timeline.textContent, /PRIVATE MEDIA TITLE/);
    const list = app.container.appendChild(new Element('div'));
    app.document.emit('htmx:beforeRequest', { detail: { elt: { dataset: {} }, target: list } });
    app.document.emit('htmx:beforeSwap', { detail: { target: list } });
    assert.match(app.timeline.textContent, /PRIVATE TRANSIENT/);
    history.save(); const cached = history.storage.get('htmx-history-cache');
    assert.ok(cached); assert.match(cached, /Stored incoming.*Stored sent reply/);
    assert.doesNotMatch(cached, /PRIVATE TRANSIENT|PRIVATE MEDIA|Also observed|Read at|newer account-side|native-thread-transient/);
    assert.equal(app.result.hidden, true); assert.equal(app.composer.value, 'My interrupted unsaved reply');
    assert.equal(app.composer.defaultValue, 'Saved draft');
    app.scroll(20); assert.equal(app.requests.length, 2, 'Saved scrolling survives a list-only history save');
});

test('HTMX history save aborts pending native read and late result cannot be cached', async () => {
    const app = setup(); const history = bundledHistory(app); history.save();
    assert.equal(app.requests[0].options.signal.aborted, true);
    await resolve(app, 0, snapshot([native('late', 'LATE PRIVATE BODY')])); history.save();
    assert.equal(nativeCards(app).length, 0); assert.doesNotMatch(history.storage.get('htmx-history-cache'), /LATE PRIVATE|Reading latest platform/);
});

test('history clone cleanup restores exact saved content without depending on the active original DOM', async () => {
    const app = setup(); const before = app.timeline.outerHTML;
    await resolve(app, 0, snapshot([native('out-1', 'Stored sent reply'), native('private', 'PREVIOUSLY CACHED PRIVATE BODY')]));
    const restored = app.panel.cloneNode(true); app.container.replaceChildren(restored);
    assert.match(restored.textContent, /PREVIOUSLY CACHED PRIVATE/);
    app.document.emit('htmx:historyRestore', { detail: { item: {} } });
    assert.equal(restored.querySelector('[data-stored-timeline-events]').outerHTML, before);
    assert.doesNotMatch(restored.textContent, /PREVIOUSLY CACHED PRIVATE|Also observed/);
    assert.equal(restored.querySelector('textarea').value, 'My interrupted unsaved reply');
    assert.equal(app.requests.length, 2, 'Restored selected conversation gets one fresh bounded read');
});


test('an upward gesture loads one saved page and one native continuation without prefetch chaining', async () => {
    const app = setup();
    await resolve(app, 0, { ...snapshot(), older_continuation: 'signed-page-2', page_key: 'native-1' });
    app.scroll(40); app.scroll(30);
    assert.equal(app.requests.length, 3);
    const nativeRequest = app.requests[2];
    assert.equal(nativeRequest.options.method, 'POST');
    assert.equal(nativeRequest.options.body, 'continuation=signed-page-2');
    assert.equal(nativeRequest.options.headers['Content-Type'], 'application/x-www-form-urlencoded');
    assert.equal(nativeRequest.options.headers['X-CSRFToken'], 'test-csrf-token');
    await resolve(app, 2, { ...snapshot([native('older-platform', 'Older native answer', '2026-10-05T10:00:00Z')]), older_continuation: 'signed-page-3', page_key: 'native-2' });
    await resolve(app, 1, app.page([stored('older-saved', 'inbound', '2026-10-05T09:00:00Z', 'Older saved incoming')]));
    assert.equal(app.requests.length, 3, 'No automatic chain after either response');
    assert.equal(nativeCards(app).length, 2);
    assert.equal(eventOrder(app)[0], 'incoming:older-saved');
    assert.match(eventOrder(app)[1], /Older native answer/);
    assert.equal(app.composer.value, 'My interrupted unsaved reply');
    app.scroll(250); app.scroll(20); assert.equal(app.requests.length, 4);
});

test('native continuation merges identical page overlap only after exact ID and direction; conflicts survive', async () => {
    const app = setup(); app.timeline.dataset.olderUrl = '';
    const original = native('external', 'Native answer');
    await resolve(app, 0, { ...snapshot([original]), older_continuation: 'cursor-2', page_key: 'page-1' });
    app.scroll(20);
    await resolve(app, 1, { ...snapshot([original, native('older', 'Earlier answer', '2026-10-05T10:00:00Z')]), older_continuation: 'cursor-3', page_key: 'page-2' });
    assert.equal(nativeCards(app).length, 2); assert.equal(app.timeline.textContent.split('Native answer').length - 1, 1);
    app.scroll(250); app.scroll(20);
    await resolve(app, 2, { ...snapshot([native('external', 'Conflicting answer')]), page_key: 'page-3' });
    assert.equal(nativeCards(app).length, 3);
    assert.match(app.timeline.textContent, /Native answer/); assert.match(app.timeline.textContent, /Conflicting answer/);
    assert.match(app.timeline.textContent, /ID is ambiguous/);
});

test('repeated native page keys, repeated cursor and no-progress observations stop further advancement', async () => {
    for (const mode of ['key', 'cursor', 'items']) {
        const app = setup(); app.timeline.dataset.olderUrl = '';
        await resolve(app, 0, { ...snapshot(), older_continuation: 'cursor-2', page_key: 'page-1' });
        app.scroll(20);
        await resolve(app, 1, { ...snapshot(mode === 'items' ? undefined : [native('older', 'Older answer')]),
            page_key: mode === 'key' ? 'page-1' : 'page-2', older_continuation: mode === 'cursor' ? 'cursor-2' : 'cursor-3' });
        assert.match(app.nativeStatus.textContent, /did not advance/);
        app.scroll(250); app.scroll(20); assert.equal(app.requests.length, 2, mode);
    }
});

test('revoked or changed native continuation scope clears all prior transient observations', async () => {
    for (const [reason, http] of [['authorization_revoked', 200], ['stale_continuation', 200], ['thread_scope_mismatch', 200],
        ['participants_unverified', 200], ['account_unavailable', 200], ['platform_permission_unavailable', 200], ['provider_unavailable', 403]]) {
        const app = setup(); app.timeline.dataset.olderUrl = '';
        await resolve(app, 0, { ...snapshot(), older_continuation: 'cursor-2' });
        app.scroll(20);
        await resolve(app, 1, { ...snapshot([]), status: 'unavailable', reason_code: reason }, false, http);
        assert.equal(nativeCards(app).length, 0, reason);
        assert.match(app.status.textContent, /access or identity changed/);
        assert.match(app.timeline.textContent, /Stored sent reply/);
        app.scroll(250); app.scroll(20); assert.equal(app.requests.length, 2);
    }
});

test('expired native continuation preserves earlier observations, stops cursor and offers fresh latest read', async () => {
    const app = setup(); app.timeline.dataset.olderUrl = '';
    await resolve(app, 0, { ...snapshot(), older_continuation: 'expired-cursor' }); app.scroll(20);
    await resolve(app, 1, { ...snapshot([]), status: 'unavailable', reason_code: 'invalid_continuation' }, false);
    assert.equal(nativeCards(app).length, 1); assert.match(app.nativeStatus.textContent, /expired or is invalid/);
    assert.equal(app.button.hidden, false); assert.equal(app.nativeRetry.hidden, true);
    app.scroll(250); app.scroll(20); assert.equal(app.requests.length, 2);
    app.click(app.button); assert.equal(app.requests.length, 3); assert.equal(app.requests[2].options.body, undefined);
    assert.equal(nativeCards(app).length, 0);
});

test('transient native-page error preserves previous observations with stale notice and permits retry', async () => {
    const app = setup(); app.timeline.dataset.olderUrl = '';
    await resolve(app, 0, { ...snapshot(), older_continuation: 'cursor-2' }); app.scroll(20);
    await resolve(app, 1, { ...snapshot([]), status: 'unavailable', reason_code: 'rate_limited' }, false);
    assert.equal(nativeCards(app).length, 1); assert.match(app.nativeStatus.textContent, /stale or incomplete/);
    assert.equal(app.nativeRetry.hidden, false); app.click(app.nativeRetry);
    assert.equal(app.requests[2].options.body, 'continuation=cursor-2');
    await resolve(app, 2, { ...snapshot([native('older', 'Recovered older platform answer')]), page_key: 'page-2' });
    assert.equal(nativeCards(app).length, 2); assert.equal(app.nativeRetry.hidden, true);
});

test('late native continuation cannot repopulate after navigation or HTMX cache save', async () => {
    for (const event of ['navigation', 'history']) {
        const app = setup(); app.timeline.dataset.olderUrl = ''; const history = bundledHistory(app);
        await resolve(app, 0, { ...snapshot(), older_continuation: 'cursor-2' }); app.scroll(20);
        if (event === 'navigation') app.document.emit('htmx:beforeRequest', { detail: { elt: { dataset: { inboxOpenMessage: 'anchor-b' } } } });
        else history.save();
        assert.equal(app.requests[1].options.signal.aborted, true);
        await resolve(app, 1, snapshot([native('late', 'LATE NATIVE PAGE BODY')]));
        assert.equal(nativeCards(app).length, 0);
        history.save(); assert.doesNotMatch(history.storage.get('htmx-history-cache'), /LATE NATIVE PAGE|cursor-2|Native answer/);
    }
});

test('bounded native session stops after 25 pages and leaves explicit incomplete notice', async () => {
    const app = setup(); app.timeline.dataset.olderUrl = '';
    await resolve(app, 0, { ...snapshot(), older_continuation: 'cursor-2', page_key: 'page-1' });
    for (let page = 2; page <= 25; page++) {
        app.scroll(250); app.scroll(20);
        await resolve(app, page - 1, { ...snapshot([native('page-' + page, 'Earlier page ' + page)]),
            older_continuation: 'cursor-' + (page + 1), page_key: 'page-' + page });
    }
    assert.equal(app.requests.length, 25); assert.equal(nativeCards(app).length, 25);
    app.scroll(250); app.scroll(20); assert.equal(app.requests.length, 25);
    assert.match(app.nativeStatus.textContent, /limit reached.*25 pages.*Older platform activity may remain/);
});

test('native rows never exceed 500; an overflowing page is not partially displayed or silently truncated', async () => {
    const app = setup(); app.timeline.dataset.olderUrl = '';
    await resolve(app, 0, { ...snapshot(Array.from({ length: 490 }, (_, i) => native('initial-' + i, 'Observed row ' + i))), older_continuation: 'cursor-2' });
    app.scroll(20);
    await resolve(app, 1, { ...snapshot(Array.from({ length: 20 }, (_, i) => native('older-' + i, 'Undisplayed row ' + i))), older_continuation: 'cursor-3' });
    assert.equal(nativeCards(app).length, 490); assert.doesNotMatch(app.timeline.textContent, /Undisplayed row/);
    assert.match(app.nativeStatus.textContent, /additional page was not displayed/);
    app.scroll(250); app.scroll(20); assert.equal(app.requests.length, 2);
});

test('stored, native and header times use the same browser-zone formatter and retain absolute timestamps', async () => {
    const row = stored('out-1', 'outbound', '2026-10-06T09:00:00Z', 'Stored sent reply');
    const time = row.querySelector('[data-stored-event-bubble]').appendChild(new Element('time'));
    time.dateTime = '2026-10-06T09:00:00Z'; time.textContent = 'Server formatted UTC';
    const app = setup({ rows: [row] });
    await resolve(app, 0, snapshot([native('different', 'Same instant', '2026-10-06T09:00:00Z')]));
    const observedTime = nativeCards(app)[0].querySelector('time');
    assert.equal(time.textContent, observedTime.textContent);
    assert.equal(observedTime.dateTime, time.dateTime);
    assert.match(app.status.textContent, /Times use your browser timezone/);
});

test('a visible stored anchor stays fixed when native arrivals insert above or below it', async () => {
    const app = setup();
    app.scroller.getBoundingClientRect = () => ({ top: 0, bottom: 160 });
    for (const row of app.timeline.children) {
        row.getBoundingClientRect = () => {
            const index = app.timeline.children.filter(element => element.dataset.timelineEvent).indexOf(row);
            return { top: index * 100 - app.scroller.scrollTop, bottom: (index + 1) * 100 - app.scroller.scrollTop };
        };
    }
    app.scroll(150); const anchored = app.timeline.children[1]; const before = anchored.getBoundingClientRect().top;
    await resolve(app, 0, snapshot([native('before', 'Before anchor', '2026-10-06T07:00:00Z'), native('after', 'After anchor', '2026-10-06T11:00:00Z')]));
    assert.equal(anchored.getBoundingClientRect().top, before);
    assert.equal(app.scroller.scrollTop, 250);
});


test('saved-history 401/403 or confirmed login redirect clears native observations even without a JSON body', async () => {
    for (const status of [401, 403, 'login']) {
        const app = setup(); await resolve(app, 0); app.scroll(20);
        app.requests[1].resolve({ ok: status === 'login', status: typeof status === 'number' ? status : 200,
            redirected: status === 'login', url: 'https://example.com/accounts/login/?next=/inbox/', text: async () => '<html>login</html>' });
        await flush();
        assert.equal(nativeCards(app).length, 0); assert.match(app.status.textContent, /access or identity changed/);
        assert.equal(app.composer.value, 'My interrupted unsaved reply');
    }
});

test('native continuation auth failure clears prior rows before trying to parse a non-JSON error', async () => {
    const app = setup(); app.timeline.dataset.olderUrl = '';
    await resolve(app, 0, { ...snapshot(), older_continuation: 'cursor-2' }); app.scroll(20);
    app.requests[1].resolve({ ok: false, status: 403, json: async () => { throw new Error('HTML forbidden'); } });
    await flush(); assert.equal(nativeCards(app).length, 0); assert.match(app.status.textContent, /access or identity changed/);
});

test('imported saved links are processed by HTMX so unsaved-draft navigation confirmation remains active', async () => {
    const app = setup(); const processed = []; app.window.htmx = { process: element => processed.push(element) };
    await resolve(app, 0); app.scroll(20);
    await resolve(app, 1, app.page([stored('older', 'inbound', '2026-10-05T08:00:00Z', 'Earlier incoming')]));
    assert.equal(processed.length, 1); assert.equal(processed[0].dataset.eventId, 'incoming:older');
    assert.equal(processed[0].parent, app.timeline);
});


test('failed, aborted or unchanged-panel HTMX requests recover retry and saved scrolling without automatic native reread', async () => {
    for (const [type, event] of [['selection', 'htmx:responseError'], ['status', 'htmx:sendError'],
        ['draft', 'htmx:timeout'], ['selection', 'htmx:sendAbort'], ['status', 'htmx:afterRequest'], ['draft', 'htmx:swapError']]) {
        const app = setup(); await resolve(app, 0); const element = new Element('button', type === 'selection' ? { inboxOpenMessage: 'anchor-b' } : {});
        app.panel.appendChild(element);
        app.document.emit('htmx:beforeRequest', { detail: { elt: element, target: app.container } });
        assert.equal(nativeCards(app).length, 0);
        app.document.emit(event, { detail: { elt: element, target: app.container, successful: false } });
        app.document.emit('htmx:afterRequest', { detail: { elt: element, target: app.container, successful: false } });
        assert.equal(app.requests.length, 1, type + ' must not trigger an automatic reread');
        assert.equal(app.button.hidden, false); assert.equal(app.button.disabled, false);
        assert.equal(app.composer.value, 'My interrupted unsaved reply');
        app.click(app.button); assert.equal(app.requests.length, 2, type + ' retry is rebound');
        await resolve(app, 1); app.scroll(20); assert.equal(app.requests.length, 3, type + ' saved scrolling is rebound');
    }
});

test('an older failed request cannot rebind or automatically read after a new panel replaces it', async () => {
    const app = setup(); await resolve(app, 0); const element = new Element('a', { inboxOpenMessage: 'anchor-b' });
    app.document.emit('htmx:beforeRequest', { detail: { elt: element } });
    const replacement = app.panel.cloneNode(true);
    app.container.replaceChildren(replacement);
    app.document.emit('htmx:afterSwap', { detail: { target: app.container } });
    assert.equal(app.requests.length, 2);
    app.document.emit('htmx:sendError', { detail: { elt: element } });
    assert.equal(app.requests.length, 2);
});


test('expired saved cursor and repeated/no-progress pages stop misleading retries', async () => {
    for (const mode of ['expired', 'repeat', 'empty']) {
        const app = setup(); await resolve(app, 0); app.scroll(20);
        const page = app.page([], { key: mode === 'repeat' ? 'first' : 'next-key', older: '/inbox/anchor-a/?history_before=next' });
        await resolve(app, 1, page, mode !== 'expired', mode === 'expired' ? 400 : 200);
        assert.equal(app.timeline.dataset.olderUrl, ''); assert.equal(app.pageRetry.hidden, true);
        assert.match(app.pageStatus.textContent, /could not be accepted|did not advance/);
        app.scroll(250); app.scroll(20); assert.equal(app.requests.length, 2);
        assert.equal(app.composer.value, 'My interrupted unsaved reply');
    }
});

test('an upward wheel gesture can load a short non-scrolling history without idle requests', async () => {
    const app = setup({ rows: [stored('in-1', 'inbound', '2026-10-06T08:00:00Z', 'Short saved history')] });
    await resolve(app, 0, { ...snapshot([]), older_continuation: 'cursor-2' });
    assert.equal(app.requests.length, 1);
    app.scroller.emit('wheel', { deltaY: 10 }); assert.equal(app.requests.length, 1);
    app.scroller.emit('wheel', { deltaY: -10 }); app.scroller.emit('wheel', { deltaY: -20 });
    assert.equal(app.requests.length, 3, 'One saved and one native page per upward wheel burst');
});

test('native completion preserves the visible anchor after the inline status grows', async () => {
    const app = setup();
    const statusHeight = () => app.status.textContent.length > 100 ? 120 : 20;
    app.scroller.getBoundingClientRect = () => ({ top: 0, bottom: 160 });
    for (const row of app.timeline.children) {
        row.getBoundingClientRect = () => {
            const index = app.timeline.children.filter(element => element.dataset.timelineEvent).indexOf(row);
            return { top: statusHeight() + index * 100 - app.scroller.scrollTop, bottom: statusHeight() + (index + 1) * 100 - app.scroller.scrollTop };
        };
    }
    app.scroll(150); const anchored = app.timeline.children[1]; const before = anchored.getBoundingClientRect().top;
    await resolve(app, 0, snapshot([native('before', 'Before anchor', '2026-10-06T07:00:00Z')]));
    assert.equal(anchored.getBoundingClientRect().top, before);
});


test('an upward wheel gesture at the existing top can request older native rows without moving scrollTop', async () => {
    const app = setup(); app.timeline.dataset.olderUrl = '';
    await resolve(app, 0, { ...snapshot(), older_continuation: 'cursor-2' });
    app.scroller.scrollTop = 0;
    app.scroller.emit('wheel', { deltaY: -10 }); app.scroller.emit('wheel', { deltaY: -20 });
    assert.equal(app.requests.length, 2); assert.equal(app.requests[1].options.body, 'continuation=cursor-2');
});


test('successful same-anchor draft and status replacements rebind safely without another automatic provider read', async () => {
    for (const kind of ['draft', 'status']) {
        const app = setup(); await resolve(app, 0); app.scroll(180);
        const element = app.panel.appendChild(new Element(kind === 'draft' ? 'form' : 'button'));
        app.document.emit('htmx:beforeRequest', { detail: { elt: element, target: app.container } });
        const replacement = app.panel.cloneNode(true);
        const scroller = replacement.querySelector('[data-inbox-scroll]'); scroller.scrollTop = 0;
        const composer = replacement.querySelector('textarea'); composer.value = kind === 'draft' ? 'Server returned saved draft' : 'My interrupted unsaved reply';
        app.container.replaceChildren(replacement);
        for (const event of ['htmx:afterSwap', 'htmx:afterRequest', 'htmx:afterSettle']) {
            app.document.emit(event, { detail: { elt: element, target: app.container, successful: true } });
        }
        assert.equal(app.requests.length, 1, kind + ' is not a new conversation activation');
        assert.equal(replacement.querySelectorAll('[data-native-thread-item]').length, 0);
        assert.equal(scroller.scrollTop, 180);
        assert.equal(composer.value, kind === 'draft' ? 'Server returned saved draft' : 'My interrupted unsaved reply');
        const retry = replacement.querySelector('[data-native-thread-refresh]');
        assert.equal(retry.hidden, false); assert.equal(retry.disabled, false);
        app.click(retry); assert.equal(app.requests.length, 2, 'Explicit retry still reads');
        await resolve(app, 1);
        scroller.scrollTop = 20; scroller.emit('scroll'); assert.equal(app.requests.length, 3, 'Saved scrolling remains bound');
    }
});

test('explicit same-anchor selection, new-anchor replacement and full-page activation still read automatically', async () => {
    for (const mode of ['same-selection', 'new-anchor']) {
        const app = setup(); await resolve(app, 0);
        const element = app.panel.appendChild(new Element('a', mode === 'same-selection' ? { inboxOpenMessage: 'anchor-a' } : {}));
        app.document.emit('htmx:beforeRequest', { detail: { elt: element, target: app.container } });
        const replacement = app.panel.cloneNode(true);
        if (mode === 'new-anchor') {
            replacement.dataset.selectedMessageId = 'anchor-b';
            replacement.querySelector('[data-native-thread]').dataset.anchorId = 'anchor-b';
            replacement.querySelector('[data-native-thread]').dataset.refreshUrl = '/inbox/anchor-b/native-thread/';
            replacement.querySelector('[data-stored-timeline-events]').dataset.timelineAnchorId = 'anchor-b';
        }
        app.container.replaceChildren(replacement);
        app.document.emit('htmx:afterSwap', { detail: { elt: element, target: app.container } });
        app.document.emit('htmx:afterSettle', { detail: { elt: element, target: app.container } });
        assert.equal(app.requests.length, 2, mode);
        if (mode === 'new-anchor') assert.equal(app.requests[1].url, '/inbox/anchor-b/native-thread/');
    }
    assert.equal(setup().requests.length, 1, 'A fresh page activation reads automatically');
});


test('successful same-scope draft/status rerenders retain one bounded observation and cursors without rereading', async () => {
    for (const kind of ['draft', 'status']) {
        const app = setup(); app.root.dataset.nativeViewScope = 'verified-server-scope'; app.timeline.dataset.olderUrl = '';
        await resolve(app, 0, { ...snapshot(), older_continuation: 'bounded-cursor-2', page_key: 'native-first' });
        app.scroll(180); const readStatus = app.status.textContent;
        const element = app.panel.appendChild(new Element(kind === 'draft' ? 'form' : 'button'));
        app.document.emit('htmx:beforeRequest', { detail: { elt: element, target: app.container } });
        assert.equal(nativeCards(app).length, 0, 'Old DOM is cleaned synchronously');
        const replacement = app.panel.cloneNode(true);
        const composer = replacement.querySelector('textarea'); composer.value = 'Server returned saved draft';
        app.container.replaceChildren(replacement);
        for (const event of ['htmx:afterSwap', 'htmx:afterRequest', 'htmx:afterSettle']) {
            app.document.emit(event, { detail: { elt: element, target: app.container, successful: true, xhr: { status: 200 } } });
        }
        assert.equal(app.requests.length, 1, kind);
        assert.equal(replacement.querySelectorAll('[data-native-thread-item]').length, 1);
        assert.equal(replacement.textContent.split('Native answer').length - 1, 1);
        assert.equal(replacement.querySelector('[data-native-thread-status]').textContent, readStatus);
        assert.equal(replacement.querySelector('[data-native-thread-refresh]').hidden, true);
        assert.equal(composer.value, 'Server returned saved draft');
        const scroller = replacement.querySelector('[data-inbox-scroll]'); assert.equal(scroller.scrollTop, 180);
        scroller.scrollTop = 20; scroller.emit('scroll');
        assert.equal(app.requests.length, 2); assert.equal(app.requests[1].options.body, 'continuation=bounded-cursor-2');
        await resolve(app, 1, snapshot([native('older', 'Earlier platform answer')]));
        assert.equal(replacement.querySelectorAll('[data-native-thread-item]').length, 2);
    }
});

test('changed or missing identity scope and failed replacements never restore old observations or implicitly reread', async () => {
    for (const [scope, status] of [['changed-actor-scope', 200], ['changed-account-scope', 200], ['changed-thread-scope', 200],
        ['', 200], ['original-scope', 403]]) {
        const app = setup(); app.root.dataset.nativeViewScope = 'original-scope'; await resolve(app, 0);
        const element = app.panel.appendChild(new Element('form'));
        app.document.emit('htmx:beforeRequest', { detail: { elt: element, target: app.container } });
        const replacement = app.panel.cloneNode(true); replacement.querySelector('[data-native-thread]').dataset.nativeViewScope = scope;
        app.container.replaceChildren(replacement);
        app.document.emit('htmx:afterSwap', { detail: { elt: element, target: app.container, xhr: { status } } });
        app.document.emit('htmx:afterSettle', { detail: { elt: element, target: app.container, xhr: { status } } });
        assert.equal(app.requests.length, 1); assert.equal(replacement.querySelectorAll('[data-native-thread-item]').length, 0);
        assert.equal(replacement.querySelector('[data-native-thread-refresh]').hidden, false);
        assert.equal(replacement.querySelector('textarea').value, 'My interrupted unsaved reply');
    }
});

test('same-scope retained observations stay out of HTMX cache during and after a rerender', async () => {
    const app = setup(); const history = bundledHistory(app); app.root.dataset.nativeViewScope = 'verified-server-scope';
    await resolve(app, 0, snapshot([native('private', 'RETAINED PRIVATE OBSERVATION')]));
    const element = app.panel.appendChild(new Element('form'));
    app.document.emit('htmx:beforeRequest', { detail: { elt: element, target: app.container } });
    history.save(); assert.doesNotMatch(history.storage.get('htmx-history-cache'), /RETAINED PRIVATE/);
    const replacement = app.panel.cloneNode(true); app.container.replaceChildren(replacement);
    app.document.emit('htmx:afterSwap', { detail: { elt: element, target: app.container, xhr: { status: 200 } } });
    assert.match(replacement.textContent, /RETAINED PRIVATE/);
    assert.equal(app.requests.length, 1);
    history.save(); assert.doesNotMatch(history.storage.get('htmx-history-cache'), /RETAINED PRIVATE|Also observed/);
    assert.equal(replacement.querySelectorAll('[data-native-thread-item]').length, 0);
});

test('fresh same-scope draft/status panels retain loaded saved rows, viewport and advanced page continuity', async () => {
    for (const kind of ['draft', 'status']) {
        const app = setup(); app.root.dataset.nativeViewScope = 'verified-server-scope';
        const fresh = app.panel.cloneNode(true);
        fresh.querySelector('[data-inbox-scroll]').dataset.headerHeight = '45';
        const freshTimeline = fresh.querySelector('[data-stored-timeline-events]');
        freshTimeline.replaceChildren(
            stored('in-1', 'inbound', '2026-10-06T08:00:00Z', 'Fresh updated saved message'),
            stored('note-1', 'internal', '2026-10-06T09:30:00Z', 'Fresh updated note', 'note'),
            stored('new', 'inbound', '2026-10-06T10:30:00Z', 'New first-page event')
        );
        const composer = fresh.querySelector('textarea'); composer.value = 'Server returned saved draft';
        await resolve(app, 0, snapshot([]));
        app.scroll(80);
        await resolve(app, 1, app.page([
            stored('older-a', 'inbound', '2026-10-04T08:00:00Z', 'Loaded earlier A'),
            stored('older-b', 'inbound', '2026-10-05T08:00:00Z', 'Loaded earlier B')
        ], { key: 'saved-second', older: '/inbox/anchor-a/?history_before=cursor-2', complete: 'false' }));
        measuredTimeline(app.panel); app.scroller.scrollTop = 40;
        const action = savedAction(app, kind);
        const detail = bundledRequest(app, action.elt);
        app.container.replaceChildren(fresh); measuredTimeline(fresh);
        app.document.emit('htmx:afterSwap', { detail: { ...detail, successful: true, xhr: { status: 200 } } });
        const rows = freshTimeline.querySelectorAll('[data-timeline-event="stored"]');
        assert.deepEqual(rows.map(row => row.dataset.eventId), ['incoming:older-a', 'incoming:older-b', 'incoming:in-1', 'note:note-1', 'incoming:new']);
        assert.doesNotMatch(freshTimeline.textContent, /Stored sent reply|Stored incoming|Stored internal note/);
        assert.match(freshTimeline.textContent, /Fresh updated saved message/);
        assert.equal(freshTimeline.dataset.olderUrl, 'https://example.com/inbox/anchor-a/?history_before=cursor-2');
        assert.equal(composer.value, 'Server returned saved draft');
        assert.equal(app.requests.length, 2, 'No provider reread on successful rerender');
        const { scroller } = measuredTimeline(fresh);
        assert.equal(rows[0].getBoundingClientRect().top, -40, 'The oldest visible saved row keeps its offset');
        scroller.scrollTop = 20; scroller.emit('scroll');
        assert.equal(app.requests[2].url, 'https://example.com/inbox/anchor-a/?history_before=cursor-2');
        await resolve(app, 2, app.page([stored('ignored', 'inbound', '2026-10-03T08:00:00Z', 'Repeated page must not import')], { key: 'saved-second' }));
        assert.doesNotMatch(freshTimeline.textContent, /Repeated page must not import/);
        assert.equal(freshTimeline.dataset.olderUrl, '', 'Previous loaded page keys survive the rerender');
    }
});

test('saved pages survive an allowed rerender even when the native read failed', async () => {
    const app = setup(); app.root.dataset.nativeViewScope = 'verified-server-scope';
    const fresh = app.panel.cloneNode(true);
    app.requests[0].reject(new Error('Native unavailable')); await flush();
    app.scroll(80);
    await resolve(app, 1, app.page([stored('older', 'inbound', '2026-10-05T08:00:00Z', 'Earlier saved despite native failure')],
        { older: '/inbox/anchor-a/?history_before=cursor-2', complete: 'false' }));
    const detail = savedAction(app, 'draft'); app.document.emit('htmx:beforeRequest', { detail });
    app.container.replaceChildren(fresh);
    app.document.emit('htmx:afterSwap', { detail: { ...detail, xhr: { status: 200 } } });
    assert.match(fresh.textContent, /Earlier saved despite native failure/);
    assert.equal(fresh.querySelector('[data-stored-timeline-events]').dataset.olderUrl, 'https://example.com/inbox/anchor-a/?history_before=cursor-2');
    assert.equal(fresh.querySelectorAll('[data-native-thread-item]').length, 0);
    assert.equal(app.requests.length, 2);
});

test('fresh first-page displacement preserves older observed saved events and refreshes selected older status', async () => {
    const app = setup(); app.root.dataset.nativeViewScope = 'verified-server-scope';
    const fresh = app.panel.cloneNode(true);
    const timeline = fresh.querySelector('[data-stored-timeline-events]');
    timeline.replaceChildren(
        stored('out-1', 'outbound', '2026-10-06T09:00:00Z', 'Fresh sent reply'),
        stored('note-1', 'internal', '2026-10-06T09:30:00Z', 'Fresh note', 'note'),
        stored('new', 'inbound', '2026-10-06T10:30:00Z', 'New event moved first-page boundary')
    );
    const selected = fresh.querySelector('[data-inbox-scroll]').appendChild(new Element('article',
        { incomingMessageId: 'older-selected', storedEventBubble: '' }));
    selected.textContent = 'Fresh selected status: resolved';
    await resolve(app, 0, snapshot([])); app.scroll(80);
    await resolve(app, 1, app.page([stored('older-selected', 'inbound', '2026-10-05T08:00:00Z', 'Stale selected status: open')],
        { older: '/inbox/anchor-a/?history_before=cursor-2', complete: 'false' }));
    const detail = savedAction(app); app.document.emit('htmx:beforeRequest', { detail });
    app.container.replaceChildren(fresh);
    app.document.emit('htmx:afterSwap', { detail: { ...detail, xhr: { status: 200 } } });
    assert.deepEqual(timeline.querySelectorAll('[data-timeline-event="stored"]').map(row => row.dataset.eventId),
        ['incoming:older-selected', 'incoming:in-1', 'reply:out-1', 'note:note-1', 'incoming:new']);
    assert.match(timeline.textContent, /Stored incoming/);
    assert.match(timeline.textContent, /Fresh selected status: resolved/);
    assert.doesNotMatch(timeline.textContent, /Stale selected status: open|Stored sent reply/);
});

test('retained saved rows contain no native supplements or temporary hidden markers', async () => {
    const app = setup(); app.root.dataset.nativeViewScope = 'verified-server-scope';
    const fresh = app.panel.cloneNode(true);
    await resolve(app, 0, snapshot([native('older', 'ONLY TEMPORARY SUPPLEMENT', '2026-10-05T08:00:00Z', 'inbound')]));
    app.scroll(80);
    const earlier = stored('older', 'inbound', '2026-10-05T08:00:00Z', '');
    earlier.querySelector('[data-timeline-day-label]').hidden = true;
    await resolve(app, 1, app.page([earlier], { older: '/inbox/anchor-a/?history_before=cursor-2', complete: 'false' }));
    assert.match(app.timeline.textContent, /ONLY TEMPORARY SUPPLEMENT/);
    const detail = savedAction(app); app.document.emit('htmx:beforeRequest', { detail });
    app.container.replaceChildren(fresh);
    app.document.emit('htmx:afterSwap', { detail: { ...detail, xhr: { status: 200 } } });
    assert.equal(fresh.textContent.split('ONLY TEMPORARY SUPPLEMENT').length - 1, 1);
    app.click(fresh.querySelector('[data-native-thread-dismiss]'));
    const timeline = fresh.querySelector('[data-stored-timeline-events]');
    assert.doesNotMatch(timeline.textContent, /ONLY TEMPORARY|Also observed/);
    assert.equal(timeline.querySelectorAll('[data-native-thread-transient]').length, 0);
    assert.equal(timeline.querySelectorAll('[data-native-original-hidden]').length, 0);
    const imported = timeline.querySelector('[data-event-id="incoming:older"]');
    assert.equal(imported.querySelector('[data-timeline-day-label]').hidden, true);
});

test('saved reuse rejects changed scopes, unsafe actions, unsuccessful or unrelated responses and prior auth loss', async () => {
    for (const mode of ['missing-scope', 'changed-actor', 'changed-account', 'changed-thread', 'delete', 'reconnect',
        'unknown-action', 'get', 'missing-proof', 'wrong-anchor-action', 'lookalike-action', 'query-action',
        'cross-origin', 'response-url', '403', 'prior-403', 'complete-fresh-history', 'history-restore']) {
        const app = setup(); app.root.dataset.nativeViewScope = 'verified-server-scope';
        const fresh = app.panel.cloneNode(true);
        await resolve(app, 0, snapshot([])); app.scroll(80);
        await resolve(app, 1, app.page([stored('older', 'inbound', '2026-10-05T08:00:00Z', 'MUST NOT RESURRECT')],
            { older: '/inbox/anchor-a/?history_before=cursor-2', complete: 'false' }));
        if (mode === 'prior-403') {
            app.scroll(20); await resolve(app, 2, '', false, 403);
        }
        const action = ['delete', 'reconnect', 'unknown-action'].includes(mode) ? mode : 'status';
        const detail = savedAction(app, action);
        if (mode === 'get') detail.requestConfig.verb = 'get';
        if (mode === 'missing-proof') delete detail.requestConfig;
        if (mode === 'wrong-anchor-action') detail.requestConfig.path = detail.elt['hx-post'] = '/inbox/anchor-b/status/';
        if (mode === 'lookalike-action') detail.requestConfig.path = detail.elt['hx-post'] = '/inbox/anchor-a/status/delete/';
        if (mode === 'query-action') detail.requestConfig.path = detail.elt['hx-post'] = '/inbox/anchor-a/status/?delete=1';
        if (mode === 'cross-origin') detail.requestConfig.path = detail.elt['hx-post'] = 'https://other.example/inbox/anchor-a/status/';
        if (mode === 'missing-scope') fresh.querySelector('[data-native-thread]').dataset.nativeViewScope = '';
        if (mode.startsWith('changed-')) fresh.querySelector('[data-native-thread]').dataset.nativeViewScope = mode;
        if (mode === 'complete-fresh-history') fresh.querySelector('[data-stored-timeline-events]').dataset.historyComplete = 'true';
        app.document.emit('htmx:beforeRequest', { detail });
        app.container.replaceChildren(fresh);
        if (mode === 'history-restore') app.document.emit('htmx:historyRestore');
        else app.document.emit('htmx:afterSwap', { detail: { ...detail, xhr: {
            status: mode === '403' ? 403 : 200,
            ...(mode === 'response-url' ? { responseURL: 'https://example.com/accounts/login/' } : {})
        } } });
        assert.doesNotMatch(fresh.textContent, /MUST NOT RESURRECT/, mode);
        assert.equal(fresh.querySelector('textarea').value, 'My interrupted unsaved reply', mode);
    }
});

test('saved pagination stops at exactly 500 rows and does not partly import an overflowing page', async () => {
    for (const amount of [2, 3]) {
        const rows = Array.from({ length: 498 }, (_, i) => stored('saved-' + i, 'inbound', '2026-10-06T08:00:00Z', 'Saved ' + i));
        const app = setup({ rows }); await resolve(app, 0, snapshot([])); app.scroll(80);
        const before = app.timeline.outerHTML; const top = app.scroller.scrollTop;
        await resolve(app, 1, app.page(Array.from({ length: amount }, (_, i) => stored('older-' + i, 'inbound',
            '2026-10-05T08:00:00Z', 'Extra page ' + i)), { older: '/inbox/anchor-a/?history_before=next', complete: 'false' }));
        if (amount === 3) {
            assert.equal(app.timeline.outerHTML.replace('data-older-url=""', 'data-older-url="/inbox/anchor-a/?history_before=cursor-1"'), before);
            assert.equal(app.scroller.scrollTop, top);
            assert.match(app.pageStatus.textContent, /additional page was not displayed/);
        } else assert.equal(app.timeline.querySelectorAll('[data-timeline-event="stored"]').length, 500);
        app.scroll(0);
        assert.equal(app.requests.length, 2, 'The bound prevents another page request');
        assert.match(app.pageStatus.textContent, /500 messages/);
        assert.match(app.pageStatus.textContent, /Earlier|earlier/);
        assert.equal(app.composer.value, 'My interrupted unsaved reply');
    }
});

test('saved pagination stops after 25 pages without dropping already displayed rows', async () => {
    const app = setup(); await resolve(app, 0, snapshot([]));
    for (let page = 2; page <= 25; page++) {
        app.scroller.scrollTop = 200; app.scroller.emit('scroll'); app.scroll(80);
        await resolve(app, page - 1, app.page([stored('older-' + page, 'inbound', '2026-10-05T08:00:00Z', 'Earlier ' + page)],
            { key: 'saved-page-' + page, older: '/inbox/anchor-a/?history_before=' + page, complete: 'false' }));
    }
    const count = app.timeline.querySelectorAll('[data-timeline-event="stored"]').length;
    app.scroll(0);
    assert.equal(app.requests.length, 25);
    assert.equal(count, 27);
    assert.equal(app.timeline.querySelectorAll('[data-timeline-event="stored"]').length, count);
    assert.match(app.pageStatus.textContent, /25 pages.*Earlier saved activity may remain/);
});

test('unknown fresh timestamps refuse old-row restoration and explain that the fresh page is shown', async () => {
    const app = setup(); app.root.dataset.nativeViewScope = 'verified-server-scope';
    const fresh = app.panel.cloneNode(true);
    fresh.querySelector('[data-event-id="reply:out-1"]').dataset.eventTime = '';
    await resolve(app, 0, snapshot([])); app.scroll(80);
    await resolve(app, 1, app.page([stored('older', 'inbound', '2026-10-05T08:00:00Z', 'Unverified outside boundary')],
        { older: '/inbox/anchor-a/?history_before=next', complete: 'false' }));
    const detail = savedAction(app); app.document.emit('htmx:beforeRequest', { detail });
    app.container.replaceChildren(fresh);
    app.document.emit('htmx:afterSwap', { detail: { ...detail, xhr: { status: 200 } } });
    assert.doesNotMatch(fresh.textContent, /Unverified outside boundary/);
    const status = fresh.querySelector('[data-stored-history-status]').textContent;
    assert.match(status, /fresh saved page is shown; scroll up/);
    assert.doesNotMatch(status, /preserved|position retained|anchor retained/);
    assert.equal(fresh.querySelector('textarea').value, 'My interrupted unsaved reply');
});

test('failed unchanged-panel requests preserve saved page keys and advanced continuation', async () => {
    const app = setup(); app.root.dataset.nativeViewScope = 'verified-server-scope';
    await resolve(app, 0, snapshot([])); app.scroll(80);
    await resolve(app, 1, app.page([stored('older', 'inbound', '2026-10-05T08:00:00Z', 'Loaded older')],
        { key: 'saved-second', older: '/inbox/anchor-a/?history_before=next', complete: 'false' }));
    const detail = savedAction(app); app.document.emit('htmx:beforeRequest', { detail });
    app.document.emit('htmx:sendError', { detail });
    app.scroll(20);
    assert.equal(app.requests[2].url, 'https://example.com/inbox/anchor-a/?history_before=next');
    await resolve(app, 2, app.page([stored('ignored', 'inbound', '2026-10-04T08:00:00Z', 'Repeated key should not import')],
        { key: 'saved-second' }));
    assert.doesNotMatch(app.timeline.textContent, /Repeated key should not import/);
    assert.match(app.timeline.textContent, /Loaded older/);
});

test('editing an existing draft is accepted only from its own rendered draft action', async () => {
    const app = setup(); app.root.dataset.nativeViewScope = 'verified-server-scope';
    const fresh = app.panel.cloneNode(true);
    await resolve(app, 0, snapshot([])); app.scroll(80);
    await resolve(app, 1, app.page([stored('older', 'inbound', '2026-10-05T08:00:00Z', 'Earlier during draft edit')],
        { older: '/inbox/anchor-a/?history_before=next', complete: 'false' }));
    const draft = app.panel.appendChild(new Element('div', { draftTargetId: 'anchor-a' })); draft.id = 'draft-reply-a';
    const form = draft.appendChild(new Element('form')); form['hx-post'] = '/inbox/replies/reply-a/edit/';
    const detail = { elt: form, target: app.container, requestConfig: { verb: 'post', path: form['hx-post'] } };
    app.document.emit('htmx:beforeRequest', { detail }); app.container.replaceChildren(fresh);
    app.document.emit('htmx:afterSwap', { detail: { ...detail, xhr: { status: 200 } } });
    assert.match(fresh.textContent, /Earlier during draft edit/);
    assert.equal(app.requests.length, 2);
});
