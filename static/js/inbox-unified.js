/* Mixed saved inbox routing; existing detail/composer controllers stay authoritative. */
(function () {
    'use strict';
    if (window.inboxUnifiedInstalled) return;
    window.inboxUnifiedInstalled = true;
    let selected = '';
    const requests = new WeakMap(), listRequests = new WeakMap(), listIntents = new WeakMap();
    const listRefreshes = new WeakSet();
    let listGeneration = 0, wantedList = '', pendingHistory = null;
    function shell() { return document.querySelector('[data-unified-shell]'); }
    function highlight() {
        if (!shell()) return;
        document.querySelectorAll('[data-unified-row]').forEach(function (row) {
            if (row.dataset.inboxOpenMessage === selected) row.setAttribute('aria-current', 'true');
            else row.removeAttribute('aria-current');
        });
    }
    window.addEventListener('inbox:selection-approved', function (event) {
        if (!shell()) return;
        selected = event.detail.messageId;
        highlight();
    });
    document.addEventListener('click', function (event) {
        const back = event.target.closest('[data-inbox-back]');
        const root = back && back.closest('[data-unified-shell]');
        if (root) { event.preventDefault(); root.dataset.activePanel = 'list'; }
    });
    document.addEventListener('change', function (event) {
        const element = event.target;
        if (!element.matches('[data-inbox-account], [data-inbox-platform]')) return;
        const form = element.closest('[data-unified-filters]');
        if (!form) return;
        // Status belongs to the selected source; changing account/platform can
        // switch canonical workflow to legacy status or back again.
        form.querySelectorAll('[name="status"], [name="workflow"], [name="view"]').forEach(field => { field.value = ''; });
    }, true);
    function normalizedListUrl(url) {
        if (url.searchParams.has('q')) url.searchParams.set('q', url.searchParams.get('q').trim());
        for (const [key, value] of Array.from(url.searchParams)) {
            if (!value || ((key === 'domain' || key === 'view') && value === 'all')) url.searchParams.delete(key);
        }
        url.searchParams.sort();
        return url.pathname + url.search;
    }
    function listSignature(element, refresh) {
        const form = element.closest('#inbox-filters');
        const url = new URL(form ? form.action : element.getAttribute('href'), window.location.href);
        if (form) url.search = new URLSearchParams(new FormData(form)).toString();
        if (form && !refresh) url.searchParams.delete('cursor');
        return normalizedListUrl(url);
    }
    function cancelHistory() {
        const pending = pendingHistory;
        pendingHistory = null;
        if (!pending) return;
        // HTMX's history loader bypasses beforeRequest/beforeSwap and swaps
        // directly in onload. Cancel the XHR and detach that callback.
        pending.xhr.onload = null;
        pending.xhr.abort();
    }
    function beginHistory(path) {
        cancelHistory();
        listGeneration++;
        wantedList = normalizedListUrl(new URL(path, window.location.href));
    }
    function trackHistory(xhr, path, normal) {
        const pending = {xhr, path, normal};
        pendingHistory = pending;
        xhr.addEventListener('loadend', function () {
            // Successful historyRestore already clears this record. Aborted
            // older requests must never release or replace a newer restore.
            if (pendingHistory !== pending || !shell()) return;
            pendingHistory = null;
            listGeneration++;
            const form = document.querySelector('[data-unified-filters]');
            wantedList = form ? listSignature(form, true) : '';
            if (wantedList) window.history.replaceState(window.history.state, '', wantedList);
            window.dispatchEvent(new CustomEvent('htmx-error', {detail:{
                message:'That inbox view could not be restored. Your previous filters and unsaved reply are still here.'
            }}));
        }, {once:true});
    }
    function cachedWorkspaceInbox(url) {
        try {
            const entries = JSON.parse(window.localStorage.getItem('htmx-history-cache') || '[]');
            return Array.isArray(entries) && entries.some(function (entry) {
                if (!entry || typeof entry.url !== 'string') return false;
                try {
                    const saved = new URL(entry.url, window.location.href);
                    return saved.origin === url.origin && saved.pathname === url.pathname;
                } catch (_) { return false; }
            });
        } catch (_) { return false; }
    }
    window.addEventListener('popstate', function (event) {
        const root = shell(), form = document.querySelector('[data-unified-filters]');
        if (!root || !form) return;
        const url = new URL(window.location.href), feed = new URL(form.action, window.location.href);
        if (url.origin !== feed.origin || url.pathname !== feed.pathname) return;
        beginHistory(url.href);
        if (!event.state || !event.state.htmx || !cachedWorkspaceInbox(url)) return;
        // Pre-upgrade snapshots serialized the whole body, sometimes including
        // unsaved validation text. Never delete or rewrite those cache bytes.
        // Bypass only this workspace feed's cache hit using public HTMX APIs.
        event.stopImmediatePropagation();
        document.dispatchEvent(new CustomEvent('inbox:history-navigation'));
        const source = document.createElement('a');
        source.href = url.href; source.hidden = true;
        source.dataset.unifiedHistoryRestore = url.pathname + url.search;
        source.setAttribute('data-no-error-toast', '');
        root.appendChild(source);
        window.htmx.ajax('GET', url.href, {
            source, target:'#inbox-list-content', swap:'innerHTML settle:0ms', select:'#inbox-list-content > *',
            headers:{'HX-History-Restore-Request':'true'}
        }).catch(function () {}).finally(function () { source.remove(); });
    }, true);
    document.addEventListener('htmx:historyCacheMiss', function (event) {
        if (!shell()) return;
        beginHistory(event.detail.path);
        trackHistory(event.detail.xhr, event.detail.path, false);
    });
    document.addEventListener('htmx:confirm', function (event) {
        if (!shell() || !event.detail.target || event.detail.target.id !== 'inbox-list-content') return;
        const element = event.detail.elt, refresh = element.matches('[data-unified-filters]');
        const signature = listSignature(element, refresh);
        // A read acknowledgement must not supersede an ongoing restore or a
        // newer user choice with the still-visible, old form's filter values.
        if (refresh && (pendingHistory || (wantedList && wantedList !== signature))) { event.preventDefault(); return; }
        if (!refresh) {
            const trigger = event.detail.triggeringEvent;
            const generation = trigger ? listIntents.get(trigger) : undefined;
            // HTMX invokes confirm again when draining queue-last. That is the
            // original user intent, not a new choice after browser navigation.
            if (generation !== undefined) {
                if (generation !== listGeneration) event.preventDefault();
                return;
            }
            cancelHistory();
            wantedList = signature;
            listGeneration++;
            if (trigger) listIntents.set(trigger, listGeneration);
        } else if (!wantedList) wantedList = signature;
    });
    document.addEventListener('htmx:beforeRequest', function (event) {
        if (!shell()) return;
        if (event.detail.target && event.detail.target.id === 'inbox-list-content' && event.detail.xhr) {
            listRequests.set(event.detail.xhr, listGeneration);
            if (event.detail.elt && event.detail.elt.matches('[data-unified-filters]')) listRefreshes.add(event.detail.xhr);
            const restore = event.detail.elt && event.detail.elt.dataset.unifiedHistoryRestore;
            if (restore) trackHistory(event.detail.xhr, restore, true);
        }
        const element = event.detail.elt;
        const panel = element && element.closest('[data-inbox-panel]');
        const id = element && element.dataset.inboxOpenMessage ||
            (panel && (panel.dataset.conversationId || panel.dataset.selectedMessageId));
        // Also protect legacy status/draft/reply responses when another type
        // has been selected. Canonical requests keep their existing guard.
        if (id && event.detail.xhr) requests.set(event.detail.xhr, id);
    });
    document.addEventListener('htmx:beforeSwap', function (event) {
        const generation = event.detail.xhr && listRequests.get(event.detail.xhr);
        if (generation !== undefined && generation !== listGeneration) {
            event.detail.shouldSwap = false; event.preventDefault(); return;
        }
        const id = event.detail.xhr && requests.get(event.detail.xhr);
        if (id && selected && id !== selected) { event.detail.shouldSwap = false; event.preventDefault(); }
    });
    document.addEventListener('htmx:afterSwap', function (event) {
        highlight();
        const xhr = event.detail && event.detail.xhr;
        if (xhr && listRefreshes.has(xhr) && listRequests.get(xhr) === listGeneration &&
                event.detail.target && event.detail.target.id === 'inbox-list-content') {
            // Only an accepted refresh can rebase its old-page intent. HTMX has
            // replaced the URL and list; failures retain the retryable old scope.
            const form = document.querySelector('[data-unified-filters]');
            wantedList = form ? listSignature(form, true) : '';
        }
        if (pendingHistory && pendingHistory.normal && event.detail.xhr === pendingHistory.xhr) {
            document.dispatchEvent(new CustomEvent('htmx:historyRestore', {bubbles:true, detail:{
                path:pendingHistory.path, cacheMiss:true
            }}));
        }
    });
    document.addEventListener('htmx:historyRestore', function (event) {
        if (!shell()) return;
        if (pendingHistory && event.detail.path === pendingHistory.path) pendingHistory = null;
        // History restores the list pane only. Keep the current detail/draft,
        // but invalidate requests started for the previously visible filters.
        listGeneration++;
        const form = document.querySelector('[data-unified-filters]');
        wantedList = form ? listSignature(form, true) : '';
        highlight();
    });
    window.addEventListener('pagehide', cancelHistory);
}());
