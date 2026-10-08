"""Synthetic dispatcher experiment. SQLite covers sequential behavior ONLY.

Every test uses real commits, never Django TestCase transaction wrapping.
PostgreSQL-only tests exercise independent connections, locks and concurrent
ingestion/pause/recovery. No test contacts a provider or authorizes live sends.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from apps.inbox import reply_coordination as coordinator
from apps.inbox.conversations import upsert_conversation_message
from apps.inbox.models import (
    ConversationMessage,
    ConversationSyncState,
    ConversationWorkState,
    InboxConversation,
    InboxReply,
    SendOperation,
)
from apps.inbox.tests.support.synthetic_dispatch import (
    ManualClock,
    ScriptedTransport,
    SyntheticCrashError,
    SyntheticDispatcher,
    SyntheticFault,
    SyntheticGate,
    SyntheticOutcome,
)
from apps.mcp.models import EventOutbox
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db(transaction=True)
COMPLETE = SyntheticGate.COMPLETE


@pytest.fixture(autouse=True)
def synthetic_only(settings, inbox_account, enroll_conversation_accounts, monkeypatch):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    settings.INBOX_REPLY_COORDINATION_ENABLED = True
    enroll_conversation_accounts(inbox_account)

    def forbidden(*args, **kwargs):
        pytest.fail("synthetic dispatcher attempted provider/network access")

    monkeypatch.setattr("providers.get_provider", forbidden)
    monkeypatch.setattr("httpx.Client.send", forbidden)
    monkeypatch.setattr("httpx.AsyncClient.send", forbidden)


@pytest.fixture
def clock():
    return ManualClock(timezone.now())


@pytest.fixture
def actor(inbox_account):
    return coordinator.ReplyActorScope(
        "user:synthetic-dispatch-operator", inbox_account.workspace_id, frozenset({inbox_account.pk}), True
    )


def observe(account, clock, *, mid="synthetic-incoming", outbound=False, peer="synthetic-peer", **kwargs):
    extra = {"message_recipient_id": peer if outbound else account.account_platform_id}
    extra.update(kwargs.pop("extra", {}))
    with patch("django.utils.timezone.now", return_value=clock()):
        return upsert_conversation_message(
            account,
            platform_message_id=mid,
            sender_id=account.account_platform_id if outbound else peer,
            body=kwargs.pop("body", "Synthetic answer" if outbound else "Synthetic question"),
            extra=extra,
            occurred_at=kwargs.pop("occurred_at", clock()),
            source=kwargs.pop("source", "webhook"),
            **kwargs,
        )


def prepare(actor, row, **overrides):
    conversation = InboxConversation.objects.get(pk=row.conversation_id)
    state = ConversationWorkState.objects.get(conversation=conversation)
    args = {
        "conversation_id": conversation.pk,
        "social_account_id": conversation.social_account_id,
        "platform": conversation.platform,
        "expected_revision": conversation.revision,
        "expected_generation": state.generation,
        "target_message_id": row.pk,
        "body": "Synthetic combined answer",
        "idempotency_key": "synthetic-stable-operation",
    }
    args.update(overrides)
    return coordinator.prepare_reply(actor, **args)


def pause(actor, row, *, paused=True):
    conversation = InboxConversation.objects.get(pk=row.conversation_id)
    state = ConversationWorkState.objects.get(conversation=conversation)
    return coordinator.set_owner_paused(
        actor,
        conversation_id=conversation.pk,
        social_account_id=conversation.social_account_id,
        platform=conversation.platform,
        paused=paused,
        expected_revision=conversation.revision,
        expected_generation=state.generation,
    )


@pytest.fixture
def ready(inbox_account, actor, clock):
    row = observe(inbox_account, clock, extra={"conversation_id": "synthetic-thread"})
    operation = prepare(actor, row)
    clock.instant = ConversationWorkState.objects.get(conversation_id=row.conversation_id).due_at
    operation = coordinator.claim_reply(actor, operation_id=operation.pk, now=clock())
    return row, operation


def begin(harness, actor, operation, **overrides):
    return harness.begin_attempt(
        actor,
        **{
            "operation_id": operation.pk,
            "claim_token": operation.claim_token,
            "fencing_token": operation.fencing_token,
            "gate": COMPLETE,
            **overrides,
        },
    )


def assert_unknown(operation):
    operation.refresh_from_db()
    assert operation.status == "outcome_unknown"
    assert operation.external_attempted_at is not None
    state = ConversationWorkState.objects.get(conversation_id=operation.conversation_id)
    assert state.active_operation_id == operation.pk


def invalidate(kind, inbox_account, actor, clock, row):
    if kind == "pause":
        pause(actor, row)
    elif kind == "incoming":
        clock.advance(1)
        observe(inbox_account, clock, mid="synthetic-next-question")
    elif kind == "outgoing":
        observe(inbox_account, clock, mid="synthetic-native-outgoing", outbound=True)
    elif kind == "revision":
        observe(inbox_account, clock, body="Synthetic corrected context")
    else:
        assert kind == "identity"
        observe(
            inbox_account,
            clock,
            mid="synthetic-group-evidence",
            extra={
                "conversation_id": "synthetic-thread",
                "participant_ids": [inbox_account.account_platform_id, "synthetic-peer", "synthetic-other-peer"],
            },
        )


def state_snapshot(operation):
    return ConversationWorkState.objects.values().get(conversation_id=operation.conversation_id)


def test_bounded_anonymous_burst_dispatches_latest_target_once(inbox_account, actor, clock):
    started = clock()
    observe(inbox_account, clock, mid="synthetic-old-outgoing", outbound=True, occurred_at=started - timedelta(days=1))
    first = observe(inbox_account, clock, mid="synthetic-short-0", body="Hello")
    obsolete = prepare(actor, first)
    for index in range(1, 8):
        clock.advance(4)
        observe(inbox_account, clock, mid=f"synthetic-short-{index}", body="More context")
    clock.advance(1)
    share = observe(
        inbox_account,
        clock,
        mid="synthetic-attachment-share",
        body="",
        extra={"attachments": [{"type": "share", "url": "https://example.invalid/synthetic-share"}]},
    )
    clock.advance(1)
    final = observe(inbox_account, clock, mid="synthetic-final", body="Please answer this last question")
    state = ConversationWorkState.objects.get(conversation_id=final.conversation_id)
    assert state.burst_started_at == started
    assert state.due_at == started + timedelta(seconds=coordinator.MAX_WAIT_SECONDS)
    assert state.latest_incoming_id == final.pk
    obsolete.refresh_from_db()
    assert obsolete.status == "superseded"
    # Replaying the attachment cannot move the latest target or postpone work.
    observe(
        inbox_account,
        clock,
        mid=share.platform_message_id,
        body="",
        occurred_at=share.occurred_at,
        extra={"attachments": [{"type": "share", "url": "https://example.invalid/synthetic-share"}]},
    )
    state.refresh_from_db()
    assert state.latest_incoming_id == final.pk
    assert state.due_at == started + timedelta(seconds=coordinator.MAX_WAIT_SECONDS)
    operation = prepare(actor, final, idempotency_key="synthetic-final-operation")
    operation = coordinator.claim_reply(actor, operation_id=operation.pk, now=clock())
    harness = SyntheticDispatcher(clock)
    ticket = begin(harness, actor, operation)
    transport = ScriptedTransport(SyntheticOutcome.CONFIRMED)
    result = harness.invoke(actor, ticket, transport, gate=COMPLETE)
    assert result.outcome is SyntheticOutcome.CONFIRMED
    assert result.synthetic and not result.send_allowed
    assert len(transport.requests) == 1
    assert transport.requests[0].target_id == final.pk
    assert ConversationWorkState.objects.get(pk=state.pk).due_at is None
    assert not InboxReply.objects.exists()
    assert not EventOutbox.objects.exists()


def test_quiet_window_and_original_preflight_are_still_closed(inbox_account, actor, clock):
    row = observe(inbox_account, clock)
    operation = prepare(actor, row)
    with pytest.raises(coordinator.ReplyCoordinationError, match="not_due"):
        coordinator.claim_reply(actor, operation_id=operation.pk, now=clock())
    clock.advance(coordinator.DEBOUNCE_SECONDS)
    operation = coordinator.claim_reply(actor, operation_id=operation.pk, now=clock())
    report = coordinator.check_before_send(
        actor,
        operation_id=operation.pk,
        claim_token=operation.claim_token,
        fencing_token=operation.fencing_token,
        now=clock(),
    )
    assert report["local_state_valid"]
    assert not report["send_allowed"]
    assert not report["live_dispatch_enabled"]
    assert not report["freshness_complete"]


@pytest.mark.parametrize("gate", [SyntheticGate.UNKNOWN, SyntheticGate.FAILED, "synthetic_complete"])
def test_unknown_freshness_never_becomes_authority_from_successful_account_sync(
    ready, inbox_account, actor, clock, gate
):
    _, operation = ready
    ConversationSyncState.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform=inbox_account.platform,
        stream="dm",
        status="success",
        last_success_at=clock(),
    )
    with pytest.raises(coordinator.ReplyCoordinationError, match="synthetic_freshness_not_established"):
        begin(SyntheticDispatcher(clock), actor, operation, gate=gate)
    operation.refresh_from_db()
    assert operation.status == "claimed"
    assert operation.external_attempted_at is None


def test_outer_transaction_and_autocommit_off_cannot_hide_uncommitted_marker(ready, actor, clock):
    _, operation = ready
    harness = SyntheticDispatcher(clock)
    with transaction.atomic(), pytest.raises(RuntimeError, match="outermost committed transaction"):
        begin(harness, actor, operation)
    connection.set_autocommit(False)
    try:
        with pytest.raises(RuntimeError, match="outermost committed transaction"):
            begin(harness, actor, operation)
    finally:
        connection.rollback()
        connection.set_autocommit(True)
    operation.refresh_from_db()
    assert operation.external_attempted_at is None


@pytest.mark.parametrize("phase", ["before_marker", "during_marker", "after_commit", "transport"])
def test_crash_boundary_and_fresh_instance_recovery(ready, actor, clock, phase):
    row, operation = ready
    harness = SyntheticDispatcher(clock)
    transport = ScriptedTransport(SyntheticFault.CRASH)
    if phase in {"before_marker", "during_marker"}:
        original = coordinator.mark_outcome_unknown

        def crash(*args, **kwargs):
            if phase == "during_marker":
                original(*args, **kwargs)
            raise SyntheticCrashError("synthetic crash inside uncommitted boundary")

        with patch.object(coordinator, "mark_outcome_unknown", side_effect=crash), pytest.raises(SyntheticCrashError):
            begin(harness, actor, operation)
        operation.refresh_from_db()
        assert operation.status == "claimed"
        assert operation.external_attempted_at is None
    else:
        ticket = begin(harness, actor, operation)
        if phase == "transport":
            with pytest.raises(SyntheticCrashError):
                harness.invoke(actor, ticket, transport, gate=COMPLETE)
        assert_unknown(operation)
    assert len(transport.requests) == int(phase == "transport")
    assert transport.accepted_operation_ids == ([operation.pk] if phase == "transport" else [])
    restarted = SyntheticDispatcher(clock)
    recovered = restarted.recover(actor, operation_id=operation.pk)
    assert not recovered.retry_allowed and not recovered.send_allowed
    assert recovered.attempt_recorded == (phase in {"after_commit", "transport"})
    assert prepare(actor, row).pk == operation.pk
    clock.advance(coordinator.LEASE_SECONDS + 1)
    with pytest.raises(coordinator.ReplyCoordinationError):
        coordinator.claim_reply(actor, operation_id=operation.pk, now=clock())
    with pytest.raises(coordinator.ReplyCoordinationError):
        begin(restarted, actor, operation)
    with pytest.raises(coordinator.ReplyCoordinationError):
        prepare(actor, row, idempotency_key="synthetic-retry-cannot-escape")
    assert len(transport.requests) == int(phase == "transport")
    assert transport.accepted_operation_ids == ([operation.pk] if phase == "transport" else [])


@pytest.mark.parametrize("outcome", list(SyntheticOutcome) + [SyntheticFault.TIMEOUT])
def test_typed_outcomes_are_synthetic_and_do_not_touch_legacy_delivery(ready, inbox_message, actor, clock, outcome):
    row, operation = ready
    harness = SyntheticDispatcher(clock)
    ticket = begin(harness, actor, operation)
    before = list(ConversationMessage.objects.values())
    old_status = inbox_message.status
    transport = ScriptedTransport(outcome)
    report = harness.invoke(actor, ticket, transport, gate=COMPLETE)
    operation.refresh_from_db()
    expected = SyntheticOutcome.UNKNOWN if outcome is SyntheticFault.TIMEOUT else outcome
    assert report.outcome is expected
    assert report.synthetic and not report.send_allowed
    assert operation.outcome_code == expected.value
    assert transport.accepted_operation_ids == (
        [operation.pk] if outcome in {SyntheticOutcome.CONFIRMED, SyntheticFault.TIMEOUT} else []
    )
    assert (
        operation.status
        == {
            SyntheticOutcome.CONFIRMED: "confirmed",
            SyntheticOutcome.DEFINITELY_NOT_SENT: "failed",
            SyntheticOutcome.UNKNOWN: "outcome_unknown",
        }[expected]
    )
    assert list(ConversationMessage.objects.values()) == before
    assert not InboxReply.objects.exists()
    assert not EventOutbox.objects.exists()
    inbox_message.refresh_from_db()
    assert inbox_message.status == old_status
    assert prepare(actor, row).pk == operation.pk
    with pytest.raises(coordinator.ReplyCoordinationError):
        coordinator.claim_reply(actor, operation_id=operation.pk, now=clock())
    # A typed no-effect result permits a NEW explicit intent, never blind retry.
    if expected is SyntheticOutcome.DEFINITELY_NOT_SENT:
        assert prepare(actor, row, idempotency_key="synthetic-new-reviewed-intent").pk != operation.pk
    else:
        with pytest.raises(coordinator.ReplyCoordinationError):
            prepare(actor, row, idempotency_key="synthetic-new-reviewed-intent")


@pytest.mark.parametrize("change", ["pause", "incoming", "outgoing", "revision", "identity"])
@pytest.mark.parametrize("phase", ["before_marker", "after_marker", "inflight"])
def test_invalidation_before_and_after_boundary_is_conservative(ready, inbox_account, actor, clock, change, phase):
    row, operation = ready
    harness = SyntheticDispatcher(clock)
    transport = ScriptedTransport(SyntheticOutcome.CONFIRMED)
    if phase == "before_marker":
        invalidate(change, inbox_account, actor, clock, row)
        with pytest.raises(coordinator.ReplyCoordinationError):
            begin(harness, actor, operation)
        operation.refresh_from_db()
        assert operation.external_attempted_at is None
    else:
        ticket = begin(harness, actor, operation)
        if phase == "after_marker":
            invalidate(change, inbox_account, actor, clock, row)
            before = state_snapshot(operation)
            with pytest.raises(coordinator.ReplyCoordinationError):
                harness.invoke(actor, ticket, transport, gate=COMPLETE)
            assert state_snapshot(operation) == before
        else:
            snapshot = []

            def changed_during_transport(request):
                assert not connection.in_atomic_block
                invalidate(change, inbox_account, actor, clock, row)
                snapshot.append(state_snapshot(operation))

            transport.on_enter = changed_during_transport
            result = harness.invoke(actor, ticket, transport, gate=COMPLETE)
            assert result.outcome is SyntheticOutcome.UNKNOWN
            assert state_snapshot(operation) == snapshot[0]
        assert_unknown(operation)
    assert len(transport.requests) == int(phase == "inflight")


@pytest.mark.parametrize(
    "change", ["flag", "master", "capture", "grant", "actor", "account", "workspace", "account_workspace", "platform"]
)
@pytest.mark.parametrize("phase", ["before_invoke", "inflight"])
def test_revoked_scope_never_exposes_body_or_erases_unknown(
    ready, inbox_account, actor, clock, settings, change, phase
):
    _, operation = ready
    harness = SyntheticDispatcher(clock)
    ticket = begin(harness, actor, operation)
    current_scope = actor

    def revoke(request=None):
        nonlocal current_scope
        if change == "flag":
            settings.INBOX_REPLY_COORDINATION_ENABLED = False
        elif change == "master":
            settings.INBOX_CONVERSATION_V2_ENABLED = False
        elif change == "capture":
            settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
        elif change == "grant":
            current_scope = replace(actor, allowed_account_ids=frozenset())
        elif change == "actor":
            current_scope = replace(actor, actor_id="user:synthetic-other-operator")
        elif change == "workspace":
            current_scope = replace(actor, workspace_id=uuid4())
        elif change == "account":
            SocialAccount.objects.filter(pk=inbox_account.pk).update(connection_status="disconnected")
        elif change == "account_workspace":
            moved = Workspace.objects.create(
                name="Synthetic moved brand", organization=inbox_account.workspace.organization
            )
            SocialAccount.objects.filter(pk=inbox_account.pk).update(workspace=moved)
        else:
            SocialAccount.objects.filter(pk=inbox_account.pk).update(platform="instagram_login")

    transport = ScriptedTransport(SyntheticOutcome.CONFIRMED)
    if phase == "before_invoke":
        revoke()
        with pytest.raises(coordinator.ReplyCoordinationError):
            harness.invoke(current_scope, ticket, transport, gate=COMPLETE)
        assert transport.requests == []
    else:
        # Explicit phases let the caller refresh its principal snapshot after
        # the fake request. A stale ReplyActorScope cannot discover revocation.
        transport.on_enter = revoke
        transport.outcome = SyntheticFault.CRASH
        with pytest.raises(SyntheticCrashError):
            harness.invoke(actor, ticket, transport, gate=COMPLETE)
        result = harness.settle(current_scope, ticket, SyntheticOutcome.CONFIRMED)
        assert result.outcome is SyntheticOutcome.UNKNOWN
    assert_unknown(operation)
    with pytest.raises(coordinator.ReplyCoordinationError):
        SyntheticDispatcher(clock).recover(current_scope, operation_id=operation.pk)


@pytest.mark.parametrize(
    "field", ["claim_token", "fencing_token", "account_id", "platform", "actor_id", "payload_fingerprint"]
)
def test_bad_ticket_cannot_invoke_or_settle(ready, actor, clock, field):
    _, operation = ready
    harness = SyntheticDispatcher(clock)
    ticket = begin(harness, actor, operation)
    values = {
        "claim_token": uuid4(),
        "fencing_token": True,
        "account_id": uuid4(),
        "platform": "instagram_login",
        "actor_id": "user:synthetic-other-operator",
        "payload_fingerprint": "0" * 64,
    }
    forged = replace(ticket, **{field: values[field]})
    transport = ScriptedTransport(SyntheticOutcome.CONFIRMED)
    with pytest.raises(coordinator.ReplyCoordinationError):
        harness.invoke(actor, forged, transport, gate=COMPLETE)
    assert transport.requests == []
    report = harness.settle(actor, forged, SyntheticOutcome.CONFIRMED)
    assert report.outcome is SyntheticOutcome.UNKNOWN
    assert_unknown(operation)


def test_timeout_expiry_and_late_receipt_do_not_unlock_or_retry(ready, actor, clock):
    row, operation = ready
    harness = SyntheticDispatcher(clock)
    ticket = begin(harness, actor, operation)
    transport = ScriptedTransport(SyntheticFault.TIMEOUT)
    assert harness.invoke(actor, ticket, transport, gate=COMPLETE).outcome is SyntheticOutcome.UNKNOWN
    # Even an immediate later callback cannot upgrade a completed unknown.
    assert harness.settle(actor, ticket, SyntheticOutcome.CONFIRMED).outcome is SyntheticOutcome.UNKNOWN
    clock.advance(coordinator.LEASE_SECONDS)
    restarted = SyntheticDispatcher(clock)
    before = state_snapshot(operation)
    assert restarted.settle(actor, ticket, SyntheticOutcome.CONFIRMED).outcome is SyntheticOutcome.UNKNOWN
    assert state_snapshot(operation) == before
    with pytest.raises(coordinator.ReplyCoordinationError):
        restarted.invoke(actor, ticket, transport, gate=COMPLETE)
    with pytest.raises(coordinator.ReplyCoordinationError):
        prepare(actor, row, idempotency_key="synthetic-expired-new-key")
    assert len(transport.requests) == 1
    assert_unknown(operation)


def test_expired_local_claim_differs_from_possibly_attempted_work(ready, actor, clock):
    row, operation = ready
    clock.advance(coordinator.LEASE_SECONDS)
    with pytest.raises(coordinator.ReplyCoordinationError, match="lease_expired"):
        begin(SyntheticDispatcher(clock), actor, operation)
    recovery = SyntheticDispatcher(clock).recover(actor, operation_id=operation.pk)
    assert recovery.status == "claimed" and not recovery.attempt_recorded and not recovery.retry_allowed
    pause(actor, row)
    operation.refresh_from_db()
    assert operation.status == "superseded" and operation.external_attempted_at is None


def test_lease_expiring_during_transport_preserves_unknown_without_consuming_work(ready, actor, clock):
    _, operation = ready
    harness = SyntheticDispatcher(clock)
    ticket = begin(harness, actor, operation)
    before = state_snapshot(operation)
    transport = ScriptedTransport(
        SyntheticOutcome.CONFIRMED, on_enter=lambda request: clock.advance(coordinator.LEASE_SECONDS)
    )
    result = harness.invoke(actor, ticket, transport, gate=COMPLETE)
    assert result.outcome is SyntheticOutcome.UNKNOWN
    assert state_snapshot(operation) == before
    assert transport.accepted_operation_ids == [operation.pk]
    assert_unknown(operation)


def test_gate_rechecked_after_committed_marker_and_terminal_receipt_not_replayed(ready, actor, clock):
    _, operation = ready
    harness = SyntheticDispatcher(clock)
    ticket = begin(harness, actor, operation)
    transport = ScriptedTransport(SyntheticOutcome.CONFIRMED)
    with pytest.raises(coordinator.ReplyCoordinationError, match="synthetic_freshness_not_established"):
        harness.invoke(actor, ticket, transport)
    assert transport.requests == []
    assert_unknown(operation)
    assert harness.invoke(actor, ticket, transport, gate=COMPLETE).outcome is SyntheticOutcome.CONFIRMED
    before = state_snapshot(operation)
    repeated = harness.settle(actor, ticket, SyntheticOutcome.CONFIRMED)
    assert repeated.code == "synthetic_receipt_not_current"
    assert state_snapshot(operation) == before
    recovered = SyntheticDispatcher(clock).recover(actor, operation_id=operation.pk)
    assert recovered.status == "confirmed" and recovered.outcome_code == "synthetic_confirmed"
    assert transport.accepted_operation_ids == [operation.pk]


@pytest.mark.parametrize("field", ["body", "target_message_id", "expected_revision", "expected_generation"])
def test_key_payload_conflicts_survive_restart(ready, actor, clock, field):
    row, operation = ready
    begin(SyntheticDispatcher(clock), actor, operation)
    changes = {
        "body": "Different synthetic answer",
        "target_message_id": uuid4(),
        "expected_revision": operation.expected_revision + 1,
        "expected_generation": operation.expected_generation + 1,
    }
    assert prepare(actor, row).pk == operation.pk
    with pytest.raises(coordinator.ReplyCoordinationError, match="idempotency_conflict"):
        prepare(actor, row, **{field: changes[field]})
    assert_unknown(operation)


def test_same_identifiers_are_isolated_by_brand_platform_conversation_and_actor(
    ready, inbox_account, actor, clock, enroll_conversation_accounts
):
    row, operation = ready
    ticket = begin(SyntheticDispatcher(clock), actor, operation)
    other_workspace = Workspace.objects.create(
        name="Synthetic other brand", organization=inbox_account.workspace.organization
    )
    for workspace, platform, own_id in [
        (inbox_account.workspace, "facebook", "synthetic-other-page"),
        (other_workspace, "facebook", inbox_account.account_platform_id),
        (inbox_account.workspace, "instagram_login", inbox_account.account_platform_id),
    ]:
        account = SocialAccount.objects.create(
            workspace=workspace,
            platform=platform,
            account_platform_id=own_id,
            account_name="Synthetic same public name",
            oauth_access_token="",
        )
        enroll_conversation_accounts(account)
        scoped_actor = replace(actor, workspace_id=workspace.pk, allowed_account_ids=frozenset({account.pk}))
        other_row = observe(account, clock)
        other_operation = prepare(scoped_actor, other_row)
        assert other_operation.pk != operation.pk
        assert other_operation.conversation_id != operation.conversation_id
        with pytest.raises(coordinator.ReplyCoordinationError):
            SyntheticDispatcher(clock).invoke(
                scoped_actor, ticket, ScriptedTransport(SyntheticOutcome.CONFIRMED), gate=COMPLETE
            )
    # A second verified conversation can work independently within the account.
    other_peer = observe(inbox_account, clock, mid="synthetic-other-thread", peer="synthetic-other-peer")
    assert prepare(actor, other_peer).conversation_id != operation.conversation_id
    # A different actor cannot read/claim/settle the original or use a new key
    # to create a second active operation on its conversation.
    other_actor = replace(actor, actor_id="user:synthetic-second-operator")
    with pytest.raises(coordinator.ReplyCoordinationError, match="outcome_unknown"):
        prepare(other_actor, row)
    with pytest.raises(coordinator.ReplyCoordinationError, match="not_found_or_denied"):
        SyntheticDispatcher(clock).recover(other_actor, operation_id=operation.pk)
    assert_unknown(operation)


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL independent connections/row locks required; SQLite is sequential-only")


def in_connection(action):
    close_old_connections()
    try:
        # Bound failures without hanging CI if a regression accidentally holds
        # locks across transport. PostgreSQL connections are thread-local.
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '5s'")
            cursor.execute("SET statement_timeout = '10s'")
        return action()
    finally:
        close_old_connections()


@pytest.mark.parametrize("second_request", ["same", "different_payload", "different_key"])
def test_postgresql_simultaneous_prepare_preserves_key_and_single_active_operation(
    inbox_account, actor, clock, second_request
):
    require_postgresql()
    row = observe(inbox_account, clock)
    barrier = Barrier(2)

    def worker(second):
        barrier.wait(timeout=10)
        overrides = {}
        if second and second_request == "different_payload":
            overrides["body"] = "Another synthetic draft"
        elif second and second_request == "different_key":
            overrides["idempotency_key"] = "synthetic-other-key"
        try:
            return str(prepare(actor, row, **overrides).pk)
        except coordinator.ReplyCoordinationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(in_connection, lambda second=second: worker(second)) for second in [False, True]]
        results = [future.result(timeout=20) for future in futures]
    operation = SendOperation.objects.get(conversation_id=row.conversation_id)
    assert results.count(str(operation.pk)) == (2 if second_request == "same" else 1)
    if second_request != "same":
        assert ("idempotency_conflict" if second_request == "different_payload" else "operation_in_progress") in results
    assert ConversationWorkState.objects.get(conversation_id=row.conversation_id).active_operation_id == operation.pk


@pytest.mark.parametrize("stage", ["claim", "boundary", "entry"])
def test_postgresql_two_workers_have_one_claim_boundary_and_transport_entry(ready, actor, clock, stage):
    require_postgresql()
    _, operation = ready
    harness = SyntheticDispatcher(clock)
    if stage == "claim":
        # Reset only this synthetic fixture to exercise actual simultaneous
        # claim_reply calls; neither worker receives a pre-created claim.
        SendOperation.objects.filter(pk=operation.pk).update(
            status="prepared", claim_token=None, fencing_token=0, lease_expires_at=None
        )
        ConversationWorkState.objects.filter(conversation_id=operation.conversation_id).update(fencing_counter=0)
        ticket = None
    elif stage == "entry":
        ticket = begin(harness, actor, operation)
    else:
        ticket = None
    barrier = Barrier(2)
    transport = ScriptedTransport(SyntheticOutcome.CONFIRMED)

    def worker():
        barrier.wait(timeout=10)
        try:
            current = operation
            if stage == "claim":
                current = coordinator.claim_reply(actor, operation_id=operation.pk, now=clock())
            owned_ticket = ticket or begin(SyntheticDispatcher(clock), actor, current)
            return SyntheticDispatcher(clock).invoke(actor, owned_ticket, transport, gate=COMPLETE).outcome.value
        except coordinator.ReplyCoordinationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(in_connection, worker) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results.count(SyntheticOutcome.CONFIRMED.value) == 1
    assert len(transport.requests) == 1
    assert ConversationWorkState.objects.get(conversation_id=operation.conversation_id).fencing_counter == 1
    operation.refresh_from_db()
    assert operation.status == "confirmed" and operation.outcome_code == "synthetic_confirmed"


@pytest.mark.parametrize("change", ["pause", "incoming", "outgoing", "revision", "identity", "recover"])
def test_postgresql_inflight_marker_is_committed_and_transport_releases_locks(
    ready, inbox_account, actor, clock, change
):
    require_postgresql()
    row, operation = ready
    harness = SyntheticDispatcher(clock)
    ticket = begin(harness, actor, operation)
    entered = Event()
    release = Event()

    def block_transport(request):
        assert not connection.in_atomic_block
        entered.set()
        assert release.wait(timeout=15), "test failed to release the synthetic request"

    transport = ScriptedTransport(SyntheticOutcome.CONFIRMED, on_enter=block_transport)
    with ThreadPoolExecutor(max_workers=2) as executor:
        dispatch = executor.submit(in_connection, lambda: harness.invoke(actor, ticket, transport, gate=COMPLETE))
        try:
            assert entered.wait(timeout=10)

            def independent_observer():
                # A separate connection must see the actual committed marker,
                # not an in-memory ticket or an uncommitted savepoint.
                fresh = SendOperation.objects.get(pk=operation.pk)
                assert fresh.status == "outcome_unknown" and fresh.outcome_code == "synthetic_inflight"
                assert fresh.external_attempted_at == clock()
                if change != "recover":
                    invalidate(change, inbox_account, actor, clock, row)
                snapshot = SyntheticDispatcher(clock).recover(actor, operation_id=operation.pk)
                assert snapshot.attempt_recorded and not snapshot.retry_allowed
                return state_snapshot(operation)

            observed = executor.submit(in_connection, independent_observer).result(timeout=12)
        finally:
            release.set()
        result = dispatch.result(timeout=12)
    if change == "recover":
        assert result.outcome is SyntheticOutcome.CONFIRMED
    else:
        assert result.outcome is SyntheticOutcome.UNKNOWN
        assert state_snapshot(operation) == observed
        assert_unknown(operation)
    assert len(transport.requests) == 1
    assert not InboxReply.objects.exists()
    assert not EventOutbox.objects.exists()
