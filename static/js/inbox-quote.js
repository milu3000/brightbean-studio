/* Explicit native quote selection stays inside the current conversation form. */
(function () {
    'use strict';
    if (window.inboxQuoteInstalled) return;
    window.inboxQuoteInstalled = true;
    const busy = new WeakSet();
    function currentForm(panel) {
        if (!panel || !panel.isConnected || document.querySelector('[data-canonical-panel]') !== panel) return null;
        const form = panel.querySelector('[data-inbox-reply-form]');
        return form && form.isConnected ? form : null;
    }
    function editable(form) { return form && form.dataset.inboxQuoteSupported === 'true' && !busy.has(form); }
    function refresh() {
        const panel = document.querySelector('[data-canonical-panel]'), form = currentForm(panel);
        if (!panel) return;
        const field = form && form.querySelector('[data-inbox-quote-id]');
        panel.querySelectorAll('[data-inbox-quote-target]').forEach(function (button) {
            button.hidden = !form || form.dataset.inboxQuoteSupported !== 'true';
            button.disabled = !editable(form);
            button.setAttribute('aria-pressed', String(Boolean(field && field.value === button.dataset.inboxQuoteTarget)));
        });
        const cancel = form && form.querySelector('[data-inbox-quote-cancel]');
        if (cancel) cancel.disabled = !editable(form);
    }
    function setQuote(form, identifier, sender, body) {
        const field = form.querySelector('[data-inbox-quote-id]'), preview = form.querySelector('[data-inbox-quote-preview]');
        if (!field || !preview) return;
        const scroller = form.closest('[data-canonical-panel]').querySelector('[data-canonical-scroll]');
        const atBottom = scroller && scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop <= 3;
        field.value = identifier;
        preview.querySelector('[data-inbox-quote-preview-sender]').textContent = sender;
        preview.querySelector('[data-inbox-quote-preview-body]').textContent = body;
        preview.hidden = !identifier; refresh();
        if (atBottom) scroller.scrollTop = scroller.scrollHeight;
    }
    document.addEventListener('click', function (event) {
        const button = event.target.closest('[data-inbox-quote-target], [data-inbox-quote-cancel]');
        if (!button || button.disabled) return;
        const panel = button.closest('[data-canonical-panel]'), form = currentForm(panel);
        if (!editable(form)) return;
        event.preventDefault();
        if (button.matches('[data-inbox-quote-cancel]')) { if (form.contains(button)) setQuote(form, '', '', ''); return; }
        const row = button.closest('[data-canonical-message]');
        if (!row || !panel.contains(row) || row.dataset.canonicalMessage !== button.dataset.inboxQuoteTarget) return;
        // Read only normal visible slots, never a whole bubble containing a
        // separately requested internal archive or attachment metadata.
        const body = row.querySelector('[data-inbox-quote-body]'), sender = row.querySelector('[data-inbox-quote-sender]');
        setQuote(form, button.dataset.inboxQuoteTarget, sender ? sender.textContent.trim().slice(0, 100) : '', body ? body.textContent.trim().slice(0, 160) : 'Message');
        const field = form.querySelector('textarea'); if (field) field.focus({preventScroll:true});
    });
    document.addEventListener('htmx:beforeRequest', function (event) {
        const form = event.detail.elt && event.detail.elt.closest('[data-inbox-reply-form]'); if (form) { busy.add(form); refresh(); }
    });
    document.addEventListener('htmx:afterRequest', function (event) {
        const form = event.detail.elt && event.detail.elt.closest('[data-inbox-reply-form]'); if (form) busy.delete(form); refresh();
    });
    document.addEventListener('htmx:afterSwap', refresh);
    document.addEventListener('htmx:historyRestore', refresh);
    document.addEventListener('inbox:history-loaded', refresh);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', refresh); else refresh();
})();
