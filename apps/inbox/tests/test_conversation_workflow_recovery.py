"""New synthetic acceptance tests for the reconstructed workflow contract."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.utils import timezone

from apps.inbox.conversation_workflow import (
    LiveObservation,
    canonical_incoming_observed,
    mark_conversation_done,
    observe_canonical_message,
)
from apps.inbox.dm_send_gate import DMSendGateError
from apps.inbox.models import ConversationMessage, InboxConversation

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def workflow(inbox_account, settings):
    settings.INBOX_CONVERSATION_WORKFLOW_ENABLED = True
    now = timezone.now()
    conversation = InboxConversation.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform=inbox_account.platform,
        platform_conversation_id="synthetic-thread",
        peer_id="synthetic-peer",
        identity_kind="platform",
        conversation_type="direct",
        classification_reason="participants_pair",
        workflow_state="done",
        workflow_baseline_at=now - timedelta(hours=1),
        workflow_completed_at=now,
    )
    row = ConversationMessage.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform=inbox_account.platform,
        conversation=conversation,
        platform_message_id="synthetic-inbound",
        direction="inbound",
        conversation_type="direct",
        sender_id="synthetic-peer",
        recipient_id=inbox_account.account_platform_id,
        occurred_at=now - timedelta(minutes=1),
        body="Synthetic incoming",
        delivery_status="observed",
    )
    return SimpleNamespace(account=inbox_account, conversation=conversation, row=row, now=now)


def evidence(flow):
    return LiveObservation(flow.conversation.workflow_baseline_at, flow.now)


@pytest.mark.parametrize("source", ["poll", "webhook", "legacy_backfill", "app_send"])
def test_unspecified_observation_never_reopens(workflow, source):
    assert not observe_canonical_message(workflow.row, source=source, is_new=True)
    workflow.conversation.refresh_from_db()
    assert workflow.conversation.workflow_state == "done"
    assert workflow.conversation.incoming_generation == 0


def test_new_live_arrival_after_done_reopens_despite_older_provider_time_once(workflow):
    listener = Mock()
    canonical_incoming_observed.connect(listener, weak=False)
    try:
        assert observe_canonical_message(workflow.row, source="webhook", observation=evidence(workflow), is_new=True)
        assert not observe_canonical_message(workflow.row, source="poll", observation=evidence(workflow), is_new=True)
    finally:
        canonical_incoming_observed.disconnect(listener)
    workflow.conversation.refresh_from_db()
    workflow.row.refresh_from_db()
    assert workflow.conversation.workflow_state == "needs_action"
    assert workflow.conversation.incoming_generation == workflow.row.incoming_generation == 1
    assert listener.call_count == 1


@pytest.mark.parametrize("mutation", ["old", "future", "wrong_recipient", "deleted", "scope", "baseline"])
def test_invalid_live_evidence_is_quiet(workflow, mutation):
    row = workflow.row
    observation = evidence(workflow)
    if mutation == "old":
        row.occurred_at = workflow.conversation.workflow_baseline_at
    elif mutation == "future":
        row.occurred_at = workflow.now + timedelta(hours=1)
    elif mutation == "wrong_recipient":
        row.recipient_id = "wrong-own-account"
    elif mutation == "deleted":
        row.is_deleted = True
    elif mutation == "scope":
        row.platform = "wrong-platform"
    else:
        observation = LiveObservation(workflow.now, workflow.now)
    row.save()
    assert not observe_canonical_message(row, source="webhook", observation=observation, is_new=True)
    workflow.conversation.refresh_from_db()
    assert workflow.conversation.incoming_generation == 0


@pytest.mark.parametrize("kind", ["group", "unknown"])
def test_read_generation_does_not_grant_direct_workflow(workflow, kind):
    InboxConversation.objects.filter(pk=workflow.conversation.pk).update(conversation_type=kind, peer_id="")
    assert observe_canonical_message(workflow.row, source="webhook", observation=evidence(workflow), is_new=True)
    workflow.conversation.refresh_from_db()
    assert workflow.conversation.incoming_generation == 1
    assert workflow.conversation.workflow_state == "done"


def test_uncertain_done_requires_current_revision_and_explicit_review(workflow):
    conversation = workflow.conversation
    conversation.workflow_order_uncertain = True
    conversation.revision = 2
    conversation.save()
    for kwargs in ({}, {"confirm_order_uncertain": True}, {"confirm_order_uncertain": True, "expected_revision": 1}):
        with pytest.raises(DMSendGateError):
            mark_conversation_done(
                conversation=conversation, expected_generation=0, authorization=lambda account: None, **kwargs
            )
    result = mark_conversation_done(
        conversation=conversation,
        expected_generation=0,
        authorization=lambda account: None,
        confirm_order_uncertain=True,
        expected_revision=2,
    )
    assert result.workflow_state == "done"
    assert not result.workflow_order_uncertain
    assert result.revision == 3


def test_stale_done_cannot_acknowledge_new_incoming(workflow):
    observe_canonical_message(workflow.row, source="webhook", observation=evidence(workflow), is_new=True)
    with pytest.raises(DMSendGateError, match="New messages"):
        mark_conversation_done(
            conversation=workflow.conversation, expected_generation=0, authorization=lambda account: None
        )


def test_workflow_service_gates_default_off(workflow, settings):
    settings.INBOX_CONVERSATION_WORKFLOW_ENABLED = False
    assert not observe_canonical_message(workflow.row, source="webhook", observation=evidence(workflow), is_new=True)
    with pytest.raises(DMSendGateError, match="not enabled"):
        mark_conversation_done(
            conversation=workflow.conversation, expected_generation=0, authorization=lambda account: None
        )


def test_unknown_message_on_direct_thread_gets_activity_then_direct_promotion_once(workflow):
    ConversationMessage.objects.filter(pk=workflow.row.pk).update(conversation_type="unknown")
    first = observe_canonical_message(workflow.row, source="webhook", observation=evidence(workflow), is_new=True)
    assert first.read_observed and not first.actionable_observed
    workflow.conversation.refresh_from_db()
    assert workflow.conversation.workflow_state == "done"
    ConversationMessage.objects.filter(pk=workflow.row.pk).update(conversation_type="direct")
    promoted = observe_canonical_message(
        workflow.row, source="poll", observation=evidence(workflow), first_actionable=True
    )
    assert not promoted.read_observed and promoted.actionable_observed
    workflow.conversation.refresh_from_db()
    assert workflow.conversation.workflow_state == "needs_action"
    assert workflow.conversation.incoming_generation == 1


def test_action_promotion_without_a_live_read_generation_is_inert(workflow):
    assert not observe_canonical_message(
        workflow.row, source="poll", observation=evidence(workflow), first_actionable=True
    )
    workflow.conversation.refresh_from_db()
    assert workflow.conversation.workflow_state == "done"


@pytest.mark.parametrize("older", [True, False])
def test_native_outbound_requires_current_verified_order_and_never_advances_read(workflow, older):
    InboxConversation.objects.filter(pk=workflow.conversation.pk).update(
        workflow_state="needs_action", incoming_watermark_at=workflow.row.occurred_at
    )
    ConversationMessage.objects.filter(pk=workflow.row.pk).update(
        direction="outbound",
        sender_id=workflow.account.account_platform_id,
        recipient_id="synthetic-peer",
        occurred_at=workflow.row.occurred_at + timedelta(seconds=-10 if older else 10),
    )
    observe_canonical_message(workflow.row, source="webhook", observation=evidence(workflow), is_new=True)
    workflow.conversation.refresh_from_db()
    assert workflow.conversation.workflow_state == ("needs_action" if older else "waiting")
    assert workflow.conversation.incoming_generation == 0


def new_native_outbound(workflow):
    row = workflow.row
    row.direction = "outbound"
    row.sender_id = workflow.account.account_platform_id
    row.recipient_id = workflow.conversation.peer_id
    row.save()
    InboxConversation.objects.filter(pk=workflow.conversation.pk).update(
        workflow_state=None, workflow_completed_at=None
    )
    workflow.conversation.refresh_from_db()
    return row


def test_explicit_new_live_native_outbound_initializes_waiting_without_read_activity(workflow):
    from apps.inbox.conversation_workflow import canonical_actionable_observed

    row = new_native_outbound(workflow)
    listener = Mock()
    canonical_incoming_observed.connect(listener, weak=False)
    canonical_actionable_observed.connect(listener, weak=False)
    try:
        observe_canonical_message(
            row, source="webhook", observation=evidence(workflow), is_new=True, initialize_outbound=True
        )
    finally:
        canonical_incoming_observed.disconnect(listener)
        canonical_actionable_observed.disconnect(listener)
    workflow.conversation.refresh_from_db()
    row.refresh_from_db()
    assert workflow.conversation.workflow_state == "waiting"
    assert workflow.conversation.workflow_outbound_at == row.occurred_at
    assert workflow.conversation.incoming_generation == 0 and row.incoming_generation is None
    listener.assert_not_called()


@pytest.mark.parametrize("restriction", ["unspecified", "history", "older_context", "pre_cutover", "unknown_kind"])
def test_outbound_first_initialization_never_reclassifies_historical_or_unverified_hold(workflow, restriction):
    row = new_native_outbound(workflow)
    if restriction == "older_context":
        ConversationMessage.objects.create(
            workspace=workflow.account.workspace,
            social_account=workflow.account,
            platform=workflow.account.platform,
            conversation=workflow.conversation,
            platform_message_id="older-context",
            direction="outbound",
            sender_id=workflow.account.account_platform_id,
            recipient_id=workflow.conversation.peer_id,
        )
    if restriction == "pre_cutover":
        row.occurred_at = workflow.conversation.workflow_baseline_at
        row.save(update_fields=["occurred_at"])
    if restriction == "unknown_kind":
        row.conversation_type = "unknown"
        row.save(update_fields=["conversation_type"])
    observe_canonical_message(
        row,
        source="webhook",
        observation=None if restriction == "history" else evidence(workflow),
        is_new=True,
        initialize_outbound=restriction != "unspecified",
    )
    workflow.conversation.refresh_from_db()
    assert workflow.conversation.workflow_state is None
    assert workflow.conversation.incoming_generation == 0 and workflow.conversation.workflow_outbound_at is None


def test_equal_live_native_outbound_time_marks_order_uncertain_without_consuming_incoming(workflow):
    observe_canonical_message(workflow.row, source="webhook", observation=evidence(workflow), is_new=True)
    outbound = ConversationMessage.objects.create(
        workspace=workflow.account.workspace,
        social_account=workflow.account,
        platform=workflow.account.platform,
        conversation=workflow.conversation,
        platform_message_id="tied-native-outbound",
        direction="outbound",
        conversation_type="direct",
        sender_id=workflow.account.account_platform_id,
        recipient_id=workflow.conversation.peer_id,
        occurred_at=workflow.row.occurred_at,
        delivery_status="observed",
    )
    observe_canonical_message(outbound, source="webhook", observation=evidence(workflow), is_new=True)
    workflow.conversation.refresh_from_db()
    assert workflow.conversation.workflow_state == "needs_action"
    assert workflow.conversation.workflow_order_uncertain
    assert workflow.conversation.incoming_generation == 1
