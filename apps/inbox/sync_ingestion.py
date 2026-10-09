"""Normalized webhook queue and exact-message native attribution.

The owning signed HTTP route alone may invoke the Instagram deletion bridge.
No sender pair constructs a thread. A signed unattributed delivery is saved in
the same canonical store, read-only until a native exact-ID observation attributes
it. Unverified internal queue input remains held without claiming source proof.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from django.utils import timezone

from .durable_sync import claim_ownership, lock_connection
from .models import ConversationMessage, InboxSyncConnection, InboxSyncReceipt
from .sender_display import normalize_sender_name
from .sync_contracts import MessageObservation, calendar_months, valid_id, validate_observation
from .sync_identity import SyncError, canonical_owns_account, identity_matches  # noqa: F401 stable writer guard

_TIMES = ("occurred_at", "observed_at", "provider_updated_at", "snapshot_started_at")


@dataclass(frozen=True)
class VerifiedMetaDelivery:
    """Internal route evidence. Never construct from request JSON or a message ID."""

    account_id: object
    workspace_id: object
    platform: str
    native_target: str
    webhook_target_id: str
    connection_id: object
    generation: object


def verified_meta_delivery(account, native_target):
    """Only the owning-app HMAC-verified route calls this factory."""
    connection = InboxSyncConnection.objects.filter(social_account_id=account.pk).first()
    if (
        not valid_id(native_target)
        or native_target != account.account_platform_id
        or connection is None
        or not identity_matches(account, connection)
    ):
        return None
    return VerifiedMetaDelivery(
        account.pk,
        account.workspace_id,
        account.platform,
        native_target,
        account.webhook_target_id,
        connection.pk,
        connection.generation,
    )


def _delivery_matches(proof, account, connection):
    return isinstance(proof, VerifiedMetaDelivery) and (
        proof.account_id,
        proof.workspace_id,
        proof.platform,
        proof.native_target,
        proof.webhook_target_id,
        proof.connection_id,
        proof.generation,
    ) == (
        account.pk,
        account.workspace_id,
        account.platform,
        account.account_platform_id,
        account.webhook_target_id,
        connection.pk,
        connection.generation,
    )


def _encode(item):
    payload = asdict(item)
    for name in _TIMES:
        payload[name] = payload[name].isoformat() if payload[name] is not None else None
    if len(json.dumps(payload).encode()) > 65536:
        raise SyncError("invalid_observation")
    return payload


def _decode(payload):
    data = dict(payload)
    for name in _TIMES:
        data[name] = datetime.fromisoformat(data[name]) if data.get(name) is not None else None
    data["participant_ids"], data["attachments"] = tuple(data["participant_ids"]), tuple(data["attachments"])
    return MessageObservation(**data)


def _locked_for_account(account):
    connection_id = (
        InboxSyncConnection.objects.filter(social_account_id=account.pk).values_list("pk", flat=True).first()
    )
    if connection_id is None:
        raise SyncError("connection_unavailable")
    fresh, connection = lock_connection(connection_id)
    if (fresh.workspace_id, fresh.platform, fresh.account_platform_id, fresh.webhook_target_id) != (
        account.workspace_id,
        account.platform,
        account.account_platform_id,
        account.webhook_target_id,
    ):
        raise SyncError("enrollment_or_identity_revoked")
    return fresh, connection


def _enqueue(account, connection, observation, *, kind):
    claim_ownership(connection)
    payload = _encode(observation)
    # Replays with the same original observation time are idempotent. Deletion
    # is intrinsically one monotonic fact per native message ID.
    key_material = [kind, observation.platform_message_id]
    if kind != "instagram_deleted":
        content = dict(payload)
        content.pop("observed_at")
        content.pop("snapshot_started_at")
        key_material.append(content)
    key = hashlib.sha256(json.dumps(key_material, sort_keys=True).encode()).hexdigest()
    receipt, _ = InboxSyncReceipt.objects.get_or_create(
        connection=connection,
        connection_generation=connection.generation,
        event_key=key,
        defaults={
            "platform_message_id": observation.platform_message_id,
            "kind": kind,
            "context": "live" if connection.bootstrap_baseline_at else "bootstrap",
            "payload": payload,
            "observed_at": observation.observed_at,
            "expires_at": calendar_months(observation.occurred_at or observation.observed_at, 6),
        },
    )
    return receipt


@transaction.atomic
def enqueue_message(account, observation, *, verified_delivery=None):
    account, connection = _locked_for_account(account)
    if observation.source != "webhook" or observation.withdrawn_verified or not valid_id(observation.sender_id):
        raise SyncError("invalid_observation")
    validate_observation(observation, now=timezone.now(), allow_pending_identity=True)
    if verified_delivery is not None and not _delivery_matches(verified_delivery, account, connection):
        raise SyncError("enrollment_or_identity_revoked")
    receipt = _enqueue(
        account, connection, observation, kind="signed_message" if verified_delivery is not None else "message"
    )
    _process_locked(account, connection, receipt)
    return receipt


@transaction.atomic
def enqueue_instagram_withdrawal(account, platform_message_id):
    """Called only after the IG Login route verifies its owning app's HMAC.

    Exact official contract: object=instagram, entry.messaging.message.mid and
    is_deleted=true. No FB capability, guessed sender, or original payload body.
    The tombstone commits before acknowledging delivery, even for an unseen mid.
    """
    account, connection = _locked_for_account(account)
    if account.platform != "instagram_login" or not valid_id(platform_message_id):
        raise SyncError("unsupported_withdrawal")
    item = MessageObservation(
        platform_message_id,
        "",
        "",
        "",
        (),
        "",
        None,
        timezone.now(),
        source="webhook",
        withdrawn_verified=True,
        content_available=False,
    )
    receipt = _enqueue(account, connection, item, kind="instagram_deleted")
    _process_locked(account, connection, receipt)
    return receipt


def _process_locked(account, connection, receipt):
    from .sync_observations import reduce_observation

    if receipt.status in {"processed", "unassigned"}:
        return True
    if receipt.connection_generation != connection.generation:
        receipt.status, receipt.last_error_code = "quarantined", "generation_changed"
        receipt.save(update_fields=["status", "last_error_code", "updated_at"])
        return False
    item = _decode(receipt.payload)
    if not item.conversation_id and not item.withdrawn_verified:
        row = (
            ConversationMessage.objects.filter(
                social_account=account,
                workspace_id=account.workspace_id,
                platform=account.platform,
                platform_message_id=item.platform_message_id,
                observation_state__connection_generation=connection.generation,
                conversation__sync_identity__connection=connection,
                conversation__sync_identity__connection_generation=connection.generation,
            )
            .select_related("conversation")
            .first()
        )
        if row is None or row.conversation_attribution != "platform":
            if receipt.kind == "signed_message":
                reduce_observation(account, connection, item, context=receipt.context, allow_unassigned=True)
                receipt.status, receipt.processed_at, receipt.payload = "unassigned", timezone.now(), {}
                receipt.last_error_code = "native_identity_pending"
                receipt.save(update_fields=["status", "processed_at", "payload", "last_error_code", "updated_at"])
                return True
            receipt.status, receipt.last_error_code = "awaiting_identity", "native_identity_pending"
            receipt.save(update_fields=["status", "last_error_code", "updated_at"])
            return False
        # Exact mid bridges native membership. Participants remain unknown until
        # a current native snapshot proves them; a webhook cannot inherit direct.
        item = replace(item, conversation_id=row.conversation.platform_conversation_id)
    reduce_observation(account, connection, item, context=receipt.context)
    receipt.status, receipt.processed_at, receipt.payload, receipt.last_error_code = "processed", timezone.now(), {}, ""
    receipt.save(update_fields=["status", "processed_at", "payload", "last_error_code", "updated_at"])
    return True


@transaction.atomic
def process_receipt(receipt_id):
    scope = InboxSyncReceipt.objects.filter(pk=receipt_id).values("connection_id").first()
    if scope is None:
        return False
    account, connection = lock_connection(scope["connection_id"])
    receipt = InboxSyncReceipt.objects.select_for_update().get(pk=receipt_id)
    return _process_locked(account, connection, receipt)


def drain_receipts(connection_id, *, limit=20):
    # Prefer new deliveries over unresolved old IDs. Updated timestamps rotate
    # unresolved work fairly; no lease needed because account lock serializes it.
    ids = list(
        InboxSyncReceipt.objects.filter(connection_id=connection_id, status__in=["pending", "awaiting_identity"])
        .annotate(
            queue_priority=Case(When(status="pending", then=Value(0)), default=Value(1), output_field=IntegerField())
        )
        .order_by("queue_priority", "updated_at")
        .values_list("pk", flat=True)[: min(limit, 100)]
    )
    processed = 0
    for receipt_id in ids:
        processed += bool(process_receipt(receipt_id))
    return processed


def ingest_meta_webhook(account, messaging, *, verified_instagram_delivery=False, verified_delivery=None):
    """Return True when durable ownership consumes/holds the legacy DM path."""
    if not canonical_owns_account(account):
        return False
    from providers.meta_inbox_content import classify_conversation_identity, normalize_attachments

    from .tasks import UNKNOWN_MESSAGE_TIMESTAMP
    from .webhooks import _meta_message_timestamp

    data = messaging.get("message")
    if not isinstance(data, dict):
        return True
    mid = data.get("mid")
    if not valid_id(mid):
        return True
    try:
        if data.get("is_deleted") is True:
            if verified_instagram_delivery and account.platform == "instagram_login":
                enqueue_instagram_withdrawal(account, mid)
            # Unsupported deletion fields are never ordinary content snapshots.
            return True
        sender, recipient = messaging.get("sender") or {}, messaging.get("recipient") or {}
        if not isinstance(sender, dict) or not isinstance(recipient, dict):
            return True
        sender_id, recipient_id = str(sender.get("id") or ""), str(recipient.get("id") or "")
        occurred = _meta_message_timestamp(messaging.get("timestamp"))
        if occurred == UNKNOWN_MESSAGE_TIMESTAMP:
            occurred = None
        conversation = messaging.get("conversation") or {}
        conversation_id = str(conversation.get("id") or "") if isinstance(conversation, dict) else ""
        if not conversation_id:
            conversation_id = str(messaging.get("conversation_id") or "")
        # Endpoint pairs cannot prove a native thread's participant set.
        kind, reason, _peer = classify_conversation_identity(
            messaging, own_ids={account.account_platform_id, account.webhook_target_id} - {""}, sender_id=sender_id
        )
        item = MessageObservation(
            mid,
            conversation_id,
            sender_id,
            recipient_id,
            (),
            data.get("text") or "",
            occurred,
            timezone.now(),
            attachments=tuple(normalize_attachments(messaging)),
            sender_name=normalize_sender_name(sender),
            source="webhook",
            outbound_verified=data.get("is_echo") is True,
            conversation_type=kind,
            classification_reason=reason,
        )
        enqueue_message(account, item, verified_delivery=verified_delivery)
    except SyncError as exc:
        if exc.code not in {"enrollment_or_identity_revoked", "connection_unavailable", "invalid_observation"}:
            raise
    return True
