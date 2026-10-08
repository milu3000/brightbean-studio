"""Reconstructed page-atomic canonical synchronization; disabled by default."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import salted_hmac

from providers.meta_inbox_paging import PAGE_LIMITS

from .conversation_policy import capture_allowed
from .locking import lock_dm_account
from .models import InboxSyncCheckpoint, InboxSyncConnection
from .sync_contracts import CONTEXTS, ConversationObservation, calendar_months, valid_id, validate_page
from .sync_identity import SyncError, bind_new_conversation, classify_participants, identity_matches

ROUTE_CONTRACT = "meta-dm-v25-v1"
LEASE_SECONDS = 90
MAX_SCAN_PAGES = 10000
MAX_RESTARTS = 3


@dataclass(frozen=True)
class PageLease:
    checkpoint_id: uuid.UUID
    connection_id: uuid.UUID
    connection_generation: uuid.UUID
    scan_generation: int
    fence: int
    token: uuid.UUID
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    stream: str
    scope_key: str
    context: str
    cursor: str
    participants: tuple
    coverage_from: datetime
    claimed_at: datetime
    route_contract: str
    auth_fingerprint: str
    content_fields_mode: str = "extended"
    attempts: int = 0


def auth_fingerprint(account):
    return salted_hmac("inbox.durable-sync.auth.v1", account.oauth_access_token, algorithm="sha256").hexdigest()


def capture_permitted(account, connection):
    return bool(
        getattr(settings, "INBOX_DURABLE_SYNC_ENABLED", False) is True
        and identity_matches(account, connection)
        and connection.enabled
        and account.connection_status in {"connected", "token_expiring"}
        and account.platform in {"facebook", "instagram_login"}
        and capture_allowed(account)
        and connection.auth_fingerprint == auth_fingerprint(account)
        and connection.route_contract == ROUTE_CONTRACT
    )


def lock_connection(connection_id):
    identity = InboxSyncConnection.objects.filter(pk=connection_id).values("social_account_id", "workspace_id").first()
    if identity is None:
        raise SyncError("connection_unavailable")
    account = lock_dm_account(identity["social_account_id"], identity["workspace_id"])
    connection = InboxSyncConnection.objects.select_for_update().get(pk=connection_id)
    if not capture_permitted(account, connection):
        raise SyncError("enrollment_or_identity_revoked")
    return account, connection


def claim_ownership(connection):
    if connection.ownership_claimed_at is None:
        connection.ownership_claimed_at = timezone.now()
        connection.save(update_fields=["ownership_claimed_at", "updated_at"])


@transaction.atomic
def start_scan(connection_id, *, context="live", stream="conversations", scope_key="account", now=None):
    now = now or timezone.now()
    _account, connection = lock_connection(connection_id)
    if context not in CONTEXTS or stream not in {"conversations", "messages"} or not valid_id(scope_key):
        raise SyncError("invalid_scope")
    if stream == "conversations" and scope_key != "account":
        raise SyncError("invalid_scope")
    claim_ownership(connection)
    checkpoint, created = InboxSyncCheckpoint.objects.get_or_create(
        connection=connection,
        context=context,
        stream=stream,
        scope_key=scope_key,
        defaults={
            "connection_generation": connection.generation,
            "scan_started_at": now,
            "coverage_from": calendar_months(now, -6),
        },
    )
    if checkpoint.connection_generation != connection.generation:
        raise SyncError("generation_changed")
    if not created and checkpoint.status == "complete":
        old_start = checkpoint.scan_started_at
        checkpoint.scan_generation += 1
        checkpoint.status, checkpoint.cursor = "ready", ""
        checkpoint.pages_committed = checkpoint.restarts = checkpoint.attempts = 0
        checkpoint.recent_cursor_digests = []
        checkpoint.scan_started_at = now
        checkpoint.coverage_from = calendar_months(now, -6)
        if context == "live":
            # Slow scans overlap their START, not only their last commit.
            checkpoint.coverage_from = max(checkpoint.coverage_from, old_start - timedelta(hours=6))
        checkpoint.save()
    return checkpoint


@transaction.atomic
def claim_page(checkpoint_id, *, now=None):
    now = now or timezone.now()
    identity = InboxSyncCheckpoint.objects.filter(pk=checkpoint_id).values("connection_id").first()
    if identity is None:
        raise SyncError("checkpoint_unavailable")
    account, connection = lock_connection(identity["connection_id"])
    checkpoint = InboxSyncCheckpoint.objects.select_for_update().get(pk=checkpoint_id)
    if checkpoint.connection_generation != connection.generation:
        raise SyncError("generation_changed")
    if (
        connection.blocked_reason
        or (connection.retry_at and connection.retry_at > now)
        or checkpoint.status in {"complete", "blocked"}
        or (checkpoint.retry_at and checkpoint.retry_at > now)
        or (checkpoint.lease_expires_at and checkpoint.lease_expires_at > now)
    ):
        return None
    if (
        InboxSyncCheckpoint.objects.filter(connection=connection, lease_expires_at__gt=now)
        .exclude(pk=checkpoint.pk)
        .exists()
    ):
        return None
    if checkpoint.pages_committed >= MAX_SCAN_PAGES:
        checkpoint.status, checkpoint.last_error_code = "blocked", "scan_budget_reached"
        checkpoint.save(update_fields=["status", "last_error_code", "updated_at"])
        return None
    if (
        checkpoint.context in {"bootstrap", "backfill"}
        and InboxSyncCheckpoint.objects.filter(
            connection=connection,
            context="live",
            connection_generation=connection.generation,
            status__in=["ready", "retry", "running"],
        )
        .filter(Q(retry_at__isnull=True) | Q(retry_at__lte=now))
        .exists()
    ):
        return None
    if (
        checkpoint.content_fields_mode == "basic"
        and checkpoint.content_probe_after
        and checkpoint.content_probe_after <= now
    ):
        checkpoint.content_fields_mode = "extended"
    claim_ownership(connection)
    connection.last_served_at = now
    connection.save(update_fields=["last_served_at", "updated_at"])
    checkpoint.fence += 1
    checkpoint.lease_token = uuid.uuid4()
    checkpoint.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
    checkpoint.status = "running"
    checkpoint.save(
        update_fields=["fence", "lease_token", "lease_expires_at", "status", "content_fields_mode", "updated_at"]
    )
    return PageLease(
        checkpoint.pk,
        connection.pk,
        connection.generation,
        checkpoint.scan_generation,
        checkpoint.fence,
        checkpoint.lease_token,
        account.workspace_id,
        account.pk,
        checkpoint.stream,
        checkpoint.scope_key,
        checkpoint.context,
        checkpoint.cursor,
        tuple(checkpoint.participant_ids),
        checkpoint.coverage_from,
        now,
        connection.route_contract,
        connection.auth_fingerprint,
        checkpoint.content_fields_mode,
        checkpoint.attempts,
    )


def _current_lease(lease, now):
    account, connection = lock_connection(lease.connection_id)
    checkpoint = InboxSyncCheckpoint.objects.select_for_update().get(pk=lease.checkpoint_id)
    expected = (
        lease.connection_generation,
        lease.scan_generation,
        lease.fence,
        lease.token,
        lease.cursor,
        lease.stream,
        lease.scope_key,
        lease.context,
        lease.participants,
        lease.coverage_from,
        lease.content_fields_mode,
        lease.attempts,
    )
    current = (
        checkpoint.connection_generation,
        checkpoint.scan_generation,
        checkpoint.fence,
        checkpoint.lease_token,
        checkpoint.cursor,
        checkpoint.stream,
        checkpoint.scope_key,
        checkpoint.context,
        tuple(checkpoint.participant_ids),
        checkpoint.coverage_from,
        checkpoint.content_fields_mode,
        checkpoint.attempts,
    )
    if (
        current != expected
        or connection.generation != lease.connection_generation
        or connection.auth_fingerprint != lease.auth_fingerprint
        or connection.route_contract != lease.route_contract
        or (account.pk, account.workspace_id) != (lease.account_id, lease.workspace_id)
        or checkpoint.status != "running"
        or checkpoint.lease_expires_at is None
        or checkpoint.lease_expires_at <= now
        or checkpoint.lease_expires_at != lease.claimed_at + timedelta(seconds=LEASE_SECONDS)
    ):
        raise SyncError("lease_lost")
    return account, connection, checkpoint


@transaction.atomic
def commit_page(lease, page, *, now=None):
    now = now or timezone.now()
    digest = validate_page(page, lease, now)
    account, connection, checkpoint = _current_lease(lease, now)
    next_digest = hashlib.sha256(page.next_cursor.encode()).hexdigest() if page.next_cursor else ""
    if page.next_cursor and (page.next_cursor == lease.cursor or next_digest in checkpoint.recent_cursor_digests):
        raise SyncError("cursor_repeated")
    observations = page.observations
    if lease.context == "live" and lease.stream == "messages":
        # Provider pages commonly arrive newest first. Their verified native
        # times order this single atomic snapshot; ties retain provider order
        # and undated rows remain without invented chronology.
        observations = sorted(observations, key=lambda item: (item.occurred_at is None, item.occurred_at or now))
    for item in observations:
        if isinstance(item, ConversationObservation):
            from .conversations import _conversation
            from .sync_identity import assert_conversation_provenance

            assert_conversation_provenance(account, connection, item.platform_conversation_id, item.participant_ids)
            kind, reason, peer = classify_participants(account, item.participant_ids)
            conversation = _conversation(account, item.platform_conversation_id, peer, kind=kind, reason=reason)
            bind_new_conversation(conversation, connection)
            if connection.bootstrap_baseline_at and conversation.workflow_baseline_at is None:
                conversation.workflow_baseline_at = connection.bootstrap_baseline_at
                conversation.save(update_fields=["workflow_baseline_at", "updated_at"])
            child, created = InboxSyncCheckpoint.objects.get_or_create(
                connection=connection,
                stream="messages",
                scope_key=item.platform_conversation_id,
                context=lease.context,
                defaults={
                    "connection_generation": connection.generation,
                    "participant_ids": list(item.participant_ids),
                    "scan_started_at": lease.claimed_at,
                    "coverage_from": lease.coverage_from,
                },
            )
            if not created:
                if child.connection_generation != connection.generation:
                    raise SyncError("generation_changed")
                child.participant_ids = list(item.participant_ids)
                if child.status == "complete":
                    child.scan_generation += 1
                    child.status, child.cursor = "ready", ""
                    child.pages_committed = child.restarts = 0
                    child.recent_cursor_digests = []
                    child.scan_started_at, child.coverage_from = lease.claimed_at, lease.coverage_from
                child.save()
        elif item.occurred_at is None or item.occurred_at >= lease.coverage_from:
            from .sync_observations import reduce_observation

            reduce_observation(account, connection, item, context=lease.context)
    checkpoint.cursor = page.next_cursor
    checkpoint.last_page_digest = digest
    checkpoint.recent_cursor_digests = (checkpoint.recent_cursor_digests + [next_digest])[-32:] if next_digest else []
    checkpoint.pages_committed += 1
    checkpoint.last_committed_at = now
    checkpoint.coverage = "provider_edge_ended" if page.exhausted else "partial"
    checkpoint.status = "complete" if page.exhausted else "ready"
    checkpoint.lease_token = checkpoint.lease_expires_at = checkpoint.retry_at = None
    checkpoint.last_error_code, checkpoint.attempts = "", 0
    checkpoint.save()
    return checkpoint


@transaction.atomic
def fail_page(lease, error, *, now=None):
    now = now or timezone.now()
    _account, connection, checkpoint = _current_lease(lease, now)
    stable_codes = {
        "cursor_invalid",
        "cursor_repeated",
        "permission_unavailable",
        "rate_limited",
        "provider_unavailable",
        "invalid_page",
        "invalid_page_identity",
        "invalid_observation",
        "duplicate_page_identity",
        "page_too_large",
        "pagination_unverified",
        "response_too_large",
        "invalid_response",
        "canonical_provenance_unverified",
        "content_fields_unavailable",
        "page_size_rejected",
    }
    code = error.code if isinstance(error, SyncError) and error.code in stable_codes else "provider_unavailable"
    checkpoint.last_error_code = code
    checkpoint.attempts = min(20, checkpoint.attempts + 1)
    checkpoint.lease_token = checkpoint.lease_expires_at = None
    if code == "content_fields_unavailable" and lease.stream == "messages" and lease.content_fields_mode == "extended":
        # The failed request already spent its reserved GET. Retry this exact
        # cursor on a later budgeted page; never add an unreserved fallback GET.
        checkpoint.content_fields_mode = "basic"
        checkpoint.content_probe_after = now + timedelta(hours=6)
        checkpoint.status = "retry"
    elif code == "page_size_rejected" and lease.attempts < len(PAGE_LIMITS) - 1:
        # A rejected page advances no data/cursor. The next reserved attempt
        # decreases its row limit; rejection at the minimum is terminal.
        checkpoint.status = "retry"
    elif code in {"cursor_invalid", "cursor_repeated"} and checkpoint.restarts < MAX_RESTARTS:
        checkpoint.restarts += 1
        checkpoint.scan_generation += 1
        checkpoint.cursor, checkpoint.recent_cursor_digests = "", []
        checkpoint.status = "retry"
    elif code in stable_codes - {"rate_limited", "provider_unavailable"}:
        checkpoint.status = "blocked"
    else:
        checkpoint.status = "retry"
    if checkpoint.status == "retry":
        delay = min(3600, 30 * 2 ** (checkpoint.attempts - 1)) + checkpoint.pk.int % 17
        retry_after = getattr(error, "retry_after", None)
        if isinstance(retry_after, int) and not isinstance(retry_after, bool) and retry_after > 0:
            delay = max(delay, min(retry_after, 7 * 86400))
        checkpoint.retry_at = now + timedelta(seconds=delay)
        if code == "rate_limited":
            connection.retry_at = checkpoint.retry_at
            connection.save(update_fields=["retry_at", "updated_at"])
    if code == "permission_unavailable":
        connection.blocked_reason = code
        connection.save(update_fields=["blocked_reason", "updated_at"])
    checkpoint.save()
    return checkpoint


def run_one_page(checkpoint_id, adapter):
    lease = claim_page(checkpoint_id)
    if lease is None:
        return None
    try:
        return commit_page(lease, adapter.fetch(lease))  # HTTP occurs outside all DB locks.
    except SyncError as error:
        if error.code in {"lease_lost", "enrollment_or_identity_revoked", "generation_changed"}:
            raise
        return fail_page(lease, error)
