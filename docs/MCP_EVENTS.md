# MCP Events: inbound direct messages

Implementation target: [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events), checked October 2, 2026. The current integration uses MCP 2.0 protocol version `2026-07-28`. This implementation has offline contract/security tests; live ChatGPT event activation has **not** been tested.

## Contract

All calls use the existing authenticated `/api/v1/mcp` endpoint and its existing rate limiting/auditing.

- `server/discover` advertises `events: {}` and `2026-07-28` when enabled. Legacy `initialize` continues returning `2025-03-26` unless the client explicitly requests the modern revision.
- `events/list` returns `inbox.dm.received` only when the principal has `use_inbox` and an eligible account.
- Subscribe arguments require one `social_account_id` UUID. Unknown fields, non-DM event names, unsupported delivery modes, and replay cursors are rejected.
- `events/subscribe` accepts webhook `url` and client-supplied `whsec_` secret (canonical base64 of 24–64 bytes), plus optional `ttlMs`. The response has deterministic `id`, finite `refreshBefore`, `cursor: null`, and `truncated: false`. Default/maximum lifetime is 24 hours, also capped by the authenticating credential's expiry. `ttlMs: null` still receives a finite grant.
- `events/unsubscribe` accepts the original name, arguments, and webhook URL (no secret required), returns `{}`, and is idempotent. It can cancel an owned subscription after account access or callback allowlist changes; it also works with event delivery disabled.
- Subscription identity includes API key ID or OAuth user + client application, the fixed workspace, normalized callback URL, event name, and canonical arguments. Different API keys, OAuth users, or OAuth clients cannot refresh/cancel each other's subscriptions. Cancellation can find the same owner/client's original account subscription after the active workspace changes; refresh still requires the matching current workspace. OAuth token refresh retains the identity while rebinding to the latest authenticated token checksum; bearer tokens are never stored here.

Events contain `eventId`, `name`, occurrence `timestamp`, `data`, and `cursor: null`. `data` contains only `message_id`, `workspace_id`, and `social_account_id`; private message bodies, sender names, access tokens, and instructions are excluded. Call the existing `get_inbox_message` tool to read authoritative contents under current permissions. Event time comes from the provider, not ingestion time.

## Configuration and operation

The feature defaults off, and the exact callback-host allowlist defaults empty (fail closed). A later authorized rollout requires:

1. Apply the `mcp_server` migration to the intended database.
2. Configure `MCP_EVENTS_ENABLED=True` and `MCP_EVENTS_ALLOWED_CALLBACK_HOSTS` with separately verified receiver DNS names. Entries are exact hostnames, not wildcards, suffixes, origins, or URLs. No production callback host is guessed or preconfigured.
3. Keep the application's existing `SECRET_KEY` and `ENCRYPTION_KEY_SALT` secure and stable. Existing AES-GCM encrypted fields protect signing secrets, callback URLs, and serialized outbox bodies. This implementation does not create new signing keys or grant credentials.
4. Run the existing `python manage.py process_tasks` worker. `post_migrate` registers the recurring `recover_mcp_event_outbox` task every 60 seconds using the project's existing registration helper. Enabling delivery later does not require a different queue system.
5. Connect/rescan the MCP plugin and explicitly authorize the desired event subscription. ChatGPT supplies the destination and signing key. This step creates persistent access and must be separately authorized.
6. Verify the full live lifecycle in a staging account: discovery, challenge, matching/nonmatching inbound DM, token refresh/revocation, unsubscribe, and retry deduplication. Neither successful HTTP receipt nor these offline tests establish that ChatGPT completed a downstream task.

Optional `MCP_EVENTS_SUBSCRIPTION_TTL_SECONDS` lowers the default granted TTL. The code still caps grants to 24 hours and credential expiry. No callback is sent merely by enabling the feature or migrating an empty database.

## Delivery and recovery

Inbox ingestion calls `enqueue_inbox_event(message)` only for a newly inserted inbound DM and **inside the same database transaction** as that message. The hook atomically writes an `EventOutbox` row per matching active subscription, then schedules the background worker after commit. Rollback removes both rows and queued work; a failed task enqueue leaves a durable outbox row for recovery. Webhook/poll duplicates share inbox uniqueness and an outbox unique constraint on `(subscription, event_id)`.

Explicit history imports, pre-subscription timestamps, malformed/unknown timestamps, implausible future timestamps, non-DMs, sent replies and self echoes do not trigger delivery. This event type has no replay protocol. Once a subscription expires or is stopped, a later resubscribe starts a new generation and never revives cancelled pending events.

Each send rechecks current user activity, workspace membership and `use_inbox`, account workspace/connection, and current key grant/allowlist/revocation/expiry or exact OAuth token user/client/scope/expiry. Any loss of access cancels pending delivery. A subscription's workspace is fixed even if the user's selected workspace later changes. Outbox processing locks the subscription before the delivery row: concurrent workers cannot simultaneously send a row, and unsubscribe waits for an in-flight send before returning. As with any webhook, revocation cannot recall a request already transmitted.

Delivery is **at least once**, not exactly once. Crash-after-receipt can retry; `eventId`, `webhook-id`, and exact serialized bytes remain stable, while each attempt gets a fresh signing timestamp and HMAC. Receivers must deduplicate `webhook-id`. Success is any 2xx. Timeouts, DNS/connection failures, 408/425/429 and 5xx retry with exponential backoff, starting at 30 seconds and capped at one hour, for at most eight normal attempts. The recovery sweep dispatches due rows in bounded batches. 410 stops the subscription; 413 and other permanent responses are not retried. Persisted failure reason/status/attempt count allows operator diagnosis without storing callback response content. A recurring sweep clears expired subscriptions' keys and old rotation keys.

Callback verification uses signed random challenges, 2xx plus constant-time challenge comparison, and a five-minute verification cache bound to principal/callback/signing key. Secret rotation requires verification, then signs with old and new keys for five minutes. Every verification/delivery validates HTTPS, exact allowlisted hostname, port 443, and all DNS answers. It connects to a validated public numeric address with original-host TLS/SNI verification, blocks redirects/proxies/private and special-use destinations, and caps request/response size and network time.

## Tests

Run `pytest apps/mcp/tests apps/inbox/tests/test_ingestion_events.py` using the project's normal PostgreSQL test settings. All HTTP/DNS/socket interactions in the callback suite are mocked; fixture secrets and OAuth tokens are explicitly synthetic. PostgreSQL-only concurrency regressions verify actual row-lock behavior; SQLite tests cover functional behavior but cannot establish concurrency guarantees.

## OAuth active-workspace continuity

The upstream OAuth tools use the user's currently selected dashboard workspace.
A subscription remains bound to the workspace/account that originally authorized
it, but switching the active workspace can prevent its later renewal and the
`get_inbox_message` fetch for that original workspace. Cancellation still works
across a workspace switch, and remains restricted to the same user and OAuth
client. Do not claim seamless multi-workspace OAuth event automation.

For stable background automation, use an explicitly authorized workspace-scoped
API key with the minimum `use_inbox` permission and account allowlist, or keep
the OAuth connection on its selected workspace and renew after refreshing the
access token. Creating a real key or grant is a separate operator-authorized
step; this branch and its mock tests do not create one.
