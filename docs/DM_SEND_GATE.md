# BrightBean DM send gate: local implementation and acceptance boundary

The later unified-inbox release also protects ordinary, unenrolled DM replies
through the existing InboxReply receipt. See [unified release and recovery](UNIFIED_INBOX_RELEASE.md).
The "unenrolled legacy lifecycle" description below records this gate's original
scope; it does not override the current shared receipt/uncertainty checks.

This account-scoped barrier covers BrightBean's classic inbox composer, draft
send, REST create-and-send / draft send, and MCP new / draft send. Those paths
use `apps.inbox.services.send_reply_now`. The supported enrolled platforms are
Facebook Messenger (`facebook`) and Instagram Direct Login (`instagram_login`).
Comments, posts, native Instagram/Facebook sends, third-party services and
external consumer queues are outside this boundary. This is not a conversation
coordinator or a replacement for the existing per-message Events delivery.

The migration enrolls no accounts. V2 capture/coordination settings remain
default-off and do not control this gate. Unenrolled accounts retain the legacy
send lifecycle. Once an account has a control row, toggling flags, changing its
platform/workspace or changing a key's allowlist cannot bypass that row. No new
OAuth permission, MCP control tool, automation, webhook or mutating UI/API is
included. No production enrollment or real provider send has been performed by
this local development slice.

## Persistence and send contract

Enrollment pins account UUID, workspace UUID, platform namespace and native
platform account ID. It starts paused, with an epoch and `coverage_from`.
`brightbean-dm-gate-v1` is the implemented coverage version, not a claim that
all deployed processes run this source. Initial enrollment never establishes
zero historical unknown sends.

The caller's already-authorized target and body are pinned before refreshing
rows or waiting for locks. The common service locks account then reply/message,
checks the persisted gate, current identity, target, content and authorization,
then commits an unknown attempt and reply status in a true outermost transaction.
It rejects an enclosing `atomic`, `ATOMIC_REQUESTS` or manual autocommit-off
context before network. Do not wrap enrolled calls in such a transaction.

Only the invocation that created that marker re-locks the account and target,
rechecks its epoch, content and current authorization, and enters the provider.
The account lock remains held through the single provider call, returned MID
persistence and commit. Ingestion uses that same account lock, so an unmarked
echo must wait for the returned outbound ID to commit. There are no automatic
provider retries in this path. A valid nonempty string MID of at most 255
characters is required to record acceptance. Optional ledger/SLA failures cannot
make an accepted send retryable.

The committed unknown marker means **possibly in flight**, including a crash
before HTTP. It does not prove that HTTP was entered. A timeout, invalid/empty
response ID, parse failure, generic provider exception, unsupported dispatch,
or failure to persist/commit acceptance leaves unknown durable. Any unresolved
attempt holds all new sends for that account, including a newly created draft,
a different input idempotency key, a later inbound or a resumed epoch. Expiry,
TTL, lease, flags and pause/resume never clear that uncertainty. Unknown replies
cannot be edited, discarded or removed by the classic failed-row cleanup.

Only the original invocation can record known-not-sent after its own preflight
refusal, before entering dispatch. In addition, the two supported one-POST
adapters' explicit HTTP 401/403 refusals, with the matching provider identity,
are classified as not sent. Other errors remain unknown, even if a human might
reasonably suspect rejection. Known-refused attempts can be retried with the
same body; recorded attempts and their replies remain protected from editing or
deletion. No outcome reconciliation, force-clear, test exception or manual
old-target override is provided in this slice.

Current dispatch authorization re-reads the active user, non-archived workspace,
membership and valid organization-scoped custom role. REST/API-key MCP also
re-read key expiry/revocation, permissions and account allowlist; OAuth MCP
re-resolves the original bearer and its current scope/expiry. There is no
implicit system actor: enrolled callers without current authorization fail
closed. No credential or raw provider response is persisted in gate metadata.

## Pause, resume and readback

Server-only functions `enroll_dm_send_control` and `set_dm_send_paused` are
internal operator contracts. The operator must have separate authority for the
exact account; these functions are not a new authorization surface. The pause
contract takes account/workspace identity and an expected epoch. It uses the
same account lock as dispatch and returns only after the actual commit. A
blocked lock, database error or stale epoch raises; it is not a pause success.

After every possible sender is verified on this version and a pause commits,
no new BrightBean DM provider call can begin for the enrolled account. A send
already in progress completes before pause acknowledgement. Provider-accepted
messages may still arrive later. Nothing here recalls a Meta delivery. A later
callback or new draft still encounters the persisted gate.

Resume advances the epoch and records a cutoff. Both the original inbound
`received_at` and its first local observation `created_at`, and the draft's
`created_at`, must be strictly later than the cutoff and no later than now.
Invalid/naive/future timestamps fail closed. Therefore ordinary old unanswered
messages, inbound observed while paused, stale drafts and delayed automation
creating a fresh draft for an old target all remain held. Resume does not mean
sending is possible while a tracked unknown remains.

Existing session `use_inbox` permission protects the GET-only readback route:
`/workspace/<workspace_id>/inbox/accounts/<account_id>/dm-send-status/`.
Foreign workspace/account identities are denied. For enrolled identities it
returns the committed pause flag, epoch, tracked unresolved count, coverage
version/start, resume cutoff, observation time and precise boundary labels.
The row and count are one SQL snapshot; the epoch/observation time describes
that observation, not future state. `legacy_coverage_incomplete=true` and
`external_consumer_queue=unobservable` remain explicit. Drafts are not queued
sends. No global readiness boolean or claim of historical zero unknown exists.

A bounded acceptance procedure is:

1. Verify isolated PostgreSQL migration/race tests and full regression gates
2. Under separately authorized release work, verify the exact source version on
   **all** web/worker senders, drain older processes and verify no unmanaged
   send path is active. Mixed versions cannot establish this gate's guarantee
3. With separately authorized enrollment of an exact account identity, enroll
   paused. Read back the committed pause, epoch, coverage and unresolved count
4. Confirm that UI/REST/MCP attempts are blocked and no new provider call starts.
   Existing attempts and external/native queues remain separately observable
5. Any real test/resume requires its own authorization and a newly observed,
   newly received target after resume. The current change includes no real
   account verification, read/capture enrollment, responder change or live send

## Migration and rollback

`0007_dm_send_gate` adds control/attempt tables and the 7-character `unknown`
reply state within the existing 10-character status column. Existing sent,
draft and failed rows are untouched. Existing public tool/request schemas are
unchanged. Attempt metadata is limited to exact row references, epoch, a digest,
status, safe reason code and timestamps.

Protected foreign keys retain attempt history and prevent account/reply deletion
from erasing uncertainty. The existing disconnect route rejects an enrolled
account **before** provider unsubscribe/revoke or orphan cleanup; it takes the
same account lock so enrollment cannot race past that check. No disconnect or
new credential action is performed. Other ORM cascades are stopped by PROTECT;
no cleanup/override facility is introduced.

Reversing the migration refuses when any control exists. Empty fresh installs
can reverse. Schema reversibility does not establish a safe operational
rollback: old code ignores these controls. Preserve control/attempt tables,
keep senders stopped or on a reviewed gate-compatible version, and never roll
back to a sender that ignores a committed pause. Deleting history, resetting
unknown to failed, or replaying old inbound work is not a recovery procedure.

## Verification limits

Tests exercise the actual shared services and real UI/REST/MCP adapters with
synthetic identities and stubbed providers. They cover unknown outcomes,
crash/persistence boundaries, current authority, identity changes, target cutoff,
classic cleanup, protected disconnect, migration over existing lifecycle rows,
and immediate unknown UI readback. Dedicated PostgreSQL-only tests use separate
connections for sender contention, pause ordering, initial enrollment and echo
serialization. SQLite skips those explicitly and proves only sequential
functional behavior. No production DB or real provider credential is used.
