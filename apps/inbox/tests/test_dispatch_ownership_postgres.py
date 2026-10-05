"""V2 dispatch races require PostgreSQL and independent committed connections.

These tests deliberately skip on SQLite. They synchronize at actual account
locks, the durable marker boundary, and entry into the synthetic provider.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, get_ident
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection

from apps.inbox import conversations, reply_coordination, services
from apps.inbox import dm_send_gate as gate
from apps.inbox import reply_dispatch as dispatch
from apps.inbox.models import ConversationWorkState, DMSendAttempt, InboxReply, SendOperation
from apps.inbox.services import create_reply_draft, send_reply_now
from apps.inbox.tests.test_dispatch_ownership import (
    REJECTED,
    assert_unknown,
    claim,
    deliver,
    identity,
    incoming,
    prepare,
    set_paused,
)
from apps.inbox.tests.test_dispatch_ownership import clock as clock  # noqa: F401
from apps.inbox.tests.test_dispatch_ownership import owned as owned  # noqa: F401

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


def backend_pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def test_two_invocations_same_owned_operation_only_one_provider_call(owned):
    operation = claim(owned)
    entered, release, waiting_at_lock, second_finished = Event(), Event(), Event(), Event()
    second_thread = []
    provider_connections = []
    original_lock = reply_coordination.lock_dm_account

    def lock(*args, **kwargs):
        if second_thread and get_ident() == second_thread[0]:
            waiting_at_lock.set()
        return original_lock(*args, **kwargs)

    def provider(*args, **kwargs):
        provider_connections.append(backend_pid())
        entered.set()
        assert release.wait(10)
        return "synthetic-owned-outbound"

    def second():
        second_thread.append(get_ident())
        try:
            return deliver(owned, operation)
        except REJECTED:
            return None
        finally:
            second_finished.set()

    with (
        patch.object(reply_coordination, "lock_dm_account", side_effect=lock),
        patch("apps.inbox.services._dispatch_to_platform", side_effect=provider) as send,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(run, deliver, owned, operation)
        try:
            assert entered.wait(10)
            assert backend_pid() != provider_connections[0]
            # Read through the main thread's distinct DB connection while the
            # sender holds its locks and has not returned from the provider.
            assert_unknown(SendOperation.objects.get(pk=operation.pk))
            other = pool.submit(run, second)
            assert waiting_at_lock.wait(10)
            assert not second_finished.wait(0.2)
        finally:
            release.set()
        first.result(timeout=15)
        other.result(timeout=15)
    assert send.call_count == 1
    assert DMSendAttempt.objects.count() == 1
    assert SendOperation.objects.get(pk=operation.pk).status == "confirmed"


def test_different_keys_same_input_cannot_reserve_or_deliver_twice(owned):
    start = Barrier(2)
    at_actual_lock = Barrier(2)
    original_lock = reply_coordination.lock_dm_account

    def lock(*args, **kwargs):
        # Both transactions have reached the production lock acquisition, not
        # merely started a worker. The account row serializes their decisions.
        at_actual_lock.wait(timeout=10)
        return original_lock(*args, **kwargs)

    def reserve(key):
        start.wait(timeout=10)
        try:
            return prepare(owned, body=f"Independent {key}", idempotency_key=key)
        except REJECTED:
            return None

    with (
        patch.object(reply_coordination, "lock_dm_account", side_effect=lock),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(run, reserve, "independent-key-1")
        second = pool.submit(run, reserve, "independent-key-2")
        results = [first.result(timeout=15), second.result(timeout=15)]
    winners = [result for result in results if result is not None]
    assert len(winners) == 1 and SendOperation.objects.count() == 1
    operation = claim(owned, winners[0])
    with patch("apps.inbox.services._dispatch_to_platform", return_value="synthetic-outbound") as provider:
        deliver(owned, operation)
        with pytest.raises(REJECTED):
            prepare(owned, idempotency_key="third-key-after-terminal-success")
    assert provider.call_count == 1 and DMSendAttempt.objects.count() == 1


def test_owner_pause_between_committed_marker_and_dispatch_stops_provider(owned):
    operation = claim(owned)
    marked, release = Event(), Event()
    original = gate._prepare_attempt

    def marker(*args, **kwargs):
        attempt = original(*args, **kwargs)
        marked.set()
        assert release.wait(10)
        return attempt

    with (
        patch.object(gate, "_prepare_attempt", side_effect=marker),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        sender = pool.submit(run, deliver, owned, operation)
        try:
            assert marked.wait(10)
            assert_unknown(SendOperation.objects.get(pk=operation.pk))
            assert set_paused(owned).paused
        finally:
            release.set()
        with pytest.raises(REJECTED):
            sender.result(timeout=15)
    provider.assert_not_called()
    operation.refresh_from_db()
    assert operation.status == "failed" and operation.attempt.outcome == "not_sent"
    assert operation.reply.status == "failed"


def test_pause_acknowledgment_waits_for_provider_transaction_to_commit(owned):
    operation = claim(owned)
    entered, release, waiting_at_lock, acknowledged = Event(), Event(), Event(), Event()
    pause_thread = []
    original_lock = reply_coordination.lock_dm_account

    def lock(*args, **kwargs):
        if pause_thread and get_ident() == pause_thread[0]:
            waiting_at_lock.set()
        return original_lock(*args, **kwargs)

    def provider(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return "synthetic-outbound"

    def pause_owner():
        pause_thread.append(get_ident())
        result = set_paused(owned)
        acknowledged.set()
        return result

    with (
        patch.object(reply_coordination, "lock_dm_account", side_effect=lock),
        patch("apps.inbox.services._dispatch_to_platform", side_effect=provider) as send,
        patch("apps.inbox.conversations.record_reply", return_value=None),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        sender = pool.submit(run, deliver, owned, operation)
        try:
            assert entered.wait(10)
            pauser = pool.submit(run, pause_owner)
            assert waiting_at_lock.wait(10)
            assert not acknowledged.wait(0.2)
        finally:
            release.set()
        sender.result(timeout=15)
        assert pauser.result(timeout=15).paused
    assert acknowledged.is_set() and send.call_count == 1
    assert SendOperation.objects.get(pk=operation.pk).status == "confirmed"
    owned.row = incoming(owned)
    with pytest.raises(REJECTED):
        prepare(owned)


def test_new_inbound_waits_for_provider_then_invalidates_next_generation(owned):
    operation = claim(owned)
    entered, release, waiting_at_lock, ingested = Event(), Event(), Event(), Event()
    inbound_thread = []
    original_lock = conversations.lock_dm_account

    def lock(*args, **kwargs):
        if inbound_thread and get_ident() == inbound_thread[0]:
            waiting_at_lock.set()
        return original_lock(*args, **kwargs)

    def provider(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return "synthetic-owned-outbound"

    def ingest():
        inbound_thread.append(get_ident())
        row = incoming(owned)
        ingested.set()
        return row

    with (
        patch.object(conversations, "lock_dm_account", side_effect=lock),
        patch("apps.inbox.services._dispatch_to_platform", side_effect=provider) as send,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        sender = pool.submit(run, deliver, owned, operation)
        try:
            assert entered.wait(10)
            ingestion = pool.submit(run, ingest)
            assert waiting_at_lock.wait(10)
            assert not ingested.wait(0.2)
        finally:
            release.set()
        sender.result(timeout=15)
        row = ingestion.result(timeout=15)
    state = ConversationWorkState.objects.get(conversation_id=operation.conversation_id)
    assert state.generation > operation.expected_generation
    assert state.latest_incoming_id == row.pk
    assert SendOperation.objects.get(pk=operation.pk).status == "confirmed"
    assert send.call_count == 1


def test_owner_enrollment_cannot_be_bypassed_by_legacy_sender_waiting_on_account(owned):
    owned.row = incoming(owned, peer="synthetic-not-yet-owned-peer")
    reply = create_reply_draft(message=owned.row.legacy_message, body="Synthetic legacy draft")
    enrollment_locked, release, sender_at_lock = Event(), Event(), Event()
    original_retire = dispatch._retire_work
    original_lock = services.lock_dm_account

    def retire(*args, **kwargs):
        result = original_retire(*args, **kwargs)
        enrollment_locked.set()
        assert release.wait(10)
        return result

    def lock(*args, **kwargs):
        sender_at_lock.set()
        return original_lock(*args, **kwargs)

    def enroll():
        return dispatch.enroll_conversation_owner(owned.scope, **identity(owned), authorization=owned.authorization)

    with (
        patch.object(dispatch, "_retire_work", side_effect=retire),
        patch.object(services, "lock_dm_account", side_effect=lock),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        enrollment = pool.submit(run, enroll)
        try:
            assert enrollment_locked.wait(10)
            sender = pool.submit(
                run, send_reply_now, reply, actor=owned.user, automated=True, authorization=owned.authorization
            )
            assert sender_at_lock.wait(10)
        finally:
            release.set()
        assert enrollment.result(timeout=15).paused
        with pytest.raises(REJECTED):
            sender.result(timeout=15)
    provider.assert_not_called()
    assert not DMSendAttempt.objects.exists()
    assert InboxReply.objects.get(pk=reply.pk).status == "draft"


def test_worker_crash_after_marker_remains_visible_to_another_connection(owned):
    operation = claim(owned)
    marked, release = Event(), Event()
    original = gate._prepare_attempt

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        marked.set()
        assert release.wait(10)
        raise SystemExit("synthetic crash outside transaction")

    with (
        patch.object(gate, "_prepare_attempt", side_effect=crash),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        sender = pool.submit(run, deliver, owned, operation)
        try:
            assert marked.wait(10)
            assert_unknown(SendOperation.objects.get(pk=operation.pk))
        finally:
            release.set()
        with pytest.raises(SystemExit):
            sender.result(timeout=15)
    provider.assert_not_called()
    assert_unknown(SendOperation.objects.get(pk=operation.pk))
    with patch("apps.inbox.services._dispatch_to_platform") as retry, pytest.raises(REJECTED):
        deliver(owned, operation)
    retry.assert_not_called()
