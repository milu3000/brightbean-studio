# V2 dispatch ownership and current-actor send bridge

This change is stacked on the DM send gate in PR #11. It does not include the
synthetic dispatcher from PR #9 or the isolated timeline preview from PR #10.
The new code is an actual request-driven bridge to the existing Facebook and
Instagram Direct Login providers. Tests replace the provider HTTP boundary with
synthetic responses. No account is enrolled or enabled by this migration.

It is not an unattended scheduler, AI answer generator, subscription migration,
provider-history completeness guarantee, or finished production UI replacement.
Existing per-message Events, ingestion, inbox work and manual interfaces remain.
No old code or stored history is removed in this change.

## Why ownership is separate from the account gate

PR #11 serializes individual reply delivery and conservatively holds an account
after an unknown outcome. It does not deduplicate two independently created
successful replies to the same incoming message. During a cutover, running both
the old responder and a new responder without ownership can therefore send two
answers. A database account lock alone does not choose a response owner.

`DMConversationOwnership` explicitly pins one conversation, workspace, account,
platform/native account identity, observed peer and authenticated principal. It
starts paused, has a compare-and-set epoch, and retains a resume cutoff. Owning a
conversation does not grant an API permission. The principal must retain its
existing current inbox read/send permissions and account scope on every action.

For owned conversations, the existing UI, REST and MCP send boundary refuses
unbound legacy drafts. This remains enforced when rollout flags are turned off.
Where an account has ownership but an inbound cannot be safely attributed to an
unowned canonical conversation, legacy sending is held rather than treating a
missing link as a bypass. Explicitly unowned, correctly attributed conversations
retain their existing account-gate behavior.

## Rollout switches and operator controls

`INBOX_REPLY_DISPATCH_ENABLED` defaults to `False`. Request-driven dispatch also
requires both existing V2/coordination flags, exact capture and read enrollment,
an existing persisted account gate, and explicit conversation ownership. Turning
on the new flag does not create any of these enrollments or grant access.

The server-only controls in `apps.inbox.reply_dispatch` are:

- `enroll_conversation_owner`: enroll the current, freshly authorized principal
  against an already enrolled account gate; initially paused
- `set_conversation_owner_paused`: change the owner pause under the account lock,
  exact epoch and observed revision/generation; acknowledge only after commit
- `transfer_conversation_owner`: the current authorized owner transfers to a
  verified existing principal and leaves the conversation paused

These functions have no public enrollment/transfer tool or route. Operators need
separate authority for the exact account, conversation, principal and action.
They do not create tokens, OAuth grants, subscriptions or new account allowlists.
A non-owner management override and automatic lost-owner recovery are absent.
The account-level emergency pause remains available independently of V2 flags.

Resume and transfer retire pending work and fence old claims. Resume never
revives older targets; a later incoming observation is required. Unknown
operations and attempts survive pause, resume, transfer, lease expiry and flag
changes. There is no automatic reconciliation, force-clear, expiry retry or
deletion-based recovery procedure.

## Request-driven workflow

REST routes use the existing API-key authentication and permission intersection:

- `POST /api/v1/conversation-replies/prepare`
- `POST /api/v1/conversation-replies/{operation_id}/claim`
- `POST /api/v1/conversation-replies/{operation_id}/dispatch`
- `GET /api/v1/conversation-replies/{operation_id}`

The equivalent MCP tools are `prepare_conversation_reply`,
`claim_conversation_reply`, `dispatch_conversation_reply`, and
`get_conversation_reply_operation`. They are hidden from discovery and reject
cached calls when the bridge is disabled. MCP accepts its existing API-key and
OAuth actors; REST does not gain new authentication mechanisms.

Prepare requires the exact conversation/account/platform, target message,
observed revision/generation, owner epoch, body and stable idempotency key. Claim
requires due work and returns an operation-bound claim/fencing token. Dispatch
requires that claim, current owner epoch, current authorization and explicit
`acknowledge_observed_state=true`. Caller JSON cannot select another principal.
Principal identifiers are derived from authentication as `key:<key UUID>` or
`oauth:<user UUID>`; session operator calls use `user:<user UUID>`.

This acknowledgement accepts the stated limitation that native/external activity
may arrive later. It is **not** evidence of complete provider freshness, a
provider compare-and-send primitive, or absence of a native reply. Every result
continues to report `freshness_complete=false` and `external_atomicity=false`.
Automated dispatch always uses the normal automated reply-window checks and
never the HUMAN_AGENT extension.

`get_reply_coordination` exposes a safe observational owner epoch/pause/identity
readback, without returning owner principal or claim secrets. Operation reads
are restricted to the currently authorized operation principal and omit draft
body, idempotency keys and stored claim tokens. Reading never sends or resumes.

## Durable dispatch and result mapping

The service locks the account before conversation/ownership/work/operation and
reply rows. It verifies the current principal, owner epoch, generation/revision,
latest non-deleted canonical incoming, exact legacy mapping, timing, body and
claim. A caller-supplied actor snapshot alone does not authorize dispatch.

The existing DM gate commits the operation, reply and attempt's unknown marker
together before the provider call. It rechecks the same state and current grants
before entry, including after resolving provider credentials. A durable unknown
marker can mean a crash before HTTP; it does not prove that HTTP began.

Real provider acceptance settles the `SendOperation`, `InboxReply` and
`DMSendAttempt` in one transaction. Optional conversation projection or SLA
bookkeeping cannot make accepted delivery retryable. Accepted-target evidence
prevents another operation/idempotency key or fresh legacy draft from sending
the same incoming again, even if the optional outgoing projection fails.

Explicit known refusals remain known-not-sent. Timeouts, generic errors, invalid
response IDs, crashes and acceptance-write failures remain unknown and hold
further delivery. The confirmed result and protected receipt are distinct from
the synthetic PR #9 settlement; this bridge never substitutes ScriptedTransport
for a production provider.

Own confirmed application sends consume their work without introducing a new
human pause. Native or otherwise uncertain outgoing activity continues to pause
and invalidate future work. This does not infer who authored a native message.

## Data and rollback

Migration `0008` is additive. It preserves existing sent/draft/failed replies and
dry-run operations, creates no ownership rows, and changes no grants or flags.
New durable metadata consists of pinned identifiers, owner scope/epoch, cutoff,
row links and accepted target MID. It does not add raw provider responses,
credentials or a second message-body archive. Existing operation bodies remain
subject to their existing storage behavior.

Protected bidirectional operation/attempt links keep dispatched receipts from
being erased by ordinary ORM deletion. Ownership and referenced account/history
identities are protected. No retention expiry, purge or cleanup is added.
Migration reversal refuses once ownership or the new durable links exist.

After enrollment, code rollback to a sender that ignores ownership would bypass
the hold. Preserve the data and keep senders stopped or on a reviewed compatible
version. Turning off a flag is not a release of ownership and does not authorize
fallback to legacy delivery. Do not delete records or reset unknown to failed to
obtain a rollback.

## Acceptance boundary

Before any real cutover, verify exact source on every web/worker sender, drain
older processes and inventory external responders. Dual observation can coexist;
there must be a single authorized sending path. An empty local queue or zero
tracked unknown count does not prove external or historical work is absent.

After the integrated PostgreSQL and browser/operational gates, separately approve
one exact account, consenting test peer, short window, capture/read scope,
retention implications, explicit pause/resume and the intended real reply. Capture
can observe other platform-provided DMs in that account/window and retained
records do not disappear when flags are turned off. Test Instagram Direct Login
and Facebook separately. Stop on any unexplained unknown, duplicate, wrong
recipient, stale target or authority mismatch.

This draft's tests, push and CI do not authorize deployment, enrollment, new
grants, subscriptions, live sends, old-flow removal or broader data collection.

## Group identity and media observation boundary

Conversation and message observations now carry `direct`, `group` or `unknown`
plus a bounded evidence reason. Existing rows default to unknown; no historical
direct/group identity is invented. Sender/recipient endpoints prove direction,
not complete membership. Only a complete valid own-account/peer participant pair
permits direct classification. Missing, malformed, truncated, tail-page or
contradictory evidence is held as unknown. Confirmed group evidence is not erased
by a later text-only poll. Unknown/group observations cannot enter this V2
dispatcher or the owned-account legacy fallback.

The legacy `message_type=dm` filter remains compatible, while UI/REST/MCP display
the explicit conversation type. Per-message inbox rows are retained; this does
not restore the previously cancelled grouped inbox interface. A provider-native
conversation ID is never synthesized from a screenshot, sender name or timing.

Meta's [Instagram Messaging API](https://www.postman.com/meta/instagram/folder/uxudqu0/send-api)
does not support group messaging. The native app can show a group and media that
the connected API does not supply. These changes do not promise a native group
ID, title, complete member list or missing group photos. Where Instagram already
returns participant evidence, legacy reads retain only the bounded type/reason
summary; new full member-history capture is not enabled. No new provider field
request, OAuth scope, subscription or background media collection is added.

Message-level `content_status` is stored independently from actual attachment
metadata. Unsupported content, missing optional fields, partial content and
withdrawal remain visible even when some photos are present. Status-only
observations do not become fake counted attachments. Six distinct unidentified
photo metadata entries in one payload remain six; replays do not grow that count.
Safe payload previews are retained; broken previews show an explicit fallback.
No media bytes are fetched, downloaded, cached or retained by this change.

V2 history still returns a small attachment preview, with the retained metadata
count and `get_conversation_attachments` for bounded, read-only continuation.
Its cursor binds the current principal, exact enrollment, message and content
version. The retained count is not a claim of complete native media. URLs may
expire or require login; an available link is not proof that its contents were
read. Oversized metadata is explicitly marked and entire URLs are withheld,
never truncated into unsafe or nonfunctional links. Unsafe and credential-bearing
URLs remain excluded. Withdrawn content does not reappear after a stale poll.

Migration `0009` adds only bounded type/reason/content-status metadata. It does
not backfill identities or media, and refuses reversal while persisted dispatch
ownership exists. Production cutover still requires the acceptance gates above.

## Earlier ownership-only candidate verification (2026-10-05 UTC)

The following results describe the ownership-only candidate before the later
group/media extension. They are retained as provenance, not applied to untested
changes in that extension. Exact-head CI and the later aggregate supersede them.

The executable candidate was tested from an immutable source snapshot, with
synthetic identities, an isolated SQLite database, cleared inherited service
environment and an outbound-network guard. All 948 tracked candidate files
matched the source-tree manifest. This verification section was appended after
those runs; executable files are unchanged.

- New ownership/migration/request-adapter cases: **96 passed, 7 skipped**. The
  seven skipped cases require PostgreSQL and separate real database connections
- Full local aggregate: **3,528 passed, 22 skipped, 5 failed**. The 22 skips are
  21 row-lock/PostgreSQL cases and one existing non-Redis configuration skip
- The five failures were individually reproduced on unchanged PR #11 source
  `69bf853e201c0f1a70dc2ea6a64683b5246aee47` with the same SQLite configuration:
  JSON `contains`, PostgreSQL `pg_indexes`, `gen_random_uuid`, Django's deliberate
  refusal to close an in-memory SQLite connection, and the existing publisher
  concurrency test's SQLite table-lock error. They are not reported as passes
- Whole-project Ruff lint and formatting (495 Python files), mypy (615 source
  files), migration drift, Django system checks and diff checks passed
- Independent review: **8 offline adversarial probes passed**, including native
  outgoing pause/resume and the same API-key authorization callback's ability to
  tighten a hold after rollout/capture withdrawal. The two review findings were
  fixed and rechecked; no unresolved actionable code finding remained

The local executor prohibits socket creation, including Unix sockets, even for
an isolated PostgreSQL server. No network or security setting was changed to
bypass that limit. New PostgreSQL concurrency cases must execute in the normal
repository CI for the exact published head. Previous PRs' green runs do not
count as this candidate's PostgreSQL evidence. Docker build and gitleaks also
remain exact-head CI gates rather than claims based on the local SQLite run.

Production takeover remains **NO-GO** until exact-head CI, all-sender version
verification, operator enrollment/handoff acceptance, a bounded real-provider
trial and the intended consumer migration are complete. This bridge makes
request-driven dispatch possible; it does not install a scheduler, migrate an
external responder, ship PR #10's UI, or make native sends atomic with local
state. Do not remove legacy responsibilities merely because the bridge passes
its narrower tests.

## Integrated group/media candidate verification (2026-10-05 UTC)

The final integrated executable source was frozen and verified against all
**956 tracked files** before recording these results. Only this result section
was added afterward.

- Full isolated SQLite aggregate: **3,616 passed, 22 skipped, 5 failed**. The
  failures are the same five baseline SQLite limitations listed above, each
  reproduced separately on unchanged PR #11. The skips remain 21 row-lock/
  PostgreSQL cases plus the existing non-Redis configuration case
- All newly added committed test modules: **184 passed, 7 PostgreSQL-only
  skipped**, with no new-test failures. This includes 51 classification cases,
  independent content-status retention, attachment-page scope/cursor/size tests,
  and the original ownership/dispatch/migration cases
- Five dependency-free JavaScript behavior cases passed through the ordinary
  pytest suite. They verify failed/cached previews, load recovery, unrelated
  errors and repeated HTMX updates; they are not a real-browser visual pass
- Whole-project Ruff lint/format (500 Python files), mypy (621 source files),
  migration drift, Django checks and diff checks passed
- Group/media independent re-review: **13 offline probes passed** after fixes
  for incomplete participant tail pages, ownership-safe classification reversal,
  sticky partial-content evidence and oversized attachment responses. No remaining
  blocking finding was reported within that review's scope

The exact published head still requires PostgreSQL CI, gitleaks and Docker
validation. Real browser rendering, provider-visible media availability, native
group metadata and production takeover have not been verified by these local
tests. No real account enrollment, flags/grants/subscription changes, media
download, private payload logging, live DM, merge or deployment was performed.
