const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/js/inbox-filters.js'), 'utf8');
function setup(value = 'coffee') {
    const events = new Map(), triggered = [];
    const input = { value, matches: selector => selector === '[data-inbox-search]', focus() { this.focused = true; } };
    const clear = { hidden: false, closest: selector => selector === 'form' ? form : clear };
    const form = { querySelector: selector => selector === '[data-inbox-search]' ? input : clear };
    input.closest = () => form;
    const document = { readyState: 'complete', querySelector: () => null, querySelectorAll: () => [input],
        addEventListener(name, fn) { events.set(name, [...(events.get(name) || []), fn]); } };
    const context = { document, window: { htmx: { trigger(...args) { triggered.push(args); } } } };
    vm.runInNewContext(source, context);
    return { input, clear, form, triggered, context, emit(name, event) { (events.get(name) || []).forEach(fn => fn(event)); } };
}
test('clear is keyboard-accessible through the button click and keeps focus in the empty search', () => {
    const app = setup(); assert.equal(app.clear.hidden, false);
    app.emit('click', { target: app.clear });
    assert.equal(app.input.value, ''); assert.equal(app.clear.hidden, true); assert.equal(app.input.focused, true);
    assert.equal(app.triggered[0][1], 'inbox:clear-search');
});
test('clearing removes query and pagination while preserving every unrelated filter', () => {
    const app = setup(); const parameters = { q: '', page: 3, cursor: 'old', platform: 'facebook', account: ['a', 'b'], assigned: 'person', status: 'open', date_from: '2026-10-01' };
    app.emit('htmx:configRequest', { detail: { elt: app.input, parameters } });
    assert.deepEqual(parameters, { platform: 'facebook', account: ['a', 'b'], assigned: 'person', status: 'open', date_from: '2026-10-01' });
});
test('typed or pasted values toggle the clear button and duplicate installation adds no handlers', () => {
    const app = setup(''); assert.equal(app.clear.hidden, true);
    app.input.value = 'tea'; app.emit('input', { target: app.input }); assert.equal(app.clear.hidden, false);
    vm.runInNewContext(source, app.context); app.emit('click', { target: app.clear }); assert.equal(app.triggered.length, 1);
});

test('read-only plain search submits the empty query while retaining the account selection', () => {
    const app = setup(); let submissions = 0;
    app.form.dataset = {inboxPlainFilters:''}; app.form.requestSubmit = () => { submissions++; };
    app.emit('click', {target:app.clear});
    assert.equal(submissions,1); assert.equal(app.triggered.length,0); assert.equal(app.input.value,'');
});
