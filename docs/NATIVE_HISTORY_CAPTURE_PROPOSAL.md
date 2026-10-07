# Proposed saved native history scope

This is a design for a later, separately approved change. The current interactive
history candidate does not enable any capture enrollment, save native message
content, or activate background synchronization.

## Intended outcome

A selected account's supported one-to-one conversations, including conversations
started in the native app, appear in the same inbox on later visits and devices.
Incoming messages, native account-side observations, BrightBean receipts, notes,
and drafts keep their distinct provenance and meaning. Seeing a native message
never establishes which person/app authored it or grants permission to send.

## Minimum account and data decision

Start with exactly one user-selected account, pinned by workspace UUID, account
UUID and platform (`instagram_login` or Facebook Page Messenger). An account name
alone is insufficient. No wildcard enrollment or automatic inclusion of other
brands/accounts is allowed. Existing caller workspace/account grants remain the
read boundary; no new OAuth scopes are implied.

The decision must explicitly cover receiving and storing both directions' text,
provider conversation/message IDs, participant identifiers needed for safe direct
classification, timestamps, content availability, and attachment/share metadata
and URLs. Media files are not downloaded or cached. Unsupported group content
is not promised as complete group history. Read failures, deleted content,
provider retention and missing pages remain visible coverage limits.

The current ledger has no automatic content-expiry policy. Adopting it unchanged
would retain captured native content until an authorized product deletion, subject
to existing unresolved-send protection. Do not describe this merely as a temporary
read or silently invent a new retention period. The account decision must disclose
this persistence and how deletion/disconnection works.

## Historical import

Recommend a separately bounded first import, at most the previous 30 days for the
single approved account, then new observations going forward. This proposed range
is not authorization. Preview account identity, date range and available coverage
before executing the import. Earlier history remains an explicit further decision,
not an unbounded crawl. Provider limits may prevent even the bounded range from
being complete. Imported history must not create incoming work, alerts, automatic
replies, or artificial BrightBean delivery receipts.

## Required implementation before enabling

- Project enrolled ledger rows into the web timeline and account-scoped API/MCP
  views, deduplicating provider IDs against saved incoming rows and sent receipts
- Drive the conversation list from a scoped conversation projection so an
  outgoing-only thread can be listed without fabricating an inbound message
- Record synchronization freshness, partial coverage and bounded backfill progress;
  provider failure must not look like an empty or unanswered conversation
- Keep collection authorization, read grants, send ownership, and dispatch eligibility
  distinct. Current capture enrollment also changes which DM safety path is used;
  enabling flags alone is therefore not a complete or safe product migration
- Test poll/webhook duplicates, native and BrightBean echoes, interrupted import,
  account moves/revocation, data deletion, and simultaneous sending/reconciliation
- Preserve existing drafts, unresolved outcomes, account-level safety holds and
  one-sender guarantees. No send retry or new recipient is implied by capture

## Rollout and acceptance

Test first with synthetic data and PostgreSQL concurrency checks. Deploy code
with enrollment still empty. After the one-account decision, enable only the exact
capture/read tuple on both web and worker, with dispatch/coordination activation
remaining a separate decision. Verify real received, native-sent and BrightBean-sent
messages, outgoing-only threads, media/share fallback, ID deduplication, reload and
cross-device persistence, and coverage labels. Use only separately authorized real
send content/recipients; a read acceptance is not permission to send test messages.

On failure, stop new capture and reads for that account, preserve already stored
history and delivery receipts, and repair forward. Disabling a flag does not erase
stored content or undo in-flight reads. Do not roll back to a schema or sender that
cannot preserve the new safety receipts, and do not restore an older database over
new business data. Any deletion or new retention policy requires its own appropriate
approval rather than being hidden inside rollback.
