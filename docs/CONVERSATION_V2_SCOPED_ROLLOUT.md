# Conversation V2 scoped capture and read acceptance

This slice adds a controlled rollout boundary around the existing history
projection. It does not activate production history, send messages, change
OAuth permissions, replace legacy events, or connect a reply dispatcher.
Passing synthetic tests and deploying disabled code are separate from verifying
Instagram and Facebook with real account observations.

## Enrollment contract

`INBOX_CONVERSATION_V2_ENABLED` remains the master kill switch and defaults to
false. Two additional settings default to empty JSON arrays:

- `INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS`: accounts whose normal observations
  may enrich and update the V2 ledger
- `INBOX_CONVERSATION_V2_READ_ACCOUNTS`: accounts eligible for the new read-only
  tools, intersected with capture enrollment and the caller's current grants

Each entry pins all three values: `workspace_id`, `social_account_id`, and
`platform`. Only `instagram_login` and `facebook` are supported. Match exact
UUIDs and platform namespaces, never names, handles, similar content or nearby
timestamps. An account moved to another workspace or platform must not inherit
the old enrollment.

An empty list enrolls nobody. Invalid JSON, a wrong container, malformed entries,
unknown entry fields or unsupported platforms close that entire list. There is
no wildcard or fallback to every account. A read entry outside the capture set
does not grant access. Current `use_inbox`, workspace and account grants still
apply independently.
These settings are literal JSON strings, not environment-variable references;
malformed or recursively nested values must not prevent application startup.

This example is synthetic and does not authorize activation:

```dotenv
INBOX_CONVERSATION_V2_ENABLED=false
INBOX_REPLY_COORDINATION_ENABLED=false
INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS='[{"workspace_id":"11111111-1111-4111-8111-111111111111","social_account_id":"22222222-2222-4222-8222-222222222222","platform":"instagram_login"}]'
INBOX_CONVERSATION_V2_READ_ACCOUNTS='[]'
```

With an approved capture enrollment and master enabled, an empty read enrollment
is shadow capture: it saves eligible observations without exposing the new
history tools. Read acceptance is a separate, explicit enrollment step. All four
new V2 tools must be hidden during pure shadow, including coordination reads.
Handler checks remain authoritative for cached or direct calls.
Pure shadow does not provide a new MCP inspection surface. If no separately
authorized administrative read surface is available, the approved test must
include a later account-scoped read step to verify what was captured; do not
claim that capture was fully checked before it could be inspected.

Removing enrollment or disabling the master stops capture and denies reads in
processes running that configuration. Configuration rollout is not instantaneous
across old processes: verify both web and worker and drain old instances.
Preserve existing rows and uncertainty; never use rollback to delete history or
replay old inbox work. An already in-flight provider read cannot be recalled, so
writers recheck the refreshed account identity under the existing account lock.

## Boundaries that must agree

The account decision must cover provider request fields and outgoing retention,
polling, webhook ingestion, ledger writes, sync-state writes, successful local
send projection, local history backfill and internal coordination eligibility.
An excluded account retains legacy provider behavior. A projection failure must
not convert an accepted external send into a retryable failure.

The new reads intersect enrollment before querying conversation summaries,
unassigned counts, sync metadata, explicit message targets or history. Cursors
must become unusable when their effective enrolled identity or current grant
changes. Discovery is not authorization.
The SQL itself must bind the captured enrollment tuple and current account
identity. A platform move between permission lookup and the data query cannot
authorize a different namespace merely because it reuses the same account UUID.

The existing sender/echo/sent-ID defenses remain in force even when capture is
disabled. A previously observed outbound provider MID must not become a new
legacy inbound message merely because its account leaves the canary list.
Safety-only ID matching does not expose stored message content.

Local backfill remains a distinct, opt-in operation. Applying it requires an
exact enrolled account within the supplied workspace; preview performs no
writes. Counts must distinguish eligible source records from excluded records
and must not imply that every processed record creates a new row. Do not run
remote inbox backfill as a substitute for live acceptance.
The existing remote inbox command retains its distinct historical import
behavior, but must label observations as history so they do not start new V2
bursts. Notification suppression alone is not a reliable history marker.

## Synthetic verification

Use only invented accounts, provider IDs, texts and media URLs in repository
fixtures. Never commit real customer messages or private account identifiers.

- Verify master-off, empty lists, malformed configuration and unsupported
  platforms all fail closed
- Verify two accounts on the same provider remain isolated, including when the
  excluded account has otherwise valid workspace and principal permissions
- Exercise provider construction, polling, webhook, direct writer, sync state,
  app-send projection, both backfill entry points and coordinator gates
- Change enrollment or account workspace/platform between a provider request
  and the locked write; require no incorrectly scoped record or sync update
- Change platform between a read's scope snapshot and its SQL query; require
  denial for summaries, messages, legacy targets, sync and coordination views
- Keep capture-only data unreadable; then verify explicit read enrollment,
  revoked enrollment, cached tool calls and signed cursors
- Replay a known outbound MID after removal and master-off; require no inbound
  row, notification or received event
- Preserve archived work, unknown outcomes, history-gap and identity holds;
  native source is not evidence of a human author or a resolved conversation
- Retain the anonymous old-outgoing, short-message burst, attachment share and
  final-text fixtures in both observation orders

Run the complete regression suite, lint, formatting, types and migration drift
checks. PostgreSQL CI must cover actual row-lock behavior; SQLite results cannot
establish that guarantee. Synthetic payloads verify implementation behavior, not
an account's current platform permissions or webhook delivery.

## Before any real test traffic

Choose one explicitly approved account, workspace and test peer. Verify the
existing grant and observation behavior without adding scopes or reconnecting
accounts automatically. A `connected` label is not proof of messaging scopes:
Instagram grant introspection may be unknown, and a successful profile or
comment read does not establish DM access.

Check the exact account's existing received-event consumers before asking anyone
to send a test DM. The original `inbox.dm.received` contract remains active.
Shadow capture, the coordination flag and coordinator pause do not prevent an
existing external responder or legacy sender from replying.

A test marker alone is not proof that no reply will be sent. Establish an
observation-only rule for the exact test run and peer, or obtain approval to
pause the relevant account's responder and verify queued/in-flight work is
quiescent. Do not change automations implicitly. Do not use unsubscribe as a
temporary mute: it can clear signing secrets and cancel pending delivery, and
cannot recall a callback already delivered to an external consumer.
An attachment-only message has no text marker, so the no-send boundary must also
cover that verified peer and test window. Capture enrollment applies to all
platform-available DM observations for the selected account during the window,
not just messages containing a test marker.

## Live acceptance matrix

Run Instagram Login first, then Facebook Page Messenger independently. Passing
one does not establish the other. Keep an unrelated account excluded and use
synthetic or existing read-only evidence for that negative control; do not
generate extra real traffic on an excluded account.

| Step | Controlled action | Required observation |
| --- | --- | --- |
| Capture only | After approval, enroll one pinned account; reads and coordination remain off | Only that account can enrich/write V2 history; new V2 reads remain unavailable |
| Incoming burst | The owner/tester sends harmless short text, an attachment-only share, and a final text from the verified test peer | Original identities, provider times and directions survive; the final target is retained |
| Native outgoing | The owner replies once from the native business-account interface to that same test peer | One outbound observation; no invented inbound parent, new inbox item, received event or claim of human authorship |
| Natural duplicate observations | Observe normal webhook and poll paths as they occur | One canonical row per scoped provider MID; no artificial provider retries or history replay |
| Read acceptance | Under the approved inspection step, explicitly enroll only that test account for reads | Inspect capture results through bounded history and reply context, with partial/unknown coverage disclosed |
| Revocation | Apply the approved rollback and verify both process versions | Cached reads deny access, capture stops, rows remain, and re-enabling does not replay historical work |

Record sanitized IDs, directions, provider timestamps, first-seen times, source,
attribution, attachment availability, conversation revision and observable
inbox/event outcomes. Keep real evidence private. If a source path provides no
sample, mark it unverified; an absent webhook does not prove no native reply.

`get_reply_context` takes a legacy inbox UUID, not a Meta MID. The original event
already supplies that UUID. A native MID must be matched through an authorized,
exact workspace/account/platform lookup or through enrolled V2 history. Never
guess by display name or time. Missing attachments remain unavailable; metadata
or a URL does not prove that the media was fetched. Neither provider currently
guarantees complete conversation/message pagination or old-history recovery.

## Later dispatcher and UI dependencies

The next read-only UI can show a bounded timeline beside an existing inbox
message, using the same scoped read service, source labels and coverage caveats.
It must not create new work for outgoing messages or restore the cancelled
grouped-DM UI by default. New read panels must not silently resolve work.

Actual combined replies need a separate dispatcher with a durable pre-attempt
boundary, stable operation identity, short debounce plus maximum wait, one
in-flight operation, last-moment target/revision/freshness checks and conservative
unknown-outcome reconciliation. Native activity can still arrive late.

Human pause/resume controls must reach every applicable send path before they
can claim to pause assistant replies. Legacy sends and external event consumers
must migrate deliberately; adding a UI button or a new guarded endpoint alone
does not protect old send tools. Missing client revision must not silently bless
a draft created from stale context. Complete the broader PostgreSQL
pause/ingestion/claim race and recovery tests before live dispatch.

Local contracts, mock adapters and isolated UI work can progress without real
messages. Live enrollment, real test traffic, responder changes, expanded OAuth
access and actual sending require their own concrete approved test or rollout
scope. Nothing in this document activates them.
