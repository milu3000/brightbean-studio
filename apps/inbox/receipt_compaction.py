"""Read-time receipt privacy. No scheduled compaction, purge or recovery deadline.

Restricted content storage is intentionally never joined or returned here.
"""

from django.db.models import Q

from .models import ConversationMessage, InboxReply


def reply_display_content(reply):
    from .canonical_content import native_receipt_relocation_allowed, visible_content

    if {"inbox_message_id", "conversation_id", "action_nonce"} & reply.get_deferred_fields():
        return {
            "available": False,
            "body": "",
            "send_error": "",
            "is_deleted": False,
            "is_expired": False,
            "content_status": "unavailable",
            "reason": "unavailable",
        }

    current = (
        InboxReply.objects.select_related("inbox_message__social_account")
        .filter(
            pk=reply.pk,
            inbox_message_id=reply.inbox_message_id,
            conversation_id=reply.conversation_id,
            action_nonce=reply.action_nonce,
        )
        .first()
    )
    hidden = {
        "available": False,
        "body": "",
        "send_error": "",
        "is_deleted": False,
        "is_expired": False,
        "content_status": "unavailable",
        "reason": "unavailable",
    }
    if current is None:
        return hidden
    if current.content_compacted_at:
        return {**hidden, "is_expired": True, "content_status": "expired", "reason": "expired"}
    message = current.inbox_message
    account = message.social_account
    if account.workspace_id != message.workspace_id:
        return hidden
    if current.conversation_id:
        from .canonical_access import archive_identity, read_connection
        from .conversation_policy import read_allowed
        from .models import InboxConversation

        conversation = InboxConversation.objects.filter(
            pk=current.conversation_id,
            workspace_id=message.workspace_id,
            social_account=account,
            platform=account.platform,
            platform_conversation_id=current.platform_conversation_id,
            peer_id=current.recipient_id,
        ).first()
        if (
            conversation is None
            or current.account_platform_id != account.account_platform_id
            or not (read_allowed(account) or archive_identity(account))
        ):
            return hidden
        try:
            connection, _archive = read_connection(account)
        except ValueError:
            return hidden
        if current.connection_generation != (connection.generation if connection else None):
            return hidden
    candidates = list(
        ConversationMessage.objects.filter(
            Q(legacy_reply_id=current.pk)
            | (
                Q(social_account_id=message.social_account_id, platform_message_id=current.platform_reply_id)
                if current.platform_reply_id
                else Q(pk__in=[])
            )
        )[:2]
    )
    if candidates:
        if len(candidates) != 1:
            return hidden
        row = candidates[0]
        if (
            row.workspace_id != message.workspace_id
            or row.social_account_id != message.social_account_id
            or row.platform != message.social_account.platform
            or row.direction != "outbound"
            or row.legacy_reply_id not in {None, current.pk}
            or (
                current.conversation_id
                and row.conversation_id != current.conversation_id
                and not native_receipt_relocation_allowed(row, current)
            )
        ):
            return hidden
        content = visible_content(row)
        return {**content, "send_error": current.send_error if content["available"] else ""}
    # A canonical sent receipt without its outgoing projection cannot use its
    # raw body as a fallback around missing/withdrawn generation evidence.
    if current.conversation_id and current.status == "sent":
        return hidden
    return {
        "available": True,
        "body": current.body,
        "send_error": current.send_error,
        "is_deleted": False,
        "is_expired": False,
        "content_status": "text",
        "reason": "",
    }
