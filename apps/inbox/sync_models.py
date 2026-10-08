"""New durable sync reconstruction. Empty schema never enrolls an account."""

import uuid

from django.db import models


class InboxSyncConnection(models.Model):
    """Immutable native scope plus a separate, replaceable request credential fence."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    social_account = models.OneToOneField("social_accounts.SocialAccount", on_delete=models.PROTECT)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.PROTECT)
    platform = models.CharField(max_length=30)
    account_platform_id = models.CharField(max_length=255)
    webhook_target_id = models.CharField(max_length=255, blank=True, default="")
    generation = models.UUIDField(default=uuid.uuid4, editable=False)
    auth_fingerprint = models.CharField(max_length=64)
    route_contract = models.CharField(max_length=40, default="meta-dm-v25-v1")
    enabled = models.BooleanField(default=False)
    # An explicit durable ownership latch survives bootstrap pause/disconnect.
    ownership_claimed_at = models.DateTimeField(null=True, blank=True)
    bootstrap_baseline_at = models.DateTimeField(null=True, blank=True)
    retry_at = models.DateTimeField(null=True, blank=True)
    blocked_reason = models.CharField(max_length=40, blank=True, default="")
    last_served_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inbox_sync_connection"


class ConversationSyncIdentity(models.Model):
    conversation = models.OneToOneField(
        "inbox.InboxConversation", on_delete=models.PROTECT, primary_key=True, related_name="sync_identity"
    )
    connection = models.ForeignKey(InboxSyncConnection, on_delete=models.PROTECT)
    connection_generation = models.UUIDField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inbox_conversation_sync_identity"


class ConversationObservationState(models.Model):
    """One canonical message's evidence and restricted archive; never a second timeline."""

    message = models.OneToOneField(
        "inbox.ConversationMessage", on_delete=models.PROTECT, primary_key=True, related_name="observation_state"
    )
    connection_generation = models.UUIDField()
    content_fingerprint = models.CharField(max_length=64)
    provider_updated_at = models.DateTimeField(null=True, blank=True)
    provider_revision = models.PositiveBigIntegerField(null=True, blank=True)
    last_observed_at = models.DateTimeField()
    repair_required = models.BooleanField(default=False)
    conflict_count = models.PositiveSmallIntegerField(default=0)
    live_observed_at = models.DateTimeField(null=True, blank=True)
    live_conversation_id = models.UUIDField(null=True, blank=True)
    actionable_observed_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    retained_body = models.TextField(blank=True, default="")
    retained_attachments = models.JSONField(default=list, blank=True)
    retained_legacy_body = models.TextField(blank=True, default="")
    retained_legacy_attachments = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField()
    # No expiry job, destructive cleanup, or extra recovery duration is enabled.
    expired_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inbox_conversation_observation"


class InboxSyncCheckpoint(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(InboxSyncConnection, on_delete=models.PROTECT, related_name="checkpoints")
    connection_generation = models.UUIDField()
    stream = models.CharField(max_length=20, choices=[("conversations", "Conversations"), ("messages", "Messages")])
    scope_key = models.CharField(max_length=255, default="account")
    context = models.CharField(
        max_length=12, choices=[(value, value) for value in ("bootstrap", "backfill", "live", "repair")]
    )
    scan_generation = models.PositiveBigIntegerField(default=1)
    status = models.CharField(max_length=12, default="ready")
    cursor = models.CharField(max_length=1024, blank=True, default="")
    participant_ids = models.JSONField(default=list, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    fence = models.PositiveBigIntegerField(default=0)
    retry_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    restarts = models.PositiveSmallIntegerField(default=0)
    pages_committed = models.PositiveIntegerField(default=0)
    recent_cursor_digests = models.JSONField(default=list, blank=True)
    last_page_digest = models.CharField(max_length=64, blank=True, default="")
    last_error_code = models.CharField(max_length=40, blank=True, default="")
    content_fields_mode = models.CharField(max_length=12, default="extended")
    content_probe_after = models.DateTimeField(null=True, blank=True)
    scan_started_at = models.DateTimeField()
    coverage_from = models.DateTimeField()
    coverage = models.CharField(max_length=20, default="unknown")
    last_committed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inbox_sync_checkpoint"
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "stream", "scope_key", "context"], name="inbox_sync_checkpoint_unique"
            )
        ]
        indexes = [models.Index(fields=["status", "retry_at"], name="inbox_sync_checkpoint_due")]


class InboxSyncBudget(models.Model):
    """Bounded app-window counters and at most two live account reservations."""

    app_key = models.CharField(max_length=80, primary_key=True)
    window_started_at = models.DateTimeField()
    gets_reserved = models.PositiveSmallIntegerField(default=0)
    account_spend = models.JSONField(default=dict)
    active_leases = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inbox_sync_budget"
