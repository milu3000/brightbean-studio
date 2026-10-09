"""Explicit V2 ownership and an observed-state, current-actor dispatch bridge.

This is not an unattended scheduler or a provider freshness guarantee. Meta
cannot atomically compare our local revision and send, and native/late activity
can remain unobserved. A caller must acknowledge those limits for each dispatch.
Persisted ownership continues to block legacy sends when rollout flags are off.
No enrollment, resume, transfer, reconciliation or retry runs automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.api_keys.models import ApiKey
from apps.members.models import WorkspaceMembership

from . import reply_coordination as coordinator
from .conversation_policy import read_allowed
from .dm_send_gate import DMSendGateError, _authorize, _check_identity, _outermost_required
from .locking import lock_dm_account
from .models import (
    ConversationMessage,
    DMConversationOwnership,
    DMSendControl,
    InboxMessage,
    InboxReply,
    SendOperation,
)


@dataclass(frozen=True)
class DispatchBinding:
    """Internal capability facts; never deserialize this from request JSON."""

    scope: coordinator.ReplyActorScope
    operation_id: object
    claim_token: object
    fencing_token: int
    owner_epoch: int
    authorization: object
    automated: bool = True


def dispatch_enabled():
    return coordinator.enabled() and getattr(settings, "INBOX_REPLY_DISPATCH_ENABLED", False) is True


def _enabled():
    if not dispatch_enabled():
        raise coordinator.ReplyCoordinationError("dispatch_disabled")


def _validate_owner_principal(account, principal):
    """Resolve an existing principal without minting or reading bearer material."""
    try:
        kind, identifier = principal.split(":", 1)
        if str(UUID(identifier)) != identifier or kind not in {"user", "oauth", "key"}:
            raise ValueError
    except (ValueError, AttributeError):
        raise coordinator.ReplyCoordinationError("invalid_owner") from None
    if kind == "key":
        key = ApiKey.objects.defer("token_hash").filter(pk=identifier, workspace_id=account.workspace_id).first()
        if (
            key is None
            or not key.is_active
            or not {"use_inbox", "reply_from_inbox"}.issubset(key.permissions or [])
            or not key.social_accounts.filter(pk=account.pk, workspace_id=account.workspace_id).exists()
        ):
            raise coordinator.ReplyCoordinationError("owner_authorization_unavailable")
        user_id = key.issued_by_id
    else:
        user_id = identifier
    member = (
        WorkspaceMembership.objects.select_related("custom_role", "workspace")
        .filter(
            user_id=user_id,
            user__is_active=True,
            workspace_id=account.workspace_id,
            workspace__is_archived=False,
        )
        .first()
    )
    if (
        member is None
        or (member.custom_role_id and member.custom_role.organization_id != member.workspace.organization_id)
        or not all(member.effective_permissions.get(p, False) for p in ("use_inbox", "reply_from_inbox"))
    ):
        raise coordinator.ReplyCoordinationError("owner_authorization_unavailable")


def _current_authorization(scope, account, authorization):
    if getattr(authorization, "actor_scope", None) != scope.actor_id:
        raise DMSendGateError("authorization_principal", "Current authorization does not match the owning principal.")
    _authorize(authorization, account)
    _validate_owner_principal(account, scope.actor_id)


def _ownership(account, conversation, *, required=False):
    owner = DMConversationOwnership.objects.select_for_update().filter(conversation=conversation).first()
    if owner is None:
        if required:
            raise coordinator.ReplyCoordinationError("ownership_required")
        return None
    if (
        owner.workspace_id != account.workspace_id
        or owner.social_account_id != account.pk
        or owner.platform != account.platform
        or owner.account_platform_id != account.account_platform_id
        or owner.platform_conversation_id != conversation.platform_conversation_id
        or owner.identity_kind != conversation.identity_kind
        or owner.peer_id != conversation.peer_id
        or not coordinator._verified(conversation)
    ):
        raise coordinator.ReplyCoordinationError("ownership_identity_changed")
    control = DMSendControl.objects.filter(pk=owner.control_id, social_account=account).first()
    if control is None:
        raise coordinator.ReplyCoordinationError("account_gate_required")
    _check_identity(control, account)
    return owner


def check_coordinator_owner(scope, account, conversation, *, operation=None, required=False, allow_paused=False):
    """Ownership checks also guard the older local coordinator entry points."""
    owner = _ownership(account, conversation, required=required)
    if owner is None:
        if operation is not None and operation.ownership_id:
            raise coordinator.ReplyCoordinationError("ownership_required")
        return None
    if owner.owner_scope != scope.actor_id:
        raise coordinator.ReplyCoordinationError("not_current_owner")
    if operation is not None and (operation.ownership_id != owner.pk or operation.owner_epoch != owner.epoch):
        raise coordinator.ReplyCoordinationError("stale_owner_epoch")
    if owner.paused and not allow_paused:
        raise coordinator.ReplyCoordinationError("owner_paused")
    return owner


def _retire_work(state, reason):
    # _invalidate never clears possible delivery. Fence even unknown operations.
    coordinator._invalidate(state, reason)
    state.generation += 1
    state.fencing_counter += 1
    state.owner_paused = True
    state.pause_reason = reason
    state.burst_started_at = None
    state.due_at = None
    state.save()


def enroll_conversation_owner(scope, *, conversation_id, social_account_id, platform, authorization):
    """Operator-only explicit enrollment of the current principal, initially paused.

    No authority/grant is created; the account gate must already exist. This
    function is intentionally absent from REST/MCP/UI enrollment surfaces.
    """
    _outermost_required()
    _enabled()
    with transaction.atomic(durable=True):
        account = coordinator._account(scope, social_account_id, platform)
        conversation = coordinator._conversation(account, conversation_id)
        _current_authorization(scope, account, authorization)
        if not read_allowed(account) or not coordinator._verified(conversation):
            raise coordinator.ReplyCoordinationError("not_found_or_denied")
        control = DMSendControl.objects.filter(social_account=account).first()
        if control is None:
            raise coordinator.ReplyCoordinationError("account_gate_required")
        _check_identity(control, account)
        owner = _ownership(account, conversation)
        if owner:
            if owner.owner_scope != scope.actor_id:
                raise coordinator.ReplyCoordinationError("not_current_owner")
            return owner
        owner = DMConversationOwnership.objects.create(
            control=control,
            conversation=conversation,
            **coordinator._scope(account),
            account_platform_id=account.account_platform_id,
            platform_conversation_id=conversation.platform_conversation_id,
            peer_id=conversation.peer_id,
            identity_kind=conversation.identity_kind,
            owner_scope=scope.actor_id,
        )
        state = coordinator._state(conversation, create=True)
        _retire_work(state, "ownership_enrolled")
        return owner


def _control_context(
    scope,
    conversation_id,
    social_account_id,
    platform,
    authorization,
    expected_epoch,
    expected_revision,
    expected_generation,
    *,
    hold_only=False,
):
    if hold_only:
        # Tightening an already-persisted hold does not depend on capture/read
        # rollout, connection health, or provider dispatch being enabled.
        if not scope.can_use_inbox or str(social_account_id) not in {str(pk) for pk in scope.allowed_account_ids}:
            raise coordinator.ReplyCoordinationError("not_found_or_denied")
        account = lock_dm_account(social_account_id, scope.workspace_id)
        if account is None or account.platform != platform or platform not in coordinator._PLATFORMS:
            raise coordinator.ReplyCoordinationError("not_found_or_denied")
    else:
        account = coordinator._account(scope, social_account_id, platform)
    conversation = coordinator._conversation(account, conversation_id)
    owner = check_coordinator_owner(scope, account, conversation, required=True, allow_paused=True)
    _current_authorization(scope, account, authorization)
    state = coordinator._state(conversation, create=True)
    if isinstance(expected_epoch, bool) or not isinstance(expected_epoch, int) or owner.epoch != expected_epoch:
        raise coordinator.ReplyCoordinationError("stale_owner_epoch")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or conversation.revision != expected_revision
    ):
        raise coordinator.ReplyCoordinationError("stale_revision")
    if (
        isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or state.generation != expected_generation
    ):
        raise coordinator.ReplyCoordinationError("stale_generation")
    return account, conversation, owner, state


def set_conversation_owner_paused(
    scope,
    *,
    conversation_id,
    social_account_id,
    platform,
    paused,
    expected_epoch,
    expected_revision,
    expected_generation,
    authorization,
):
    """Durable owner pause/resume; resume never revives a previous target."""
    _outermost_required()
    if not isinstance(paused, bool):
        raise coordinator.ReplyCoordinationError("invalid_pause")
    # A persisted owner can tighten its hold even with every rollout flag off.
    # Current principal authorization and the exact pinned identity still apply.
    with transaction.atomic(durable=True):
        account, conversation, owner, state = _control_context(
            scope,
            conversation_id,
            social_account_id,
            platform,
            authorization,
            expected_epoch,
            expected_revision,
            expected_generation,
            hold_only=paused,
        )
        if not paused:
            _enabled()
            if not read_allowed(account):
                raise coordinator.ReplyCoordinationError("not_found_or_denied")
            if state.identity_quarantined:
                raise coordinator.ReplyCoordinationError("identity_reconciliation_required")
            if state.history_gap or state.conversation_revision != conversation.revision:
                raise coordinator.ReplyCoordinationError("history_gap")
        if owner.paused == paused and state.owner_paused == paused:
            return owner
        _retire_work(state, "owner_paused" if paused else "owner_resumed")
        owner.paused = paused
        owner.epoch += 1
        if not paused:
            owner.resume_cutoff = timezone.now()
            state.owner_paused = False
            state.pause_reason = ""
            state.save(update_fields=["owner_paused", "pause_reason", "updated_at"])
        owner.save(update_fields=["paused", "epoch", "resume_cutoff", "updated_at"])
        return owner


def transfer_conversation_owner(
    scope,
    *,
    conversation_id,
    social_account_id,
    platform,
    new_owner_scope,
    expected_epoch,
    expected_revision,
    expected_generation,
    authorization,
):
    """Current-owner-only transfer; grants remain unchanged and delivery stays held.

    The operator supplies a verified existing principal. Public callers cannot
    choose a principal here. A non-owner takeover needs a separate authorized
    management policy and is deliberately not implemented by this bridge.
    """
    _outermost_required()
    if not isinstance(new_owner_scope, str) or not 1 <= len(new_owner_scope) <= 255:
        raise coordinator.ReplyCoordinationError("invalid_owner")
    with transaction.atomic(durable=True):
        account, _, owner, state = _control_context(
            scope,
            conversation_id,
            social_account_id,
            platform,
            authorization,
            expected_epoch,
            expected_revision,
            expected_generation,
            hold_only=True,
        )
        _validate_owner_principal(account, new_owner_scope)
        _retire_work(state, "owner_transferred")
        owner.owner_scope = new_owner_scope
        owner.paused = True
        owner.epoch += 1
        owner.save(update_fields=["owner_scope", "paused", "epoch", "updated_at"])
        return owner


@transaction.atomic
def prepare_owned_reply(scope, *, authorization, expected_owner_epoch, **kwargs):
    _enabled()
    account = coordinator._account(scope, kwargs["social_account_id"], kwargs["platform"])
    conversation = coordinator._conversation(account, kwargs["conversation_id"])
    owner = check_coordinator_owner(scope, account, conversation, required=True)
    if (
        isinstance(expected_owner_epoch, bool)
        or not isinstance(expected_owner_epoch, int)
        or owner.epoch != expected_owner_epoch
    ):
        raise coordinator.ReplyCoordinationError("stale_owner_epoch")
    _current_authorization(scope, account, authorization)
    if not read_allowed(account):
        raise coordinator.ReplyCoordinationError("not_found_or_denied")
    return coordinator.prepare_reply(scope, **kwargs)


@transaction.atomic
def claim_owned_reply(scope, *, operation_id, authorization, now=None):
    _enabled()
    account, conversation, _, operation = coordinator._load_operation(scope, operation_id)
    check_coordinator_owner(scope, account, conversation, operation=operation, required=True)
    _current_authorization(scope, account, authorization)
    if not read_allowed(account):
        raise coordinator.ReplyCoordinationError("not_found_or_denied")
    return coordinator.claim_reply(scope, operation_id=operation_id, now=now)


def lock_dispatch_context(account, binding):
    """Account -> conversation -> ownership -> work -> operation, before replies."""
    if binding is None:
        return None
    snapshot = SendOperation.objects.filter(
        pk=binding.operation_id,
        actor_scope=binding.scope.actor_id,
        workspace_id=account.workspace_id,
        social_account_id=account.pk,
        platform=account.platform,
    ).first()
    if snapshot is None:
        raise coordinator.ReplyCoordinationError("not_found_or_denied")
    conversation = coordinator._conversation(account, snapshot.conversation_id)
    owner = _ownership(account, conversation, required=True)
    state = coordinator._state(conversation)
    operation = SendOperation.objects.select_for_update().get(pk=snapshot.pk)
    return conversation, owner, state, operation


def _recipient(message):
    from providers.meta_messaging import resolve_recipient_id

    extra = dict(message.extra or {})
    if message.sender_handle:
        extra.setdefault("recipient_id", message.sender_handle)
    return resolve_recipient_id(extra)


def human_observed_action_allowed(binding, reply, *, cutoff, now):
    """Server session-only, new action after resume; never revive old queued work.

    Called only alongside the full owner/claim/current-state validation. The
    signed newest-page proof was checked before this immutable operation formed.
    """
    if binding is None or binding.automated or cutoff is None:
        return False
    user_id = getattr(binding.authorization, "human_session_user_id", None)
    if user_id is None or binding.scope.actor_id != f"user:{user_id}":
        return False
    operation = SendOperation.objects.filter(pk=binding.operation_id, reply=reply).select_related("ownership").first()
    if (
        operation is None
        or operation.conversation_action_nonce != reply.action_nonce
        or not reply.action_nonce
        or operation.owner_epoch != binding.owner_epoch
        or operation.ownership is None
        or operation.ownership.epoch != operation.owner_epoch
        or operation.actor_scope != binding.scope.actor_id
        or operation.human_observed_at is None
    ):
        return False
    coordinator._validate_payload(operation)
    return bool(
        cutoff < reply.created_at <= operation.human_observed_at <= operation.created_at <= now
        and timedelta(0) <= now - reply.inbox_message.received_at
    )


def check_conversation_send(account, message, reply, binding=None):
    """Persistent fail-closed ownership check shared by every existing send path.

    With any account ownership, unlinked/ambiguous DM targets are held: absence
    of a canonical link cannot prove that a legacy request is outside ownership.
    This check deliberately ignores rollout flags for legacy exclusion.
    """
    owners = DMConversationOwnership.objects.filter(social_account_id=account.pk)
    if binding is None:
        if not owners.exists():
            # A linked V2 reply can never escape via metadata/account changes.
            if SendOperation.objects.filter(reply=reply, ownership__isnull=False).exists():
                raise DMSendGateError("ownership_required", "This reply requires its bound V2 dispatch operation.")
            return
        row = ConversationMessage.objects.filter(
            legacy_message=message,
            **coordinator._scope(account),
            platform_message_id=message.platform_message_id,
            direction="inbound",
            is_deleted=False,
        ).first()
        if (
            row is None
            or row.conversation_id is None
            or row.conversation.conversation_type != "direct"
            or not coordinator._verified(row.conversation)
            or owners.filter(Q(conversation_id=row.conversation_id) | Q(peer_id=_recipient(message))).exists()
        ):
            raise DMSendGateError("conversation_owned", "This DM requires its current owner's V2 dispatch operation.")
        return
    _enabled()
    if not read_allowed(account):
        raise coordinator.ReplyCoordinationError("not_found_or_denied")
    conversation, owner, state, operation = lock_dispatch_context(account, binding)
    check_coordinator_owner(binding.scope, account, conversation, operation=operation, required=True)
    _current_authorization(binding.scope, account, binding.authorization)
    if (
        isinstance(binding.owner_epoch, bool)
        or not isinstance(binding.owner_epoch, int)
        or owner.epoch != binding.owner_epoch
    ):
        raise coordinator.ReplyCoordinationError("stale_owner_epoch")
    if (
        isinstance(binding.fencing_token, bool)
        or not isinstance(binding.fencing_token, int)
        or operation.claim_token is None
        or str(operation.claim_token) != str(binding.claim_token)
        or operation.fencing_token != binding.fencing_token
        or state.fencing_counter != binding.fencing_token
        or state.active_operation_id != operation.pk
        or operation.status not in {"claimed", "outcome_unknown"}
    ):
        raise coordinator.ReplyCoordinationError("invalid_claim")
    now = timezone.now()
    if operation.lease_expires_at is None or now >= operation.lease_expires_at:
        raise coordinator.ReplyCoordinationError("lease_expired")
    coordinator._validate_payload(operation)
    target = coordinator._validate(
        account,
        conversation,
        state,
        expected_revision=operation.expected_revision,
        expected_generation=operation.expected_generation,
        target_id=operation.target_id,
        due=True,
        now=now,
        composer_reply=coordinator._composer_intent(operation),
        human_observed_at=operation.human_observed_at,
    )
    if (
        operation.reply_id != reply.pk
        or operation.body != reply.body
        or target.legacy_message_id != message.pk
        or target.platform_message_id != message.platform_message_id
        or operation.target_platform_message_id != target.platform_message_id
        or _recipient(message) != owner.peer_id
        or message.message_type != "dm"
        or target.occurred_at != message.received_at
        or target.occurred_at > now
    ):
        raise coordinator.ReplyCoordinationError("target_changed")
    timestamps = (target.occurred_at, target.first_seen_at, operation.created_at, reply.created_at)
    if any(value is None or value > now for value in timestamps) or (
        (owner.resume_cutoff is None or any(value <= owner.resume_cutoff for value in timestamps))
        and not human_observed_action_allowed(binding, reply, cutoff=owner.resume_cutoff, now=now)
    ):
        raise coordinator.ReplyCoordinationError("old_target")
    if operation.conversation_action_nonce is None and (
        SendOperation.objects.filter(
            conversation=conversation, target_platform_message_id=target.platform_message_id, status="confirmed"
        )
        .exclude(pk=operation.pk)
        .exists()
    ):
        raise coordinator.ReplyCoordinationError("target_already_answered")
    return operation


def mark_dispatch_attempt(binding, attempt, reply):
    """Called in the same durable transaction as the gate's unknown marker."""
    if binding is None:
        return
    operation = SendOperation.objects.select_for_update().get(pk=binding.operation_id)
    if operation.status != "claimed" or operation.attempt_id or operation.external_attempted_at:
        raise coordinator.ReplyCoordinationError("outcome_unknown")
    if operation.reply_id != reply.pk:
        raise coordinator.ReplyCoordinationError("target_changed")
    operation.status = "outcome_unknown"
    operation.external_attempted_at = timezone.now()
    operation.attempt = attempt
    operation.outcome_code = "attempt_committed"
    operation.save(update_fields=["status", "external_attempted_at", "attempt", "outcome_code", "updated_at"])
    attempt.operation = operation
    attempt.save(update_fields=["operation"])


def settle_dispatch_attempt(binding, attempt, reply, *, sent):
    """Same transaction as actual gate/reply result; never called by a retry."""
    if binding is None:
        return
    operation = SendOperation.objects.select_for_update().get(pk=binding.operation_id)
    if operation.attempt_id != attempt.pk or operation.reply_id != reply.pk or operation.status != "outcome_unknown":
        raise coordinator.ReplyCoordinationError("attempt_changed")
    state = coordinator._state(operation.conversation)
    operation.status = "confirmed" if sent else "failed"
    operation.outcome_code = "provider_accepted" if sent else attempt.reason_code
    operation.save(update_fields=["status", "outcome_code", "updated_at"])
    if state.active_operation_id == operation.pk:
        state.active_operation = None
        fields = ["active_operation", "updated_at"]
        if (
            state.generation == operation.expected_generation
            and state.conversation_revision == operation.expected_revision
            and state.latest_incoming_id == operation.target_id
        ):
            state.burst_started_at = None
            state.due_at = None
            fields += ["burst_started_at", "due_at"]
        # A newer incoming can have invalidated the marker before HTTP. Its
        # pending work belongs to the newer generation and must not be erased.
        state.save(update_fields=fields)


def dispatch_reply(
    scope,
    *,
    operation_id,
    claim_token,
    fencing_token,
    expected_owner_epoch,
    authorization,
    actor=None,
    acknowledge_observed_state=False,
    automated=True,
):
    """Dispatch one current actor's claimed intent with explicit known limits.

    True acknowledges *incomplete* provider freshness, not a freshness assertion.
    All network traffic uses the existing production provider and durable gate.
    Calls after possible dispatch never retry, including after expiry/transfer.
    """
    _outermost_required()
    _enabled()
    if not automated and (
        getattr(authorization, "human_session_user_id", None) is None
        or getattr(authorization, "human_session_user_id", None) != getattr(actor, "pk", None)
    ):
        raise coordinator.ReplyCoordinationError("human_sender_required")
    if acknowledge_observed_state is not True:
        raise coordinator.ReplyCoordinationError("observed_state_acknowledgement_required")
    binding = DispatchBinding(
        scope, operation_id, claim_token, fencing_token, expected_owner_epoch, authorization, automated
    )
    with transaction.atomic(durable=True):
        account, conversation, state, operation = coordinator._load_operation(scope, operation_id)
        check_coordinator_owner(scope, account, conversation, operation=operation, required=True)
        _current_authorization(scope, account, authorization)
        if operation.status == "confirmed":
            return operation
        if operation.status == "outcome_unknown":
            raise coordinator.ReplyCoordinationError("outcome_unknown")
        coordinator._fence(state, operation, claim_token, fencing_token)
        if operation.external_attempted_at or operation.attempt_id:
            raise coordinator.ReplyCoordinationError("outcome_unknown")
        target = coordinator._target(account, conversation, operation.target_id)
        if not target.legacy_message_id:
            raise coordinator.ReplyCoordinationError("target_not_linked")
        message = InboxMessage.objects.select_for_update().get(pk=target.legacy_message_id)
        message.social_account = account
        if operation.reply_id:
            reply = InboxReply.objects.select_for_update().get(pk=operation.reply_id)
        else:
            reply = InboxReply.objects.create(inbox_message=message, body=operation.body, author=actor)
            operation.reply = reply
            operation.save(update_fields=["reply", "updated_at"])
        check_conversation_send(account, message, reply, binding)
    from .services import send_reply_now

    send_reply_now(reply, actor=actor, automated=automated, authorization=authorization, dispatch_binding=binding)
    return SendOperation.objects.get(pk=operation.pk)
