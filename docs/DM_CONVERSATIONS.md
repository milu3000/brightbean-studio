# DM conversation view (draft)

This change groups **DMs only** in the Studio web inbox. It preserves every
`InboxMessage`, `InboxReply`, note and platform message ID. REST/MCP retain their
existing per-message contracts. It does not change ingestion, delete historical
echo records, fetch remote history, or deploy anything.

## Identity and pagination

- All reads remain workspace scoped. Group identities include the connected
  social account, so names, brands, providers and accounts are never conflated.
- A valid native `extra.conversation_id` takes precedence. On Meta's supported
  one-to-one DM connections only, a provider-confirmed `extra.sender_id` or raw
  `extra.sender.id` is a fallback. Display names and `sender_handle` alone are
  deliberately insufficient.
- A sender-only webhook row joins a native conversation only if that sender maps
  to exactly one known native conversation and that conversation has just one
  known remote sender. Multiple native threads, conflicting IDs, malformed data
  or missing identity stay separate. No speculative group-chat merge.
- A later poll can enrich metadata without rewriting message IDs. The next view
  incorporates it; ambiguous new evidence may conservatively split a group.
- Group before applying the 50-row page size. Filters match any incoming member,
  and the preview and status describe the full matching conversation. The latest
  successful Studio reply also bumps a conversation's list position; drafts and
  failed replies do not. The text preview is explicitly the latest **received**
  message, while its timestamp is last activity.
- Detail history pages contain 100 chronological events, newest page first:
  incoming messages, sent Studio replies and internal notes. A new reply to an
  old incoming message is visible on the newest page. Pending replies remain
  visible regardless of the original incoming message's age. Older-history
  pages link back to the newest page before composing.

## Status and actions

Conversation status is a summary, not a new workflow object:

1. Any unread member: **Unread**
2. Otherwise any open member: **Needs attention**
3. Otherwise at least one resolved member: **Resolved**
4. Otherwise all members archived: **Archived**

The count needing attention includes unread and open incoming messages. It does
not infer resolution from the presence of an outgoing reply. Opening a history
page marks only incoming messages actually shown on that page as read/open,
never resolved. Other history pages and newer arrivals retain their state.

Use **Manage** on an incoming message for its existing status, assignment and
SLA controls. These actions affect that message only. A **Back to conversation**
link restores the combined view. A new reply from the combined view targets the
latest received message; saved drafts keep their original message association
and existing delivery policy. Existing auto-resolve-on-reply behavior remains
per message. There is no implicit resolve/archive/assign-all behavior. Grouped
DM rows deliberately have no misleading single-message bulk checkbox.

## Known limits / review before production

- This is the history **recorded by Studio**, not a claim of complete native-app
  history. Provider polling currently skips the account's own native messages;
  replies made outside Studio may be absent, and provider history pagination is
  not completed by this PR. Attachments are not newly rendered here.
- Historical bad echo rows are not removed or reclassified. Outgoing-ingestion
  fixes and any separately authorized cleanup remain independent work.
- Existing data is not migrated. Missing trustworthy identity remains separate.
- The index scans selected identity/status/time columns for all DMs in the
  workspace (or one account in detail). Database JSON key extraction avoids
  fetching raw payloads or bodies, and only the visible page loads message
  bodies. CPU/memory still scale linearly with stored DM metadata. Very large
  inboxes need a persisted indexed Conversation model and a planned backfill;
  this PR makes no unmeasured large-scale performance claim.
- This first version intentionally has no conversation-wide ownership, SLA or
  resolution. Those semantics require their own product decision and migration.

## Validation

Tests cover native IDs, webhook fallback and enrichment, ambiguous multi-thread
and multi-participant cases, malformed/missing/conflicting IDs, same names across
brands/platforms/workspaces, non-Meta fallback refusal, grouping before
pagination, old-member filters, latest outbound activity, merged timeline,
pending drafts, recent replies to old incoming messages, page-specific read
marking, single-message management, permissions, and the native-history notice.

Run `pytest apps/inbox apps/api/tests/test_inbox_router.py
apps/mcp/tests/test_inbox_tools.py`, then full pytest, Ruff, mypy and Django's
migration check. No schema migration is expected.

### Results for this draft

- Full local PostgreSQL suite: 2706 passed, 1 skipped before the final additional
  nullable-author regression. The final monolithic rerun was killed at a media
  test's memory peak; a complete non-media run plus the complete media suite is
  used for final verification, with no tests removed. Final counts are in the PR.
- New conversation tests: 22 passed, including nullable Studio authors.
- Ruff lint/format, mypy and Tailwind build passed. Gitleaks export scan found no
  leaks. Django reports no model migration changes.
- Synthetic Django-rendered preview was generated, but the dedicated cloud
  browser rejected localhost with `ERR_BLOCKED_BY_CLIENT`. Visual/mobile and
  real-browser interaction QA is **not verified**. Do not deploy this draft on
  the strength of server-side tests alone.
- The inherited CI workflow only triggers for `main`, not this PR's maintenance
  branch target. An optional branch-filter change was omitted because the
  existing GitHub OAuth authorization lacks workflow scope. No permissions,
  credentials, workflow or deploy settings were expanded. GitHub CI must not be
  represented as passed if no checks were triggered; local results are separate.
