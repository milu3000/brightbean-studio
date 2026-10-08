"""Stop an existing account while preserving canonical history and send evidence.

Internal producer called only by the already-authorized disconnect route. It
does not perform a provider request, create a grant, or erase message content.
"""

from uuid import uuid4

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .dm_send_gate import DMSendGateError
from .locking import lock_dm_account
from .models import (
    ConversationMessage,
    ConversationWorkState,
    DMConversationOwnership,
    DMSendControl,
    InboxArchiveIdentity,
    InboxConversation,
    InboxReply,
    InboxSyncConnection,
)


def has_retained_inbox(account):
    return bool(
        InboxConversation.objects.filter(social_account_id=account.pk).exists()
        or ConversationMessage.objects.filter(social_account_id=account.pk).exists()
        or DMSendControl.objects.filter(social_account_id=account.pk).exists()
        or InboxSyncConnection.objects.filter(social_account_id=account.pk).exists()
        or InboxReply.objects.filter(inbox_message__social_account_id=account.pk).exists()
    )


@transaction.atomic
def preserve_inbox_disconnect(account):
    current = lock_dm_account(account.pk, account.workspace_id)
    if current is None or (
        current.platform,
        current.account_platform_id,
        current.webhook_target_id,
        current.analytics_auth_updated_at,
    ) != (account.platform, account.account_platform_id, account.webhook_target_id, account.analytics_auth_updated_at):
        raise DMSendGateError("identity_changed", "The account connection changed. Reload before disconnecting.")
    if current.connection_status == "disconnected":
        return current
    connection = InboxSyncConnection.objects.select_for_update().filter(social_account=current).first()
    if connection is not None:
        from .sync_identity import identity_matches

        if not identity_matches(current, connection):
            raise DMSendGateError("identity_changed", "The retained inbox identity does not match this account.")
    InboxArchiveIdentity.objects.create(
        social_account=current,
        workspace_id=current.workspace_id,
        platform=current.platform,
        account_platform_id=current.account_platform_id,
        webhook_target_id=current.webhook_target_id,
        archived_connection=connection,
        connection_generation=connection.generation if connection else None,
    )
    DMSendControl.objects.filter(social_account=current).update(paused=True, epoch=F("epoch") + 1)
    DMConversationOwnership.objects.filter(social_account=current).update(paused=True, epoch=F("epoch") + 1)
    ConversationWorkState.objects.filter(conversation__social_account=current).update(
        owner_paused=True,
        fencing_counter=F("fencing_counter") + 1,
        pause_reason="account_disconnected",
        due_at=None,
    )
    if connection is not None:
        connection.enabled = False
        connection.generation = uuid4()
        connection.blocked_reason = "account_disconnected"
        connection.save(update_fields=["enabled", "generation", "blocked_reason", "updated_at"])
        connection.checkpoints.update(lease_token=None, lease_expires_at=None, fence=F("fence") + 1, status="blocked")
    current.connection_status = "disconnected"
    current.oauth_access_token = current.oauth_refresh_token = ""
    current.token_expires_at = None
    current.analytics_auth_updated_at = timezone.now()
    current.webhooks_active = False
    current.save(
        update_fields=[
            "connection_status",
            "oauth_access_token",
            "oauth_refresh_token",
            "token_expires_at",
            "analytics_auth_updated_at",
            "webhooks_active",
            "updated_at",
        ]
    )
    return current
