# Signed messages without a native conversation

A Meta webhook may have a valid provider message ID without a native conversation
ID. After the existing owning-app HMAC verification, the route binds that event
to its exact account, workspace, native target and connection generation. The
normalizer can save the already accepted content as an unassigned
`ConversationMessage`. It creates no conversation, guesses no direct thread from
sender/recipient IDs, and leaves direction unknown unless native endpoint or
verified echo evidence establishes it.

The canonical row, its generation evidence and clearing the normalized receipt
payload commit atomically. The remaining receipt has status `unassigned` and
minimal signed-source/replay evidence. Internal queue input without verified
route evidence remains held and cannot claim this visibility contract.

Unassigned insertion produces no read generation, actionable workflow or MCP
incoming event. It grants no send permission. Read adapters use the actual
canonical message UUID, `conversation_id=None`, and fresh account/native-generation
proof; they must apply the ordinary withdrawal/explicit-expiry restrictions.

A later native observation of the exact account/generation/provider message ID
attributes the same row in place and updates its revision timestamp. Persisted
post-cutover signed evidence may then establish one live promotion, including
when the native proof arrives through repair. Bootstrap history remains quiet;
normal replay guards still prevent a second generation or outgoing event.

For a first-seen withdrawal with an older saved legacy copy, the explicit
internal viewer may fall back to that copy only if the withdrawal producer
validated its captured own-account endpoint evidence and recorded the exact
canonical UUID/generation proof. A differing or unknown native owner is never
presented as the captured original. Ordinary timeline/search/automation output
never uses this fallback. Capability queries select metadata and existence only;
archive text is loaded only by the explicitly scoped internal-review accessor.
