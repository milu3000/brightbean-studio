"""Prevent ordinary deletion from erasing an unresolved existing reply.

No new history or expiry policy is created. Original invocation settlement can
resolve UNKNOWN; no deletion, retry or manual force-clear does so here.
"""

from django.db.models.deletion import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.social_accounts.models import SocialAccount

from .locking import lock_dm_account
from .models import InboxMessage, InboxReply

UNRESOLVED_HISTORY = (
    "Delivery outcome is unknown or unverified. Keep this account and its message history until the result is verified."
)


def has_unresolved_replies(account_id):
    from .reply_safety import is_unresolved_reply

    candidates = InboxReply.objects.filter(
        inbox_message__social_account_id=account_id,
        status__in=[InboxReply.Status.UNKNOWN, InboxReply.Status.FAILED],
    ).select_related("inbox_message")
    return any(is_unresolved_reply(reply) for reply in candidates.iterator(chunk_size=100))


def _protect(account_id, workspace_id, *, message_id=None, reply_id=None):
    # Send preparation/settlement and deletion use the same account lock. A
    # deletion cannot check a draft, wait for delivery, then erase UNKNOWN.
    account = lock_dm_account(account_id, workspace_id)
    if account is None:
        # A stale message workspace must not make its account's receipt erasable.
        # This internal deletion hook locks only; it grants no read/send access.
        account = SocialAccount.objects.select_for_update().filter(pk=account_id).first()
        if account is None:
            return
    receipts = InboxReply.objects.select_for_update().filter(inbox_message__social_account_id=account_id)
    if message_id is not None:
        receipts = receipts.filter(inbox_message_id=message_id)
    if reply_id is not None:
        receipts = receipts.filter(pk=reply_id)
    from .reply_safety import is_unresolved_reply

    for candidate in receipts.filter(status__in=[InboxReply.Status.UNKNOWN, InboxReply.Status.FAILED]).iterator(
        chunk_size=100
    ):
        if is_unresolved_reply(candidate):
            raise ProtectedError(UNRESOLVED_HISTORY, [candidate])


@receiver(pre_delete, sender=SocialAccount, dispatch_uid="inbox.protect_unknown_account")
def protect_account(sender, instance, **kwargs):
    _protect(instance.pk, instance.workspace_id)


@receiver(pre_delete, sender=InboxMessage, dispatch_uid="inbox.protect_unknown_message")
def protect_message(sender, instance, **kwargs):
    _protect(instance.social_account_id, instance.workspace_id, message_id=instance.pk)


@receiver(pre_delete, sender=InboxReply, dispatch_uid="inbox.protect_unknown_reply")
def protect_reply(sender, instance, **kwargs):
    message = (
        InboxMessage.objects.filter(pk=instance.inbox_message_id).values("social_account_id", "workspace_id").first()
    )
    if message:
        _protect(message["social_account_id"], message["workspace_id"], reply_id=instance.pk)
