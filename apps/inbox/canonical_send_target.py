"""Metadata-only compatibility adapter for an actual canonical inbound.

This is not ingestion. It never copies body, emits activity, or manufactures an
incoming for an outbound-only thread. Existing receipt transport keeps its FK.
"""

from copy import copy

from django.db.models import Q
from django.utils import timezone

from .dm_send_gate import DMSendGateError
from .models import ConversationMessage, InboxMessage


def is_transport_projection(message):
    return isinstance(message.extra, dict) and message.extra.get("transport_projection") is True


def exclude_transport_projections(queryset):
    # Missing JSON keys evaluate to SQL NULL on supported databases. Anchor
    # exclusion with key existence so ordinary legacy rows remain visible.
    return queryset.exclude(Q(extra__has_key="transport_projection") & Q(extra__transport_projection=True))


def canonical_projection_view(message):
    """Fresh canonical shadow wins over raw legacy body on every read path.

    Caller owns current actor permissions. A native-ID shadow is sufficient to
    withhold old raw content even when its legacy FK was never linked.
    """
    from .canonical_content import legacy_content_restriction, visible_content

    current = (
        InboxMessage.objects.select_related("social_account")
        .filter(
            pk=message.pk,
            workspace_id=message.workspace_id,
            social_account_id=message.social_account_id,
            platform_message_id=message.platform_message_id,
        )
        .first()
    )
    projected = copy(current or message)
    if current is not None and current.message_type != "dm":
        return projected
    candidates = list(
        ConversationMessage.objects.filter(
            Q(legacy_message_id=message.pk)
            | Q(social_account_id=message.social_account_id, platform_message_id=message.platform_message_id)
        )[:2]
    )
    restriction = legacy_content_restriction(current.extra) if current is not None else ""
    if current is not None and not candidates and not is_transport_projection(current):
        return _restricted_legacy_view(projected, restriction) if restriction else projected
    row = candidates[0] if len(candidates) == 1 else None
    valid = bool(
        current is not None
        and row is not None
        and row.workspace_id == current.workspace_id
        and row.social_account_id == current.social_account_id
        and row.platform == current.social_account.platform
        and row.platform_message_id == current.platform_message_id
        and row.direction == "inbound"
        and row.legacy_message_id in {None, current.pk}
    )
    content = (
        visible_content(row)
        if valid
        else {"available": False, "body": "", "attachments": [], "content_status": "unavailable"}
    )
    projected.body = content["body"]
    projected.extra = {"inbox_attachments": content["attachments"], "content_status": content["content_status"]}
    if valid:
        projected.sender_name = row.sender_name
        projected.sender_handle = row.sender_id
        projected.received_at = row.occurred_at or current.received_at
        projected.extra.update(conversation_type=row.conversation_type, classification_reason=row.classification_reason)
    else:
        projected.sender_name = projected.sender_handle = ""
    projected.sender_avatar_url = ""
    projected.canonical_content_available = content["available"]
    projected.canonical_content_status = content["content_status"]
    return _restricted_legacy_view(projected, restriction) if restriction else projected


def _restricted_legacy_view(projected, restriction):
    # No alternate media field can resurrect a restricted default projection.
    extra = projected.extra if isinstance(projected.extra, dict) else {}
    identity_keys = {
        "conversation_id",
        "conversation_type",
        "classification_reason",
        "sender_id",
        "recipient_id",
        "message_recipient_id",
        "participant_ids",
    }
    projected.extra = {key: value for key, value in extra.items() if key in identity_keys}
    projected.extra.update(inbox_attachments=[], content_status="expired" if restriction == "expired" else "removed")
    if restriction == "withdrawn":
        projected.extra["is_deleted"] = True
    projected.body = ""
    projected.sender_avatar_url = ""
    projected.canonical_content_available = False
    projected.canonical_content_status = "expired" if restriction == "expired" else "removed"
    return projected


def validate_anchor_identity(row, conversation, account):
    own = {account.account_platform_id, account.webhook_target_id} - {"", None}
    if (
        row is None
        or row.conversation_id != conversation.pk
        or row.workspace_id != account.workspace_id
        or row.social_account_id != account.pk
        or row.platform != account.platform
        or row.direction != "inbound"
        or row.conversation_type != "direct"
        or row.sender_id != conversation.peer_id
        or row.recipient_id not in own
        or not isinstance(row.platform_message_id, str)
        or not row.platform_message_id
        or len(row.platform_message_id) > 255
        or any(c.isspace() for c in row.platform_message_id)
        or row.delivery_status != "observed"
    ):
        raise DMSendGateError("invalid_inbound", "A verified incoming message in this conversation is required.")


def validate_anchor(row, conversation, account):
    validate_anchor_identity(row, conversation, account)
    if (
        row.is_deleted
        or row.content_status in {"removed", "withdrawn", "deleted", "unsent", "unavailable"}
        or row.occurred_at is None
        or timezone.is_naive(row.occurred_at)
        or row.occurred_at > timezone.now()
    ):
        raise DMSendGateError("incoming_unavailable", "The incoming message is unavailable for sending.")
    # The shared policy owns current durable-generation/expiry checks when that
    # module is installed; pre-durable baseline rows have no expiry sidecar.
    try:
        from .canonical_content import visible_content
    except ModuleNotFoundError as exc:
        if exc.name != "apps.inbox.canonical_content":
            raise
    else:
        if not visible_content(row)["available"]:
            raise DMSendGateError("incoming_unavailable", "The incoming message is unavailable for sending.")


def latest_inbound(conversation, account):
    row = (
        ConversationMessage.objects.filter(
            conversation=conversation,
            workspace_id=account.workspace_id,
            social_account=account,
            platform=account.platform,
            direction="inbound",
            is_deleted=False,
            occurred_at__isnull=False,
        )
        .exclude(content_status__in=["removed", "withdrawn", "deleted", "unsent", "expired", "unavailable"])
        .order_by("-occurred_at", "-first_seen_at", "-id")
        .first()
    )
    validate_anchor(row, conversation, account)
    return row


def transport_target(row, conversation, account, *, materialize=False):
    """Caller holds the account lock for materialization; reads are pure."""
    validate_anchor(row, conversation, account)
    extra = {
        "transport_projection": True,
        "canonical_message_id": str(row.pk),
        "conversation_id": conversation.platform_conversation_id,
        "conversation_type": "direct",
        "classification_reason": "participants_pair",
        "sender_id": conversation.peer_id,
        "recipient_id": conversation.peer_id,
        "message_recipient_id": row.recipient_id,
        "participant_ids": [row.recipient_id, conversation.peer_id],
    }
    existing = InboxMessage.objects.filter(social_account=account, platform_message_id=row.platform_message_id).first()
    if existing is not None:
        current_extra = existing.extra if isinstance(existing.extra, dict) else {}
        linked = ConversationMessage.objects.filter(legacy_message=existing).first()
        if (
            existing.workspace_id != account.workspace_id
            or existing.message_type != "dm"
            or existing.sender_handle != row.sender_id
            or current_extra.get("conversation_id") != conversation.platform_conversation_id
            or existing.received_at != row.occurred_at
            or linked is None
            or linked.pk != row.pk
            or (is_transport_projection(existing) and current_extra.get("canonical_message_id") != str(row.pk))
        ):
            raise DMSendGateError(
                "legacy_identity_conflict", "The existing transport identity conflicts with this conversation."
            )
        existing.social_account = account
        return existing
    if row.legacy_message_id:
        raise DMSendGateError("legacy_identity_conflict", "The canonical incoming has a different transport identity.")
    target = InboxMessage(
        workspace_id=account.workspace_id,
        social_account=account,
        platform_message_id=row.platform_message_id,
        message_type="dm",
        sender_name="",
        sender_handle=row.sender_id,
        body="",
        extra=extra,
        status="archived",
        received_at=row.occurred_at,
    )
    if materialize:
        target.save()
        row.legacy_message = target
        row.save(update_fields=["legacy_message", "updated_at"])
    # Existing safety checks use this reverse relation in both preview and send.
    target.conversation_message = row
    return target
