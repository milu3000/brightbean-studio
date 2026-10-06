/* Preserve unsaved composer text and keep list state aligned with real requests. */
(function () {
    'use strict';
    if (window.inboxNavigationInstalled) return;
    window.inboxNavigationInstalled = true;

    function hasUnsavedText() {
        const panel = document.querySelector('[data-inbox-panel]');
        if (!panel) return false;
        return Array.from(panel.querySelectorAll('textarea')).some(function (field) {
            return field.value !== field.defaultValue;
        });
    }

    document.addEventListener('htmx:confirm', function (event) {
        const element = event.detail.elt;
        if (!element || !element.matches('[data-inbox-open-message]') || !hasUnsavedText()) return;
        event.preventDefault();
        if (window.confirm('Discard unsaved text and open this message?')) event.detail.issueRequest(true);
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
