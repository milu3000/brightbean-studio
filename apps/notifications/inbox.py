"""Recipient-local inbox notification lifecycle; no new external delivery."""

from django.apps import apps
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.inbox.locking import lock_dm_account
from apps.inbox.models import InboxConversation, InboxMessage
from apps.members.models import WorkspaceMembership

from .content import STICKY_RESTRICTIONS, canonical_content_restriction, uuid_value
from .identity_v1 import canonical_identity, event_identity, message_identity
from .models import Channel, DeliveryStatus, EventType, InboxNotificationEvent, Notification, NotificationDelivery


def sender_label(name="", handle="", *, sender_id="", platform=""):
    for candidate in (name, handle):
        value = candidate.strip() if isinstance(candidate, str) else ""
        bare = value.lstrip("@")
        if bare and not bare.isdigit() and bare != str(sender_id) and len(bare) <= 255 and uuid_value(bare) is None:
            return value
    platform = {"facebook": "Facebook", "instagram": "Instagram", "instagram_login": "Instagram"}.get(platform)
    return f"{platform} contact" if platform else "Unknown sender"


def message_label(message):
    extra = message.extra if isinstance(message.extra, dict) else {}
    peer = str(extra.get("sender_id") or "")
    platform = message.social_account.platform
    label = sender_label(message.sender_name, message.sender_handle, sender_id=peer, platform=platform)
    fallback = sender_label(platform=platform)
    if label == fallback and peer:
        prior = (
            InboxMessage.objects.filter(workspace_id=message.workspace_id, social_account_id=message.social_account_id)
            .filter(Q(sender_handle=peer) | Q(extra__sender_id=peer))
            .exclude(pk=message.pk)
            .order_by("-received_at")[:20]
        )
        for row in prior:
            known = sender_label(row.sender_name, row.sender_handle, sender_id=peer, platform=platform)
            if known != fallback:
                return known
    return label


def notification_title(notification):
    if notification.inbox_message_id:
        return f"New {notification.inbox_message.type_display} from {message_label(notification.inbox_message)}"
    prefix, sep, name = notification.title.rpartition(" from ")
    return f"{prefix} from {sender_label(name)}" if sep else notification.title


def _member(user, workspace_id):
    member = (
        WorkspaceMembership.objects.select_for_update(of=("self",))
        .select_related("custom_role", "workspace")
        .filter(user=user, user__is_active=True, workspace_id=workspace_id, workspace__is_archived=False)
        .first()
    )
    return (
        member
        if (
            member is not None
            and member.effective_permissions.get("use_inbox") is True
            and (not member.custom_role_id or member.custom_role.organization_id == member.workspace.organization_id)
        )
        else None
    )


def _read_watermark(user, conversation):
    try:
        model = apps.get_model("inbox", "ConversationReadState")
    except LookupError:
        return 0, None
    return model.objects.filter(user_id=user.pk, conversation_id=conversation.pk).values_list(
        "read_generation", "updated_at"
    ).first() or (0, None)


def _sticky_data(notification, data):
    restriction = (notification.data or {}).get("content_restriction")
    return {**data, "content_restriction": restriction} if restriction in STICKY_RESTRICTIONS else data


@transaction.atomic
def _record(
    *,
    user,
    workspace,
    subject_key,
    event_key,
    title,
    body,
    occurred_at,
    data,
    inbox_message=None,
    conversation=None,
    event_revision=0,
):
    title = title[:255]
    seen, read_at = False, None
    restriction = ""
    if conversation is not None and event_revision:
        # Same lock order as reader acknowledgement. A committed earlier ack
        # must be visible before a delayed notification decides unread state.
        account = lock_dm_account(conversation.social_account_id, workspace.pk)
        if account is None or account.platform != conversation.platform:
            return None
        current = (
            InboxConversation.objects.select_for_update()
            .filter(
                pk=conversation.pk, workspace_id=workspace.pk, social_account_id=account.pk, platform=account.platform
            )
            .first()
        )
        if (
            current is None
            or getattr(current, "incoming_generation", 0) < event_revision
            or canonical_identity(current) != subject_key
        ):
            return None
        conversation = current
        generation, read_at = _read_watermark(user, current)
        seen = generation >= event_revision
        restriction = canonical_content_restriction(data.get("canonical_message_id"))
        if restriction:
            body = ""
        # Crucial transaction boundary: the signal can precede insertion of
        # durable provenance. Never persist temporary unavailability as a
        # permanent hide flag. Display rechecks the canonical proof after commit.
        if restriction in STICKY_RESTRICTIONS:
            data = {**data, "content_restriction": restriction}
    if not _member(user, workspace.pk):
        return None
    receipt = (
        InboxNotificationEvent.objects.select_related("notification").filter(user=user, event_key=event_key).first()
    )
    if receipt is not None:
        notification = receipt.notification
        changes = []
        if notification.subject_key == subject_key and notification.source_revision == event_revision:
            if inbox_message is not None and notification.inbox_message_id is None:
                notification.inbox_message, notification.data, notification.title = (
                    inbox_message,
                    _sticky_data(notification, data),
                    title,
                )
                changes += ["inbox_message", "data", "title"]
            if restriction in STICKY_RESTRICTIONS:
                notification.body = ""
                notification.data = {**notification.data, "content_restriction": restriction}
                changes += ["body", "data"]
            if seen and notification.conversation_id == conversation.pk:
                notification.is_read, notification.read_revision, notification.read_at = (
                    True,
                    notification.revision,
                    read_at,
                )
                changes += ["is_read", "read_revision", "read_at"]
        if changes:
            notification.save(update_fields=set(changes))
        return notification
    notification, created = Notification.objects.select_for_update().get_or_create(
        user=user,
        event_type=EventType.NEW_INBOX_MESSAGE,
        subject_key=subject_key,
        defaults={
            "workspace": workspace,
            "inbox_message": inbox_message,
            "conversation": conversation,
            "title": title,
            "body": body[:200],
            "data": data,
            "source_revision": event_revision,
            "latest_message_at": occurred_at,
            "is_read": seen,
            "read_revision": 1 if seen else 0,
            "read_at": read_at if seen else None,
        },
    )
    InboxNotificationEvent.objects.create(user=user, notification=notification, event_key=event_key)
    if not created:
        if event_revision and event_revision <= notification.source_revision:
            return notification
        if (
            not event_revision
            and occurred_at
            and (
                (notification.dismissed_at and occurred_at <= notification.dismissed_at)
                or (notification.latest_message_at and occurred_at < notification.latest_message_at)
            )
        ):
            return notification
        notification.revision += 1
        notification.event_count += 1
        notification.source_revision = max(notification.source_revision, event_revision)
        notification.is_read, notification.read_at = seen, read_at if seen else None
        if seen:
            notification.read_revision = notification.revision
        notification.dismissed_at = None
        notification.title, notification.body, notification.data = title, body[:200], data
        notification.inbox_message = inbox_message
        notification.conversation = conversation or notification.conversation
        notification.latest_message_at, notification.last_event_at = occurred_at, timezone.now()
        notification.save()
    else:
        from .engine import _resolve_channels

        if Channel.IN_APP in _resolve_channels(user, EventType.NEW_INBOX_MESSAGE):
            NotificationDelivery.objects.create(
                notification=notification,
                channel=Channel.IN_APP,
                status=DeliveryStatus.DELIVERED,
                delivered_at=timezone.now(),
            )
    return notification


def _canonical_for_user(user, conversation, message, event_revision, legacy=None):
    label = sender_label(
        message.sender_name or getattr(legacy, "sender_name", ""),
        getattr(legacy, "sender_handle", ""),
        sender_id=message.sender_id,
        platform=conversation.platform,
    )
    kind = {"direct": "Direct Message", "group": "Group Message"}.get(conversation.conversation_type, "Message")
    data = {
        "workspace_id": str(conversation.workspace_id),
        "conversation_id": str(conversation.pk),
        "canonical_message_id": str(message.pk),
    }
    if legacy is not None:
        data["message_id"] = str(legacy.pk)
    return _record(
        user=user,
        workspace=conversation.workspace,
        subject_key=canonical_identity(conversation),
        event_key=event_identity(conversation.social_account_id, conversation.platform, message.platform_message_id),
        title=f"New {kind} from {label}",
        body=message.body,
        occurred_at=message.occurred_at,
        data=data,
        inbox_message=legacy,
        conversation=conversation,
        event_revision=event_revision,
    )


def notify_legacy_incoming(user, data):
    message_id = uuid_value(data.get("message_id"))
    message = (
        InboxMessage.objects.select_related("workspace", "social_account", "conversation_message__conversation")
        .filter(pk=message_id)
        .first()
        if message_id
        else None
    )
    if (
        message is None
        or str(message.workspace_id) != str(data.get("workspace_id"))
        or message.workspace_id != message.social_account.workspace_id
    ):
        return None
    row = getattr(message, "conversation_message", None)
    if row is not None:
        if row.is_deleted or row.direction != "inbound":
            return None
        generation = getattr(row, "incoming_generation", None)
        if hasattr(row, "incoming_generation") and not generation:
            return None
        if generation and row.conversation_id:
            return _canonical_for_user(user, row.conversation, row, generation, message)
    extra = message.extra if isinstance(message.extra, dict) else {}
    if any(extra.get(key) for key in ("is_deleted", "is_echo", "is_outgoing")) or message.status == "archived":
        return None
    conversation = row.conversation if row is not None and row.conversation_id else None
    subject_key = message_identity(message, conversation)
    domain = message.message_type
    if domain in {"comment", "mention"}:
        from apps.inbox.public_threads import public_thread_key

        from .identity_v1 import digest

        subject_key = digest(public_thread_key(message))
        domain = "public"
    event_key = event_identity(
        message.social_account_id, message.social_account.platform, message.platform_message_id, domain
    )
    if domain == "public":
        # Reuse receipts made before comment/mention became one event facet.
        prior = InboxNotificationEvent.objects.filter(
            user=user,
            event_key__in=[
                event_identity(
                    message.social_account_id, message.social_account.platform, message.platform_message_id, kind
                )
                for kind in ("comment", "mention")
            ],
        ).first()
        if prior is not None:
            event_key = prior.event_key
    return _record(
        user=user,
        workspace=message.workspace,
        subject_key=subject_key,
        event_key=event_key,
        title=f"New {message.type_display} from {message_label(message)}",
        body=message.body,
        occurred_at=message.received_at,
        data=data,
        inbox_message=message,
        conversation=conversation,
    )


def notify_conversation_incoming(conversation, message, event_revision, **kwargs):
    if (
        isinstance(event_revision, bool)
        or not isinstance(event_revision, int)
        or event_revision < 1
        or message.conversation_id != conversation.pk
        or message.workspace_id != conversation.workspace_id
        or message.social_account_id != conversation.social_account_id
        or message.platform != conversation.platform
        or conversation.social_account.workspace_id != conversation.workspace_id
        or not message.platform_message_id
        or message.direction != "inbound"
        or message.is_deleted
        or getattr(message, "incoming_generation", None) != event_revision
        or getattr(conversation, "incoming_generation", None) != event_revision
    ):
        return []
    legacy = message.legacy_message
    users = (
        [legacy.assigned_to]
        if legacy is not None and legacy.assigned_to_id
        else [
            m.user
            for m in WorkspaceMembership.objects.filter(
                workspace_id=conversation.workspace_id, workspace_role__in=["owner", "manager"]
            ).select_related("user")
        ]
    )
    return [
        _canonical_for_user(user, conversation, message, event_revision, legacy)
        for user in sorted(users, key=lambda user: str(user.pk))
    ]


def on_canonical_incoming(sender, *, conversation, message, event_revision, **kwargs):
    return notify_conversation_incoming(conversation, message, event_revision)


@transaction.atomic
def on_canonical_content_restricted(sender, *, message, reason, **kwargs):
    if (
        reason not in STICKY_RESTRICTIONS
        or message.body
        or message.attachments
        or message.workspace_id != message.social_account.workspace_id
        or message.platform != message.social_account.platform
    ):
        return 0
    legacy_ids = set(
        InboxMessage.objects.filter(
            workspace_id=message.workspace_id, social_account_id=message.social_account_id, message_type="dm"
        )
        .filter(Q(platform_message_id=message.platform_message_id) | Q(pk=message.legacy_message_id))
        .values_list("pk", flat=True)
    )
    legacy_strings = {str(pk) for pk in legacy_ids}
    exact = (
        Q(data__canonical_message_id=str(message.pk))
        | Q(inbox_message_id__in=legacy_ids)
        | Q(data__message_id__in=legacy_strings)
    )
    generation = getattr(message, "incoming_generation", None)
    if message.conversation_id and generation:
        exact |= Q(conversation_id=message.conversation_id, source_revision=generation, inbox_message__isnull=True)
    rows = (
        Notification.objects.select_for_update()
        .filter(event_type=EventType.NEW_INBOX_MESSAGE)
        .filter(
            Q(workspace_id=message.workspace_id)
            | Q(workspace__isnull=True, data__workspace_id=str(message.workspace_id))
        )
        .filter(exact)
    )
    count = 0
    for notification in rows:
        data = dict(notification.data or {})
        latest = (
            data.get("canonical_message_id") == str(message.pk)
            or notification.inbox_message_id in legacy_ids
            or (not notification.subject_key and data.get("message_id") in legacy_strings)
            or (
                notification.inbox_message_id is None
                and notification.conversation_id == message.conversation_id
                and bool(generation)
                and notification.source_revision == generation
            )
        )
        notification.body = ""
        for key in ("body", "text", "preview", "snippet", "summary", "message_text", "content"):
            data.pop(key, None)
        if latest:
            data["content_restriction"] = reason
        notification.data = data
        notification.save(update_fields=["body", "data"])
        count += 1
    return count
