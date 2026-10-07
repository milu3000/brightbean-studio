"""Explicit native quote identity. Quoted content is never copied into receipts."""

from uuid import UUID

from django.conf import settings

from .canonical_content import visible_content
from .dm_send_gate import DMSendGateError
from .models import ConversationMessage

RUNTIME_PLATFORMS = frozenset({"facebook", "instagram_login"})


def supported(account):
    return (
        getattr(settings, "INBOX_CONVERSATION_COMPOSER_ENABLED", False) is True
        and account.platform in RUNTIME_PLATFORMS
    )


def parse_quote_id(value):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, bool):
            raise ValueError
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise DMSendGateError("invalid_quote", "Select a canonical message to quote, or clear the quote.") from None


def resolve_quote(conversation, account, identifier):
    identifier = parse_quote_id(identifier)
    if identifier is None:
        return None
    if not supported(account):
        raise DMSendGateError("quote_unsupported", "Native quotes are not supported for this account.")
    row = ConversationMessage.objects.filter(
        pk=identifier,
        conversation=conversation,
        workspace_id=account.workspace_id,
        social_account=account,
        platform=account.platform,
    ).first()
    own = {account.account_platform_id, account.webhook_target_id} - {"", None}
    if (
        row is None
        or row.conversation_type != "direct"
        or conversation.conversation_type != "direct"
        or conversation.peer_ambiguous
        or conversation.identity_kind != "platform"
        or not conversation.platform_conversation_id
        or not isinstance(row.platform_message_id, str)
        or not row.platform_message_id
        or len(row.platform_message_id) > 255
        or any(c.isspace() or ord(c) < 32 for c in row.platform_message_id)
        or row.delivery_status not in {"observed", "provider_accepted"}
        or not (
            (row.direction == "inbound" and row.sender_id == conversation.peer_id and row.recipient_id in own)
            or (row.direction == "outbound" and row.sender_id in own and row.recipient_id == conversation.peer_id)
        )
    ):
        raise DMSendGateError("quote_unverified", "This message does not verify a quote in the current conversation.")
    content = visible_content(row)
    if not content["available"] or content["content_status"] == "unavailable":
        raise DMSendGateError(
            "quote_unavailable", "This quoted message is unavailable. Clear it or select another message."
        )
    from .sync_identity import canonical_read_connection

    connection = canonical_read_connection(account)
    return {
        "row": row,
        "mid": row.platform_message_id,
        "native_conversation_id": conversation.platform_conversation_id,
        "generation": connection.generation if connection is not None else None,
        "preview": {
            "id": str(row.pk),
            "body": content["body"][:240],
            "sender_name": row.sender_name,
            "direction": row.direction,
            "unavailable": False,
        },
    }


def pinned_quote(reply):
    return (
        reply.quote_target_id,
        reply.quote_platform_message_id,
        reply.quote_platform_conversation_id,
        reply.quote_connection_generation,
    )


def requested_quote(value):
    return (
        (value["row"].pk, value["mid"], value["native_conversation_id"], value["generation"])
        if value
        else (None, "", "", None)
    )


def set_quote(reply, value):
    (
        reply.quote_target_id,
        reply.quote_platform_message_id,
        reply.quote_platform_conversation_id,
        reply.quote_connection_generation,
    ) = requested_quote(value)


def validate_quote(reply, conversation, account):
    value = resolve_quote(conversation, account, reply.quote_target_id)
    if pinned_quote(reply) != requested_quote(value):
        raise DMSendGateError(
            "quote_changed", "The quote identity changed. Clear and select it again before a new action."
        )
    return value


def quote_preview(reply, conversation, account):
    if reply.quote_target_id is None:
        return None
    try:
        return validate_quote(reply, conversation, account)["preview"]
    except ValueError:
        return {"id": str(reply.quote_target_id), "body": "", "sender_name": "", "direction": "", "unavailable": True}
