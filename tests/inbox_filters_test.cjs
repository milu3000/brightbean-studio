const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/js/inbox-filters.js'), 'utf8');
function setup(value = 'coffee') {
    const events = new Map(), triggered = [], operations = [];
    const input = { value, matches: selector => selector === '[data-inbox-search]', focus() { this.focused = true; } };
    const clear = { hidden: false, closest: selector => selector === 'form' ? form : clear };
    const form = { querySelector: selector => selector === '[data-inbox-search]' ? input : clear };
    input.closest = () => form;
    const document = { readyState: 'complete', querySelector: () => null, querySelectorAll: () => [input],
        addEventListener(name, fn) { events.set(name, [...(events.get(name) || []), fn]); } };
    const context = { document, window: { htmx: { process(element) { operations.push(['process', element]); }, trigger(...args) { operations.push(['trigger', ...args]); triggered.push(args); } } } };
    vm.runInNewContext(source, context);
    return { input, clear, form, triggered, operations, context, emit(name, event) { (events.get(name) || []).forEach(fn => fn(event)); } };
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
test('unified mutation refresh drops snapshot pagination but preserves all selected filters', () => {
    const app = setup();
    const form = {closest:()=>form,matches:selector=>['[data-canonical-filters]','[data-unified-filters]'].includes(selector)};
    const parameters = {domain:'comment',q:'coffee',page:3,cursor:'stale-snapshot',platform:'facebook',account:'synthetic-account',status:'unread',view:'mine'};
    app.emit('htmx:configRequest',{detail:{elt:form,parameters}});
    assert.deepEqual(parameters,{domain:'comment',q:'coffee',platform:'facebook',account:'synthetic-account',status:'unread',view:'mine'});
});
test('non-unified canonical refresh retains its existing cursor contract', () => {
    const app = setup();
    const form = {closest:()=>form,matches:selector=>selector==='[data-canonical-filters]'};
    const parameters = {domain:'dm',cursor:'canonical-cursor',account:'synthetic-account',workflow:'waiting'};
    app.emit('htmx:configRequest',{detail:{elt:form,parameters}});
    assert.deepEqual(parameters,{domain:'dm',cursor:'canonical-cursor',account:'synthetic-account',workflow:'waiting'});
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

test('clear initializes a newly swapped input before dispatching the HTMX trigger', () => {
    const app = setup();
    app.emit('click', { target: app.clear });
    assert.deepEqual(app.operations.map(item => item[0]), ['process', 'trigger']);
    assert.equal(app.operations[0][1], app.input);
    assert.equal(app.operations[1][1], app.input);
});
