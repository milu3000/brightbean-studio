/* Shared by the split inbox and direct detail page. Only navigation GETs are
 * aborted: a submitted reply may already have reached the provider. Late
 * responses from any previous detail must never replace the current one. */
function inboxNavigation() {
    return {
        activePanel: 'list',
        detailVersion: 0,
        detailLoading: false,
        detailReady: true,
        detailError: false,
        detailUrl: '',
        detailRequests: new WeakMap(),
        pendingDetailRequest: null,

        isDetailTarget(target) {
            return target && (target.id === 'inbox-detail-panel' || target.closest('#inbox-detail-panel'));
        },

        beforeDetailRequest(event) {
            const { target, xhr, requestConfig } = event.detail;
            if (!this.isDetailTarget(target)) return;
            if (requestConfig.verb.toLowerCase() === 'get' && target.id === 'inbox-detail-panel') {
                this.cancelDetailNavigation();
                this.pendingDetailRequest = xhr;
                this.detailLoading = true;
                this.detailUrl = requestConfig.path;
            }
            this.detailRequests.set(xhr, this.detailVersion);
        },

        beforeDetailSwap(event) {
            const version = this.detailRequests.get(event.detail.xhr);
            if (version !== undefined && version !== this.detailVersion) {
                event.detail.shouldSwap = false;
                event.preventDefault();
            }
        },

        afterDetailRequest(event) {
            if (event.detail.xhr === this.pendingDetailRequest) {
                this.pendingDetailRequest = null;
                this.detailLoading = false;
                this.detailError = !this.detailReady;
            }
        },

        afterDetailSwap(event) {
            if (event.detail.target.id === 'inbox-detail-panel' &&
                this.detailRequests.get(event.detail.xhr) === this.detailVersion) {
                this.detailReady = true;
                this.detailError = false;
            }
        },

        retryDetail() {
            if (!this.detailUrl) return;
            htmx.ajax('GET', this.detailUrl, {
                source: '#inbox-detail-panel', target: '#inbox-detail-panel', swap: 'innerHTML',
            }).catch(() => {});
        },

        cancelDetailNavigation() {
            const pending = this.pendingDetailRequest;
            this.detailVersion += 1;
            this.pendingDetailRequest = null;
            this.detailLoading = false;
            this.detailReady = false;
            this.detailError = false;
            if (pending) pending.abort();
        },

        closeDetail() {
            this.cancelDetailNavigation();
            this.activePanel = 'list';
        },

        restoreDetailHistory(event) {
            // Match HTMX's history handler. Hash-only/native history does not
            // rebuild the page, so hiding its ready detail would strand it.
            if (event.state && event.state.htmx) this.closeDetail();
        },

        destroy() {
            this.cancelDetailNavigation();
        },
    };
}
