"""Persistent, principal-bound MCP Events subscriptions and transactional outbox."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.encryption import EncryptedTextField


class EventSubscription(models.Model):
    # Deterministic opaque digest of principal, callback, name and canonical filters.
    id = models.CharField(primary_key=True, max_length=68, editable=False)
    principal = models.CharField(max_length=200, db_index=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE)
    social_account = models.ForeignKey("social_accounts.SocialAccount", on_delete=models.CASCADE)
    api_key = models.ForeignKey("api_keys.ApiKey", null=True, blank=True, on_delete=models.CASCADE)
    # Never persist a bearer token; bind OAuth to the exact latest authenticated grant.
    oauth_application_id = models.PositiveBigIntegerField(null=True, blank=True)
    oauth_token_checksum = models.CharField(max_length=64, blank=True, default="")
    name = models.CharField(max_length=100)
    arguments = models.JSONField(default=dict)
    callback_url = EncryptedTextField()
    callback_hash = models.CharField(max_length=64, db_index=True)
    signing_secret = EncryptedTextField()
    previous_secret = EncryptedTextField(blank=True, default="")
    previous_secret_until = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    active = models.BooleanField(default=True, db_index=True)
    stopped_reason = models.CharField(max_length=40, blank=True, default="")
    # A new generation never revives cancelled deliveries on resubscribe.
    generation = models.UUIDField(default=uuid.uuid4, editable=False)
    started_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(api_key__isnull=False, oauth_application_id__isnull=True, oauth_token_checksum="")
                    | (
                        models.Q(api_key__isnull=True, oauth_application_id__isnull=False)
                        & ~models.Q(oauth_token_checksum="")
                    )
                ),
                name="mcp_subscription_credential_kind",
            ),
        ]


class EventOutbox(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DELIVERED = "delivered", "Delivered"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(EventSubscription, on_delete=models.CASCADE, related_name="deliveries")
    message = models.ForeignKey("inbox.InboxMessage", on_delete=models.CASCADE)
    generation = models.UUIDField()
    event_id = models.CharField(max_length=68)
    # Serialize once; retries preserve the original body and event ID.
    payload = EncryptedTextField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_status = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=40, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["subscription", "event_id"], name="mcp_event_subscription_dedupe"),
        ]
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="mcp_outbox_due")]
