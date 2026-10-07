/* Only the saved conversation reader supplies this view. */
(function () {
    'use strict';
    if (window.inboxCanonicalInstalled) return;
    window.inboxCanonicalInstalled = true;
    let active = null, desiredId = '';
    const requests = new WeakMap();
    function current(state) {
        return (!desiredId || desiredId === state.id) && active === state && state.panel.isConnected && document.querySelector('[data-canonical-panel]') === state.panel;
    }
    function visible(state) {
        return current(state) && document.visibilityState !== 'hidden' && state.panel.getClientRects().length > 0;
    }
    function localTimes(root) {
        root.querySelectorAll('time[datetime]').forEach(function (element) {
            const value = element.getAttribute('datetime');
            if (!/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value || '')) return;
            const time = Date.parse(value);
            if (Number.isFinite(time)) { element.textContent = new Date(time).toLocaleString(undefined, { timeZoneName: 'short' }); element.title = element.textContent; }
        });
    }
    function safeUrl(value) {
        if (!value) return '';
        try { const url = new URL(value, window.location.href); return url.origin === window.location.origin ? url.href : ''; }
        catch (_) { return ''; }
    }
    function cancel() {
        if (!active) return;
        active.controllers.forEach(controller => controller.abort());
        active.scroller.removeEventListener('scroll', active.onScroll);
        active.scroller.removeEventListener('wheel', active.onWheel);
        if (active.observer) active.observer.disconnect();
        if (active.poll) window.clearInterval(active.poll);
        active = null;
    }
    function scheduleRead(state, token) {
        if (token) state.readToken = token;
        window.requestAnimationFrame(function () { if (visible(state)) acknowledge(state); });
    }
    async function acknowledge(state) {
        const token = state.readToken, csrf = state.panel.querySelector('[name="csrfmiddlewaretoken"]');
        const url = safeUrl(state.panel.dataset.readAckUrl);
        if (!visible(state) || !token || state.acknowledged === token || state.ackLoading || !csrf || !url) return;
        const controller = new AbortController(), status = state.panel.querySelector('[data-canonical-read-status]');
        const retry = state.panel.querySelector('[data-canonical-read-retry]');
        state.controllers.add(controller); state.ackLoading = true; retry.hidden = true;
        try {
            const response = await fetch(url, { method:'POST', credentials:'same-origin', cache:'no-store', signal:controller.signal,
                headers:{'X-CSRFToken':csrf.value,'Content-Type':'application/x-www-form-urlencoded',Accept:'application/json'}, body:new URLSearchParams({read_ack_token:token}).toString() });
            if (!current(state)) return;
            if (!response.ok || response.redirected) throw new Error('unavailable');
            const result = await response.json();
            if (!current(state)) return;
            if (result.source !== 'canonical' || result.conversation_id !== state.id) throw new Error('wrong_conversation');
            state.acknowledged = token; status.textContent = '';
            if (result.read_state && typeof result.read_state.unread === 'boolean') state.panel.querySelectorAll('[data-canonical-unread]').forEach(node => { node.hidden = !result.read_state.unread; });
            if (Number.isSafeInteger(result.unread_count) && result.unread_count >= 0) document.querySelectorAll('[data-inbox-unread-count]').forEach(node => { node.textContent = String(result.unread_count); node.style.display = result.unread_count ? '' : 'none'; });
            if (result.notifications_marked_read > 0 && window.htmx) window.htmx.trigger(document.body, 'notificationsChanged');
            const filters = document.querySelector('[data-canonical-filters]');
            if (filters && window.htmx) window.htmx.trigger(filters, 'inbox:refresh-list');
        } catch (_) {
            if (current(state) && !controller.signal.aborted) { status.textContent = 'Read status could not be saved.'; retry.hidden = false; }
        } finally {
            state.controllers.delete(controller); state.ackLoading = false;
            if (current(state) && state.readToken !== token) scheduleRead(state);
        }
    }
    function pinned(state) {
        return state.scroller.scrollHeight - state.scroller.clientHeight - state.scroller.scrollTop < 60;
    }
    function anchor(state) {
        if (!state.scroller.getBoundingClientRect) return null;
        const top = state.scroller.getBoundingClientRect().top;
        const row = Array.from(state.page.querySelectorAll('[data-canonical-message]')).find(item => item.getBoundingClientRect().bottom > top + 1);
        return row ? {row, offset:row.getBoundingClientRect().top - top} : null;
    }
    function remember(state) { state.anchor = anchor(state); state.pinned = pinned(state); }
    function adjust(state, saved, fallback) {
        state.adjusting = true;
        if (saved && saved.row.isConnected) state.scroller.scrollTop += saved.row.getBoundingClientRect().top - state.scroller.getBoundingClientRect().top - saved.offset;
        else if (fallback !== undefined) state.scroller.scrollTop = fallback;
        state.lastTop = state.scroller.scrollTop; remember(state);
        window.requestAnimationFrame(function () { state.adjusting = false; });
    }
    function showLatest(state, changed) {
        const button = state.panel.querySelector('[data-canonical-latest]');
        if (button) { button.hidden = false; button.textContent = changed ? 'New activity · Latest' : 'Latest'; }
    }
    function unavailable(state) {
        state.page.replaceChildren(); state.page.dataset.olderUrl = ''; state.page.dataset.undatedUrl = '';
        state.panel.querySelectorAll('[data-canonical-load]').forEach(node => { node.hidden = true; });
        state.panel.querySelectorAll('[data-inbox-reply-form] button').forEach(node => { node.disabled = true; });
        state.panel.querySelector('[data-canonical-history-status]').textContent = 'Conversation unavailable. Reopen the inbox.';
        state.controllers.forEach(pending => pending.abort());
        document.dispatchEvent(new CustomEvent('inbox:content-unavailable'));
    }
    async function fetchPage(state, url, controller) {
        const response = await fetch(url, {credentials:'same-origin', cache:'no-store', signal:controller.signal});
        if (!current(state)) return null;
        if ([401,403,404].includes(response.status) || response.redirected) { unavailable(state); return null; }
        if (!response.ok) throw new Error(response.status === 409 ? 'stale' : 'unavailable');
        const html = await response.text(); if (!current(state)) return null;
        const parsed = new DOMParser().parseFromString(html, 'text/html');
        const page = parsed.querySelector('[data-canonical-page]');
        if (!page || page.dataset.conversationId !== state.id) throw new Error('invalid');
        if (state.page.dataset.viewScope && page.dataset.viewScope !== state.page.dataset.viewScope) { unavailable(state); return null; }
        return {page, header:parsed.querySelector('[data-canonical-fresh-header]')};
    }
    function syncButtons(state) {
        ['dated','undated'].forEach(lane => {
            const button = state.panel.querySelector('[data-canonical-load="' + lane + '"]');
            if (button) button.hidden = !state.page.dataset[lane === 'dated' ? 'olderUrl' : 'undatedUrl'];
        });
    }
    async function loadOlder(state, lane) {
        if (!current(state) || state.loading.has(lane) || state.loading.has('head')) return;
        const key = lane === 'undated' ? 'undatedUrl' : 'olderUrl', url = safeUrl(state.page.dataset[key]);
        const button = state.panel.querySelector('[data-canonical-load="' + lane + '"]');
        const status = state.panel.querySelector('[data-canonical-history-status]');
        if (!url || !button) return;
        const controller = new AbortController(); state.controllers.add(controller); state.loading.add(lane); button.disabled = true;
        status.textContent = 'Loading earlier messages…';
        try {
            const result = await fetchPage(state, url, controller); if (!result || !current(state)) return;
            const page = result.page, selector = lane === 'undated' ? '[data-canonical-undated]' : '[data-canonical-dated]';
            const source = page.querySelector(selector), target = state.page.querySelector(selector);
            if (!source || !target) throw new Error('invalid');
            const ids = new Set(Array.from(state.page.querySelectorAll('[data-canonical-message]'), row => row.dataset.canonicalMessage));
            const rows = Array.from(source.querySelectorAll('[data-canonical-message]')).filter(row => !ids.has(row.dataset.canonicalMessage));
            if (!rows.length && safeUrl(page.dataset[key]) === url) throw new Error('invalid');
            const saved = anchor(state), top = state.scroller.scrollTop, height = state.scroller.scrollHeight, first = target.firstChild;
            rows.forEach(function (row) { const copy = document.importNode(row, true); if (lane === 'dated') target.insertBefore(copy, first); else target.appendChild(copy); if (window.htmx) window.htmx.process(copy); });
            const fallback = lane === 'dated' ? top + state.scroller.scrollHeight - height : top;
            const displayed = Array.from(target.querySelectorAll('[data-canonical-message]')), maximum = lane === 'dated' ? 450 : 50;
            if (displayed.length > maximum) {
                const removed = lane === 'dated' ? displayed.slice(maximum) : displayed.slice(0, displayed.length - maximum);
                removed.forEach(row => row.remove()); state.trimmed = true; showLatest(state, false);
            }
            localTimes(target); document.dispatchEvent(new CustomEvent('inbox:history-loaded'));
            state.page.dataset[key] = safeUrl(page.dataset[key]); syncButtons(state); status.textContent = '';
            adjust(state, saved, fallback); state.readingOlder = true;
            scheduleRead(state, page.dataset.readAckToken);
        } catch (error) {
            if (current(state) && !controller.signal.aborted) {
                if (['stale','invalid'].includes(error.message)) { state.page.dataset[key] = ''; button.hidden = true; showLatest(state, false); }
                status.textContent = error.message === 'stale' ? 'Messages changed. Load Latest to continue.' : 'Earlier messages could not be loaded. Try again.';
            }
        } finally { state.controllers.delete(controller); state.loading.delete(lane); if (current(state)) button.disabled = false; }
    }
    function updateComposer(state, page) {
        window.requestAnimationFrame(function () {
            if (!visible(state) || state.page !== page) return;
            const form = state.panel.querySelector('[data-inbox-reply-form]'); if (!form) return;
            const revision = form.querySelector('[name="composer_revision"]');
            const send = form.querySelector('[data-inbox-send-button]');
            if (!revision || !send) return;
            let reason = page.dataset.sendReason || '';
            const matching = revision.value === page.dataset.composerRevision;
            if (matching) {
                state.panel.querySelectorAll('[name="composer_observation_token"]').forEach(field => { field.value = page.dataset.composerObservationToken || ''; });
                state.panel.querySelectorAll('[name="composer_scope_token"]').forEach(field => { field.value = page.dataset.composerScopeToken || ''; });
                state.panel.querySelectorAll('[data-inbox-needs-latest]').forEach(node => { node.remove(); });
            } else reason = 'The saved draft changed. Reopen this conversation to review it.';
            send.disabled = !matching || page.dataset.sendAllowed !== 'true';
            send.setAttribute('aria-disabled', send.disabled ? 'true' : 'false');
            let hold = state.panel.querySelector('#reply-hold-reason');
            if (!hold) { hold = document.createElement('p'); hold.id = 'reply-hold-reason'; hold.setAttribute('role','status'); form.before(hold); }
            hold.textContent = send.disabled ? reason : ''; hold.hidden = !send.disabled;
            if (send.disabled) send.setAttribute('aria-describedby','reply-hold-reason'); else send.removeAttribute('aria-describedby');
        });
    }
    async function refresh(state, latest) {
        if (!visible(state) || state.loading.size || document.querySelector('[data-inbox-detail-view]') || state.panel.querySelector('.htmx-request')) return;
        let url = safeUrl(state.page.dataset.latestUrl); if (!url) return;
        const adoption = state.panel.querySelector('[name="adopt_reply_id"]');
        if (adoption && adoption.value) { const selected = new URL(url); selected.searchParams.set('adopt_reply_id', adoption.value); url = selected.href; }
        const controller = new AbortController(); state.controllers.add(controller); state.loading.add('head');
        const status = state.panel.querySelector('[data-canonical-history-status]');
        try {
            const result = await fetchPage(state, url, controller); if (!result || !current(state)) return;
            const page = result.page;
            if (state.panel.querySelector('.htmx-request')) { showLatest(state, true); return; }
            const changed = page.dataset.revision !== state.page.dataset.revision || page.dataset.composerRefreshKey !== state.page.dataset.composerRefreshKey;
            if (!latest && !changed) return;
            if (!latest && (state.readingOlder || state.trimmed || !pinned(state))) { showLatest(state, true); return; }
            // A fresh head is applied only here. Background checks never advance read/send observation tokens.
            const replacement = document.importNode(page, true); state.page.replaceWith(replacement); state.page = replacement;
            const header = result.header && result.header.content && result.header.content.firstElementChild;
            const oldHeader = state.panel.querySelector('#inbox-canonical-header');
            if (header && oldHeader) { const copy = document.importNode(header, true); oldHeader.replaceWith(copy); if (window.htmx) window.htmx.process(copy); }
            if (window.htmx) window.htmx.process(replacement);
            updateComposer(state, replacement);
            state.trimmed = false; state.readingOlder = false; syncButtons(state); status.textContent = '';
            const button = state.panel.querySelector('[data-canonical-latest]'); if (button) button.hidden = true;
            localTimes(replacement); document.dispatchEvent(new CustomEvent('inbox:history-loaded'));
            adjust(state, null, state.scroller.scrollHeight); scheduleRead(state, page.dataset.readAckToken);
            if (state.observer) { state.observer.disconnect(); state.observer.observe(state.page); state.observer.observe(state.scroller); }
        } catch (_) { if (current(state) && !controller.signal.aborted) status.textContent = 'Messages could not be refreshed. Try again.'; }
        finally { state.controllers.delete(controller); state.loading.delete('head'); }
    }
    function initialize() {
        localTimes(document);
        const panel = document.querySelector('[data-canonical-panel]');
        if (active && active.panel === panel) return;
        cancel(); if (!panel || (desiredId && panel.dataset.conversationId !== desiredId)) return;
        desiredId = panel.dataset.conversationId;
        const page = panel.querySelector('[data-canonical-page]'), scroller = panel.querySelector('[data-canonical-scroll]');
        if (!page || !scroller || page.dataset.conversationId !== panel.dataset.conversationId) return;
        const state = { panel, page, scroller, id:panel.dataset.conversationId, controllers:new Set(), loading:new Set() }; active = state;
        scroller.scrollTop = scroller.scrollHeight; state.lastTop = scroller.scrollTop;
        state.onScroll = function () { const top = scroller.scrollTop; if (!state.adjusting && current(state) && top < state.lastTop && top <= 120) loadOlder(state, 'dated'); state.lastTop = top; if (!state.adjusting) { remember(state); if (!state.trimmed && pinned(state)) state.readingOlder = false; } };
        state.onWheel = function (event) { if (current(state) && event.deltaY < 0 && scroller.scrollTop <= 0) loadOlder(state, 'dated'); };
        scroller.addEventListener('scroll', state.onScroll, {passive:true}); scroller.addEventListener('wheel', state.onWheel, {passive:true});
        remember(state);
        if (window.ResizeObserver) {
            state.observer = new window.ResizeObserver(function () {
                if (!current(state) || state.adjusting) return;
                if (state.pinned && !state.trimmed && !state.readingOlder) adjust(state, null, scroller.scrollHeight);
                else adjust(state, state.anchor);
            });
            state.observer.observe(state.page); state.observer.observe(scroller);
        }
        if (window.setInterval) state.poll = window.setInterval(function () { refresh(state, false); }, 15000);
        scheduleRead(state, page.dataset.readAckToken);
    }
    document.addEventListener('click', function (event) {
        const load = event.target.closest('[data-canonical-load]'); if (load && active && active.panel.contains(load)) loadOlder(active, load.dataset.canonicalLoad);
        const latest = event.target.closest('[data-canonical-latest]'); if (latest && active && active.panel.contains(latest)) refresh(active, true);
        const check = event.target.closest('[data-canonical-refresh]'); if (check && active && active.panel.contains(check)) refresh(active, false);
        const back = event.target.closest('[data-canonical-back]'), shell = back && back.closest('[data-canonical-shell]');
        if (shell) { event.preventDefault(); shell.dataset.activePanel = 'list'; }
        const retry = event.target.closest('[data-canonical-read-retry]'); if (retry && active && active.panel.contains(retry)) scheduleRead(active);
    });
    window.addEventListener('inbox:selection-approved', function (event) { desiredId = event.detail.messageId; cancel(); });
    document.addEventListener('htmx:beforeRequest', function (event) {
        const element = event.detail.elt;
        const owner = element && element.closest && element.closest('[data-canonical-panel]');
        const id = element && element.dataset.inboxOpenMessage || (owner && owner.dataset.conversationId);
        if (id && event.detail.xhr) requests.set(event.detail.xhr, id);
        if (element && element.dataset.inboxOpenMessage) { if (!desiredId) desiredId = element.dataset.inboxOpenMessage; cancel(); }
    });
    document.addEventListener('htmx:beforeSwap', function (event) {
        const id = event.detail.xhr && requests.get(event.detail.xhr);
        if (id && desiredId && id !== desiredId) { event.detail.shouldSwap = false; event.preventDefault(); }
    });
    document.addEventListener('htmx:afterRequest', initialize);
    document.addEventListener('htmx:afterSwap', function () {
        const cursor = document.querySelector('[data-canonical-list-cursor]'); if (cursor) cursor.value = new URL(window.location.href).searchParams.get('cursor') || '';
        initialize();
        if (active && active.panel.querySelector('[data-inbox-needs-latest]')) showLatest(active, false);
    });
    document.addEventListener('htmx:historyRestore', initialize);
    document.addEventListener('visibilitychange', function () { if (active) scheduleRead(active); });
    window.addEventListener('inbox-select', function () { const shell = document.querySelector('[data-canonical-shell]'); if (shell) shell.dataset.activePanel = 'detail'; });
    window.addEventListener('inbox-refresh', function () { const form = document.querySelector('[data-canonical-filters]'); if (form && window.htmx) window.htmx.trigger(form, 'inbox:refresh-list'); });
    window.addEventListener('pagehide', cancel);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize); else initialize();
})();
