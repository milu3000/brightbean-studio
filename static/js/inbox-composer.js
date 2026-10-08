/* Page-local send preference; all submissions still pass through the real form. */
(function () {
    'use strict';
    if (window.inboxComposerInstalled) return;
    window.inboxComposerInstalled = true;
    let enterSends = false;
    const composing = new WeakSet(), composedAt = new WeakMap(), busy = new WeakSet();
    function replyForm(element) {
        const form = element && element.closest('[data-inbox-reply-form]');
        return form && form.isConnected ? form : null;
    }
    function refresh() {
        document.querySelectorAll('[data-inbox-reply-form]').forEach(function (form) {
            const checkbox = form.querySelector('[data-inbox-enter-send]');
            const hint = form.querySelector('[data-inbox-shortcut-hint]');
            if (checkbox) checkbox.checked = enterSends;
            if (hint) hint.textContent = enterSends ? 'Shift+Enter for a new line' : 'Ctrl+Enter to send';
        });
    }
    document.addEventListener('change', function (event) {
        if (!event.target.matches('[data-inbox-enter-send]') || !replyForm(event.target)) return;
        enterSends = event.target.checked; refresh();
    });
    document.addEventListener('compositionstart', function (event) {
        if (replyForm(event.target)) composing.add(event.target);
    });
    document.addEventListener('compositionend', function (event) {
        composing.delete(event.target); composedAt.set(event.target, Date.now());
    });
    document.addEventListener('keydown', function (event) {
        const field = event.target, form = replyForm(field);
        if (!field.matches('textarea') || event.key !== 'Enter' || !form || event.defaultPrevented || event.repeat ||
            event.isComposing || event.keyCode === 229 || composing.has(field) || Date.now() - (composedAt.get(field) || 0) < 100) return;
        if (event.shiftKey || event.altKey || event.metaKey || (enterSends ? event.ctrlKey : !event.ctrlKey)) return;
        event.preventDefault();
        const send = form.querySelector('[data-inbox-send-button]');
        if (!send || send.disabled || send.getAttribute('aria-disabled') === 'true' || busy.has(form) ||
            field.disabled || field.readOnly || !field.value.trim() || !form.checkValidity()) return;
        form.requestSubmit(send);
    });
    document.addEventListener('htmx:beforeRequest', function (event) {
        const form = replyForm(event.detail.elt); if (form) busy.add(form);
    });
    document.addEventListener('htmx:afterRequest', function (event) {
        const form = replyForm(event.detail.elt); if (form) busy.delete(form);
    });
    document.addEventListener('htmx:afterSwap', refresh);
    document.addEventListener('htmx:historyRestore', refresh);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', refresh);
    else refresh();
})();
