"""Advance an existing owner transport snapshot from a proven canonical change.

The canonical ledger is authoritative. No ownership, permission, historical live
work or missing history is manufactured here. Caller holds the account lock.
"""

from django.db import transaction
from django.db.models import F

from .models import ConversationWorkState, DMConversationOwnership, InboxConversation, SendOperation


def capture_owned_transition(account, row, native_conversation_id):
    from . import reply_coordination as coordinator

    if not coordinator.enabled() or not DMConversationOwnership.objects.filter(social_account_id=account.pk).exists():
        return None
    ids = {row.conversation_id} if row and row.conversation_id else set()
    if native_conversation_id:
        ids.update(
            InboxConversation.objects.filter(
                workspace_id=account.workspace_id,
                social_account_id=account.pk,
                platform=account.platform,
                platform_conversation_id=native_conversation_id,
            ).values_list("pk", flat=True)
        )
    snapshots = {}
    for conversation in InboxConversation.objects.select_for_update().filter(
        pk__in=ids, workspace_id=account.workspace_id, social_account_id=account.pk, platform=account.platform
    ):
        state = ConversationWorkState.objects.select_for_update().filter(conversation=conversation).first()
        owner = DMConversationOwnership.objects.filter(conversation=conversation).first()
        if state is not None and owner is not None:
            snapshots[conversation.pk] = {
                "revision": conversation.revision,
                "state_revision": state.conversation_revision,
                "history_gap": state.history_gap,
                "owner_id": owner.pk,
                "owner_epoch": owner.epoch,
            }
    return {
        "conversations": snapshots,
        "exists": row is not None,
        "direction": row.direction if row else None,
        "conversation_id": row.conversation_id if row else None,
        "attribution": row.conversation_attribution if row else "",
    }


def observe_owned_transition(account, row, before, *, source, actionable_transition, live):
    from . import reply_coordination as coordinator

    try:
        with transaction.atomic():
            _observe_owned_transition(
                account, row, before, source=source, actionable_transition=actionable_transition, live=live
            )
    except coordinator.ReplyCoordinationError:
        # Broken coordinator state must hold sends, not hide newly captured
        # canonical content. Preserve all active/UNKNOWN operation evidence.
        ConversationWorkState.objects.filter(conversation_id=row.conversation_id).update(
            history_gap=True,
            owner_paused=True,
            pause_reason="owner_snapshot_conflict",
            due_at=None,
            burst_started_at=None,
        )


def _observe_owned_transition(account, row, before, *, source, actionable_transition, live):
    from . import reply_coordination as coordinator

    if before is None or row.conversation_id is None or not coordinator.enabled():
        return
    snapshot = before["conversations"].get(row.conversation_id)
    if snapshot is None:
        return
    conversation = InboxConversation.objects.select_for_update().get(pk=row.conversation_id)
    state = ConversationWorkState.objects.select_for_update().get(conversation=conversation)
    owner = DMConversationOwnership.objects.filter(conversation=conversation).first()
    if owner is None or (owner.pk, owner.epoch) != (snapshot["owner_id"], snapshot["owner_epoch"]):
        return
    if (
        owner.workspace_id,
        owner.social_account_id,
        owner.platform,
        owner.account_platform_id,
        owner.platform_conversation_id,
        owner.peer_id,
    ) != (
        account.workspace_id,
        account.pk,
        account.platform,
        account.account_platform_id,
        conversation.platform_conversation_id,
        conversation.peer_id,
    ):
        coordinator.invalidate_conversations(account, [conversation.pk])
        return
    if (
        snapshot["history_gap"]
        or snapshot["state_revision"] != snapshot["revision"]
        or state.history_gap
        or state.conversation_revision not in {snapshot["revision"], conversation.revision}
    ):
        # A real pre-existing gap is never healed by a new observation.
        coordinator._invalidate(state, "history_gap")
        state.history_gap, state.due_at, state.burst_started_at = True, None, None
        state.save(update_fields=["history_gap", "due_at", "burst_started_at", "active_operation", "updated_at"])
        return
    changed = conversation.revision != snapshot["revision"]
    # Upsert/classification and the canonical workflow may each advance the
    # same transaction's revision. Pinning the pre-change snapshot above lets
    # the existing reducer inspect this exact change without interpreting its
    # known intermediate revisions as lost provider history.
    state.conversation_revision = conversation.revision - int(changed)
    state.save(update_fields=["conversation_revision", "updated_at"])
    incoming = bool(actionable_transition and row.direction == "inbound" and state.latest_incoming_id != row.pk)
    is_new = incoming if row.direction == "inbound" else not before["exists"]
    previous_direction, previous_conversation, previous_attribution = (
        before["direction"],
        before["conversation_id"],
        before["attribution"],
    )
    confirmed_echo = bool(
        row.direction == "outbound"
        and before["direction"] == "outbound"
        and before["conversation_id"] == row.conversation_id
        and row.legacy_reply_id
        and SendOperation.objects.filter(
            conversation_id=row.conversation_id,
            ownership=owner,
            workspace_id=account.workspace_id,
            social_account_id=account.pk,
            platform=account.platform,
            reply_id=row.legacy_reply_id,
            status="confirmed",
            attempt__outcome="sent",
            attempt__completed_at__isnull=False,
            attempt__control_id=owner.control_id,
            attempt__reply_id=row.legacy_reply_id,
            attempt__operation_id=F("pk"),
            reply__status="sent",
            reply__sent_at__isnull=False,
            reply__send_generation__gt=0,
            reply__conversation_id=row.conversation_id,
            reply__account_platform_id=account.account_platform_id,
            reply__recipient_id=conversation.peer_id,
            reply__platform_conversation_id=conversation.platform_conversation_id,
            reply__connection_generation=row.observation_state.connection_generation,
            reply__platform_reply_id=row.platform_message_id,
            conversation_action_nonce=F("reply__action_nonce"),
        ).exists()
    )
    if confirmed_echo or (row.direction == "outbound" and not live and state.latest_incoming_id is None):
        # A known own receipt echo is not another native responder. Initial
        # outgoing-only history with no live target also creates no new pause.
        is_new = False
        previous_direction, previous_conversation, previous_attribution = "outbound", row.conversation_id, "platform"
    coordinator.observe_message(
        row.pk,
        source=source,
        is_new=is_new,
        previous_direction=previous_direction,
        previous_conversation_id=previous_conversation,
        previous_attribution=previous_attribution,
        changed=changed,
    )
