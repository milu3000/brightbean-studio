"""Fresh canonical visibility; transient missing provenance never becomes sticky."""

from uuid import UUID

from django.db.models import Q

from apps.inbox.models import ConversationMessage, InboxMessage

STICKY_RESTRICTIONS = {"withdrawn", "expired"}


def uuid_value(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def canonical_content(row):
    if row is None:
        return {"available": False, "body": "", "reason": "unavailable"}
    try:
        from apps.inbox.canonical_content import visible_content
    except ModuleNotFoundError as exc:
        if exc.name != "apps.inbox.canonical_content":
            raise
        return {"available": False, "body": "", "reason": "unavailable"}
    return visible_content(row)


def canonical_content_restriction(message_id):
    value = uuid_value(message_id)
    row = ConversationMessage.objects.filter(pk=value).first() if value else None
    visibility = canonical_content(row)
    return "" if visibility["available"] else visibility.get("reason") or "unavailable"


def notification_body(notification):
    data = notification.data if isinstance(notification.data, dict) else {}
    if data.get("content_restriction") in STICKY_RESTRICTIONS:
        return ""
    # A producer may precede durable provenance insertion in the same
    # transaction. Re-evaluate transient reasons from the exact source now.
    workspace_id = notification.workspace_id or uuid_value(data.get("workspace_id"))
    if "canonical_message_id" in data:
        source_id = uuid_value(data["canonical_message_id"])
        if not source_id or not workspace_id:
            return ""
        sources = ConversationMessage.objects.filter(pk=source_id, workspace_id=workspace_id)
        if notification.conversation_id:
            sources = sources.filter(conversation_id=notification.conversation_id)
        if notification.inbox_message_id:
            sources = sources.filter(legacy_message_id=notification.inbox_message_id)
        return canonical_content(sources.first())["body"][:200]
    legacy_id = notification.inbox_message_id
    if not legacy_id and notification.event_type == "new_inbox_message" and "message_id" in data:
        legacy_id = uuid_value(data["message_id"])
        if legacy_id is None:
            return ""
    if legacy_id:
        legacy = (
            InboxMessage.objects.select_related("social_account")
            .filter(pk=legacy_id, workspace_id=workspace_id, social_account__workspace_id=workspace_id)
            .first()
        )
        if legacy is None:
            return ""
        if legacy.message_type == "dm":
            from apps.inbox.canonical_content import legacy_content_restriction

            if legacy_content_restriction(legacy.extra):
                return ""
            identity = Q(legacy_message_id=legacy.pk)
            if legacy.platform_message_id:
                identity |= Q(
                    social_account_id=legacy.social_account_id, platform_message_id=legacy.platform_message_id
                )
            sources = list(ConversationMessage.objects.filter(identity)[:2])
            if len(sources) > 1:
                return ""
            source = sources[0] if sources else None
            if source is not None:
                if (
                    source.workspace_id != workspace_id
                    or source.social_account_id != legacy.social_account_id
                    or source.platform != legacy.social_account.platform
                    or source.direction != "inbound"
                    or source.legacy_message_id not in {None, legacy.pk}
                    or (notification.conversation_id and source.conversation_id != notification.conversation_id)
                ):
                    return ""
                return canonical_content(source)["body"][:200]
            extra = legacy.extra if isinstance(legacy.extra, dict) else {}
            if notification.conversation_id or extra.get("canonical_message_id"):
                return ""
        return legacy.body[:200]
    if notification.conversation_id:
        if not workspace_id or not notification.source_revision:
            return ""
        # This compatibility format is only available after the watermark schema.
        if not hasattr(ConversationMessage, "incoming_generation"):
            return ""
        source = ConversationMessage.objects.filter(
            workspace_id=workspace_id,
            conversation_id=notification.conversation_id,
            incoming_generation=notification.source_revision,
        ).first()
        return canonical_content(source)["body"][:200]
    return notification.body
