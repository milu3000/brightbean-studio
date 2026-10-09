/* One temporary platform read per opened conversation. Never submits or replaces drafts. */
(function () {
    'use strict';
    if (window.inboxNativeThreadInstalled) return;
    window.inboxNativeThreadInstalled = true;
    let active = null;
    let suspended = null;
    const opened = new WeakSet();
    const savedRowLimit = 500;
    const savedPageLimit = 25;
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
    const diagnosticReasons = new Set(Object.keys(reasons).concat([
        'bounded_snapshot', 'no_messages_observed', 'read_failed', 'invalid_limit', 'stale_page',
        'pagination_unavailable', 'invalid_continuation', 'expired_continuation', 'stale_continuation'
    ]));
    const diagnosticCounts = {
        scanned_count: 'nativeReadScannedCount', returned_count: 'nativeReadReturnedCount', skipped_count: 'nativeReadSkippedCount'
    };
    function clearReadMetadata(root) {
        delete root.dataset.nativeReadReasonCode;
        Object.values(diagnosticCounts).forEach(key => { delete root.dataset[key]; });
    }
    function readMetadata(root, result) {
        clearReadMetadata(root);
        root.dataset.nativeReadReasonCode = diagnosticReasons.has(result.reason_code) ? result.reason_code : 'unrecognized_response';
        const coverage = result.coverage || {};
        Object.entries(diagnosticCounts).forEach(([field, key]) => {
            const value = coverage[field];
            if (Number.isInteger(value) && value >= 0 && value <= 100) root.dataset[key] = String(value);
        });
    }
    function node(parent, tag, text, className) {
        const element = document.createElement(tag);
        if (text) element.textContent = text;
        if (className) element.className = className;
        parent.appendChild(element);
        return element;
    }
    function transient(element) { element.dataset.nativeThreadTransient = ''; return element; }
    function eventTime(value) {
        // A date without a timezone must never acquire the browser's timezone.
        if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
        const parts = value.slice(0, 19).split(/[-T:]/).map(Number);
        if (parts[1] < 1 || parts[1] > 12 || parts[2] < 1 ||
            parts[2] > new Date(Date.UTC(parts[0], parts[1], 0)).getUTCDate() ||
            parts[3] > 23 || parts[4] > 59 || parts[5] > 59) return null;
        const time = Date.parse(value);
        return Number.isFinite(time) ? time : null;
    }
    function stamp(value) {
        const time = eventTime(value);
        return time === null ? '' : new Date(time).toLocaleString(undefined, { timeZoneName: 'short' });
    }
    function identity(value) {
        return typeof value === 'string' && value.length <= 255 && /^[^\s\x00-\x1f\x7f]+$/.test(value) ? value : '';
    }
    function attachmentLink(value) {
        if (typeof value !== 'string' || /[\s\\]/.test(value)) return '';
        try {
            const url = new URL(value);
            return url.protocol === 'https:' && !url.username && !url.password ? value : '';
        } catch (_) { return ''; }
    }
    function renderAttachments(parent, attachments) {
        attachments.forEach(function (attachment) {
            const media = node(parent, 'div', '', 'inbox-attachment border-t border-stone-200 mt-2 pt-2');
            const types = { image: 'Photo', video: 'Video', audio: 'Audio', file: 'File', share: 'Shared content' };
            node(media, 'p', types[attachment.type] || 'Attachment', 'text-[12px] font-semibold text-stone-600');
            if (typeof attachment.title === 'string') node(media, 'p', attachment.title, 'text-[12px] break-words');
            const url = attachment.availability === 'available' && attachmentLink(attachment.url);
            let post = false, preview = '';
            if (url && attachment.type === 'share') {
                const parts = new URL(url), host = parts.hostname.toLowerCase(), path = parts.pathname;
                post = (['instagram.com', 'www.instagram.com'].includes(host) && /^\/(p|reel|reels|tv|stories)\/[^/]+/.test(path)) ||
                    (['facebook.com', 'www.facebook.com', 'm.facebook.com', 'mbasic.facebook.com'].includes(host) &&
                        (/\/(posts|photos|videos|reel|share)\/[^/]+/.test(path) ||
                            (['/permalink.php', '/story.php', '/photo.php', '/watch/'].includes(path) && !!parts.search))) ||
                    (['threads.net', 'www.threads.net', 'threads.com', 'www.threads.com'].includes(host) && /^\/@[^/]+\/post\/[^/]+/.test(path)) ||
                    (host === 'fb.watch' && !!path.replaceAll('/', ''));
            }
            const candidate = attachmentLink(attachment.preview_url);
            if (candidate) {
                const host = new URL(candidate).hostname.toLowerCase();
                if (['fbcdn.net', 'cdninstagram.com', 'fbsbx.com'].some(domain => host === domain || host.endsWith('.' + domain))) preview = candidate;
            }
            function link(label, target) {
                const anchor = node(media, 'a', label, 'text-[12px] text-orange-700 underline mr-3');
                anchor.href = target; anchor.target = '_blank';
                anchor.rel = 'noopener noreferrer'; anchor.referrerPolicy = 'no-referrer';
                return anchor;
            }
            if (preview) {
                const imageLink = link('', post ? url : preview);
                imageLink.setAttribute('aria-label', post ? 'Open original post' : 'View image');
                const img = node(imageLink, 'img', '', 'max-w-full max-h-64 rounded-md object-contain mt-2');
                img.src = preview; img.alt = 'Attachment preview'; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer';
                img.dataset.inboxPreview = '';
                const fallback = node(media, 'p', 'Preview unavailable.', 'text-[12px] text-stone-500');
                fallback.dataset.inboxPreviewFallback = ''; fallback.hidden = true;
            }
            if (url) {
                link(post ? 'Open original post' : attachment.type === 'image' ? 'View image' : 'Open attachment', url);
            } else if (!preview) {
                node(media, 'p', 'Content unavailable.', 'text-[12px] text-stone-500');
            }
            if (preview && preview !== url) link('View image', preview);
        });
    }
    function partial(item) {
        return item.body_truncated || item.attachments_truncated ||
            ['partial', 'unsupported', 'fields_unavailable', 'removed'].includes(item.content_status);
    }
    function renderItem(parent, item, explanation) {
        const wrapper = transient(node(parent, 'div', '', item.direction === 'outbound' ? 'flex justify-end' : 'flex justify-start'));
        wrapper.dataset.timelineEvent = 'native';
        wrapper.dataset.eventTime = eventTime(item.occurred_at) === null ? '' : item.occurred_at;
        wrapper.dataset.eventDirection = item.direction;
        wrapper.dataset.platformMessageId = identity(item.platform_message_id);
        const card = node(wrapper, 'article', '', 'max-w-full rounded-xl border border-stone-200 bg-stone-50 p-3');
        card.dataset.nativeThreadItem = '';
        card.style.maxWidth = '85%';
        const direction = item.direction === 'outbound' ? 'Account-side message' : item.direction === 'inbound' ? 'Incoming message' : 'Direction unverified';
        node(card, 'p', direction, 'text-[11px] font-semibold text-stone-600');
        const time = node(card, 'time', stamp(item.occurred_at) || 'Time unavailable · chronological position unverified', 'text-[11px] text-stone-500 mt-1');
        if (eventTime(item.occurred_at) !== null) time.dateTime = item.occurred_at;
        if (explanation) node(card, 'p', explanation, 'text-[11px] text-amber-800 mt-1');
        if (typeof item.body === 'string' && item.body) node(card, 'p', item.body, 'text-[13px] text-stone-800 whitespace-pre-wrap break-words mt-2');
        const attachments = Array.isArray(item.attachments) ? item.attachments : [];
        renderAttachments(card, attachments);
        if (!item.body && !attachments.length) node(card, 'p', 'No displayable text or media was provided. The original content has not been verified.', 'text-[12px] text-stone-500 mt-2');
        if (partial(item)) node(card, 'p', 'Some content is missing, removed, unsupported or shortened in this view.', 'text-[12px] text-stone-500 mt-2');
        return wrapper;
    }
    function storedEvents(timeline) {
        return Array.from(timeline.children).filter(element => element.dataset.timelineEvent === 'stored');
    }
    function removeLoadedSelectedAside(state) {
        const rows = storedEvents(state.timeline).filter(element => element.dataset.eventId === 'incoming:' + state.anchor);
        if (rows.length !== 1 || rows[0].dataset.eventKind !== 'incoming' || rows[0].dataset.eventDirection !== 'inbound') return;
        const selected = element => Array.from(element.querySelectorAll('[data-incoming-message-id]'))
            .filter(bubble => bubble.dataset.incomingMessageId === state.anchor);
        if (selected(rows[0]).length !== 1) return;
        state.panel.querySelectorAll('aside').forEach(aside => {
            if (!state.timeline.contains(aside) && aside.getAttribute('aria-label') === 'Selected message outside this history page' &&
                selected(aside).length === 1) aside.remove();
        });
    }
    function restoreTimeline(timeline) {
        // DOM markers also survive a history clone; no in-memory state is needed.
        timeline.querySelectorAll('[data-native-thread-transient]').forEach(element => element.remove());
        timeline.querySelectorAll('[data-native-original-hidden]').forEach(element => {
            element.hidden = element.dataset.nativeOriginalHidden === 'true';
            delete element.dataset.nativeOriginalHidden;
        });
    }
    function clearControls(root) {
        clearReadMetadata(root);
        const button = root.querySelector('[data-native-thread-refresh]');
        button.disabled = false;
        button.hidden = true;
        root.querySelector('[data-native-thread-result]').hidden = true;
        root.querySelector('[data-native-thread-status]').textContent = '';
        root.querySelector('[data-native-history-status]').textContent = '';
        root.querySelector('[data-native-history-retry]').hidden = true;
        const warning = root.querySelector('[data-native-thread-warning]');
        warning.hidden = true;
        warning.textContent = '';
    }
    function current(state) {
        return active === state && state.panel.isConnected && state.root.isConnected && state.timeline.isConnected &&
            document.querySelector('[data-inbox-panel]') === state.panel &&
            state.panel.dataset.selectedMessageId === state.anchor &&
            state.timeline.dataset.timelineAnchorId === state.anchor;
    }
    function cancel() {
        if (!active) return;
        const state = active;
        active = null;
        if (state.controller) state.controller.abort();
        if (state.pageController) state.pageController.abort();
        if (state.nativePageController) state.nativePageController.abort();
        state.scroller.removeEventListener('scroll', state.onScroll);
        state.scroller.removeEventListener('wheel', state.onWheel);
        restoreTimeline(state.timeline);
        clearControls(state.root);
    }
    function clearSnapshots() {
        suspended = null;
        cancel();
        document.querySelectorAll('[data-stored-timeline-events]').forEach(restoreTimeline);
        document.querySelectorAll('[data-native-thread]').forEach(clearControls);
    }
    function discardObservation(state) {
        if (state.controller) state.controller.abort();
        state.controller = null;
        if (state.nativePageController) state.nativePageController.abort();
        state.nativePageController = null;
        state.nativeCursor = '';
        state.loading = false;
        state.items = null;
        restoreTimeline(state.timeline);
        clearControls(state.root);
    }
    function localTimes(panel) {
        panel.querySelectorAll('time').forEach(element => {
            const wrapper = element.closest('[data-timeline-event]');
            if (wrapper && eventTime(wrapper.dataset.eventTime) === null) return;
            const formatted = stamp(element.dateTime || element.getAttribute('datetime'));
            if (formatted) element.textContent = formatted;
        });
        panel.querySelectorAll('[data-timeline-day-label]').forEach(element => {
            const wrapper = element.closest('[data-timeline-event]');
            const time = wrapper && eventTime(wrapper.dataset.eventTime);
            if (time !== null && time !== undefined) element.textContent = new Date(time).toLocaleDateString();
        });
    }
    function supplement(stored, item) {
        const bubble = stored.querySelector('[data-stored-event-bubble]') || stored;
        const evidence = transient(node(bubble, 'div', '', 'text-[11px] text-stone-500 mt-2'));
        const body = stored.querySelector('[data-stored-event-body]');
        if ((!body || !body.textContent) && typeof item.body === 'string' && item.body) {
            node(evidence, 'p', item.body, 'text-[13px] text-stone-800 whitespace-pre-wrap break-words mt-2');
        }
        // Only a usable exact attachment URL proves that an attachment is already
        // displayed; unavailable attachments remain explicit observations.
        const existing = new Set(Array.from(stored.querySelectorAll('a')).map(link => attachmentLink(link.href)).filter(Boolean));
        const extra = (Array.isArray(item.attachments) ? item.attachments : []).filter(attachment => {
            const url = attachment.availability === 'available' && attachmentLink(attachment.url);
            return !url || !existing.has(url);
        });
        if (extra.length) {
            renderAttachments(evidence, extra);
        }
        if (partial(item)) node(evidence, 'p', 'The platform observation is partial; saved content is retained.');
    }
    function merge(state, items) {
        restoreTimeline(state.timeline);
        const stored = storedEvents(state.timeline);
        const byId = new Map();
        stored.forEach(element => {
            const id = identity(element.dataset.platformMessageId);
            if (id) byId.set(id, (byId.get(id) || []).concat(element));
        });
        const counts = new Map();
        items.forEach(item => { const id = identity(item.platform_message_id); if (id) counts.set(id, (counts.get(id) || 0) + 1); });
        let last = -Infinity;
        const ordered = stored.every(element => {
            const time = eventTime(element.dataset.eventTime);
            if (time === null) return true;
            const follows = time >= last;
            last = time;
            return follows;
        });
        const pending = [];
        items.forEach((item, index) => {
            const id = identity(item.platform_message_id);
            const matches = byId.get(id) || [];
            const time = eventTime(item.occurred_at);
            const direction = ['inbound', 'outbound'].includes(item.direction);
            let explanation = '';
            let unpositioned = false;
            if (!id) explanation = 'Not merged: no reliable platform message ID.';
            else if (counts.get(id) !== 1 || matches.length > 1) explanation = 'Not merged: this message ID is ambiguous.';
            else if (!direction || (matches.length && matches[0].dataset.eventDirection !== item.direction)) explanation = 'Not merged: message direction could not be matched safely.';
            else if (matches.length) {
                const match = matches[0];
                const body = match.querySelector('[data-stored-event-body]');
                if (time === null || eventTime(match.dataset.eventTime) === null) {
                    explanation = 'Not merged: a message timestamp is unavailable or uncertain.';
                    unpositioned = true;
                }
                else if (body && body.textContent && item.body && body.textContent !== item.body) explanation = 'Not merged: platform content differs from the saved record. Both sources are retained.';
                else { supplement(match, item); return; }
            }
            if (!ordered) explanation += (explanation ? ' ' : '') + 'Chronological position cannot be verified against this saved page.';
            pending.push({ item, index, time: ordered && direction && !unpositioned ? time : null, explanation });
        });
        pending.sort((a, b) => (a.time === null) - (b.time === null) || (a.time === null ? 0 : a.time - b.time) || a.index - b.index);
        let unpositioned;
        pending.forEach(record => {
            if (record.time === null) {
                if (!unpositioned) {
                    unpositioned = transient(node(state.timeline, 'section', '', 'space-y-3 border-t border-amber-200 pt-3'));
                    unpositioned.dataset.nativeUnpositioned = '';
                    node(unpositioned, 'p', 'Time unavailable', 'text-[12px] text-amber-800');
                }
                renderItem(unpositioned, record.item, record.explanation);
            } else {
                const wrapper = renderItem(state.timeline, record.item, record.explanation);
                const next = stored.find(element => {
                    const time = eventTime(element.dataset.eventTime);
                    return time !== null && time > record.time;
                });
                if (next) state.timeline.insertBefore(wrapper, next);
                else if (unpositioned) state.timeline.insertBefore(wrapper, unpositioned);
            }
        });
        // Replace day separators only for this temporary combined view. The
        // saved separators and their original hidden state are restored exactly.
        state.timeline.querySelectorAll('[data-timeline-day-label]').forEach(element => {
            element.dataset.nativeOriginalHidden = String(element.hidden);
            element.hidden = true;
        });
        let day = '';
        Array.from(state.timeline.children).forEach(element => {
            if (!element.dataset.timelineEvent) return;
            const time = eventTime(element.dataset.eventTime);
            if (time === null) { day = ''; return; }
            const nextDay = new Date(time).toLocaleDateString();
            if (nextDay !== day) {
                const label = transient(document.createElement('p'));
                label.className = 'text-[11px] text-stone-500 text-center py-2';
                label.textContent = nextDay;
                state.timeline.insertBefore(label, element);
                day = nextDay;
            }
        });
    }
    function scrollAnchor(state) {
        if (!state.scroller.getBoundingClientRect) return null;
        const viewport = state.scroller.getBoundingClientRect();
        const rows = state.timeline.querySelectorAll('[data-timeline-event]');
        for (const element of rows) {
            const box = element.getBoundingClientRect();
            if (box.bottom > viewport.top && box.top < viewport.bottom) {
                return { element, id: element.dataset.eventId, platformId: element.dataset.platformMessageId,
                    direction: element.dataset.eventDirection, offset: box.top - viewport.top };
            }
        }
        return null;
    }
    function keepAnchor(state, anchor, beforeHeight, beforeTop) {
        let element = anchor && anchor.element;
        if (anchor && !element.isConnected) {
            const matches = Array.from(state.timeline.querySelectorAll('[data-timeline-event]')).filter(row =>
                anchor.id ? row.dataset.eventId === anchor.id : anchor.platformId &&
                    row.dataset.platformMessageId === anchor.platformId && row.dataset.eventDirection === anchor.direction);
            element = matches.length === 1 ? matches[0] : null;
        }
        if (element && element.isConnected) {
            state.scroller.scrollTop += element.getBoundingClientRect().top - state.scroller.getBoundingClientRect().top - anchor.offset;
        } else {
            state.scroller.scrollTop = beforeTop + state.scroller.scrollHeight - beforeHeight;
        }
        state.lastTop = state.scroller.scrollTop;
    }
    function bottom(state) {
        state.scroller.scrollTop = state.scroller.scrollHeight;
        state.lastTop = state.scroller.scrollTop;
    }
    async function refresh(state) {
        if (!current(state) || (state.controller && state.loading)) return;
        clearReadMetadata(state.root);
        if (state.controller) state.controller.abort();
        restoreTimeline(state.timeline);
        if (state.nativePageController) state.nativePageController.abort();
        state.nativePageController = null;
        state.nativeCursor = '';
        state.nativePages = 0;
        state.nativeCursors = new Set();
        state.nativePageKeys = new Set();
        state.items = null;
        const controller = new AbortController();
        state.controller = controller;
        state.loading = true;
        const status = state.root.querySelector('[data-native-thread-status]');
        const warning = state.root.querySelector('[data-native-thread-warning]');
        const button = state.root.querySelector('[data-native-thread-refresh]');
        state.root.querySelector('[data-native-history-status]').textContent = '';
        state.root.querySelector('[data-native-history-retry]').hidden = true;
        state.root.querySelector('[data-native-thread-result]').hidden = false;
        warning.hidden = true;
        warning.textContent = '';
        status.textContent = 'Reading latest platform conversation…';
        button.hidden = true;
        button.disabled = true;
        const valid = () => current(state) && state.controller === controller;
        let succeeded = false;
        try {
            const csrf = document.querySelector('[name=csrfmiddlewaretoken]');
            if (!csrf || !csrf.value) throw new Error('csrf_missing');
            const response = await fetch(state.root.dataset.refreshUrl, {
                method: 'POST', headers: { 'X-CSRFToken': csrf.value, Accept: 'application/json' },
                credentials: 'same-origin', cache: 'no-store', signal: controller.signal
            });
            if (!valid()) return;
            if (authorizationLost(response)) { clearPlatformAccess(state); return; }
            const result = await response.json();
            if (!valid()) return;
            if (result.anchor_message_id !== state.anchor) throw new Error('mismatched_anchor');
            readMetadata(state.root, result);
            if (result.status !== 'observed' || !response.ok) {
                status.textContent = reasons[result.reason_code] || 'Messages could not be loaded. Try again.';
                return;
            }
            if (!Array.isArray(result.items) || result.items.some(item => !item || item.source !== 'platform_observed')) throw new Error('invalid_items');
            state.items = result.items;
            state.nativePages = 1;
            if (typeof result.page_key === 'string' && result.page_key) state.nativePageKeys.add(result.page_key);
            state.nativeCursor = continuation(result.older_continuation);
            const anchor = scrollAnchor(state);
            const beforeHeight = state.scroller.scrollHeight;
            const beforeTop = state.scroller.scrollTop;
            merge(state, state.items);
            const partial = Boolean(result.more_available || (result.coverage && result.coverage.truncated));
            status.textContent = partial && !state.nativeCursor ? 'Earlier messages could not be loaded.' :
                result.items.length ? '' : 'No messages were returned.';
            if (result.newer_outbound_observed) {
                warning.textContent = 'Newer account activity is available.';
                warning.hidden = false;
            }
            succeeded = !partial || Boolean(state.nativeCursor);
            if (state.userScrolledUp && anchor) keepAnchor(state, anchor, beforeHeight, beforeTop);
            else if (!state.userScrolledUp) bottom(state);
        } catch (_) {
            if (valid()) {
                clearReadMetadata(state.root);
                restoreTimeline(state.timeline);
                state.items = null;
                warning.hidden = true;
                status.textContent = 'Messages could not be loaded. Try again.';
            }
        } finally {
            if (valid()) { state.loading = false; button.disabled = false; button.hidden = succeeded; }
        }
    }
    function clearPlatformAccess(state) {
        state.retentionBlocked = true;
        discardObservation(state);
        state.root.querySelector('[data-native-thread-result]').hidden = false;
        state.root.querySelector('[data-native-thread-status]').textContent = 'Conversation access or identity changed. Temporary platform observations have been cleared.';
    }
    function authorizationLost(response) {
        if (response.status === 401 || response.status === 403) return true;
        if (!response.redirected || !response.url) return false;
        try {
            const url = new URL(response.url);
            return url.origin === window.location.origin && url.pathname === '/accounts/login/';
        } catch (_) { return false; }
    }
    function continuation(value) {
        return typeof value === 'string' && value.length > 0 && value.length <= 6144 && !/[\s\x00-\x1f\x7f]/.test(value) ? value : '';
    }
    function combinePages(previous, next) {
        const combined = previous.slice();
        const key = item => identity(item.platform_message_id) && ['inbound', 'outbound'].includes(item.direction) ? item.direction + ':' + item.platform_message_id : '';
        const counts = new Map();
        next.forEach(item => { const id = key(item); if (id) counts.set(id, (counts.get(id) || 0) + 1); });
        next.forEach(item => {
            const id = key(item);
            const matches = id ? previous.filter(old => key(old) === id) : [];
            // Repeated boundary rows from different pages can be collapsed only
            // after exact identity and identical observed fields are established.
            if (id && counts.get(id) === 1 && matches.length === 1 && JSON.stringify(matches[0]) === JSON.stringify(item)) return;
            combined.push(item);
        });
        return combined;
    }
    async function loadOlderNative(state) {
        if (!current(state) || state.nativePageController || !state.nativeCursor || !state.items) return;
        const status = state.root.querySelector('[data-native-history-status]');
        const retry = state.root.querySelector('[data-native-history-retry]');
        if (state.items.length >= 500 || state.nativePages >= 25) {
            state.nativeCursor = '';
            status.textContent = 'Temporary platform history limit reached (500 messages or 25 pages). Older platform activity may remain.';
            return;
        }
        const cursor = state.nativeCursor;
        if (state.nativeCursors.has(cursor)) {
            state.nativeCursor = '';
            status.textContent = 'Earlier messages could not be loaded. Try again.';
            return;
        }
        const controller = new AbortController();
        state.nativePageController = controller;
        status.textContent = 'Loading earlier platform messages…';
        retry.hidden = true;
        const valid = () => current(state) && state.nativePageController === controller;
        try {
            const csrf = document.querySelector('[name=csrfmiddlewaretoken]');
            if (!csrf || !csrf.value) throw new Error('csrf_missing');
            const response = await fetch(state.root.dataset.refreshUrl, {
                method: 'POST', headers: { 'X-CSRFToken': csrf.value, Accept: 'application/json', 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({ continuation: cursor }).toString(),
                credentials: 'same-origin', cache: 'no-store', signal: controller.signal
            });
            if (!valid()) return;
            if (authorizationLost(response)) { clearPlatformAccess(state); return; }
            const result = await response.json();
            if (!valid()) return;
            const revoked = ['authorization_required', 'authorization_revoked', 'stale', 'thread_scope_mismatch',
                'unverified_thread', 'participants_unverified', 'message_scope_unverified', 'stale_continuation',
                'account_unavailable', 'platform_permission_unavailable'].includes(result.reason_code);
            if (revoked || response.status === 401 || response.status === 403 || result.anchor_message_id !== state.anchor) {
                clearPlatformAccess(state);
                return;
            }
            if (['invalid_continuation', 'expired_continuation', 'continuation_expired', 'continuation_invalid', 'stale_page'].includes(result.reason_code)) {
                clearReadMetadata(state.root);
                state.nativeCursor = '';
                status.textContent = result.reason_code === 'stale_page' ?
                    'The platform page changed while reading. Reload the latest platform conversation to start a new read.' :
                    'This earlier-history read has expired or is invalid. Reload the latest platform conversation to start a new read.';
                const reload = state.root.querySelector('[data-native-thread-refresh]');
                reload.textContent = 'Reload latest platform conversation';
                reload.hidden = false;
                return;
            }
            if (!response.ok || result.status !== 'observed' || result.anchor_message_id !== state.anchor ||
                !Array.isArray(result.items) || result.items.some(item => !item || item.source !== 'platform_observed')) throw new Error('native_page_invalid');
            if (typeof result.page_key === 'string' && result.page_key && state.nativePageKeys.has(result.page_key)) {
                state.nativeCursor = '';
                status.textContent = 'Earlier messages could not be loaded. Try again.';
                return;
            }
            const combined = combinePages(state.items, result.items);
            if (combined.length > 500) {
                state.nativeCursor = '';
                status.textContent = 'Temporary platform history limit reached (500 messages). This additional page was not displayed; older activity may remain.';
                return;
            }
            const anchor = scrollAnchor(state);
            const beforeHeight = state.scroller.scrollHeight;
            const beforeTop = state.scroller.scrollTop;
            const advanced = combined.length > state.items.length;
            state.items = combined;
            state.nativePages += 1;
            state.nativeCursors.add(cursor);
            if (typeof result.page_key === 'string' && result.page_key) state.nativePageKeys.add(result.page_key);
            const next = continuation(result.older_continuation);
            state.nativeCursor = next && !state.nativeCursors.has(next) && advanced ? next : '';
            merge(state, state.items);
            status.textContent = !advanced || (next && !state.nativeCursor) ?
                'Earlier messages could not be loaded. Try again.' : state.nativeCursor ?
                '' :
                '';
            keepAnchor(state, anchor, beforeHeight, beforeTop);
        } catch (_) {
            if (valid()) {
                status.textContent = 'Earlier messages could not be loaded. Try again.';
                retry.hidden = false;
            }
        } finally { if (valid()) state.nativePageController = null; }
    }
    function sameOriginUrl(value) {
        if (!value) return '';
        try {
            const url = new URL(value, window.location.href);
            return url.origin === window.location.origin && !url.username && !url.password ? url.href : '';
        } catch (_) { return ''; }
    }
    function savedHistoryAction(state, event) {
        // Only these existing POSTs leave saved history intact. A same-anchor
        // panel alone is not proof that reconnect/delete/other actions do so.
        const detail = event.detail;
        const element = detail.elt;
        const config = detail.requestConfig;
        if (!element || !state.panel.contains(element) || !config ||
            String(config.verb).toLowerCase() !== 'post') return '';
        const declared = element.getAttribute('hx-post') || element.getAttribute('data-hx-post');
        const requested = sameOriginUrl(config.path);
        if (!requested || sameOriginUrl(declared) !== requested) return '';
        const url = new URL(requested);
        if (url.search || url.hash) return '';
        const refreshUrl = sameOriginUrl(state.root.dataset.refreshUrl);
        if (!refreshUrl) return '';
        const suffix = '/' + state.anchor + '/native-thread/';
        const nativePath = new URL(refreshUrl).pathname;
        if (!nativePath.endsWith(suffix)) return '';
        const prefix = nativePath.slice(0, -suffix.length) + '/';
        const target = state.panel.querySelector('[data-reply-target-id]');
        const targetId = target ? identity(target.dataset.replyTargetId) : state.anchor;
        const allowed = [prefix + state.anchor + '/status/', prefix + targetId + '/reply/draft/'];
        const draft = element.closest('[data-draft-target-id]');
        if (draft && state.panel.contains(draft) && typeof draft.id === 'string' && draft.id.startsWith('draft-')) {
            const replyId = identity(draft.id.slice(6));
            if (replyId) allowed.push(prefix + 'replies/' + replyId + '/edit/');
        }
        return allowed.includes(url.pathname) ? requested : '';
    }
    function savedEventKey(element) {
        const time = eventTime(element.dataset.eventTime);
        const id = element.dataset.eventId;
        const fraction = String(element.dataset.eventTime).match(/\.(\d+)(?:Z|[+-])/);
        // Date.parse truncates microseconds; saved cursor ordering must not.
        const subMillisecond = ((fraction && fraction[1]) || '').slice(3).padEnd(9, '0');
        return time === null || !identity(id) ? null : [time, subMillisecond, id];
    }
    function beforeSaved(a, b) {
        return a[0] < b[0] || (a[0] === b[0] && (a[1] < b[1] || (a[1] === b[1] && a[2] < b[2])));
    }
    function retainSavedHistory(state, saved) {
        const fresh = storedEvents(state.timeline);
        const keys = fresh.map(savedEventKey);
        if (!fresh.length || keys.some(key => !key) || state.timeline.dataset.historyComplete === 'true') return false;
        const boundary = keys.reduce((oldest, key) => beforeSaved(key, oldest) ? key : oldest);
        // New notes/replies can displace the whole first page without changing
        // native scope. Reusing an old cursor then would skip an unseen gap.
        const overlap = saved.rows.filter(element => element.dataset.eventId === boundary[2]);
        const oldBoundary = overlap.length === 1 && savedEventKey(overlap[0]);
        if (!oldBoundary || oldBoundary.some((value, index) => value !== boundary[index]) ||
            fresh.filter(element => element.dataset.eventId === boundary[2]).length !== 1) return false;
        const freshIds = new Set(fresh.map(element => element.dataset.eventId));
        // Only earlier rows lie outside the new response's authoritative
        // range. Missing IDs inside that range must not be resurrected.
        const earlier = saved.rows.filter(element => {
            const key = savedEventKey(element);
            return key && beforeSaved(key, boundary) && !freshIds.has(element.dataset.eventId);
        });
        if (!earlier.length || fresh.length + earlier.length > savedRowLimit) return false;
        const first = state.timeline.firstChild;
        earlier.forEach(element => {
            // A selected older incoming may also have a freshly rendered
            // detail outside the first page. Its current status/body wins.
            const incomingId = element.dataset.eventKind === 'incoming' && element.dataset.eventId.slice('incoming:'.length);
            const bubble = incomingId && Array.from(state.panel.querySelectorAll('[data-incoming-message-id]'))
                .find(candidate => candidate.dataset.incomingMessageId === incomingId);
            const oldBubble = bubble && element.querySelector('[data-stored-event-bubble]');
            if (oldBubble) { oldBubble.parentNode.replaceChild(document.importNode(bubble, true), oldBubble); }
            state.timeline.insertBefore(element, first);
            if (window.htmx && window.htmx.process) window.htmx.process(element);
        });
        state.pageKeys = new Set(saved.pageKeys);
        state.pageKeys.delete(saved.firstPageKey);
        state.pageKeys.add(state.timeline.dataset.timelinePageKey);
        state.savedPages = saved.pages;
        state.timeline.dataset.olderUrl = saved.olderUrl;
        state.timeline.dataset.historyComplete = saved.complete;
        storedEvents(state.timeline).forEach((element, index) => { element.dataset.storedOrder = String(index); });
        state.root.querySelector('[data-stored-history-status]').textContent = saved.status;
        state.root.querySelector('[data-stored-history-retry]').hidden = saved.retryHidden;
        removeLoadedSelectedAside(state);
        return true;
    }
    async function loadOlder(state) {
        if (!current(state) || state.pageController) return;
        const url = sameOriginUrl(state.timeline.dataset.olderUrl);
        if (!url) return;
        const controller = new AbortController();
        state.pageController = controller;
        const status = state.root.querySelector('[data-stored-history-status]');
        const retry = state.root.querySelector('[data-stored-history-retry]');
        if (storedEvents(state.timeline).length >= savedRowLimit || state.savedPages >= savedPageLimit) {
            state.timeline.dataset.olderUrl = '';
            status.textContent = 'Message limit reached.';
            retry.hidden = true;
            state.pageController = null;
            return;
        }
        status.textContent = 'Loading earlier saved messages…';
        retry.hidden = true;
        const valid = () => current(state) && state.pageController === controller;
        try {
            const response = await fetch(url, {
                method: 'GET', headers: { 'HX-Request': 'true', 'HX-Target': 'inbox-thread', Accept: 'text/html' },
                credentials: 'same-origin', cache: 'no-store', signal: controller.signal
            });
            if (!valid()) return;
            if (authorizationLost(response)) { clearPlatformAccess(state); return; }
            if (response.status === 400) {
                state.timeline.dataset.olderUrl = '';
                status.textContent = 'The earlier saved-history position could not be accepted. Reopen this conversation to refresh it after saving your draft.';
                return;
            }
            if (!response.ok) throw new Error('history_unavailable');
            const html = await response.text();
            if (!valid()) return;
            const parsed = new DOMParser().parseFromString(html, 'text/html');
            const pages = parsed.querySelectorAll('[data-stored-timeline-events]');
            const page = pages.length === 1 && pages[0];
            if (!page || page.dataset.timelineAnchorId !== state.anchor || !page.dataset.timelinePageKey) throw new Error('history_scope');
            if (state.pageKeys.has(page.dataset.timelinePageKey)) {
                state.timeline.dataset.olderUrl = '';
                status.textContent = 'Earlier saved history did not advance. Reopen this conversation to refresh it after saving your draft.';
                return;
            }
            const olderUrl = page.dataset.olderUrl ? sameOriginUrl(page.dataset.olderUrl) : '';
            if (page.dataset.olderUrl && !olderUrl) throw new Error('history_url');
            const earlier = storedEvents(page);
            const knownIds = new Set(storedEvents(state.timeline).map(element => element.dataset.eventId));
            if (!earlier.some(element => !knownIds.has(element.dataset.eventId)) && olderUrl) {
                state.timeline.dataset.olderUrl = '';
                status.textContent = 'Earlier saved history did not advance. More saved activity may remain; reopen after saving your draft.';
                return;
            }
            if (earlier.some(element => !element.dataset.eventId)) throw new Error('history_events');
            const newIds = new Set(earlier.map(element => element.dataset.eventId).filter(id => !knownIds.has(id)));
            if (storedEvents(state.timeline).length + newIds.size > savedRowLimit) {
                state.timeline.dataset.olderUrl = '';
                status.textContent = 'Message limit reached.';
                return;
            }
            const anchor = scrollAnchor(state);
            const beforeHeight = state.scroller.scrollHeight;
            const beforeTop = state.scroller.scrollTop;
            restoreTimeline(state.timeline);
            const existing = new Set(storedEvents(state.timeline).map(element => element.dataset.eventId));
            const first = state.timeline.firstChild;
            earlier.forEach(element => {
                if (existing.has(element.dataset.eventId)) return;
                existing.add(element.dataset.eventId);
                const copy = document.importNode(element, true);
                state.timeline.insertBefore(copy, first);
                // Imported saved links must keep HTMX's draft confirmation and
                // selected-message navigation behavior.
                if (window.htmx && window.htmx.process) window.htmx.process(copy);
            });
            storedEvents(state.timeline).forEach((element, index) => { element.dataset.storedOrder = String(index); });
            state.timeline.dataset.olderUrl = olderUrl;
            state.timeline.dataset.historyComplete = page.dataset.historyComplete;
            state.pageKeys.add(page.dataset.timelinePageKey);
            state.savedPages += 1;
            removeLoadedSelectedAside(state);
            localTimes(state.panel);
            if (state.items) merge(state, state.items);
            status.textContent = '';
            keepAnchor(state, anchor, beforeHeight, beforeTop);
        } catch (_) {
            if (valid()) { status.textContent = 'Earlier messages could not be loaded. Try again.'; retry.hidden = false; }
        } finally { if (valid()) state.pageController = null; }
    }
    function initialize(options) {
        const failedRecovery = Boolean(options && options.recover);
        let recovering = failedRecovery;
        const previous = suspended;
        const root = document.querySelector('[data-native-thread]');
        if (!root) return;
        if (opened.has(root) && !recovering) {
            if (active && active.root === root && !active.userScrolledUp && active.scroller.clientHeight > 0) bottom(active);
            return;
        }
        const panel = root.closest('[data-inbox-panel]');
        const timeline = panel && panel.querySelector('[data-stored-timeline-events]');
        const scroller = panel && panel.querySelector('[data-inbox-scroll]');
        if (!timeline || !scroller || panel.dataset.selectedMessageId !== root.dataset.anchorId ||
            timeline.dataset.timelineAnchorId !== root.dataset.anchorId) return;
        // A successful draft/status render is the same activation even though
        // HTMX replaced every node. Do not turn it into another provider read.
        const rerender = previous && previous.panel !== panel && previous.anchor === root.dataset.anchorId && !previous.selection;
        recovering = recovering || Boolean(rerender);
        const response = options && options.detail && options.detail.xhr;
        const successful = !failedRecovery && !(options && options.detail && options.detail.successful === false) &&
            (!response || (response.status >= 200 && response.status < 300));
        const retained = rerender && successful && previous.observation && previous.scope &&
            previous.scope === root.dataset.nativeViewScope ? previous.observation : null;
        const saved = rerender && successful && response && previous.saved && previous.scope &&
            previous.scope === root.dataset.nativeViewScope &&
            (!response.responseURL || sameOriginUrl(response.responseURL) === previous.saved.action) ? previous.saved : null;
        cancel();
        restoreTimeline(timeline);
        opened.add(root);
        const state = { root, panel, timeline, scroller, anchor: root.dataset.anchorId, userScrolledUp: false,
            items: null, pageKeys: new Set([timeline.dataset.timelinePageKey]), savedPages: 1 };
        active = state;
        suspended = null;
        if (failedRecovery && previous && previous.panel === panel) {
            state.pageKeys = previous.pageKeys;
            state.savedPages = previous.savedPages;
            state.retentionBlocked = previous.retentionBlocked;
        }
        if (saved && !retainSavedHistory(state, saved)) {
            root.querySelector('[data-stored-history-status]').textContent =
                'History changed. Load earlier messages again.';
        }
        localTimes(panel);
        if (recovering) {
            state.userScrolledUp = retained ? previous.userScrolledUp : true;
            state.lastTop = scroller.scrollTop;
        } else bottom(state);
        state.onScroll = function () {
            if (!current(state)) return;
            const top = scroller.scrollTop;
            const movedUp = top < state.lastTop;
            state.lastTop = top;
            if (movedUp) state.userScrolledUp = true;
            if (movedUp && top <= 120) {
                state.lastWheel = Date.now();
                loadOlder(state);
                loadOlderNative(state);
            }
        };
        scroller.addEventListener('scroll', state.onScroll, { passive: true });
        state.onWheel = function (event) {
            // Short histories and an already-topmost viewport cannot emit a
            // further upward scroll event. A fresh wheel gesture still works.
            if (!current(state) || event.deltaY >= 0 ||
                (scroller.scrollTop > 0 && scroller.scrollHeight > scroller.clientHeight)) return;
            const now = Date.now();
            const newGesture = !state.lastWheel || now - state.lastWheel > 250;
            state.lastWheel = now;
            state.userScrolledUp = true;
            if (newGesture) { loadOlder(state); loadOlderNative(state); }
        };
        scroller.addEventListener('wheel', state.onWheel, { passive: true });
        if (recovering) {
            root.querySelector('[data-native-thread-result]').hidden = false;
            if (retained) {
                state.items = retained.items;
                state.nativeCursor = retained.cursor;
                state.nativePages = retained.pages;
                state.nativeCursors = retained.cursors;
                state.nativePageKeys = retained.pageKeys;
                merge(state, state.items);
                root.querySelector('[data-native-thread-status]').textContent = retained.status;
                root.querySelector('[data-native-thread-warning]').textContent = retained.warning;
                root.querySelector('[data-native-thread-warning]').hidden = retained.warningHidden;
                root.querySelector('[data-native-thread-refresh]').hidden = retained.retryHidden;
                root.querySelector('[data-native-history-status]').textContent = retained.historyStatus;
                root.querySelector('[data-native-history-retry]').hidden = retained.historyRetryHidden;
            } else {
                root.querySelector('[data-native-thread-status]').textContent = 'Temporary platform observations were cleared. Your draft and saved history are still here.';
                root.querySelector('[data-native-thread-refresh]').hidden = false;
            }
            if (rerender) {
                scroller.scrollTop = previous.scrollTop;
                const anchor = previous.scrollAnchor;
                const matches = anchor ? Array.from(timeline.querySelectorAll('[data-timeline-event]')).filter(element =>
                    anchor.id ? element.dataset.eventId === anchor.id : anchor.platformId &&
                        element.dataset.platformMessageId === anchor.platformId && element.dataset.eventDirection === anchor.direction) : [];
                const row = matches.length === 1 && matches[0];
                if (row && row.getBoundingClientRect && scroller.getBoundingClientRect) {
                    scroller.scrollTop += row.getBoundingClientRect().top - scroller.getBoundingClientRect().top - anchor.offset;
                }
                state.lastTop = scroller.scrollTop;
            }
            return;
        }
        refresh(state);
        bottom(state);
        if (window.requestAnimationFrame) window.requestAnimationFrame(function () {
            if (current(state) && !state.userScrolledUp) bottom(state);
        });
    }
    function suspendRequest(event) {
        const visible = active && scrollAnchor(active);
        const savedAction = active && !active.retentionBlocked && active.root.dataset.nativeViewScope && savedHistoryAction(active, event);
        const savedState = savedAction && active.savedPages > 1 ? {
            action: savedAction, timeline: active.timeline, pageKeys: new Set(active.pageKeys), pages: active.savedPages,
            firstPageKey: active.timeline.dataset.timelinePageKey,
            olderUrl: active.timeline.dataset.olderUrl, complete: active.timeline.dataset.historyComplete,
            status: active.pageController ? 'Earlier saved loading was interrupted. Scroll up or retry to continue.' :
                active.root.querySelector('[data-stored-history-status]').textContent,
            retryHidden: active.pageController ? false : active.root.querySelector('[data-stored-history-retry]').hidden
        } : null;
        const observation = active && active.items && {
            items: active.items, cursor: active.nativeCursor, pages: active.nativePages,
            cursors: new Set(active.nativeCursors), pageKeys: new Set(active.nativePageKeys),
            status: active.root.querySelector('[data-native-thread-status]').textContent,
            warning: active.root.querySelector('[data-native-thread-warning]').textContent,
            warningHidden: active.root.querySelector('[data-native-thread-warning]').hidden,
            retryHidden: active.root.querySelector('[data-native-thread-refresh]').hidden,
            historyStatus: active.root.querySelector('[data-native-history-status]').textContent,
            historyRetryHidden: active.root.querySelector('[data-native-history-retry]').hidden
        };
        const previous = active ? { root: active.root, panel: active.panel, anchor: active.anchor, element: event.detail.elt,
            scope: active.root.dataset.nativeViewScope, observation, userScrolledUp: active.userScrolledUp,
            pageKeys: new Set(active.pageKeys), savedPages: active.savedPages, retentionBlocked: active.retentionBlocked,
            selection: Boolean(event.detail.elt && event.detail.elt.dataset.inboxOpenMessage),
            scrollTop: active.scroller.scrollTop,
            scrollAnchor: visible ? { id: visible.id, platformId: visible.platformId, direction: visible.direction, offset: visible.offset } : null } : suspended;
        clearSnapshots();
        if (savedState) {
            // Clone only after transient supplements and hidden-state markers
            // are removed. These bounded nodes remain in memory, never storage.
            savedState.rows = storedEvents(savedState.timeline).map(element => element.cloneNode(true));
            delete savedState.timeline;
            previous.saved = savedState;
        }
        suspended = previous;
    }
    function recoverRequest(event) {
        if (!suspended || active || event.detail.elt !== suspended.element) return;
        if (suspended.root.isConnected && document.querySelector('[data-inbox-panel]') === suspended.panel &&
            suspended.panel.dataset.selectedMessageId === suspended.anchor) initialize({ recover: true });
        else suspended = null;
    }
    document.addEventListener('click', function (event) {
        if (!event.target.closest) return;
        if (event.target.closest('[data-inbox-back]')) { clearSnapshots(); return; }
        if (event.target.closest('[data-native-thread-dismiss]')) {
            event.preventDefault();
            if (active) discardObservation(active);
            else clearSnapshots();
            return;
        }
        if (event.target.closest('[data-native-thread-refresh]')) {
            event.preventDefault();
            if (active && event.target.closest('[data-native-thread]') === active.root) refresh(active);
        }
        if (event.target.closest('[data-native-history-retry]')) {
            event.preventDefault();
            if (active && event.target.closest('[data-native-thread]') === active.root) loadOlderNative(active);
        }
        if (event.target.closest('[data-stored-history-retry]')) {
            event.preventDefault();
            if (active && event.target.closest('[data-native-thread]') === active.root) loadOlder(active);
        }
    });
    document.addEventListener('htmx:beforeRequest', function (event) {
        const element = event.detail.elt;
        const target = event.detail.target;
        if ((element && element.dataset.inboxOpenMessage) || (active && target &&
            (target === active.timeline || target.contains(active.timeline)))) suspendRequest(event);
    });
    document.addEventListener('htmx:beforeSwap', function (event) {
        const target = event.detail.target;
        if (active && target && (target === active.panel || target.contains(active.panel) || target.contains(active.timeline))) suspendRequest(event);
    });
    ['htmx:afterRequest', 'htmx:responseError', 'htmx:sendError', 'htmx:sendAbort', 'htmx:timeout', 'htmx:swapError'].forEach(name => {
        document.addEventListener(name, recoverRequest);
    });
    document.addEventListener('htmx:afterSwap', initialize);
    document.addEventListener('htmx:afterSettle', initialize);
    document.addEventListener('htmx:beforeHistorySave', function () {
        if (active) discardObservation(active);
        document.querySelectorAll('[data-stored-timeline-events]').forEach(restoreTimeline);
        document.querySelectorAll('[data-native-thread]').forEach(clearControls);
    });
    document.addEventListener('htmx:historyRestore', function () {
        clearSnapshots();
        const root = document.querySelector('[data-native-thread]');
        if (root) opened.delete(root);
        initialize();
    });
    window.addEventListener('popstate', clearSnapshots);
    document.addEventListener('inbox:history-navigation', clearSnapshots);
    window.addEventListener('pagehide', clearSnapshots);
    window.addEventListener('pageshow', function (event) {
        if (event.persisted) {
            clearSnapshots();
            const root = document.querySelector('[data-native-thread]');
            if (root) opened.delete(root);
            initialize();
        }
    });
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize);
    else initialize();
}());
