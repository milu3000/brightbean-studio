"""Attachments enrich existing DMs, never create another arrival or reply window."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.inbox.models import InboxMessage
from apps.inbox.tasks import UNKNOWN_MESSAGE_TIMESTAMP, InboxSyncEngine
from apps.inbox.webhooks import _handle_facebook_messaging

pytestmark = pytest.mark.django_db
URL = "https://www.instagram.com/p/shared-post/"


def _poll(timestamp, *, text="", extra=None):
    return SimpleNamespace(
        platform_message_id="shared-1",
        sender_name="Customer",
        sender_id="customer",
        text=text,
        message_type="dm",
        timestamp=timestamp,
        extra=extra or {"conversation_id": "conversation", "sender_id": "customer"},
    )


def _webhook(timestamp, *, text="", echo=False):
    return {
        "sender": {"id": "customer"},
        "timestamp": int(timestamp.timestamp() * 1000),
        "message": {
            "mid": "shared-1",
            "text": text,
            "is_echo": echo,
            "attachments": [{"type": "ig_post", "payload": {"url": URL, "title": "Shared post"}}],
        },
    }


@pytest.mark.parametrize("first", ["poll", "webhook"])
@pytest.mark.parametrize("body", ["", "Here is the article"])
def test_both_arrival_orders_preserve_share_status_and_original_window(inbox_account, first, body):
    old = timezone.now() - timedelta(hours=25)
    with (
        patch("apps.inbox.tasks.InboxSyncEngine._notify_new_message") as notify,
        patch("apps.mcp.events.enqueue_inbox_event") as enqueue,
    ):
        if first == "poll":
            InboxSyncEngine()._upsert_message(inbox_account, _poll(old, text=body))
        else:
            _handle_facebook_messaging(inbox_account, _webhook(old, text=body))
        message = InboxMessage.objects.get()
        original_time = message.received_at
        message.status = "archived"
        message.save(update_fields=["status"])
        for _ in range(2):
            _handle_facebook_messaging(inbox_account, _webhook(timezone.now(), text=body))
            InboxSyncEngine()._upsert_message(inbox_account, _poll(timezone.now()))
        message.refresh_from_db()
        assert message.body == body
        assert message.content_type == ("mixed" if body else "attachment")
        assert message.attachments[0]["url"] == URL
        assert len(message.attachments) == 1
        assert message.status == "archived"
        assert message.received_at == original_time
        assert message.received_at < timezone.now() - timedelta(hours=24)
        assert InboxMessage.objects.count() == 1
        notify.assert_called_once()
        enqueue.assert_called_once()


def test_attachment_echo_never_creates_message_or_notification(inbox_account):
    with patch("apps.mcp.events.enqueue_inbox_event") as enqueue:
        _handle_facebook_messaging(inbox_account, _webhook(timezone.now(), echo=True))
    assert not InboxMessage.objects.exists()
    enqueue.assert_not_called()


def test_explicit_deleted_content_stays_unavailable_after_poll(inbox_account):
    now = timezone.now()
    with (
        patch("apps.inbox.tasks.InboxSyncEngine._notify_new_message"),
        patch("apps.mcp.events.enqueue_inbox_event") as enqueue,
    ):
        _handle_facebook_messaging(inbox_account, _webhook(now, text="Original"))
        deleted = _webhook(now)
        deleted["message"] = {"mid": "shared-1", "is_deleted": True}
        _handle_facebook_messaging(inbox_account, deleted)
        InboxSyncEngine()._upsert_message(inbox_account, _poll(now, text="Stale copy"))
    message = InboxMessage.objects.get()
    assert message.body == ""
    assert message.attachments == [] and message.content_status == "removed"
    enqueue.assert_called_once()


@pytest.mark.parametrize("source", ["poll", "webhook"])
@pytest.mark.parametrize("later", ["poll", "webhook"])
def test_unknown_deletion_is_silent_tombstone_and_cannot_resurrect(inbox_account, source, later):
    now = timezone.now()
    with (
        patch("apps.inbox.tasks.InboxSyncEngine._notify_new_message") as notify,
        patch("apps.mcp.events.enqueue_inbox_event") as enqueue,
    ):
        if source == "poll":
            InboxSyncEngine()._upsert_message(inbox_account, _poll(now, extra={"is_deleted": True}))
        else:
            data = _webhook(now)
            data["message"] = {"mid": "shared-1", "is_deleted": True}
            _handle_facebook_messaging(inbox_account, data)
        if later == "poll":
            InboxSyncEngine()._upsert_message(inbox_account, _poll(now, text="Delayed original"))
        else:
            _handle_facebook_messaging(inbox_account, _webhook(now, text="Delayed original"))
    message = InboxMessage.objects.get()
    assert message.status == "archived"
    assert message.received_at == UNKNOWN_MESSAGE_TIMESTAMP
    assert message.body == ""
    assert message.attachments == [] and message.content_status == "removed"
    notify.assert_not_called()
    enqueue.assert_not_called()
