const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/inbox-composer.js'), 'utf8');
function setup() {
    const listeners = new Map(); let now = 1000, forms = [];
    const document = { readyState: 'complete', querySelectorAll: () => forms,
        addEventListener(name, callback) { listeners.set(name, [...(listeners.get(name) || []), callback]); } };
    const context = { document, window: {}, Date: { now: () => now } };
    const emit = (name, event) => (listeners.get(name) || []).forEach(fn => fn(event));
    function form() {
        const result = { isConnected: true, valid: true, submitted: 0, closest() { return this; },
            checkValidity() { return this.valid; }, requestSubmit(button) { assert.equal(button, this.send); this.submitted++; emit('htmx:beforeRequest', { detail: { elt: this } }); },
            querySelector(selector) { return { '[data-inbox-enter-send]': this.checkbox, '[data-inbox-shortcut-hint]': this.hint, '[data-inbox-send-button]': this.send }[selector]; } };
        const child = (selector, fields = {}) => ({ closest: () => result, matches: value => value === selector, ...fields });
        result.text = child('textarea', { value: 'Unsent reply' }); result.checkbox = child('[data-inbox-enter-send]', { checked: false });
        result.hint = { textContent: '' }; result.send = child('button', { disabled: false, getAttribute: () => null }); forms.push(result); return result;
    }
    const initial = form(); vm.runInNewContext(source, context);
    const key = (options = {}, field = initial.text) => { const event = { target: field, key: 'Enter', preventDefault() { this.defaultPrevented = true; }, ...options }; emit('keydown', event); return event; };
    const toggle = checked => { initial.checkbox.checked = checked; emit('change', { target: initial.checkbox }); };
    return { initial, context, emit, key, toggle, tick: value => { now += value; }, replace() { initial.isConnected = false; forms = []; return form(); } };
}
test('default Ctrl+Enter sends through the real form while Enter remains a newline', () => {
    const app = setup(); assert.equal(app.key().defaultPrevented, undefined); assert.equal(app.initial.submitted, 0);
    app.key({ ctrlKey: true }); assert.equal(app.initial.submitted, 1); assert.equal(app.initial.text.value, 'Unsent reply');
});
test('opt-in Enter respects Shift, duplicate sends, and HTMX swaps without changing text', () => {
    const app = setup(); app.toggle(true); app.key({ shiftKey: true }); assert.equal(app.initial.submitted, 0);
    app.key(); app.key(); assert.equal(app.initial.submitted, 1);
    const next = app.replace(); app.emit('htmx:afterSwap', {}); assert.equal(next.checkbox.checked, true);
    app.key({}, app.initial.text); assert.equal(app.initial.submitted, 1); app.key({}, next.text); assert.equal(next.submitted, 1);
});
test('IME composition, Safari229 and immediate composition-end Enter do not submit', () => {
    const app = setup(); app.toggle(true); app.key({ isComposing: true }); app.key({ keyCode: 229 });
    app.emit('compositionstart', { target: app.initial.text }); app.key(); app.emit('compositionend', { target: app.initial.text }); app.key();
    app.tick(99); app.key(); assert.equal(app.initial.submitted, 0); app.tick(2); app.key(); assert.equal(app.initial.submitted, 1);
});
test('blank, invalid, disabled, readonly and repeated keypresses cannot send', () => {
    for (const change of [form => { form.text.value = ' '; }, form => { form.valid = false; }, form => { form.send.disabled = true; }, form => { form.text.readOnly = true; }]) {
        const app = setup(); app.toggle(true); change(app.initial); app.key(); assert.equal(app.initial.submitted, 0);
    }
    const app = setup(); app.toggle(true); for (const options of [{ repeat: true }, { altKey: true }, { metaKey: true }, { defaultPrevented: true }]) app.key(options);
    assert.equal(app.initial.submitted, 0);
});
test('failed saves release the busy fence and installing twice does not duplicate sends', () => {
    const app = setup(); app.toggle(true); vm.runInNewContext(source, app.context);
    app.emit('htmx:beforeRequest', { detail: { elt: app.initial.send } }); app.key(); assert.equal(app.initial.submitted, 0);
    app.emit('htmx:afterRequest', { detail: { elt: app.initial.send } }); app.key(); assert.equal(app.initial.submitted, 1);
});
test('notes and editors outside the primary reply composer keep ordinary Enter behavior', () => {
    const app = setup(); app.toggle(true); const note = { matches: () => true, closest: () => null };
    assert.equal(app.key({}, note).defaultPrevented, undefined); assert.equal(app.initial.submitted, 0);
});
