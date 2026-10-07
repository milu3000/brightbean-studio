/* Preserve unsaved composer text and keep list state aligned with real requests. */
(function () {
    'use strict';
    if (window.inboxNavigationInstalled) return;
    window.inboxNavigationInstalled = true;

    function localTimes() {
        document.querySelectorAll('[data-inbox-panel] time[datetime], #inbox-message-list time[datetime]').forEach(function (element) {
            const value = element.getAttribute('datetime');
            if (!/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value || '')) return;
            const time = Date.parse(value);
            if (Number.isFinite(time)) { element.textContent = new Date(time).toLocaleString(undefined, {timeZoneName:'short'}); element.title = element.textContent; }
        });
    }
    document.addEventListener('htmx:afterSwap', localTimes);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', localTimes); else localTimes();

    function hasUnsavedText() {
        const panel = document.querySelector('[data-inbox-panel]');
        if (!panel) return false;
        if (panel.querySelector('[data-inbox-unsaved-text]')) return true;
        const quote = panel.querySelector('[data-inbox-quote-id]');
        if (quote && quote.value !== quote.dataset.inboxQuoteInitial) return true;
        return Array.from(panel.querySelectorAll('textarea')).some(function (field) {
            return field.value !== field.defaultValue;
        });
    }

    document.addEventListener('htmx:confirm', function (event) {
        const element = event.detail.elt;
        if (!element || !element.matches('[data-inbox-open-message], [data-inbox-leave-composer]')) return;
        function approved() { if (element.dataset && element.dataset.inboxOpenMessage) window.dispatchEvent(new CustomEvent('inbox:selection-approved', {detail:{messageId:element.dataset.inboxOpenMessage}})); }
        if (!hasUnsavedText()) { if (!event.defaultPrevented) approved(); return; }
        event.preventDefault();
        if (window.confirm('Discard unsaved changes and continue?')) { approved(); event.detail.issueRequest(true); }
    });

    window.addEventListener('beforeunload', function (event) {
        if (!hasUnsavedText()) return;
        event.preventDefault(); event.returnValue = '';
    });

    document.addEventListener('htmx:beforeRequest', function (event) {
        const element = event.detail.elt;
        if (element && element.dataset.inboxOpenMessage) {
            window.dispatchEvent(new CustomEvent('inbox-select', {
                detail: { messageId: element.dataset.inboxOpenMessage }
            }));
        }
    });

    document.addEventListener('inbox:refresh', function () {
        window.dispatchEvent(new CustomEvent('inbox-refresh'));
    });

    document.addEventListener('htmx:afterSwap', function (event) {
        if (event.detail.target && event.detail.target.id === 'inbox-message-list') {
            // A filtered or paginated list must not retain hidden bulk selections.
            window.dispatchEvent(new CustomEvent('inbox-list-changed'));
        }
    });

    function showRequestError(event) {
        const element = event.detail.elt;
        const panel = document.querySelector('[data-inbox-panel]');
        if (!panel || !element || !panel.contains(element)) return;
        let warning = panel.querySelector('[data-inbox-request-error]');
        if (!warning) {
            warning = document.createElement('p');
            warning.dataset.inboxRequestError = '';
            warning.setAttribute('role', 'alert');
            warning.className = 'px-5 py-3 text-sm text-red-700';
            panel.prepend(warning);
        }
        warning.textContent = 'The request could not be completed. Your text is still here. Refresh to check the reply status before sending again.';
    }
    document.addEventListener('htmx:responseError', showRequestError);
    document.addEventListener('htmx:sendError', showRequestError);
})();
