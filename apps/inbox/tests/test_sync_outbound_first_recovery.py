"""Native outbound-first discovery initializes only a proven new live thread."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from apps.inbox.conversation_workflow import canonical_actionable_observed, canonical_incoming_observed
from apps.inbox.durable_sync import claim_page, commit_page, start_scan
from apps.inbox.models import ConversationMessage, ConversationWorkState, InboxSyncConnection
from apps.inbox.sync_contracts import MessageObservation, SyncPage
from apps.inbox.tests.test_durable_pages_recovery import durable as _durable

durable = _durable
pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "context,kind,old,expected",
    [
        ("live", "direct", False, "waiting"),
        ("bootstrap", "direct", False, None),
        ("backfill", "direct", False, None),
        ("repair", "direct", False, None),
        ("live", "unknown", False, None),
        ("live", "group", False, None),
        ("live", "direct", True, None),
    ],
)
def test_first_native_outbound_has_no_incoming_work(durable, context, kind, old, expected):
    now = timezone.now()
    baseline = now - timedelta(minutes=2)
    InboxSyncConnection.objects.filter(pk=durable.pk).update(bootstrap_baseline_at=baseline)
    lease = claim_page(start_scan(durable.pk, context=context, stream="messages", scope_key="native-outbound").pk)
    item = MessageObservation(
        "own-native-first",
        lease.scope_key,
        "page-1",
        "peer-1",
        ("page-1", "peer-1") if kind == "direct" else (),
        "Native first message",
        baseline - timedelta(seconds=1) if old else now,
        timezone.now(),
        source="poll",
        conversation_type=kind,
    )
    incoming, actionable = Mock(), Mock()
    canonical_incoming_observed.connect(incoming, weak=False)
    canonical_actionable_observed.connect(actionable, weak=False)
    try:
        commit_page(lease, SyncPage((item,)))
        row = ConversationMessage.objects.get(platform_message_id=item.platform_message_id)
        assert row.direction == "outbound" and row.delivery_status == "observed"
        assert row.conversation.workflow_state == expected
        assert row.conversation.incoming_generation == 0 and row.incoming_generation is None
        assert not ConversationWorkState.objects.exists()
        assert incoming.call_count == actionable.call_count == 0
    finally:
        canonical_incoming_observed.disconnect(incoming)
        canonical_actionable_observed.disconnect(actionable)


def test_existing_quiet_outbound_history_keeps_its_reviewed_hold(durable):
    now = timezone.now()
    InboxSyncConnection.objects.filter(pk=durable.pk).update(bootstrap_baseline_at=now - timedelta(minutes=2))
    cp = start_scan(durable.pk, context="backfill", stream="messages", scope_key="old-outbound")
    lease = claim_page(cp.pk)
    item = MessageObservation(
        "older-own",
        lease.scope_key,
        "page-1",
        "peer-1",
        ("page-1", "peer-1"),
        "Saved outbound history",
        now - timedelta(minutes=1),
        timezone.now(),
    )
    commit_page(lease, SyncPage((item,)))
    lease = claim_page(start_scan(durable.pk, context="live", stream="messages", scope_key=cp.scope_key).pk)
    commit_page(
        lease, SyncPage((replace(item, platform_message_id="newer-own", occurred_at=now, observed_at=timezone.now()),))
    )
    assert ConversationMessage.objects.get(platform_message_id="newer-own").conversation.workflow_state is None


@pytest.mark.parametrize("newest_first", [False, True])
@pytest.mark.parametrize("equal", [False, True])
def test_atomic_native_page_orders_reliable_two_way_activity(durable, newest_first, equal):
    now = timezone.now()
    InboxSyncConnection.objects.filter(pk=durable.pk).update(bootstrap_baseline_at=now - timedelta(minutes=2))
    lease = claim_page(start_scan(durable.pk, context="live", stream="messages", scope_key="two-way").pk)
    inbound = MessageObservation(
        "incoming-first",
        lease.scope_key,
        "peer-1",
        "page-1",
        ("page-1", "peer-1"),
        "Question",
        now - timedelta(seconds=2),
        timezone.now(),
    )
    outbound = replace(
        inbound,
        platform_message_id="native-answer",
        sender_id="page-1",
        recipient_id="peer-1",
        body="Native answer",
        occurred_at=inbound.occurred_at if equal else now,
    )
    commit_page(lease, SyncPage((outbound, inbound) if newest_first else (inbound, outbound)))
    conversation = ConversationMessage.objects.get(platform_message_id=inbound.platform_message_id).conversation
    assert conversation.workflow_state == ("needs_action" if equal else "waiting")
    assert conversation.workflow_order_uncertain is equal
    assert conversation.incoming_generation == 1
