"""Durable normalized delivery receipts; no raw signed payload or provider token."""

import uuid

from django.db import models


class InboxSyncReceipt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey("inbox.InboxSyncConnection", on_delete=models.PROTECT, related_name="receipts")
    connection_generation = models.UUIDField()
    event_key = models.CharField(max_length=64)
    platform_message_id = models.CharField(max_length=255)
    context = models.CharField(max_length=12)
    kind = models.CharField(max_length=24, default="message")
    status = models.CharField(max_length=24, default="pending")
    # Only bounded normalized content. Cleared atomically once canonical commit succeeds.
    payload = models.JSONField(default=dict)
    observed_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    processed_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=40, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inbox_sync_receipt"
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "connection_generation", "event_key"], name="inbox_sync_receipt_dedupe"
            )
        ]
        indexes = [models.Index(fields=["status", "updated_at"], name="inbox_sync_receipt_due")]
