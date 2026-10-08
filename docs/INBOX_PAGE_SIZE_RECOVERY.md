# Bounded Meta inbox page recovery

This change is a local follow-up to the canonical inbox source contract described
in [CANONICAL_READ_CONTRACT.md](CANONICAL_READ_CONTRACT.md). It does not enroll
accounts, change credentials, widen permissions or activate a deployment.

## Recognized failure

Recovery applies only to HTTP 400/500 with numeric Graph error code `1` and the
bounded message phrase `please reduce the amount of data`. Other provider,
permission and quota errors retain their existing error paths. Error bodies and
provider URLs are not copied into public sync status.

## Canonical durable pages

- Page limits are 50, 25, 10, 5 and 1. The existing checkpoint's per-page attempt
  count chooses the limit; no new database field is needed.
- A recognized rejection records `page_size_rejected` without committing any
  message, attachment, coverage or cursor. The next due attempt requests the
  same pinned edge and cursor, with the same identity and content fields.
- Recovery waits for the existing durable backoff and GET reservation. A
  conversation page uses one GET; a message page uses the existing metadata GET
  plus one message GET. No retry GET is hidden inside an adapter call.
- Rejection at limit 1 blocks the checkpoint. It does not repeat forever or
  become an empty successful response. Existing successful-page deduplication,
  cursor fencing and account revocation checks remain in force.
- Other transient retries also use the lower limit selected by the attempt
  count. A successful page resets attempts, so the following page starts at 50.
  Metadata-only failures cannot be resized and keep their provider-error path.
- Only validated provider edge exhaustion yields `provider_edge_ended`; that is
  not a promise of complete platform history or complete media.

## Legacy Instagram Login polling

The successful nested-request path remains compatible with its existing partial
coverage semantics. Only the recognized rejection splits that request into a
conversation edge and separate message edges.

- A single DM poll has a 40-GET maximum, including the initial nested request,
  all shrinking retries and any optional-content-field compatibility retry.
  Existing comment polling remains independent of that DM budget.
- Each refused edge shrinks within the same 50-to-1 ladder. Cursor and `since`
  filters stay attached to the same edge; a conversation cursor is never reused
  as a message cursor.
- Provider `next` URLs are validated but never followed. Requests reconstruct
  fixed Instagram routes using the validated cursor. Unexpected routes, changed
  filters, repeated cursors and exhausted budgets produce an explicit failure.
- Overlapping message pages deduplicate by conversation/message identity and
  conservatively merge saved attachment metadata and classification warnings.
  Partial-content fallback remains visibly partial.
- No accumulated DM prefix is returned if a later edge fails. This prevents a
  failed walk from advancing the legacy watermark past omitted messages.
  Accounts exceeding the bounded legacy walk still need durable synchronization;
  this fallback is not a replacement for its persisted checkpoints.

All new requests preserve enrollment restrictions. Missing/incomplete
participants remain unknown, groups do not become direct conversations, and no
reply permissions or messaging windows change.
