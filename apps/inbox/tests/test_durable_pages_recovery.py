"""Fresh crash/fence/live-promotion proofs for the reconstructed page service."""

import uuid
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from apps.inbox.conversation_workflow import canonical_actionable_observed, canonical_incoming_observed
from apps.inbox.durable_sync import (
    auth_fingerprint,
    claim_page,
    commit_page,
    fail_page,
    restart_stalled_live_scan,
    start_scan,
)
from apps.inbox.models import ConversationMessage, InboxMessage, InboxSyncConnection
from apps.inbox.sync_contracts import ConversationObservation, MessageObservation, SyncPage
from apps.inbox.sync_identity import SyncError, canonical_owns_account

pytestmark = pytest.mark.django_db


@pytest.fixture
def durable(inbox_account, settings, enroll_conversation_accounts):
    settings.INBOX_DURABLE_SYNC_ENABLED = True
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    settings.INBOX_CONVERSATION_WORKFLOW_ENABLED = True
    enroll_conversation_accounts(inbox_account)
    return InboxSyncConnection.objects.create(
        social_account=inbox_account,
        workspace=inbox_account.workspace,
        platform=inbox_account.platform,
        account_platform_id=inbox_account.account_platform_id,
        auth_fingerprint=auth_fingerprint(inbox_account),
        enabled=True,
    )


def checkpoint(binding, *, context="backfill"):
    return start_scan(binding.pk, context=context, stream="messages", scope_key="thread-1")


def message(lease, *, mid="m-1", body="Hello", participants=("page-1", "peer-1"), **kwargs):
    return MessageObservation(
        mid,
        lease.scope_key,
        "peer-1",
        "page-1",
        participants,
        body,
        timezone.now() - timedelta(minutes=1),
        timezone.now(),
        **kwargs,
    )


def stalled_live_pair(binding, *, now, history_context="bootstrap"):
    binding.bootstrap_baseline_at = now - timedelta(hours=1)
    binding.save(update_fields=["bootstrap_baseline_at"])
    started = now - timedelta(minutes=6)
    checkpoints = []
    for context in (history_context, "live"):
        cp = start_scan(binding.pk, context=context, stream="messages", scope_key="thread-1", now=started)
        commit_page(claim_page(cp.pk, now=started), SyncPage((), "cursorA", False, started), now=started)
        lease = claim_page(cp.pk, now=started)
        checkpoints.append(fail_page(lease, SyncError("pagination_no_progress"), now=started))
    return checkpoints[1], checkpoints[0], lease


@pytest.mark.parametrize("history_context", ["bootstrap", "repair"])
@pytest.mark.parametrize("historical_error", ["cursor_repeated", "pagination_no_progress"])
def test_stalled_live_rollover_preserves_history_and_fences_old_results(
    durable, enroll_conversation_accounts, history_context, historical_error
):
    from apps.inbox.tests.test_sync_ingestion_recovery import instagram

    enroll_conversation_accounts(instagram(durable))
    now = timezone.now()
    cp, history, stale = stalled_live_pair(durable, now=now, history_context=history_context)
    type(history).objects.filter(pk=history.pk).update(last_error_code=historical_error)
    original = type(history).objects.values().get(pk=history.pk)
    # Discovery may touch updated_at; this must not prevent a due head poll.
    assert cp.updated_at > now - timedelta(minutes=1)
    assert restart_stalled_live_scan(cp.pk, now=now) is True
    cp.refresh_from_db()
    assert cp.status == "ready" and cp.cursor == "" and cp.coverage == "partial"
    assert cp.scan_generation == stale.scan_generation + 1 and cp.fence > stale.fence
    assert restart_stalled_live_scan(cp.pk, now=now) is False
    assert type(history).objects.values().get(pk=history.pk) == original
    for operation in (
        lambda: commit_page(stale, SyncPage(observed_at=now), now=now),
        lambda: fail_page(stale, SyncError("provider_unavailable"), now=now),
    ):
        with pytest.raises(SyncError, match="lease_lost"):
            operation()
    lease = claim_page(cp.pk, now=now)
    assert lease and lease.cursor == "" and claim_page(cp.pk, now=now) is None


@pytest.mark.parametrize(
    "mutation",
    [
        "baseline",
        "context",
        "stream",
        "status",
        "error",
        "coverage",
        "cursor",
        "generation",
        "lease",
        "expired_lease",
        "retry",
        "started_recently",
        "committed_recently",
        "history_context",
        "history_missing",
        "history_connection",
        "history_cursor",
        "history_scope",
        "history_generation",
        "history_coverage",
        "history_status",
        "history_error",
        "history_lease",
        "blocked_connection",
        "retry_connection",
        "connection_generation",
        "disabled",
        "token",
        "native",
        "route",
        "facebook",
    ],
)
def test_stalled_live_rollover_refuses_without_current_independent_gap_proof(
    durable, enroll_conversation_accounts, mutation
):
    from apps.inbox.tests.test_sync_ingestion_recovery import instagram

    account = instagram(durable)
    enroll_conversation_accounts(account)
    now = timezone.now()
    cp, history, _ = stalled_live_pair(durable, now=now)
    changes = {
        "context": {"context": "backfill"},
        "stream": {"stream": "conversations"},
        "status": {"status": "ready"},
        "error": {"last_error_code": "cursor_repeated"},
        "coverage": {"coverage": "unknown"},
        "cursor": {"cursor": ""},
        "generation": {"connection_generation": uuid.uuid4()},
        "lease": {"lease_token": uuid.uuid4(), "lease_expires_at": now + timedelta(seconds=60)},
        "expired_lease": {"lease_token": uuid.uuid4(), "lease_expires_at": now - timedelta(seconds=1)},
        "retry": {"retry_at": now + timedelta(minutes=1)},
        "started_recently": {"scan_started_at": now - timedelta(minutes=4)},
        "committed_recently": {"last_committed_at": now - timedelta(minutes=4)},
    }
    history_changes = {
        "history_context": {"context": "backfill"},
        "history_cursor": {"cursor": "different"},
        "history_scope": {"scope_key": "different-thread"},
        "history_generation": {"connection_generation": uuid.uuid4()},
        "history_coverage": {"coverage": "unknown"},
        "history_status": {"status": "ready"},
        "history_error": {"last_error_code": "provider_unavailable"},
        "history_lease": {"lease_token": uuid.uuid4(), "lease_expires_at": now + timedelta(seconds=60)},
    }
    connection_changes = {
        "baseline": {"bootstrap_baseline_at": None},
        "blocked_connection": {"blocked_reason": "permission_unavailable"},
        "retry_connection": {"retry_at": now + timedelta(minutes=1)},
        "connection_generation": {"generation": uuid.uuid4()},
        "disabled": {"enabled": False},
        "route": {"route_contract": "unverified-route"},
    }
    if mutation in changes:
        type(cp).objects.filter(pk=cp.pk).update(**changes[mutation])
    elif mutation in history_changes:
        type(history).objects.filter(pk=history.pk).update(**history_changes[mutation])
    elif mutation in connection_changes:
        type(durable).objects.filter(pk=durable.pk).update(**connection_changes[mutation])
    elif mutation == "history_missing":
        history.delete()
    elif mutation == "history_connection":
        from apps.social_accounts.models import SocialAccount

        other_account = SocialAccount.objects.create(
            workspace=account.workspace, platform=account.platform, account_platform_id="other-account"
        )
        other_connection = InboxSyncConnection.objects.create(
            social_account=other_account,
            workspace=account.workspace,
            platform=account.platform,
            account_platform_id=other_account.account_platform_id,
            auth_fingerprint=auth_fingerprint(other_account),
            generation=durable.generation,
        )
        type(history).objects.filter(pk=history.pk).update(connection=other_connection)
    elif mutation in {"token", "native"}:
        if mutation == "token":
            account.oauth_access_token = "synthetic-new-token"
        else:
            account.account_platform_id = "different-native"
        account.save()
    else:
        account.platform = durable.platform = "facebook"
        account.save()
        durable.save()
        enroll_conversation_accounts(account)
    original = list(type(cp).objects.order_by("pk").values())
    if mutation in {"disabled", "route", "token", "native"}:
        with pytest.raises(SyncError, match="enrollment_or_identity_revoked"):
            restart_stalled_live_scan(cp.pk, now=now)
    else:
        assert restart_stalled_live_scan(cp.pk, now=now) is False
    assert list(type(cp).objects.order_by("pk").values()) == original


def test_new_stall_cursor_cannot_replace_the_only_gap_evidence(durable, enroll_conversation_accounts):
    from apps.inbox.tests.test_sync_ingestion_recovery import instagram

    enroll_conversation_accounts(instagram(durable))
    now = timezone.now()
    cp, history, _ = stalled_live_pair(durable, now=now)
    original = type(history).objects.values().get(pk=history.pk)
    assert restart_stalled_live_scan(cp.pk, now=now)
    commit_page(claim_page(cp.pk, now=now), SyncPage((), "new-gap", False, now), now=now)
    fail_page(claim_page(cp.pk, now=now), SyncError("pagination_no_progress"), now=now)
    assert restart_stalled_live_scan(cp.pk, now=now + timedelta(minutes=6)) is False
    cp.refresh_from_db()
    assert cp.cursor == "new-gap" and cp.status == "blocked" and cp.coverage == "partial"
    assert type(history).objects.values().get(pk=history.pk) == original


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_stalled_rollovers_start_only_one_generation(durable, enroll_conversation_accounts):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connection

    from apps.inbox.tests.test_sync_ingestion_recovery import instagram

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock proof requires PostgreSQL CI.")
    enroll_conversation_accounts(instagram(durable))
    now = timezone.now()
    cp, history, _ = stalled_live_pair(durable, now=now)
    original = type(history).objects.values().get(pk=history.pk)
    generation = cp.scan_generation
    barrier = Barrier(2)

    def restart():
        close_old_connections()
        barrier.wait(timeout=5)
        try:
            return restart_stalled_live_scan(cp.pk, now=now)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(lambda _: restart(), range(2)))
    assert sorted(outcomes) == [False, True]
    cp.refresh_from_db()
    assert cp.scan_generation == generation + 1
    assert type(history).objects.values().get(pk=history.pk) == original


def test_crash_rolls_back_whole_page_and_cursor(durable):
    lease = claim_page(checkpoint(durable).pk)
    from apps.inbox.sync_observations import reduce_observation

    def crash(account, binding, item, **kwargs):
        result = reduce_observation(account, binding, item, **kwargs)
        if item.platform_message_id == "m-2":
            raise RuntimeError("synthetic crash")
        return result

    items = (message(lease), message(lease, mid="m-2"))
    with patch("apps.inbox.sync_observations.reduce_observation", side_effect=crash), pytest.raises(RuntimeError):
        commit_page(lease, SyncPage(items, "nextA", False))
    assert not ConversationMessage.objects.exists()
    current = durable.checkpoints.get()
    assert current.cursor == "" and current.pages_committed == 0
    commit_page(lease, SyncPage(items, "nextA", False))
    assert ConversationMessage.objects.count() == 2 and not InboxMessage.objects.exists()
    assert claim_page(current.pk).cursor == "nextA"
    with pytest.raises(SyncError, match="lease_lost"):
        commit_page(lease, SyncPage(items))


def test_takeover_and_scope_fences(durable):
    cp = checkpoint(durable)
    old = claim_page(cp.pk)
    assert claim_page(cp.pk) is None
    type(cp).objects.filter(pk=cp.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    new = claim_page(cp.pk)
    assert new.fence > old.fence
    with pytest.raises(SyncError, match="lease_lost"):
        commit_page(old, SyncPage((message(old),)))
    altered = replace(new, context="live")
    with pytest.raises(SyncError, match="lease_lost"):
        commit_page(altered, SyncPage((message(altered),)))
    commit_page(new, SyncPage((message(new),)))


@pytest.mark.parametrize("change", ["flag", "token", "native", "generation"])
def test_inflight_revocation_discards_all_rows(durable, settings, change):
    lease = claim_page(checkpoint(durable).pk)
    if change == "flag":
        settings.INBOX_DURABLE_SYNC_ENABLED = False
    elif change == "generation":
        InboxSyncConnection.objects.filter(pk=durable.pk).update(generation=uuid.uuid4())
    else:
        account = durable.social_account
        if change == "token":
            account.oauth_access_token = "synthetic-rotated"
        else:
            account.account_platform_id = "other-native"
        account.save()
    with pytest.raises(SyncError):
        commit_page(lease, SyncPage((message(lease),)))
    assert not ConversationMessage.objects.exists()


def prepare_live(binding):
    binding.bootstrap_baseline_at = timezone.now() - timedelta(hours=1)
    binding.save(update_fields=["bootstrap_baseline_at"])
    listing = start_scan(binding.pk, context="live")
    lease = claim_page(listing.pk)
    commit_page(lease, SyncPage((ConversationObservation("thread-1", ("page-1", "peer-1")),)))
    return binding.checkpoints.get(stream="messages")


def test_provenance_precedes_signals_and_unknown_promotes_once(durable):
    cp = prepare_live(durable)
    reads, actions = Mock(), Mock()

    def check_proof(sender, message, **kwargs):
        assert message.observation_state.connection_generation == durable.generation
        assert message.conversation.sync_identity.connection_generation == durable.generation
        reads()

    canonical_incoming_observed.connect(check_proof, weak=False)
    canonical_actionable_observed.connect(actions, weak=False)
    try:
        lease = claim_page(cp.pk)
        first = message(lease, participants=())
        commit_page(lease, SyncPage((first,)))
        row = ConversationMessage.objects.get()
        assert row.conversation_type == "unknown" and row.incoming_generation == 1
        assert row.observation_state.live_observed_at is not None
        assert row.observation_state.actionable_observed_at is None
        assert reads.call_count == 1 and actions.call_count == 0
        for _ in range(2):
            cp = checkpoint(durable, context="live")
            lease = claim_page(cp.pk)
            commit_page(lease, SyncPage((message(lease),)))
        row.refresh_from_db()
        assert row.conversation_type == "direct" and row.conversation.workflow_state == "needs_action"
        assert row.incoming_generation == row.conversation.incoming_generation == 1
        assert row.observation_state.actionable_observed_at is not None
        assert reads.call_count == actions.call_count == 1
    finally:
        canonical_incoming_observed.disconnect(check_proof)
        canonical_actionable_observed.disconnect(actions)


def test_history_and_echo_stay_quiet_and_ownership_survives_pause(durable):
    receiver = Mock()
    canonical_incoming_observed.connect(receiver, weak=False)
    try:
        lease = claim_page(checkpoint(durable).pk)
        commit_page(lease, SyncPage((message(lease),)))
        assert canonical_owns_account(durable.social_account)
        durable.enabled, durable.generation = False, uuid.uuid4()
        durable.save()
        assert canonical_owns_account(durable.social_account)
        assert ConversationMessage.objects.get().incoming_generation is None
        receiver.assert_not_called()
    finally:
        canonical_incoming_observed.disconnect(receiver)


def test_cursor_restart_bounded_and_rate_limit_applies_to_account(durable):
    cp = checkpoint(durable)
    lease = claim_page(cp.pk)
    commit_page(lease, SyncPage((message(lease),), "cursorA", False))
    lease = claim_page(cp.pk)
    result = fail_page(lease, SyncError("cursor_invalid"))
    assert result.cursor == "" and result.scan_generation == 2 and result.status == "retry"
    type(cp).objects.filter(pk=cp.pk).update(retry_at=None)
    lease = claim_page(cp.pk)
    before = timezone.now()
    result = fail_page(lease, SyncError("rate_limited", retry_after=300))
    durable.refresh_from_db()
    assert result.retry_at >= before + timedelta(seconds=300) and durable.retry_at == result.retry_at
    assert claim_page(cp.pk) is None


def test_newer_webhook_revision_can_replace_poll_but_ambiguous_edit_waits(durable):
    cp = checkpoint(durable)
    lease = claim_page(cp.pk)
    commit_page(lease, SyncPage((message(lease, provider_revision=2),)))
    cp = checkpoint(durable)
    lease = claim_page(cp.pk)
    commit_page(lease, SyncPage((message(lease, body="Unordered edit", source="webhook"),)))
    row = ConversationMessage.objects.get()
    assert row.body == "Hello" and row.observation_state.repair_required
    cp = checkpoint(durable)
    lease = claim_page(cp.pk)
    commit_page(lease, SyncPage((message(lease, body="Verified newer", source="webhook", provider_revision=3),)))
    row.refresh_from_db()
    assert row.body == "Verified newer" and not row.observation_state.repair_required


@pytest.mark.django_db(transaction=True)
def test_postgres_two_claimers_get_only_one_live_lease(durable):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connection

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock proof requires PostgreSQL CI.")
    cp = checkpoint(durable)
    barrier = Barrier(2)

    def claim():
        close_old_connections()
        barrier.wait(timeout=5)
        try:
            return claim_page(cp.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(lambda _: claim(), range(2)))
    assert sum(value is not None for value in outcomes) == 1


def test_staged_retention_deadline_does_not_activate_expiry_processing(durable):
    from apps.inbox.sync_observations import withdrawn_content_for_internal_review

    lease = claim_page(checkpoint(durable).pk)
    commit_page(lease, SyncPage((message(lease),)))
    row = ConversationMessage.objects.get()
    state = row.observation_state
    deadline = timezone.now() - timedelta(days=1)
    state.expires_at = deadline
    state.save(update_fields=["expires_at"])
    lease = claim_page(checkpoint(durable).pk)
    commit_page(lease, SyncPage((message(lease, body="New provider snapshot", snapshot_started_at=timezone.now()),)))
    row.refresh_from_db()
    assert row.body == "New provider snapshot" and row.observation_state.expires_at == deadline
    state.refresh_from_db()
    state.withdrawn_at = timezone.now()
    state.retained_body = "Saved before withdrawal"
    state.save()
    assert withdrawn_content_for_internal_review(row)["body"] == "Saved before withdrawal"
    state.expired_at = timezone.now()
    state.save(update_fields=["expired_at"])
    assert withdrawn_content_for_internal_review(row) is None
