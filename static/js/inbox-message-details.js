/* Explicit human reading only. Never preload retained text or media. */
(function () {
    'use strict';
    if (window.inboxMessageDetailsInstalled) return;
    window.inboxMessageDetailsInstalled = true;
    let active = null;
    function close() {
        if (!active) return;
        const value = active; active = null;
        if (value.controller) value.controller.abort();
        value.dialog.close(); value.dialog.remove();
        if (value.button.isConnected) value.button.focus();
    }
    function valid(state) {
        return active === state && state.panel.isConnected && state.row.isConnected &&
            document.querySelector('[data-canonical-panel]') === state.panel && document.visibilityState !== 'hidden';
    }
    async function load(state, cursor) {
        if (!valid(state) || state.loading) return;
        state.loading = true; state.more.hidden = true; state.content.replaceChildren(); state.status.textContent = 'Loading…';
        const controller = new AbortController(); state.controller = controller;
        try {
            const url = new URL(state.row.dataset.detailUrl, window.location.href);
            const csrf = state.panel.querySelector('[name="csrfmiddlewaretoken"]');
            if (url.origin !== window.location.origin || !csrf) throw new Error('unavailable');
            const response = await fetch(url.href, {method:'POST', credentials:'same-origin', cache:'no-store', signal:controller.signal,
                headers:{'X-CSRFToken':csrf.value, 'Content-Type':'application/x-www-form-urlencoded', Accept:'application/json'},
                body:new URLSearchParams({kind:state.kind, cursor:cursor || ''}).toString()});
            if (!valid(state)) return;
            if (!response.ok || response.redirected) throw new Error('unavailable');
            const data = await response.json();
            if (!valid(state)) return;
            if (data.source !== 'canonical' || data.id !== state.row.dataset.canonicalMessage ||
                    String(data.conversation_id || '') !== state.panel.dataset.conversationId || data.kind !== state.kind) throw new Error('unavailable');
            if (data.available === false || data.is_expired || (data.is_deleted && state.kind !== 'retained')) throw new Error('unavailable');
            const text = document.createElement('p'); text.style.whiteSpace = 'pre-wrap'; text.textContent = data.body || ''; state.content.appendChild(text);
            (data.items || []).forEach(function (item) {
                const block = document.createElement('p'); block.textContent = item.title || item.type || 'Attachment';
                try {
                    const target = new URL(item.url);
                    if (target.protocol === 'https:' && !target.username && !target.password) {
                        const link = document.createElement('a'); link.href = target.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.referrerPolicy = 'no-referrer'; link.textContent = ' Open attachment link'; block.appendChild(link);
                    }
                } catch (_) { /* Missing link is not a stored media file. */ }
                state.content.appendChild(block);
            });
            state.status.textContent = state.kind === 'retained' && (data.items || []).length ? '附件連結（未保存檔案）' : '';
            state.cursor = data.next_cursor || ''; state.more.hidden = !state.cursor;
        } catch (_) {
            if (valid(state) && !controller.signal.aborted) { state.content.replaceChildren(); state.status.textContent = 'Content unavailable. Close and reopen to try again.'; }
        } finally { state.loading = false; }
    }
    document.addEventListener('click', function (event) {
        const button = event.target.closest('[data-inbox-detail-open]'); if (!button) return;
        const row = button.closest('[data-canonical-message]'), panel = button.closest('[data-canonical-panel]');
        if (!row || !panel) return;
        close();
        const dialog = document.createElement('dialog'); dialog.dataset.inboxDetailView = ''; dialog.className = 'inbox-message-detail-view';
        const title = document.createElement('h2'); title.id = 'inbox-message-detail-title'; title.textContent = button.dataset.inboxDetailOpen === 'retained' ? '已收回 · 內部查看' : 'Message content';
        dialog.setAttribute('aria-labelledby', title.id);
        const done = document.createElement('button'); done.type = 'button'; done.dataset.inboxDetailClose = ''; done.textContent = 'Close'; done.addEventListener('click', close);
        const status = document.createElement('p'); status.setAttribute('role', 'status');
        const content = document.createElement('div'); content.dataset.inboxDetailContent = '';
        const more = document.createElement('button'); more.type = 'button'; more.dataset.inboxDetailMore = ''; more.textContent = 'Next'; more.hidden = true;
        dialog.append(title, done, status, content, more); document.body.appendChild(dialog);
        const state = {dialog, status, content, more, button, row, panel, kind:button.dataset.inboxDetailOpen}; active = state;
        more.addEventListener('click', function () { load(state, state.cursor); }); dialog.addEventListener('cancel', function (event) { event.preventDefault(); close(); });
        dialog.showModal(); load(state);
    });
    window.addEventListener('inbox:selection-approved', close);
    window.addEventListener('inbox-select', close);
    window.addEventListener('pagehide', close);
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') close(); });
    document.addEventListener('htmx:beforeHistorySave', close);
    document.addEventListener('inbox:content-unavailable', close);
    document.addEventListener('htmx:beforeSwap', function (event) { if (active && event.detail.target && event.detail.target.contains(active.row)) close(); });
    document.addEventListener('click', function (event) { if (event.target.closest('[data-canonical-back]')) close(); });
})();
