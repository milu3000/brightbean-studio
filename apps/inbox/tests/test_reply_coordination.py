"""Anonymous local coordination tests, not proof of PostgreSQL or live sends."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from apps.inbox.conversations import link_legacy_message, upsert_conversation_message
from apps.inbox.models import (
    ConversationMessage,
    ConversationSyncState,
    ConversationWorkState,
    InboxConversation,
    InboxMessage,
    InboxReply,
    SendOperation,
)
from apps.inbox.reply_coordination import (
    DEBOUNCE_SECONDS,
    LEASE_SECONDS,
    MAX_WAIT_SECONDS,
    ReplyActorScope,
    ReplyCoordinationError,
    check_before_send,
    claim_reply,
    enabled,
    invalidate_conversations,
    mark_outcome_unknown,
    observe_message,
    prepare_reply,
    quarantine_transferred_uncertainty,
    set_owner_paused,
)
from apps.mcp.models import EventOutbox
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def flags(settings, inbox_account, enroll_conversation_accounts):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    settings.INBOX_REPLY_COORDINATION_ENABLED = True
    enroll_conversation_accounts(inbox_account)


@pytest.fixture
def actor(inbox_account):
    return ReplyActorScope("user:synthetic-operator", inbox_account.workspace_id, frozenset({inbox_account.pk}), True)


def observe(account, *, mid="incoming-1", at=None, source="webhook", outbound=False, peer="peer-1", **kwargs):
    at = at or timezone.now()
    extra = {"message_recipient_id": peer if outbound else account.account_platform_id}
    extra.update(kwargs.pop("extra", {}))
    with patch("django.utils.timezone.now", return_value=at):
        return upsert_conversation_message(
            account,
            platform_message_id=mid,
            sender_id=account.account_platform_id if outbound else peer,
            body=kwargs.pop("body", "Synthetic answer" if outbound else "Synthetic question"),
            extra=extra,
            occurred_at=kwargs.pop("occurred_at", at),
            source=source,
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
        "body": "Synthetic draft",
        "idempotency_key": "stable-attempt-1",
    }
    args.update(overrides)
    return prepare_reply(actor, **args)


def pause(actor, row, paused=True, **overrides):
    conversation = InboxConversation.objects.get(pk=row.conversation_id)
    state = ConversationWorkState.objects.get(conversation=conversation)
    args = {
        "conversation_id": conversation.pk,
        "social_account_id": conversation.social_account_id,
        "platform": conversation.platform,
        "paused": paused,
        "expected_revision": conversation.revision,
        "expected_generation": state.generation,
    }
    args.update(overrides)
    return set_owner_paused(actor, **args)


def claim(actor, operation, **overrides):
    due = ConversationWorkState.objects.get(conversation_id=operation.conversation_id).due_at
    return claim_reply(actor, operation_id=operation.pk, **{"now": due, **overrides})


def check(actor, operation, **overrides):
    return check_before_send(
        actor,
        **{
            "operation_id": operation.pk,
            "claim_token": operation.claim_token,
            "fencing_token": operation.fencing_token,
            "now": operation.lease_expires_at - timedelta(seconds=LEASE_SECONDS),
            **overrides,
        },
    )


def unknown(actor, operation, **overrides):
    return mark_outcome_unknown(
        actor,
        **{
            "operation_id": operation.pk,
            "claim_token": operation.claim_token,
            "fencing_token": operation.fencing_token,
            **overrides,
        },
    )


@pytest.mark.parametrize("v2,coordination", [(False, False), (True, False), (False, True)])
def test_default_off_and_phase1_dependency(settings, inbox_account, actor, v2, coordination):
    settings.INBOX_CONVERSATION_V2_ENABLED = v2
    settings.INBOX_REPLY_COORDINATION_ENABLED = coordination
    assert not enabled()
    observe(inbox_account)
    assert not ConversationWorkState.objects.exists()
    assert not SendOperation.objects.exists()
    with pytest.raises(ReplyCoordinationError, match="disabled"):
        prepare_reply(
            actor,
            conversation_id=None,
            social_account_id=inbox_account.pk,
            platform=inbox_account.platform,
            expected_revision=0,
            expected_generation=0,
            target_message_id=None,
            body="Draft",
            idempotency_key="key",
        )


def test_capture_enrollment_allows_local_coordination_without_read_enrollment(settings, inbox_account, actor):
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    row = observe(inbox_account)
    operation = claim(actor, prepare(actor, row))
    assert check(actor, operation)["local_state_valid"] is True
    assert check(actor, operation)["send_allowed"] is False


@pytest.mark.parametrize("entrance", ["prepare", "claim", "check", "pause", "resume", "unknown"])
@pytest.mark.parametrize("exclusion", ["removed", "other_account", "workspace", "platform"])
def test_local_entrances_deny_excluded_enrollment_and_preserve_holds(
    settings, inbox_account, actor, entrance, exclusion
):
    row = observe(inbox_account)
    operation = claim(actor, prepare(actor, row))
    unknown(actor, operation)
    state = ConversationWorkState.objects.get()
    state.identity_quarantined = True
    state.history_gap = True
    state.owner_paused = True
    state.pause_reason = "identity_uncertain"
    state.save()
    before_state = list(ConversationWorkState.objects.values())
    before_operations = list(SendOperation.objects.values())
    entries = deepcopy(settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS)
    if exclusion == "removed":
        entries = []
    elif exclusion == "other_account":
        entries[0]["social_account_id"] = str(inbox_account.workspace_id)
    elif exclusion == "workspace":
        entries[0]["workspace_id"] = str(inbox_account.pk)
    else:
        entries[0]["platform"] = "instagram_login"
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = entries
    with pytest.raises(ReplyCoordinationError, match="not_found_or_denied"):
        if entrance == "prepare":
            prepare(actor, row)
        elif entrance == "claim":
            claim(actor, operation)
        elif entrance == "check":
            check(actor, operation)
        elif entrance in {"pause", "resume"}:
            pause(actor, row, paused=entrance == "pause")
        else:
            unknown(actor, operation)
    assert list(ConversationWorkState.objects.values()) == before_state
    assert list(SendOperation.objects.values()) == before_operations


@pytest.mark.parametrize("entrance", ["observe", "invalidate", "quarantine"])
@pytest.mark.parametrize("exclusion", ["removed", "other_account", "workspace", "platform", "capture_disabled"])
def test_internal_hooks_refresh_capture_enrollment_and_preserve_holds(
    settings, inbox_account, actor, entrance, exclusion
):
    row = observe(inbox_account)
    operation = claim(actor, prepare(actor, row))
    unknown(actor, operation)
    source = InboxConversation.objects.get(pk=row.conversation_id)
    destination = InboxConversation.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform=inbox_account.platform,
        platform_conversation_id="quarantine-destination",
        peer_id="peer-2",
        identity_kind="platform",
    )
    state = ConversationWorkState.objects.get(conversation=source)
    state.identity_quarantined = True
    state.history_gap = True
    state.owner_paused = True
    state.pause_reason = "identity_uncertain"
    state.save()
    before_state = list(ConversationWorkState.objects.values())
    before_operations = list(SendOperation.objects.values())
    if exclusion == "removed":
        settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
    elif exclusion == "other_account":
        entries = deepcopy(settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS)
        entries[0]["social_account_id"] = str(inbox_account.workspace_id)
        settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = entries
    elif exclusion == "workspace":
        moved = Workspace.objects.create(name="Moved", organization=inbox_account.workspace.organization)
        SocialAccount.objects.filter(pk=inbox_account.pk).update(workspace=moved)
    elif exclusion == "platform":
        SocialAccount.objects.filter(pk=inbox_account.pk).update(platform="instagram_login")
    else:
        settings.INBOX_CONVERSATION_V2_ENABLED = False
    if entrance == "observe":
        assert observe_message(row.pk, source="webhook", is_new=True, changed=True) is None
    elif entrance == "invalidate":
        assert invalidate_conversations(inbox_account, [source.pk]) is None
    else:
        assert quarantine_transferred_uncertainty(inbox_account, [source.pk], destination.pk) is None
    assert list(ConversationWorkState.objects.values()) == before_state
    assert list(SendOperation.objects.values()) == before_operations


@pytest.mark.parametrize("entrance", ["observe", "invalidate", "quarantine"])
def test_internal_hooks_check_enrollment_after_account_lock(settings, inbox_account, actor, entrance):
    row = observe(inbox_account)
    operation = claim(actor, prepare(actor, row))
    unknown(actor, operation)
    destination = InboxConversation.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform=inbox_account.platform,
        platform_conversation_id="lock-refresh-destination",
        peer_id="peer-2",
        identity_kind="platform",
    )
    before_state = list(ConversationWorkState.objects.values())
    before_operations = list(SendOperation.objects.values())
    from apps.inbox.locking import lock_dm_account

    def remove_enrollment_at_lock(account_id, workspace_id):
        current = lock_dm_account(account_id, workspace_id)
        settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
        return current

    with patch("apps.inbox.reply_coordination.lock_dm_account", side_effect=remove_enrollment_at_lock) as locked:
        if entrance == "observe":
            observe_message(row.pk, source="webhook", is_new=True, changed=True)
        elif entrance == "invalidate":
            invalidate_conversations(inbox_account, [row.conversation_id])
        else:
            quarantine_transferred_uncertainty(inbox_account, [row.conversation_id], destination.pk)
    locked.assert_called_once_with(inbox_account.pk, inbox_account.workspace_id)
    assert list(ConversationWorkState.objects.values()) == before_state
    assert list(SendOperation.objects.values()) == before_operations


@pytest.mark.parametrize("share_first", [False, True])
def test_short_burst_shared_content_and_final_text_are_one_work_state(inbox_account, share_first):
    start = timezone.now()
    parts = [
        ("context", "A synthetic topic", {}),
        ("card", "", {"attachments": [{"type": "share", "url": "https://example.com/item"}]}),
    ]
    if share_first:
        parts.reverse()
    for i, (mid, body, extra) in enumerate(parts):
        observe(inbox_account, mid=mid, body=body, extra=extra, at=start + timedelta(seconds=i * 2))
    final = observe(inbox_account, mid="final", body="Which option fits?", at=start + timedelta(seconds=4))
    state = ConversationWorkState.objects.get()
    assert state.generation == 3
    assert state.latest_incoming_id == final.pk
    assert state.burst_started_at == start
    assert state.due_at == start + timedelta(seconds=4 + DEBOUNCE_SECONDS)
    assert ConversationMessage.objects.filter(conversation=state.conversation).count() == 3
    assert not InboxMessage.objects.exists()
    assert not InboxReply.objects.exists()
    assert not EventOutbox.objects.exists()


def test_exact_replay_does_not_extend_due_or_generation(inbox_account):
    start = timezone.now()
    row = observe(inbox_account, at=start)
    state = ConversationWorkState.objects.get()
    observe(inbox_account, at=start + timedelta(seconds=20), occurred_at=start)
    state.refresh_from_db()
    assert state.generation == 1
    assert state.latest_incoming_id == row.pk
    assert state.due_at == start + timedelta(seconds=DEBOUNCE_SECONDS)


def test_continuous_incoming_honors_maximum_wait(inbox_account):
    start = timezone.now()
    for offset in range(0, 40, 2):
        observe(inbox_account, mid=f"incoming-{offset}", at=start + timedelta(seconds=offset))
    state = ConversationWorkState.objects.get()
    assert state.burst_started_at == start
    assert state.due_at == start + timedelta(seconds=MAX_WAIT_SECONDS)
    assert state.generation == 20


def test_backfill_never_creates_work_or_invalidates_existing_draft(inbox_account, actor):
    observe(inbox_account, source="legacy_backfill")
    assert not ConversationWorkState.objects.exists()
    row = observe(inbox_account, mid="live")
    operation = prepare(actor, row)
    state = ConversationWorkState.objects.get()
    observe(inbox_account, mid="old-outgoing", outbound=True, source="legacy_backfill")
    state.refresh_from_db()
    operation.refresh_from_db()
    assert state.generation == 1
    assert not state.owner_paused
    assert operation.status == "prepared"
    # Historical additions still change ledger revision, so the old prepared
    # intent cannot pass a fresh preflight even though no work was queued.
    with pytest.raises(ReplyCoordinationError, match="history_gap"):
        claim(actor, operation)


def test_idempotency_same_payload_returns_original_and_changed_payload_conflicts(inbox_account, actor):
    row = observe(inbox_account)
    first = prepare(actor, row)
    assert prepare(actor, row).pk == first.pk
    with pytest.raises(ReplyCoordinationError, match="idempotency_conflict"):
        prepare(actor, row, body="Different answer")
    assert SendOperation.objects.count() == 1


def test_new_target_invalidates_draft_and_old_claim(inbox_account, actor):
    start = timezone.now()
    row = observe(inbox_account, at=start)
    operation = claim(actor, prepare(actor, row))
    newer = observe(
        inbox_account, mid="new-final", at=start + timedelta(seconds=6), occurred_at=start + timedelta(seconds=6)
    )
    operation.refresh_from_db()
    assert operation.status == "superseded"
    state = ConversationWorkState.objects.get()
    assert state.latest_incoming_id == newer.pk
    with pytest.raises(ReplyCoordinationError, match="invalid_claim"):
        check(actor, operation)
    with pytest.raises(ReplyCoordinationError, match="stale_target"):
        prepare(actor, row, idempotency_key="old-target")
    assert prepare(actor, newer, idempotency_key="new-target").target_id == newer.pk


def test_prepared_intent_can_be_staged_before_due_but_not_claimed(inbox_account, actor):
    at = timezone.now()
    row = observe(inbox_account, at=at)
    operation = prepare(actor, row)
    with pytest.raises(ReplyCoordinationError, match="not_due"):
        claim(actor, operation, now=at)
    operation.refresh_from_db()
    assert operation.status == "prepared"


def test_single_flight_is_conversation_scoped_even_for_other_actor(inbox_account, actor):
    row = observe(inbox_account)
    prepare(actor, row)
    with pytest.raises(ReplyCoordinationError, match="operation_in_progress"):
        prepare(replace(actor, actor_id="user:other-operator"), row)
    assert SendOperation.objects.count() == 1


def test_claim_has_fence_and_never_calls_provider_or_changes_legacy(inbox_account, actor):
    legacy = InboxMessage.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform_message_id="legacy",
        message_type="dm",
        sender_name="Synthetic peer",
        status="archived",
        received_at=timezone.now(),
    )
    row = observe(inbox_account, legacy_message=legacy)
    with patch("apps.inbox.services.get_provider") as provider:
        operation = claim(actor, prepare(actor, row))
        result = check(actor, operation)
        provider.assert_not_called()
    assert result["local_state_valid"] is True
    assert result["send_allowed"] is False
    assert result["live_dispatch_enabled"] is False
    assert result["freshness_complete"] is False
    assert result["external_atomicity"] is False
    assert operation.status == "claimed"
    assert operation.claim_token is not None
    assert operation.fencing_token == 1
    assert operation.external_attempted_at is None
    legacy.refresh_from_db()
    assert legacy.status == "archived"
    assert not InboxReply.objects.exists()
    assert not EventOutbox.objects.exists()


@pytest.mark.parametrize("sync_status", ["running", "failed", "unknown", "success"])
def test_historical_sync_success_never_claims_complete_freshness(inbox_account, actor, sync_status):
    row = observe(inbox_account)
    ConversationSyncState.objects.create(
        workspace_id=inbox_account.workspace_id,
        social_account=inbox_account,
        platform=inbox_account.platform,
        stream="dm",
        status=sync_status,
        coverage="partial",
        last_success_at=timezone.now() - timedelta(days=1),
    )
    result = check(actor, claim(actor, prepare(actor, row)))
    assert result["dm_sync_status"] == sync_status
    assert result["freshness_complete"] is False
    assert result["live_dispatch_enabled"] is False


def test_expired_lease_does_not_become_failed_or_reclaimable(inbox_account, actor):
    row = observe(inbox_account)
    operation = claim(actor, prepare(actor, row))
    with pytest.raises(ReplyCoordinationError, match="lease_expired"):
        check(actor, operation, now=operation.lease_expires_at)
    with pytest.raises(ReplyCoordinationError, match="not_prepared"):
        claim(actor, operation, now=operation.lease_expires_at)
    operation.refresh_from_db()
    assert operation.status == "claimed"
    unknown(actor, operation, now=operation.lease_expires_at + timedelta(seconds=1))
    operation.refresh_from_db()
    assert operation.status == "outcome_unknown"


def test_unknown_blocks_different_key_new_message_and_pause_resume(inbox_account, actor):
    row = observe(inbox_account)
    operation = claim(actor, prepare(actor, row))
    unknown(actor, operation)
    assert prepare(actor, row).status == "outcome_unknown"
    with pytest.raises(ReplyCoordinationError, match="outcome_unknown"):
        prepare(actor, row, idempotency_key="new-key")
    pause(actor, row)
    pause(actor, row, paused=False)
    newer = observe(inbox_account, mid="newer")
    with pytest.raises(ReplyCoordinationError, match="outcome_unknown"):
        prepare(actor, newer, idempotency_key="newer-key")
    operation.refresh_from_db()
    assert operation.status == "outcome_unknown"
    assert ConversationWorkState.objects.get().active_operation_id == operation.pk


@pytest.mark.parametrize("explicit", [True, False])
def test_owner_pause_or_native_outgoing_invalidates_without_claiming_human_authorship(inbox_account, actor, explicit):
    row = observe(inbox_account)
    operation = claim(actor, prepare(actor, row))
    if explicit:
        pause(actor, row)
    else:
        observe(inbox_account, mid="native-outgoing", outbound=True)
    state = ConversationWorkState.objects.get()
    operation.refresh_from_db()
    assert state.owner_paused
    assert state.pause_reason == ("owner_requested" if explicit else "outgoing_observed")
    assert state.due_at is None
    assert operation.status == "superseded"
    assert not InboxMessage.objects.exists()
    assert not EventOutbox.objects.exists()
    state = pause(actor, row, paused=False)
    assert not state.owner_paused
    assert state.due_at is None
    with pytest.raises(ReplyCoordinationError, match="no_pending_work"):
        prepare(actor, row, idempotency_key="after-resume")
    newer = observe(inbox_account, mid="new-after-resume")
    new_op = claim(actor, prepare(actor, newer, idempotency_key="fresh-key"))
    assert new_op.fencing_token > operation.fencing_token
    with pytest.raises(ReplyCoordinationError, match="invalid_claim"):
        check(actor, new_op, claim_token=operation.claim_token, fencing_token=operation.fencing_token)


def test_stale_owner_action_is_rejected_by_generation_even_if_revision_unchanged(inbox_account, actor):
    row = observe(inbox_account)
    previous = ConversationWorkState.objects.get().generation
    pause(actor, row)
    with pytest.raises(ReplyCoordinationError, match="stale_generation"):
        pause(actor, row, paused=False, expected_generation=previous)
    assert ConversationWorkState.objects.get().owner_paused


@pytest.mark.parametrize("change", ["workspace", "account_grant", "permission", "actor", "platform", "disconnected"])
def test_scopes_are_rechecked_before_claim(inbox_account, actor, change):
    row = observe(inbox_account)
    operation = prepare(actor, row)
    if change == "workspace":
        other = Workspace.objects.create(name="Other", organization=inbox_account.workspace.organization)
        SocialAccount.objects.filter(pk=inbox_account.pk).update(workspace=other)
    elif change == "account_grant":
        actor = replace(actor, allowed_account_ids=frozenset())
    elif change == "permission":
        actor = replace(actor, can_use_inbox=False)
    elif change == "actor":
        actor = replace(actor, actor_id="user:unrelated-operator")
    elif change == "platform":
        SocialAccount.objects.filter(pk=inbox_account.pk).update(platform="instagram_login")
    else:
        SocialAccount.objects.filter(pk=inbox_account.pk).update(connection_status="disconnected")
    with pytest.raises(ReplyCoordinationError, match="not_found_or_denied"):
        claim(actor, operation)
    operation.refresh_from_db()
    assert operation.status == "prepared"


def test_no_cross_account_peer_or_thread_merge(inbox_account, actor, enroll_conversation_accounts):
    other = SocialAccount.objects.create(
        workspace=inbox_account.workspace,
        platform=inbox_account.platform,
        account_platform_id="other-account",
        account_name="Other synthetic account",
    )
    enroll_conversation_accounts(other)
    row = observe(inbox_account)
    other_row = observe(other)
    other_peer = observe(inbox_account, mid="other-peer-message", peer="peer-2")
    assert ConversationWorkState.objects.count() == 3
    assert len({row.conversation_id, other_row.conversation_id, other_peer.conversation_id}) == 3
    with pytest.raises(ReplyCoordinationError, match="not_found_or_denied"):
        prepare(actor, other_row)
    with pytest.raises(ReplyCoordinationError, match="stale_target"):
        prepare(actor, row, target_message_id=other_peer.pk)


@pytest.mark.parametrize("extra", [{"message_recipient_id": ""}, {"participant_ids": ["page-1", "peer-1", "peer-2"]}])
def test_unverified_or_group_identity_creates_no_work(inbox_account, extra):
    observe(inbox_account, extra=extra)
    assert not ConversationWorkState.objects.exists()


@pytest.mark.parametrize("kind", ["paused", "unknown"])
def test_fallback_merge_preserves_pause_and_unknown_as_fail_closed(inbox_account, actor, kind):
    row = observe(inbox_account)
    fallback_id = row.conversation_id
    operation = claim(actor, prepare(actor, row))
    if kind == "unknown":
        unknown(actor, operation)
    else:
        pause(actor, row)
    provider = InboxConversation.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform=inbox_account.platform,
        platform_conversation_id="native-thread",
        identity_kind="platform",
    )
    provider_row = observe(inbox_account, mid="provider-observation", extra={"conversation_id": "native-thread"})
    assert provider_row.conversation_id == provider.pk
    assert InboxConversation.objects.filter(pk=fallback_id).exists()
    state = ConversationWorkState.objects.get(conversation_id=fallback_id)
    operation.refresh_from_db()
    row.refresh_from_db()
    assert row.conversation_id == fallback_id
    assert operation.conversation_id == fallback_id
    assert state.owner_paused
    assert state.due_at is None
    assert operation.status == ("outcome_unknown" if kind == "unknown" else "superseded")
    with pytest.raises(ReplyCoordinationError):
        check(actor, operation)
    with pytest.raises(ReplyCoordinationError):
        prepare(actor, row, idempotency_key="old-identity-retry")
    # A provider-only newcomer is not allowed to bypass retained uncertainty.
    assert not ConversationWorkState.objects.filter(conversation=provider).exists()


def test_bulk_identity_withdrawal_invalidates_existing_claim(inbox_account, actor):
    row = observe(inbox_account, extra={"conversation_id": "thread-1"})
    operation = claim(actor, prepare(actor, row))
    observe(
        inbox_account,
        mid="group-evidence",
        extra={
            "conversation_id": "thread-1",
            "participant_ids": [inbox_account.account_platform_id, "peer-1", "peer-2"],
        },
    )
    state = ConversationWorkState.objects.get()
    operation.refresh_from_db()
    assert state.owner_paused
    assert state.pause_reason == "identity_uncertain"
    assert operation.status == "superseded"
    with pytest.raises(ReplyCoordinationError, match="invalid_claim"):
        check(actor, operation)


@pytest.mark.django_db(transaction=True)
def test_postgresql_two_workers_can_claim_only_once(inbox_account, actor):
    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL row locks; SQLite is not concurrency proof")
    row = observe(inbox_account)
    operation = prepare(actor, row)
    due = ConversationWorkState.objects.get().due_at
    barrier = Barrier(2)

    def run():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                result = claim_reply(actor, operation_id=operation.pk, now=due)
                return ("claimed", result.fencing_token)
            except ReplyCoordinationError as exc:
                return (exc.code, None)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))
    assert sorted(code for code, _ in results) == ["claimed", "not_prepared"]
    assert ConversationWorkState.objects.get().fencing_counter == 1


def test_second_provider_thread_withdraws_fallback_target_without_losing_unknown(inbox_account, actor):
    observe(inbox_account, mid="thread-1-evidence", extra={"conversation_id": "thread-1"})
    original = observe(inbox_account)
    original.refresh_from_db()
    latest = ConversationWorkState.objects.get().latest_incoming
    operation = claim(actor, prepare(actor, latest))
    unknown(actor, operation)
    observe(inbox_account, mid="thread-2-evidence", extra={"conversation_id": "thread-2"})
    original.refresh_from_db()
    state = ConversationWorkState.objects.get()
    operation.refresh_from_db()
    assert original.conversation_id is None
    assert state.owner_paused
    assert state.pause_reason == "identity_uncertain"
    assert state.due_at is None
    assert state.active_operation_id == operation.pk
    assert operation.status == "outcome_unknown"
    with pytest.raises(ReplyCoordinationError):
        check(actor, operation)


def test_body_edit_supersedes_draft_without_extending_burst(inbox_account, actor):
    at = timezone.now()
    row = observe(inbox_account, at=at)
    operation = prepare(actor, row)
    state = ConversationWorkState.objects.get()
    observe(inbox_account, at=at + timedelta(seconds=3), occurred_at=at, body="Edited synthetic question")
    operation.refresh_from_db()
    state.refresh_from_db()
    assert operation.status == "superseded"
    assert state.generation == 1
    assert state.latest_incoming_id == row.pk
    assert state.due_at == at + timedelta(seconds=DEBOUNCE_SECONDS)


def test_pause_replay_of_same_native_outgoing_does_not_repause_after_resume(inbox_account, actor):
    row = observe(inbox_account)
    at = timezone.now()
    observe(inbox_account, mid="outgoing", outbound=True, at=at)
    pause(actor, row, paused=False)
    observe(inbox_account, mid="outgoing", outbound=True, at=at + timedelta(seconds=3), occurred_at=at)
    state = ConversationWorkState.objects.get()
    assert not state.owner_paused
    assert state.due_at is None


@pytest.mark.parametrize("platform", ["threads", "instagram"])
def test_unverified_adapter_cannot_create_coordination_work(inbox_account, platform):
    inbox_account.platform = platform
    inbox_account.save(update_fields=["platform"])
    observe(inbox_account)
    assert not ConversationWorkState.objects.exists()


@pytest.mark.parametrize("field", ["body", "expected_revision", "expected_generation"])
def test_mutated_operation_payload_cannot_claim_or_pass_preflight(inbox_account, actor, field):
    row = observe(inbox_account)
    operation = claim(actor, prepare(actor, row))
    SendOperation.objects.filter(pk=operation.pk).update(**{field: "Changed payload" if field == "body" else 999})
    with pytest.raises(ReplyCoordinationError, match="invalid_payload_fingerprint"):
        check(actor, operation)


def test_same_idempotency_key_is_scoped_to_conversation(inbox_account, actor):
    first = observe(inbox_account)
    second = observe(inbox_account, mid="other-peer", peer="peer-2")
    operation_a = prepare(actor, first)
    operation_b = prepare(actor, second)
    assert operation_a.idempotency_key == operation_b.idempotency_key
    assert operation_a.pk != operation_b.pk
    assert operation_a.conversation_id != operation_b.conversation_id


@pytest.mark.parametrize("source", ["poll", "webhook"])
def test_newly_attributed_existing_outbound_pauses_pending_work(inbox_account, actor, source):
    at = timezone.now()
    outgoing = observe(
        inbox_account,
        mid="initially-unassigned-outbound",
        outbound=True,
        at=at,
        occurred_at=None,
        extra={"message_recipient_id": ""},
        source=source,
    )
    assert outgoing.conversation_id is None
    row = observe(inbox_account, at=at + timedelta(seconds=1))
    operation = prepare(actor, row)
    outgoing = observe(
        inbox_account,
        mid="initially-unassigned-outbound",
        outbound=True,
        at=at + timedelta(seconds=2),
        occurred_at=None,
        source=source,
    )
    state = ConversationWorkState.objects.get()
    operation.refresh_from_db()
    assert outgoing.conversation_id == row.conversation_id
    assert state.owner_paused
    assert state.pause_reason == "outgoing_observed"
    assert state.due_at is None
    assert operation.status == "superseded"
    with pytest.raises(ReplyCoordinationError, match="owner_paused"):
        prepare(actor, row, idempotency_key="cannot-bypass-outgoing")


@pytest.mark.parametrize("delete_target", [False, True])
def test_reassigned_unknown_target_quarantines_destination_across_resume_and_new_incoming(
    inbox_account, actor, delete_target
):
    row = observe(inbox_account)
    source_conversation_id = row.conversation_id
    operation = claim(actor, prepare(actor, row))
    unknown(actor, operation)
    moved = observe(
        inbox_account,
        mid=row.platform_message_id,
        peer="peer-2",
        source="poll",
        extra={"conversation_id": "new-thread"},
    )
    assert moved.conversation_id != source_conversation_id
    destination_id = moved.conversation_id
    state = ConversationWorkState.objects.get(conversation_id=destination_id)
    assert state.identity_quarantined
    assert state.owner_paused
    assert state.due_at is None
    operation.refresh_from_db()
    assert operation.conversation_id == source_conversation_id
    assert operation.status == "outcome_unknown"
    with pytest.raises(ReplyCoordinationError, match="identity_reconciliation_required"):
        pause(actor, moved, paused=False)
    if delete_target:
        moved.delete()
    latest = observe(
        inbox_account, mid="new-target-on-new-thread", peer="peer-2", extra={"conversation_id": "new-thread"}
    )
    state.refresh_from_db()
    assert state.identity_quarantined
    assert state.due_at is None
    with pytest.raises(ReplyCoordinationError, match="identity_reconciliation_required"):
        prepare(actor, latest, idempotency_key="cannot-bypass-identity-uncertainty")
    with pytest.raises(ReplyCoordinationError, match="identity_reconciliation_required"):
        pause(actor, latest, paused=False)
    operation.refresh_from_db()
    assert operation.status == "outcome_unknown"


def test_identity_quarantine_propagates_through_another_transfer(inbox_account, actor):
    original = observe(inbox_account)
    unknown(actor, claim(actor, prepare(actor, original)))
    moved = observe(
        inbox_account,
        mid=original.platform_message_id,
        peer="peer-2",
        source="poll",
        extra={"conversation_id": "thread-2"},
    )
    assert ConversationWorkState.objects.get(conversation_id=moved.conversation_id).identity_quarantined
    # Transfer another row from the held conversation, with no operation of
    # its own. A second identity change must not launder the inherited hold.
    next_row = observe(inbox_account, mid="next-row", peer="peer-2")
    moved_again = observe(
        inbox_account,
        mid=next_row.platform_message_id,
        peer="peer-3",
        source="poll",
        extra={"conversation_id": "thread-3"},
    )
    assert moved_again.conversation_id != moved.conversation_id
    assert ConversationWorkState.objects.get(conversation_id=moved_again.conversation_id).identity_quarantined
    with pytest.raises(ReplyCoordinationError, match="identity_reconciliation_required"):
        pause(actor, moved_again, paused=False)


def test_newest_first_poll_keeps_final_question_as_target(inbox_account, actor):
    at = timezone.now()
    final = observe(inbox_account, mid="final-question", at=at, occurred_at=at - timedelta(seconds=1), source="poll")
    draft = prepare(actor, final)
    for offset, (mid, body) in enumerate([("shared-card", ""), ("short-context", "Synthetic context")], start=1):
        observe(
            inbox_account,
            mid=mid,
            body=body,
            at=at + timedelta(seconds=offset),
            occurred_at=at - timedelta(seconds=offset + 1),
            source="poll",
        )
    state = ConversationWorkState.objects.get()
    draft.refresh_from_db()
    assert state.latest_incoming_id == final.pk
    assert not state.ordering_uncertain
    assert state.generation == 3
    assert draft.status == "superseded"
    assert prepare(actor, final, idempotency_key="fresh-final-question").target_id == final.pk


@pytest.mark.parametrize("kind", ["unknown", "tie"])
def test_uncertain_message_order_holds_instead_of_guessing_latest(inbox_account, actor, kind):
    at = timezone.now()
    first = observe(inbox_account, at=at)
    operation = prepare(actor, first)
    observe(
        inbox_account,
        mid="uncertain-order",
        at=at + timedelta(seconds=1),
        occurred_at=None if kind == "unknown" else at,
    )
    state = ConversationWorkState.objects.get()
    operation.refresh_from_db()
    assert state.ordering_uncertain
    assert state.due_at is None
    assert operation.status == "superseded"
    with pytest.raises(ReplyCoordinationError, match="ordering_uncertain"):
        prepare(actor, first, idempotency_key="cannot-guess-order")
    pause(actor, first)
    pause(actor, first, paused=False)
    later = observe(inbox_account, mid="later-after-resume", at=at + timedelta(seconds=2))
    with pytest.raises(ReplyCoordinationError, match="ordering_uncertain"):
        prepare(actor, later, idempotency_key="still-needs-order-reconciliation")


def test_delayed_older_observation_does_not_requeue_old_target_after_resume(inbox_account, actor):
    at = timezone.now()
    final = observe(inbox_account, at=at)
    pause(actor, final)
    pause(actor, final, paused=False)
    observe(inbox_account, mid="delayed-history", at=at + timedelta(seconds=5), occurred_at=at - timedelta(days=1))
    state = ConversationWorkState.objects.get()
    assert state.latest_incoming_id == final.pk
    assert state.due_at is None
    with pytest.raises(ReplyCoordinationError, match="no_pending_work"):
        prepare(actor, final)


@pytest.mark.parametrize("reversed_batch", [False, True])
def test_full_synthetic_burst_with_old_outgoing_and_share_keeps_final_target(inbox_account, actor, reversed_batch):
    at = timezone.now()
    parts = [
        ("old-outgoing", "Earlier answer", True, {}),
        ("context-a", "First short context", False, {}),
        ("context-b", "Second short context", False, {}),
        ("shared-card", "", False, {"inbox_attachments": [{"type": "share", "url": "https://example.com/card"}]}),
        ("final-question", "Final synthetic question?", False, {}),
    ]
    ordered = list(enumerate(parts))
    if reversed_batch:
        ordered.reverse()
    final = None
    for arrival, (position, (mid, body, outbound, extra)) in enumerate(ordered):
        row = observe(
            inbox_account,
            mid=mid,
            body=body,
            outbound=outbound,
            extra=extra,
            source="poll",
            at=at + timedelta(seconds=arrival),
            occurred_at=at - timedelta(seconds=20 - position),
        )
        if mid == "final-question":
            final = row
    state = ConversationWorkState.objects.get()
    assert state.latest_incoming_id == final.pk
    assert not state.owner_paused
    assert not state.ordering_uncertain
    assert state.due_at is not None
    assert ConversationMessage.objects.count() == 5
    assert ConversationMessage.objects.get(platform_message_id="shared-card").attachments
    operation = prepare(actor, final)
    assert operation.target_id == final.pk
    assert not check(actor, claim(actor, operation))["send_allowed"]


def test_flag_off_identity_transfer_cannot_erase_existing_unknown(settings, inbox_account, actor):
    original = observe(inbox_account)
    operation = claim(actor, prepare(actor, original))
    unknown(actor, operation)
    settings.INBOX_REPLY_COORDINATION_ENABLED = False
    moved = observe(
        inbox_account,
        mid=original.platform_message_id,
        peer="peer-2",
        source="poll",
        extra={"conversation_id": "other-thread"},
    )
    assert ConversationWorkState.objects.get(conversation_id=moved.conversation_id).identity_quarantined
    settings.INBOX_REPLY_COORDINATION_ENABLED = True
    latest = observe(inbox_account, mid="later-target", peer="peer-2", extra={"conversation_id": "other-thread"})
    with pytest.raises(ReplyCoordinationError, match="identity_reconciliation_required"):
        prepare(actor, latest, idempotency_key="cannot-launder-via-flag")


def test_flag_off_new_incoming_cannot_be_hidden_by_new_revision_or_later_observation(settings, inbox_account, actor):
    at = timezone.now()
    original = observe(inbox_account, at=at)
    settings.INBOX_REPLY_COORDINATION_ENABLED = False
    observe(inbox_account, mid="missed-while-disabled", at=at + timedelta(seconds=2))
    settings.INBOX_REPLY_COORDINATION_ENABLED = True
    with pytest.raises(ReplyCoordinationError, match="history_gap"):
        prepare(actor, original)
    later = observe(
        inbox_account, mid="after-reenable", at=at + timedelta(seconds=3), occurred_at=at + timedelta(seconds=1)
    )
    state = ConversationWorkState.objects.get()
    assert state.history_gap
    assert state.due_at is None
    with pytest.raises(ReplyCoordinationError, match="history_gap"):
        prepare(actor, later, idempotency_key="cannot-freshen-gap")
    with pytest.raises(ReplyCoordinationError, match="history_gap"):
        pause(actor, later, paused=False)


@pytest.mark.parametrize("missed_observation", [False, True])
def test_legacy_link_only_advances_an_already_current_snapshot(settings, inbox_account, actor, missed_observation):
    row = observe(inbox_account)
    if missed_observation:
        settings.INBOX_REPLY_COORDINATION_ENABLED = False
        observe(inbox_account, mid="missed-before-link")
        settings.INBOX_REPLY_COORDINATION_ENABLED = True
    legacy = InboxMessage.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform_message_id="linked-legacy",
        message_type="dm",
        sender_name="Synthetic peer",
        received_at=timezone.now(),
    )
    link_legacy_message(row, legacy)
    state = ConversationWorkState.objects.get()
    conversation = InboxConversation.objects.get(pk=row.conversation_id)
    if missed_observation:
        assert state.conversation_revision < conversation.revision
        with pytest.raises(ReplyCoordinationError, match="history_gap"):
            prepare(actor, row)
    else:
        assert state.conversation_revision == conversation.revision
        assert prepare(actor, row).status == "prepared"


def test_known_multi_revision_promotion_is_conservatively_held(inbox_account, actor):
    first = observe(inbox_account)
    promoted = observe(inbox_account, mid="provider-evidence", extra={"conversation_id": "native-thread"})
    assert promoted.conversation_id == first.conversation_id
    state = ConversationWorkState.objects.get()
    assert state.history_gap
    assert state.due_at is None
    with pytest.raises(ReplyCoordinationError, match="history_gap"):
        prepare(actor, promoted)


def test_owner_pause_cannot_freshen_a_missed_history_snapshot(settings, inbox_account, actor):
    first = observe(inbox_account)
    settings.INBOX_REPLY_COORDINATION_ENABLED = False
    observe(inbox_account, mid="missed-before-pause")
    settings.INBOX_REPLY_COORDINATION_ENABLED = True
    pause(actor, first)
    assert ConversationWorkState.objects.get().history_gap
    with pytest.raises(ReplyCoordinationError, match="history_gap"):
        pause(actor, first, paused=False)


@pytest.mark.parametrize("outbound", [False, True])
@pytest.mark.parametrize("when", ["newer", "same", "unknown"])
def test_first_coordination_state_checks_preexisting_phase1_ledger(settings, inbox_account, actor, outbound, when):
    at = timezone.now()
    target_time = at - timedelta(seconds=5)
    previous_time = {"newer": at - timedelta(seconds=2), "same": target_time, "unknown": None}[when]
    settings.INBOX_REPLY_COORDINATION_ENABLED = False
    observe(inbox_account, mid="preexisting-history", outbound=outbound, at=at, occurred_at=previous_time)
    assert not ConversationWorkState.objects.exists()
    settings.INBOX_REPLY_COORDINATION_ENABLED = True
    candidate = observe(
        inbox_account, mid="first-late-observation", at=at + timedelta(seconds=1), occurred_at=target_time
    )
    assert ConversationWorkState.objects.get().latest_incoming_id == candidate.pk
    code = "newer_or_uncertain_outgoing" if outbound else "newer_or_uncertain_incoming"
    with pytest.raises(ReplyCoordinationError, match=code):
        prepare(actor, candidate)
    assert not SendOperation.objects.exists()
    assert not EventOutbox.objects.exists()


@pytest.mark.parametrize("outbound", [False, True])
def test_first_coordination_state_allows_reliably_older_phase1_context(settings, inbox_account, actor, outbound):
    at = timezone.now()
    settings.INBOX_REPLY_COORDINATION_ENABLED = False
    observe(inbox_account, mid="known-older-history", outbound=outbound, at=at, occurred_at=at - timedelta(seconds=10))
    settings.INBOX_REPLY_COORDINATION_ENABLED = True
    current = observe(
        inbox_account, mid="actual-final-question", at=at + timedelta(seconds=1), occurred_at=at - timedelta(seconds=5)
    )
    assert prepare(actor, current).target_id == current.pk


def test_unknown_target_provenance_survives_unassigned_then_new_thread(inbox_account, actor):
    original = observe(inbox_account)
    operation = claim(actor, prepare(actor, original))
    unknown(actor, operation)
    source_id = operation.conversation_id
    observe(inbox_account, mid="proof-one", extra={"conversation_id": "thread-one"})
    observe(inbox_account, mid="proof-two", extra={"conversation_id": "thread-two"})
    original.refresh_from_db()
    assert original.conversation_id is None
    moved = observe(
        inbox_account,
        mid=original.platform_message_id,
        peer="peer-2",
        source="poll",
        extra={"conversation_id": "thread-three"},
    )
    assert moved.conversation_id != source_id
    state = ConversationWorkState.objects.get(conversation_id=moved.conversation_id)
    assert state.identity_quarantined
    operation.refresh_from_db()
    assert operation.conversation_id == source_id
    assert operation.target_id == original.pk
    assert operation.status == "outcome_unknown"
    with pytest.raises(ReplyCoordinationError, match="identity_reconciliation_required"):
        pause(actor, moved, paused=False)
    latest = observe(inbox_account, mid="later-third-thread", peer="peer-2", extra={"conversation_id": "thread-three"})
    with pytest.raises(ReplyCoordinationError, match="identity_reconciliation_required"):
        prepare(actor, latest, idempotency_key="cannot-launder-through-unassigned")
