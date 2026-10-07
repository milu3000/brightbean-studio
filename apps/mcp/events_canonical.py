"""Fresh canonical source checks for reference-only MCP event delivery.

No retained content is selected. Missing source proof pauses a pending callback;
only actual expiry/withdrawal or an invalid message scope cancels it.
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.dispatch import receiver
from django.utils import timezone

from apps.inbox.conversation_workflow import canonical_actionable_observed
from apps.inbox.models import ConversationMessage, ConversationSyncIdentity, InboxMessage
from apps.inbox.sync_identity import SyncError, canonical_read_connection
from apps.inbox.sync_observations import canonical_content_restricted
from apps.mcp.models import EventOutbox
from apps.social_accounts.models import SocialAccount

SOURCE_UNAVAILABLE = "canonical_source_unavailable"
WITHDRAWN = {"removed", "withdrawn", "deleted", "unsent"}


def canonical_for_legacy(message):
    """A native ID match also protects historical deliveries predating a link."""
    return (
        ConversationMessage.objects.filter(
            Q(legacy_message_id=message.pk)
            | Q(social_account_id=message.social_account_id, platform_message_id=message.platform_message_id)
        )
        .only("pk", "workspace_id", "social_account_id", "platform_message_id", "legacy_message_id")
        .order_by("pk")
        .first()
    )


def canonical_source(message_id, *, expected_generation=None, expected_revision=None, require_live=True):
    """Return the fresh row, immutable connection generation, and bounded reason."""
    row = (
        ConversationMessage.objects.select_related("conversation", "observation_state")
        .defer(
            "body",
            "attachments",
            "observation_state__retained_body",
            "observation_state__retained_attachments",
            "observation_state__retained_legacy_body",
            "observation_state__retained_legacy_attachments",
        )
        .filter(pk=message_id)
        .first()
    )
    if row is None:
        return None, None, "message_scope_changed"
    state = getattr(row, "observation_state", None)
    now = timezone.now()
    if row.is_deleted or row.content_status in WITHDRAWN or (state and state.withdrawn_at):
        return row, None, "content_withdrawn"
    if row.content_status == "expired" or (state and state.expired_at):
        return row, None, "content_expired"
    account = (
        SocialAccount.objects.filter(
            pk=row.social_account_id,
            workspace_id=row.workspace_id,
            platform=row.platform,
            connection_status="connected",
        )
        .only("id", "workspace_id", "platform", "account_platform_id", "webhook_target_id", "connection_status")
        .first()
    )
    conversation = row.conversation
    if account is None or conversation is None:
        return row, None, SOURCE_UNAVAILABLE
    try:
        connection = canonical_read_connection(account)
    except SyncError:
        return row, None, SOURCE_UNAVAILABLE
    own = {account.account_platform_id, account.webhook_target_id} - {"", None}
    if (
        (conversation.workspace_id, conversation.social_account_id, conversation.platform)
        != (row.workspace_id, row.social_account_id, row.platform)
        or not row.platform_message_id
        or row.direction != "inbound"
        or row.delivery_status != "observed"
        or row.conversation_type != "direct"
        or conversation.conversation_type != "direct"
        or conversation.peer_ambiguous
        or not row.sender_id
        or row.sender_id != conversation.peer_id
        or row.sender_id in own
        or row.recipient_id not in own
        or row.legacy_reply_id is not None
        or row.occurred_at is None
        or timezone.is_naive(row.occurred_at)
        or row.occurred_at > now + timedelta(seconds=5)
    ):
        return row, None, "message_scope_changed"
    if (
        row.legacy_message_id
        and not InboxMessage.objects.filter(
            pk=row.legacy_message_id,
            workspace_id=row.workspace_id,
            social_account_id=row.social_account_id,
            platform_message_id=row.platform_message_id,
            message_type="dm",
        ).exists()
    ):
        return row, None, "message_scope_changed"
    # Existing additive legacy projections have no durable connection. Preserve
    # that route, but only after the same fresh restriction and direction checks.
    if connection is None and not require_live and expected_generation is None and state is None:
        return row, None, ""
    if (
        connection is None
        or state is None
        or state.connection_generation != connection.generation
        or (expected_generation is not None and expected_generation != connection.generation)
        or conversation.identity_kind != "platform"
        or not conversation.platform_conversation_id
        or not ConversationSyncIdentity.objects.filter(
            conversation_id=conversation.pk,
            connection_id=connection.pk,
            connection_generation=connection.generation,
        ).exists()
    ):
        return row, None, SOURCE_UNAVAILABLE
    if require_live and (
        not connection.bootstrap_baseline_at
        or conversation.workflow_baseline_at != connection.bootstrap_baseline_at
        or conversation.workflow_state is None
        or row.occurred_at <= connection.bootstrap_baseline_at
        or row.incoming_generation is None
        or row.incoming_generation < 1
        or row.incoming_generation > conversation.incoming_generation
        or (expected_revision is not None and row.incoming_generation != expected_revision)
    ):
        return row, None, SOURCE_UNAVAILABLE
    return row, connection.generation, ""


def delivery_source_error(delivery, subscription):
    """Recheck both canonical deliveries and legacy rows mapped since enqueue."""
    legacy = InboxMessage.objects.defer("body").filter(pk=delivery.message_id).first() if delivery.message_id else None
    if delivery.message_id and (
        legacy is None
        or legacy.workspace_id != subscription.workspace_id
        or legacy.social_account_id != subscription.social_account_id
        or legacy.message_type != "dm"
    ):
        return "message_scope_changed"
    if legacy is not None:
        extra = legacy.extra if isinstance(legacy.extra, dict) else {}
        from providers.meta_inbox_content import is_deleted_content

        if is_deleted_content(extra) or extra.get("canonical_content_restriction") == "withdrawn":
            return "content_withdrawn"
        if extra.get("canonical_content_restriction") == "expired":
            return "content_expired"
        if extra.get("is_echo") or extra.get("is_self") or extra.get("direction") == "outbound":
            return "message_scope_changed"
    mapped = canonical_for_legacy(legacy) if legacy is not None else None
    canonical_id = delivery.canonical_message_id or (mapped.pk if mapped else None)
    if canonical_id:
        row, generation, error = canonical_source(
            canonical_id,
            expected_generation=delivery.canonical_generation,
            expected_revision=delivery.canonical_event_revision,
            require_live=delivery.canonical_generation is not None,
        )
        if error:
            return error
        if generation is not None:
            from apps.inbox.canonical_access import enabled
            from apps.inbox.conversation_policy import read_allowed

            # Persist the observation before rollout/read availability changes.
            # Delivery waits until the emitted real ID is actually resolvable.
            if not enabled() or not read_allowed(row.social_account):
                return SOURCE_UNAVAILABLE
        if (
            row.workspace_id != subscription.workspace_id
            or row.social_account_id != subscription.social_account_id
            or (mapped is not None and mapped.pk != row.pk)
            or (legacy is not None and legacy.platform_message_id != row.platform_message_id)
        ):
            return "message_scope_changed"
        if delivery.canonical_generation is not None:
            from apps.mcp.events import _event_id

            if delivery.event_id != _event_id(row, subscription):
                return "message_scope_changed"
    elif legacy is None:
        return "message_scope_changed"
    else:
        from apps.inbox.sync_identity import canonical_owns_account

        if canonical_owns_account(legacy.social_account):
            # A pre-cutover row cannot bypass the newly authoritative ledger.
            return SOURCE_UNAVAILABLE
    return ""


@receiver(canonical_actionable_observed, dispatch_uid="mcp.canonical_actionable_event")
def receive_canonical_actionable(sender, *, message, source, event_revision, **kwargs):
    from apps.mcp.events import enqueue_canonical_event

    enqueue_canonical_event(message, source=source, event_revision=event_revision)


@receiver(canonical_content_restricted, dispatch_uid="mcp.canonical_event_restriction")
def cancel_restricted_events(sender, *, message, reason, **kwargs):
    if reason not in {"withdrawn", "expired"}:
        return
    # Validate persisted restriction instead of trusting a stale signal object.
    row, _generation, error = canonical_source(message.pk, require_live=False)
    if row is None or error not in {"content_withdrawn", "content_expired"}:
        return
    matching = Q(canonical_message_id=row.pk) | Q(
        message__social_account_id=row.social_account_id,
        message__workspace_id=row.workspace_id,
        message__platform_message_id=row.platform_message_id,
    )
    if row.legacy_message_id:
        matching |= Q(message_id=row.legacy_message_id)
    with transaction.atomic():
        EventOutbox.objects.filter(matching).exclude(
            status__in=[EventOutbox.Status.DELIVERED, EventOutbox.Status.CANCELLED]
        ).update(status=EventOutbox.Status.CANCELLED, last_error=error)
