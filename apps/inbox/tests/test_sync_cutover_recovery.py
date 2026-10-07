from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from apps.inbox.conversation_workflow import canonical_actionable_observed
from apps.inbox.durable_sync import claim_page, commit_page, start_scan
from apps.inbox.models import ConversationMessage, InboxConversation
from apps.inbox.sync_contracts import ConversationObservation, SyncPage
from apps.inbox.sync_cutover import establish_cutover, preview_cutover
from apps.inbox.sync_identity import SyncError
from apps.inbox.sync_provenance import apply_provenance, preview_provenance
from apps.inbox.tests.test_durable_pages_recovery import durable as _durable
from apps.inbox.tests.test_durable_pages_recovery import message

durable = _durable
pytestmark = pytest.mark.django_db


def bootstrap(binding):
    listing = start_scan(binding.pk, context="bootstrap")
    lease = claim_page(listing.pk)
    commit_page(lease, SyncPage((ConversationObservation("thread-1", ("page-1", "peer-1")),)))
    cp = binding.checkpoints.get(stream="messages")
    lease = claim_page(cp.pk)
    commit_page(lease, SyncPage((message(lease),)))
    return cp


def test_cutover_is_explicit_quiet_and_handles_arrivals_after_boundary(durable):
    cp = bootstrap(durable)
    event = Mock()
    canonical_actionable_observed.connect(event, weak=False)
    try:
        preview = preview_cutover(durable.pk)
        conversation = InboxConversation.objects.get()
        assert conversation.workflow_baseline_at is None and preview["suggested_mapping"] == {
            str(conversation.pk): None
        }
        baseline = establish_cutover(
            durable.pk, expected_fingerprint=preview["fingerprint"], workflow_mapping={str(conversation.pk): "done"}
        )
        assert event.call_count == 0 and ConversationMessage.objects.get().incoming_generation is None
        # Replay pre-cutover history quietly, discover a fresh post-cutover item.
        live = start_scan(durable.pk, context="live", stream="messages", scope_key=cp.scope_key)
        lease = claim_page(live.pk)
        commit_page(
            lease,
            SyncPage(
                (
                    message(lease),
                    replace(message(lease, mid="after"), occurred_at=baseline + timedelta(microseconds=1)),
                ),
            ),
        )
        assert event.call_count == 1
        conversation.refresh_from_db()
        assert conversation.workflow_state == "needs_action"
        next_live = start_scan(durable.pk, context="live", stream="messages", scope_key=cp.scope_key)
        lease = claim_page(next_live.pk)
        commit_page(
            lease, SyncPage((replace(message(lease, mid="after"), occurred_at=baseline + timedelta(microseconds=1)),))
        )
        assert event.call_count == 1
    finally:
        canonical_actionable_observed.disconnect(event)


def test_cutover_rejects_stale_preview_missing_mapping_and_unreviewed_partial(durable):
    bootstrap(durable)
    preview = preview_cutover(durable.pk)
    with pytest.raises(SyncError, match="mapping"):
        establish_cutover(durable.pk, expected_fingerprint=preview["fingerprint"], workflow_mapping={})
    InboxConversation.objects.update(revision=99)
    with pytest.raises(SyncError, match="snapshot_changed"):
        establish_cutover(
            durable.pk, expected_fingerprint=preview["fingerprint"], workflow_mapping=preview["suggested_mapping"]
        )
    durable.checkpoints.update(status="ready", coverage="partial")
    preview = preview_cutover(durable.pk)
    with pytest.raises(SyncError, match="partial_coverage"):
        establish_cutover(
            durable.pk, expected_fingerprint=preview["fingerprint"], workflow_mapping=preview["suggested_mapping"]
        )
    establish_cutover(
        durable.pk,
        expected_fingerprint=preview["fingerprint"],
        workflow_mapping=preview["suggested_mapping"],
        accept_partial=True,
    )
    assert InboxConversation.objects.get().workflow_state is None


def test_cutover_cannot_cross_inflight_page(durable):
    cp = start_scan(durable.pk, context="bootstrap")
    claim_page(cp.pk)
    preview = preview_cutover(durable.pk)
    with pytest.raises(SyncError, match="inflight"):
        establish_cutover(
            durable.pk, expected_fingerprint=preview["fingerprint"], workflow_mapping={}, accept_partial=True
        )


def test_provenance_preview_is_bounded_and_unknown_native_owner_stays_quarantined(durable):
    conversation = InboxConversation.objects.create(
        workspace=durable.workspace,
        social_account=durable.social_account,
        platform=durable.platform,
        platform_conversation_id="old-thread",
        identity_kind="platform",
        peer_id="peer-1",
        conversation_type="direct",
    )
    defaults = dict(
        workspace=durable.workspace,
        social_account=durable.social_account,
        platform=durable.platform,
        conversation=conversation,
        conversation_attribution="platform",
        direction="inbound",
        sender_id="peer-1",
        occurred_at=timezone.now() - timedelta(days=200),
        body="Old private text",
    )
    proven = ConversationMessage.objects.create(**defaults, platform_message_id="proven", recipient_id="page-1")
    unknown = ConversationMessage.objects.create(
        **defaults, platform_message_id="unknown", recipient_id="previous-owner"
    )
    preview = preview_provenance(durable.pk)
    assert not hasattr(proven, "observation_state")
    assert "Old private text" not in str(preview)
    result = apply_provenance(durable.pk, expected_fingerprint=preview["fingerprint"])
    assert result["bound"] == 1
    proven.refresh_from_db()
    unknown.refresh_from_db()
    assert proven.observation_state.expires_at < timezone.now() and proven.body == "Old private text"
    assert not hasattr(unknown, "observation_state") and unknown.body == "Old private text"
    assert proven.incoming_generation is None


def test_provenance_apply_rejects_changed_original(durable):
    preview = preview_provenance(durable.pk)
    durable.generation = __import__("uuid").uuid4()
    durable.save()
    with pytest.raises(SyncError, match="snapshot_changed"):
        apply_provenance(durable.pk, expected_fingerprint=preview["fingerprint"])
