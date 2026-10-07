"""Bounded preview/apply for provable pre-existing canonical source identities.

No body rewrite, new live event, enrollment, purge, or automatic acceptance of
unknown native ownership. Application requires a reviewed digest and is separate
from subsequent bootstrap cutover.
"""

import hashlib
import json

from django.db import transaction
from django.utils import timezone

from .locking import lock_dm_account
from .models import ConversationMessage, ConversationObservationState, ConversationSyncIdentity, InboxSyncConnection
from .sync_contracts import calendar_months, content_fingerprint
from .sync_identity import SyncError, identity_matches


def _proof(row, connection):
    own = {connection.account_platform_id, connection.webhook_target_id} - {""}
    if row.workspace_id != connection.workspace_id or row.platform != connection.platform:
        return "scope_mismatch"
    if (
        not row.conversation_id
        or not row.conversation.platform_conversation_id
        or row.conversation_attribution != "platform"
    ):
        return "native_conversation_unproven"
    if (
        row.direction == "inbound"
        and row.recipient_id in own
        and row.sender_id
        and row.sender_id not in own
        or row.direction == "outbound"
        and row.sender_id in own
    ):
        pass
    else:
        return "native_owner_unproven"
    if row.legacy_message_id:
        legacy = row.legacy_message
        if (legacy.workspace_id, legacy.social_account_id, legacy.platform_message_id, legacy.message_type) != (
            row.workspace_id,
            row.social_account_id,
            row.platform_message_id,
            "dm",
        ):
            return "legacy_scope_mismatch"
    return "provable"


def preview_provenance(connection_id, *, after="", limit=100):
    connection = InboxSyncConnection.objects.select_related("social_account").get(pk=connection_id)
    if not identity_matches(connection.social_account, connection):
        raise SyncError("canonical_provenance_unverified")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise SyncError("invalid_preview_limit")
    query = (
        ConversationMessage.objects.filter(
            social_account_id=connection.social_account_id, observation_state__isnull=True
        )
        .select_related("conversation", "legacy_message")
        .order_by("pk")
    )
    if after:
        query = query.filter(pk__gt=after)
    rows = list(query[:limit])
    records = [
        {
            "message_id": str(row.pk),
            "conversation_id": str(row.conversation_id or ""),
            "result": _proof(row, connection),
            "updated_at": row.updated_at.isoformat(),
            "content_digest": content_fingerprint(row.body, row.attachments),
        }
        for row in rows
    ]
    document = {
        "connection": str(connection.pk),
        "generation": str(connection.generation),
        "after": after,
        "limit": limit,
        "rows": records,
    }
    return {
        **document,
        "fingerprint": hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest(),
        "next_after": str(rows[-1].pk) if rows else "",
        "applied": False,
    }


@transaction.atomic
def apply_provenance(connection_id, *, expected_fingerprint, after="", limit=100):
    identity = InboxSyncConnection.objects.get(pk=connection_id)
    account = lock_dm_account(identity.social_account_id, identity.workspace_id)
    connection = InboxSyncConnection.objects.select_for_update().get(pk=connection_id)
    if not identity_matches(account, connection):
        raise SyncError("canonical_provenance_unverified")
    preview = preview_provenance(connection_id, after=after, limit=limit)
    if preview["fingerprint"] != expected_fingerprint:
        raise SyncError("provenance_snapshot_changed")
    bound = 0
    for item in preview["rows"]:
        if item["result"] != "provable":
            continue
        row = ConversationMessage.objects.select_for_update().get(pk=item["message_id"])
        proof, _ = ConversationSyncIdentity.objects.get_or_create(
            conversation_id=row.conversation_id,
            defaults={"connection": connection, "connection_generation": connection.generation},
        )
        if proof.connection_id != connection.pk or proof.connection_generation != connection.generation:
            raise SyncError("canonical_provenance_unverified")
        ConversationObservationState.objects.create(
            message=row,
            connection_generation=connection.generation,
            content_fingerprint=content_fingerprint(row.body, row.attachments),
            last_observed_at=row.updated_at,
            expires_at=calendar_months(row.occurred_at or row.first_seen_at, 6),
            withdrawn_at=row.updated_at if row.is_deleted else None,
        )
        bound += 1
    if bound and connection.ownership_claimed_at is None:
        connection.ownership_claimed_at = timezone.now()
        connection.save(update_fields=["ownership_claimed_at", "updated_at"])
    return {**preview, "applied": True, "bound": bound}
