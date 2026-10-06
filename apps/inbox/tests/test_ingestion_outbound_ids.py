"""Known outbound IDs and duplicate timestamps never create fresh inbound work."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.inbox.models import InboxMessage, InboxReply
from apps.inbox.tasks import UNKNOWN_MESSAGE_TIMESTAMP, InboxSyncEngine, _safe_message_timestamp
from apps.inbox.tests.test_ingestion_events import _messaging, _polled
from apps.inbox.tests.test_ingestion_events import subscription as event_subscription  # noqa: F401
from apps.inbox.webhooks import _handle_facebook_messaging
from apps.mcp.models import EventOutbox
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("event_subscription")]


def _sent_reply(account, outbound_id="outbound-1"):
    message = InboxMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform_message_id="original-inbound",
        message_type="dm",
        sender_name="Customer",
        sender_handle="customer-1",
        body="Question",
        received_at=timezone.now() - timedelta(minutes=10),
    )
    return InboxReply.objects.create(
        inbox_message=message,
        body="Sent reply",
        platform_reply_id=outbound_id,
        status=InboxReply.Status.SENT,
        sent_at=timezone.now(),
    )


def _ingest(account, source, **kwargs):
    if source == "webhook":
        _handle_facebook_messaging(account, _messaging(mid="outbound-1", **kwargs))
    else:
        InboxSyncEngine()._upsert_message(account, _polled(mid="outbound-1", **kwargs))


@pytest.mark.parametrize("source", ["webhook", "poll"])
def test_known_outbound_without_echo_flags_is_silent(inbox_account, source):
    _sent_reply(inbox_account)
    with patch.object(InboxSyncEngine, "_notify_new_message") as notify:
        _ingest(inbox_account, source, sender="unrecognized-provider-sender")
    assert InboxMessage.objects.count() == 1
    assert not EventOutbox.objects.exists()
    notify.assert_not_called()


@pytest.mark.parametrize("source", ["webhook", "poll"])
@pytest.mark.parametrize("foreign_workspace", [False, True])
def test_same_outbound_id_on_other_account_does_not_hide_inbound(inbox_account, source, foreign_workspace):
    workspace = inbox_account.workspace
    if foreign_workspace:
        workspace = Workspace.objects.create(name="Foreign", organization=workspace.organization)
    other = SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="other-account",
        account_name="Other",
    )
    _sent_reply(other)
    _ingest(inbox_account, source)
    assert InboxMessage.objects.filter(social_account=inbox_account, platform_message_id="outbound-1").exists()
    assert EventOutbox.objects.count() == 1


@pytest.mark.parametrize("first_source", ["webhook", "poll"])
def test_duplicate_poll_preserves_first_inbound_timestamp(inbox_account, first_source):
    original = timezone.now() - timedelta(hours=30)
    if first_source == "webhook":
        _ingest(inbox_account, first_source, timestamp=int(original.timestamp() * 1000))
    else:
        _ingest(inbox_account, first_source, timestamp=original)
    stored = InboxMessage.objects.get().received_at
    _ingest(inbox_account, "poll", timestamp=timezone.now())
    _ingest(inbox_account, "poll", timestamp=None)
    assert InboxMessage.objects.get().received_at == stored
    assert not EventOutbox.objects.exists()


def test_naive_provider_timestamp_is_unknown():
    naive = timezone.now().replace(tzinfo=None)
    assert _safe_message_timestamp(naive) == UNKNOWN_MESSAGE_TIMESTAMP


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("source", ["webhook", "poll"])
def test_echo_before_send_response_waits_for_persisted_outbound_id(inbox_account, source, user):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import close_old_connections, connection

    from apps.inbox.dm_send_gate import session_send_authorization
    from apps.inbox.services import send_reply_now
    from apps.members.models import WorkspaceMembership

    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row-level locks")
    reply = _sent_reply(inbox_account)
    WorkspaceMembership.objects.update_or_create(
        user=user, workspace=inbox_account.workspace, defaults={"workspace_role": "owner"}
    )
    message = reply.inbox_message
    message.extra = {"conversation_type": "direct", "classification_reason": "participants_pair"}
    message.save(update_fields=["extra"])
    reply.status = InboxReply.Status.DRAFT
    reply.platform_reply_id = ""
    reply.save(update_fields=["status", "platform_reply_id"])
    sending, release, arriving, ingested = Event(), Event(), Event(), Event()

    def provider_send(*_args, **_kwargs):
        sending.set()
        assert release.wait(10)
        return SimpleNamespace(platform_message_id="outbound-1")

    def ingest():
        arriving.set()
        _ingest(inbox_account, source, sender="unrecognized-provider-sender")
        ingested.set()

    def run(function, *args, **kwargs):
        close_old_connections()
        try:
            return function(*args, **kwargs)
        finally:
            close_old_connections()

    with patch("apps.inbox.services.get_provider") as provider, ThreadPoolExecutor(max_workers=2) as pool:
        provider.return_value.reply_to_message.side_effect = provider_send
        sending_job = pool.submit(
            run, send_reply_now, reply, actor=user, automated=True, authorization=session_send_authorization(user)
        )
        try:
            assert sending.wait(10)
            ingestion_job = pool.submit(run, ingest)
            assert arriving.wait(10)
            assert not ingested.wait(0.2)
        finally:
            release.set()
        sending_job.result(timeout=15)
        ingestion_job.result(timeout=15)
    assert ingested.is_set()
    reply.refresh_from_db()
    assert reply.status == "sent" and reply.platform_reply_id == "outbound-1"
    assert InboxMessage.objects.count() == 1
    assert not EventOutbox.objects.exists()
