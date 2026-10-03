# Conversation V2 phase 2: local reply coordination

## Exact slice and unchanged contracts

This phase builds and tests a local coordination contract on top of phase 1
(`a300c3d`). It does not connect a new provider send path. Existing inbox tools,
reply endpoints, inbound event schemas, work statuses, archives and UI retain
their existing behavior. In particular, old sends are **not protected by this
new coordinator**. No production-ready automatic reply guarantee is implied.

Both `INBOX_CONVERSATION_V2_ENABLED` and
`INBOX_REPLY_COORDINATION_ENABLED` must be enabled; both default to false.
Turning either off disables the new coordinator and its read-only MCP tool.
There is no flag that enables external dispatch in this slice.
The internal coordinator additionally requires the exact account's capture
enrollment. Its MCP projection requires read enrollment too. Empty enrollment
is disabled, not a fallback to all accounts. See the
[scoped rollout contract](CONVERSATION_V2_SCOPED_ROLLOUT.md).

The new local objects separate coordination from history and old inbox work:

- `ConversationWorkState`: one verified conversation's latest eligible inbound,
  burst timing, generation, owner pause and active operation
- `SendOperation`: a durable idempotency key, immutable draft identity/version,
  local reservation and explicit outcome state

No model status means a legacy inbox message is resolved. An observed outgoing
does not identify its human/AI author or the incoming message it answers.

## Burst and handoff contract

Only exact workspace/account/platform/conversation identity is used. Names,
handles, similar text and nearby timestamps are never conversation keys. A
one-to-one peer must remain verified and unambiguous. Group/unknown identities
fail closed rather than merge people or brands.

Live new inbound observations advance one pending burst. A short quiet window
is bounded by a maximum wait from the start of that burst. Replaying an existing
message does not indefinitely postpone the deadline. Historical backfill does
not start work or replay events. Canonical message UUIDs identify targets;
reliable provider timestamps order candidates only inside an already verified
conversation. A newly observed older incoming cannot replace a later known
target. An unknown timestamp or tied timestamp on different messages creates a
durable ordering hold instead of guessing the latest target from arrival or
UUID. Ordinary resume does not clear that hold; explicit reconciliation is
deferred. The ledger retains original timestamps and observation order.
This minimum implementation keeps an ordering hold even if a subsequently
observed message has a later timestamp. Earlier timestamp ties can therefore
require explicit reconciliation too; automatic narrowing to a unique newest
candidate is a later usability improvement, not an implemented guarantee.

An outgoing known to predate the current inbound target still invalidates stale
draft context, but does not imply that this later question was answered or
permanently pause it. Newer, tied or unknown-time outgoing is a conservative
interruption. This distinction keeps newest-first history batches usable without
treating late arrival as a newly authored reply.

Local defaults are a 5-second quiet window, a 30-second maximum wait and a
30-second dry-run claim lease. These are stored/checkable deadlines only; no
scheduler wakes or sends a reply when a deadline expires.

Changed history, identity, owner pause or observed outgoing invalidates stale
draft assumptions. Owner resume requires a newly checked draft; it does not
resurrect a superseded operation. Native outgoing is treated conservatively as
an interruption, without claiming it was written by a human.
An existing outgoing-observed pause is not automatically cleared by later
incoming. Explicit owner handoff remains required in this conservative slice.

In this minimum slice, resume is prospective: it clears the previous burst and
only a subsequent new inbound starts another burst. Incoming messages observed
while paused remain in history, but resume does not automatically requeue them.
An explicit, freshly version-checked requeue-current-target action is deferred;
this avoids accidentally answering an old question after native outgoing.

If an uncertain operation's history is reassigned to another conversation, the
operation remains at its original identity and the destination is durably
quarantined. Ordinary resume, another incoming, another idempotency key, or
deleting/reassigning the moved message cannot clear that quarantine. Explicit
identity/outcome reconciliation is a future action, not an automatic timeout.

Disabling the coordinator does not erase existing uncertainty. An identity move
still preserves existing quarantine as a safety-only hold with no due work.
On reactivation, missed ledger revisions create a durable `history_gap`; a new
observation or ordinary resume cannot silently declare the old work snapshot
current. Pure legacy-reference linkage advances the snapshot only when it was
already aligned, never across an existing gap. Multi-revision identity promotion
can conservatively require reconciliation too in this slice.

## Prepare and preflight contract

A caller supplies a stable idempotency key, scoped actor identity, exact target,
expected conversation revision and work generation. Repeating the same key and
payload returns the same operation. Reusing it for a different payload is a
conflict. An uncertain external outcome is never converted into a retryable
failure merely because a lease expired or the caller lost a response.

Local reservation rechecks identity, scope, current revision, current target,
pause state, due time and competing operation. Fencing tokens describe a local
reservation, not provider authorization. The phase contains no provider network
call, background dispatcher, new write MCP tool or writable public endpoint.
Preflight reports `local_state_valid` separately and always reports
`send_allowed=false`; unknown or failed sync is never send authorization.

The new `get_reply_coordination` MCP tool is observational: it returns current
generation, pause, deadline, safe target and active-operation status after
current workspace/account checks. It never returns draft text, idempotency keys,
actor identities, fingerprints or claim tokens. Reading cannot reserve, resume,
resolve or send. Existing `get_reply_context` continues truthfully reporting
`send_preconditions_enforced=false` for the existing send path.

## Activation gates and remaining work

1. Validate PostgreSQL row locks, simultaneous claims, pause/ingestion races,
   uniqueness, failure recovery and migration behavior in an isolated database.
   SQLite validates sequential functional contracts only.
2. Implement and review an explicitly enabled dispatcher/adapter and durable
   pre-attempt boundary. Ambiguous network outcomes require provider-specific
   reconciliation; do not invent provider idempotency support.
3. Verify IG Login and Facebook capabilities, permissions, freshness, timeouts
   and real echo/outgoing behavior separately. Partial sync success does not
   prove a conversation is fully current. Threads remains unsupported and gated.
4. Wire authorized human controls and AI orchestration deliberately. Pausing
   this coordinator does not currently stop legacy sends or native-platform
   activity. Review eventual UX without restoring the cancelled grouping UI.
5. Migrate consumers to one conversation-level job/response deliberately. The
   original per-message inbound events are still delivered unchanged; this
   phase does not silently coalesce or replace their external delivery.

Even a correctly locked local dispatcher cannot atomically coordinate with a
person sending directly in Instagram or Facebook. Last-moment sync, revisions,
single-flight and conservative uncertainty handling reduce that race, not
eliminate it. Live dispatch stays unavailable until these gates are met.

## Verification approach

Use anonymous synthetic histories, including an old outgoing, several short
incoming messages, an attachment-only share and a final incoming text. Cover
one bounded burst, final target, duplicate replay, maximum wait, pause/resume,
outgoing interruption, identity retraction, cross-brand isolation, idempotency
conflicts and unknown-outcome non-retry. Check default-off and unchanged legacy
tool schemas/events alongside the complete regression suite, lint, formatting,
types and migration drift. Report PostgreSQL-only skips and existing SQLite
incompatibilities explicitly; do not treat focused passes as a full result.
