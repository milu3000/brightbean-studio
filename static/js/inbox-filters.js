/* Keep filter state explicit while clearing only the search query and page. */
(function () {
    'use strict';
    if (window.inboxFiltersInstalled) return;
    window.inboxFiltersInstalled = true;
    function update() {
        document.querySelectorAll('[data-inbox-search]').forEach(function (input) {
            const clear = input.closest('form').querySelector('[data-inbox-clear-search]');
            if (clear) clear.hidden = !input.value;
        });
        filterAccounts(false);
    }
    function filterAccounts(reset) {
        const platform = document.querySelector('[data-inbox-platform]');
        const account = document.querySelector('[data-inbox-account]');
        if (!platform || !account) return;
        const values = Array.from(platform.selectedOptions).map(option => option.value).filter(Boolean);
        Array.from(account.options).forEach(function (option) {
            const excluded = Boolean(option.dataset.platform && values.length && !values.includes(option.dataset.platform));
            if (reset && excluded) option.selected = false;
            // A bookmarked unavailable selection must not be silently dropped.
            option.hidden = excluded && !option.selected;
            option.disabled = excluded && !option.selected;
        });
        if (!Array.from(account.selectedOptions).length) account.value = '';
    }
    document.addEventListener('input', function (event) {
        if (event.target.matches('[data-inbox-search]')) update();
    });
    document.addEventListener('change', function (event) {
        if (event.target.matches('[data-inbox-platform]')) filterAccounts(true);
    }, true);
    document.addEventListener('click', function (event) {
        const button = event.target.closest('[data-inbox-clear-search]');
        const input = button && button.closest('form').querySelector('[data-inbox-search]');
        if (!input) return;
        input.value = ''; update(); input.focus();
        const form = input.closest('form');
        if (form.dataset && Object.hasOwn(form.dataset, 'inboxPlainFilters')) form.requestSubmit();
        else if (window.htmx) window.htmx.trigger(input, 'inbox:clear-search');
    });
    document.addEventListener('htmx:configRequest', function (event) {
        const element = event.detail.elt;
        if (!element || !element.closest('#inbox-filters')) return;
        const parameters = event.detail.parameters;
        if (!element.matches('[data-canonical-filters]')) { delete parameters.page; delete parameters.cursor; }
        if (parameters.q === '') delete parameters.q;
    });
    document.addEventListener('htmx:afterSwap', update);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', update);
    else update();
})();
