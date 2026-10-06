# Unified inbox release and recovery

The inbox now presents already-stored direct-message history as one conversation
when an exact native thread ID exists within the same account and workspace.
Missing or malformed IDs remain separate. Sender names and handles never create
a conversation. Incoming records, sent BrightBean replies and internal notes
retain their original IDs, timestamps, status and assignment. Opening history
does not mark every message read or resolved, and filters/bulk selection retain
their explicit per-message meaning.

The composer names its incoming target. A newer arrival cannot silently retarget
an unsaved or failed reply. Existing drafts are reused or explicitly edited;
sent and unresolved receipts cannot be replaced by a second ordinary draft.
An explicit “Send another reply” action keeps the original incoming target and
selects a confirmed sent receipt as the parent of a new message. Delivery
state, message classification, attachments and missing-content explanations use
the same stored facts throughout the inbox, REST and MCP.

## Existing-data boundary

This slice enables no provider capture, enrollment, subscription, OAuth grant,
remote history import or new message-body store. Migration `inbox.0010` adds
only delivery metadata to existing replies: a nullable unique follow-up parent,
`is_follow_up` and `not_sent_verified` booleans, and an internal
`send_generation` counter. Existing rows default to false/zero;
no historical result is inferred or rewritten. Database defaults also
allow a compatible earlier ORM to insert records without those columns. Existing V2 capture/read and
ownership controls stay separately enforced and are never implicitly enrolled.

The ordinary Facebook conversation request now asks for `participants{id}` on
the same conversation edge it already polls. Only the bounded classification
and reason are retained when V2 capture is off; participant IDs/names/email and
native outbound history are not copied into those ordinary inbox records.
Instagram already requested participants. No new permission or history walk is
introduced. A specific unsupported-field response gets one narrower `id` retry;
missing evidence stays unknown, and authentication/quota errors are not retried.

REST `GET /api/v1/inbox/{message_id}/thread` and MCP `get_inbox_thread` share a
bounded projection of the existing inbox messages and BrightBean replies. They
check the current account allowlist and inbox permission before resolving the
anchor; cursors bind the caller, account, workspace, anchor and native identity.
These reads neither fetch providers nor expose internal notes. REST reply creation
and MCP `create_reply_draft`/`send_reply` accept optional `follow_up_reply_id` only
for an intentional additional message. It must identify an accessible confirmed
sent receipt on that same incoming target. The stored proof fields are read-only
in API/MCP responses; callers cannot assert a not-sent result. Truncated
previews identify the existing full-message read path. They explicitly report
incomplete native history and BrightBean-only outbound coverage.

Instagram group messages or media omitted by Meta cannot be reconstructed.
Unknown/group identity is shown honestly and cannot use the direct-message send
API. Links may expire; a supplied link does not prove media contents were read.
No server-side media downloading or caching is introduced. The optional manual
receipt lookup is an ephemeral read of the selected existing conversation, not
a new stored outbound history or recurring capture policy.

## One delivery boundary

All UI, REST and MCP sends still enter `apps.inbox.services.send_reply_now`.
Direct-message sends now require a supported provider and verified direct
identity even when the account has never been V2-enrolled. Current raw evidence
cannot be hidden by an older canonical projection. Deleted/outgoing targets,
conflicting group evidence, changed recipients, obsolete account scope, stale
authorization and automated reply-window violations fail closed.

For an unenrolled DM, the existing `InboxReply` is committed as `unknown` before
the provider call. A second transaction locks account then reply, rechecks the
original target/body/current authorization, and keeps that lock through provider
dispatch and receipt settlement. Each durable preparation increments a generation;
the original invocation must still own that generation at dispatch, so a reviewed
receipt followed by a new retry cannot be mistaken for an older attempt. The
callback rechecks after resolving provider
credentials. A crash or failed acceptance write leaves durable uncertainty.
Existing enrolled controls, attempts and owned operations retain their stronger
bindings and cannot be bypassed through the ordinary composer or legacy tools.

One incoming target has one reply intent. A matching draft can be reused; a
changed body requires explicit draft editing. Sent receipts prevent another
ordinary intent for that target. An explicitly selected confirmed sent receipt
may have one follow-up child; repeated clicks reuse that child, and a further
message must explicitly select a later sent receipt. Neither a retry nor a new
incoming message silently becomes this additional intent. A deleted parent
leaves `is_follow_up` set and prevents the orphan from sending. Uncertain delivery holds new DM sends for that account,
including across changed thread metadata or workspace reassignment. No retry,
lease expiry, flag change, discard or fresh draft clears uncertainty.

Historical DM `failed` rows are not assumed to be proven refusals: the old sender
used that state for generic exceptions. Without definitive existing attempt
evidence or the current service's `not_sent_verified=True` delivery metadata, they retain
their original state/error but are presented as delivery-unverified and held.
There is no migration that retags, erases or invents results for old records.

## Unresolved-history protection

The same uncertainty predicate controls sending, editing, discarding, ordinary
ORM cascades and account disconnect. An unresolved reply blocks account/message
deletion; disconnect returns 409 before provider unsubscribe/revoke or orphan
cleanup. Deletion and send use the same account-first lock order. This changes
when those existing records may be deleted; this protection and the explicit
manual review behavior must be included in the rollout approval. It adds no separate retention duration or expiry policy.

Definitively not-sent failures remain reviewable/retryable under their existing
attempt/ownership protections. A workspace manager who also has inbox-read and reply permissions can explicitly review an
unenrolled receipt in the inbox. The form has no preselected outcome. Confirmed
sent requires the platform receipt ID and aware delivery time; confirmed not
sent requires an explicit statement that non-delivery was verified on the
platform. Merely failing to find a message is not proof. Uncertainty can always
remain unchanged. A separate manager-requested lookup can read at most the first
100 messages of that one already-known native conversation using the current
platform grant. It does not discover other threads, follow pagination, refresh
credentials, subscribe, or save fetched bodies/attachments. Only up to five
matching receipt IDs/times are returned as candidates for explicit human review;
missing, ambiguous or unsupported results never prove non-delivery. Scope,
current permissions and receipt version are checked before and after the read.
The read itself never resolves the receipt or preselects confirmation.

The service locks account then receipt, checks the displayed
timestamp/generation and current permissions, and records the manual result and actor in an
existing internal note. This never sends or automatically retries a message.
A late original invocation cannot overwrite the reviewed result.

This manual path refuses any existing account control, attempt, ownership or
coordinated operation: it cannot release the separately enrolled gate. There is
no anonymous, API-key, MCP or generic force-clear endpoint. The application admin
makes receipt fields read-only so it cannot silently manufacture delivery proof.
Direct database administration can bypass application invariants and is not a
recovery procedure.

## Deployment checks

1. Confirm the exact tested source tree and all GitHub CI checks, including
   PostgreSQL cross-connection races, migration drift, gitleaks and Docker.
2. Confirm the actual BrightBean web/worker deployment pair and current
   non-secret capture/read flags. Do not rely on a template or deployment label.
3. Preserve the existing database and obtain a fresh recovery point using the
   already authorized backup capability. Do not change backup retention/PITR.
4. Inventory every sending process and external responder. Drain incompatible
   web/worker senders before admitting requests to the new boundary. An empty
   local queue is not evidence that an external responder is idle.
5. Run additive migrations before the new worker starts, verify web/worker both
   run the tested version, and keep all existing capture/read enrollments as
   authorized. No rollout instruction here enables V2 capture or ownership.
6. Check HTTP health, database/migration state, worker processing, scoped
   authenticated reads, the inbox and drafts, media fallback, analytics, and
   current send-eligibility reasons. HTTP `/health/` alone is insufficient.
7. Do not send a real test reply without an exact authorized recipient/body.
   Synthetic/provider-stub tests do not count as a recipient-visible test.

## Recovery must retain receipt enforcement

`INBOX_CONVERSATION_PRESENTATION_ENABLED=False` restores individual inbox
rows/detail presentation while keeping every send and retention protection.
This is a presentation fallback, not a data rollback.

`INBOX_DM_SENDS_ENABLED=False` is an emergency DM sending hold shared by all
interfaces. It preserves drafts/receipts, does not recall accepted delivery, and
does not resolve uncertainty or release ownership. Verify the hold on all
running senders after draining incompatible processes. It is not a substitute
for identifying and repairing the fault.

Before admitting the first new DM send, keep a reviewed fallback build that
retains the latest `reply_safety`, `receipt_retention`, reconciliation services,
follow-up/orphan interpretation, not-sent proof, send-generation fencing, and
existing enrolled/ownership gates. Retaining only database columns with an older
ORM or an earlier safety implementation is insufficient. Roll back presentation/other application code
only to that compatible build. A pre-release `8835d03` or unmodified PR14 sender
does not enforce the new unenrolled receipts and is not safe after they exist.
If the fault is in receipt enforcement itself, stop DM sending and repair
forward while preserving evidence; do not downgrade around the hold.

Do not reverse safety migrations after controls/ownership/attempts exist. Do not
restore a database snapshot merely to remove a failed deployment: that can erase
accepted messages and new business data. Any data restoration is a separate,
explicit recovery decision with its data-loss boundary identified.

## Verification record

The candidate retains PR14's tested integration of analytics and messaging.
New release checks must run on the final unified-inbox head; PR14's 4,030-pass
PostgreSQL result is historical evidence, not this release's test result.
Local SQLite runs explicitly skip PostgreSQL races and retain the known five
SQLite-specific limitations. Browser static-file loading and local server binds
are unavailable in this executor; template/JavaScript checks and a CSS build
must not be described as full browser or live-provider acceptance.

Provider read reference: [Meta Conversations API](https://www.postman.com/meta/messenger-platform-api/folder/22794852-255610cd-47f5-4f4d-b3fa-71aec360be9a). The new bounded lookup is covered by synthetic shape/scope tests; live provider acceptance remains a release check and unsupported fields fail unconfirmed.
