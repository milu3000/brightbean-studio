# Conversation V2 phase 1

This is an additive, default-disabled DM history projection. It does not replace
InboxMessage work state, the existing UI, reply authorization, or MCP inbound
Events. It does not implement V2 send-version preconditions or guaranteed full
platform history.

## Scope

- Facebook Page Messenger and Instagram Login DM observations are retained in
  both directions when `INBOX_CONVERSATION_V2_ENABLED=true`.
- Native outgoing/echoes never become legacy inbound work or emit new-inbound
  notifications/events. No inbound parent is invented for outgoing messages.
- Real provider conversation IDs are scoped to workspace/account/platform.
  Exact verified one-to-one peers may bridge webhook/poll observations only
  while unambiguous. Unknown or ambiguous attribution stays null and readable
  in an explicit account-scoped unassigned query.
- Threads is represented by an explicit unsupported DM capability gate, not a
  guessed endpoint, an Instagram substitute, a scope expansion, or scraping.
  A verified future adapter requires implementation and contract tests.
- Saved native observations are not proof of human authorship or recipient
  delivery. Legacy local SENT records without provider IDs remain unverified.

## Data and rollback

Migration 0005 adds the conversation, canonical-message and stream-sync tables.
It does not rewrite legacy messages, archived statuses, assignments or drafts.
Provider message IDs deduplicate within account/platform scope. Account locks
serialize ledger ingestion with the existing send path. Deleted content stays
unavailable even after delayed provider payloads.

Use the opt-in `backfill_conversation_history` command only for a separately
reviewed local/database migration. Inspect its help and dry run first. It reads
existing local DM/reply rows; it is not remote platform history import. It never
replays inbound events or creates new legacy inbox work.

Disable `INBOX_CONVERSATION_V2_ENABLED` to stop V2 capture and hide the three new
MCP tools. Existing inbox tools, sends and inbound-event schema remain present.
Do not drop the new tables on rollback: they may contain native outgoing with no
legacy counterpart. Data captured while the feature is off is not magically
backfilled when it is enabled again.

## Read-only MCP tools

- `list_conversations`: optional `social_account_id`, `limit`, `cursor`. Returns
  identities in immutable creation order and an unassigned-message count.
- `get_conversation_messages`: either `conversation_id`, or explicit
  `social_account_id` plus `unassigned_only=true`; optional `limit`, `cursor`.
  Returns canonical observations, provenance, attribution, safe attachments and
  per-account DM-stream sync status.
- `get_reply_context`: legacy inbound DM `message_id`, optional `limit`.
  Returns the target, recent observations, original work status, sync caveats and
  whether a later timestamped outbound has been observed. Missing ledger or
  missing attribution is explicit; no observed outgoing does not prove nobody
  replied on Instagram.

All require current `use_inbox` plus workspace/account allowlist checks. Reads
never mark work as read/resolved, refresh providers, or authorize sending.
Existing `inbox.dm.received` IDs can be supplied to `get_reply_context` without
changing the event payload or replay behavior.

History uses signed cursors bound to principal, workspace, current account
allowlist and query. Pagination uses immutable first-observation time plus UUID,
with a captured upper bound excluding newly observed rows. This is observation
order, not necessarily provider-send order; `occurred_at` is separate and can be
unknown. Edits/attribution can change between pages, so inspect conversation
revision and re-read on changes. Cursors expire after 24 hours and cannot be
reused after a grant/filter change.

Page limits are 1–100 (default 50; reply context defaults to 20). Serialized
inner JSON is bounded to 64 KiB; individual observations are capped, with explicit
body/attachment truncation flags. A continuation cursor advances only past
returned rows. Long content can be truncated even for a single message: use its
provider/legacy reference when available; this phase does not offer full-text
chunk retrieval for arbitrarily long native messages.

## Freshness and capability limits

DM and comment observations have separate attempt/success/error state. A
successful poll is only partial account-stream observation, never evidence of a
complete conversation. This phase does not add all nested remote pagination or
reconstruct unavailable old Instagram history. Attachment metadata or a usable
link also does not mean the content was fetched or is still accessible.

The current send behavior remains in place. `send_preconditions_enforced=false`
is returned by reply context: no new claim, revision guard, unknown-outcome
reconciliation or human-handoff workflow has been implemented in this phase.
Native platform activity can arrive late, so even later local guards cannot make
external IG activity atomic with BrightBean sends.

## Verification before activation

Run full pytest, ruff check/format, mypy and migration drift checks. PostgreSQL
row-lock/concurrency cases require a real isolated PostgreSQL database; SQLite
functional passes do not establish locking guarantees. Before production,
separately verify IG and Facebook real inbound/native-outgoing/echo behavior,
account scopes, event non-replay, history coverage and flag rollback. A server
deployment alone does not refresh an installed MCP client's cached tool schema.

## Next phase: burst-aware replies

The user wants a sequence of short messages, a shared card and a final question
read together, rather than one reply per inbound event. Anonymous synthetic
fixtures cover both arrival orders, preserve original provider timestamps and
directions, include all six observations within bounded context, and preserve
the specified final target. Real private texts, people and assets are not copied
into the repository.

Actual event coalescing/send coordination is deferred: per verified conversation
short debounce plus a maximum wait, a single in-flight reply job, a fresh target
and revision check before send, and draft invalidation on human takeover/native
outgoing. Never merge across participants, accounts or brands. Phase 1 does not
implement these send guards or authorize sending; shared content alone is not
proof that a text reply is needed.
