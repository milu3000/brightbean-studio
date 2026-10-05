const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../static/js/inbox-media.js"), "utf8");
function context(images = []) {
    const listeners = new Map();
    const document = {
        querySelectorAll: () => images,
        addEventListener: (name, handler) => {
            const values = listeners.get(name) || [];
            values.push(handler);
            listeners.set(name, values);
        },
    };
    const sandbox = { window: {}, document, fetch: () => { throw Error("No media fetch allowed"); } };
    vm.createContext(sandbox);
    return { sandbox, images, listeners, run: () => vm.runInContext(source, sandbox) };
}
function image(complete = true, width = 0) {
    const note = { hidden: true };
    const item = {
        hidden: false, complete, naturalWidth: width,
        matches: (selector) => selector === "img[data-inbox-preview]",
        closest: () => ({ querySelector: () => note }),
    };
    return { item, note };
}
test("already failed preview becomes a truthful fallback", () => {
    const { item, note } = image(); const ctx = context([item]); ctx.run();
    assert.equal(item.hidden, true); assert.equal(note.hidden, false);
});
test("future errors and successful loads update only local display", () => {
    const { item, note } = image(false); const ctx = context([item]); ctx.run();
    ctx.listeners.get("error")[0]({ target: item });
    assert.equal(item.hidden, true); assert.equal(note.hidden, false);
    ctx.listeners.get("load")[0]({ target: item });
    assert.equal(item.hidden, false); assert.equal(note.hidden, true);
});
test("HTMX replacement is inspected without new listeners", () => {
    const ctx = context(); ctx.run(); const { item, note } = image(); ctx.images.push(item);
    ctx.listeners.get("htmx:afterSwap")[0](); ctx.run();
    assert.equal(item.hidden, true); assert.equal(note.hidden, false);
    assert.equal(ctx.listeners.get("error").length, 1);
});
test("unrelated resource errors are ignored", () => {
    const ctx = context(); ctx.run(); const unrelated = { matches: () => false, hidden: false };
    ctx.listeners.get("error")[0]({ target: unrelated });
    assert.equal(unrelated.hidden, false);
});
test("successful cached image remains visible", () => {
    const { item, note } = image(true, 240); const ctx = context([item]); ctx.run();
    assert.equal(item.hidden, false); assert.equal(note.hidden, true);
});
