# Interactive native conversation history

An Instagram or Facebook reply sent in the native app is not an `InboxReply`.
BrightBean's saved `replies` array and open/archived work status cannot establish
whether someone answered on the platform. The ordinary inbox poll deliberately
excludes native outbound messages unless separately authorized V2 capture is
enabled. This feature does not change that capture policy.

## One timeline, driven by the conversation being viewed

Opening an existing DM conversation starts one bounded platform read for that
selected conversation. The result joins saved history in one timeline: incoming
messages on the left, account-side messages on the right, and internal notes
clearly distinguished. Initial opening positions the view at the latest messages.
If the user moves upward while a read is pending, the result preserves that
position instead of pulling the user back to the bottom.

Scrolling upward requests one earlier saved-history page and, when the platform
provided a verified continuation, one earlier platform page. There is no timer,
prefetch loop, account-wide scan, or request caused merely by refreshing the
inbox list. Compact loading, incomplete-history, and retry notices replace the
separate manual-read block. Failed navigation can recover the current panel
without restarting a platform request automatically.

REST uses `POST /api/v1/inbox/{message_id}/native-thread/read`; MCP uses
`read_native_inbox_thread`. Both accept `limit` (1–100, default 50) and an optional
`continuation` returned as `older_continuation` by an earlier read. Actual pages
contain at most 20 provider messages. All three surfaces use the same reader.
Existing stored-message API tools still do not initiate a provider read.

## Scope and pagination

The caller must currently have `use_inbox` and access to the exact account and
workspace. Key/OAuth grants and membership are rechecked, along with the selected
message, account, credential and native conversation identity, before and after
the provider requests. Callers cannot supply a different platform thread ID or
URL. Missing identity, groups, conflicting identity, unsupported accounts or
unverifiable participants return no native content. The optional V2 ledger is
not exposed by this feature.

The first page makes one bounded GET to the fixed Instagram/Facebook API host.
An earlier page makes at most two: verify the exact conversation and participant
pair, then read that same conversation's messages edge. It uses the existing
credential, follows no redirects or provider paging URLs, refreshes no credential,
and creates no subscription, enrollment, or background task. A continuation is
issued only when the provider's own next-page metadata confirms the expected
host, API version, thread, messages edge, and cursor. Unsupported shapes stop
pagination with an incomplete-history notice. Meta's generic cursor contract is
implemented in its [official SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/api.py).

Continuations expire after 15 minutes and are signed and bound to the current
account, selected message, native thread, actor, credential context, and requested
limit. They are positions, never access grants. No raw provider URL, access token,
or message body is included. Each page repeats authorization and identity checks.
Every accepted provider row retains its identity, time and direction in the
output; body or attachment fields can be shortened with explicit flags. Invalid
or omitted row identities cannot be skipped while advancing. This avoids the old
gap where 100 fetched rows could be reduced to 50 before advancing a provider
cursor. Initial valid observations remain displayable if their ordering cannot
establish a safe continuation; later pages must prove forward progress toward
older messages.

The browser bounds temporary history to 500 native messages or 25 pages. Saved
history uses a separate signed keyset position, so newly arriving messages do not
shift older pages. Loading either source preserves the visible scroll anchor.
Exhaustion, limits, failures, and unsupported provider pagination never claim
complete lifetime history.

## Identity, delivery and privacy

Only a unique, exact provider message ID with matching direction can join a saved
event. Conflicting IDs/content or uncertain timestamps remain visibly distinguished;
text similarity is never identity evidence. A match keeps the saved receipt and
author, adding platform observation/media information without duplicating the
message body. Different observed times remain labeled by source. All displayed
dates use one browser timezone, stated in the read notice.

Native rows are observations, not BrightBean delivery receipts, and do not identify
which human or app wrote them. Reads never create `InboxReply` or
`ConversationMessage`, change work status, erase drafts, or overwrite the composer
target. The existing detail-page unread-to-open action remains separate. Native
reads cannot extend a reply window, clear an uncertain send, or release an owner.

Navigation discards the transient observations. Native cards and supplements are
cleared before the bundled HTMX library saves any page to browser history, including
cloned/restored DOM. Responses use `private, no-store`; ordinary API audit metadata
records action/outcome, never returned bodies or continuation values. No image or
attachment is downloaded into BrightBean storage, and rendered media links require
an explicit click. Links can expire or require platform sign-in.

This covers already identified, supported one-to-one conversations. It does not
discover outgoing-only conversations missing from the inbox, recover group history,
or provide continuous cross-device native synchronization. Persistent capture and
saved native history require a separate bounded account decision. No new OAuth
permission, database migration, capture enrollment or retention rule accompanies
this candidate.

## Verification boundary

Local synthetic tests cover cursor continuity and omission budgets, account/actor
scope, revocation races, fixed endpoint validation, no-store/no-write behavior,
combined timeline identity, paging, draft preservation, scrolling and HTMX history
cleanup. UTC and America/Los_Angeles runs catch mixed-timezone display regressions.
They do not prove that a live provider exposes all historical messages or accepts
the same paging route for every account. Real FB/IG continuation behavior and actual
mobile/desktop rendering must be verified on the released candidate. A provider
that does not supply the supported route remains explicitly unavailable for older
native pages rather than being silently treated as complete history.
