/* Mixed saved inbox routing; existing detail/composer controllers stay authoritative. */
(function () {
    'use strict';
    if (window.inboxUnifiedInstalled) return;
    window.inboxUnifiedInstalled = true;
    let selected = '';
    const requests = new WeakMap(), listRequests = new WeakMap();
    let listGeneration = 0, wantedList = '';
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
    function listSignature(element, refresh) {
        const form = element.closest('#inbox-filters');
        const url = new URL(form ? form.action : element.getAttribute('href'), window.location.href);
        if (form) url.search = new URLSearchParams(new FormData(form)).toString();
        if (form && !refresh) url.searchParams.delete('cursor');
        if (url.searchParams.has('q')) url.searchParams.set('q', url.searchParams.get('q').trim());
        for (const [key, value] of Array.from(url.searchParams)) {
            if (!value || (key === 'domain' && value === 'all')) url.searchParams.delete(key);
        }
        url.searchParams.sort();
        return url.pathname + url.search;
    }
    document.addEventListener('htmx:confirm', function (event) {
        if (!shell() || !event.detail.target || event.detail.target.id !== 'inbox-list-content') return;
        const element = event.detail.elt, refresh = element.matches('[data-unified-filters]');
        const signature = listSignature(element, refresh);
        // A read acknowledgement may arrive while a newer type or filter is
        // queued. Its old form must never cancel or follow that user choice.
        if (refresh && wantedList && wantedList !== signature) { event.preventDefault(); return; }
        if (!refresh) { wantedList = signature; listGeneration++; }
        else if (!wantedList) wantedList = signature;
    });
    document.addEventListener('htmx:beforeRequest', function (event) {
        if (!shell()) return;
        if (event.detail.target && event.detail.target.id === 'inbox-list-content' && event.detail.xhr) {
            listRequests.set(event.detail.xhr, listGeneration);
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
    document.addEventListener('htmx:afterSwap', highlight);
}());
