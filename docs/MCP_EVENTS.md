# MCP Events: inbound direct messages

Implementation target: [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events), checked October 2, 2026. The current integration uses MCP 2.0 protocol version `2026-07-28`. This implementation has offline contract/security tests; live ChatGPT event activation has **not** been tested.

**Activation gate:** the dual-era wire implementation and offline safety tests
are included in this branch. Production activation, connector rediscovery,
callback verification, and a real inbound-message wake/reply cycle remain
separate acceptance steps. `MCP_EVENTS_ENABLED=false` is still the default;
no real signing secret, OAuth grant, API key, or subscription is created by this
code. A live callback/automation must be authorized before activation.

With Events off, `server/discover` retains the deployed method-not-found fallback
and existing legacy tools work as before. With Events on, the same authenticated
endpoint serves MCP 2.0 requests and legacy clients independently.

## Contract

All calls use the existing authenticated `/api/v1/mcp` endpoint and its existing rate limiting/auditing.

- `server/discover` advertises `events: {}`, modern `2026-07-28`, and legacy `2025-03-26` when enabled. Every modern request carries `params._meta` protocol version and client capabilities, with matching `MCP-Protocol-Version` and `Mcp-Method` headers. Tool calls also require matching `Mcp-Name`, including the specified Base64 sentinel decoding.
- All modern results have `resultType: "complete"` and `_meta` server identity. Discovery and tool lists have `ttlMs: 0` / `cacheScope: "private"`; HTTP responses are private/no-store. Metadata is validated against vendored, dated official schemas, with no remote schema fetching.
- Missing/malformed metadata, invalid parameters and header mismatches return HTTP 400; unsupported versions return `-32022` and supported versions; unknown modern methods return HTTP 404 / `-32601`. Modern JSON-RPC batches are rejected before dispatch. Request methods cannot be executed as notifications.
- `initialize` always negotiates only legacy `2025-03-26`, even when its proposal is newer; modern per-request `initialize` is not a method. Legacy result shapes remain unchanged. Present browser Origins must match the endpoint or an exact `MCP_ALLOWED_ORIGINS` entry. Normal server clients may omit Origin.
- `events/list` returns `inbox.dm.received` only when the principal has `use_inbox` and an eligible account.
- Subscribe arguments require one `social_account_id` UUID. Lifecycle field types are validated before network calls. Unknown fields, non-DM event names, unsupported delivery modes, and replay cursors are rejected. Optional integer `maxAgeMs` is accepted but ignored because this emit-only event has no replay.
- `events/subscribe` accepts webhook `url` and client-supplied `whsec_` secret (canonical base64 of 24–64 bytes), plus optional `ttlMs`. The response has deterministic `id`, finite `refreshBefore`, `cursor: null`, and `truncated: false`. Default/maximum lifetime is 24 hours, also capped by the authenticating credential's expiry. `ttlMs: null` still receives a finite grant.
- `events/unsubscribe` accepts the original name, arguments, and webhook URL (no secret required), returns a complete empty result in modern mode, and is idempotent. It can cancel an owned subscription after account access or callback allowlist changes; it also works with event delivery disabled.
- Subscription identity includes API key ID or OAuth user + client application, the fixed workspace, normalized callback URL, event name, and canonical arguments. Different API keys, OAuth users, or OAuth clients cannot refresh/cancel each other's subscriptions. Cancellation can find the same owner/client's original account subscription after the active workspace changes; refresh still requires the matching current workspace. OAuth token refresh retains the identity while rebinding to the latest authenticated token checksum; bearer tokens are never stored here.

Events contain `eventId`, `name`, occurrence `timestamp`, `data`, and `cursor: null`. `data` contains only `message_id`, `workspace_id`, and `social_account_id`; private message bodies, sender names, access tokens, and instructions are excluded. Call the existing `get_inbox_message` tool to read authoritative contents under current permissions. Event time comes from the provider, not ingestion time.

## Configuration and operation

The feature defaults off, and the exact callback-host allowlist defaults empty (fail closed). A later authorized rollout requires:

1. Apply the `mcp_server` migration to the intended database.
2. Configure `MCP_EVENTS_ENABLED=True` and `MCP_EVENTS_ALLOWED_CALLBACK_HOSTS` with separately verified receiver DNS names. Entries are exact hostnames, not wildcards, suffixes, origins, or URLs. No production callback host is guessed or preconfigured.
3. Keep the application's existing `SECRET_KEY` and `ENCRYPTION_KEY_SALT` secure and stable. Existing AES-GCM encrypted fields protect signing secrets, callback URLs, and serialized outbox bodies. This implementation does not create new signing keys or grant credentials.
4. Run the existing `python manage.py process_tasks` worker. `post_migrate` registers the recurring `recover_mcp_event_outbox` task every 60 seconds using the project's existing registration helper. Enabling delivery later does not require a different queue system.
5. Refresh/rescan the installed MCP plugin connection so it observes server version `1.1.0` and the enabled discovery/event catalog. A code deployment alone does not update a cached connector capability snapshot. Verify that the connected app appears in the platform's event-source list and its schema discovery includes `inbox.dm.received` with required `social_account_id`. Do not create a scheduled polling substitute if it is absent. The platform may require the user to refresh/reconnect or update their installed custom plugin; use the actual plugin's current supported flow rather than inventing a version switch.
6. Explicitly authorize the account-scoped event subscription and its persistent callback access. ChatGPT supplies the callback and signing secret; configure only the verified exact callback hostname. Do not invent a host or paste signing secrets into chat, source, logs, or environment examples. This step is separate from deploying code and must not silently create a new credential/grant.
7. Verify the full live lifecycle in a staging account: discovery, challenge, matching/nonmatching inbound DM, token refresh/revocation, unsubscribe, and retry deduplication. Neither successful HTTP receipt nor these offline tests establish that ChatGPT completed a downstream task.

If an authenticated, correctly formed modern subscribe request has valid current
owner/workspace/account access and a well-formed signing secret but its callback
host is not allowed, error `-32015` includes `data.candidateHost` and
`data.requiresApproval: true`. The hint contains only that request's normalized
hostname, not its scheme, port, path, query, signing secret, or configured hosts.
Malformed requests, unauthorized principals/accounts, and malformed URLs receive
no candidate hint. No DNS lookup, callback, subscription creation, or allowlist
change occurs. The host is **unverified and unapproved**: confirm it came from the
intended platform setup, verify the destination, and obtain the required approval
before configuring it. There is no assumed fixed OpenAI callback hostname.

Optional `MCP_EVENTS_SUBSCRIPTION_TTL_SECONDS` lowers the default granted TTL. The code still caps grants to 24 hours and credential expiry. No callback is sent merely by enabling the feature or migrating an empty database.

## Delivery and recovery

Inbox ingestion calls `enqueue_inbox_event(message)` only for a newly inserted inbound DM and **inside the same database transaction** as that message. The hook atomically writes an `EventOutbox` row per matching active subscription, then schedules the background worker after commit. Rollback removes both rows and queued work; a failed task enqueue leaves a durable outbox row for recovery. Webhook/poll duplicates share inbox uniqueness and an outbox unique constraint on `(subscription, event_id)`.

Explicit history imports, pre-subscription timestamps, malformed/unknown timestamps, implausible future timestamps, non-DMs, sent replies and self echoes do not trigger delivery. Both poll and webhook ingestion check known sent provider IDs scoped to the same workspace/account, in addition to echo/direction/own-sender markers. Duplicate DM polls preserve the first stored provider timestamp; an unknown original timestamp is not silently replaced with ingestion time. This event type has no replay protocol. Once a subscription expires or is stopped, a later resubscribe starts a new generation and never revives cancelled pending events.

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


## Sending policy and API behavior change

MCP `send_reply` and REST inbox send endpoints are automated/programmatic entry
points. Meta DM replies require a known timezone-aware original inbound timestamp
with age in `[0, 24 hours)` at dispatch. Unknown/naive/future timestamps and the
exact 24-hour boundary are rejected. Both new sends and failed/draft retries are
checked after acquiring locks and again immediately before provider dispatch.
Automated requests never use Meta's human-only `HUMAN_AGENT` exemption. The
explicit dashboard human reply path retains its existing human behavior.

This is an intentional tightening of API behavior, not a database migration:
clients previously relying on programmatic late replies must surface the error
and arrange a legitimate human response, not retry with a forged newer timestamp
or another transport. An unsupported provider must not be reported as a successful
automated delivery. Inactive users and archived workspaces are rejected by both
OAuth and API-key auth, including already-warmed API-key cache entries.

A per-account row lock serializes DM delivery/persistence with poll/webhook
insertion, so an unmarked echo arriving while a send is in flight is checked only
after the provider's outbound ID is saved. Message content is always untrusted
third-party data; it never authorizes tool use, account changes, external sharing,
or deviations from the user's SOP.

## Live acceptance and rollback

1. Deploy this exact tested commit to **both web and worker**. Mixed releases do
   not establish the complete send/ingestion/outbox safety contract. Run migrations
   through the existing release flow (this patch has no new model migration).
2. Check authenticated legacy tools with Events disabled, then modern discovery,
   tools/list, a harmless read tool, and events/list with valid modern metadata.
3. Refresh the plugin and verify event-source registration before claiming the
   feature is available. Absent source/schema is a platform activation blocker,
   even when direct endpoint checks pass.
4. After approval, subscribe only the intended social account. Verify challenge
   success and a stored active subscription under the correct user/workspace.
5. Ask the user for a **new** inbound test DM. Verify one original inbox row,
   one event ID, authenticated delivery, and arrival in the intended conversation.
   Fetch its authoritative body with current permissions; follow the approved SOP.
6. With the authorized reply, verify provider success and saved outbound ID, then
   verify its echo creates no new inbound row or event through webhook **and** poll.
   Check nonmatching account, duplicate, history import, expired/unknown time, and
   revoked access paths. Offline/mock success is not proof of this live cycle.
7. Verify cancellation stops delivery and keys are cleared. For rollback set
   Events false; a modern unsubscribe remains available even then. Recheck legacy
   tools. Do not claim that disabling the flag deletes existing subscriptions:
   they remain stored until unsubscribe or the expiry cleanup can run again.

The automation owner must preserve the user's exact reply authority,
recipient/account scope, and semantic conditions. The server emits identifiers
only; it does not itself choose or send the assistant's reply.

### Uncertain provider-send outcomes

If a provider accepts a message but its response is lost, or the process crashes
before the returned outbound ID commits, no local implementation can infer that
ID safely. The send remains failed/uncertain instead of being reported as
confirmed. Do not blindly retry a customer-facing reply after an ambiguous
network failure; inspect the provider conversation first. Normal provider echo
and own-sender markers still suppress outbound messages; a payload stripped of
all such evidence and carrying an unknown ID cannot be reliably classified.
The account lock closes the confirmed-response/persistence race, not this
unobservable remote-outcome problem. No exactly-once reply guarantee is claimed.
