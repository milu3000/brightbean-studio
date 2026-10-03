# Conversation V2: synthetic dispatcher contract experiment

## Scope

This is a runnable **test-support-only experiment**, not a production dispatcher.
It uses the existing `ConversationWorkState` and `SendOperation` tables in a
disposable test database. Every account, history, draft, freshness assumption and
receipt is synthetic. No new migration, setting, default, endpoint, tool, event
schema, provider adapter, scheduler or UI is introduced. Runtime code does not
import the harness.

Run the focused contract with the repository's isolated PostgreSQL test setup
from [Maintaining the fork](MAINTAINING_FORK.md):

```sh
pytest apps/inbox/tests/test_synthetic_dispatch.py
```

The implementation is in
`apps/inbox/tests/support/synthetic_dispatch.py`. Tests exercise real canonical
ingestion, local preparation/claims and persisted state. The in-memory
`ScriptedTransport` is the only accepted transport; it has no network/provider
implementation. HTTP/provider entry points are additionally blocked in this
test module. Its request list is observational, never a deduplication store.
Never run the harness against production, copied customer data or live accounts.

PostgreSQL tests use separate thread-local connections, barriers and events.
SQLite may run sequential contracts with an explicitly isolated test settings
module, but skips every PostgreSQL concurrency/commit-visibility test. A SQLite
pass is not PostgreSQL proof. The complete regression suite, formatting, types,
migration drift and PostgreSQL CI remain separate release gates.

## What the experiment does

1. Existing canonical observations form a bounded burst and identify its latest
   eligible target. `prepare_reply` persists the draft, revision, generation,
   principal-scoped idempotency key and payload fingerprint. `claim_reply`
   obtains the existing local fence when the burst is due
2. `begin_attempt` checks the existing preflight and commits possible-effect
   evidence: `outcome_unknown`, `external_attempted_at`, and the explicit
   `synthetic_pre_attempt` code. It returns an immutable ticket without the body
3. `invoke` reloads exact workspace/account/platform/conversation/actor scope,
   verifies the ticket/fence, current target, revision, generation, lease and
   gate, then commits `synthetic_inflight` once. Only after this commit does it
   expose the synthetic request to the fake transport, with no DB locks held
4. `settle` reloads everything after transport. A typed fake confirmation or
   guaranteed-no-effect refusal can finish only that still-current operation.
   An unknown result, lost response, expired lease or changed context remains
   unknown. `recover` reads persisted evidence and never invokes transport,
   reclaims a lease, or grants retry permission

Both transitions require autocommit at entry and use an outermost durable
transaction. The harness explicitly rejects nesting, including Django TestCase
transactions and `ATOMIC_REQUESTS`: releasing a savepoint cannot establish a
committed attempt boundary. Tests use `django_db(transaction=True)` throughout.

The existing `check_before_send` remains unchanged and always returns
`send_allowed=false`. `SyntheticGate.COMPLETE` is an explicit invented premise
for this experiment, not a conversion of that denial into live authorization.
Unknown/failed gates are denied before any transport, including when account
sync reports success. Account sync does not establish complete thread freshness.

The caller must supply a freshly authorized `ReplyActorScope` for each call.
Persisted ownership, account health, capture enrollment and flags are rechecked;
the harness cannot discover principal grant revocation hidden in a stale caller
snapshot. A denial during settlement preserves the already-committed unknown
evidence without bypassing grants or returning the draft body.

## Failure and receipt semantics

| Boundary or outcome | Persisted interpretation | Retry behavior |
| --- | --- | --- |
| Crash before/during marker transaction | Original local-only claim; no attempt evidence | No automatic lease reclaim; explicit pause can supersede it |
| Crash after marker commit, before fake entry | Possible attempt, even if the fake never ran | Unknown; no automatic retry |
| Crash during fake transport | Possible effect and lost response | Unknown across a fresh harness instance |
| Typed `synthetic_confirmed` | Synthetic operation confirmed; only its matching burst consumed | Same key returns original operation, never resends |
| Typed `synthetic_definitely_not_sent` | Fake guarantees no effect; operation failed | A new explicit intent/key may be prepared; same operation is not retried |
| Timeout, unknown or unexpected exception | Possible effect without sufficient evidence | Unknown; new keys cannot bypass the active hold |
| Pause, incoming, outgoing, identity or revision change | In-flight evidence retained; changed work not consumed | Late receipt cannot clear the newer generation or pause |
| Receipt after expiry, scope revocation or a completed unknown | Insufficient current authority/evidence | Remains unknown; no implicit reconciliation |

The pre-attempt and in-flight codes distinguish a committed planned attempt from
a committed one-time entry reservation. A crash after that reservation can still
leave zero fake calls; neither code proves that transport or a provider received
anything. Recording
uncertainty before transport deliberately admits false uncertainty after a
crash; claiming known failure would permit a duplicate in a real implementation.

The synthetic `confirmed`/`failed` statuses are labeled by their synthetic
outcome codes. They never create `InboxReply.SENT`, an outbound ledger message,
an event, or a resolved/archived legacy inbox item. A refusal is typed proof from
the fake, not an inference from an HTTP error or timeout. Provider-specific
reconciliation remains unimplemented. Even an immediate later callback after
unknown is held for reconciliation in this conservative experiment.

Settlement is intentionally not a reconciliation API. A repeated settlement of
an already terminal operation returns `synthetic_receipt_not_current` without
changing the persisted result. Use the scoped recovery snapshot to inspect the
original synthetic status. This does not imply a second transport attempt.

## Covered interleavings and limits

The anonymous sequence contains an old outgoing, short incoming messages,
an attachment-only share and final text. It verifies the short quiet window,
maximum wait, latest target and duplicate replay. Other sequential contracts
cover payload conflicts, restart identity, stale/forged tickets, expired claims,
revoked scopes, account/brand/platform/conversation/actor isolation, interruption
on either side of the marker and preservation of legacy delivery state.

PostgreSQL-only tests race workers at claims, durable boundaries and fake entry.
A separate connection must see `synthetic_inflight` while the fake is blocked,
then complete real pause, ingestion, identity-change or recovery transactions
before the fake returns. The late receipt must leave the changed work intact.
These tests use bounded waits and database lock/statement timeouts so holding
locks across transport fails rather than hiding the race or hanging CI.

There is necessarily a gap between the last committed local check and transport
entry. A pause committed before that check prevents fake invocation; a pause or
native activity after it cannot recall a request. Local locks cannot make native
Instagram/Facebook sends atomic with application state. This coordinator's
pause does not stop legacy send tools, external event consumers or native sends.

Production dispatch still requires the explicit adapter, last-moment real
freshness evidence, provider-specific reconciliation, deliberate migration of
all consumers/send paths, and separately approved account acceptance described
in [phase 2](CONVERSATION_V2_PHASE2.md) and the
[scoped rollout](CONVERSATION_V2_SCOPED_ROLLOUT.md). Synthetic test success or
deployment of these test files does not satisfy those live gates.
