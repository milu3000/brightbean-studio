const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const root = path.resolve(__dirname, '../..');
const navigation = fs.readFileSync(path.join(root, 'static/js/inbox-navigation.js'), 'utf8');
const controller = fs.readFileSync(path.join(root, 'templates/inbox/feed.html'), 'utf8')
    .match(/function inboxController\(\) \{[\s\S]*?<\/script>/)[0]
    .replace('</script>', '').replace(/\{\{[\s\S]*?\}\}/g, '0').replace(/\{%[\s\S]*?%\}/g, '');

function harness() {
    const rows = new Map(['a', 'b', 'c'].map(id => [id, { id: 'msg-' + id, classList: {
        values: new Set(['is-unread']),
        add(value) { this.values.add(value); },
        remove(value) { this.values.delete(value); },
    }}]));
    const panel = { id: 'inbox-detail-panel' };
    const thread = { id: 'inbox-thread', closest() { return panel; } };
    const calls = [];
    let state;
    function request(verb = 'get', target = panel, url = '/current/') {
        const xhr = { aborted: false, abort() {
            this.aborted = true;
            state.afterDetailRequest({ detail: { xhr: this } });
        }};
        state.beforeDetailRequest({ detail: { target, xhr, requestConfig: { verb, path: url } } });
        return xhr;
    }
    const context = vm.createContext({
        window: { innerWidth: 390 },
        document: {
            getElementById(id) { return rows.get(id.replace('msg-', '')); },
            querySelectorAll() { return [...rows.values()]; },
        },
        htmx: { ajax(verb, url, options) {
            calls.push({ verb, url, options, xhr: request(verb, panel, url) });
            return Promise.resolve();
        }},
    });
    vm.runInContext(navigation + '\n' + controller, context);
    state = context.inboxController();
    function swap(xhr) {
        const event = { detail: { xhr, shouldSwap: true }, prevented: false,
            preventDefault() { this.prevented = true; } };
        state.beforeDetailSwap(event);
        return event;
    }
    return { state, rows, panel, thread, calls, request, swap };
}

test('rapid different conversations abort old GET and reject delayed old responses', () => {
    const h = harness();
    h.state.selectMessage('a', '/a/');
    const old = h.calls[0].xhr;
    h.state.selectMessage('b', '/b/');
    const current = h.calls[1].xhr;
    assert.equal(old.aborted, true);
    assert.equal(h.swap(old).prevented, true);
    assert.equal(h.swap(current).detail.shouldSwap, true);
    assert.equal(h.rows.get('a').classList.values.has('is-active'), false);
    assert.equal(h.rows.get('b').classList.values.has('is-active'), true);
    assert.equal(h.calls[1].options.source, '#inbox-detail-panel');
    assert.equal(h.state.detailLoading, true);
    h.state.afterDetailRequest({ detail: { xhr: old } });
    assert.equal(h.state.detailLoading, true);
    h.state.afterDetailRequest({ detail: { xhr: current } });
    assert.equal(h.state.detailLoading, false);
});

test('Back cancels pending navigation and a late response cannot reopen detail', () => {
    const h = harness();
    h.state.selectMessage('a', '/a/');
    const old = h.calls[0].xhr;
    h.state.closeDetail();
    assert.equal(old.aborted, true);
    assert.equal(h.state.activePanel, 'list');
    assert.equal(h.state.detailLoading, false);
    assert.equal(h.swap(old).detail.shouldSwap, false);
    h.state.afterDetailRequest({ detail: { xhr: old } });
    assert.equal(h.state.activePanel, 'list');
});

test('repeated clicks replace the pending GET without a stale loading reset', () => {
    const h = harness();
    for (let i = 0; i < 3; i++) h.state.selectMessage('a', '/a/');
    assert.deepEqual(h.calls.map(call => call.xhr.aborted), [true, true, false]);
    assert.equal(h.state.pendingDetailRequest, h.calls[2].xhr);
    assert.equal(h.state.detailLoading, true);
    for (const call of h.calls.slice(0, 2)) assert.equal(h.swap(call.xhr).prevented, true);
    assert.equal(h.swap(h.calls[2].xhr).prevented, false);
});

test('history or Manage navigation is superseded by a newer conversation', () => {
    const h = harness();
    const history = h.request('get');
    h.state.selectMessage('b', '/b/');
    assert.equal(history.aborted, true);
    assert.equal(h.swap(history).detail.shouldSwap, false);
    assert.equal(h.swap(h.calls[0].xhr).detail.shouldSwap, true);
});

test('pending sends are never aborted but their late HTML cannot enter a newer conversation', () => {
    const h = harness();
    const send = h.request('post', h.thread);
    const draft = h.request('post', h.panel);
    h.state.selectMessage('b', '/b/');
    assert.equal(send.aborted, false);
    assert.equal(draft.aborted, false);
    assert.equal(h.swap(send).detail.shouldSwap, false);
    assert.equal(h.swap(draft).detail.shouldSwap, false);
});

test('unrelated HTMX work is left alone and destroying component cancels only navigation', () => {
    const h = harness();
    const other = h.request('get', { id: 'notifications', closest() { return null; } });
    const current = h.request('get');
    h.state.destroy();
    assert.equal(current.aborted, true);
    assert.equal(other.aborted, false);
    assert.equal(h.swap(other).detail.shouldSwap, true);
});


test('failed new selection never re-enables previous composer and Retry uses requested URL', () => {
    const h = harness();
    h.state.selectMessage('b', '/b/');
    const failed = h.calls[0].xhr;
    assert.equal(h.state.detailReady, false);
    h.state.afterDetailRequest({ detail: { xhr: failed, failed: true } });
    assert.equal(h.state.detailLoading, false);
    assert.equal(h.state.detailReady, false);
    assert.equal(h.state.detailError, true);
    h.state.retryDetail();
    assert.equal(h.calls[1].url, '/b/');
    assert.equal(h.state.detailLoading, true);
    const retried = h.calls[1].xhr;
    h.state.afterDetailSwap({ detail: { xhr: failed, target: h.panel } });
    assert.equal(h.state.detailReady, false);
    h.state.afterDetailSwap({ detail: { xhr: retried, target: h.panel } });
    h.state.afterDetailRequest({ detail: { xhr: retried } });
    assert.equal(h.state.detailReady, true);
    assert.equal(h.state.detailError, false);
});

test('late afterSwap from a cancelled read cannot unhide detail after Back', () => {
    const h = harness();
    const old = h.request('get');
    h.state.closeDetail();
    h.state.afterDetailSwap({ detail: { xhr: old, target: h.panel } });
    assert.equal(h.state.detailReady, false);
    assert.equal(h.state.activePanel, 'list');
});

test('successful 204/no-swap response cannot reveal the old composer', () => {
    const h = harness();
    const noContent = h.request('get');
    h.state.afterDetailRequest({ detail: { xhr: noContent, successful: true } });
    assert.equal(h.state.detailReady, false);
    assert.equal(h.state.detailLoading, false);
    assert.equal(h.state.detailError, true);
});

test('filter replacing the selected list row cannot detach the navigation request source', () => {
    const h = harness();
    h.state.selectMessage('a', '/a/');
    const call = h.calls[0];
    h.rows.clear(); // An independent HTMX filter/list swap removed all rows.
    assert.equal(call.options.source, '#inbox-detail-panel');
    h.state.afterDetailSwap({ detail: { xhr: call.xhr, target: h.panel } });
    h.state.afterDetailRequest({ detail: { xhr: call.xhr } });
    assert.equal(h.state.detailLoading, false);
    assert.equal(h.state.detailReady, true);
});

test('hash/native Back and Forward preserve ready standalone detail', () => {
    const h = harness();
    h.state.activePanel = 'detail';
    for (const state of [null, {}, { unrelated: true }]) {
        h.state.restoreDetailHistory({ state });
        assert.equal(h.state.detailReady, true);
        assert.equal(h.state.activePanel, 'detail');
        assert.equal(h.state.detailError, false);
    }
});

test('HTMX history restoration cancels the old in-flight detail', () => {
    const h = harness();
    h.state.selectMessage('a', '/a/');
    const old = h.calls[0].xhr;
    h.state.restoreDetailHistory({ state: { htmx: true } });
    assert.equal(old.aborted, true);
    assert.equal(h.state.activePanel, 'list');
    assert.equal(h.swap(old).detail.shouldSwap, false);
});
