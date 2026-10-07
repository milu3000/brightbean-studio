"""One canonical reducer: persisted proof precedes workflow/notification signals."""

from django.dispatch import Signal
from django.utils import timezone

from .models import ConversationMessage, ConversationObservationState
from .sync_contracts import (  # noqa: F401 (stable integration imports)
    ConversationObservation,
    MessageObservation,
    SyncPage,
    calendar_months,
    content_fingerprint,
    validate_observation,
)
from .sync_identity import SyncError, assert_conversation_provenance, bind_new_conversation

canonical_content_restricted = Signal()


def _revision_order(state, observation):
    for old, new in (
        (state.provider_revision, observation.provider_revision),
        (state.provider_updated_at, observation.provider_updated_at),
    ):
        if old is not None and new is not None:
            return (new > old) - (new < old)
    return None


def reduce_observation(account, connection, observation, *, context, allow_unassigned=False):
    """Caller holds the account/connection lock and atomic page/receipt transaction."""
    from .conversation_workflow import LiveObservation, observe_canonical_message
    from .conversations import upsert_conversation_message
    from .durable_sync import claim_ownership

    if allow_unassigned and (observation.source != "webhook" or observation.conversation_id):
        raise SyncError("invalid_observation")
    validate_observation(
        observation, now=timezone.now(), allow_unattributed_withdrawal=True, allow_pending_identity=allow_unassigned
    )
    row = ConversationMessage.objects.filter(
        social_account=account,
        workspace_id=account.workspace_id,
        platform=account.platform,
        platform_message_id=observation.platform_message_id,
    ).first()
    newly_seen = row is None
    from .sync_owned import capture_owned_transition, observe_owned_transition

    owned_before = capture_owned_transition(account, row, observation.conversation_id)
    state = ConversationObservationState.objects.filter(message=row).first() if row else None
    if row is not None and (state is None or state.connection_generation != connection.generation):
        raise SyncError("canonical_provenance_unverified")
    if observation.conversation_id:
        assert_conversation_provenance(account, connection, observation.conversation_id, observation.participant_ids)
    elif not observation.withdrawn_verified and (not allow_unassigned or (row and row.conversation_id)):
        raise SyncError("native_identity_pending")
    claim_ownership(connection)
    order = _revision_order(state, observation) if state else None
    effective_attachments = (
        observation.attachments if observation.attachments_complete or row is None else tuple(row.attachments)
    )
    same = bool(
        row
        and content_fingerprint(row.body, row.attachments)
        == content_fingerprint(observation.body, effective_attachments)
    )
    current_snapshot = bool(
        observation.snapshot_started_at is not None
        and (row is None or observation.snapshot_started_at >= (state.last_observed_at if state else row.updated_at))
    )
    withdrawn = observation.withdrawn_verified or bool(row and row.is_deleted)
    expired = bool((row and row.content_status == "expired") or (state and state.expired_at))
    accept = bool(
        (row is None or same or order == 1 or current_snapshot)
        and order != -1
        and observation.content_available
        and not withdrawn
        and not expired
    )
    previous_body, previous_attachments = (row.body, row.attachments) if row else ("", [])
    extra = {
        "conversation_id": observation.conversation_id,
        "message_recipient_id": observation.recipient_id,
        "inbox_attachments": list(effective_attachments) if accept else previous_attachments,
    }
    if observation.content_fetch_status == "basic_fallback" or (
        observation.content_fetch_status == "fields_requested" and accept
    ):
        extra["content_fetch_status"] = observation.content_fetch_status
    if observation.participant_ids and not allow_unassigned:
        extra["participant_ids"] = list(observation.participant_ids)
    if observation.conversation_type:
        extra["conversation_type"] = observation.conversation_type
    if observation.classification_reason:
        extra["classification_reason"] = observation.classification_reason
    direction = None
    if allow_unassigned:
        # Endpoints may prove direction, never a direct conversation identity.
        own = {account.account_platform_id, account.webhook_target_id} - {""}
        direction = "unknown"
        if observation.sender_id in own:
            direction = "outbound"
        elif observation.recipient_id in own and observation.sender_id and not observation.outbound_verified:
            direction = "inbound"
        elif observation.outbound_verified and observation.recipient_id not in own:
            direction = "outbound"
        if extra.get("conversation_type") != "group":
            extra["conversation_type"], extra["classification_reason"] = "unknown", "participants_missing"
    if observation.outbound_verified and (not allow_unassigned or direction == "outbound"):
        extra["direction"] = "outbound"
    if withdrawn:
        extra["is_deleted"] = True
    row = upsert_conversation_message(
        account,
        platform_message_id=observation.platform_message_id,
        sender_id=observation.sender_id,
        sender_name=observation.sender_name,
        body=observation.body if accept else previous_body,
        extra=extra,
        occurred_at=observation.occurred_at,
        source=observation.source,
        content_authority=accept,
        suppress_work=True,
        direction=direction,
    )
    if row is None:
        raise SyncError("enrollment_or_identity_revoked")
    if allow_unassigned and row.conversation_id is not None:
        raise SyncError("native_identity_pending")
    identity = bind_new_conversation(row.conversation, connection) if row.conversation_id else None
    if state is None:
        state = ConversationObservationState(
            message=row,
            connection_generation=connection.generation,
            content_fingerprint=content_fingerprint(row.body, row.attachments),
            last_observed_at=observation.observed_at,
            expires_at=calendar_months(row.occurred_at or row.first_seen_at, 6),
        )
    if accept:
        state.content_fingerprint = content_fingerprint(row.body, row.attachments)
        if observation.provider_updated_at is not None:
            state.provider_updated_at = observation.provider_updated_at
        if observation.provider_revision is not None:
            state.provider_revision = observation.provider_revision
        if current_snapshot or order == 1:
            state.repair_required = False
    elif not withdrawn and not expired and observation.content_available and not same and order != -1:
        state.repair_required = True
        state.conflict_count = min(65535, state.conflict_count + 1)
    if not observation.attachments_complete and not withdrawn and not expired:
        state.repair_required = True
    newly_withdrawn = observation.withdrawn_verified and state.withdrawn_at is None
    if newly_withdrawn:
        state.withdrawn_at = observation.observed_at
        if state.expired_at is None:
            state.retained_body, state.retained_attachments = previous_body, previous_attachments
    state.last_observed_at = max(state.last_observed_at, observation.observed_at)
    # Refined original occurrence can shorten a fallback deadline, never extend it.
    if row.occurred_at is not None:
        state.expires_at = min(state.expires_at, calendar_months(row.occurred_at, 6))
    state.save()
    # All provenance above is in place before any consumer's fresh privacy check.
    if newly_withdrawn:
        _redact_legacy_projection(row, state)
        canonical_content_restricted.send(sender=ConversationMessage, message=row, reason="withdrawn")
    baseline = connection.bootstrap_baseline_at
    from .models import InboxSyncReceipt

    signed_live = bool(
        baseline
        and observation.occurred_at
        and observation.occurred_at > baseline
        and row.conversation_id
        and InboxSyncReceipt.objects.filter(
            connection=connection,
            connection_generation=connection.generation,
            platform_message_id=row.platform_message_id,
            kind="signed_message",
            context="live",
            observed_at__gte=observation.occurred_at,
            observed_at__lte=observation.observed_at,
        )
        .exclude(status="quarantined")
        .exists()
    )
    if row.conversation_id:
        InboxSyncReceipt.objects.filter(
            connection=connection,
            connection_generation=connection.generation,
            platform_message_id=row.platform_message_id,
            status="unassigned",
        ).update(status="processed", last_error_code="", updated_at=timezone.now())
    live = bool(
        (context == "live" or signed_live)
        and baseline
        and observation.occurred_at
        and observation.occurred_at > baseline
        and not withdrawn
        and not expired
        and row.conversation_id
    )
    actionable_transition = False
    if live and not (row.direction == "outbound" and row.legacy_reply_id):
        # An app receipt already settled its pinned incoming generation. A
        # native echo, including a refined timestamp, is no new human answer.
        conversation = row.conversation
        direct_thread = bool(
            row.conversation_type == "direct"
            and conversation.conversation_type == "direct"
            and not conversation.peer_ambiguous
            and conversation.peer_id
        )
        direct = direct_thread and row.sender_id == conversation.peer_id
        initialize_outbound = bool(
            context == "live"
            and newly_seen
            and direct_thread
            and row.direction == "outbound"
            and row.sender_id in {connection.account_platform_id, connection.webhook_target_id} - {"", None}
            and row.recipient_id == conversation.peer_id
            and conversation.workflow_state is None
            and identity is not None
            and identity.created_at >= baseline
            and not ConversationMessage.objects.filter(conversation=conversation).exclude(pk=row.pk).exists()
        )
        if initialize_outbound:
            conversation.workflow_baseline_at = baseline
            conversation.save(update_fields=["workflow_baseline_at", "updated_at"])
        if (
            direct
            and row.direction == "inbound"
            and conversation.workflow_state is None
            and identity is not None
            and identity.created_at >= baseline
        ):
            # A genuinely post-cutover discovered thread may start new work.
            # Initial reviewed threads with an explicit hold remain held.
            conversation.workflow_state = "needs_action"
            conversation.workflow_baseline_at = baseline
            conversation.save(update_fields=["workflow_state", "workflow_baseline_at", "updated_at"])
        first_actionable = bool(
            direct
            and state.actionable_observed_at is None
            and (state.live_conversation_id is None or state.live_conversation_id == row.conversation_id)
        )
        result = observe_canonical_message(
            row,
            source=observation.source,
            observation=LiveObservation(baseline, observation.observed_at),
            is_new=state.live_observed_at is None,
            first_actionable=first_actionable,
            initialize_outbound=initialize_outbound,
        )
        row.refresh_from_db()
        if row.incoming_generation is not None and state.live_observed_at is None:
            state.live_observed_at, state.live_conversation_id = observation.observed_at, row.conversation_id
        actionable_transition = bool(result and getattr(result, "actionable_observed", False))
        if actionable_transition:
            state.actionable_observed_at = state.actionable_observed_at or observation.observed_at
        state.save(update_fields=["live_observed_at", "live_conversation_id", "actionable_observed_at", "updated_at"])
    observe_owned_transition(
        account,
        row,
        owned_before,
        source=observation.source,
        actionable_transition=actionable_transition,
        live=live,
    )
    return row


def _legacy_archive_own_proof(row, state, extra):
    """Require captured native endpoint proof before presenting an old copy."""
    from .models import InboxSyncConnection
    from .sync_contracts import valid_id

    connection = InboxSyncConnection.objects.filter(
        social_account_id=row.social_account_id,
        workspace_id=row.workspace_id,
        platform=row.platform,
        generation=state.connection_generation,
    ).first()
    if connection is None:
        return False
    own = {connection.account_platform_id, connection.webhook_target_id} - {""}

    def endpoint(flat_names, nested_name):
        nested = extra.get(nested_name)
        values = [extra.get(name) for name in flat_names]
        values.append(nested.get("id") if isinstance(nested, dict) else None)
        values = [value for value in values if value not in (None, "")]
        if not values or any(not valid_id(value) for value in values) or len(set(values)) != 1:
            return ""
        return values[0]

    sender = endpoint(("sender_id",), "sender")
    recipient = endpoint(("message_recipient_id", "recipient_id"), "recipient")
    return bool(sender in own or (sender and sender not in own and recipient in own))


def _redact_legacy_projection(row, state):
    """Preserve differing old copies before removing fallback content fields."""
    from providers.meta_inbox_content import normalize_attachments

    from .models import InboxMessage

    legacy = (
        InboxMessage.objects.select_for_update()
        .filter(
            workspace_id=row.workspace_id,
            social_account_id=row.social_account_id,
            platform_message_id=row.platform_message_id,
            message_type="dm",
        )
        .first()
    )
    if legacy is None:
        return
    extra = legacy.extra if isinstance(legacy.extra, dict) else {}
    attachments = normalize_attachments(extra)
    archive_proven = _legacy_archive_own_proof(row, state, extra)
    if legacy.body and legacy.body != state.retained_body:
        state.retained_legacy_body = legacy.body
    if attachments and attachments != state.retained_attachments:
        state.retained_legacy_attachments = attachments
    state.save(update_fields=["retained_legacy_body", "retained_legacy_attachments", "updated_at"])
    keys = {
        "sender_id",
        "message_recipient_id",
        "recipient_id",
        "conversation_id",
        "participant_ids",
        "conversation_type",
        "classification_reason",
        "direction",
        "canonical_message_id",
        "canonical_transport_projection",
        "transport_projection",
    }
    legacy.extra = {key: value for key, value in extra.items() if key in keys}
    legacy.extra.update(is_deleted=True, canonical_content_restriction="withdrawn")
    if archive_proven:
        legacy.extra["canonical_retained_legacy_proof"] = {
            "message_id": str(row.pk),
            "generation": str(state.connection_generation),
        }
    legacy.body = ""
    legacy.save(update_fields=["body", "extra"])


def _legacy_archive_matches(row, state):
    from .models import InboxMessage

    return InboxMessage.objects.filter(
        workspace_id=row.workspace_id,
        social_account_id=row.social_account_id,
        social_account__platform=row.platform,
        social_account__workspace_id=row.workspace_id,
        platform_message_id=row.platform_message_id,
        message_type="dm",
        extra__canonical_retained_legacy_proof={
            "message_id": str(row.pk),
            "generation": str(state.connection_generation),
        },
    ).exists()


def withdrawn_content_available_for_internal_review(row):
    """Metadata-only capability after the caller's fresh human READ scope check."""
    if row.content_status == "expired":
        return False
    query = ConversationObservationState.objects.filter(
        message_id=row.pk, withdrawn_at__isnull=False, expired_at__isnull=True
    )
    state = query.only("message_id", "connection_generation").first()
    if state is None:
        return False
    if query.exclude(retained_body="", retained_attachments=[]).exists():
        return True
    return bool(
        query.exclude(retained_legacy_body="", retained_legacy_attachments=[]).exists()
        and _legacy_archive_matches(row, state)
    )


def withdrawn_content_for_internal_review(row, *, now=None):
    """Caller must obtain the row through current actor/account read scope first."""
    state = ConversationObservationState.objects.filter(message=row).first()
    # The staged deadline is metadata only until an approved expiry transition.
    if state is None or not state.withdrawn_at or state.expired_at or row.content_status == "expired":
        return None
    body, attachments, source = state.retained_body, state.retained_attachments, "canonical_captured"
    if (
        not body
        and not attachments
        and (state.retained_legacy_body or state.retained_legacy_attachments)
        and _legacy_archive_matches(row, state)
    ):
        body, attachments, source = state.retained_legacy_body, state.retained_legacy_attachments, "legacy_captured"
    return {
        "withdrawn": True,
        "body": body,
        "attachments": attachments,
        "expires_at": state.expires_at,
        "body_source": source,
    }


def record_app_send_provenance(row, reply):
    """An actual accepted receipt is immediately readable, never native-delivered proof."""
    from .models import InboxConversation
    from .sync_identity import canonical_read_connection

    if row is None or reply.status != "sent" or not reply.platform_reply_id or reply.sent_at is None:
        return False
    connection = canonical_read_connection(row.social_account)
    if connection is None:
        return False
    intended = InboxConversation.objects.filter(
        pk=reply.conversation_id,
        social_account_id=connection.social_account_id,
        workspace_id=connection.workspace_id,
        platform=connection.platform,
        platform_conversation_id=reply.platform_conversation_id,
        peer_id=reply.recipient_id,
        conversation_type="direct",
        peer_ambiguous=False,
        sync_identity__connection=connection,
        sync_identity__connection_generation=connection.generation,
    ).first()
    if (
        intended is None
        or row.conversation_id not in {None, intended.pk}
        or reply.connection_generation != connection.generation
        or reply.account_platform_id != connection.account_platform_id
        or row.platform_message_id != reply.platform_reply_id
        or row.legacy_reply_id != reply.pk
        or row.direction != "outbound"
        or row.sender_id != connection.account_platform_id
        or row.recipient_id != reply.recipient_id
        or reply.send_generation < 1
    ):
        return False
    if (
        reply.dm_send_attempts.exists()
        and not reply.dm_send_attempts.filter(
            outcome="sent",
            completed_at__isnull=False,
            control__social_account_id=connection.social_account_id,
            control__workspace_id=connection.workspace_id,
            control__account_platform_id=connection.account_platform_id,
        ).exists()
    ):
        return False
    if row.conversation_id is None:
        # A confirmed app receipt belongs to the selected, pinned send intent.
        # Empty attribution deliberately does not claim native thread observation;
        # a later exact-ID provider echo may establish its actual placement.
        row.conversation = intended
        row.conversation_attribution = ""
        row.save(update_fields=["conversation", "conversation_attribution", "updated_at"])
    state, _ = ConversationObservationState.objects.get_or_create(
        message=row,
        defaults={
            "connection_generation": connection.generation,
            "content_fingerprint": content_fingerprint(row.body, row.attachments),
            "last_observed_at": timezone.now(),
            "expires_at": calendar_months(reply.sent_at, 6),
        },
    )
    return state.connection_generation == connection.generation
