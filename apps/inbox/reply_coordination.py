"""Default-off local DM burst coordination, with optional explicit ownership.

Callers supply a freshly authorized actor scope; this module rechecks persisted
workspace/account/platform ownership. PostgreSQL account/conversation locks
serialize ingestion and local reservations. SQLite tests do not establish this
cross-worker contract. Native activity can arrive late: freshness is incomplete
and no local transaction makes an external send atomic. No worker consumes due times. The separate reply_dispatch bridge can send only
explicitly enrolled, currently authorized, fenced operations.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .conversation_policy import capture_allowed
from .locking import lock_dm_account
from .models import ConversationMessage, ConversationSyncState, ConversationWorkState, InboxConversation, SendOperation

DEBOUNCE_SECONDS = 5
MAX_WAIT_SECONDS = 30
LEASE_SECONDS = 30
LIVE_DISPATCH_ENABLED = False
_ACTIVE = {"prepared", "claimed", "outcome_unknown"}
_PLATFORMS = {"facebook", "instagram_login"}


class ReplyCoordinationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ReplyActorScope:
    """Internal authorization snapshot, never constructed from request JSON.

    actor_id is a stable authenticated principal identifier (including its
    principal kind). The caller must refresh permission and account grants on
    every call. This value is not an authorization token or an HTTP/MCP API.
    """

    actor_id: str
    workspace_id: object
    allowed_account_ids: frozenset
    can_use_inbox: bool = False


def enabled():
    return bool(
        getattr(settings, "INBOX_CONVERSATION_V2_ENABLED", False)
        and getattr(settings, "INBOX_REPLY_COORDINATION_ENABLED", False)
    )


def _now(value):
    value = value or timezone.now()
    if timezone.is_naive(value):
        raise ReplyCoordinationError("invalid_time")
    return value


def _account(scope, account_id, platform):
    if not enabled():
        raise ReplyCoordinationError("disabled")
    if (
        not scope.can_use_inbox
        or not isinstance(scope.actor_id, str)
        or not 1 <= len(scope.actor_id) <= 255
        or str(account_id) not in {str(item) for item in scope.allowed_account_ids}
    ):
        raise ReplyCoordinationError("not_found_or_denied")
    account = lock_dm_account(account_id, scope.workspace_id)
    if (
        account is None
        or account.platform != platform
        or platform not in _PLATFORMS
        or account.connection_status not in {"connected", "token_expiring"}
        or not capture_allowed(account)
    ):
        raise ReplyCoordinationError("not_found_or_denied")
    return account


def _capture_account(account):
    """Refresh enrollment and identity before internal coordination mutations."""
    current = lock_dm_account(account.pk, account.workspace_id)
    if (
        current is None
        or current.platform != account.platform
        or current.platform not in _PLATFORMS
        or not capture_allowed(current)
    ):
        return None
    return current


def _scope(account):
    return {"workspace_id": account.workspace_id, "social_account_id": account.pk, "platform": account.platform}


def _conversation(account, conversation_id):
    conversation = InboxConversation.objects.select_for_update().filter(pk=conversation_id, **_scope(account)).first()
    if conversation is None:
        raise ReplyCoordinationError("not_found_or_denied")
    return conversation


def _verified(conversation):
    # Conservative one-to-one phase: a provider thread alone is not proof of a
    # recipient. Retained conflicting fallback identities require reconciliation.
    return bool(
        conversation.conversation_type == InboxConversation.ConversationType.DIRECT
        and conversation.peer_id
        and not conversation.peer_ambiguous
        and conversation.identity_kind in InboxConversation.IdentityKind.values
        and not InboxConversation.objects.filter(
            workspace_id=conversation.workspace_id,
            social_account_id=conversation.social_account_id,
            platform=conversation.platform,
            peer_id=conversation.peer_id,
        )
        .exclude(pk=conversation.pk)
        .exists()
    )


def _state(conversation, *, create=False):
    state = ConversationWorkState.objects.select_for_update().filter(conversation=conversation).first()
    if state is None and create:
        state = ConversationWorkState.objects.create(
            conversation=conversation, conversation_revision=conversation.revision
        )
    if state is None:
        raise ReplyCoordinationError("no_pending_work")
    return state


def _active(state):
    operations = SendOperation.objects.select_for_update().filter(conversation=state.conversation, status__in=_ACTIVE)
    operation = operations.first()
    if (operation.pk if operation else None) != state.active_operation_id:
        raise ReplyCoordinationError("invalid_active_operation")
    if operation and (
        operation.workspace_id != state.conversation.workspace_id
        or operation.social_account_id != state.conversation.social_account_id
        or operation.platform != state.conversation.platform
    ):
        raise ReplyCoordinationError("invalid_active_operation")
    return operation


def _invalidate(state, reason):
    operation = _active(state)
    if operation:
        if operation.status == "outcome_unknown" or operation.external_attempted_at:
            operation.status = "outcome_unknown"
            operation.outcome_code = "reconciliation_required"
        else:
            operation.status = "superseded"
            operation.outcome_code = reason
            state.active_operation = None
        operation.save(update_fields=["status", "outcome_code", "updated_at"])


@transaction.atomic
def invalidate_conversations(account, conversation_ids):
    """Internal identity-withdrawal hook; preserve unknown outcomes and pause."""
    account = _capture_account(account)
    if account is None:
        return
    for conversation in (
        InboxConversation.objects.select_for_update().filter(pk__in=conversation_ids, **_scope(account)).order_by("pk")
    ):
        state = ConversationWorkState.objects.select_for_update().filter(conversation=conversation).first()
        if state is None:
            continue
        _invalidate(state, "identity_uncertain")
        state.generation += 1
        if state.conversation_revision != conversation.revision:
            state.history_gap = True
        state.owner_paused = True
        state.pause_reason = "identity_uncertain"
        state.burst_started_at = None
        state.due_at = None
        state.save()


@transaction.atomic
def quarantine_transferred_uncertainty(account, source_conversation_ids, destination_conversation_id):
    """Preserve uncertainty when identity changes, without moving operations.

    A durable quarantine survives target deletion and ordinary owner resume.
    Propagate existing quarantine too, so a second identity move cannot erase
    the blocker. Clearing it needs a future explicit reconciliation workflow.
    """
    # Capture must remain enrolled, but this safety hook deliberately ignores
    # the coordination flag. It creates no due work and cannot clear old holds.
    if not source_conversation_ids or destination_conversation_id is None:
        return
    account = _capture_account(account)
    if account is None:
        return
    uncertain = (
        SendOperation.objects.filter(**_scope(account), conversation_id__in=source_conversation_ids)
        .filter(Q(status="outcome_unknown") | Q(external_attempted_at__isnull=False))
        .exists()
    )
    inherited = ConversationWorkState.objects.filter(
        conversation_id__in=source_conversation_ids,
        conversation__workspace_id=account.workspace_id,
        conversation__social_account_id=account.pk,
        conversation__platform=account.platform,
        identity_quarantined=True,
    ).exists()
    if not uncertain and not inherited:
        return
    conversation = _conversation(account, destination_conversation_id)
    state = _state(conversation, create=True)
    _invalidate(state, "identity_uncertain")
    state.identity_quarantined = True
    state.owner_paused = True
    state.pause_reason = "identity_uncertain"
    state.generation += 1
    state.conversation_revision = conversation.revision
    state.burst_started_at = None
    state.due_at = None
    state.save()


@transaction.atomic
def observe_message(
    message_id,
    *,
    source,
    is_new,
    previous_direction=None,
    previous_conversation_id=None,
    previous_attribution="",
    changed=False,
    now=None,
):
    """Internal ingestion hook; duplicates/backfill never schedule a new burst.

    Only first-observed live incoming rows schedule work. An edit or newly
    learned attribution may invalidate intent but never extends the debounce.
    Reliable provider times order targets inside an already-verified thread;
    they never establish identity. Unknown/tied order holds the burst.
    """
    if not enabled() or source not in {"webhook", "poll", "app_send"}:
        return None
    row = ConversationMessage.objects.get(pk=message_id)
    account = lock_dm_account(row.social_account_id, row.workspace_id)
    if (
        account is None
        or row.platform != account.platform
        or row.platform not in _PLATFORMS
        or not row.conversation_id
        or not capture_allowed(account)
    ):
        return None
    conversation = _conversation(account, row.conversation_id)
    state = ConversationWorkState.objects.select_for_update().filter(conversation=conversation).first()
    if state and (
        state.history_gap
        or (not state.identity_quarantined and state.conversation_revision != conversation.revision - int(changed))
    ):
        # Do not let a new observation conceal history missed while disabled.
        # Multi-revision identity changes also require explicit reconciliation.
        _invalidate(state, "history_gap")
        state.history_gap = True
        state.burst_started_at = None
        state.due_at = None
        state.save()
        return state
    valid_identity = _verified(conversation) and not (state and state.identity_quarantined)
    if not valid_identity and state is None:
        return None
    incoming = (
        is_new
        and source in {"webhook", "poll"}
        and row.direction == "inbound"
        and not row.is_deleted
        and valid_identity
    )
    outgoing = row.direction == "outbound" and (
        is_new
        or previous_direction != "outbound"
        or previous_conversation_id != row.conversation_id
        or previous_attribution not in InboxConversation.IdentityKind.values
    )
    if state is None and not incoming:
        return None
    state = state or _state(conversation, create=True)
    now = _now(now)
    current = ConversationMessage.objects.filter(
        pk=state.latest_incoming_id, conversation=conversation, **_scope(account)
    ).first()
    if (
        outgoing
        and current
        and current.occurred_at is not None
        and row.occurred_at is not None
        and row.occurred_at < current.occurred_at
    ):
        # A clearly older outgoing is context, not proof the current question
        # was answered. Unknown/equal/later timestamps still pause conservatively.
        outgoing = False
    own_confirmed_outgoing = bool(
        outgoing
        and source == "app_send"
        and row.legacy_reply_id
        and SendOperation.objects.filter(
            **_scope(account),
            conversation=conversation,
            reply_id=row.legacy_reply_id,
            ownership__isnull=False,
            status="confirmed",
            attempt__outcome="sent",
        ).exists()
    )
    if valid_identity and own_confirmed_outgoing:
        # The dispatch transaction already settled and consumed this operation.
        # Record its observation without treating our own accepted send as a new
        # native takeover. Never clear an existing hold or reinstate due work.
        _invalidate(state, "outgoing_observed")
        state.generation += 1
        state.burst_started_at = None
        state.due_at = None
    elif not valid_identity or outgoing:
        _invalidate(state, "identity_uncertain" if not valid_identity else "outgoing_observed")
        state.generation += 1
        state.owner_paused = True
        state.pause_reason = "identity_uncertain" if not valid_identity else "outgoing_observed"
        state.burst_started_at = None
        state.due_at = None
    elif incoming:
        _invalidate(state, "new_incoming")
        state.generation += 1
        advances_target = current is None or (
            row.occurred_at is not None and current.occurred_at is not None and row.occurred_at > current.occurred_at
        )
        if row.occurred_at is None or (
            current and (current.occurred_at is None or row.occurred_at == current.occurred_at)
        ):
            state.ordering_uncertain = True
        if state.latest_incoming_id and current is None:
            state.ordering_uncertain = True
        if advances_target:
            state.latest_incoming = row
            state.latest_incoming_at = now
        if state.ordering_uncertain:
            state.due_at = None
        elif not state.owner_paused and (state.burst_started_at is not None or advances_target):
            state.burst_started_at = state.burst_started_at or now
            state.due_at = min(
                now + timedelta(seconds=DEBOUNCE_SECONDS),
                state.burst_started_at + timedelta(seconds=MAX_WAIT_SECONDS),
            )
    elif changed:
        _invalidate(state, "conversation_changed")
    state.conversation_revision = conversation.revision
    state.save()
    return state


def _target(account, conversation, target_id):
    target = ConversationMessage.objects.filter(
        pk=target_id, conversation=conversation, direction="inbound", is_deleted=False, **_scope(account)
    ).first()
    if target is None or not target.platform_message_id:
        raise ReplyCoordinationError("invalid_target")
    own_ids = {account.account_platform_id, account.webhook_target_id} - {"", None}
    if target.sender_id != conversation.peer_id or target.recipient_id not in own_ids:
        raise ReplyCoordinationError("unverified_target_peer")
    return target


def _validate(account, conversation, state, *, expected_revision, expected_generation, target_id, due, now):
    if state.identity_quarantined:
        raise ReplyCoordinationError("identity_reconciliation_required")
    if state.history_gap or state.conversation_revision != conversation.revision:
        raise ReplyCoordinationError("history_gap")
    if state.ordering_uncertain:
        raise ReplyCoordinationError("ordering_uncertain")
    if not _verified(conversation):
        raise ReplyCoordinationError("unverified_conversation")
    if conversation.revision != expected_revision or state.generation != expected_generation:
        raise ReplyCoordinationError("stale_revision")
    if state.latest_incoming_id is None or str(state.latest_incoming_id) != str(target_id):
        raise ReplyCoordinationError("stale_target")
    if state.owner_paused:
        raise ReplyCoordinationError("owner_paused")
    if state.due_at is None:
        raise ReplyCoordinationError("no_pending_work")
    if due and now < state.due_at:
        raise ReplyCoordinationError("not_due")
    target = _target(account, conversation, target_id)
    if target.occurred_at is None:
        raise ReplyCoordinationError("ordering_uncertain")
    observations = ConversationMessage.objects.filter(conversation=conversation, **_scope(account))
    ordering_conflict = Q(occurred_at__isnull=True) | Q(occurred_at__gte=target.occurred_at)
    # A newly enabled coordinator may have no work snapshot for older phase-1
    # rows. Check the actual scoped ledger, not just the observations this work
    # state has processed. Time is ordering evidence, never identity evidence.
    if (
        observations.filter(direction="inbound", is_deleted=False)
        .exclude(pk=target.pk)
        .filter(ordering_conflict)
        .exists()
    ):
        raise ReplyCoordinationError("newer_or_uncertain_incoming")
    if observations.filter(direction="outbound").filter(ordering_conflict).exists():
        raise ReplyCoordinationError("newer_or_uncertain_outgoing")
    return target


def _payload(body, target_id, revision, generation):
    return hashlib.sha256(
        json.dumps(
            {"body": body, "target": str(target_id), "revision": revision, "generation": generation},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _validate_payload(operation):
    if operation.payload_fingerprint != _payload(
        operation.body, operation.target_id, operation.expected_revision, operation.expected_generation
    ):
        raise ReplyCoordinationError("invalid_payload_fingerprint")


@transaction.atomic
def prepare_reply(
    scope,
    *,
    conversation_id,
    social_account_id,
    platform,
    expected_revision,
    expected_generation,
    target_message_id,
    body,
    idempotency_key,
    now=None,
):
    """Persist a local intent; exact-key replays return its original outcome."""
    account = _account(scope, social_account_id, platform)
    conversation = _conversation(account, conversation_id)
    if (
        not isinstance(body, str)
        or not body.strip()
        or len(body) > 20000
        or not isinstance(idempotency_key, str)
        or not 1 <= len(idempotency_key) <= 128
        or isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
        or isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or expected_generation < 0
    ):
        raise ReplyCoordinationError("invalid_payload")
    from .reply_dispatch import check_coordinator_owner

    owner = check_coordinator_owner(scope, account, conversation)
    fingerprint = _payload(body, target_message_id, expected_revision, expected_generation)
    existing = SendOperation.objects.filter(
        **_scope(account), conversation=conversation, actor_scope=scope.actor_id, idempotency_key=idempotency_key
    ).first()
    if existing:
        _validate_payload(existing)
        if existing.payload_fingerprint != fingerprint:
            raise ReplyCoordinationError("idempotency_conflict")
        return existing
    state = _state(conversation)
    active = _active(state)
    if active:
        raise ReplyCoordinationError(
            "outcome_unknown" if active.status == "outcome_unknown" else "operation_in_progress"
        )
    target = _validate(
        account,
        conversation,
        state,
        expected_revision=expected_revision,
        expected_generation=expected_generation,
        target_id=target_message_id,
        due=False,
        now=_now(now),
    )
    if (
        SendOperation.objects.filter(conversation=conversation, status="confirmed")
        .filter(Q(target_id=target.pk) | Q(target_platform_message_id=target.platform_message_id))
        .exists()
    ):
        raise ReplyCoordinationError("target_already_answered")
    operation = SendOperation.objects.create(
        ownership=owner,
        owner_epoch=owner.epoch if owner else 0,
        target_platform_message_id=target.platform_message_id if owner else "",
        **_scope(account),
        conversation=conversation,
        actor_scope=scope.actor_id,
        idempotency_key=idempotency_key,
        payload_fingerprint=fingerprint,
        body=body,
        target=target,
        expected_revision=expected_revision,
        expected_generation=expected_generation,
    )
    state.active_operation = operation
    state.save(update_fields=["active_operation", "updated_at"])
    return operation


def _load_operation(scope, operation_id):
    # Initial scope lookup is read-only. Account -> conversation -> work -> op
    # is the common lock order, also used by canonical ingestion.
    snapshot = SendOperation.objects.filter(
        pk=operation_id, workspace_id=scope.workspace_id, actor_scope=scope.actor_id
    ).first()
    if snapshot is None:
        raise ReplyCoordinationError("not_found_or_denied")
    account = _account(scope, snapshot.social_account_id, snapshot.platform)
    conversation = _conversation(account, snapshot.conversation_id)
    from .reply_dispatch import check_coordinator_owner

    check_coordinator_owner(scope, account, conversation, operation=snapshot, allow_paused=True)
    state = _state(conversation)
    operation = SendOperation.objects.select_for_update().get(pk=snapshot.pk)
    if operation.actor_scope != scope.actor_id:
        raise ReplyCoordinationError("not_found_or_denied")
    if (operation.workspace_id, operation.social_account_id, operation.platform, operation.conversation_id) != (
        account.workspace_id,
        account.pk,
        account.platform,
        conversation.pk,
    ):
        raise ReplyCoordinationError("not_found_or_denied")
    return account, conversation, state, operation


@transaction.atomic
def claim_reply(scope, *, operation_id, now=None):
    """Acquire one fenced local dry-run reservation, never permission to send.

    Expiry does not imply failure and never automatically reclaims an operation.
    An expired local-only reservation can be superseded by explicit owner pause;
    anything possibly dispatched must remain unknown pending reconciliation.
    """
    account, conversation, state, operation = _load_operation(scope, operation_id)
    now = _now(now)
    _validate_payload(operation)
    if operation.status != "prepared" or operation.external_attempted_at:
        raise ReplyCoordinationError("outcome_unknown" if operation.status == "outcome_unknown" else "not_prepared")
    if _active(state) != operation:
        raise ReplyCoordinationError("invalid_active_operation")
    _validate(
        account,
        conversation,
        state,
        expected_revision=operation.expected_revision,
        expected_generation=operation.expected_generation,
        target_id=operation.target_id,
        due=True,
        now=now,
    )
    state.fencing_counter += 1
    operation.status = "claimed"
    operation.claim_token = uuid.uuid4()
    operation.fencing_token = state.fencing_counter
    operation.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
    operation.save(update_fields=["status", "claim_token", "fencing_token", "lease_expires_at", "updated_at"])
    state.save(update_fields=["fencing_counter", "updated_at"])
    return operation


def _fence(state, operation, claim_token, fencing_token):
    if (
        isinstance(fencing_token, bool)
        or not isinstance(fencing_token, int)
        or operation.status != "claimed"
        or operation.claim_token is None
        or str(operation.claim_token) != str(claim_token)
        or operation.fencing_token != fencing_token
        or state.fencing_counter != fencing_token
        or state.active_operation_id != operation.pk
    ):
        raise ReplyCoordinationError("invalid_claim")


@transaction.atomic
def check_before_send(scope, *, operation_id, claim_token, fencing_token, now=None):
    """Validate local state only. Every successful result still forbids dispatch."""
    account, conversation, state, operation = _load_operation(scope, operation_id)
    now = _now(now)
    _fence(state, operation, claim_token, fencing_token)
    _validate_payload(operation)
    if operation.external_attempted_at:
        raise ReplyCoordinationError("outcome_unknown")
    if operation.lease_expires_at is None or now >= operation.lease_expires_at:
        raise ReplyCoordinationError("lease_expired")
    if _active(state) != operation:
        raise ReplyCoordinationError("invalid_active_operation")
    _validate(
        account,
        conversation,
        state,
        expected_revision=operation.expected_revision,
        expected_generation=operation.expected_generation,
        target_id=operation.target_id,
        due=True,
        now=now,
    )
    sync = ConversationSyncState.objects.filter(**_scope(account), stream="dm").first()
    return {
        "local_state_valid": True,
        "send_allowed": False,
        "live_dispatch_enabled": False,
        "freshness_complete": False,
        "external_atomicity": False,
        "dm_sync_status": sync.status if sync else "unknown",
        "dm_last_success_at": sync.last_success_at.isoformat() if sync and sync.last_success_at else None,
        "note": "Local dry-run reservation only; account sync does not establish thread freshness.",
    }


@transaction.atomic
def set_owner_paused(
    scope, *, conversation_id, social_account_id, platform, paused, expected_revision, expected_generation, now=None
):
    """Explicit owner pause/resume clears pending work; resume never revives it."""
    account = _account(scope, social_account_id, platform)
    conversation = _conversation(account, conversation_id)
    from .models import DMConversationOwnership

    if DMConversationOwnership.objects.filter(conversation=conversation).exists():
        raise ReplyCoordinationError("ownership_control_required")
    if (
        not isinstance(paused, bool)
        or isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or conversation.revision != expected_revision
    ):
        raise ReplyCoordinationError("stale_revision")
    _now(now)
    state = _state(conversation, create=True)
    if (
        isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or state.generation != expected_generation
    ):
        raise ReplyCoordinationError("stale_generation")
    if not paused and (state.history_gap or state.conversation_revision != conversation.revision):
        raise ReplyCoordinationError("history_gap")
    if not paused and state.identity_quarantined:
        raise ReplyCoordinationError("identity_reconciliation_required")
    _invalidate(state, "owner_paused" if paused else "owner_resumed")
    state.generation += 1
    if state.conversation_revision != conversation.revision:
        state.history_gap = True
    state.owner_paused = paused
    state.pause_reason = "owner_requested" if paused else ""
    state.burst_started_at = None
    state.due_at = None
    state.save()
    return state


@transaction.atomic
def mark_outcome_unknown(scope, *, operation_id, claim_token, fencing_token, now=None):
    """Record explicit uncertainty evidence. No retry or reconciliation adapter.

    This never calls a provider. A stale lease may still have caused an external
    effect in a future adapter, so expiry does not prevent recording uncertainty.
    """
    _, _, state, operation = _load_operation(scope, operation_id)
    if operation.status == "outcome_unknown":
        if str(operation.claim_token) != str(claim_token) or operation.fencing_token != fencing_token:
            raise ReplyCoordinationError("invalid_claim")
        return operation
    _fence(state, operation, claim_token, fencing_token)
    operation.status = "outcome_unknown"
    operation.external_attempted_at = operation.external_attempted_at or _now(now)
    operation.outcome_code = "reconciliation_required"
    operation.save(update_fields=["status", "external_attempted_at", "outcome_code", "updated_at"])
    return operation
