"""Trusted existing refresh protocol rotates request fences, never history identity."""

from django.db import transaction
from django.utils import timezone

from .durable_sync import auth_fingerprint
from .locking import lock_dm_account
from .models import InboxSyncConnection
from .sync_identity import identity_matches


@transaction.atomic
def trusted_refresh_completed(account, *, previous_fingerprint):
    """Only SocialAccount.refresh_oauth_token calls this after winning its CAS.

    No added grant or provider request. An arbitrary credential replacement has
    no trusted-refresh proof and must not call this integration hook.
    """
    current = lock_dm_account(account.pk, account.workspace_id)
    connection = InboxSyncConnection.objects.select_for_update().filter(social_account_id=account.pk).first()
    if (
        connection is None
        or not identity_matches(current, connection)
        or connection.auth_fingerprint != previous_fingerprint
    ):
        return False
    replacement = auth_fingerprint(current)
    if replacement == connection.auth_fingerprint:
        return True
    connection.auth_fingerprint = replacement
    if connection.blocked_reason == "permission_unavailable":
        connection.blocked_reason = ""
    connection.retry_at = None
    connection.save(update_fields=["auth_fingerprint", "blocked_reason", "retry_at", "updated_at"])
    for checkpoint in connection.checkpoints.select_for_update():
        checkpoint.fence += 1
        checkpoint.scan_generation += 1
        checkpoint.cursor = ""
        checkpoint.lease_token = checkpoint.lease_expires_at = checkpoint.retry_at = None
        checkpoint.recent_cursor_digests = []
        checkpoint.pages_committed = checkpoint.attempts = checkpoint.restarts = 0
        checkpoint.status = "ready"
        checkpoint.last_error_code = ""
        checkpoint.coverage = "unknown"
        # Keep the original coverage boundary. Rotation cannot skip the tail of
        # an interrupted bootstrap/repair or extend message retention.
        checkpoint.scan_started_at = timezone.now()
        checkpoint.save()
    return True
