# Event clients and conversation actions

The existing `inbox.dm.received` event remains a minimal reference. Its
`data.message_id` identifies a real retained legacy incoming row when one is
already linked; otherwise it identifies the canonical incoming message.
`get_inbox_message` accepts both namespaces and preserves the incoming DTO,
including `canonical_conversation_id` and a canonical read reference. Native
outgoing messages never masquerade as incoming events.

For accounts taken over by canonical capture/composer, old per-incoming write
calls deliberately stop. They return a structured `canonical_composer_required`
error with the conversation ID and these callable tools:

1. `get_inbox_conversation_composer`
2. `save_inbox_conversation_draft`
3. `send_inbox_conversation_reply`
4. `retire_inbox_conversation_reply`

Read the current composer and carry its `scope_token`, `composer_revision` as
`expected_revision`, and `action_nonce`. A new intentional message uses a new
nonce. A retry preserves the same nonce, body and quote; once attempted, changing
any of them is rejected. UNKNOWN results require review and never create an
automatic successor. Draft permission remains separate from send permission.

Clients must update their write calls at this boundary. The server does not
silently turn an old incoming-based request into a new conversation action or
manufacture a nonce on the client's behalf. The existing scoped subscription,
read permissions, sender ownership and automated reply-window rules remain in
force. A structured upgrade error is guidance, not a new grant or send approval.

REST equivalents are under `/api/v1/inbox/conversations/{conversation_id}/`:
`composer` (GET), `draft`, `send`, and `retire` (POST). API and MCP actions share
the same typed schema and service boundary. Pre-cutover legacy accounts keep
their existing write contract. No subscription, capture flag or grant is
activated by these compatibility routes.
