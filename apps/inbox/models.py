"""Models for the Unified Social Inbox (F-3.1)."""

import uuid

from django.conf import settings
from django.db import models

from apps.common.managers import WorkspaceScopedManager


class InboxMessage(models.Model):
    class MessageType(models.TextChoices):
        COMMENT = "comment", "Comment"
        MENTION = "mention", "Mention"
        DM = "dm", "Direct Message"
        REVIEW = "review", "Review"

    class Status(models.TextChoices):
        UNREAD = "unread", "Unread"
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"
        ARCHIVED = "archived", "Archived"

    class Sentiment(models.TextChoices):
        POSITIVE = "positive", "Positive"
        NEUTRAL = "neutral", "Neutral"
        NEGATIVE = "negative", "Negative"

    class SentimentSource(models.TextChoices):
        AUTO = "auto", "Auto"
        MANUAL = "manual", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="inbox_messages",
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="inbox_messages",
    )
    platform_message_id = models.CharField(max_length=255, db_index=True)
    message_type = models.CharField(
        max_length=20,
        choices=MessageType.choices,
        default=MessageType.COMMENT,
        db_index=True,
    )
    sender_name = models.CharField(max_length=255)
    sender_handle = models.CharField(max_length=255, blank=True, default="")
    sender_avatar_url = models.URLField(max_length=500, blank=True, default="")
    body = models.TextField(blank=True, default="")
    sentiment = models.CharField(
        max_length=10,
        choices=Sentiment.choices,
        default=Sentiment.NEUTRAL,
        db_index=True,
    )
    sentiment_source = models.CharField(
        max_length=10,
        choices=SentimentSource.choices,
        default=SentimentSource.AUTO,
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.UNREAD,
        db_index=True,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_inbox_messages",
    )
    parent_message = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="thread_replies",
    )
    related_post = models.ForeignKey(
        "composer.PlatformPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inbox_messages",
    )
    extra = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "inbox_message"
        ordering = ["-received_at"]
        unique_together = [("social_account", "platform_message_id")]
        indexes = [
            models.Index(
                fields=["workspace", "status", "-received_at"],
                name="inbox_msg_ws_status_recv",
            ),
            models.Index(
                fields=["workspace", "assigned_to", "status"],
                name="inbox_msg_ws_assign_status",
            ),
            models.Index(
                fields=["workspace", "social_account", "-received_at"],
                name="inbox_msg_ws_account_recv",
            ),
        ]

    def __str__(self):
        return f"{self.type_display} from {self.sender_name}"

    def _conversation_classification(self):
        if self.message_type != self.MessageType.DM:
            return "", ""
        row = getattr(self, "conversation_message", None)
        if row is not None:
            evidence = row.conversation if row.conversation_id else row
            return evidence.conversation_type, evidence.classification_reason
        from providers.meta_inbox_content import classify_conversation_identity

        extra = self.extra if isinstance(self.extra, dict) else {}
        summary = extra.get("conversation_type"), extra.get("classification_reason")
        if (
            not any(key in extra for key in ("participant_ids", "participants"))
            and all(isinstance(value, str) for value in summary)
            and summary
            in {
                ("direct", "participants_pair"),
                ("group", "participants_group"),
            }
        ):
            return summary
        kind, reason, _peer = classify_conversation_identity(
            self.extra,
            own_ids=[self.social_account.account_platform_id, self.social_account.webhook_target_id],
        )
        return kind, reason

    @property
    def conversation_type(self):
        return self._conversation_classification()[0]

    @property
    def classification_reason(self):
        return self._conversation_classification()[1]

    @property
    def type_display(self):
        if self.message_type != self.MessageType.DM:
            return self.get_message_type_display()
        return {"direct": "Direct Message", "group": "Group Message"}.get(
            self.conversation_type, "Message (type unknown)"
        )

    @property
    def attachments(self):
        from providers.meta_inbox_content import normalize_attachments

        return normalize_attachments(self.extra)

    @property
    def content_type(self):
        if self.attachments:
            return "mixed" if self.body else "attachment"
        return "text" if self.body else "unknown"

    @property
    def content_status(self):
        from providers.meta_inbox_content import message_content_status

        return message_content_status(self.extra, self.body or "")

    @property
    def content_preview(self):
        if self.body:
            return self.body
        labels = {"share": "Shared content", "image": "Photo", "video": "Video", "audio": "Audio", "file": "File"}
        attachments = self.attachments
        return labels.get(attachments[0]["type"], "Non-text message") if attachments else "Non-text message"

    @property
    def platform(self):
        return self.social_account.platform


class InboxReply(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        UNKNOWN = "unknown", "Outcome unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inbox_message = models.ForeignKey(
        InboxMessage,
        on_delete=models.CASCADE,
        related_name="replies",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="inbox_replies",
    )
    body = models.TextField()
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    platform_reply_id = models.CharField(max_length=255, blank=True, default="")
    send_error = models.TextField(blank=True, default="")
    # ``sent_at`` is null until the reply is actually delivered to the platform;
    # a row now exists in ``draft``/``failed`` states before any send happens.
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inbox_reply"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_status_display()} reply by {self.author} ({self.created_at:%Y-%m-%d %H:%M})"


class DMSendControl(models.Model):
    """Explicit enrollment pins an identity; flags cannot remove this hold."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    social_account = models.OneToOneField(
        "social_accounts.SocialAccount", on_delete=models.PROTECT, related_name="dm_send_control"
    )
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.PROTECT)
    platform = models.CharField(max_length=30)
    account_platform_id = models.CharField(max_length=255)
    paused = models.BooleanField(default=True)
    epoch = models.PositiveBigIntegerField(default=1)
    resume_cutoff = models.DateTimeField(null=True, blank=True)
    coverage_from = models.DateTimeField()
    coverage_version = models.CharField(max_length=40)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inbox_dm_send_control"


class DMSendAttempt(models.Model):
    class Outcome(models.TextChoices):
        UNKNOWN = "unknown", "Possibly in flight / outcome unknown"
        SENT = "sent", "Provider accepted"
        NOT_SENT = "not_sent", "Known not sent"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    control = models.ForeignKey(DMSendControl, on_delete=models.PROTECT, related_name="attempts")
    reply = models.ForeignKey(InboxReply, on_delete=models.PROTECT, related_name="dm_send_attempts")
    operation = models.OneToOneField(
        "SendOperation", on_delete=models.PROTECT, null=True, blank=True, related_name="durable_attempt"
    )
    epoch = models.PositiveBigIntegerField()
    fingerprint = models.CharField(max_length=64)
    outcome = models.CharField(max_length=10, choices=Outcome.choices, default=Outcome.UNKNOWN)
    reason_code = models.CharField(max_length=40, default="attempt_committed")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "inbox_dm_send_attempt"
        constraints = [
            models.UniqueConstraint(
                fields=["control"], condition=models.Q(outcome="unknown"), name="inbox_dm_one_unresolved"
            ),
        ]


class InternalNote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inbox_message = models.ForeignKey(
        InboxMessage,
        on_delete=models.CASCADE,
        related_name="internal_notes",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="inbox_notes",
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inbox_internal_note"
        ordering = ["created_at"]

    def __str__(self):
        return f"Note by {self.author} on {self.created_at:%Y-%m-%d %H:%M}"


class SavedReply(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="saved_replies",
    )
    title = models.CharField(max_length=255)
    body = models.TextField(
        help_text="Supports variables: {sender_name}, {account_name}, {post_url}",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_saved_replies",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "inbox_saved_reply"
        ordering = ["title"]

    def __str__(self):
        return self.title

    def render(self, context: dict) -> str:
        """Substitute variables in body with context values."""
        text = self.body
        for key, value in context.items():
            text = text.replace(f"{{{key}}}", str(value))
        return text


class InboxSLAConfig(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="inbox_sla_config",
    )
    target_response_minutes = models.PositiveIntegerField(default=120)
    is_active = models.BooleanField(default=False)
    auto_resolve_on_reply = models.BooleanField(
        default=True,
        help_text="Automatically mark messages as resolved when a reply is sent.",
    )

    class Meta:
        db_table = "inbox_sla_config"

    def __str__(self):
        return f"SLA Config for {self.workspace} ({self.target_response_minutes}min)"


class InboxConversation(models.Model):
    """Conversation identity only; never an inbox work item or an SLA state."""

    class ConversationType(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        DIRECT = "direct", "Direct"
        GROUP = "group", "Group"

    class IdentityKind(models.TextChoices):
        PLATFORM = "platform", "Provider conversation"
        VERIFIED_PEER = "verified_peer", "Verified one-to-one peer"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="inbox_conversations")
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.CASCADE, related_name="inbox_conversations"
    )
    platform = models.CharField(max_length=30)
    platform_conversation_id = models.CharField(max_length=255, null=True, blank=True)
    peer_id = models.CharField(max_length=255, blank=True, default="")
    peer_ambiguous = models.BooleanField(default=False)
    conversation_type = models.CharField(
        max_length=10, choices=ConversationType.choices, default=ConversationType.UNKNOWN
    )
    classification_reason = models.CharField(max_length=40, default="participants_missing")
    identity_kind = models.CharField(max_length=20, choices=IdentityKind.choices)
    revision = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "inbox_conversation"
        constraints = [
            models.UniqueConstraint(
                fields=["social_account", "platform", "platform_conversation_id"], name="inbox_convo_provider_unique"
            ),
            models.UniqueConstraint(
                fields=["social_account", "platform", "peer_id"],
                condition=models.Q(platform_conversation_id__isnull=True) & ~models.Q(peer_id=""),
                name="inbox_convo_fallback_unique",
            ),
        ]
        indexes = [models.Index(fields=["workspace", "social_account", "peer_id"], name="inbox_convo_scope_peer")]


class ConversationMessage(models.Model):
    """Additive DM ledger, including native outbound and unattributed messages.

    No raw provider payload, credentials, assignment, status or notification
    state belongs here. A missing provider ID is only valid for a linked local
    legacy reply whose delivery cannot be verified.
    """

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"
        UNKNOWN = "unknown", "Unknown"

    class DeliveryStatus(models.TextChoices):
        OBSERVED = "observed", "Observed on provider"
        PROVIDER_ACCEPTED = "provider_accepted", "Accepted by provider"
        DELIVERY_UNVERIFIED = "delivery_unverified", "Delivery unverified"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="conversation_messages"
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.CASCADE, related_name="conversation_messages"
    )
    platform = models.CharField(max_length=30)
    conversation = models.ForeignKey(
        InboxConversation, on_delete=models.SET_NULL, null=True, blank=True, related_name="messages"
    )
    conversation_attribution = models.CharField(
        max_length=20, choices=InboxConversation.IdentityKind.choices, blank=True, default=""
    )
    platform_message_id = models.CharField(max_length=255, null=True, blank=True)
    direction = models.CharField(max_length=10, choices=Direction.choices, default=Direction.UNKNOWN)
    conversation_type = models.CharField(
        max_length=10,
        choices=InboxConversation.ConversationType.choices,
        default=InboxConversation.ConversationType.UNKNOWN,
    )
    classification_reason = models.CharField(max_length=40, default="participants_missing")
    sender_id = models.CharField(max_length=255, blank=True, default="")
    recipient_id = models.CharField(max_length=255, blank=True, default="")
    sender_name = models.CharField(max_length=255, blank=True, default="")
    body = models.TextField(blank=True, default="")
    attachments = models.JSONField(default=list, blank=True)
    # Bounded observation status only; no raw payload or media cache is retained.
    content_status = models.CharField(max_length=25, default="unknown")
    occurred_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    sources = models.JSONField(default=list, blank=True)
    delivery_status = models.CharField(max_length=25, choices=DeliveryStatus.choices, default=DeliveryStatus.OBSERVED)
    legacy_message = models.OneToOneField(
        InboxMessage, on_delete=models.SET_NULL, null=True, blank=True, related_name="conversation_message"
    )
    legacy_reply = models.OneToOneField(
        InboxReply, on_delete=models.SET_NULL, null=True, blank=True, related_name="conversation_message"
    )
    first_seen_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "inbox_conversation_message"
        ordering = ["occurred_at", "first_seen_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["social_account", "platform", "platform_message_id"], name="inbox_convo_msg_provider_unique"
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "social_account", "occurred_at"], name="inbox_convo_msg_scope_time"),
            models.Index(fields=["conversation", "occurred_at"], name="inbox_convo_msg_thread_time"),
            models.Index(fields=["conversation", "first_seen_at", "id"], name="inbox_convo_msg_observed"),
        ]


class ConversationSyncState(models.Model):
    """Per-stream observations; a poll attempt is not successful DM freshness."""

    class Status(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    class Coverage(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        PARTIAL = "partial", "Partial"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="conversation_sync_states"
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.CASCADE, related_name="conversation_sync_states"
    )
    platform = models.CharField(max_length=30)
    stream = models.CharField(max_length=20, choices=[("dm", "Direct messages"), ("comment", "Comments")])
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UNKNOWN)
    coverage = models.CharField(max_length=10, choices=Coverage.choices, default=Coverage.UNKNOWN)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=40, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "inbox_conversation_sync_state"
        constraints = [
            models.UniqueConstraint(fields=["social_account", "platform", "stream"], name="inbox_convo_sync_unique"),
        ]


class ConversationWorkState(models.Model):
    """Optional local coordination, separate from history and legacy inbox work."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.OneToOneField(InboxConversation, on_delete=models.CASCADE, related_name="reply_work_state")
    generation = models.PositiveBigIntegerField(default=0)
    conversation_revision = models.PositiveBigIntegerField(default=0)
    latest_incoming = models.ForeignKey(
        ConversationMessage, on_delete=models.SET_NULL, null=True, blank=True, related_name="latest_reply_work_states"
    )
    burst_started_at = models.DateTimeField(null=True, blank=True)
    latest_incoming_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    owner_paused = models.BooleanField(default=False)
    # Identity transfer involving uncertain delivery must survive ordinary
    # resume and even deletion of the transferred target. No clearing API yet.
    identity_quarantined = models.BooleanField(default=False)
    ordering_uncertain = models.BooleanField(default=False)
    history_gap = models.BooleanField(default=False)
    pause_reason = models.CharField(max_length=40, blank=True, default="")
    active_operation = models.ForeignKey(
        "SendOperation", on_delete=models.SET_NULL, null=True, blank=True, related_name="active_work_states"
    )
    fencing_counter = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inbox_conversation_work_state"
        indexes = [models.Index(fields=["owner_paused", "due_at"], name="inbox_work_pause_due")]


class DMConversationOwnership(models.Model):
    """Explicit, durable ownership of one observed DM identity; never a grant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    control = models.ForeignKey(DMSendControl, on_delete=models.PROTECT, related_name="conversation_owners")
    conversation = models.OneToOneField(InboxConversation, on_delete=models.PROTECT, related_name="dispatch_ownership")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.PROTECT)
    social_account = models.ForeignKey("social_accounts.SocialAccount", on_delete=models.PROTECT)
    platform = models.CharField(max_length=30)
    account_platform_id = models.CharField(max_length=255)
    platform_conversation_id = models.CharField(max_length=255, null=True, blank=True)
    peer_id = models.CharField(max_length=255)
    identity_kind = models.CharField(max_length=20)
    owner_scope = models.CharField(max_length=255)
    epoch = models.PositiveBigIntegerField(default=1)
    paused = models.BooleanField(default=True)
    resume_cutoff = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inbox_dm_conversation_ownership"


class SendOperation(models.Model):
    """Stable intent; a claim alone is never authorization to dispatch."""

    class Status(models.TextChoices):
        PREPARED = "prepared", "Prepared locally"
        CLAIMED = "claimed", "Claimed locally"
        CONFIRMED = "confirmed", "Provider accepted or explicitly reconciled"
        FAILED = "failed", "Explicitly known not sent"
        OUTCOME_UNKNOWN = "outcome_unknown", "Outcome unknown; reconciliation required"
        SUPERSEDED = "superseded", "Superseded before dispatch"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="send_operations")
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.CASCADE, related_name="send_operations"
    )
    platform = models.CharField(max_length=30)
    conversation = models.ForeignKey(InboxConversation, on_delete=models.CASCADE, related_name="send_operations")
    actor_scope = models.CharField(max_length=255)
    idempotency_key = models.CharField(max_length=128)
    payload_fingerprint = models.CharField(max_length=64)
    body = models.TextField()
    target = models.ForeignKey(
        ConversationMessage, on_delete=models.SET_NULL, null=True, related_name="reply_send_operations"
    )
    expected_revision = models.PositiveBigIntegerField()
    expected_generation = models.PositiveBigIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PREPARED)
    claim_token = models.UUIDField(null=True, blank=True)
    fencing_token = models.PositiveBigIntegerField(default=0)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    # Durable before provider dispatch. Never reclaim or retry by timeout.
    external_attempted_at = models.DateTimeField(null=True, blank=True)
    ownership = models.ForeignKey(
        DMConversationOwnership, on_delete=models.PROTECT, null=True, blank=True, related_name="operations"
    )
    owner_epoch = models.PositiveBigIntegerField(default=0)
    reply = models.OneToOneField(
        InboxReply, on_delete=models.PROTECT, null=True, blank=True, related_name="send_operation"
    )
    attempt = models.OneToOneField(
        DMSendAttempt, on_delete=models.PROTECT, null=True, blank=True, related_name="send_operation"
    )
    # Survives removal of the optional ledger target; never inferred from time.
    target_platform_message_id = models.CharField(max_length=255, blank=True, default="")
    outcome_code = models.CharField(max_length=40, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "inbox_send_operation"
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "actor_scope", "idempotency_key"], name="inbox_send_idempotency_unique"
            ),
            models.UniqueConstraint(
                fields=["conversation", "target_platform_message_id"],
                condition=models.Q(status="confirmed") & ~models.Q(target_platform_message_id=""),
                name="inbox_send_confirmed_target",
            ),
            models.UniqueConstraint(
                fields=["conversation"],
                condition=models.Q(status__in=["prepared", "claimed", "outcome_unknown"]),
                name="inbox_send_single_active",
            ),
        ]
