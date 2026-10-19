"""Saved acceptance can repair a missing projection without a second POST."""

from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.inbox.accepted_projection import repair_missing_sent_replies, repair_sent_reply
from apps.inbox.conversations import record_reply
from apps.inbox.models import ConversationMessage, InboxReply
from apps.inbox.tasks import InboxSyncEngine
from apps.inbox.tests.test_conversation_history import _sent
from apps.inbox.tests.test_conversation_history import v2 as v2

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("v2")]


def test_restart_after_projection_failure_uses_original_receipt(inbox_account):
    reply = _sent(inbox_account)
    with patch("apps.inbox.accepted_projection.repair_sent_reply", side_effect=RuntimeError("synthetic failure")):
        assert repair_missing_sent_replies() == {"repaired": 0, "held": 0, "failed": 1}
    reply.refresh_from_db()
    assert reply.status == "sent" and reply.platform_reply_id == "native-1"
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        assert repair_missing_sent_replies() == {"repaired": 1, "held": 0, "failed": 0}
        assert repair_missing_sent_replies() == {"repaired": 0, "held": 0, "failed": 0}
        provider.assert_not_called()
    row = ConversationMessage.objects.get(legacy_reply=reply)
    assert row.platform_message_id == reply.platform_reply_id
    assert row.direction == "outbound" and row.delivery_status == "provider_accepted"
    assert row.body == reply.body and row.occurred_at == reply.sent_at
    reply.inbox_message.refresh_from_db()
    assert reply.inbox_message.status == "archived"


@pytest.mark.parametrize("status", ["draft", "failed", "unknown"])
def test_unaccepted_receipt_never_repaired(inbox_account, status):
    reply = _sent(inbox_account)
    InboxReply.objects.filter(pk=reply.pk).update(status=status)
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        assert repair_sent_reply(reply.pk) is False
        provider.assert_not_called()
    assert not ConversationMessage.objects.exists()


def test_compacted_receipt_never_restores_body(inbox_account):
    reply = _sent(inbox_account)
    InboxReply.objects.filter(pk=reply.pk).update(content_compacted_at=timezone.now())
    assert repair_sent_reply(reply.pk) is False
    assert not ConversationMessage.objects.exists()


def test_native_withdrawal_is_never_overwritten(inbox_account):
    reply = _sent(inbox_account)
    row = record_reply(reply)
    ConversationMessage.objects.filter(pk=row.pk).update(legacy_reply=None, body="", is_deleted=True)
    assert repair_sent_reply(reply.pk) is False
    row.refresh_from_db()
    assert row.is_deleted and row.body == "" and row.legacy_reply_id is None


def test_removed_capture_enrollment_holds_repair(inbox_account, settings):
    reply = _sent(inbox_account)
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
    assert repair_sent_reply(reply.pk) is False
    assert repair_missing_sent_replies() == {"repaired": 0, "held": 0, "failed": 0}
    assert not ConversationMessage.objects.exists()


def test_existing_worker_repairs_before_polling():
    with (
        patch("apps.inbox.accepted_projection.repair_missing_sent_replies") as repair,
        patch("apps.inbox.sync_scheduler.run_sync_cycle") as sync,
        patch.object(InboxSyncEngine, "sync_all"),
        patch.object(InboxSyncEngine, "check_sla"),
    ):
        InboxSyncEngine().run_cycle()
    repair.assert_called_once_with()
    sync.assert_called_once_with()


@pytest.mark.parametrize("limit", [False, 0, -1, 1001, "100"])
def test_repair_limit_is_bounded(limit):
    with pytest.raises(ValueError):
        repair_missing_sent_replies(limit=limit)


@pytest.mark.parametrize(
    "field,value",
    [("platform_reply_id", "bad id"), ("not_sent_verified", True), ("account_platform_id", "changed-native-account")],
)
def test_conflicting_saved_receipt_is_held(inbox_account, field, value):
    reply = _sent(inbox_account)
    InboxReply.objects.filter(pk=reply.pk).update(**{field: value})
    assert repair_sent_reply(reply.pk) is False
    assert not ConversationMessage.objects.exists()


def test_held_prefix_does_not_starve_later_receipts_after_worker_restart(inbox_account):
    from datetime import UTC, datetime, timedelta

    replies = [_sent(inbox_account, mid="synthetic-fair-0")]
    for index in range(1, 3):
        replies.append(
            InboxReply.objects.create(
                inbox_message=replies[0].inbox_message,
                status="sent",
                body="Synthetic fair repair",
                platform_reply_id=f"synthetic-fair-{index}",
                sent_at=timezone.now(),
            )
        )
    InboxReply.objects.filter(pk__in=[reply.pk for reply in replies[:2]]).update(account_platform_id="old-account")
    first_slot = datetime(2026, 1, 1, tzinfo=UTC)
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        assert repair_missing_sent_replies(limit=1, now=first_slot) == {"repaired": 0, "held": 1, "failed": 0}
        # There is no process memory/cursor to carry across these invocations.
        assert repair_missing_sent_replies(limit=1, now=first_slot + timedelta(minutes=10)) == {
            "repaired": 1,
            "held": 0,
            "failed": 0,
        }
        provider.assert_not_called()
    assert ConversationMessage.objects.get(legacy_reply=replies[2]).platform_message_id == "synthetic-fair-2"
