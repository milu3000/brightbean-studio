"""Explicit, reviewable bootstrap boundary; no automatic enrollment or activation."""

import hashlib
import json

from django.db import transaction
from django.utils import timezone

from .durable_sync import lock_connection, start_scan
from .models import InboxConversation, InboxSyncConnection
from .sync_identity import SyncError, canonical_read_connection


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def preview_cutover(connection_id):
    connection = InboxSyncConnection.objects.select_related("social_account").get(pk=connection_id)
    if canonical_read_connection(connection.social_account).pk != connection.pk:
        raise SyncError("canonical_provenance_unverified")
    conversations = list(
        InboxConversation.objects.filter(
            sync_identity__connection=connection, sync_identity__connection_generation=connection.generation
        )
        .order_by("pk")
        .values("id", "revision", "workflow_state", "workflow_baseline_at", "conversation_type", "peer_ambiguous")
    )
    if len(conversations) > 10000:
        raise SyncError("cutover_review_too_large")
    checkpoints = list(
        connection.checkpoints.order_by("pk").values(
            "id",
            "connection_generation",
            "scan_generation",
            "status",
            "coverage",
            "pages_committed",
            "last_page_digest",
            "content_fields_mode",
            "content_probe_after",
            "lease_token",
            "lease_expires_at",
            "updated_at",
        )
    )
    receipts = list(
        connection.receipts.order_by("pk").values("id", "connection_generation", "status", "event_key", "updated_at")
    )
    partial = [
        str(row["id"])
        for row in checkpoints
        if row["status"] != "complete"
        or row["coverage"] != "provider_edge_ended"
        or row["content_fields_mode"] != "extended"
    ]
    pending = [str(row["id"]) for row in receipts if row["status"] != "processed"]
    legacy_counts = {}
    for conversation in conversations:
        counts = {}
        rows = (
            InboxConversation.objects.get(pk=conversation["id"])
            .messages.filter(legacy_message__isnull=False)
            .values_list("legacy_message__status", flat=True)
        )
        for status in rows:
            counts[status] = counts.get(status, 0) + 1
        legacy_counts[str(conversation["id"])] = counts
    scope = {
        "connection_id": str(connection.pk),
        "generation": str(connection.generation),
        "workspace_id": str(connection.workspace_id),
        "account_platform_id": connection.account_platform_id,
        "baseline": connection.bootstrap_baseline_at,
        "conversations": conversations,
        "checkpoints": checkpoints,
        "receipts": receipts,
        "legacy_status_counts": legacy_counts,
    }
    return {
        "fingerprint": _digest(scope),
        "scope": scope,
        "partial_checkpoint_ids": partial,
        "pending_receipt_ids": pending,
        "provider_exhaustion_is_history_complete": False,
        "suggested_mapping": {str(row["id"]): None for row in conversations},
    }


@transaction.atomic
def establish_cutover(connection_id, *, expected_fingerprint, workflow_mapping, accept_partial=False):
    """Reviewed caller supplies exact preview and explicit initial state per thread.

    None holds ambiguous/unmapped history. Existing manual status is never silently
    marked handled or transformed into an action flood. Before this instant all
    captured history remains quiet; a six-hour overlapping head scan discovers
    post-cutover arrivals and persisted message IDs deduplicate their promotion.
    """
    _account, connection = lock_connection(connection_id)
    if connection.bootstrap_baseline_at is not None:
        raise SyncError("already_cut_over")
    preview = preview_cutover(connection_id)
    if preview["fingerprint"] != expected_fingerprint:
        raise SyncError("cutover_snapshot_changed")
    now = timezone.now()
    if connection.checkpoints.filter(lease_expires_at__gt=now).exists():
        raise SyncError("cutover_page_inflight")
    if not connection.checkpoints.filter(stream="conversations", connection_generation=connection.generation).exists():
        raise SyncError("bootstrap_not_observed")
    if any(row["connection_generation"] != connection.generation for row in preview["scope"]["checkpoints"]):
        raise SyncError("generation_changed")
    if (preview["partial_checkpoint_ids"] or preview["pending_receipt_ids"]) and accept_partial is not True:
        raise SyncError("partial_coverage_requires_review")
    required = set(preview["suggested_mapping"])
    if (
        not isinstance(workflow_mapping, dict)
        or set(workflow_mapping) != required
        or any(
            value is not None and (not isinstance(value, str) or value not in {"needs_action", "waiting", "done"})
            for value in workflow_mapping.values()
        )
    ):
        raise SyncError("initial_mapping_required")
    for conversation in InboxConversation.objects.select_for_update().filter(pk__in=required):
        state = workflow_mapping[str(conversation.pk)]
        if state is not None and (conversation.conversation_type != "direct" or conversation.peer_ambiguous):
            raise SyncError("ambiguous_initial_mapping")
        conversation.workflow_state, conversation.workflow_baseline_at = state, now
        conversation.revision += 1
        conversation.save(update_fields=["workflow_state", "workflow_baseline_at", "revision", "updated_at"])
    connection.bootstrap_baseline_at = now
    connection.ownership_claimed_at = connection.ownership_claimed_at or now
    connection.save(update_fields=["bootstrap_baseline_at", "ownership_claimed_at", "updated_at"])
    start_scan(connection.pk, context="live", now=now)
    return now
