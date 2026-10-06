const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/inbox-native-thread.js'), 'utf8');
const camel = name => name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());

class Element {
    constructor(tag, dataset = {}) {
        this.tagName = tag;
        this.dataset = dataset;
        this.children = [];
        this.parent = null;
        this.hidden = false;
        this.disabled = false;
        this._text = '';
    }
    appendChild(element) { element.parent = this; this.children.push(element); return element; }
    replaceChildren(...elements) {
        this.children.forEach(child => { child.parent = null; });
        this.children = [];
        this._text = '';
        elements.forEach(child => this.appendChild(child));
    }
    set textContent(value) { this.replaceChildren(); this._text = value; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(' '); }
    set innerHTML(_) { throw new Error('Untrusted native content must never be interpreted as HTML'); }
    get innerHTML() { return this._text + this.children.map(child => child.outerHTML).join(''); }
    get outerHTML() { return '<' + this.tagName + '>' + this.innerHTML + '</' + this.tagName + '>'; }
    cloneNode(deep) {
        const copy = new Element(this.tagName, { ...this.dataset });
        copy._text = this._text;
        copy.value = this.value;
        copy.defaultValue = this.defaultValue;
        copy.hidden = this.hidden;
        copy.disabled = this.disabled;
        if (deep) this.children.forEach(child => copy.appendChild(child.cloneNode(true)));
        return copy;
    }
    matches(selector) {
        if (selector === '[name=csrfmiddlewaretoken]') return this.name === 'csrfmiddlewaretoken';
        const data = selector.match(/^\[data-([a-z-]+)\]$/);
        return data ? Object.hasOwn(this.dataset, camel(data[1])) : this.tagName === selector;
    }
    closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector); }
    querySelector(selector) {
        for (const child of this.children) {
            if (child.matches(selector)) return child;
            const nested = child.querySelector(selector);
            if (nested) return nested;
        }
        return null;
    }
    querySelectorAll(selector) {
        return this.children.flatMap(child => [
            ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)
        ]);
    }
    contains(element) { return this === element || this.children.some(child => child.contains(element)); }
    get isConnected() { return this.tagName === 'document' || Boolean(this.parent?.isConnected); }
}

function events(target) {
    const listeners = new Map();
    target.addEventListener = (name, callback) => {
        listeners.set(name, [...(listeners.get(name) || []), callback]);
    };
    target.emit = (name, event = {}) => {
        for (const listener of listeners.get(name) || []) listener(event);
    };
    return target;
}

function setup() {
    const document = events(new Element('document'));
    document.createElement = tag => new Element(tag);
    const window = events({});
    const requests = [];
    const csrf = document.appendChild(new Element('input'));
    csrf.name = 'csrfmiddlewaretoken';
    csrf.value = 'test-csrf-token';
    const container = document.appendChild(new Element('main'));
    const panel = container.appendChild(new Element('div', { inboxPanel: '', selectedMessageId: 'anchor-a' }));
    const root = panel.appendChild(new Element('section', {
        nativeThread: '', anchorId: 'anchor-a', refreshUrl: '/inbox/anchor-a/native-thread/'
    }));
    const button = root.appendChild(new Element('button', { nativeThreadRefresh: '' }));
    const result = root.appendChild(new Element('div', { nativeThreadResult: '' }));
    result.hidden = true;
    const dismiss = result.appendChild(new Element('button', { nativeThreadDismiss: '' }));
    const status = result.appendChild(new Element('p', { nativeThreadStatus: '' }));
    const warning = result.appendChild(new Element('p', { nativeThreadWarning: '' }));
    const items = result.appendChild(new Element('div', { nativeThreadItems: '' }));
    const history = panel.appendChild(new Element('div'));
    history.id = 'inbox-thread';
    history.textContent = 'Stored incoming and sent history';
    const composer = panel.appendChild(new Element('textarea'));
    composer.value = 'My interrupted unsaved reply';
    composer.defaultValue = 'Saved draft';
    const context = {
        document, window, AbortController, URL, Date,
        fetch(url, options) {
            return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject }));
        }
    };
    vm.runInNewContext(source, context);
    return { document, window, container, panel, root, button, dismiss, result, status, warning, items,
        history, composer, csrf, requests, context,
        click(target) { document.emit('click', { target, preventDefault() {} }); }
    };
}

const flush = () => new Promise(resolve => setImmediate(resolve));
const snapshot = (body = 'Native answer') => ({
    status: 'observed', reason_code: 'bounded_snapshot', anchor_message_id: 'anchor-a',
    checked_at: '2026-10-06T10:00:00Z', history_complete: false, persisted: false,
    items: [{ direction: 'outbound', body, occurred_at: '2026-10-06T09:00:00Z', attachments: [] }],
    coverage: {}, newer_outbound_observed: true
});

function bundledHistory(app) {
    // Run the actual pinned HTMX 2.0.4 saveCurrentPageToHistory, cache-write
    // and clean-clone implementations, with their ordinary browser adapters.
    const bundled = fs.readFileSync(path.join(__dirname, '../static/js/htmx.min.js'), 'utf8');
    assert.ok(bundled.includes('version:"2.0.4"'), 'Review history regression on HTMX upgrades');
    const begin = bundled.indexOf('function Ut(');
    const end = bundled.indexOf('function $t(', begin);
    assert.ok(begin > 0 && end > begin);
    const storage = new Map();
    const replaced = [];
    const context = {
        ne: () => app.document,
        B: () => true,
        U: value => value,
        S: JSON.parse,
        se: (values, callback) => values.forEach(callback),
        x: (element, selector) => element.querySelectorAll(selector),
        G: () => {},
        he: (_, name, detail) => app.document.emit(name, { detail }),
        fe: (_, name) => { throw new Error(name); },
        Q: { config: { historyEnabled: true, historyCacheSize: 10, requestClass: 'htmx-request' } },
        Bt: null,
        location: { pathname: '/inbox/', search: '?status=open' },
        window: { scrollY: 0, location: { href: 'https://example.com/inbox/?status=open' } },
        history: { replaceState: (...args) => replaced.push(args) },
        localStorage: {
            getItem: key => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, value),
            removeItem: key => storage.delete(key)
        }
    };
    app.document.body = app.container;
    app.document.title = 'Inbox';
    vm.runInNewContext(bundled.slice(begin, end), context);
    return { storage, replaced, save() { context.zt(); } };
}
async function resolve(app, index, data = snapshot(), ok = true) {
    app.requests[index].resolve({ ok, json: async () => data });
    await flush();
}

test('only a user click reads; POST includes CSRF and never includes composer data', async () => {
    const app = setup();
    app.document.emit('DOMContentLoaded');
    app.document.emit('htmx:afterSwap', { detail: { target: app.panel } });
    assert.equal(app.requests.length, 0);
    app.click(app.button);
    const request = app.requests[0];
    assert.equal(request.url, '/inbox/anchor-a/native-thread/');
    assert.equal(request.options.method, 'POST');
    assert.equal(request.options.headers['X-CSRFToken'], 'test-csrf-token');
    assert.equal(request.options.cache, 'no-store');
    assert.equal(request.options.credentials, 'same-origin');
    assert.equal(request.options.body, undefined);
    await resolve(app, 0);
    assert.equal(app.composer.value, 'My interrupted unsaved reply');
    assert.equal(app.history.textContent, 'Stored incoming and sent history');
    assert.equal(app.panel.children.at(-1), app.composer);
    assert.match(app.status.textContent, /Not saved.*incomplete or already out of date/);
    assert.match(app.status.textContent, /does not establish who sent/);
    assert.match(app.items.textContent, /Account-side message · observed on platform/);
    assert.match(app.warning.textContent, /newer account-side message/);
    assert.equal(app.items.querySelector('button'), null);
    assert.equal(app.items.querySelector('form'), null);
});

test('duplicate clicks are coalesced; dismiss and repeat ignore the older result', async () => {
    const app = setup();
    app.click(app.button);
    app.click(app.button);
    assert.equal(app.requests.length, 1);
    app.click(app.dismiss);
    assert.equal(app.requests[0].options.signal.aborted, true);
    assert.equal(app.result.hidden, true);
    app.click(app.button);
    await resolve(app, 1, snapshot('Newest reply'));
    await resolve(app, 0, snapshot('Discarded older reply'));
    assert.match(app.items.textContent, /Newest reply/);
    assert.doesNotMatch(app.items.textContent, /Discarded older reply/);
    assert.equal(app.composer.value, 'My interrupted unsaved reply');
});

test('navigation invalidates a read before the new selection arrives', async () => {
    const app = setup();
    app.click(app.button);
    app.document.emit('htmx:beforeRequest', { detail: { elt: { dataset: { inboxOpenMessage: 'anchor-b' } } } });
    assert.equal(app.requests[0].options.signal.aborted, true);
    await resolve(app, 0, snapshot('Must not appear'));
    assert.equal(app.items.textContent, '');
    assert.equal(app.result.hidden, true);
    assert.equal(app.composer.value, 'My interrupted unsaved reply');
});

test('disconnected or changed anchor cannot accept a late response', async () => {
    for (const change of [app => app.container.replaceChildren(), app => { app.panel.dataset.selectedMessageId = 'anchor-b'; }]) {
        const app = setup();
        app.click(app.button);
        change(app);
        await resolve(app, 0, snapshot('Wrong anchor contents'));
        assert.equal(app.items.textContent, '');
    }
});

test('dismiss during JSON parsing prevents content from reappearing', async () => {
    const app = setup();
    let release;
    app.click(app.button);
    app.requests[0].resolve({ ok: true, json: () => new Promise(resolve => { release = resolve; }) });
    await flush();
    app.click(app.dismiss);
    release(snapshot('Late parsed reply'));
    await flush();
    assert.equal(app.items.textContent, '');
    assert.equal(app.result.hidden, true);
});

test('timeline pagination preserves the snapshot; replacing the detail clears it', async () => {
    const app = setup();
    app.click(app.button);
    app.document.emit('htmx:beforeSwap', { detail: { target: app.history } });
    assert.equal(app.requests[0].options.signal.aborted, false);
    await resolve(app, 0);
    app.document.emit('htmx:beforeSwap', { detail: { target: app.container } });
    assert.equal(app.items.textContent, '');
    assert.equal(app.result.hidden, true);
});

test('Back, Forward, browser restore and mobile close discard temporary content', async () => {
    for (const event of ['popstate', 'pagehide', 'pageshow', 'mobile-back']) {
        const app = setup();
        app.click(app.button);
        await resolve(app, 0);
        if (event === 'mobile-back') app.click(app.panel.appendChild(new Element('a', { inboxBack: '' })));
        else app.window.emit(event, { persisted: true });
        assert.equal(app.result.hidden, true, event);
        assert.equal(app.items.textContent, '', event);
        assert.equal(app.composer.value, 'My interrupted unsaved reply');
    }
});

test('platform text and media titles stay literal; unsafe links have a truthful fallback', async () => {
    const app = setup();
    app.click(app.button);
    const data = snapshot('<img src=x onerror=alert(1)>');
    data.items[0].attachments = [
        { type: 'image', title: '<script>bad()</script>', availability: 'available', url: 'javascript:alert(1)' },
        { type: 'file', title: 'Real file', availability: 'available', url: 'https://example.com/file.pdf' }
    ];
    data.items[0].body_truncated = true;
    data.more_available = true;
    await resolve(app, 0, data);
    assert.match(app.items.textContent, /<img src=x onerror=alert\(1\)>/);
    assert.match(app.items.textContent, /<script>bad\(\)<\/script>/);
    assert.equal(app.items.querySelector('img'), null);
    assert.equal(app.items.querySelector('script'), null);
    assert.match(app.items.textContent, /Media unavailable/);
    assert.match(app.items.textContent, /shortened in this view/);
    assert.match(app.items.textContent, /No further page was loaded/);
    const link = app.items.querySelector('a');
    assert.equal(link.href, 'https://example.com/file.pdf');
    assert.equal(link.rel, 'noopener noreferrer');
});

test('network errors and unexpected anchor results preserve drafts and allow retry', async () => {
    const app = setup();
    app.click(app.button);
    app.requests[0].reject(new Error('PRIVATE RAW ERROR'));
    await flush();
    assert.match(app.status.textContent, /Your draft is still here/);
    assert.doesNotMatch(app.status.textContent, /PRIVATE RAW/);
    assert.equal(app.button.disabled, false);
    app.click(app.button);
    await resolve(app, 1, { ...snapshot('Wrong contents'), anchor_message_id: 'wrong' });
    assert.equal(app.items.textContent, '');
    assert.equal(app.composer.value, 'My interrupted unsaved reply');
});

test('auth revocation and unavailable results show reasons without contents', async () => {
    for (const [reason, expected] of [
        ['authorization_revoked', /access changed/], ['missing_native_thread', /no reliable platform/],
        ['unverified_thread', /one-to-one/], ['unsupported_platform', /does not support/]
    ]) {
        const app = setup();
        app.click(app.button);
        await resolve(app, 0, { ...snapshot('DO NOT RENDER'), status: 'unavailable', reason_code: reason }, false);
        assert.match(app.status.textContent, expected);
        assert.equal(app.items.textContent, '');
    }
});

test('empty result never claims no reply exists; no body or media stays unverified', async () => {
    const app = setup();
    app.click(app.button);
    await resolve(app, 0, { ...snapshot(), items: [], newer_outbound_observed: false });
    assert.match(app.items.textContent, /does not mean no reply exists/);
    assert.equal(app.warning.hidden, true);
    app.click(app.button);
    await resolve(app, 1, snapshot(''));
    assert.match(app.items.textContent, /original content has not been verified/);
});

test('missing CSRF never sends a request; reloading script never duplicates listeners', () => {
    const app = setup();
    vm.runInNewContext(source, app.context);
    app.csrf.value = '';
    app.click(app.button);
    assert.equal(app.requests.length, 0);
    app.csrf.value = 'token';
    app.click(app.button);
    assert.equal(app.requests.length, 1);
});

test('real HTMX list-only history clone/write saves ordinary history without native content', async () => {
    const app = setup();
    const history = bundledHistory(app);
    app.click(app.button);
    await resolve(app, 0, snapshot('PRIVATE TRANSIENT PLATFORM BODY'));
    assert.match(app.items.textContent, /PRIVATE TRANSIENT/);
    // A filter or page link swaps only the list; neither existing navigation
    // hook sees a selected-message request or a containing panel swap.
    const list = app.container.appendChild(new Element('div'));
    list.id = 'inbox-message-list';
    app.document.emit('htmx:beforeRequest', { detail: { elt: { dataset: {} }, target: list } });
    app.document.emit('htmx:beforeSwap', { detail: { target: list } });
    assert.match(app.items.textContent, /PRIVATE TRANSIENT/);
    history.save();
    const cached = history.storage.get('htmx-history-cache');
    assert.ok(cached, 'Ordinary history navigation keeps its cache');
    assert.match(cached, /Stored incoming and sent history/);
    assert.doesNotMatch(cached, /PRIVATE TRANSIENT|Account-side message|Read at|newer account-side/);
    assert.equal(history.replaced.length, 1);
    assert.equal(app.result.hidden, true);
    assert.equal(app.composer.value, 'My interrupted unsaved reply');
    assert.equal(app.composer.defaultValue, 'Saved draft');
    assert.equal(app.panel.children.at(-1), app.composer);
});

test('HTMX history save aborts pending reads and late responses cannot reappear or be cached', async () => {
    const app = setup();
    const history = bundledHistory(app);
    app.click(app.button);
    history.save();
    assert.equal(app.requests[0].options.signal.aborted, true);
    await resolve(app, 0, snapshot('LATE PRIVATE BODY'));
    history.save();
    assert.equal(app.items.textContent, '');
    assert.doesNotMatch(history.storage.get('htmx-history-cache'), /LATE PRIVATE|Reading platform/);
    assert.equal(app.composer.value, 'My interrupted unsaved reply');
});

test('history restore clears a copied snapshot even when active still points at the old DOM', async () => {
    const app = setup();
    app.click(app.button);
    await resolve(app, 0, snapshot('PREVIOUSLY CACHED PRIVATE BODY'));
    const restored = app.panel.cloneNode(true);
    app.container.replaceChildren(restored);
    assert.match(restored.textContent, /PREVIOUSLY CACHED PRIVATE/);
    app.document.emit('htmx:historyRestore', { detail: { item: {} } });
    assert.equal(restored.querySelector('[data-native-thread-items]').textContent, '');
    assert.equal(restored.querySelector('[data-native-thread-result]').hidden, true);
    assert.equal(restored.querySelector('textarea').value, 'My interrupted unsaved reply');
    assert.equal(restored.querySelector('[data-native-thread-refresh]').disabled, false);
});
