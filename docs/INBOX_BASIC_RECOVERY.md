# Inbox recovery using the current canonical database

The operational read-only URL is
`/workspace/<workspace_uuid>/inbox/basic/` on the existing Studio origin.
Open the normal workspace inbox URL and append `basic/`, removing its old query
string. `basic/preserved/` opens original stored DM records that could not be
linked safely during migration. Each record keeps its real InboxMessage UUID.

The view is plain server-rendered HTML. Search, platform/account filters and
older/newest links work without JavaScript or HTMX. Canonical-only incoming and
native outgoing rows, mapped incoming rows, and archived same-identity history
use `canonical_reads`; no temporary platform fetch or second message store is
involved. Known occurrence times have chronological pages. Missing times stay
in a separately labelled section. Default withdrawal/expiry/provenance privacy
applies to previews, content and attachment links.

## Safe stop and recovery state

Keep the current canonical-capable binary and migrated schema. For an incident,
operators can hold DM sends with `INBOX_DM_SENDS_ENABLED=False` and stop scheduled
capture with `INBOX_DURABLE_SYNC_ENABLED=False`; persisted connection `enabled`
can also be paused using the existing operational process. Existing durable
receipts, checkpoints, generations and unknown-send holds must remain intact.
The recovery page requires `INBOX_CANONICAL_READ_ENABLED=True`,
`INBOX_CONVERSATION_V2_ENABLED=True`, and the already-reviewed exact account
capture/read enrollment. Those existing enrollment entries are retained while
capture is paused; no new account is enrolled by opening the page. Composer and
workflow presentation flags may remain disabled. These are documentation of
reviewable settings, not production commands or an activation performed here.

Do not disable the canonical read gate as a rollback. Owned accounts return an
explicit unavailable hold if their reader/enrollment disappears. Do not deploy
the pre-canonical a936 binary or restore an older database to recover the UI:
those paths can omit newly synchronized history and lose send/receipt fences.
The basic page is the compatible presentation fallback while the durable schema,
authorization and receipt controls stay in place.

## Preserved history

The human session-only preserved section reads existing InboxMessage records
under fresh workspace/account `use_inbox` permissions. Missing native IDs or
participant proof remain an explicitly unlinked record; no native thread,
canonical row, incoming event or send permission is invented. Transport-only
adapter rows are excluded. A linked canonical row or same-account native-ID
shadow always takes privacy precedence, including when its FK was removed or
its platform/source was rebound. Ambiguous or denied shadows cannot restore
raw legacy body/media. Search uses the same visible projection.

Truly unimported historical records do not receive a newly invented retention
cutoff. Their original stored access and tombstones are preserved. This section
has no MCP/AI endpoint, composer, quote action, retained-body reveal or read
acknowledgement. Ordinary authenticated session expiry renewal can still occur;
GETs preserve inbox messages, drafts, receipts, workflow/read cursors and even
the user's selected workspace preference. No retention worker, purge or new
recovery deadline is added.

A planned `expires_at` date passing does not itself hide history. Expiry
processing is inactive; only explicit applied expiry markers are redacted.
This release does not claim an enforced retention maximum or a purge policy.

## Full text, attachments and original local records

A shortened canonical preview links to a bounded full-text reader;
`canonical_reads.read_message_body(scope, message_id, cursor=None, limit=2000)`
returns `body`, `body_offset`, `has_more`, `next_cursor`, actual message and
conversation IDs, and explicit content visibility. The signed cursor binds the
actor, original source identity, conversation revision and current safe-content
version. The normal human UI can use the same service for “Read full text”.
Attachments use the existing shared attachment pager. No continuation reads a
restricted archive or downloads media bytes.

Preserved detail pages also show original BrightBean replies, drafts and internal
notes as separately labelled local records attached to that original message.
They are never rendered as invented native messages. Each collection and each
long body has bounded continuation. Reply visibility comes exclusively from
`reply_display_content`; a held/expired/withdrawn reply never falls back to raw
saved text. Notes retain the original authorized workspace scope. Every page
rechecks current grants, original record identity and applicable canonical shadow
privacy, so an old cursor cannot cross withdrawal, reassignment or revocation.
