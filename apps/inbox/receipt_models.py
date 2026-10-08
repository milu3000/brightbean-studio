"""Restricted preservation metadata, separate from ordinary inbox projections.

No TTL job, recovery deadline or destructive cleanup is defined here.
"""

import uuid

from django.db import models


class InboxReplyContentRecovery(models.Model):
    reply = models.OneToOneField(
        "inbox.InboxReply", on_delete=models.PROTECT, primary_key=True, related_name="restricted_content"
    )
    body = models.TextField(blank=True, default="")
    send_error = models.TextField(blank=True, default="")
    canonical_body = models.TextField(blank=True, default="")
    canonical_attachments = models.JSONField(default=list, blank=True)
    reason = models.CharField(max_length=30)
    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inbox_reply_content_recovery"


class InboxArchiveIdentity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.PROTECT, related_name="inbox_archives"
    )
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.PROTECT)
    platform = models.CharField(max_length=30)
    account_platform_id = models.CharField(max_length=255)
    webhook_target_id = models.CharField(max_length=255, blank=True, default="")
    archived_connection = models.ForeignKey(
        "inbox.InboxSyncConnection", on_delete=models.PROTECT, null=True, blank=True
    )
    connection_generation = models.UUIDField(null=True, blank=True)
    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inbox_archive_identity"
        ordering = ["-archived_at", "-id"]
