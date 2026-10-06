"""Explicit manager review of unresolved, unenrolled DM receipts only.

This records a human assertion. It performs no provider request or retry and
cannot override persisted account gates, ownership or coordinated operations.
"""

from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.members.models import WorkspaceMembership

from .dm_send_gate import DMSendGateError
from .locking import lock_dm_account
from .models import DMConversationOwnership, DMSendControl, InboxMessage, InboxReply, InternalNote, SendOperation
from .reply_safety import is_unresolved_reply


def _error(code, message):
    return DMSendGateError("reconciliation_" + code, message)


def _aware_timestamp(value):
    if isinstance(value, str):
        try:
            value = parse_datetime(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) and timezone.is_aware(value) else None


def _eligible(reply, account, message, actor):
    member = (
        WorkspaceMembership.objects.select_related("custom_role", "workspace")
        .filter(
            user_id=getattr(actor, "pk", None),
            user__is_active=True,
            workspace_id=account.workspace_id,
            workspace__is_archived=False,
        )
        .first()
    )
    if (
        member is None
        or (member.custom_role_id and member.custom_role.organization_id != member.workspace.organization_id)
        or not all(
            member.effective_permissions.get(permission, False)
            for permission in ("use_inbox", "manage_workspace_settings", "reply_from_inbox")
        )
    ):
        raise _error(
            "denied", "Current permission to read and reply from this inbox and manage its workspace is required."
        )
    if (
        message.workspace_id != account.workspace_id
        or message.social_account_id != account.pk
        or message.message_type != "dm"
    ):
        raise _error("denied", "This reply's current account and incoming message must be reviewed first.")
    if (
        DMSendControl.objects.filter(social_account_id=account.pk).exists()
        or DMConversationOwnership.objects.filter(social_account_id=account.pk).exists()
        or reply.dm_send_attempts.exists()
        or SendOperation.objects.filter(Q(reply_id=reply.pk) | Q(target__legacy_message_id=message.pk)).exists()
    ):
        raise _error(
            "managed", "This reply uses managed send controls and cannot be resolved through this review form."
        )
    if reply.status not in {InboxReply.Status.UNKNOWN, InboxReply.Status.FAILED} or not is_unresolved_reply(reply):
        raise _error(
            "not_required", "This reply no longer has an unresolved delivery outcome. Reload its current result."
        )


def reconciliation_availability(reply, *, actor):
    """Read-only advice for the review form; submission always rechecks it."""
    current = InboxReply.objects.select_related("inbox_message__social_account").filter(pk=reply.pk).first()
    try:
        if current is None:
            raise _error("not_required", "This reply is no longer available.")
        message = current.inbox_message
        _eligible(current, message.social_account, message, actor)
    except DMSendGateError as exc:
        return {"allowed": False, "code": exc.code, "reason": str(exc)}
    return {"allowed": True, "code": "ready", "reason": ""}


@transaction.atomic
def reconcile_reply_outcome(
    *,
    reply,
    actor,
    expected_updated_at,
    expected_send_generation,
    outcome,
    platform_reply_id="",
    sent_at=None,
    confirmed=False,
):
    """Resolve exactly the receipt version explicitly reviewed by this actor."""
    selected = reply.inbox_message
    account = lock_dm_account(selected.social_account_id, selected.workspace_id)
    if account is None:
        raise _error("denied", "The selected reply's account changed; reload before reviewing.")
    current = InboxReply.objects.select_for_update().filter(pk=reply.pk).first()
    if current is None or current.inbox_message_id != selected.pk:
        raise _error("stale", "This reply changed after you opened it. Reload and review the current receipt.")
    message = InboxMessage.objects.select_for_update().get(pk=current.inbox_message_id)
    _eligible(current, account, message, actor)
    expected = _aware_timestamp(expected_updated_at)
    if (
        expected is None
        or current.updated_at != expected
        or type(expected_send_generation) is not int
        or expected_send_generation < 0
        or current.send_generation != expected_send_generation
    ):
        raise _error("stale", "This reply changed after you opened it. Reload and review the current receipt.")
    prior_status = current.status
    if outcome not in {"sent", "not_sent"}:
        raise _error("outcome_invalid", "Select whether platform review confirmed sent or confirmed not sent.")
    if confirmed is not True:
        raise _error(
            "confirmation_required",
            "Explicitly confirm reliable evidence for this delivery outcome. A message's absence alone is insufficient.",
        )
    if outcome == "sent":
        receipt_time = _aware_timestamp(sent_at)
        earliest = max(message.received_at, current.created_at) - timedelta(seconds=1)
        if (
            not isinstance(platform_reply_id, str)
            or not platform_reply_id
            or len(platform_reply_id) > 255
            or any(
                character.isspace() or ord(character) < 32 or ord(character) == 127 for character in platform_reply_id
            )
            or receipt_time is None
            or receipt_time > timezone.now()
            or receipt_time < earliest
        ):
            raise _error("receipt_invalid", "Enter the provider message ID and its actual send time with a time zone.")
        if (
            (current.platform_reply_id and current.platform_reply_id != platform_reply_id)
            or (current.sent_at and current.sent_at != receipt_time)
            or InboxReply.objects.filter(
                inbox_message__social_account_id=account.pk, platform_reply_id=platform_reply_id
            )
            .exclude(pk=current.pk)
            .exists()
        ):
            raise _error(
                "receipt_conflict",
                "That provider message ID already belongs to another receipt or conflicts with this reply.",
            )
        current.status, current.platform_reply_id = InboxReply.Status.SENT, platform_reply_id
        current.sent_at, current.not_sent_verified = receipt_time, False
        decision = f"Manually confirmed sent after platform review. Provider message ID: {platform_reply_id}; sent at {receipt_time.isoformat()}."
    elif outcome == "not_sent":
        if platform_reply_id or sent_at or current.platform_reply_id or current.sent_at:
            raise _error(
                "receipt_conflict", "A reply with provider receipt details cannot be marked not sent through this form."
            )
        current.status, current.not_sent_verified = InboxReply.Status.FAILED, True
        decision = "Manually confirmed not sent based on explicit platform review. This is a human assertion, not an automated provider confirmation. No retry was sent."
    # Keep the original error on its receipt. Audit notes do not copy arbitrary
    # provider diagnostics, payloads, secrets or the customer's message body.
    current.save(update_fields=["status", "platform_reply_id", "sent_at", "not_sent_verified", "updated_at"])
    InternalNote.objects.create(
        inbox_message=message,
        author=actor,
        body=(
            f"Delivery outcome reviewed for reply {current.pk}.\n"
            f"Status: {prior_status} → {current.status}. Reviewed receipt version: {expected.isoformat()}; send generation {expected_send_generation}.\n"
            f"{decision}\nThe previous delivery diagnostic remains on the original reply receipt."
        ),
    )
    return current
