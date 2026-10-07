"""Inbound DM invariants across webhook/poll ingestion and the MCP outbox."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.api_keys.models import ApiKey
from apps.inbox.models import InboxMessage
from apps.inbox.tasks import UNKNOWN_MESSAGE_TIMESTAMP, InboxSyncEngine
from apps.inbox.webhooks import _create_if_new, _handle_facebook_messaging
from apps.mcp.events import EVENT_NAME, enqueue_inbox_event
from apps.mcp.models import EventOutbox, EventSubscription
from apps.members.models import WorkspaceMembership
from apps.notifications.models import Channel, DeliveryStatus, EventType, Notification

pytestmark = pytest.mark.django_db


def _messaging(*, mid="dm-1", timestamp=None, sender="customer-1", message=None):
    if timestamp is None:
        timestamp = int(timezone.now().timestamp() * 1000)
    return {
        "sender": {"id": sender, "name": "Customer"},
        "recipient": {"id": "page-1"},
        "timestamp": timestamp,
        "message": {"mid": mid, "text": "Hello", **(message or {})},
    }


def _polled(*, mid="dm-1", sender="customer-1", timestamp=None, extra=None, message_type="dm"):
    return SimpleNamespace(
        platform_message_id=mid,
        sender_id=sender,
        sender_name="Customer",
        text="Hello",
        timestamp=timestamp if timestamp is not None else timezone.now(),
        extra=extra or {},
        message_type=message_type,
    )


@pytest.fixture
def subscription(settings, user, inbox_account):
    """Persist a local-only authorized subscription without contacting a callback."""
    settings.MCP_EVENTS_ENABLED = True
    WorkspaceMembership.objects.create(
        user=user, workspace=inbox_account.workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    key = ApiKey.objects.create(
        workspace=inbox_account.workspace,
        issued_by=user,
        name="Test event consumer",
        lookup_prefix="event-test-key",
        token_hash="unused-test-hash",
        permissions=["use_inbox"],
    )
    key.social_accounts.add(inbox_account)
    return EventSubscription.objects.create(
        id="sub_test_ingestion",
        principal=f"key:{key.pk}:{key.workspace_id}",
        owner=user,
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        api_key=key,
        name=EVENT_NAME,
        arguments={"social_account_id": str(inbox_account.pk)},
        callback_url="https://callback.example.test/events",
        callback_hash="test-callback-hash",
        signing_secret="test-only-never-sent-signing-secret",
        started_at=timezone.now() - timedelta(minutes=30),
        expires_at=timezone.now() + timedelta(hours=1),
    )


@pytest.mark.parametrize("platform", ["facebook", "instagram", "instagram_login"])
@pytest.mark.parametrize("own_sender", ["page-1", "linked-page"])
def test_webhook_skips_own_sender_without_echo_flag(inbox_account, platform, own_sender):
    inbox_account.platform = platform
    inbox_account.webhook_target_id = "linked-page"
    with patch("apps.mcp.events.enqueue_inbox_event") as enqueue:
        _handle_facebook_messaging(inbox_account, _messaging(sender=own_sender))
    assert not InboxMessage.objects.exists()
    enqueue.assert_not_called()


def test_webhook_skips_message_echo_even_for_unrecognized_sender(inbox_account):
    with patch("apps.mcp.events.enqueue_inbox_event") as enqueue:
        _handle_facebook_messaging(inbox_account, _messaging(message={"is_echo": True}))
    assert not InboxMessage.objects.exists()
    enqueue.assert_not_called()


def test_webhook_creation_helper_defends_against_echo(inbox_account):
    with patch("apps.mcp.events.enqueue_inbox_event") as enqueue:
        _create_if_new(
            inbox_account, "dm-echo", "dm", "Customer", "customer-1", "Hello", {"message": {"is_echo": True}}
        )
    assert not InboxMessage.objects.exists()
    enqueue.assert_not_called()


def test_attachment_only_webhook_remains_an_inbound_message(inbox_account, subscription):
    _handle_facebook_messaging(
        inbox_account,
        _messaging(message={"text": None, "attachments": [{"type": "image", "payload": {"url": "opaque"}}]}),
    )
    assert InboxMessage.objects.get().body == ""
    assert EventOutbox.objects.count() == 1


@pytest.mark.parametrize("sender", ["page-1", "linked-page", 123])
def test_poll_skips_own_sender(inbox_account, sender):
    inbox_account.webhook_target_id = "linked-page"
    if sender == 123:
        inbox_account.account_platform_id = "123"
    inbox_account.save(update_fields=["webhook_target_id", "account_platform_id"])
    with patch("apps.mcp.events.enqueue_inbox_event") as enqueue:
        InboxSyncEngine()._upsert_message(inbox_account, _polled(sender=sender))
    assert not InboxMessage.objects.exists()
    enqueue.assert_not_called()


@pytest.mark.parametrize(
    "extra",
    [
        {"is_echo": True},
        {"message": {"is_echo": True}},
        {"is_self": True},
        {"direction": "outbound"},
        {"sender_id": "page-1"},
        {"sender": {"id": "page-1"}},
    ],
)
def test_poll_skips_provider_outgoing_markers(inbox_account, extra):
    with patch("apps.mcp.events.enqueue_inbox_event") as enqueue:
        InboxSyncEngine()._upsert_message(inbox_account, _polled(extra=extra))
    assert not InboxMessage.objects.exists()
    enqueue.assert_not_called()


@pytest.mark.parametrize("as_string", [False, True])
def test_webhook_preserves_meta_milliseconds(inbox_account, as_string):
    expected = datetime(2024, 4, 7, 12, 34, 56, 789000, tzinfo=UTC)
    timestamp = int(expected.timestamp() * 1000)
    _handle_facebook_messaging(inbox_account, _messaging(timestamp=str(timestamp) if as_string else timestamp))
    message = InboxMessage.objects.get()
    assert message.received_at == expected
    assert timezone.now() - message.received_at > timedelta(hours=24)


@pytest.mark.parametrize(
    "bad_timestamp",
    [None, "", "yesterday", "NaN", "Infinity", True, False, {}, [], 10**100, -(10**100)],
)
def test_invalid_webhook_timestamp_never_becomes_fresh(inbox_account, subscription, bad_timestamp):
    payload = _messaging()
    payload["timestamp"] = bad_timestamp
    _handle_facebook_messaging(inbox_account, payload)
    assert InboxMessage.objects.get().received_at == UNKNOWN_MESSAGE_TIMESTAMP
    assert not EventOutbox.objects.exists()


def test_missing_webhook_timestamp_never_becomes_fresh(inbox_account, subscription):
    payload = _messaging()
    del payload["timestamp"]
    _handle_facebook_messaging(inbox_account, payload)
    assert InboxMessage.objects.get().received_at == UNKNOWN_MESSAGE_TIMESTAMP
    assert not EventOutbox.objects.exists()


def test_future_webhook_timestamp_cannot_extend_reply_window(inbox_account, subscription):
    future_ms = int((timezone.now() + timedelta(days=30)).timestamp() * 1000)
    _handle_facebook_messaging(inbox_account, _messaging(timestamp=future_ms))
    assert InboxMessage.objects.get().received_at == UNKNOWN_MESSAGE_TIMESTAMP
    assert not EventOutbox.objects.exists()


def test_old_webhook_retains_original_time_without_replaying_history(inbox_account, subscription):
    old = timezone.now() - timedelta(days=3)
    _handle_facebook_messaging(inbox_account, _messaging(timestamp=int(old.timestamp() * 1000)))
    assert abs(InboxMessage.objects.get().received_at - old) < timedelta(milliseconds=1)
    assert not EventOutbox.objects.exists()


@pytest.mark.parametrize("source", ["webhook", "poll"])
def test_new_dm_enqueues_once_and_duplicate_is_silent(inbox_account, subscription, source):
    with patch("apps.mcp.events.enqueue_inbox_event", wraps=enqueue_inbox_event) as enqueue:
        for _ in range(2):
            _ingest(inbox_account, source)
    assert InboxMessage.objects.count() == 1
    assert EventOutbox.objects.count() == 1
    enqueue.assert_called_once()
    delivery = EventOutbox.objects.get()
    payload = json.loads(delivery.payload)
    assert payload["eventId"] == EventOutbox.objects.get().event_id
    assert payload["data"]["message_id"] == str(InboxMessage.objects.get().pk)
    assert payload["data"]["message_id"] == str(InboxMessage.objects.get().pk)


def test_webhook_then_poll_same_provider_id_is_one_event(inbox_account, subscription):
    _handle_facebook_messaging(inbox_account, _messaging(mid=123))
    InboxSyncEngine()._upsert_message(inbox_account, _polled(mid="123"))
    assert InboxMessage.objects.count() == 1
    assert EventOutbox.objects.count() == 1


@pytest.mark.parametrize("source", ["webhook", "poll"])
def test_no_stable_provider_id_is_ignored(inbox_account, source):
    with patch("apps.mcp.events.enqueue_inbox_event") as enqueue:
        if source == "webhook":
            _handle_facebook_messaging(inbox_account, _messaging(mid=""))
        else:
            InboxSyncEngine()._upsert_message(inbox_account, _polled(mid=""))
    assert not InboxMessage.objects.exists()
    enqueue.assert_not_called()


@pytest.mark.parametrize("source", ["webhook", "poll"])
def test_non_dm_does_not_emit_dm_event(inbox_account, subscription, source):
    with patch("apps.mcp.events.enqueue_inbox_event") as enqueue:
        if source == "webhook":
            _create_if_new(inbox_account, "comment-1", "comment", "Customer", "customer-1", "Hello", {})
        else:
            InboxSyncEngine()._upsert_message(inbox_account, _polled(message_type="comment"))
    assert InboxMessage.objects.get().message_type == "comment"
    enqueue.assert_not_called()


@pytest.mark.parametrize("source", ["webhook", "poll"])
def test_explicit_backfill_never_enqueues(inbox_account, subscription, source):
    with (
        patch("apps.mcp.events.enqueue_inbox_event") as enqueue,
        patch.object(InboxSyncEngine, "_notify_new_message") as notify,
    ):
        if source == "webhook":
            _create_if_new(
                inbox_account,
                "dm-history",
                "dm",
                "Customer",
                "customer-1",
                "Hello",
                {},
                received_at=timezone.now(),
                notify=False,
            )
        else:
            InboxSyncEngine()._upsert_message(inbox_account, _polled(), notify=False)
    assert InboxMessage.objects.count() == 1
    enqueue.assert_not_called()
    notify.assert_not_called()
    assert not EventOutbox.objects.exists()


def test_first_poll_backlog_is_silent_but_new_message_enqueues(inbox_account, subscription):
    with patch("apps.inbox.tasks.get_provider") as get_provider:
        get_provider.return_value.get_messages.return_value = [
            _polled(mid="old", timestamp=timezone.now() - timedelta(days=3)),
            _polled(mid="fresh"),
        ]
        InboxSyncEngine()._sync_account(inbox_account)
    assert InboxMessage.objects.count() == 2
    assert EventOutbox.objects.get().message.platform_message_id == "fresh"


def test_invalid_poll_timestamp_has_safe_fallback(inbox_account, subscription):
    message = _polled()
    message.timestamp = None
    InboxSyncEngine()._upsert_message(inbox_account, message)
    assert InboxMessage.objects.get().received_at == UNKNOWN_MESSAGE_TIMESTAMP
    assert not EventOutbox.objects.exists()


def _ingest(account, source):
    if source == "webhook":
        _handle_facebook_messaging(account, _messaging())
    else:
        InboxSyncEngine()._upsert_message(account, _polled())


@pytest.mark.parametrize("source", ["webhook", "poll"])
def test_enqueue_failure_rolls_back_ingestion_and_retry_recovers(inbox_account, subscription, source):
    def fail_after_enqueue(message):
        assert connection.in_atomic_block
        enqueue_inbox_event(message)
        assert EventOutbox.objects.count() == 1
        raise RuntimeError("simulated enqueue failure")

    with (
        patch("apps.mcp.events.enqueue_inbox_event", side_effect=fail_after_enqueue),
        pytest.raises(RuntimeError, match="simulated enqueue failure"),
    ):
        _ingest(inbox_account, source)
    assert not InboxMessage.objects.exists()
    assert not EventOutbox.objects.exists()
    assert not Notification.objects.exists()

    _ingest(inbox_account, source)
    assert InboxMessage.objects.count() == 1
    assert EventOutbox.objects.count() == 1


@pytest.mark.parametrize("source", ["webhook", "poll"])
def test_outer_transaction_rollback_removes_inbox_outbox_and_schedule(
    inbox_account,
    subscription,
    source,
    django_capture_on_commit_callbacks,
):
    with patch("apps.mcp.events._queue_outbox") as schedule:
        with (
            django_capture_on_commit_callbacks(execute=True),
            pytest.raises(RuntimeError, match="abort ingestion"),
            transaction.atomic(),
        ):
            _ingest(inbox_account, source)
            assert InboxMessage.objects.count() == 1
            assert EventOutbox.objects.count() == 1
            raise RuntimeError("abort ingestion")
        schedule.assert_not_called()
    assert not InboxMessage.objects.exists()
    assert not EventOutbox.objects.exists()
    assert not Notification.objects.exists()


@pytest.mark.parametrize("source", ["webhook", "poll"])
def test_outbox_scheduling_waits_for_successful_commit(
    inbox_account,
    subscription,
    source,
    django_capture_on_commit_callbacks,
):
    with patch("apps.mcp.events._queue_outbox") as schedule:
        with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
            _ingest(inbox_account, source)
            assert EventOutbox.objects.count() == 1
            schedule.assert_not_called()
        schedule.assert_called_once_with([str(EventOutbox.objects.get().pk)])


@pytest.mark.parametrize("source", ["webhook", "poll"])
def test_existing_new_inbox_notification_still_delivered_once(inbox_account, subscription, user, source):
    _ingest(inbox_account, source)
    _ingest(inbox_account, source)
    notification = Notification.objects.get(user=user, event_type=EventType.NEW_INBOX_MESSAGE)
    message = InboxMessage.objects.get()
    assert notification.data == {
        "message_id": str(message.pk),
        "workspace_id": str(inbox_account.workspace_id),
    }
    assert notification.body == "Hello"
    delivery = notification.deliveries.get()
    assert delivery.channel == Channel.IN_APP
    assert delivery.status == DeliveryStatus.DELIVERED
    assert EventOutbox.objects.count() == 1
