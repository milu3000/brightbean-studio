# Canonical inbox browser regression

`pytest apps/inbox/tests/test_canonical_browser.py` includes a real, installed
Chrome/Chromium test. It uses Node's standard library and Chromium's CDP pipe;
there is no Playwright dependency, browser download, listener, or live app server.
`CHROME_BIN` or `CHROMIUM_BIN` may name an already installed executable.

The fixture is exported from an isolated Django test database with two synthetic
conversations and more than 500 messages. The real session views render the feed,
panels, cursor fragments, and a new saved DB message. Native reads and provider
dispatch are mocked and asserted unused. The browser intercepts every request
before network access and fulfills only this fixture and repository JavaScript.
Keyboard submissions return an intercepted 204; they do not run a send route.

The actual bundled HTMX and Alpine execute, along with all inbox controllers,
templates, attachment cards, and canonical CSS. Since the existing pytest job
does not build Tailwind, a small explicitly synthetic `base.html` supplies the
viewport shell and the required utility styles. These tests cover the canonical
fragment layout, not every production sidebar or Tailwind utility.

The DOM gate covers desktop/mobile layout, a visible composer during scroll,
localized time, fast conversation changes and delayed stale history, unsaved
draft confirmation, native quote selection/cancellation, real Enter/Ctrl+Enter
and Shift+Enter input, composition/IME guards, prepending history with delayed
image decoding, resize, bounded history continuation beyond 500 rows, Latest,
and DB-only refresh while reading older messages. An accepted conversation
selection also records DOM mutations to reject even a temporary stale panel or
read acknowledgement. Explicit body, attachment, and withdrawn-content viewers
use real session POST response exports with signed continuation; tests check no
prefetch, bounded Next, plain-text rendering, Close, and delayed-response cleanup.
The synthetic current-owner fixture also verifies that background refresh leaves
composer scope/observation tokens unchanged, visible Latest updates them only on
its animation frame, and a conflicting saved draft revision preserves local text
and quote while keeping Send held. Its competing draft is saved only in the test
database; browser submissions still return an intercepted 204.

In CI, a missing browser/Node, launch failure, CDP failure, timeout, or failed
assertion fails pytest. Nothing downloads or changes workflow permissions.
Locally, an explicitly known environment blocker can be recorded as:

    BRIGHTBEAN_BROWSER_BLOCKED_REASON='verified reason' pytest apps/inbox/tests/test_canonical_browser.py -rs

This is a skip stating **DOM assertions NEVER RAN**, not a pass. CI rejects this
override. Without an explicit blocker, a browser launch failure is always a
failure, even locally. Do not repeatedly launch a browser after a verified
sandbox denial.

The separate fixture-export test still runs when the local DOM gate is blocked.
Its Node `--validate-fixtures` mode validates data and dependencies only, and
explicitly reports that browser assertions did not run. Controller VM unit tests
in `tests/inbox_canonical_test.cjs` are also separate from real browser evidence.
