"""Canonical actions use the existing owner claim and durable DM dispatcher.

No enrollment, resume, transfer, timeout reclaim, or reconciliation occurs here.
"""

from django.db import transaction
from django.utils import timezone

from . import reply_coordination as coordinator
from . import reply_dispatch as dispatcher
from .dm_send_gate import DMSendGateError, _authorize, _check_identity
from .locking import lock_dm_account
from .models import (
    ConversationMessage,
    ConversationWorkState,
    DMConversationOwnership,
    DMSendAttempt,
    InboxConversation,
    InboxReply,
    SendOperation,
)


def owner_for(conversation):
    return DMConversationOwnership.objects.select_related("control").filter(conversation=conversation).first()


def scope_facts(conversation):
    owner = owner_for(conversation)
    if owner is None:
        return None
    state = ConversationWorkState.objects.filter(conversation=conversation).first()
    return [
        str(owner.pk),
        owner.epoch,
        owner.paused,
        owner.control.epoch,
        owner.control.paused,
        conversation.revision,
        state.generation if state else None,
        state.conversation_revision if state else None,
    ]


def _scope(account, authorization):
    actor = getattr(authorization, "actor_scope", None)
    if not actor:
        raise DMSendGateError("authorization_principal", "A current authenticated sender principal is required.")
    _authorize(authorization, account)
    return coordinator.ReplyActorScope(actor, account.workspace_id, frozenset({account.pk}), True)


def _observation(account, conversation, draft_authorization, token):
    from .canonical_reads import verify_composer_observation

    factory = getattr(draft_authorization, "canonical_scope", None)
    if factory is None:
        raise DMSendGateError(
            "observation_scope_required", "Read the newest conversation with the current actor before sending."
        )
    return verify_composer_observation(factory(account), conversation.pk, token)


def availability(account, conversation, authorization, observation_token=None, *, draft_authorization=None):
    owner = owner_for(conversation)
    if owner is None:
        return None
    data = {
        "owned": True,
        "owner_epoch": owner.epoch,
        "review_tool": "get_reply_coordination",
        "automatic_takeover_allowed": False,
    }
    try:
        _authorize(authorization, account)
        _check_identity(owner.control, account)
        if (
            owner.workspace_id != account.workspace_id
            or owner.social_account_id != account.pk
            or owner.platform != account.platform
            or owner.account_platform_id != account.account_platform_id
            or owner.platform_conversation_id != conversation.platform_conversation_id
            or owner.peer_id != conversation.peer_id
            or owner.identity_kind != conversation.identity_kind
        ):
            raise DMSendGateError("ownership_identity_changed", "The configured sender identity requires review.")
        if owner.owner_scope != getattr(authorization, "actor_scope", None):
            raise DMSendGateError(
                "not_current_owner",
                "Another configured sender owns this conversation. Review ownership before an operator changes it.",
            )
        if not dispatcher.dispatch_enabled():
            raise DMSendGateError("dispatch_disabled", "The existing owner dispatcher is not enabled.")
        if owner.paused or owner.control.paused:
            raise DMSendGateError("owner_paused", "The configured sender is paused. Review its existing controls.")
        state = ConversationWorkState.objects.filter(conversation=conversation).first()
        if state is None:
            raise DMSendGateError("no_work_snapshot", "The configured sender has no verified conversation snapshot.")
        for held, code in (
            (state.identity_quarantined, "identity_reconciliation_required"),
            (state.history_gap or state.conversation_revision != conversation.revision, "history_gap"),
            (state.ordering_uncertain, "ordering_uncertain"),
            (state.owner_paused, "owner_paused"),
        ):
            if held:
                raise DMSendGateError(code, "The configured sender's observed conversation requires review.")
        _observation(account, conversation, draft_authorization or authorization, observation_token)
    except ValueError as exc:
        return {**data, "allowed": False, "code": getattr(exc, "code", "owner_held"), "reason": str(exc)}
    return {**data, "allowed": True, "code": "ready", "reason": ""}


def send_owned(reply, *, authorization, draft_authorization, observation_token, scope_token, actor, automated):
    from .conversation_composer import _check_scope, validate_conversation_reply

    human = (
        not automated
        and getattr(authorization, "human_session_user_id", None) == getattr(actor, "pk", None)
        and getattr(actor, "pk", None) is not None
    )
    if not automated and not human:
        raise DMSendGateError("human_sender_required", "Only the current session actor may use the human reply window.")
    with transaction.atomic():
        account = lock_dm_account(reply.inbox_message.social_account_id, reply.inbox_message.workspace_id)
        if account is None:
            raise DMSendGateError("identity_changed", "The outgoing account changed.")
        conversation = InboxConversation.objects.select_for_update().get(pk=reply.conversation_id)
        current = InboxReply.objects.select_for_update().get(pk=reply.pk)
        _check_scope(scope_token, account, conversation)
        scope = _scope(account, authorization)
        observed = _observation(account, conversation, draft_authorization, observation_token)
        owner = dispatcher.check_coordinator_owner(scope, account, conversation, required=True)
        validate_conversation_reply(current.inbox_message, current)
        state = coordinator._state(conversation)
        operation = SendOperation.objects.filter(reply=current).first()
        if operation is None:
            if current.send_generation or current.dm_send_attempts.exists():
                raise DMSendGateError("attempted_action", "An attempted action cannot acquire a new owner operation.")
            current.conversation_incoming_generation = observed["incoming_generation"]
            current.save(update_fields=["conversation_incoming_generation", "updated_at"])
            target = ConversationMessage.objects.get(legacy_message_id=current.inbox_message_id)
            operation = dispatcher.prepare_owned_reply(
                scope,
                authorization=authorization,
                expected_owner_epoch=owner.epoch,
                conversation_id=conversation.pk,
                social_account_id=account.pk,
                platform=account.platform,
                expected_revision=conversation.revision,
                expected_generation=state.generation,
                target_message_id=target.pk,
                body=current.body,
                idempotency_key=f"composer:{current.action_nonce}",
                composer_reply=current,
                human_observed_at=timezone.now() if human else None,
            )
        if operation.conversation_action_nonce != current.action_nonce:
            raise DMSendGateError(
                "invalid_conversation_action", "The owner operation does not match this action nonce."
            )
        claimed = dispatcher.claim_owned_reply(scope, operation_id=operation.pk, authorization=authorization)
    dispatcher.dispatch_reply(
        scope,
        operation_id=claimed.pk,
        claim_token=claimed.claim_token,
        fencing_token=claimed.fencing_token,
        expected_owner_epoch=claimed.owner_epoch,
        authorization=authorization,
        actor=actor,
        acknowledge_observed_state=True,
        automated=automated,
    )
    reply.refresh_from_db()
    return reply


@transaction.atomic
def bind_observed_generation(reply, draft_authorization, token):
    """A rendered newest page may update only a still-local action watermark."""
    from .conversation_composer import _resolve, _validate_reply_intent

    account, conversation = _resolve(reply.conversation, lock=True)
    current = InboxReply.objects.select_for_update().get(pk=reply.pk)
    _authorize(draft_authorization, account)
    _validate_reply_intent(current, conversation, account)
    observed = _observation(account, conversation, draft_authorization, token)
    if (
        current.send_generation
        or current.dm_send_attempts.exists()
        or SendOperation.objects.filter(reply=current).exists()
    ):
        raise DMSendGateError("attempted_action", "The outgoing action's observed generation is already frozen.")
    current.conversation_incoming_generation = observed["incoming_generation"]
    current.save(update_fields=["conversation_incoming_generation", "updated_at"])
    reply.conversation_incoming_generation = current.conversation_incoming_generation


def can_retire_local_operation(reply, authorization):
    operation = SendOperation.objects.filter(reply=reply).first()
    if operation is None:
        return True
    principal = getattr(authorization, "actor_scope", None)
    current_owner = owner_for(reply.conversation)
    owner_matches = operation.actor_scope == principal or bool(
        operation.status == "superseded"
        and current_owner is not None
        and current_owner.owner_scope == principal
        and operation.ownership_id == current_owner.pk
        and operation.owner_epoch < current_owner.epoch
    )
    return bool(
        operation.conversation_action_nonce == reply.action_nonce
        and owner_matches
        and operation.status in {"prepared", "claimed", "superseded"}
        and not operation.external_attempted_at
        and not operation.attempt_id
        and not DMSendAttempt.objects.filter(operation=operation).exists()
    )


def retire_local_operation(reply, authorization):
    """Caller holds account/conversation/reply locks; never clear attempted work."""
    operation = SendOperation.objects.select_for_update().filter(reply=reply).first()
    if operation is None:
        return
    if not can_retire_local_operation(reply, authorization):
        raise DMSendGateError(
            "attempted_action", "This owner operation requires review and cannot be closed as a local draft."
        )
    state = coordinator._state(reply.conversation)
    if state.active_operation_id == operation.pk:
        state.active_operation = None
        state.fencing_counter += 1
        state.save(update_fields=["active_operation", "fencing_counter", "updated_at"])
    operation.status, operation.outcome_code = "superseded", "composer_retired"
    operation.save(update_fields=["status", "outcome_code", "updated_at"])
