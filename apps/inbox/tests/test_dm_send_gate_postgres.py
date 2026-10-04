"""Cross-connection locking evidence. SQLite never counts as a race pass."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection, transaction

from apps.inbox import dm_send_gate as gate
from apps.inbox.models import DMSendAttempt, InboxMessage, InboxReply
from apps.inbox.services import ReplyStateError, create_reply_draft, send_reply_now
from apps.inbox.tests.test_dm_send_gate import draft_for, message_for, pause, send
from apps.inbox.tests.test_dm_send_gate import enrolled as enrolled  # noqa: F401
from apps.inbox.tests.test_ingestion_events import _messaging
from apps.inbox.webhooks import _handle_facebook_messaging
from apps.social_accounts.models import SocialAccount

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def postgres_only():
    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL row locks and separate committed connections")


def run(function, *args, **kwargs):
    close_old_connections()
    try:
        return function(*args, **kwargs)
    finally:
        close_old_connections()


def test_two_senders_same_reply_only_one_provider_call(enrolled):
    reply = draft_for(enrolled)
    calling, release, second_started = Event(), Event(), Event()

    def provider(*args, **kwargs):
        calling.set()
        assert release.wait(10)
        return "synthetic-outbound"

    def second():
        copy = InboxReply.objects.select_related("inbox_message__social_account").get(pk=reply.pk)
        second_started.set()
        with pytest.raises(ReplyStateError):
            send(enrolled, copy)

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=provider) as dispatch,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(run, send, enrolled, reply)
        try:
            assert calling.wait(10)
            # A separate connection can see the truly committed marker while
            # the send transaction is still holding its account/reply locks.
            assert DMSendAttempt.objects.get().outcome == "unknown"
            other = pool.submit(run, second)
            assert second_started.wait(10)
        finally:
            release.set()
        first.result(timeout=15)
        other.result(timeout=15)
    assert dispatch.call_count == 1


def test_pause_ack_waits_for_send_commit_and_blocks_new_dispatch(enrolled):
    reply = draft_for(enrolled)
    calling, release, pause_started, acknowledged = Event(), Event(), Event(), Event()

    def provider(*args, **kwargs):
        calling.set()
        assert release.wait(10)
        return "synthetic-outbound"

    def pausing():
        pause_started.set()
        result = pause(enrolled)
        acknowledged.set()
        return result

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=provider) as dispatch,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(run, send, enrolled, reply)
        try:
            assert calling.wait(10)
            pauser = pool.submit(run, pausing)
            assert pause_started.wait(10)
            assert not acknowledged.wait(0.2)
        finally:
            release.set()
        first.result(timeout=15)
        assert pauser.result(timeout=15).paused
    assert acknowledged.is_set()
    assert InboxReply.objects.get(pk=reply.pk).status == "sent"
    with (
        patch("apps.inbox.services._dispatch_to_platform") as later,
        pytest.raises(gate.DMSendGateError, match="paused"),
    ):
        send(enrolled, draft_for(enrolled))
    later.assert_not_called()
    assert dispatch.call_count == 1


def test_pause_between_marker_and_dispatch_invalidates_original_invocation(enrolled):
    reply = draft_for(enrolled)
    marked, release = Event(), Event()
    original = gate._prepare_attempt

    def mark(*args):
        attempt = original(*args)
        marked.set()
        assert release.wait(10)
        return attempt

    with (
        patch.object(gate, "_prepare_attempt", side_effect=mark),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        sending = pool.submit(run, send, enrolled, reply)
        try:
            assert marked.wait(10)
            assert DMSendAttempt.objects.get().outcome == "unknown"
            assert pause(enrolled).paused
        finally:
            release.set()
        with pytest.raises(gate.DMSendGateError, match="paused"):
            sending.result(timeout=15)
    provider.assert_not_called()
    assert DMSendAttempt.objects.get().outcome == "not_sent"


def test_initial_enrollment_while_legacy_sender_waits_is_not_bypassed(inbox_account, user):
    from apps.inbox import services

    reply = create_reply_draft(message=message_for(inbox_account), body="Synthetic answer")
    held, allow_enrollment, at_account_lock = Event(), Event(), Event()
    original_lock = services.lock_dm_account

    def sender_lock(*args):
        # Signal after any incorrect pre-lock enrollment lookup would have
        # happened, immediately before entering the actual contested lock.
        at_account_lock.set()
        return original_lock(*args)

    def enroller():
        with transaction.atomic():
            SocialAccount.objects.select_for_update().get(pk=inbox_account.pk)
            held.set()
            assert allow_enrollment.wait(10)
            # Enrollment service demands outermost commit; emulate its locked
            # insert inside this deliberate contention transaction.
            from django.utils import timezone

            from apps.inbox.models import DMSendControl

            DMSendControl.objects.create(
                social_account=inbox_account,
                workspace=inbox_account.workspace,
                platform=inbox_account.platform,
                account_platform_id=inbox_account.account_platform_id,
                coverage_from=timezone.now(),
                coverage_version=gate.COVERAGE_VERSION,
            )

    def sender():
        return send_reply_now(reply, actor=user, authorization=gate.session_send_authorization(user))

    with (
        patch("apps.inbox.services.lock_dm_account", side_effect=sender_lock),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        enrolling = pool.submit(run, enroller)
        try:
            assert held.wait(10)
            sending = pool.submit(run, sender)
            assert at_account_lock.wait(10)
        finally:
            allow_enrollment.set()
        enrolling.result(timeout=15)
        with pytest.raises(gate.DMSendGateError, match="paused"):
            sending.result(timeout=15)
    provider.assert_not_called()


def test_unmarked_echo_waits_for_committed_outbound_id(enrolled):
    reply = draft_for(enrolled)
    calling, release, arriving, ingested = Event(), Event(), Event(), Event()

    def provider(*args, **kwargs):
        calling.set()
        assert release.wait(10)
        return SimpleNamespace(platform_message_id="synthetic-outbound")

    def echo():
        arriving.set()
        _handle_facebook_messaging(enrolled.account, _messaging(mid="synthetic-outbound", sender="synthetic-peer"))
        ingested.set()

    with patch("apps.inbox.services.get_provider") as provider_factory, ThreadPoolExecutor(max_workers=2) as pool:
        provider_factory.return_value.reply_to_message.side_effect = provider
        sender = pool.submit(run, send, enrolled, reply)
        try:
            assert calling.wait(10)
            ingestion = pool.submit(run, echo)
            assert arriving.wait(10)
            assert not ingested.wait(0.2)
        finally:
            release.set()
        sender.result(timeout=15)
        ingestion.result(timeout=15)
    assert InboxMessage.objects.count() == 1
    assert InboxReply.objects.get(pk=reply.pk).platform_reply_id == "synthetic-outbound"
