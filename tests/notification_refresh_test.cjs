const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../templates/base.html'), 'utf8');
const start = html.indexOf('        /* Notification bell - Alpine.js component */');
const source = html.slice(start, html.indexOf('</script>', start));
function fixture({ open = true, mounted = true } = {}) {
    let count = 0;
    const handlers = new Map(), loads = [], element = {}, drawer = {};
    const bell = { open, fetchCount() { count++; } };
    const document = {
        body: { addEventListener(name, handler) { handlers.set(name, handler); } },
        querySelector(selector) { return selector.includes('x-data') ? (mounted ? element : null) : drawer; },
    };
    const window = { Alpine: { $data: () => bell }, htmx: { trigger: (node, name) => loads.push([node, name]) } };
    vm.runInNewContext(source, { window, document });
    return { emit: (name, event = {}) => handlers.get(name)?.(event), count: () => count, loads, drawer };
}
test('canonical fetch acknowledgement refreshes Alpine 3 and an open drawer', () => {
    const f = fixture(); f.emit('notificationsChanged');
    assert.equal(f.count(), 1); assert.deepEqual(f.loads, [[f.drawer, 'load']]);
});
test('acknowledgement does not open a closed drawer', () => {
    const f = fixture({ open: false }); f.emit('notificationsChanged');
    assert.equal(f.count(), 1); assert.equal(f.loads.length, 0);
});
test('drawer settle cannot start a reload loop', () => {
    const f = fixture(); f.emit('htmx:afterSettle', { detail: { target: { id: 'notification-drawer-content' } } });
    assert.equal(f.count(), 1); assert.equal(f.loads.length, 0);
});
test('unmounted bell and unrelated settled content are ignored', () => {
    const f = fixture({ mounted: false }); f.emit('notificationsChanged');
    assert.equal(f.count(), 0);
    const g = fixture(); g.emit('htmx:afterSettle', { detail: { target: { id: 'other' } } });
    assert.equal(g.count(), 0);
});
function asynchronousBell() {
    const pending = [];
    const globals = { document: { body: { addEventListener() {} } }, window: {},
        fetch: () => new Promise(resolve => pending.push(resolve)), clearInterval() {} };
    vm.runInNewContext(source, globals);
    return { bell: globals.notificationBell(), pending };
}
test('a slower old poll cannot overwrite a newer count', async () => {
    const { bell, pending } = asynchronousBell();
    const old = bell.fetchCount(), current = bell.fetchCount();
    pending[1]({ ok: true, json: async () => ({ count: 0 }) }); await current;
    pending[0]({ ok: true, json: async () => ({ count: 7 }) }); await old;
    assert.equal(bell.unreadCount, 0);
});
test('destroy invalidates an outstanding count request', async () => {
    const { bell, pending } = asynchronousBell(); bell.unreadCount = 2;
    const request = bell.fetchCount(); bell.destroy();
    pending[0]({ ok: true, json: async () => ({ count: 9 }) }); await request;
    assert.equal(bell.unreadCount, 2);
});
