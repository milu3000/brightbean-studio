"""Real canonical page commits keep existing owner transport facts current."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from apps.inbox.durable_sync import auth_fingerprint, claim_page, commit_page, start_scan
from apps.inbox.models import ConversationMessage, ConversationWorkState, InboxSyncConnection, SendOperation
from apps.inbox.sync_contracts import MessageObservation, SyncPage
from apps.inbox.sync_provenance import apply_provenance, preview_provenance
from apps.inbox.tests.test_dispatch_ownership import clock as _clock
from apps.inbox.tests.test_owned_composer_bridge import accepted, inputs, send
from apps.inbox.tests.test_owned_composer_bridge import owner as _owner

owner = _owner
clock = _clock
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def canonical_owner(owner, settings):
    settings.INBOX_DURABLE_SYNC_ENABLED = True
    owner.conversation.refresh_from_db()
    owner.binding = InboxSyncConnection.objects.create(
        social_account=owner.account,
        workspace=owner.account.workspace,
        platform=owner.account.platform,
        account_platform_id=owner.account.account_platform_id,
        auth_fingerprint=auth_fingerprint(owner.account),
        enabled=True,
        bootstrap_baseline_at=owner.conversation.workflow_baseline_at,
    )
    preview = preview_provenance(owner.binding.pk)
    apply_provenance(owner.binding.pk, expected_fingerprint=preview["fingerprint"])
    return owner


def observed(owner, *, context="live", item=None, **overrides):
    owner.clock.now += timedelta(seconds=2)
    cp = start_scan(owner.binding.pk, context=context, stream="messages", scope_key="synthetic-thread")
    lease = claim_page(cp.pk)
    item = item or MessageObservation(
        f"canonical-{uuid4()}",
        "synthetic-thread",
        "synthetic-peer",
        owner.account.account_platform_id,
        (owner.account.account_platform_id, "synthetic-peer"),
        "Actual canonical incoming",
        owner.clock.now,
        owner.clock.now,
        source="poll",
        snapshot_started_at=owner.clock.now,
    )
    item = replace(item, observed_at=owner.clock.now, snapshot_started_at=owner.clock.now, **overrides)
    commit_page(lease, SyncPage((item,), observed_at=owner.clock.now))
    owner.conversation.refresh_from_db()
    return ConversationMessage.objects.get(social_account=owner.account, platform_message_id=item.platform_message_id)


def test_new_durable_incoming_advances_real_owner_target_then_can_send(canonical_owner):
    owner = canonical_owner
    before = ConversationWorkState.objects.get(conversation=owner.conversation)
    row = observed(owner)
    current = ConversationWorkState.objects.get(conversation=owner.conversation)
    assert current.latest_incoming_id == row.pk and current.generation > before.generation
    assert current.conversation_revision == owner.conversation.revision and not current.history_gap
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        reply = send(owner, inputs(owner))
    assert reply.status == "sent" and provider.call_count == 1


def test_known_quiet_history_does_not_make_work_or_a_false_gap(canonical_owner):
    owner = canonical_owner
    before = ConversationWorkState.objects.get(conversation=owner.conversation)
    row = observed(owner, context="backfill", occurred_at=owner.clock.now - timedelta(hours=1))
    current = ConversationWorkState.objects.get(conversation=owner.conversation)
    assert row.incoming_generation is None
    assert (current.latest_incoming_id, current.generation, current.due_at) == (
        before.latest_incoming_id,
        before.generation,
        before.due_at,
    )
    assert current.conversation_revision == owner.conversation.revision and not current.history_gap


def test_real_gap_is_not_healed_by_a_new_canonical_page(canonical_owner):
    owner = canonical_owner
    ConversationWorkState.objects.filter(conversation=owner.conversation).update(conversation_revision=0)
    observed(owner)
    state = ConversationWorkState.objects.get(conversation=owner.conversation)
    assert state.history_gap and state.due_at is None
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(ValueError):
        send(owner, inputs(owner))
    provider.assert_not_called()


def test_native_external_outgoing_still_pauses_existing_owner(canonical_owner):
    owner = canonical_owner
    observed(
        owner, sender_id=owner.account.account_platform_id, recipient_id="synthetic-peer", body="Native app answer"
    )
    state = ConversationWorkState.objects.get(conversation=owner.conversation)
    assert state.owner_paused and state.pause_reason == "outgoing_observed"
    assert state.conversation_revision == owner.conversation.revision


def test_exact_confirmed_app_echo_does_not_become_external_takeover(canonical_owner):
    owner = canonical_owner
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted):
        reply = send(owner, inputs(owner))
    before = ConversationWorkState.objects.get(conversation=owner.conversation)
    actual = ConversationMessage.objects.get(legacy_reply=reply)
    echoed = observed(
        owner,
        platform_message_id=actual.platform_message_id,
        sender_id=owner.account.account_platform_id,
        recipient_id="synthetic-peer",
        body=actual.body,
        occurred_at=actual.occurred_at,
    )
    current = ConversationWorkState.objects.get(conversation=owner.conversation)
    assert echoed.pk == actual.pk and current.generation == before.generation
    assert (
        not current.owner_paused
        and not current.history_gap
        and current.conversation_revision == owner.conversation.revision
    )


def test_native_commit_preserves_unknown_attempt_barrier(canonical_owner):
    from providers.exceptions import ProviderError

    owner = canonical_owner

    def uncertain(*args, **kwargs):
        kwargs["before_provider"]()
        raise ProviderError("Synthetic unknown result")

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=uncertain), pytest.raises(ValueError):
        send(owner, inputs(owner))
    original = SendOperation.objects.get()
    assert original.status == "outcome_unknown"
    observed(owner)
    original.refresh_from_db()
    assert original.status == "outcome_unknown" and original.attempt.outcome == "unknown"
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(ValueError):
        send(owner, inputs(owner))
    provider.assert_not_called()


def test_new_canonical_incoming_during_http_keeps_its_pending_work(canonical_owner):
    owner = canonical_owner
    pending = {}

    def provider(*args, **kwargs):
        kwargs["before_provider"]()
        row = observed(owner)
        state = ConversationWorkState.objects.get(conversation=owner.conversation)
        pending.update(target=row.pk, generation=state.generation, due=state.due_at, burst=state.burst_started_at)
        return "synthetic-raced-own-receipt"

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=provider):
        reply = send(owner, inputs(owner))
    owner.conversation.refresh_from_db()
    state = ConversationWorkState.objects.get(conversation=owner.conversation)
    assert reply.status == "sent" and owner.conversation.workflow_state == "needs_action"
    assert pending["due"] is not None
    assert (state.latest_incoming_id, state.generation, state.due_at, state.burst_started_at) == (
        pending["target"],
        pending["generation"],
        pending["due"],
        pending["burst"],
    )
    assert not state.history_gap and state.conversation_revision == owner.conversation.revision
    # The provider can later refine an app receipt's native occurrence. That
    # echo must not settle the generation which arrived during the HTTP call.
    observed(
        owner,
        platform_message_id=reply.platform_reply_id,
        sender_id=owner.account.account_platform_id,
        recipient_id="synthetic-peer",
        body=reply.body,
        occurred_at=owner.clock.now,
    )
    owner.conversation.refresh_from_db()
    current = ConversationWorkState.objects.get(conversation=owner.conversation)
    assert owner.conversation.workflow_state == "needs_action"
    assert (current.latest_incoming_id, current.generation, current.due_at) == (
        pending["target"],
        pending["generation"],
        pending["due"],
    )


@pytest.mark.parametrize("automated", [False, True])
def test_quiet_newer_history_stays_quiet_but_fresh_human_action_can_answer(canonical_owner, automated):
    owner = canonical_owner
    before = ConversationWorkState.objects.get(conversation=owner.conversation)
    latest = observed(owner, context="backfill")
    current = ConversationWorkState.objects.get(conversation=owner.conversation)
    assert latest.incoming_generation is None and current.latest_incoming_id == before.latest_incoming_id
    assert (current.generation, current.due_at) == (before.generation, before.due_at)
    assert not current.history_gap and current.conversation_revision == owner.conversation.revision
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        if automated:
            with pytest.raises(ValueError):
                send(owner, inputs(owner), automated=True)
            provider.assert_not_called()
        else:
            sent = send(owner, inputs(owner))
            assert sent.status == "sent" and provider.call_count == 1
            assert SendOperation.objects.get(reply=sent).target_id == latest.pk


def test_unknown_to_direct_is_one_actionable_owner_transition(canonical_owner):
    owner = canonical_owner
    before = ConversationWorkState.objects.get(conversation=owner.conversation)
    unknown = observed(owner, participant_ids=(), conversation_type="unknown")
    during = ConversationWorkState.objects.get(conversation=owner.conversation)
    generation = unknown.incoming_generation
    assert generation is not None and during.latest_incoming_id == before.latest_incoming_id
    assert during.generation == before.generation
    known = observed(owner, platform_message_id=unknown.platform_message_id, occurred_at=unknown.occurred_at)
    promoted = ConversationWorkState.objects.get(conversation=owner.conversation)
    assert known.pk == unknown.pk and known.incoming_generation == generation
    assert promoted.latest_incoming_id == known.pk and promoted.generation == before.generation + 1
    observed(owner, platform_message_id=unknown.platform_message_id, occurred_at=unknown.occurred_at)
    replay = ConversationWorkState.objects.get(conversation=owner.conversation)
    assert (replay.generation, replay.due_at) == (promoted.generation, promoted.due_at)
    assert not replay.history_gap


def test_broken_transport_pointer_holds_send_without_rolling_back_capture(canonical_owner):
    owner = canonical_owner
    with (
        patch("apps.inbox.reply_dispatch.dispatch_reply", side_effect=ValueError("synthetic local stop")),
        pytest.raises(ValueError),
    ):
        send(owner, inputs(owner))
    operation = SendOperation.objects.get()
    ConversationWorkState.objects.filter(conversation=owner.conversation).update(active_operation=None)
    actual = observed(owner)
    state = ConversationWorkState.objects.get(conversation=owner.conversation)
    assert actual.body == "Actual canonical incoming" and actual.observation_state.actionable_observed_at
    assert state.history_gap and state.owner_paused and state.pause_reason == "owner_snapshot_conflict"
    operation.refresh_from_db()
    assert operation.status == "claimed" and operation.external_attempted_at is None
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(ValueError):
        send(owner, inputs(owner))
    provider.assert_not_called()
