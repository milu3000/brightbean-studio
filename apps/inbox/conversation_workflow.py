"""Explicit normalized observations drive activity; receipt and read state are separate.

Reconstructed after source loss. Legacy callers supply no observation and remain
quiet. The sync reducer supplies evidence only after persisting current scoped
provenance; this module does not infer live delivery from poll/webhook labels.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.dispatch import Signal
from django.utils import timezone

from .dm_send_gate import DMSendGateError, _authorize
from .locking import lock_dm_account
from .models import ConversationMessage, ConversationWorkState, InboxConversation, InboxReply

canonical_incoming_observed = Signal()
canonical_actionable_observed = Signal()


def enabled():
    return getattr(settings, "INBOX_CONVERSATION_WORKFLOW_ENABLED", False) is True


@dataclass(frozen=True)
class LiveObservation:
    baseline_at: datetime
    observed_at: datetime


@dataclass(frozen=True)
class ObservationResult:
    read_observed: bool = False
    actionable_observed: bool = False

    def __bool__(self):
        return self.read_observed or self.actionable_observed


def _valid_time(value):
    return isinstance(value, datetime) and timezone.is_aware(value) and value <= timezone.now() + timedelta(seconds=5)


def _direct(conversation):
    return bool(
        conversation.conversation_type == "direct"
        and not conversation.peer_ambiguous
        and conversation.peer_id
        and conversation.platform_conversation_id
        and conversation.identity_kind == "platform"
    )


def _advance_revision(conversation):
    previous = conversation.revision
    conversation.revision += 1
    # A local semantic-state revision is not a missed provider observation.
    # Advance only an already-current work snapshot; never heal a history gap.
    ConversationWorkState.objects.filter(
        conversation=conversation, conversation_revision=previous, history_gap=False
    ).update(conversation_revision=conversation.revision)


def _locked(conversation):
    account = lock_dm_account(conversation.social_account_id, conversation.workspace_id)
    if account is None or account.platform != conversation.platform:
        raise DMSendGateError("identity_changed", "This conversation's account identity changed.")
    current = InboxConversation.objects.select_for_update().get(
        pk=conversation.pk, workspace_id=account.workspace_id, social_account=account, platform=account.platform
    )
    return account, current


@transaction.atomic
def observe_canonical_message(
    row, *, source, observation=None, is_new=False, first_actionable=False, initialize_outbound=False
):
    if not enabled() or not isinstance(observation, LiveObservation) or source not in {"poll", "webhook"}:
        return False
    if not row.conversation_id:
        return False
    account, conversation = _locked(row.conversation)
    row = (
        ConversationMessage.objects.select_for_update()
        .filter(
            pk=row.pk,
            conversation=conversation,
            social_account=account,
            workspace_id=account.workspace_id,
            platform=account.platform,
        )
        .first()
    )
    if account.connection_status != "connected":
        return False
    if row is not None and row.direction == "outbound":
        initial_outbound = bool(
            initialize_outbound is True
            and conversation.workflow_state is None
            and conversation.incoming_generation == 0
            and conversation.incoming_watermark_at is None
            and conversation.workflow_outbound_at is None
            and conversation.workflow_completed_at is None
            and not ConversationMessage.objects.filter(conversation=conversation).exclude(pk=row.pk).exists()
        )
        if (
            not is_new
            or row.is_deleted
            or row.delivery_status != "observed"
            or not row.platform_message_id
            or not _direct(conversation)
            or row.conversation_type != "direct"
            or row.sender_id not in {account.account_platform_id, account.webhook_target_id} - {"", None}
            or row.recipient_id != conversation.peer_id
            or (conversation.workflow_state is None and not initial_outbound)
            or not _valid_time(observation.baseline_at)
            or not _valid_time(observation.observed_at)
            or observation.baseline_at != conversation.workflow_baseline_at
            or observation.observed_at < observation.baseline_at
            or not _valid_time(row.occurred_at)
            or row.occurred_at <= observation.baseline_at
            or row.occurred_at > observation.observed_at + timedelta(seconds=5)
            or (conversation.incoming_watermark_at is None and not initial_outbound)
            or (conversation.incoming_watermark_at is not None and row.occurred_at < conversation.incoming_watermark_at)
            or (conversation.workflow_outbound_at and row.occurred_at <= conversation.workflow_outbound_at)
        ):
            return False
        conversation.workflow_outbound_at = row.occurred_at
        if row.occurred_at == conversation.incoming_watermark_at:
            # Equal provider timestamps cannot establish which party acted last.
            conversation.workflow_order_uncertain = True
        elif initial_outbound or conversation.workflow_state == "needs_action":
            conversation.workflow_state = "waiting"
        _advance_revision(conversation)
        conversation.save()
        return ObservationResult()
    if (
        row is None
        or row.direction != "inbound"
        or row.is_deleted
        or not row.platform_message_id
        or row.delivery_status != "observed"
        or not row.sender_id
        or row.sender_id in {account.account_platform_id, account.webhook_target_id}
        or row.recipient_id not in {account.account_platform_id, account.webhook_target_id} - {"", None}
        or not _valid_time(observation.baseline_at)
        or not _valid_time(observation.observed_at)
        or observation.observed_at < observation.baseline_at
        or conversation.workflow_baseline_at != observation.baseline_at
        or not _valid_time(row.occurred_at)
        or row.occurred_at <= observation.baseline_at
        or row.occurred_at > observation.observed_at + timedelta(seconds=5)
    ):
        return False
    novel = is_new and row.incoming_generation is None
    actionable = (
        _direct(conversation)
        and row.conversation_type == "direct"
        and row.sender_id == conversation.peer_id
        and (novel or (first_actionable and row.incoming_generation is not None))
    )
    if not novel and not actionable:
        return False
    if novel:
        conversation.incoming_generation += 1
        row.incoming_generation = conversation.incoming_generation
        row.save(update_fields=["incoming_generation", "updated_at"])
        conversation.incoming_observed_at = observation.observed_at
        if conversation.incoming_watermark_at is None or row.occurred_at > conversation.incoming_watermark_at:
            conversation.incoming_watermark_at = row.occurred_at
    if actionable and conversation.workflow_state is not None:
        # A newly seen live message was absent from a manual completion snapshot,
        # even when its provider timestamp predates that manual click.
        conversation.workflow_state = "needs_action"
        if conversation.workflow_outbound_at and row.occurred_at <= conversation.workflow_outbound_at:
            conversation.workflow_order_uncertain = True
    _advance_revision(conversation)
    conversation.save()
    if novel:
        canonical_incoming_observed.send(
            sender=observe_canonical_message,
            conversation=conversation,
            message=row,
            event_revision=conversation.incoming_generation,
            source=source,
        )
    if actionable and conversation.workflow_state is not None:
        canonical_actionable_observed.send(
            sender=observe_canonical_message,
            conversation=conversation,
            message=row,
            event_revision=row.incoming_generation,
            source=source,
        )
    return ObservationResult(novel, actionable and conversation.workflow_state is not None)


@transaction.atomic
def mark_conversation_done(
    *, conversation, expected_generation, authorization, expected_revision=None, confirm_order_uncertain=False
):
    if not enabled():
        raise DMSendGateError("workflow_disabled", "Conversation workflow is not enabled.")
    account, current = _locked(conversation)
    _authorize(authorization, account)
    if not _direct(current) or current.workflow_state is None:
        raise DMSendGateError("workflow_unavailable", "This conversation does not have a verified active workflow.")
    if (
        isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or current.incoming_generation != expected_generation
    ):
        raise DMSendGateError(
            "stale_generation", "New messages arrived. Review this conversation before marking it done."
        )
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision != current.revision
    ):
        raise DMSendGateError("stale_revision", "This conversation changed. Review its current state.")
    if current.workflow_order_uncertain:
        if confirm_order_uncertain is not True or expected_revision is None:
            raise DMSendGateError(
                "ordering_uncertain", "Review the uncertain message order before marking this conversation done."
            )
        current.workflow_reviewed_generation = current.incoming_generation
        current.workflow_order_uncertain = False
    current.workflow_state = "done"
    current.workflow_completed_at = timezone.now()
    current.workflow_completed_generation = current.incoming_generation
    _advance_revision(current)
    current.save()
    return current


@transaction.atomic
def record_composer_outcome(reply):
    if not enabled() or not reply.conversation_id:
        return
    account, conversation = _locked(reply.conversation)
    current = InboxReply.objects.select_for_update().get(pk=reply.pk)
    if current.conversation_id != conversation.pk:
        raise DMSendGateError("identity_changed", "The receipt conversation changed.")
    if current.status == "sent" and current.platform_reply_id and current.sent_at:
        if conversation.workflow_outbound_at and current.sent_at <= conversation.workflow_outbound_at:
            return
        if _direct(conversation) and conversation.workflow_state is not None:
            if current.conversation_incoming_generation == conversation.incoming_generation:
                conversation.workflow_state = "waiting"
            if not conversation.workflow_outbound_at or current.sent_at > conversation.workflow_outbound_at:
                conversation.workflow_outbound_at = current.sent_at
    elif current.status == "failed" and _direct(conversation) and conversation.workflow_state is not None:
        conversation.workflow_state = "needs_action"
    else:
        return
    _advance_revision(conversation)
    conversation.save()
