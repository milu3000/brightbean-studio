"""Unresolved existing receipts cannot disappear through ordinary cascades."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection, transaction
from django.db.models.deletion import ProtectedError
from django.urls import reverse

from apps.inbox import receipt_retention
from apps.inbox.locking import lock_dm_account
from apps.inbox.models import DMSendControl, InboxMessage, InboxReply
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount


@pytest.mark.django_db
@pytest.mark.parametrize(
    "target", ["reply", "message", "account", "reply_queryset", "message_queryset", "account_queryset"]
)
@pytest.mark.parametrize("status", ["unknown", "failed"])
def test_unknown_reply_prevents_instance_and_queryset_cascades(inbox_message, target, status):
    inbox_message.message_type = "dm"
    inbox_message.save(update_fields=["message_type"])
    reply = InboxReply.objects.create(inbox_message=inbox_message, body="Synthetic uncertain reply", status=status)
    account = inbox_message.social_account
    objects = {
        "reply": reply,
        "message": inbox_message,
        "account": account,
        "reply_queryset": InboxReply.objects.filter(pk=reply.pk),
        "message_queryset": InboxMessage.objects.filter(pk=inbox_message.pk),
        "account_queryset": SocialAccount.objects.filter(pk=account.pk),
    }
    with pytest.raises(ProtectedError, match="unknown"), transaction.atomic():
        objects[target].delete()
    assert InboxReply.objects.filter(pk=reply.pk, status=status).exists()
    assert InboxMessage.objects.filter(pk=inbox_message.pk).exists()
    assert SocialAccount.objects.filter(pk=account.pk).exists()
    assert not DMSendControl.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["unknown", "failed"])
def test_unknown_disconnect_is_refused_before_platform_or_cleanup(inbox_message, user, org_owner, client, status):
    account = inbox_message.social_account
    inbox_message.message_type = "dm"
    inbox_message.save(update_fields=["message_type"])
    WorkspaceMembership.objects.create(user=user, workspace=account.workspace, workspace_role="owner")
    InboxReply.objects.create(inbox_message=inbox_message, body="Synthetic uncertain reply", status=status)
    client.force_login(user)
    with (
        patch("apps.social_accounts.views.unsubscribe_account_webhooks") as unsubscribe,
        patch("providers.get_provider") as provider,
    ):
        response = client.post(
            reverse(
                "social_accounts:disconnect", kwargs={"workspace_id": account.workspace_id, "account_id": account.pk}
            ),
            secure=True,
        )
    assert response.status_code == 409
    unsubscribe.assert_not_called()
    provider.assert_not_called()
    assert SocialAccount.objects.filter(pk=account.pk).exists()
    assert InboxReply.objects.filter(inbox_message=inbox_message, status=status).exists()


@pytest.mark.django_db
def test_known_not_sent_draft_keeps_existing_discard_behavior(inbox_message):
    inbox_message.message_type = "dm"
    inbox_message.save(update_fields=["message_type"])
    reply = InboxReply.objects.create(
        inbox_message=inbox_message,
        body="Synthetic draft",
        status="failed",
        send_error="Provider refused before accepting",
        not_sent_verified=True,
    )
    reply_id = reply.pk
    reply.delete()
    assert not InboxReply.objects.filter(pk=reply_id).exists()


@pytest.mark.django_db
def test_workspace_reassignment_does_not_make_an_unknown_receipt_erasable(inbox_message):
    from apps.workspaces.models import Workspace

    reply = InboxReply.objects.create(inbox_message=inbox_message, body="Synthetic uncertain reply", status="unknown")
    workspace = Workspace.objects.create(
        organization=inbox_message.workspace.organization, name="Other synthetic workspace"
    )
    SocialAccount.objects.filter(pk=inbox_message.social_account_id).update(workspace=workspace)
    with pytest.raises(ProtectedError), transaction.atomic():
        reply.delete()
    assert InboxReply.objects.filter(pk=reply.pk, status="unknown").exists()


@pytest.mark.django_db(transaction=True)
def test_postgres_deletion_rechecks_receipt_after_waiting_on_account(inbox_message):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks and separate committed connections")
    reply = InboxReply.objects.create(inbox_message=inbox_message, body="Synthetic draft")
    account = inbox_message.social_account
    marked, release, deleting, deleted = Event(), Event(), Event(), Event()
    original_lock = receipt_retention.lock_dm_account

    def marker():
        close_old_connections()
        try:
            with transaction.atomic():
                lock_dm_account(account.pk, account.workspace_id)
                InboxReply.objects.filter(pk=reply.pk).update(status="unknown")
                marked.set()
                assert release.wait(10)
        finally:
            close_old_connections()

    def deletion_lock(*args, **kwargs):
        deleting.set()
        return original_lock(*args, **kwargs)

    def delete():
        close_old_connections()
        try:
            with pytest.raises(ProtectedError):
                InboxReply.objects.get(pk=reply.pk).delete()
        finally:
            deleted.set()
            close_old_connections()

    with (
        patch.object(receipt_retention, "lock_dm_account", side_effect=deletion_lock),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        preparing = pool.submit(marker)
        try:
            assert marked.wait(10)
            removing = pool.submit(delete)
            assert deleting.wait(10)
            assert not deleted.wait(0.2)
        finally:
            release.set()
        preparing.result(timeout=15)
        removing.result(timeout=15)
    assert InboxReply.objects.filter(pk=reply.pk, status="unknown").exists()


def test_reply_admin_cannot_forge_receipt_or_intent_metadata():
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    from apps.inbox.admin import InboxReplyAdmin

    admin = InboxReplyAdmin(InboxReply, AdminSite())
    request = RequestFactory().get("/admin/inbox/inboxreply/")
    assert not admin.has_add_permission(request)
    readonly = set(admin.get_readonly_fields(request))
    assert {
        "inbox_message",
        "status",
        "send_error",
        "platform_reply_id",
        "sent_at",
        "follow_up_of",
        "is_follow_up",
        "not_sent_verified",
    } <= readonly
