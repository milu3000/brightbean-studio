# One-time native conversation reads

An Instagram or Facebook reply sent in the native app is not an `InboxReply`.
BrightBean's saved `replies` array and open/archived work status cannot establish
whether someone answered on the platform. The ordinary inbox poll deliberately
excludes native outbound messages unless separately authorized V2 capture is
enabled. This feature does not change that capture policy.

The inbox now offers an explicit platform refresh for one existing message's
known native conversation. REST uses `POST /api/v1/inbox/{message_id}/native-thread/read`
with an optional `limit` (1–100, default 50); MCP uses `read_native_inbox_thread`
with the same message ID and limit. The UI, REST and MCP call the same service.
Opening an inbox item or calling the existing stored-message tools never starts
this platform read automatically.

The caller must currently have `use_inbox` and access to the exact account and
workspace. Key/OAuth grants and membership are rechecked, along with the selected
message, account, credential and native conversation identity, before and after
the provider request. Callers cannot supply a different platform thread ID or URL.
Missing identity, conflicting identity, groups, unsupported accounts or unverifiable
participants return no native content. Previously stored classification can only
restrict the read; it does not expose the optional V2 message ledger.

One call makes one bounded GET to the fixed Instagram/Facebook API host, using
the existing account credential. It follows no redirects or paging links, refreshes
no credential, and creates no subscription, enrollment or background task. Response
bytes, message count, text and attachment metadata are bounded. The reader does
not use the older provider error logger, which could include response bodies.

Returned rows are a temporary platform observation with an explicit source and
direction. They are not BrightBean delivery receipts and do not identify which
human or app wrote them. The snapshot never creates `InboxReply` or
`ConversationMessage`, changes work status, erases drafts or overwrites the
composer's target. Dismissing or navigating away discards the UI snapshot. HTTP
responses carrying snapshots use `private, no-store`; normal audit metadata
records the requested action and outcome, never the returned body or media.

Coverage remains incomplete even when the first page has no continuation link.
Truncation and additional provider pages are reported explicitly; an empty or
unavailable result does not prove there was no reply. Media URLs may expire and
no image or attachment is downloaded into BrightBean storage. The feature cannot
recover group history or content the provider does not return.

`newer_outbound_observed` is context for the reader, not a decision to resolve
work or send another message. A platform read does not extend the automated
reply window, clear an uncertain send, release an existing owner, or guarantee
that no one will reply in the native app immediately after the snapshot.

This is an on-demand read, not continuous native reply synchronization. Continuous
storage or account-wide history capture remains a separate opt-in. No new OAuth
permission, database migration, capture flag or retention rule accompanies this
feature.
