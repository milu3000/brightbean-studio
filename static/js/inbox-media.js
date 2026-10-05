/* Truthful failure state for platform-hosted previews, including HTMX swaps.
 * No fetch, download, cache, retry or URL transformation is performed here. */
(function () {
    "use strict";
    function fallback(image, failed) {
        if (!image || !image.matches || !image.matches("img[data-inbox-preview]")) return;
        const card = image.closest(".inbox-attachment");
        const note = card && card.querySelector("[data-inbox-preview-fallback]");
        image.hidden = failed;
        if (note) note.hidden = !failed;
    }
    function inspect() {
        document.querySelectorAll("img[data-inbox-preview]").forEach(function (image) {
            if (image.complete) fallback(image, image.naturalWidth === 0);
        });
    }
    if (!window.brightbeanInboxMediaListeners) {
        window.brightbeanInboxMediaListeners = true;
        document.addEventListener("error", function (event) { fallback(event.target, true); }, true);
        document.addEventListener("load", function (event) { fallback(event.target, false); }, true);
        document.addEventListener("htmx:afterSwap", inspect);
        document.addEventListener("DOMContentLoaded", inspect);
    }
    inspect();
}());
