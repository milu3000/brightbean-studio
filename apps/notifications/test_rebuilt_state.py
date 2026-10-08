"""New synthetic proofs for the rebuilt implementation, not restored old results."""

from datetime import timedelta
from importlib import import_module
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.apps import apps
from django.db import connection, transaction
from django.utils import timezone

from apps.inbox.models import ConversationMessage, InboxConversation, InboxMessage
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from .engine import notify
from .inbox import _record, on_canonical_content_restricted, sender_label
from .models import EventType, InboxNotificationEvent, Notification

pytestmark = pytest.mark.django_db


@pytest.fixture
def account(user, organization):
    workspace = Workspace.objects.create(name="Notification proofs", organization=organization)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    user.last_workspace_id = workspace.pk
    user.save(update_fields=["last_workspace_id"])
    return SocialAccount.objects.create(
        workspace=workspace, platform="facebook", account_platform_id="own-page", account_name="Page"
    )


def message(account, mid="m1", *, thread="thread", name="Lin", at=None, kind="dm", extra=None):
    return InboxMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform_message_id=mid,
        message_type=kind,
        sender_name=name,
        sender_handle="123456789",
        body=mid,
        received_at=at or timezone.now(),
        extra={"sender_id": "123456789", **({"conversation_id": thread} if thread else {}), **(extra or {})},
    )


def send(user, row):
    return notify(
        user,
        EventType.NEW_INBOX_MESSAGE,
        "New message",
        inbox_incoming=True,
        data={"message_id": str(row.pk), "workspace_id": str(row.workspace_id)},
    )


def snapshot(client, user):
    client.force_login(user)
    return client.get("/notifications/").context["notification_snapshot"]


def test_grouping_is_account_domain_and_thread_scoped(user, account):
    first = send(user, message(account))
    assert send(user, message(account, "m2")).pk == first.pk
    send(user, message(account, "m3", thread="other-thread"))
    other = SocialAccount.objects.create(
        workspace=account.workspace, platform="facebook", account_platform_id="other-page"
    )
    send(user, message(other))
    send(user, message(account, "comment", kind="comment"))
    assert Notification.objects.count() == 4
    first.refresh_from_db()
    assert first.revision == 2 and first.event_count == 2


def test_no_thread_never_merges_on_sender(user, account):
    send(user, message(account, thread=""))
    send(user, message(account, "m2", thread=""))
    assert Notification.objects.count() == 2


def test_foreign_custom_role_cannot_read_or_create_inbox_notifications(user, account):
    from apps.members.models import CustomRole
    from apps.organizations.models import Organization

    from .state import visible_notifications

    original = send(user, message(account))
    foreign = Organization.objects.create(name="Foreign organization")
    role = CustomRole.objects.create(organization=foreign, name="Foreign inbox role", permissions={"use_inbox": True})
    WorkspaceMembership.objects.filter(user=user, workspace=account.workspace).update(
        workspace_role="viewer", custom_role=role
    )
    assert not visible_notifications(user, account.workspace).filter(pk=original.pk).exists()
    assert send(user, message(account, "new")) is None
    assert Notification.objects.count() == 1


def test_event_retry_and_identity_move_do_not_create_a_new_card(user, account):
    row = message(account)
    original = send(user, row)
    send(user, row)
    row.extra["conversation_id"] = "identity-corrected-thread"
    row.save(update_fields=["extra"])
    assert send(user, row).pk == original.pk
    assert Notification.objects.count() == InboxNotificationEvent.objects.count() == 1
    original.refresh_from_db()
    assert original.revision == 1


@pytest.mark.parametrize("action", ["read", "dismiss"])
def test_stale_snapshot_does_not_consume_new_incoming(client, user, account, action):
    notification = send(user, message(account))
    old = snapshot(client, user)
    send(user, message(account, "m2"))
    assert client.post(f"/notifications/{notification.pk}/{action}/", {"snapshot": old}).status_code == 200
    notification.refresh_from_db()
    assert not notification.is_read and notification.dismissed_at is None


def test_bulk_snapshot_does_not_include_future_new_card(client, user, account):
    old = send(user, message(account))
    observed = snapshot(client, user)
    future = send(user, message(account, "future", thread="future"))
    client.post("/notifications/mark-all-read/", {"snapshot": observed})
    old.refresh_from_db()
    future.refresh_from_db()
    assert old.is_read and not future.is_read


def test_close_stays_closed_for_replay_and_old_delivery_but_new_incoming_reopens(client, user, account):
    row = message(account)
    notification = send(user, row)
    client.post(f"/notifications/{notification.pk}/dismiss/", {"snapshot": snapshot(client, user)})
    notification.refresh_from_db()
    closed = notification.dismissed_at
    assert closed
    send(user, row)
    send(user, message(account, "delayed", at=closed - timedelta(days=1)))
    notification.refresh_from_db()
    assert notification.dismissed_at == closed and notification.revision == 1
    send(user, message(account, "fresh", at=closed + timedelta(seconds=1)))
    notification.refresh_from_db()
    assert notification.dismissed_at is None and notification.revision == 2


def test_read_does_not_resolve_or_close_work(client, user, account):
    row = message(account)
    notification = send(user, row)
    client.post(f"/notifications/{notification.pk}/read/", {"snapshot": snapshot(client, user)})
    row.refresh_from_db()
    notification.refresh_from_db()
    assert row.status == "unread" and notification.is_read and notification.dismissed_at is None


def test_revoked_membership_and_foreign_scope_cannot_read(client, user, account, organization):
    notification = send(user, message(account))
    token = snapshot(client, user)
    WorkspaceMembership.objects.filter(user=user, workspace=account.workspace).delete()
    client.post("/notifications/mark-all-read/", {"snapshot": token})
    notification.refresh_from_db()
    assert not notification.is_read
    other = Workspace.objects.create(name="Other", organization=organization)
    WorkspaceMembership.objects.create(user=user, workspace=other, workspace_role="owner")
    user.last_workspace_id = other.pk
    user.save(update_fields=["last_workspace_id"])
    assert client.post("/notifications/mark-all-read/", {"snapshot": token}).status_code == 409


def test_bad_signed_snapshot_is_rejected(client, user, account):
    notification = send(user, message(account))
    token = snapshot(client, user)
    assert client.post("/notifications/mark-all-read/", {"snapshot": token + "wrong"}).status_code == 409
    notification.refresh_from_db()
    assert not notification.is_read


@pytest.mark.parametrize("field", ["is_echo", "is_deleted", "is_outgoing"])
def test_non_incoming_is_quiet(user, account, field):
    assert send(user, message(account, extra={field: True})) is None
    assert not Notification.objects.exists()


@pytest.mark.parametrize(
    "name,handle,expected",
    [("林小米", "123456789", "林小米"), ("123456789", "@lin", "@lin"), ("123456789", "123456789", "Facebook contact")],
)
def test_display_labels_are_not_raw_ids(name, handle, expected):
    assert sender_label(name, handle, sender_id="123456789", platform="facebook") == expected


def test_no_new_external_dispatch_and_assignment_is_not_incoming(user, account):
    row = message(account)
    with patch("apps.notifications.engine._dispatch") as dispatch:
        send(user, row)
        dispatch.assert_not_called()
    with patch("apps.notifications.inbox.notify_legacy_incoming") as incoming:
        notify(
            user,
            EventType.NEW_INBOX_MESSAGE,
            "You were assigned a message",
            data={"message_id": str(row.pk), "workspace_id": str(row.workspace_id)},
        )
        incoming.assert_not_called()


@pytest.fixture
def canonical(account):
    legacy = message(account)
    conversation = InboxConversation.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform="facebook",
        platform_conversation_id="thread",
        identity_kind="platform",
        conversation_type="direct",
    )
    row = ConversationMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform="facebook",
        conversation=conversation,
        platform_message_id=legacy.platform_message_id,
        direction="inbound",
        body="Canonical fresh body",
        legacy_message=legacy,
    )
    return conversation, row, legacy


@pytest.mark.parametrize("source", ["canonical", "legacy_fk", "legacy_json"])
def test_fresh_visibility_blocks_all_stored_preview_fallbacks(user, account, canonical, source):
    conversation, row, legacy = canonical
    fields = {"workspace": account.workspace, "data": {"workspace_id": str(account.workspace_id)}}
    if source == "canonical":
        fields["data"]["canonical_message_id"] = str(row.pk)
    elif source == "legacy_fk":
        fields["inbox_message"] = legacy
    else:
        fields["data"]["message_id"] = str(legacy.pk)
    notification = Notification.objects.create(
        user=user, event_type=EventType.NEW_INBOX_MESSAGE, title="New", body="Stale private body", **fields
    )
    with patch(
        "apps.notifications.content.canonical_content",
        return_value={"available": False, "body": "", "reason": "provenance_unverified"},
    ):
        assert notification.display_body == ""
    with patch(
        "apps.notifications.content.canonical_content", return_value={"available": True, "body": row.body, "reason": ""}
    ):
        assert notification.display_body == row.body


def test_transient_provenance_marker_is_not_sticky(user, account, canonical):
    conversation, row, legacy = canonical
    notification = Notification.objects.create(
        user=user,
        workspace=account.workspace,
        event_type=EventType.NEW_INBOX_MESSAGE,
        title="New",
        body="",
        data={"canonical_message_id": str(row.pk), "content_restriction": "provenance_unverified"},
    )
    with patch(
        "apps.notifications.content.canonical_content", return_value={"available": True, "body": row.body, "reason": ""}
    ):
        assert notification.display_body == row.body


@pytest.mark.parametrize("restriction", ["withdrawn", "expired"])
def test_real_restrictions_remain_sticky(user, account, canonical, restriction):
    conversation, row, legacy = canonical
    notification = Notification.objects.create(
        user=user,
        workspace=account.workspace,
        event_type=EventType.NEW_INBOX_MESSAGE,
        title="New",
        body="Stale",
        inbox_message=legacy,
        data={"content_restriction": restriction},
    )
    with patch(
        "apps.notifications.content.canonical_content", return_value={"available": True, "body": "Must not return"}
    ):
        assert notification.display_body == ""


def test_signal_before_provenance_insert_does_not_permanently_blank_preview(user, account, canonical):
    from .identity_v1 import canonical_identity, event_identity

    conversation, row, legacy = canonical
    conversation.incoming_generation = 1
    with (
        patch("apps.notifications.inbox.InboxConversation.objects.select_for_update") as locked,
        patch(
            "apps.notifications.content.canonical_content",
            return_value={"available": False, "body": "", "reason": "provenance_unverified"},
        ),
    ):
        locked.return_value.filter.return_value.first.return_value = conversation
        notification = _record(
            user=user,
            workspace=account.workspace,
            subject_key=canonical_identity(conversation),
            event_key=event_identity(account.pk, account.platform, row.platform_message_id),
            title="New",
            body=row.body,
            occurred_at=timezone.now(),
            data={"canonical_message_id": str(row.pk)},
            conversation=conversation,
            event_revision=1,
        )
    assert notification.body == "" and "content_restriction" not in notification.data
    with patch(
        "apps.notifications.content.canonical_content", return_value={"available": True, "body": row.body, "reason": ""}
    ):
        assert notification.display_body == row.body


@pytest.mark.parametrize("read_generation,event_generation,expected", [(1, 1, True), (1, 2, False)])
def test_delayed_notification_inherits_only_acknowledged_generation(
    user, account, canonical, read_generation, event_generation, expected
):
    from .identity_v1 import canonical_identity, event_identity

    conversation, row, legacy = canonical
    conversation.incoming_generation = event_generation
    with (
        patch("apps.notifications.inbox.InboxConversation.objects.select_for_update") as locked,
        patch("apps.notifications.inbox._read_watermark", return_value=(read_generation, timezone.now())),
    ):
        locked.return_value.filter.return_value.first.return_value = conversation
        notification = _record(
            user=user,
            workspace=account.workspace,
            subject_key=canonical_identity(conversation),
            event_key=event_identity(account.pk, account.platform, row.platform_message_id),
            title="New",
            body=row.body,
            occurred_at=timezone.now(),
            data={"canonical_message_id": str(row.pk)},
            conversation=conversation,
            event_revision=event_generation,
        )
    assert notification.is_read is expected and notification.dismissed_at is None


def test_content_restriction_preserves_lifecycle(user, account, canonical):
    conversation, row, legacy = canonical
    notification = Notification.objects.create(
        user=user,
        workspace=account.workspace,
        inbox_message=legacy,
        event_type=EventType.NEW_INBOX_MESSAGE,
        title="New",
        body="Stale",
        data={"message_id": str(legacy.pk)},
        revision=3,
        read_revision=2,
    )
    row.body = ""
    row.attachments = []
    row.save(update_fields=["body", "attachments"])
    assert on_canonical_content_restricted(None, message=row, reason="withdrawn") == 1
    notification.refresh_from_db()
    assert (
        notification.body == notification.display_body == ""
        and notification.revision == 3
        and notification.read_revision == 2
    )


def test_group_migration_preserves_originals(user, account):
    rows = [message(account, "a"), message(account, "b")]
    notices = [
        Notification.objects.create(
            user=user,
            event_type=EventType.NEW_INBOX_MESSAGE,
            title=f"Original{i}",
            body=f"Body{i}",
            is_read=bool(i),
            data={"message_id": str(row.pk), "workspace_id": str(row.workspace_id)},
        )
        for i, row in enumerate(rows)
    ]
    migration = import_module("apps.notifications.migrations.0007_group_existing_inbox_notifications")
    migration.forwards(apps, connection.schema_editor())
    assert Notification.objects.count() == 2 and Notification.objects.filter(superseded_by__isnull=True).count() == 1
    migration.backwards(apps, connection.schema_editor())
    for i, n in enumerate(notices):
        n.refresh_from_db()
        assert n.title == f"Original{i}" and n.body == f"Body{i}" and n.is_read is bool(i)


def test_rollback_keeps_row_and_notification_atomic(user, account):
    with pytest.raises(RuntimeError), transaction.atomic():
        send(user, message(account))
        raise RuntimeError("rollback")
    assert not Notification.objects.exists() and not InboxNotificationEvent.objects.exists()


def test_bad_canonical_id_never_falls_back(user, account, canonical):
    notification = Notification.objects.create(
        user=user,
        workspace=account.workspace,
        event_type=EventType.NEW_INBOX_MESSAGE,
        title="New",
        body="Private",
        data={"canonical_message_id": str(uuid4())},
    )
    assert notification.display_body == ""


@pytest.mark.django_db(transaction=True)
def test_postgres_duplicate_receipt_is_serialized(user, account):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks in combined CI")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    row = message(account)
    barrier = Barrier(2)

    def worker():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return send(user, row).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(lambda _: worker(), range(2))
    assert first == second
    assert Notification.objects.count() == InboxNotificationEvent.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_postgres_read_cas_waits_for_newer_revision(user, account):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks in combined CI")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import close_old_connections

    from .state import apply_snapshot

    notification = send(user, message(account))
    locked, release = Event(), Event()

    def incoming():
        close_old_connections()
        try:
            with transaction.atomic():
                Notification.objects.select_for_update().get(pk=notification.pk)
                Notification.objects.filter(pk=notification.pk).update(revision=2, is_read=False)
                locked.set()
                assert release.wait(timeout=10)
        finally:
            close_old_connections()

    def mark_read():
        close_old_connections()
        try:
            return apply_snapshot(Notification.objects.filter(user=user), [(notification.pk, 1)])
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(incoming)
        assert locked.wait(timeout=10)
        reading = pool.submit(mark_read)
        release.set()
        writing.result(timeout=10)
        assert reading.result(timeout=10) == 0
    notification.refresh_from_db()
    assert not notification.is_read


def test_long_provider_name_cannot_overflow_notification_title(user, account):
    notification = send(user, message(account, name="A" * 255))
    assert len(notification.title) <= 255
