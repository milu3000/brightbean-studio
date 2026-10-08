# Saved canonical inbox read contract

`apps/inbox/canonical_reads.py` supplies the saved conversation and message
projection used by the session UI, REST inbox-conversations routes and MCP
conversation reads. Reading never fetches a provider, acknowledges a message,
changes workflow or authorizes sending. MCP omits the UI's read acknowledgement
token. UI-only controls may decorate this common DTO without replacing its data.

## Identity and source

- A conversation `id` has `id_namespace: canonical_conversation`; a message `id`
  has `id_namespace: canonical_message`. A message `conversation_id` is the
  canonical conversation UUID, never a provider ID.
- `platform_conversation_id` and `platform_message_id` are provider identifiers.
  Their namespace is the tuple `(social_account_id, platform)`. They are not
  interchangeable with canonical UUIDs and are not globally unique. Missing
  provider IDs are `null`; legacy extras are not a fallback identity source.
- `identity_kind` states the stored conversation attribution. A verified-peer
  fallback does not invent a native conversation ID.
- `source: canonical` and `persisted: true` describe a saved ledger projection,
  not a live platform fetch. Message `sources` contains only recognized stored
  labels: `poll`, `webhook`, `app_send`, `legacy_backfill`. An empty list means
  source evidence is unavailable. Unknown/raw metadata is never returned.
- Message `first_seen_at` and `updated_at` are local persistence times.
  `occurred_at` remains the known occurrence time; missing time stays `null` with
  `timestamp_missing: true`. Local persistence time is not successful sync time.

## Classification and participant evidence

Conversation and message projections include their own stored
`conversation_type`, bounded `classification_reason` and `participants_status`:
`pair_verified`, `group_observed`, `missing`, `incomplete`, `invalid`, `conflict`
or `unknown`. These summarize retained classification evidence, not a retained
participant list, count, names, IDs or a guarantee of present-day membership.
Older group/pair evidence can outlive a later observation that omits participants.
Unknown classification is never promoted to direct from sender/recipient
endpoints or names. A malformed stored direct classification lacking pair proof,
or an ambiguous peer, is projected as unknown. A message's evidence may be less
complete than its conversation's accumulated evidence.

The existing incoming-message compatibility route keeps its original
`inbox_message` or `canonical_message` ID namespace and presents accumulated
conversation classification. Its type, reason and participant summary all use
that same conversation evidence, rather than mixing scopes.

Read responses keep `send_authorized: false`. Group or unknown evidence never
grants an automatic reply, and read classification cannot bypass send policy.

## Content and attachments

`content_status` describes the existing public content policy.
`content_available` reports visible text or a visible attachment URL; it does not
mean all original content is present. `content_completeness` is `partial` when
there is known provider omission or preview truncation, `unavailable` when the
content policy withholds it, and otherwise `unknown`. No stored observation alone
claims complete original platform content.

`body_truncated` and `attachments_truncated` describe the bounded projection;
`attachment_metadata_count` counts visible normalized saved metadata, not all
original media. The existing body/attachment continuation endpoints retrieve
only authorized saved content. `media_fetched` and `platform_media_complete`
remain false. Default reads never fall back to retained recovery content or
stale legacy bodies when content was withdrawn/expired. A zero attachment count
on a tombstone does not establish that the original message had no attachments.

## Synchronization and freshness

Each conversation includes `sync`, identical to detail-page `coverage`, with
`scope: account_dm_stream` and `source: legacy_poll_stream`. `status`, `coverage`, `last_attempt_at` and
`last_success_at` come only from the existing matching account/workspace/platform
DM stream state. No state means unknown status/coverage and null times. A failed
attempt does not advance success time; comment-stream activity is not DM success.
Provider error details are not copied into this public projection.

This is specifically `ConversationSyncState`, maintained by the legacy
`begin_sync`/`finish_sync` path. The durable checkpoint worker does not update
that state. A canonical-owned account may therefore have unknown or historical
legacy DM observations while durable pages continue to commit. These fields do
not report durable worker health or its latest page commit, and a legacy success
must not be presented as current durable-sync freshness.

An account's successful observation does not prove that every conversation or
attachment is current. `conversation_freshness: unknown` and
`history_complete: false` stay explicit. This contract adds no database fields,
provider requests, participant storage, account enrollment or access grants.
