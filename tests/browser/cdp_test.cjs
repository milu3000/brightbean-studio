/* Unit coverage only: no real browser, subprocess, socket, or UI assertion. */
'use strict';
const assert = require('node:assert/strict');
const {test} = require('node:test');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {withCleanup} = require('./cdp.cjs');

function fixture() {
    const child = new EventEmitter();
    child.stderr = new PassThrough();
    child.stdio = [null, null, child.stderr, new PassThrough(), new PassThrough()];
    child.kills = 0;
    child.kill = () => { child.kills++; child.emit('exit', null, 'SIGKILL'); child.emit('close', null, 'SIGKILL'); return true; };
    const writes = [], timers = new Set(), launches = [];
    child.stdio[3].on('data', bytes => writes.push(bytes.toString()));
    const module = {exports: {}};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'cdp.cjs'), 'utf8'), {
        module,
        require(name) {
            assert.equal(name, 'node:child_process');
            return {spawn: (...args) => { launches.push(args); return child; }};
        },
        setTimeout(callback, ms) { const timer = {callback, ms}; timers.add(timer); return timer; },
        clearTimeout(timer) { timers.delete(timer); }
    });
    const browser = new module.exports.Browser('/synthetic/chrome', '/synthetic/profile');
    const reply = message => child.stdio[4].write(JSON.stringify(message) + '\0');
    return {browser, child, writes, timers, launches, reply};
}

test('CDP uses fd 3 requests, fd 4 responses, and a real NUL terminator', async () => {
    const f = fixture(), pending = f.browser.command('Browser.getVersion', {}, undefined, 30000);
    assert.equal(JSON.stringify(f.launches[0][2].stdio), JSON.stringify(['ignore','ignore','pipe','pipe','pipe']));
    assert.equal(f.writes.length, 1);
    assert.equal(f.writes[0].charCodeAt(f.writes[0].length - 1), 0);
    const sent = JSON.parse(f.writes[0].slice(0, -1));
    assert.equal(sent.method, 'Browser.getVersion');
    f.reply({id: sent.id, result: {product: 'Chrome/SYNTHETIC'}});
    assert.equal((await pending).product, 'Chrome/SYNTHETIC');
    assert.equal(f.timers.size, 0);
});

test('cold startup has 30 seconds while ordinary CDP commands retain 8 seconds', async () => {
    const f = fixture();
    const startup = f.browser.command('Browser.getVersion', {}, undefined, 30000);
    assert.equal([...f.timers][0].ms, 30000);
    f.reply({id: 1, result: {product: 'Chrome/SYNTHETIC'}});
    await startup;
    const command = f.browser.command('Runtime.enable', {}, 'session-1');
    assert.equal([...f.timers][0].ms, 8000);
    assert.equal(JSON.parse(f.writes[1].slice(0, -1)).sessionId, 'session-1');
    f.reply({id: 2, result: {}});
    await command;
});

test('fragmented and coalesced response frames resolve the matching commands', async () => {
    const f = fixture(), one = f.browser.command('One'), two = f.browser.command('Two');
    const frames = JSON.stringify({id: 2, result: {value: 'two'}}) + '\0' + JSON.stringify({id: 1, result: {value: 'one'}}) + '\0';
    f.child.stdio[4].write(frames.slice(0, 7));
    assert.equal(f.browser.pending.size, 2);
    f.child.stdio[4].write(frames.slice(7));
    assert.equal((await one).value, 'one');
    assert.equal((await two).value, 'two');
    assert.equal(f.timers.size, 0);
});

test('startup deadline fails and a late reply cannot convert it into success', async () => {
    const f = fixture(), pending = f.browser.command('Browser.getVersion', {}, undefined, 30000);
    const rejected = assert.rejects(pending, /CDP timeout: Browser.getVersion/);
    [...f.timers][0].callback();
    await rejected;
    assert.equal(f.browser.pending.size, 0);
    f.reply({id: 1, result: {product: 'Chrome/LATE'}});
    assert.equal(f.browser.pending.size, 0);
});

test('early process exit rejects pending startup instead of waiting or passing', async () => {
    const f = fixture(), pending = f.browser.command('Browser.getVersion', {}, undefined, 30000);
    const rejected = assert.rejects(pending, /Chrome exited/);
    f.child.emit('exit', 1, null);
    await rejected;
    assert.equal(f.browser.closed, true);
    assert.equal(f.timers.size, 0);
});

test('spawn failures and CDP protocol errors remain failures', async () => {
    const f = fixture(), pending = f.browser.command('Browser.getVersion', {}, undefined, 30000);
    const rejected = assert.rejects(pending, /synthetic spawn failure/);
    f.child.emit('error', new Error('synthetic spawn failure'));
    await rejected;
    const g = fixture(), command = g.browser.command('Missing.method');
    const protocolError = assert.rejects(command, /method not found/);
    g.reply({id: 1, error: {code: -32601, message: 'method not found'}});
    await protocolError;
    assert.equal(g.timers.size, 0);
});

test('cleanup cannot hide the original scenario failure', async () => {
    const original = new Error('original synthetic assertion');
    const cleanup = new Error('synthetic ENOTEMPTY');
    await assert.rejects(withCleanup(async () => { throw original; }, async () => { throw cleanup; }), error => {
        assert(error instanceof AggregateError);
        assert.equal(error.errors[0], original);
        assert.equal(error.errors[1], cleanup);
        assert.match(error.message, /original synthetic assertion/);
        assert.match(error.message, /synthetic ENOTEMPTY/);
        return true;
    });
});

test('cleanup still fails after a successful operation, and successful cleanup preserves results', async () => {
    await assert.rejects(withCleanup(async () => 1, async () => { throw new Error('cleanup failed'); }), /cleanup failed/);
    let cleaned = false;
    assert.equal(await withCleanup(async () => 42, async () => { cleaned = true; }), 42);
    assert.equal(cleaned, true);
    const original = new Error('primary failure');
    await assert.rejects(withCleanup(async () => { throw original; }, async () => {}), error => error === original);
});

test('graceful browser shutdown waits for stdio close rather than process exit', async () => {
    const f = fixture();
    let settled = false;
    const closing = f.browser.close().then(() => { settled = true; });
    assert.equal(JSON.parse(f.writes[0].slice(0, -1)).method, 'Browser.close');
    f.reply({id: 1, result: {}});
    f.child.emit('exit', 0, null);
    await Promise.resolve(); await Promise.resolve();
    assert.equal(settled, false, 'Profile cleanup must wait for inherited streams to close');
    f.child.emit('close', 0, null);
    await closing;
    assert.equal(f.child.kills, 0);
    assert.equal(f.timers.size, 0);
});

test('browser shutdown accepts exit before its response, but a deadline remains failure', async () => {
    const f = fixture(), closing = f.browser.close();
    f.child.emit('exit', 0, null); f.child.emit('close', 0, null);
    await closing;
    assert.equal(f.timers.size, 0);
    const g = fixture(), timed = g.browser.close();
    const failure = assert.rejects(timed, /Chromium shutdown timed out/);
    [...g.timers][0].callback();
    await failure;
    assert.equal(g.child.kills, 1);
});
