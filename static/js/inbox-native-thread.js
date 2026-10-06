/* A user-triggered, temporary platform read. Never submits or replaces drafts. */
(function () {
    'use strict';
    if (window.inboxNativeThreadInstalled) return;
    window.inboxNativeThreadInstalled = true;
    let active = null;

    const reasons = {
        unsupported_platform: 'This platform does not support this conversation read.',
        account_unavailable: 'This connected account is currently unavailable.',
        missing_native_thread: 'This message has no reliable platform conversation ID.',
        unverified_thread: 'This conversation could not be verified as a one-to-one conversation.',
        provider_unavailable: 'The platform could not be reached. You can try again.',
        rate_limited: 'The platform asked us to wait. Try again later.',
        platform_permission_unavailable: 'The platform did not grant access to this conversation.',
        response_too_large: 'The platform response exceeded the safe read limit.',
        invalid_response: 'The platform response could not be verified.',
        thread_scope_mismatch: 'The returned conversation did not match the selected message.',
        participants_unverified: 'The conversation participants could not be verified.',
        message_scope_unverified: 'The returned messages could not be matched safely to this conversation.',
        authorization_required: 'You no longer have access to read this conversation.',
        authorization_revoked: 'Your access changed while reading. No platform content is shown.',
        stale: 'The selected message or account changed. Reopen it before reading again.'
    };

    function node(parent, tag, text, className) {
        const element = document.createElement(tag);
        if (text) element.textContent = text;
        if (className) element.className = className;
        parent.appendChild(element);
        return element;
    }

    function stamp(value) {
        if (typeof value !== 'string') return '';
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
    }

    function attachmentLink(value) {
        // The service also validates public URLs and strips unsafe metadata.
        if (typeof value !== 'string' || /[\s\\]/.test(value)) return '';
        try {
            const url = new URL(value);
            if (url.protocol !== 'https:' || url.username || url.password) return '';
            return value;
        } catch (_) { return ''; }
    }

    function renderItem(parent, item) {
        const card = node(parent, 'article', '', 'rounded-lg border border-stone-200 bg-stone-50 p-3');
        card.dataset.nativeThreadItem = '';
        const direction = item.direction === 'outbound' ? 'Account-side message' : 'Incoming message';
        node(card, 'p', direction + ' · observed on platform', 'text-[11px] font-semibold text-stone-600');
        const time = stamp(item.occurred_at);
        if (time) node(card, 'p', time, 'text-[11px] text-stone-500 mt-1');
        if (typeof item.body === 'string' && item.body) {
            node(card, 'p', item.body, 'text-[13px] text-stone-800 whitespace-pre-wrap break-words mt-2');
        }
        const attachments = Array.isArray(item.attachments) ? item.attachments : [];
        attachments.forEach(function (attachment) {
            const media = node(card, 'div', '', 'border-t border-stone-200 mt-2 pt-2');
            const types = { image: 'Photo', video: 'Video', audio: 'Audio', file: 'File', share: 'Shared content' };
            node(media, 'p', types[attachment.type] || 'Attachment', 'text-[12px] font-semibold text-stone-600');
            if (typeof attachment.title === 'string') node(media, 'p', attachment.title, 'text-[12px] break-words');
            const url = attachment.availability === 'available' && attachmentLink(attachment.url);
            if (url) {
                const link = node(media, 'a', 'Open attachment', 'text-[12px] text-orange-700 underline');
                link.href = url;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                link.referrerPolicy = 'no-referrer';
                node(media, 'p', 'Platform links may expire or require sign-in.', 'text-[11px] text-stone-500');
            } else {
                node(media, 'p', 'Media unavailable. The platform supplied no safe usable link.', 'text-[12px] text-stone-500');
            }
        });
        if (!item.body && !attachments.length) {
            node(card, 'p', 'No displayable text or media was provided. The original content has not been verified.', 'text-[12px] text-stone-500 mt-2');
        }
        if (item.body_truncated || item.attachments_truncated ||
            ['partial', 'unsupported', 'fields_unavailable', 'removed'].includes(item.content_status)) {
            node(card, 'p', 'Some content is missing, removed, unsupported or shortened in this view.', 'text-[12px] text-stone-500 mt-2');
        }
    }

    function clearSnapshot(root) {
        root.querySelector('[data-native-thread-refresh]').disabled = false;
        root.querySelector('[data-native-thread-result]').hidden = true;
        root.querySelector('[data-native-thread-items]').replaceChildren();
        root.querySelector('[data-native-thread-status]').textContent = '';
        root.querySelector('[data-native-thread-warning]').textContent = '';
    }

    function cancel() {
        if (!active) return;
        const state = active;
        active = null;
        state.controller.abort();
        clearSnapshot(state.root);
    }

    function clearSnapshots() {
        cancel();
        // Restored DOM may be a clone rather than active.root. Clear all
        // snapshot sections, leaving stored history and textarea values alone.
        document.querySelectorAll('[data-native-thread]').forEach(clearSnapshot);
    }

    function current(state) {
        return active === state && state.root.isConnected &&
            document.querySelector('[data-inbox-panel]') === state.panel &&
            state.panel.dataset.selectedMessageId === state.anchor;
    }

    async function refresh(root, button) {
        // Repeated clicks while a read is pending never issue duplicate reads.
        if (button.disabled) return;
        cancel();
        const panel = root.closest('[data-inbox-panel]');
        if (!panel || panel.dataset.selectedMessageId !== root.dataset.anchorId) return;
        const state = { root, panel, button, anchor: root.dataset.anchorId, controller: new AbortController() };
        active = state;
        const status = root.querySelector('[data-native-thread-status]');
        const warning = root.querySelector('[data-native-thread-warning]');
        const items = root.querySelector('[data-native-thread-items]');
        root.querySelector('[data-native-thread-result]').hidden = false;
        items.replaceChildren();
        warning.hidden = true;
        warning.textContent = '';
        status.textContent = 'Reading platform conversation…';
        button.disabled = true;
        try {
            const csrf = document.querySelector('[name=csrfmiddlewaretoken]');
            if (!csrf || !csrf.value) throw new Error('csrf_missing');
            const response = await fetch(root.dataset.refreshUrl, {
                method: 'POST',
                headers: { 'X-CSRFToken': csrf.value, Accept: 'application/json' },
                credentials: 'same-origin', cache: 'no-store', signal: state.controller.signal
            });
            if (!current(state)) return;
            const result = await response.json();
            if (!current(state)) return;
            if (result.anchor_message_id !== state.anchor) throw new Error('mismatched_anchor');
            if (result.status !== 'observed' || !response.ok) {
                status.textContent = reasons[result.reason_code] || 'The conversation could not be read. No platform content is shown. You can try again.';
                return;
            }
            if (!Array.isArray(result.items)) throw new Error('invalid_items');
            const checked = stamp(result.checked_at);
            status.textContent = (checked ? 'Read at ' + checked + '. ' : '') +
                'Not saved. This snapshot may be incomplete or already out of date. ' +
                'It does not establish who sent a message or confirm delivery of a Brightbean reply.';
            if (result.newer_outbound_observed) {
                warning.textContent = 'A newer account-side message was observed. Review it before deciding whether to reply.';
                warning.hidden = false;
            }
            if (!result.items.length) {
                node(items, 'p', 'No messages were returned in this bounded read. This does not mean no reply exists.', 'text-[12px] text-stone-500');
            }
            result.items.forEach(function (item) { renderItem(items, item); });
            if (result.more_available || (result.coverage && result.coverage.truncated)) {
                node(items, 'p', 'Additional messages may exist. No further page was loaded.', 'text-[12px] text-stone-500');
            }
        } catch (_) {
            if (current(state)) {
                items.replaceChildren();
                warning.hidden = true;
                status.textContent = 'The conversation could not be read. Your draft is still here. You can try again.';
            }
        } finally {
            if (current(state)) button.disabled = false;
        }
    }

    document.addEventListener('click', function (event) {
        if (!event.target.closest) return;
        if (event.target.closest('[data-inbox-back]')) { cancel(); return; }
        const dismiss = event.target.closest('[data-native-thread-dismiss]');
        if (dismiss) { event.preventDefault(); cancel(); return; }
        const button = event.target.closest('[data-native-thread-refresh]');
        if (!button) return;
        event.preventDefault();
        const root = button.closest('[data-native-thread]');
        if (root) refresh(root, button);
    });
    document.addEventListener('htmx:beforeRequest', function (event) {
        const element = event.detail.elt;
        if (element && element.dataset.inboxOpenMessage) cancel();
    });
    document.addEventListener('htmx:beforeSwap', function (event) {
        const target = event.detail.target;
        if (active && target && (target === active.panel || target.contains(active.panel))) cancel();
    });
    // HTMX also saves the whole page for list-only filter/pagination pushes.
    // This synchronous event precedes its clone/localStorage write. Abort
    // pending reads too, so their late results cannot repopulate the page.
    document.addEventListener('htmx:beforeHistorySave', clearSnapshots);
    document.addEventListener('htmx:historyRestore', clearSnapshots);
    window.addEventListener('popstate', cancel);
    window.addEventListener('pagehide', cancel);
    window.addEventListener('pageshow', function (event) { if (event.persisted) clearSnapshots(); });
}());
