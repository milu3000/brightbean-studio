# Inbox notification lifecycle

This implementation was reconstructed from the reviewed behavior contract. Its
verification results refer only to tests executed against this new source.

## Identity and transitions

One card belongs to a recipient, workspace, account, platform, domain and exact
conversation/thread. A name or body is never an identity key. Unknown DM identity
stays per-message; comments require an actual root/thread, not a whole post.
Content-free per-recipient message receipts prevent replay and identity-move
notifications. Existing assignment notices remain separate from incoming events.

New live incoming advances the card revision and becomes unread. More incoming
updates the same card, without another delivery. Reading acknowledges only the
exact rendered revision. Closing hides the notification without changing the
conversation workflow; a newer verified incoming reopens it. Echoes, history,
unverified canonical activity and duplicated events never reopen it.

Conversation workflow and the user's read cursor remain owned by the canonical
inbox. A late producer acquires account, conversation, membership and notification
locks in that order and inherits an already-acknowledged incoming generation.
A newer generation remains unread. Recipient membership lock order is stable.

## Read safety and history

Signed snapshots bind exact notification IDs/revisions to recipient and current
workspace. Single and bulk operations compare-and-swap those revisions. New rows
and concurrent new activity cannot be consumed by an older snapshot. Current
membership and inbox permission are rechecked; workspace selection bounds bulk
changes. JSON compatibility callers capture a request-start snapshot.

The additive migration preserves original notification rows, text, read flags,
timestamps and deliveries. Derived links choose one representative and retain
all older rows. Operational rollback must keep additive data/schema after new
live activity; no production data reset is part of this implementation.

## Fresh content and transient provenance

Every canonical or legacy-linked preview resolves its exact current canonical
source and uses the shared `canonical_content.visible_content` policy. Expired,
withdrawn, rebound or otherwise unproven content never falls back to a stale
stored or legacy body. No restricted recovery body is accessed.

Only verified withdrawal and expiry are sticky. A canonical incoming signal can
precede durable provenance insertion within its transaction. Temporary
`provenance_unverified` suppresses the stored preview at that instant but must
not become a permanent restriction. Rendering rechecks the canonical source
after commit. Existing transient markers are also re-evaluated.

Verified restriction signals redact denormalized previews without changing read,
close, grouping or receipt state. An old message restriction cannot hide a newer
message's preview. This feature adds no outward email/webhook delivery or DM send;
existing queued delivery templates use the same safe body projection.

## Validation gates

Synthetic tests cover render/405 behavior, grouping, replay, read/close races,
workspace revocation, human labels, migration preservation, fresh visibility and
the signal-before-provenance regression. PostgreSQL concurrency and integration
with the rebuilt canonical reader/sync modules remain explicit separate gates.
Never test “Mark all read” against the user's production state.
