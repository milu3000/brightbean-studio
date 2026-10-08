const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/inbox-navigation.js'), 'utf8');
const template = fs.readFileSync(path.join(__dirname, '../templates/inbox/feed.html'), 'utf8');

function controller(search, entries) {
    const script = template.match(/function inboxController\(\) \{[\s\S]*?\n\}<\/script>/)
        || template.match(/function inboxController\(\) \{[\s\S]*?\n\}\n<\/script>/);
    assert.ok(script, 'Inbox controller is present in the rendered template source');
    const context = {
        window: { location: { search } },
        document: { getElementById: () => ({ entries }) },
        URLSearchParams,
        FormData: class { constructor(form) { return form.entries; } }
    };
    vm.runInNewContext(script[0].replace('</script>', '').replace(/\{\{[^}]+\}\}/g, '0'), context);
    return context.inboxController();
}

function setup(fields = [], approved = false, quote = null) {
    const listeners = new Map();
    const dispatched = [];
    let confirmations = 0;
    const context = {
        document: {
            querySelectorAll() { return []; },
            addEventListener(name, callback) {
                listeners.set(name, [...(listeners.get(name) || []), callback]);
            },
            querySelector() { return { querySelectorAll: () => fields, querySelector: selector => selector === '[data-inbox-quote-id]' ? quote : null }; }
        },
        window: {
            addEventListener(name, callback) { listeners.set(name, [...(listeners.get(name) || []), callback]); },
            confirm() { confirmations++; return approved; },
            dispatchEvent(event) { dispatched.push(event); }
        },
        CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } }
    };
    vm.runInNewContext(source, context);
    return {
        context, listeners, dispatched,
        confirmations: () => confirmations,
        emit(name, event) { for (const listener of listeners.get(name) || []) listener(event); }
    };
}

function confirmationEvent(openMessage = true) {
    const event = {
        prevented: false,
        requested: false,
        preventDefault() { this.prevented = true; },
        detail: {
            elt: { matches: () => openMessage },
            issueRequest() { event.requested = true; }
        }
    };
    return event;
}

test('declining navigation preserves unsaved composer text without issuing a request', () => {
    const field = { value: 'Unsent answer', defaultValue: '' };
    const app = setup([field], false);
    const event = confirmationEvent();
    app.emit('htmx:confirm', event);
    assert.equal(event.prevented, true);
    assert.equal(event.requested, false);
    assert.equal(field.value, 'Unsent answer');
});

test('approving a navigation issues exactly the deferred request', () => {
    const app = setup([{ value: 'Updated draft', defaultValue: 'Saved draft' }], true);
    const event = confirmationEvent();
    app.emit('htmx:confirm', event);
    assert.equal(event.requested, true);
    assert.equal(app.confirmations(), 1);
});

test('unchanged saved drafts and timeline pagination do not ask to discard text', () => {
    const app = setup([{ value: 'Saved draft', defaultValue: 'Saved draft' }]);
    const event = confirmationEvent();
    app.emit('htmx:confirm', event);
    assert.equal(event.prevented, false);
    const paged = setup([{ value: 'Unsent text', defaultValue: '' }]);
    paged.emit('htmx:confirm', confirmationEvent(false));
    assert.equal(paged.confirmations(), 0);
});

test('row selection follows requests that actually start, including keyboard navigation', () => {
    const app = setup();
    app.emit('htmx:beforeRequest', { detail: { elt: { dataset: { inboxOpenMessage: 'message-2' } } } });
    assert.equal(app.dispatched.length, 1);
    assert.equal(app.dispatched[0].type, 'inbox-select');
    assert.equal(app.dispatched[0].detail.messageId, 'message-2');
});

test('reloading the script does not duplicate listeners or requests', () => {
    const app = setup();
    vm.runInNewContext(source, app.context);
    app.emit('inbox:refresh', {});
    assert.equal(app.dispatched.length, 1);
});

test('list replacement clears hidden bulk selections; timeline replacement does not', () => {
    const app = setup();
    app.emit('htmx:afterSwap', { detail: { target: { id: 'inbox-thread' } } });
    assert.equal(app.dispatched.length, 0);
    app.emit('htmx:afterSwap', { detail: { target: { id: 'inbox-message-list' } } });
    assert.equal(app.dispatched[0].type, 'inbox-list-changed');
});

test('a read-state refresh preserves the active page when filters are unchanged', () => {
    const app = controller('?q=coffee&page=2', [['view', 'all'], ['q', 'coffee'], ['status', '']]);
    assert.equal(app.listParams(), 'q=coffee&page=2');
});

test('a newer filter takes precedence over stale URL state and resets pagination', () => {
    const app = controller('?q=old&page=3', [['view', 'mine'], ['q', 'new'], ['status', 'open']]);
    assert.equal(app.listParams(), 'q=new&status=open&view=mine');
});


test('quote-only changes guard navigation even when hidden defaultValue changes with value', () => {
    for (const [value, initial] of [['new-target', ''], ['', 'saved-target']]) {
        const app = setup([], false, {value, defaultValue:value, dataset:{inboxQuoteInitial:initial}});
        const event = confirmationEvent(); app.emit('htmx:confirm', event);
        assert.equal(event.prevented, true); assert.equal(event.requested, false);
    }
    const app = setup([], false, {value:'saved-target',dataset:{inboxQuoteInitial:'saved-target'}});
    const event = confirmationEvent(); app.emit('htmx:confirm', event); assert.equal(event.prevented, false);
});


test('full-page account links and HX-Redirect retain the unsaved-draft unload guard', () => {
    const field = {value: 'Unsent account reply', defaultValue: 'Saved earlier draft'};
    const app = setup([field]);
    for (const navigation of ['account link', 'HX-Redirect']) {
        const event = {prevented: false, preventDefault() { this.prevented = true; }};
        app.emit('beforeunload', event);
        assert.equal(event.prevented, true, navigation);
        assert.equal(event.returnValue, '', navigation);
        assert.equal(field.value, 'Unsent account reply');
        assert.equal(field.defaultValue, 'Saved earlier draft');
    }
});

test('source-switch unload is allowed for unchanged saved drafts and held for quote-only edits', () => {
    const saved = setup([{value: 'Saved draft', defaultValue: 'Saved draft'}]);
    const allowed = {prevented: false, preventDefault() { this.prevented = true; }};
    saved.emit('beforeunload', allowed);
    assert.equal(allowed.prevented, false);
    const quote = setup([], false, {value: 'new-target', dataset: {inboxQuoteInitial: ''}});
    const held = {prevented: false, preventDefault() { this.prevented = true; }};
    quote.emit('beforeunload', held);
    assert.equal(held.prevented, true);
});
