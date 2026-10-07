# Conversation drafts and native quotes

The conversation composer uses an explicit action nonce per outgoing message.
Separate nonces permit sequential messages; repeating an already sent nonce
returns its receipt. Editing an unattempted draft uses a composer revision.
After dispatch preparation, body and quote identity are frozen for that nonce.

Replies are unquoted by default. Selecting a canonical message UUID persists
only its protected message link, provider message ID, native conversation ID,
and connection generation. Preview text is derived from the current shared
content policy and is never copied into the receipt. Clearing the quote removes
the actual provider `reply_to` field. Unavailable quotes can be cancelled before
an attempt without granting permission to send against an invalid inbound.

The existing Facebook and Instagram Direct adapters accept an explicit native
quote as top-level `reply_to: {mid: ...}`. Either party's visible, verified message
in the same native conversation may be selected. The current inbound determines
the recipient and reply window independently of the selected quote. Existing
HUMAN_AGENT rules remain unchanged, and quote errors never cause an unquoted
fallback send.

Official contracts:

- [Instagram Direct messaging](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api)
- [Messenger messages](https://developers.facebook.com/documentation/business-messaging/messenger-platform/send-messages)
- [Instagram with Facebook Login](https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/send-message)

The last platform's provider contract is documented, but this application's
Instagram Facebook Login adapter and existing grants do not enable DM dispatch.
No OAuth grant, transport scope, or Graph API version is added here.

The shared provider boundary rechecks scope, native identity, content visibility,
connection generation, ownership, account controls, permissions and immutable
payload fingerprint. A refusal before the network boundary remains known
not-sent in either receipt gate. UNKNOWN outcomes remain blocked.

Migration 0017 is additive, retains historical drafts and receipts, and refuses
reversal while quote identity exists. Rollback must keep the quote-aware receipt
boundary and schema; disabling the composer feature holds canonical actions.
An older application that ignores quote metadata is not a safe rollback target.
The composer and workflow flags default to false. No capture enrollment or
production activation is performed by these migrations.
