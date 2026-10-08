"""Offline end-to-end canonical signal, privacy and durable event contracts."""

import json
import uuid
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from apps.inbox.conversation_workflow import canonical_actionable_observed, canonical_incoming_observed
from apps.inbox.durable_sync import auth_fingerprint, claim_page, commit_page, start_scan
from apps.inbox.models import (
    ConversationMessage,
    ConversationObservationState,
    ConversationSyncIdentity,
    InboxConversation,
    InboxMessage,
    InboxSyncConnection,
)
from apps.inbox.sync_contracts import ConversationObservation, MessageObservation, SyncPage
from apps.inbox.sync_observations import canonical_content_restricted
from apps.mcp import events
from apps.mcp.events_canonical import SOURCE_UNAVAILABLE
from apps.mcp.models import EventOutbox, EventSubscription
from apps.mcp.tasks import process_delivery
from apps.mcp.tests import test_events as legacy_tests

context = legacy_tests.context
enabled = legacy_tests.enabled
params = legacy_tests.params
subscription = legacy_tests.subscription
verify = legacy_tests.verify

pytestmark = pytest.mark.django_db


@pytest.fixture
def flow(context, subscription, settings, enroll_conversation_accounts):
    settings.MCP_EVENTS_ENABLED = True
    settings.INBOX_DURABLE_SYNC_ENABLED = True
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    settings.INBOX_CONVERSATION_WORKFLOW_ENABLED = True
    settings.INBOX_CANONICAL_READ_ENABLED = True
    account = context["account"]
    enroll_conversation_accounts(account, read=True)
    baseline = timezone.now() - timedelta(hours=1)
    binding = InboxSyncConnection.objects.create(
        social_account=account,
        workspace=account.workspace,
        platform=account.platform,
        account_platform_id=account.account_platform_id,
        webhook_target_id=account.webhook_target_id,
        auth_fingerprint=auth_fingerprint(account),
        enabled=True,
        bootstrap_baseline_at=baseline,
    )
    subscription.started_at = baseline
    subscription.save(update_fields=["started_at"])
    return SimpleNamespace(account=account, binding=binding, sub=subscription, context=context, baseline=baseline)


def observation(flow, **changes):
    now = timezone.now()
    return replace(
        MessageObservation(
            "native-inbound-1",
            "native-thread-1",
            "native-peer-1",
            flow.account.account_platform_id,
            (flow.account.account_platform_id, "native-peer-1"),
            "private synthetic body",
            now - timedelta(minutes=1),
            now,
        ),
        **changes,
    )


def ingest(flow, item=None, *, context="live"):
    checkpoint = start_scan(flow.binding.pk, context=context, stream="messages", scope_key="native-thread-1")
    lease = claim_page(checkpoint.pk)
    item = replace(item, observed_at=timezone.now()) if item else observation(flow)
    commit_page(lease, SyncPage((item,)))
    return ConversationMessage.objects.get(
        platform_message_id=(item.platform_message_id if item else "native-inbound-1")
    )


def enqueue_again(row, *, source="webhook", revision=None):
    row.refresh_from_db()
    canonical_actionable_observed.send(
        sender="synthetic signal retry",
        message=row,
        source=source,
        event_revision=revision if revision is not None else row.incoming_generation,
    )


def test_page_enqueue_is_atomic_minimal_and_durable(flow, django_capture_on_commit_callbacks):
    with patch("apps.mcp.tasks.deliver_event") as queue, django_capture_on_commit_callbacks(execute=True):
        row = ingest(flow)
        assert EventOutbox.objects.count() == 1
        queue.assert_not_called()
    delivery = EventOutbox.objects.get()
    queue.assert_called_once_with(str(delivery.pk))
    assert delivery.message_id is None
    assert delivery.canonical_message_id == row.pk
    assert delivery.canonical_generation == flow.binding.generation
    assert delivery.canonical_event_revision == row.incoming_generation == 1
    assert not InboxMessage.objects.exists()
    payload = json.loads(delivery.payload)
    assert payload["data"] == {
        "message_id": str(row.pk),
        "workspace_id": str(row.workspace_id),
        "social_account_id": str(row.social_account_id),
        "conversation_id": str(row.conversation_id),
    }
    assert payload["timestamp"] == row.occurred_at.isoformat().replace("+00:00", "Z")
    assert "private synthetic body" not in delivery.payload and "native-peer" not in delivery.payload
    with patch("apps.mcp.tasks.post_signed", return_value=SimpleNamespace(status=204)) as callback:
        process_delivery(delivery.pk)
        process_delivery(delivery.pk)
    assert callback.call_count == 1
    delivery.refresh_from_db()
    assert delivery.status == "delivered"


def test_rollback_and_signal_failure_leave_no_event_or_message(flow, django_capture_on_commit_callbacks):
    with (
        patch("apps.mcp.tasks.deliver_event") as queue,
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        ingest(flow)
        assert EventOutbox.objects.exists()
        raise RuntimeError("synthetic rollback")
    assert not EventOutbox.objects.exists() and not ConversationMessage.objects.exists()
    queue.assert_not_called()

    with (
        patch("apps.mcp.events._enqueue_reference", side_effect=RuntimeError("synthetic disk error")),
        pytest.raises(RuntimeError),
    ):
        ingest(flow)
    assert not EventOutbox.objects.exists() and not ConversationMessage.objects.exists()


def test_poll_webhook_retries_keep_same_durable_event(flow):
    row = ingest(flow)
    initial = EventOutbox.objects.get()
    ingest(flow, observation(flow, source="webhook"))
    enqueue_again(row)
    assert EventOutbox.objects.count() == 1
    current = EventOutbox.objects.get()
    assert (current.event_id, current.payload, current.pk) == (initial.event_id, initial.payload, initial.pk)


def test_actual_legacy_mapping_preserves_its_real_id(flow):
    # A silently ingested history row can subsequently be verified live.
    row = ingest(flow, context="backfill")
    assert not EventOutbox.objects.exists()
    legacy = InboxMessage.objects.create(
        workspace=flow.account.workspace,
        social_account=flow.account,
        message_type="dm",
        platform_message_id=row.platform_message_id,
        body="existing projection",
        received_at=row.occurred_at,
    )
    ConversationMessage.objects.filter(pk=row.pk).update(legacy_message=legacy)
    row = ingest(flow)
    delivery = EventOutbox.objects.get()
    assert delivery.message_id == legacy.pk and delivery.canonical_message_id == row.pk
    assert json.loads(delivery.payload)["data"]["message_id"] == str(legacy.pk)
    events.enqueue_inbox_event(legacy)
    assert EventOutbox.objects.count() == InboxMessage.objects.count() == 1


@pytest.mark.parametrize("scan_context", ["bootstrap", "backfill", "repair"])
def test_nonlive_context_never_automates(flow, scan_context):
    row = ingest(flow, context=scan_context)
    assert row.incoming_generation is None
    assert not EventOutbox.objects.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"participant_ids": ()},
        {"participant_ids": ("events-account", "native-peer-1", "third-peer")},
        {"sender_id": "events-account", "recipient_id": "native-peer-1", "outbound_verified": True},
    ],
)
def test_unknown_group_and_outgoing_are_quiet(flow, changes):
    row = ingest(flow, observation(flow, **changes))
    canonical_incoming_observed.send(
        sender="synthetic read signal", conversation=row.conversation, message=row, source="webhook", event_revision=1
    )
    enqueue_again(row, revision=1)
    assert not EventOutbox.objects.exists()


def test_unknown_can_promote_once_without_replaying_read_event(flow):
    # Native thread participants are known; the message itself remains unknown.
    cp = start_scan(flow.binding.pk, context="live")
    lease = claim_page(cp.pk)
    commit_page(
        lease,
        SyncPage((ConversationObservation("native-thread-1", (flow.account.account_platform_id, "native-peer-1")),)),
    )
    row = ingest(flow, observation(flow, participant_ids=()))
    assert row.incoming_generation == 1 and not EventOutbox.objects.exists()
    row = ingest(flow)
    assert row.incoming_generation == 1 and EventOutbox.objects.count() == 1
    ingest(flow, observation(flow, source="webhook"))
    assert EventOutbox.objects.count() == 1


@pytest.mark.parametrize("source", ["app_send", "legacy_backfill", "unknown"])
def test_signal_source_does_not_bypass_live_contract(flow, source):
    row = ingest(flow)
    EventOutbox.objects.all().delete()
    enqueue_again(row, source=source)
    assert not EventOutbox.objects.exists()


@pytest.mark.parametrize("revision", [0, 2, True, "1"])
def test_signal_revision_must_match_fresh_persisted_message(flow, revision):
    row = ingest(flow)
    EventOutbox.objects.all().delete()
    enqueue_again(row, revision=revision)
    assert not EventOutbox.objects.exists()


@pytest.mark.parametrize("restriction", ["withdrawn", "expired"])
def test_withdrawal_or_expiry_cancels_pending_and_failed_never_delivered(flow, restriction):
    row = ingest(flow)
    delivery = EventOutbox.objects.get()
    for status in ("pending", "failed"):
        EventOutbox.objects.filter(pk=delivery.pk).update(status=status)
        state = row.observation_state
        if restriction == "withdrawn":
            state.withdrawn_at = timezone.now()
        else:
            state.expired_at = timezone.now() - timedelta(seconds=1)
        state.retained_body = "restricted synthetic secret"
        state.save()
        canonical_content_restricted.send(sender="synthetic persisted restriction", message=row, reason=restriction)
        delivery.refresh_from_db()
        assert delivery.status == "cancelled" and delivery.last_error == "content_" + restriction
        assert "restricted synthetic secret" not in delivery.payload
    with patch("apps.mcp.tasks.post_signed") as callback:
        process_delivery(delivery.pk)
    callback.assert_not_called()
    EventOutbox.objects.filter(pk=delivery.pk).update(status="delivered")
    canonical_content_restricted.send(sender="synthetic restriction replay", message=row, reason=restriction)
    delivery.refresh_from_db()
    assert delivery.status == "delivered"


@pytest.mark.parametrize("restriction", ["withdrawn", "expired"])
def test_delivery_rechecks_restriction_even_without_signal(flow, restriction):
    row = ingest(flow)
    delivery = EventOutbox.objects.get()
    changes = {"withdrawn_at": timezone.now()} if restriction == "withdrawn" else {"expired_at": timezone.now()}
    ConversationObservationState.objects.filter(message=row).update(**changes)
    with patch("apps.mcp.tasks.post_signed") as callback:
        process_delivery(delivery.pk)
    callback.assert_not_called()
    delivery.refresh_from_db()
    assert delivery.status == "cancelled" and delivery.attempts == 0


@pytest.mark.parametrize("mutation", ["state", "identity", "generation", "native"])
def test_missing_source_proof_stays_pending_and_can_recover(flow, mutation):
    row = ingest(flow)
    delivery = EventOutbox.objects.get()
    other = uuid.uuid4()
    if mutation == "state":
        model, pk, field, original = (
            ConversationObservationState,
            row.pk,
            "connection_generation",
            flow.binding.generation,
        )
    elif mutation == "identity":
        model, pk, field, original = (
            ConversationSyncIdentity,
            row.conversation_id,
            "connection_generation",
            flow.binding.generation,
        )
    elif mutation == "generation":
        model, pk, field, original = InboxSyncConnection, flow.binding.pk, "generation", flow.binding.generation
    else:
        model, pk, field, original = (
            type(flow.account),
            flow.account.pk,
            "account_platform_id",
            flow.account.account_platform_id,
        )
        other = "different-native-account"
    model.objects.filter(pk=pk).update(**{field: other})
    with patch("apps.mcp.tasks.post_signed") as callback:
        process_delivery(delivery.pk)
    callback.assert_not_called()
    delivery.refresh_from_db()
    assert delivery.status == "pending" and delivery.attempts == 0 and delivery.last_error == SOURCE_UNAVAILABLE
    model.objects.filter(pk=pk).update(**{field: original})
    EventOutbox.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now())
    with patch("apps.mcp.tasks.post_signed", return_value=SimpleNamespace(status=204)) as callback:
        process_delivery(delivery.pk)
    callback.assert_called_once()
    delivery.refresh_from_db()
    assert delivery.status == "delivered"


@pytest.mark.parametrize("mutation", ["direction", "row_type", "thread_type", "peer", "provider_id", "generation"])
def test_delivery_rechecks_direct_native_message_identity(flow, mutation):
    row = ingest(flow)
    delivery = EventOutbox.objects.get()
    if mutation == "thread_type":
        InboxConversation.objects.filter(pk=row.conversation_id).update(conversation_type="unknown")
    elif mutation == "generation":
        ConversationMessage.objects.filter(pk=row.pk).update(incoming_generation=99)
    else:
        fields = {
            "direction": "direction",
            "row_type": "conversation_type",
            "peer": "sender_id",
            "provider_id": "platform_message_id",
        }
        ConversationMessage.objects.filter(pk=row.pk).update(**{fields[mutation]: "changed"})
    with patch("apps.mcp.tasks.post_signed") as callback:
        process_delivery(delivery.pk)
    callback.assert_not_called()


def test_legacy_outbox_created_before_mapping_cannot_bypass_restriction(flow):
    row = ingest(flow)
    EventOutbox.objects.all().delete()
    legacy = InboxMessage.objects.create(
        workspace=flow.account.workspace,
        social_account=flow.account,
        message_type="dm",
        platform_message_id=row.platform_message_id,
        body="old retained text",
        received_at=row.occurred_at,
    )
    delivery = EventOutbox.objects.create(
        subscription=flow.sub,
        message=legacy,
        generation=flow.sub.generation,
        event_id="evt_" + legacy.pk.hex,
        payload="old minimal payload",
    )
    ConversationObservationState.objects.filter(message=row).update(withdrawn_at=timezone.now())
    with patch("apps.mcp.tasks.post_signed") as callback:
        process_delivery(delivery.pk)
    callback.assert_not_called()
    delivery.refresh_from_db()
    assert delivery.status == "cancelled" and delivery.last_error == "content_withdrawn"


def test_restriction_signal_cancels_old_unlinked_legacy_deliveries(flow):
    row = ingest(flow)
    legacy = InboxMessage.objects.create(
        workspace=flow.account.workspace,
        social_account=flow.account,
        message_type="dm",
        platform_message_id=row.platform_message_id,
        received_at=row.occurred_at,
    )
    old = EventOutbox.objects.create(
        subscription=flow.sub,
        message=legacy,
        generation=flow.sub.generation,
        event_id="evt_" + legacy.pk.hex,
        payload="old minimal payload",
    )
    ConversationObservationState.objects.filter(message=row).update(withdrawn_at=timezone.now())
    canonical_content_restricted.send(sender="synthetic", message=row, reason="withdrawn")
    assert set(EventOutbox.objects.values_list("status", flat=True)) == {"cancelled"}
    assert old.canonical_message_id is None


def test_subscription_generation_and_permissions_checked_for_canonical_delivery(flow):
    row = ingest(flow)
    delivery = EventOutbox.objects.get()
    flow.context["api_key"].social_accounts.clear()
    enqueue_again(row)
    with patch("apps.mcp.tasks.post_signed") as callback:
        process_delivery(delivery.pk)
    callback.assert_not_called()
    delivery.refresh_from_db()
    assert delivery.status == "cancelled"


def test_new_subscription_generation_gets_distinct_event_id(flow):
    row = ingest(flow)
    first = EventOutbox.objects.get()
    EventSubscription.objects.filter(pk=flow.sub.pk).update(generation=uuid.uuid4())
    enqueue_again(row)
    assert EventOutbox.objects.count() == 2
    assert EventOutbox.objects.exclude(pk=first.pk).get().event_id != first.event_id
    with patch("apps.mcp.tasks.post_signed") as callback:
        process_delivery(first.pk)
    callback.assert_not_called()
    first.refresh_from_db()
    assert first.status == "cancelled"


def test_canonical_only_outbox_is_protected_and_requires_a_message(flow):
    row = ingest(flow)
    with pytest.raises(ProtectedError):
        row.delete()
    with pytest.raises(IntegrityError), transaction.atomic():
        EventOutbox.objects.create(
            subscription=flow.sub, generation=flow.sub.generation, event_id="evt_invalid", payload="{}"
        )


def test_old_legacy_event_is_not_replayed_when_canonical_link_is_missing(flow):
    row = ingest(flow)
    EventOutbox.objects.all().delete()
    legacy = InboxMessage.objects.create(
        workspace=flow.account.workspace,
        social_account=flow.account,
        message_type="dm",
        platform_message_id=row.platform_message_id,
        received_at=row.occurred_at,
    )
    original = EventOutbox.objects.create(
        subscription=flow.sub,
        message=legacy,
        generation=flow.sub.generation,
        event_id="evt_" + legacy.pk.hex,
        payload="old minimal payload",
        status="delivered",
    )
    enqueue_again(row)
    assert EventOutbox.objects.get().pk == original.pk
    row.refresh_from_db()
    assert row.legacy_message_id is None


@pytest.mark.parametrize(
    "mutation", ["state_generation", "identity_generation", "native_identity", "key_permission", "future_subscription"]
)
def test_enqueue_requires_fresh_source_and_actor_proof(flow, mutation):
    row = ingest(flow)
    EventOutbox.objects.all().delete()
    if mutation == "state_generation":
        ConversationObservationState.objects.filter(message=row).update(connection_generation=uuid.uuid4())
    elif mutation == "identity_generation":
        ConversationSyncIdentity.objects.filter(conversation=row.conversation).update(
            connection_generation=uuid.uuid4()
        )
    elif mutation == "native_identity":
        type(flow.account).objects.filter(pk=flow.account.pk).update(webhook_target_id="another-native-owner")
    elif mutation == "key_permission":
        key = flow.context["api_key"]
        key.permissions = []
        key.save(update_fields=["permissions"])
    else:
        EventSubscription.objects.filter(pk=flow.sub.pk).update(started_at=timezone.now())
    enqueue_again(row)
    assert not EventOutbox.objects.exists()


def test_anonymous_read_signal_alone_cannot_enqueue_a_direct_row(flow):
    row = ingest(flow)
    EventOutbox.objects.all().delete()
    canonical_incoming_observed.send(
        sender="synthetic read-only signal", conversation=row.conversation, message=row, event_revision=1, source="poll"
    )
    assert not EventOutbox.objects.exists()


def test_canonical_retry_preserves_body_and_id_and_pending_proof_does_not_spend_attempt(flow):
    row = ingest(flow)
    delivery = EventOutbox.objects.get()
    with patch("apps.mcp.tasks.post_signed", return_value=SimpleNamespace(status=503)) as callback:
        process_delivery(delivery.pk)
        ConversationObservationState.objects.filter(message=row).update(connection_generation=uuid.uuid4())
        EventOutbox.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now())
        process_delivery(delivery.pk)
        assert callback.call_count == 1
        delivery.refresh_from_db()
        assert delivery.status == "pending" and delivery.attempts == 1
        ConversationObservationState.objects.filter(message=row).update(connection_generation=flow.binding.generation)
        EventOutbox.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now())
        callback.return_value = SimpleNamespace(status=204)
        process_delivery(delivery.pk)
    assert callback.call_args_list[0].args[3:5] == callback.call_args_list[1].args[3:5]
    delivery.refresh_from_db()
    assert delivery.status == "delivered" and delivery.attempts == 2


def test_content_restriction_receiver_requires_persisted_restriction(flow):
    row = ingest(flow)
    row.is_deleted = True
    canonical_content_restricted.send(sender="synthetic stale object", message=row, reason="withdrawn")
    assert EventOutbox.objects.get().status == "pending"


@pytest.mark.parametrize("gate", ["flag", "read_enrollment", "capture_enrollment", "history_flag"])
def test_temporary_reader_unavailability_persists_and_pauses_then_resumes(flow, settings, gate):
    names = {
        "flag": "INBOX_CANONICAL_READ_ENABLED",
        "history_flag": "INBOX_CONVERSATION_V2_ENABLED",
        "read_enrollment": "INBOX_CONVERSATION_V2_READ_ACCOUNTS",
        "capture_enrollment": "INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS",
    }
    # At observation time the reader may be unavailable even though capture is
    # active. Its event must already be durable when its live generation settles.
    original_read_flag = settings.INBOX_CANONICAL_READ_ENABLED
    settings.INBOX_CANONICAL_READ_ENABLED = False
    ingest(flow)
    delivery = EventOutbox.objects.get()
    settings.INBOX_CANONICAL_READ_ENABLED = original_read_flag
    name = names[gate]
    original = getattr(settings, name)
    setattr(settings, name, [] if "ACCOUNTS" in name else False)
    with patch("apps.mcp.tasks.post_signed") as callback:
        process_delivery(delivery.pk)
    callback.assert_not_called()
    delivery.refresh_from_db()
    assert delivery.status == "pending" and delivery.attempts == 0 and delivery.last_error == SOURCE_UNAVAILABLE
    setattr(settings, name, original)
    EventOutbox.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now())
    with patch("apps.mcp.tasks.post_signed", return_value=SimpleNamespace(status=204)) as callback:
        process_delivery(delivery.pk)
    callback.assert_called_once()
    delivery.refresh_from_db()
    assert delivery.status == "delivered"


def test_staged_retention_deadline_alone_does_not_cancel_delivery(flow):
    row = ingest(flow)
    delivery = EventOutbox.objects.get()
    ConversationObservationState.objects.filter(message=row).update(expires_at=timezone.now() - timedelta(days=1))
    with patch("apps.mcp.tasks.post_signed", return_value=SimpleNamespace(status=204)) as callback:
        process_delivery(delivery.pk)
    assert callback.call_count == 1
    delivery.refresh_from_db()
    assert delivery.status == "delivered"
