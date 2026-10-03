# Non-text inbox content

Inbox messages retain their existing database schema and independent message rows.
The `extra.inbox_attachments` JSON projection preserves attachment metadata across
webhook/poll ordering. It does not backfill or replay old messages, change an
existing message's status or original inbound timestamp, or enable MCP Events.

## Public REST/MCP contract

The shared inbox response now includes:

- `content_type`: `text`, `attachment`, `mixed`, or `unknown`
- `content_preview`: original body or a truthful short non-text label
- `attachments`: a bounded list with `type`, `url`, `title`, `preview_url`, and
  `availability` (`available` or `unavailable`)

`available` means a safe URL was supplied, not that its target was fetched or is
still accessible. Raw provider payloads, internal attachment IDs, access tokens,
and credentials are not part of this contract. Existing `body` remains unchanged
for attachment-only messages rather than inventing article text.

Only public-form HTTPS URLs without embedded credentials or known credential
parameters are accepted. Previews are restricted to Meta CDN hosts; other URLs
are explicit user-opened links. No server-side fetching, proxying, HTML embeds,
or unrestricted URL unfurling is introduced. Titles are escaped by Django.

## Meta contracts and graceful fallback

The implementation accepts current `ig_post`/`ig_reel` and legacy `share` webhook
attachments, plus image/video/audio/file/story variants. Duplicate legacy/current
representations are deduplicated by media ID or resource URL. Optional metadata
is not invented. A replay of an identical original webhook cannot overwrite a
newer signed URL obtained by polling.

The poll requests the documented Message attachment fields and explicit share
subfields. A specific Graph code 100 unsupported-field response retries once
with the original basic fields. Authentication, permission, rate-limit and other
errors are not hidden or retried as field-compatibility issues. Full expanded
queries are version-dependent; fallback keeps text ingestion working when a node
does not support the extras. It does not grant additional permissions.

Explicit deletion is retained as a tombstone so delayed old payloads cannot
resurrect content. An unknown deletion is archived silently with an unknown
inbound timestamp, never a new-arrival notification or fresh reply window.
Missing fields or missing URLs alone are never treated as deletion.

Official references checked 2026-10-03:

- [Instagram Login Conversations API](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/conversations-api)
- [Graph Message](https://developers.facebook.com/docs/graph-api/reference/message)
- [Instagram webhook examples](https://developers.facebook.com/documentation/instagram-platform/webhooks/examples)
- [Updated Instagram webhook types](https://developers.facebook.com/documentation/instagram-platform/webhooks/new)
- [Messenger message webhooks](https://developers.facebook.com/documentation/business-messaging/messenger-platform/webhooks/webhook-events/messages)

The Instagram guide limits detail retrieval to a conversation's most recent 20
messages and excludes long-inactive Requests conversations. Historic messages
whose raw metadata was never retained may remain unavailable. An unavailable
card does not claim that the sender deleted a message or that a post was private.
The standalone Message attachment/share edges are Pages-only; this implementation
does not use those edges for Instagram Login.

## Release checks

Run the full existing test, Ruff, formatting, mypy, Django system-check and
migration-drift gates. Focused tests cover mixed/pure shares, missing links,
malformed payloads, URL/HTML safety, typed REST/MCP parity, repeated ingestion in
both orders, original status/time and 24-hour reply-window preservation, echo
suppression, duplicate event suppression, and deletion-before-original delivery.
No database migration or data restore is needed for rollback to the prior build.
