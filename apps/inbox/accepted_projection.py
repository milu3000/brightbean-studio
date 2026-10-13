"""Repair missing outgoing history from saved sent receipts, without dispatch.

The existing sync worker supplies the retry opportunity. No new queue, capture
activation, provider read, read acknowledgement or content restoration is added.
"""

import logging

from django.db import transaction
from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from apps.social_accounts.models import SocialAccount

from .conversation_policy import capture_allowed
from .locking import lock_dm_account
from .models import ConversationMessage, DMSendAttempt, InboxConversation, InboxReply

logger = logging.getLogger(__name__)


@transaction.atomic
def repair_sent_reply(reply_id):
    """Recheck one persisted receipt under the same account lock as ingestion."""
    snapshot = InboxReply.objects.select_related("inbox_message__social_account").filter(pk=reply_id).first()
    if snapshot is None:
        return False
    message = snapshot.inbox_message
    account = lock_dm_account(message.social_account_id, message.workspace_id)
    if account is None or account.connection_status != "connected" or not capture_allowed(account):
        return False
    current = (
        InboxReply.objects.select_for_update()
        .select_related("inbox_message__social_account")
        .filter(pk=reply_id, inbox_message_id=message.pk)
        .first()
    )
    if (
        current is None
        or current.status != "sent"
        or current.inbox_message.message_type != "dm"
        or current.inbox_message.workspace_id != account.workspace_id
        or current.inbox_message.social_account_id != account.pk
        or current.inbox_message.social_account.platform != account.platform
        or current.not_sent_verified
        or current.content_compacted_at
        or not current.platform_reply_id
        or not current.sent_at
    ):
        return False
    from .conversations import _id, _timestamp, record_reply

    if _id(current.platform_reply_id) != current.platform_reply_id or _timestamp(current.sent_at) is None:
        return False
    if current.account_platform_id and current.account_platform_id != account.account_platform_id:
        return False
    # Native evidence, including withdrawal/tombstones, takes precedence. A
    # missing receipt link must never rebuild a body on an already observed ID.
    if (
        ConversationMessage.objects.filter(
            workspace_id=account.workspace_id,
            social_account=account,
            platform=account.platform,
            platform_message_id=current.platform_reply_id,
        ).exists()
        or ConversationMessage.objects.filter(legacy_reply=current).exists()
    ):
        return False
    if current.conversation_id:
        from .canonical_access import read_connection

        conversation = InboxConversation.objects.filter(
            pk=current.conversation_id,
            workspace_id=account.workspace_id,
            social_account=account,
            platform=account.platform,
            platform_conversation_id=current.platform_conversation_id,
            peer_id=current.recipient_id,
        ).first()
        if conversation is None or current.account_platform_id != account.account_platform_id:
            return False
        try:
            connection, archived = read_connection(account)
        except ValueError:
            return False
        if archived or current.connection_generation != (connection.generation if connection else None):
            return False
    return record_reply(current) is not None


def _repair_batch(queryset, limit, now):
    """Rotate bounded pages without mutating receipt versions or storing content.

    The five-minute worker's wall-clock slot survives process restarts. A fixed
    held prefix cannot prevent later saved receipts from getting a repair turn.
    """
    count = queryset.count()
    if not count:
        return []
    pages = (count + limit - 1) // limit
    slot = int((now or timezone.now()).timestamp()) // 300
    offset = (slot % pages) * limit
    return list(queryset.values_list("pk", flat=True)[offset : offset + limit])


def repair_missing_sent_replies(*, limit=100, now=None):
    """Bound local recovery; a failed projection cannot prevent other repairs."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Repair limit must be an integer between 1 and 1000.")
    accounts = [
        account.pk
        for account in SocialAccount.objects.filter(
            connection_status="connected", platform__in=["facebook", "instagram_login"]
        )
        if capture_allowed(account)
    ]
    projected = ConversationMessage.objects.filter(
        workspace_id=OuterRef("inbox_message__workspace_id"),
        social_account_id=OuterRef("inbox_message__social_account_id"),
        platform=OuterRef("inbox_message__social_account__platform"),
        platform_message_id=OuterRef("platform_reply_id"),
    )
    receipts = (
        InboxReply.objects.filter(
            status="sent",
            sent_at__isnull=False,
            content_compacted_at__isnull=True,
            inbox_message__message_type="dm",
            inbox_message__workspace_id=F("inbox_message__social_account__workspace_id"),
            inbox_message__social_account__connection_status="connected",
            conversation_message__isnull=True,
            inbox_message__social_account_id__in=accounts,
        )
        .exclude(platform_reply_id="")
        .annotate(has_projection=Exists(projected))
        .filter(has_projection=False)
        .order_by("sent_at", "pk")
    )
    result = {"repaired": 0, "held": 0, "failed": 0}
    for reply_id in _repair_batch(receipts, limit, now):
        try:
            result["repaired" if repair_sent_reply(reply_id) else "held"] += 1
        except Exception:
            # Do not log body, provider payloads or credentials. The retained
            # sent receipt remains non-sendable and can be retried next cycle.
            logger.warning("Saved sent receipt projection failed for reply %s", reply_id)
            result["failed"] += 1
    return result


def repair_missing_accepted_settlements(*, limit=100, now=None):
    """Only exact accepted attempts can retry their local operation settlement."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Repair limit must be an integer between 1 and 1000.")
    from .reply_dispatch import settle_recorded_acceptance

    candidates = DMSendAttempt.objects.filter(
        outcome="sent", reason_code="provider_accepted", operation__status="outcome_unknown"
    ).order_by("completed_at", "pk")
    result = {"repaired": 0, "held": 0, "failed": 0}
    for attempt_id in _repair_batch(candidates, limit, now):
        try:
            result["repaired" if settle_recorded_acceptance(attempt_id) else "held"] += 1
        except Exception:
            logger.warning("Saved accepted attempt settlement failed for attempt %s", attempt_id)
            result["failed"] += 1
    return result
