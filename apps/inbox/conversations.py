"""Canonical, account-serialized writes to the optional conversation ledger.

This module never creates legacy work, emits events, sends, or infers identity
from names, handles, text, or time. Callers may supply provider observations;
only a small normalized projection is retained.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from providers.meta_inbox_content import (
    _identity_ids,
    classify_conversation_identity,
    is_deleted_content,
    merge_content_status_evidence,
    merge_conversation_classification,
    merge_message_extra,
    merged_content_status,
    normalize_attachments,
)

from .conversation_policy import capture_allowed, enabled  # noqa: F401 (compatibility import)
from .locking import lock_dm_account
from .models import (
    ConversationMessage,
    ConversationSyncState,
    ConversationWorkState,
    InboxConversation,
    InboxMessage,
    InboxReply,
    SendOperation,
)

_SOURCES = {"poll", "webhook", "app_send", "legacy_backfill"}


def _id(value):
    if isinstance(value, bool) or not isinstance(value, str | int):
        return ""
    value = str(value)
    return value if value and len(value) <= 255 and not any(char.isspace() for char in value) else ""


def _dict(value):
    return value if isinstance(value, dict) else {}


def _timestamp(value):
    if (
        not isinstance(value, datetime)
        or timezone.is_naive(value)
        or value <= datetime(1970, 1, 1, tzinfo=UTC)
        or value > timezone.now() + timedelta(minutes=5)
    ):
        return None
    return value


def _scope(account):
    return {"workspace_id": account.workspace_id, "social_account_id": account.pk, "platform": account.platform}


def _identities(account, sender_id, extra, direction):
    own = {_id(account.account_platform_id), _id(account.webhook_target_id)} - {""}
    sender_id = _id(sender_id) or _id(extra.get("sender_id")) or _id(_dict(extra.get("sender")).get("id"))
    recipient_id = _id(extra.get("message_recipient_id")) or _id(_dict(extra.get("recipient")).get("id"))
    kind, reason, peer = classify_conversation_identity(extra, own_ids=own, sender_id=sender_id)
    if kind == "direct" and not recipient_id:
        participants = _identity_ids(extra.get("participant_ids", extra.get("participants"))) or []
        recipient_id = peer if sender_id in own else next(iter(own & set(participants)), "")
    return sender_id, recipient_id, peer, kind, reason


def _apply_classification(account, conversation, peer_id, kind, reason):
    """Late conflicting/group evidence fences work and withdraws pair guesses."""
    before = (
        conversation.conversation_type,
        conversation.classification_reason,
        conversation.peer_id,
        conversation.peer_ambiguous,
    )
    if kind == "direct" and conversation.peer_id and peer_id and peer_id != conversation.peer_id:
        kind, reason = "unknown", "identity_conflict"
    kind, reason = merge_conversation_classification(
        conversation.conversation_type, conversation.classification_reason, kind, reason
    )
    # Pre-migration ambiguous records cannot be upgraded by a later pair.
    if conversation.peer_ambiguous and kind != "group":
        kind, reason = "unknown", "identity_conflict"
    conversation.conversation_type, conversation.classification_reason = kind, reason
    if kind == "direct":
        conversation.peer_id = peer_id or conversation.peer_id
    else:
        conversation.peer_id = ""
        conversation.peer_ambiguous = kind == "group" or reason == "identity_conflict"
    after = (kind, reason, conversation.peer_id, conversation.peer_ambiguous)
    if before == after:
        return
    conversation.revision += 1
    conversation.save(
        update_fields=[
            "conversation_type",
            "classification_reason",
            "peer_id",
            "peer_ambiguous",
            "revision",
            "updated_at",
        ]
    )
    if kind != "direct":
        ConversationMessage.objects.filter(
            **_scope(account), conversation=conversation, conversation_attribution="verified_peer"
        ).update(conversation=None, conversation_attribution="", incoming_generation=None, updated_at=timezone.now())
        from .reply_coordination import invalidate_conversations

        invalidate_conversations(account, [conversation.pk])


def _clear_ambiguous_peer_links(account, peer_id):
    """Retract fallback guesses when more than one native thread is discovered."""
    conversations = InboxConversation.objects.filter(**_scope(account), peer_id=peer_id)
    if not peer_id or conversations.filter(platform_conversation_id__isnull=False).count() < 2:
        return
    inferred = ConversationMessage.objects.filter(
        **_scope(account), conversation__in=conversations, conversation_attribution="verified_peer"
    )
    affected = list(inferred.values_list("conversation_id", flat=True).distinct())
    inferred.update(conversation=None, conversation_attribution="", incoming_generation=None, updated_at=timezone.now())
    conversations.filter(pk__in=affected).update(revision=F("revision") + 1, updated_at=timezone.now())
    from .reply_coordination import invalidate_conversations

    invalidate_conversations(account, list(conversations.values_list("pk", flat=True)))


def _conversation(account, provider_id, peer_id, *, kind="unknown", reason="participants_missing"):
    """Use a real thread first, or an exact, unambiguous one-to-one identity.

    A pair-only identity can be promoted once to a provider conversation. If
    multiple real threads name that peer, future pair-only events stay
    unassigned. Existing provider identities are never merged by peer alone.
    """
    scoped = InboxConversation.objects.filter(**_scope(account))
    if provider_id:
        conversation = scoped.filter(platform_conversation_id=provider_id).first()
        if conversation:
            _apply_classification(account, conversation, peer_id, kind, reason)
            if conversation.conversation_type != "direct":
                return conversation
            _clear_ambiguous_peer_links(account, peer_id)
            if peer_id and scoped.filter(peer_id=peer_id, platform_conversation_id__isnull=False).count() == 1:
                fallback = scoped.filter(
                    peer_id=peer_id, platform_conversation_id__isnull=True, conversation_type="direct"
                ).first()
                if fallback and not (
                    # Preserve local uncertainty/pause records across identity
                    # changes. Never move an operation onto another thread.
                    fallback.send_operations.exists() or hasattr(fallback, "reply_work_state")
                ):
                    moved = ConversationMessage.objects.filter(**_scope(account), conversation=fallback).update(
                        conversation=conversation, incoming_generation=None, updated_at=timezone.now()
                    )
                    if moved:
                        scoped.filter(pk=conversation.pk).update(revision=F("revision") + 1, updated_at=timezone.now())
                    fallback.delete()
                elif fallback:
                    from .reply_coordination import invalidate_conversations

                    invalidate_conversations(account, [fallback.pk, conversation.pk])
            return conversation
        if (
            peer_id
            and kind == "direct"
            and not scoped.filter(peer_id=peer_id, platform_conversation_id__isnull=False).exists()
        ):
            fallback = scoped.filter(
                peer_id=peer_id, platform_conversation_id__isnull=True, conversation_type="direct"
            ).first()
            if fallback:
                fallback.platform_conversation_id = provider_id
                fallback.identity_kind = InboxConversation.IdentityKind.PLATFORM
                fallback.revision += 1
                fallback.save(update_fields=["platform_conversation_id", "identity_kind", "revision", "updated_at"])
                return fallback
        conversation = InboxConversation.objects.create(
            **_scope(account),
            platform_conversation_id=provider_id,
            peer_id=peer_id if kind == "direct" else "",
            peer_ambiguous=kind == "group" or reason == "identity_conflict",
            conversation_type=kind,
            classification_reason=reason,
            identity_kind=InboxConversation.IdentityKind.PLATFORM,
        )
        _clear_ambiguous_peer_links(account, peer_id)
        return conversation
    if not peer_id:
        return None
    candidates = list(scoped.filter(peer_id=peer_id, peer_ambiguous=False, conversation_type="direct")[:2])
    if len(candidates) > 1:
        return None
    if candidates:
        return candidates[0]
    if scoped.filter(peer_id=peer_id, platform_conversation_id__isnull=True).exists():
        return None  # Existing unverified rows are not silently backfilled.
    return InboxConversation.objects.create(
        **_scope(account),
        peer_id=peer_id,
        identity_kind=InboxConversation.IdentityKind.VERIFIED_PEER,
        conversation_type=kind,
        classification_reason=reason,
    )


@transaction.atomic
def upsert_conversation_message(
    account,
    *,
    platform_message_id,
    sender_id="",
    sender_name="",
    body="",
    extra=None,
    occurred_at=None,
    source="poll",
    legacy_message=None,
    legacy_reply=None,
    direction=None,
    content_authority=None,
    suppress_work=False,
):
    """Persist an observation, returning its canonical row (or None if disabled).

    The same account lock is used by sends, webhooks and polling. Native IDs
    reconcile only within that account/platform/workspace. Missing IDs require
    a linked SENT legacy reply and stay delivery-unverified, never fabricated.
    """
    if not capture_allowed(account) or source not in _SOURCES:
        return None
    expected_platform = account.platform
    account = lock_dm_account(account.pk, account.workspace_id)
    if account is None or account.platform != expected_platform or not capture_allowed(account):
        return None
    from .sync_identity import SyncError, canonical_owns_account, canonical_read_connection

    if canonical_owns_account(account):
        try:
            canonical_read_connection(account)
        except SyncError:
            return None
        if content_authority is None and source != "app_send":
            return None  # A claimed durable owner never reopens old writers.
    extra = _dict(extra)
    provider_id = _id(platform_message_id)
    if legacy_message is not None and (
        legacy_message.workspace_id != account.workspace_id
        or legacy_message.social_account_id != account.pk
        or legacy_message.message_type != InboxMessage.MessageType.DM
    ):
        raise ValueError("Legacy message is outside the conversation scope.")
    if legacy_reply is not None:
        original = legacy_reply.inbox_message
        if (
            original.workspace_id != account.workspace_id
            or original.social_account_id != account.pk
            or original.message_type != InboxMessage.MessageType.DM
            or legacy_reply.status != InboxReply.Status.SENT
        ):
            raise ValueError("Legacy reply is outside the conversation scope or not sent.")
    if not provider_id and legacy_reply is None:
        return None
    from .tasks import _is_outgoing_dm

    outgoing = _is_outgoing_dm(account, sender_id, extra, platform_message_id=provider_id)
    if legacy_reply is not None or outgoing:
        direction = ConversationMessage.Direction.OUTBOUND
    elif direction not in ConversationMessage.Direction.values:
        direction = ConversationMessage.Direction.INBOUND if _id(sender_id) else ConversationMessage.Direction.UNKNOWN
    sender_id, recipient_id, peer_id, kind, reason = _identities(account, sender_id, extra, direction)
    scoped = ConversationMessage.objects.filter(**_scope(account))
    row = scoped.filter(platform_message_id=provider_id).first() if provider_id else None
    local_row = scoped.filter(legacy_reply=legacy_reply).first() if legacy_reply else None
    if row is None:
        row = local_row
    if row is None:
        row = ConversationMessage(**_scope(account), platform_message_id=provider_id or None)
    meaningful_fields = (
        "conversation_id",
        "conversation_attribution",
        "platform_message_id",
        "direction",
        "conversation_type",
        "classification_reason",
        "sender_id",
        "recipient_id",
        "sender_name",
        "body",
        "attachments",
        "content_status",
        "occurred_at",
        "is_deleted",
        "delivery_status",
        "legacy_message_id",
        "legacy_reply_id",
    )
    previous = {field: deepcopy(getattr(row, field)) for field in meaningful_fields} if not row._state.adding else None
    previous_conversation_id = row.conversation_id
    # Canonical target provenance survives attribution withdrawal to null.
    # Capture before merging/deleting a local row, whose FK may be SET_NULL.
    uncertain_source_ids = set(
        SendOperation.objects.filter(**_scope(account), target_id__in={row.pk, local_row.pk if local_row else None})
        .filter(Q(status="outcome_unknown") | Q(external_attempted_at__isnull=False))
        .values_list("conversation_id", flat=True)
    )
    removed_conversation_id = None
    if local_row and local_row.pk != row.pk:
        # Only an explicit reply link and its exact provider ID permits this
        # merge. Preserve tombstones and bump the removed row's thread too.
        removed_conversation_id = local_row.conversation_id
        row.sources = sorted(set(row.sources) | set(local_row.sources))
        row.conversation_type, row.classification_reason = merge_conversation_classification(
            local_row.conversation_type,
            local_row.classification_reason,
            row.conversation_type,
            row.classification_reason,
        )
        row.is_deleted = row.is_deleted or local_row.is_deleted
        row.body = row.body or local_row.body
        row.sender_id = row.sender_id or local_row.sender_id
        row.recipient_id = row.recipient_id or local_row.recipient_id
        row.occurred_at = row.occurred_at or local_row.occurred_at
        row.attachments = normalize_attachments(
            merge_message_extra({"inbox_attachments": local_row.attachments}, {"inbox_attachments": row.attachments})
        )
        row.content_status = merge_content_status_evidence(
            local_row.content_status,
            row.content_status,
            body=row.body,
            attachments=row.attachments,
            deleted=row.is_deleted,
        )
        local_row.delete()
    if provider_id and row.platform_message_id is None:
        row.platform_message_id = provider_id
    conversation_id = _id(extra.get("conversation_id"))
    if (
        row.conversation_type == "direct"
        and row.conversation_id
        and peer_id
        and row.conversation.peer_id
        and peer_id != row.conversation.peer_id
    ):
        kind, reason, peer_id = "unknown", "identity_conflict", ""
    if (
        conversation_id
        and row.conversation_id
        and row.conversation_attribution == "platform"
        and row.conversation.platform_conversation_id != conversation_id
    ):
        # A native message cannot prove membership in two different threads.
        kind, reason, peer_id = "unknown", "identity_conflict", ""
        conversation_id = row.conversation.platform_conversation_id
    kind, reason = merge_conversation_classification(row.conversation_type, row.classification_reason, kind, reason)
    if kind != "direct":
        peer_id = ""
    row.conversation_type, row.classification_reason = kind, reason
    if (
        row.conversation_id
        and row.conversation_attribution == "verified_peer"
        and kind != "direct"
        and reason != "participants_missing"
    ):
        _apply_classification(account, row.conversation, "", kind, reason)
        row.conversation = None
        row.conversation_attribution = ""
    # The same exact provider message ID bridges later native participant
    # evidence to its already-known thread. App intent cannot make that claim.
    if (
        row.conversation_id
        and row.conversation_attribution == "platform"
        and not conversation_id
        and source in {"poll", "webhook"}
    ):
        conversation_id = row.conversation.platform_conversation_id
    conversation = (
        row.conversation
        if row.conversation_id and row.conversation_attribution == "platform" and not conversation_id
        else _conversation(account, conversation_id, peer_id, kind=kind, reason=reason)
    )
    if conversation and (
        row.conversation_id is None
        or row.conversation_id == conversation.pk
        or (conversation_id and row.conversation_attribution != "platform")
    ):
        row.conversation = conversation
        if conversation_id:
            row.conversation_attribution = "platform"
        elif row.conversation_attribution != "platform":
            row.conversation_attribution = "verified_peer"
    elif peer_id and conversation is None and row.conversation_attribution == "verified_peer":
        row.conversation = None
        row.conversation_attribution = ""
    # Outbound proof and deletion are monotonic across stale source deliveries.
    if row.direction != ConversationMessage.Direction.OUTBOUND:
        row.direction = direction
    # Polls are fresh provider snapshots; webhooks may be delayed/replayed.
    # Neither app intent nor historical local copies can overwrite native text.
    source_rank = {"legacy_backfill": 0, "app_send": 1, "webhook": 2, "poll": 3}
    authoritative = (
        content_authority
        if isinstance(content_authority, bool)
        else source_rank[source] >= max((source_rank.get(item, -1) for item in row.sources), default=-1)
    )
    own_ids = {_id(account.account_platform_id), _id(account.webhook_target_id)} - {""}
    if (
        sender_id
        and (not row.sender_id or authoritative)
        and (row.direction != "outbound" or sender_id in own_ids or row.sender_id not in own_ids)
    ):
        row.sender_id = sender_id
    if recipient_id and (not row.recipient_id or authoritative):
        row.recipient_id = recipient_id
    name = sender_name[:255] if isinstance(sender_name, str) else ""
    if (
        name
        and (not row.sender_name or row.sender_name in {sender_id, row.sender_id, "Unknown"} or authoritative)
        and (name not in {sender_id, "Unknown"} or not row.sender_name)
    ):
        row.sender_name = name
    row.is_deleted = row.is_deleted or is_deleted_content(extra)
    if row.is_deleted:
        row.body = ""
        row.attachments = []
    else:
        if isinstance(body, str) and (body or content_authority is True) and (not row.body or authoritative):
            row.body = body
        projection = (
            extra if content_authority is True else merge_message_extra({"inbox_attachments": row.attachments}, extra)
        )
        row.attachments = normalize_attachments(projection)
    row.content_status = merged_content_status(
        row.content_status, extra, body=row.body, attachments=row.attachments, deleted=row.is_deleted
    )
    timestamp = _timestamp(occurred_at)
    if timestamp and (
        row.occurred_at is None or (source in {"poll", "webhook"} and not {"poll", "webhook"} & set(row.sources))
    ):
        row.occurred_at = timestamp
    row.sources = sorted(set(row.sources or []) | {source})
    if not provider_id and not row.platform_message_id:
        row.delivery_status = ConversationMessage.DeliveryStatus.DELIVERY_UNVERIFIED
    elif {"poll", "webhook"} & set(row.sources) or (legacy_message and not legacy_reply):
        row.delivery_status = ConversationMessage.DeliveryStatus.OBSERVED
    else:
        row.delivery_status = ConversationMessage.DeliveryStatus.PROVIDER_ACCEPTED
    if legacy_message and row.direction != ConversationMessage.Direction.OUTBOUND:
        row.legacy_message = legacy_message
    if legacy_reply:
        row.legacy_reply = legacy_reply
    if previous_conversation_id and row.conversation_id != previous_conversation_id:
        row.incoming_generation = None
    row.save()
    changed = bool(
        removed_conversation_id
        or previous is None
        or any(getattr(row, field) != previous[field] for field in meaningful_fields)
    )
    if changed:
        InboxConversation.objects.filter(
            **_scope(account), pk__in={removed_conversation_id, previous_conversation_id, row.conversation_id} - {None}
        ).update(revision=F("revision") + 1, updated_at=timezone.now())
    from .reply_coordination import invalidate_conversations, observe_message, quarantine_transferred_uncertainty

    old_conversation_ids = ({removed_conversation_id, previous_conversation_id} | uncertain_source_ids) - {
        None,
        row.conversation_id,
    }
    # Historical identity changes cannot erase uncertain outcomes either.
    # Quarantine is a safety hold with no due time, not queued inbound work.
    quarantine_transferred_uncertainty(account, old_conversation_ids, row.conversation_id)
    if source != "legacy_backfill" and not suppress_work:
        invalidate_conversations(account, old_conversation_ids)
    if not suppress_work:
        observe_message(
            row.pk,
            source=source,
            is_new=previous is None,
            previous_direction=previous["direction"] if previous else None,
            previous_conversation_id=previous_conversation_id,
            previous_attribution=previous["conversation_attribution"] if previous else "",
            changed=changed,
        )
    return row


@transaction.atomic
def link_legacy_message(row, message):
    """Link a freshly created legacy work item without replaying ingestion."""
    if row is None or row.legacy_message_id == message.pk:
        return
    if (row.workspace_id, row.social_account_id) != (message.workspace_id, message.social_account_id):
        raise ValueError("Legacy message is outside the conversation scope.")
    account = lock_dm_account(row.social_account_id, row.workspace_id)
    if account is None or account.platform != row.platform or not capture_allowed(account):
        return
    row = ConversationMessage.objects.filter(pk=row.pk, **_scope(account)).first()
    if row is None or row.legacy_message_id == message.pk:
        return
    conversation = (
        InboxConversation.objects.select_for_update().filter(pk=row.conversation_id, **_scope(account)).first()
        if row.conversation_id
        else None
    )
    row.legacy_message = message
    row.save(update_fields=["legacy_message", "updated_at"])
    if conversation:
        InboxConversation.objects.filter(pk=conversation.pk).update(
            revision=F("revision") + 1, updated_at=timezone.now()
        )
        # Metadata-only linking may advance an already-current snapshot, but
        # must never hide observations missed while coordination was disabled.
        ConversationWorkState.objects.filter(
            conversation=conversation, conversation_revision=conversation.revision, history_gap=False
        ).update(conversation_revision=F("conversation_revision") + 1, updated_at=timezone.now())


@transaction.atomic
def record_reply(reply, *, source="app_send"):
    """Mirror a legacy send without upgrading unsupported/local delivery claims."""
    if not capture_allowed(reply.inbox_message.social_account) or reply.status != InboxReply.Status.SENT:
        return None
    message = reply.inbox_message
    if message.message_type != InboxMessage.MessageType.DM:
        return None
    account = message.social_account
    extra = _dict(message.extra)
    # These are provider-scoped addressing IDs, never a display handle fallback.
    peer_id = (
        (_id(reply.recipient_id) if reply.conversation_id else "")
        or _id(extra.get("sender_id"))
        or _id(_dict(extra.get("sender")).get("id"))
    )
    outbound_extra = {"message_recipient_id": peer_id, "direction": "outbound"}
    # The send result contains a message ID, not a conversation ID. A legacy
    # parent thread is not proof that the provider placed this reply in it.
    # Preserve known group/ambiguity evidence so a direct peer guess cannot
    # turn that parent into an unrelated one-to-one conversation.
    for key in ("participant_ids", "participants", "conversation_type", "classification_reason"):
        if key in extra:
            outbound_extra[key] = extra[key]
    row = upsert_conversation_message(
        account,
        platform_message_id=reply.platform_reply_id,
        sender_id=account.account_platform_id,
        sender_name=account.account_name,
        body=reply.body,
        extra=outbound_extra,
        occurred_at=reply.sent_at,
        source=source,
        legacy_reply=reply,
    )
    from .conversation_workflow import record_composer_outcome
    from .sync_observations import record_app_send_provenance

    record_app_send_provenance(row, reply)
    if reply.conversation_id:
        record_composer_outcome(reply)
    return row


@transaction.atomic
def begin_sync(account, *, started_at=None):
    """Only supported instrumented Meta providers get a per-stream attempt."""
    if not capture_allowed(account):
        return None
    expected_platform = account.platform
    account = lock_dm_account(account.pk, account.workspace_id)
    if account is None or account.platform != expected_platform or not capture_allowed(account):
        return None
    started_at = started_at or timezone.now()
    for stream in ("dm", "comment"):
        ConversationSyncState.objects.update_or_create(
            **_scope(account),
            stream=stream,
            defaults={"status": "running", "last_attempt_at": started_at, "last_error_code": ""},
        )
    return started_at


@transaction.atomic
def finish_sync(account, *, started_at, stream_results=None, imported=False, error_code=""):
    """Never infer successful DM fetching from a mixed legacy poll result."""
    if not capture_allowed(account) or started_at is None:
        return
    expected_platform = account.platform
    account = lock_dm_account(account.pk, account.workspace_id)
    if account is None or account.platform != expected_platform or not capture_allowed(account):
        return
    for state in ConversationSyncState.objects.filter(**_scope(account), last_attempt_at=started_at):
        result = _dict(_dict(stream_results).get(state.stream))
        success = imported and result.get("status") == "success"
        state.status = (
            "success" if success else "failed" if error_code or result.get("status") == "failed" else "unknown"
        )
        if success:
            state.last_success_at = started_at
            state.coverage = "partial"
            state.last_error_code = ""
        else:
            # Keep historical coverage/freshness; neither an attempt nor a
            # comment-only success can freshen a previous DM observation.
            state.last_error_code = (
                error_code if error_code in {"provider_error", "import_error", "unsupported"} else ""
            )
            if result.get("status") == "failed":
                state.last_error_code = "provider_error"
        state.save(update_fields=["status", "coverage", "last_success_at", "last_error_code", "updated_at"])
