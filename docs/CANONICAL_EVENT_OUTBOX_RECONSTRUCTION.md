# Canonical MCP event outbox reconstruction

This local reconstruction keeps `inbox.dm.received` and its existing subscription
schema. It creates no subscriptions, callback grants, enrollment, or live capture.
All rollout flags retain their existing defaults.

- The actionable workflow signal is the only canonical enqueue source. Incoming
  read activity alone cannot emit automation. Backfill, repair, initial bootstrap,
  unknown/group messages, outgoing messages, and app-send echoes stay quiet.
- Outbox records are created inside the canonical write transaction. The actual
  legacy message UUID is preserved when linked; otherwise the actual canonical
  message UUID is emitted. No placeholder InboxMessage is created.
- `mcp_server.0002_canonical_event_outbox` adds a nullable protected canonical FK,
  makes the legacy FK nullable, requires at least one message FK, and records the
  canonical connection generation and message event revision. Existing records
  and subscriptions remain intact. No data backfill runs.
- Event IDs are opaque digests of event name, account, provider message ID, and
  subscription generation. Retries preserve ID and payload. Native identity,
  direction, direct-thread classification, occurrence time, canonical generation,
  and actor/subscription authorization are freshly checked. Existing old-format
  legacy events are recognized without replay even before a canonical link exists.
- Payloads contain only event metadata and message/workspace/account UUIDs.
  Canonical content and retained recovery bodies are never selected for payloads.
- Callback workers serialize account, subscription, then outbox access. Pending
  events pause without spending a send attempt when canonical source proof or
  reader availability is temporarily missing. The event is persisted even if the
  canonical reader flag is temporarily off. Delivery resumes only after the flag,
  read/capture enrollment, and fresh source checks permit it.
- Persisted withdrawal/expiry cancels undelivered pending or failed callbacks.
  Delivery rechecks restrictions even if a signal was missed. Historical legacy
  outbox rows must pass canonical checks when a row now maps to their native ID.
  Sent callbacks cannot be recalled; their reference is resolved under the
  reader's current authorization and content policy.

The canonical reader integration must resolve both emitted UUID forms through
`get_inbox_message`. This change does not change that tool's implementation.

Verification uses synthetic fixtures, in-memory SQLite, a scrubbed environment,
and `offline_guard` blocking Python network connections. Coverage includes atomic
rollback/retry, quiet history, unknown-to-direct promotion, legacy ID compatibility,
subscription revocation/generation, source-proof recovery, reader rollout pauses,
and persisted content restrictions. PostgreSQL row-lock tests are retained but
require PostgreSQL CI; SQLite cannot prove actual concurrency locking. The schema
has not been applied to a real database, and no callback or provider was contacted.

Fresh verification on this reconstruction (2026-10-07): all MCP tests plus the
legacy ingestion, durable page, and workflow recovery suites passed: 751 passed,
4 PostgreSQL-only skips. The canonical outbox suite contributes 50 synthetic
cases. Django system checks, migration drift check, Ruff, and `git diff --check`
also passed. The staticfiles warnings reflect this test checkout's missing
collected-static directory.
