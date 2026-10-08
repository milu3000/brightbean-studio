"""Signed unattributed messages are one visible ledger row without direct work."""

import hashlib
import hmac
import json
import uuid
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.conversation_workflow import canonical_actionable_observed, canonical_incoming_observed
from apps.inbox.durable_sync import claim_page, commit_page, start_scan
from apps.inbox.models import ConversationMessage, InboxConversation, InboxMessage, InboxSyncReceipt
from apps.inbox.sync_contracts import SyncPage
from apps.inbox.sync_ingestion import enqueue_message
from apps.inbox.tests.test_durable_pages_recovery import durable as _durable
from apps.inbox.tests.test_durable_pages_recovery import message
from apps.inbox.tests.test_sync_ingestion_recovery import instagram

pytestmark = pytest.mark.django_db
durable = _durable


def deliver(
    client,
    binding,
    settings,
    *,
    mid="unsigned-thread-mid",
    recipient="page-1",
    echo=False,
    group=False,
    delete=False,
    valid=True,
    object_name="page",
    target="page-1",
):
    platform = binding.platform
    secret = "synthetic-signed-app"
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {platform: {"app_secret": secret}}
    event = {
        "sender": {"id": "peer-1"},
        "message": {"mid": mid, "text": "SIGNED UNASSIGNED BODY", "is_echo": echo, "is_deleted": delete},
        "timestamp": int((timezone.now() - timedelta(seconds=1)).timestamp() * 1000),
    }
    if recipient:
        event["recipient"] = {"id": recipient}
    if group:
        event["participant_ids"] = ["page-1", "peer-1", "third-peer"]
    payload = {"object": object_name, "entry": [{"id": target, "messaging": [event]}]}
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest() if valid else "sha256=wrong"
    route = "webhook_instagram_login" if platform == "instagram_login" else "webhook_facebook"
    return client.post(
        reverse("inbox_webhooks:" + route), body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=signature
    )


def live(binding):
    binding.bootstrap_baseline_at = timezone.now() - timedelta(hours=1)
    binding.save(update_fields=["bootstrap_baseline_at"])


@pytest.mark.parametrize(
    "recipient,echo,group,direction,kind",
    [
        ("page-1", False, False, "inbound", "unknown"),
        ("", False, False, "unknown", "unknown"),
        ("another-peer", False, False, "unknown", "unknown"),
        ("page-1", False, True, "inbound", "group"),
        ("page-1", True, False, "unknown", "unknown"),
    ],
)
def test_signed_event_is_saved_without_guessing_thread_direction_or_work(
    durable, client, settings, recipient, echo, group, direction, kind
):
    live(durable)
    reads, actions = Mock(), Mock()
    canonical_incoming_observed.connect(reads, weak=False)
    canonical_actionable_observed.connect(actions, weak=False)
    try:
        response = deliver(client, durable, settings, recipient=recipient, echo=echo, group=group)
        assert response.status_code == 200
        row = ConversationMessage.objects.get()
        assert row.body == "SIGNED UNASSIGNED BODY" and row.conversation_id is None
        assert row.direction == direction and row.conversation_type == kind
        assert row.observation_state.connection_generation == durable.generation
        assert row.incoming_generation is None and row.observation_state.actionable_observed_at is None
        assert not InboxConversation.objects.exists() and not InboxMessage.objects.exists()
        receipt = InboxSyncReceipt.objects.get()
        assert receipt.status == "unassigned" and receipt.kind == "signed_message" and receipt.payload == {}
        assert reads.call_count == actions.call_count == 0
    finally:
        canonical_incoming_observed.disconnect(reads)
        canonical_actionable_observed.disconnect(actions)


def test_exact_native_mid_attribution_promotes_once_even_in_repair(durable, client, settings):
    live(durable)
    deliver(client, durable, settings)
    original = ConversationMessage.objects.get()
    original_updated = original.updated_at
    reads, actions = Mock(), Mock()
    canonical_incoming_observed.connect(reads, weak=False)
    canonical_actionable_observed.connect(actions, weak=False)
    try:
        for _ in range(2):
            cp = start_scan(durable.pk, stream="messages", scope_key="native-thread", context="repair")
            lease = claim_page(cp.pk)
            item = replace(
                message(lease, mid=original.platform_message_id), body=original.body, occurred_at=original.occurred_at
            )
            commit_page(lease, SyncPage((item,)))
        original.refresh_from_db()
        assert original.conversation.platform_conversation_id == "native-thread"
        assert original.updated_at > original_updated and ConversationMessage.objects.count() == 1
        assert original.incoming_generation == 1 and original.observation_state.actionable_observed_at
        assert reads.call_count == actions.call_count == 1
        assert InboxSyncReceipt.objects.get().status == "processed"
    finally:
        canonical_incoming_observed.disconnect(reads)
        canonical_actionable_observed.disconnect(actions)


def test_pre_cutover_signed_history_attribution_is_quiet(durable, client, settings):
    deliver(client, durable, settings)
    row = ConversationMessage.objects.get()
    live(durable)
    # The original receipt was captured as bootstrap and cannot promote via a repair.
    lease = claim_page(start_scan(durable.pk, stream="messages", scope_key="thread", context="repair").pk)
    commit_page(lease, SyncPage((replace(message(lease, mid=row.platform_message_id), occurred_at=row.occurred_at),)))
    row.refresh_from_db()
    assert row.incoming_generation is None and row.observation_state.actionable_observed_at is None


def test_invalid_signature_wrong_target_and_object_never_create_unassigned(durable, client, settings):
    assert deliver(client, durable, settings, valid=False).status_code == 403
    assert deliver(client, durable, settings, target="other-native").status_code == 200
    assert deliver(client, durable, settings, object_name="unrelated_object").status_code == 200
    assert not ConversationMessage.objects.exists()
    assert not InboxSyncReceipt.objects.filter(kind="signed_message").exists()


def test_unverified_internal_queue_input_is_not_provider_proof(durable):
    lease = claim_page(start_scan(durable.pk, stream="messages", scope_key="thread", context="backfill").pk)
    receipt = enqueue_message(durable.social_account, replace(message(lease), source="webhook", conversation_id=""))
    assert receipt.status == "awaiting_identity" and not ConversationMessage.objects.exists()


def test_signed_unassigned_withdrawal_preserves_original_and_never_resurrects(
    durable, client, settings, enroll_conversation_accounts
):
    account = instagram(durable)
    enroll_conversation_accounts(account)
    deliver(client, durable, settings, object_name="instagram")
    original = ConversationMessage.objects.get()
    deliver(client, durable, settings, object_name="instagram", delete=True)
    original.refresh_from_db()
    assert (
        original.is_deleted
        and original.body == ""
        and original.observation_state.retained_body == "SIGNED UNASSIGNED BODY"
    )
    lease = claim_page(start_scan(durable.pk, stream="messages", scope_key="thread", context="repair").pk)
    commit_page(lease, SyncPage((message(lease, mid=original.platform_message_id, body="NEVER RESTORE"),)))
    original.refresh_from_db()
    assert original.conversation_id and original.is_deleted and original.body == ""
    assert (
        original.observation_state.retained_body == "SIGNED UNASSIGNED BODY"
        and ConversationMessage.objects.count() == 1
    )


@pytest.mark.parametrize("mutation", ["generation", "native", "workspace"])
def test_generation_change_between_signed_route_and_commit_is_rejected(durable, client, settings, mutation):
    from apps.inbox.sync_ingestion import verified_meta_delivery

    def rebind(account, target):
        result = verified_meta_delivery(account, target)
        if mutation == "generation":
            type(durable).objects.filter(pk=durable.pk).update(generation=uuid.uuid4())
        elif mutation == "native":
            type(account).objects.filter(pk=account.pk).update(account_platform_id="replacement-native")
        else:
            from apps.workspaces.models import Workspace

            other = Workspace.objects.create(
                name="Synthetic replacement workspace", organization=account.workspace.organization
            )
            type(account).objects.filter(pk=account.pk).update(workspace=other)
        return result

    with patch("apps.inbox.sync_ingestion.verified_meta_delivery", side_effect=rebind):
        response = deliver(client, durable, settings)
    assert response.status_code == 200
    assert not ConversationMessage.objects.exists() and not InboxSyncReceipt.objects.exists()


def test_known_peer_does_not_supply_missing_native_thread(durable, client, settings):
    from apps.inbox.sync_contracts import ConversationObservation

    live(durable)
    lease = claim_page(start_scan(durable.pk, context="live").pk)
    commit_page(lease, SyncPage((ConversationObservation("known-thread", ("page-1", "peer-1")),)))
    deliver(client, durable, settings)
    row = ConversationMessage.objects.get()
    assert InboxConversation.objects.get().peer_id == "peer-1"
    assert row.conversation_id is None and row.conversation_type == "unknown" and row.incoming_generation is None


def test_repeated_signed_delivery_keeps_one_row_and_one_minimal_receipt(durable, client, settings):
    instant = timezone.now()
    with patch("django.utils.timezone.now", return_value=instant):
        deliver(client, durable, settings)
        deliver(client, durable, settings)
    assert ConversationMessage.objects.count() == InboxSyncReceipt.objects.count() == 1
    assert InboxSyncReceipt.objects.get().payload == {}


def test_unassigned_canonical_and_receipt_clear_are_one_transaction(durable, client, settings):
    from apps.inbox.sync_observations import reduce_observation

    def crash(*args, **kwargs):
        reduce_observation(*args, **kwargs)
        raise RuntimeError("synthetic commit failure")

    with patch("apps.inbox.sync_observations.reduce_observation", side_effect=crash), pytest.raises(RuntimeError):
        deliver(client, durable, settings)
    assert not ConversationMessage.objects.exists() and not InboxSyncReceipt.objects.exists()
    deliver(client, durable, settings)
    assert ConversationMessage.objects.count() == 1 and InboxSyncReceipt.objects.get().payload == {}


@pytest.mark.parametrize("native_proven", [True, False])
def test_first_tombstone_internal_viewer_recovers_only_proven_legacy_original(
    durable, client, settings, enroll_conversation_accounts, native_proven
):
    from apps.inbox.canonical_content import visible_content
    from apps.inbox.sync_observations import (
        withdrawn_content_available_for_internal_review,
        withdrawn_content_for_internal_review,
    )

    account = instagram(durable)
    enroll_conversation_accounts(account)
    legacy = InboxMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform_message_id="legacy-original",
        message_type="dm",
        sender_name="Peer",
        sender_handle="peer-1",
        body="CAPTURED LEGACY ORIGINAL",
        received_at=timezone.now() - timedelta(minutes=1),
        extra={"sender": {"id": "peer-1"}, "recipient": {"id": "page-1" if native_proven else "previous-native"}},
    )
    response = deliver(client, durable, settings, mid="legacy-original", object_name="instagram", delete=True)
    assert response.status_code == 200
    row = ConversationMessage.objects.get()
    assert row.is_deleted and row.observation_state.retained_body == ""
    assert row.observation_state.retained_legacy_body == "CAPTURED LEGACY ORIGINAL"
    legacy.refresh_from_db()
    assert legacy.body == "" and visible_content(row)["body"] == ""
    assert withdrawn_content_available_for_internal_review(row) is native_proven
    internal = withdrawn_content_for_internal_review(row)
    assert internal["body"] == ("CAPTURED LEGACY ORIGINAL" if native_proven else "")
    if native_proven:
        assert internal["body_source"] == "legacy_captured"


def test_withdrawal_preserves_transport_projection_exclusion(durable, client, settings, enroll_conversation_accounts):
    from apps.inbox.canonical_send_target import (
        exclude_transport_projections,
        is_transport_projection,
        transport_target,
    )

    account = instagram(durable)
    enroll_conversation_accounts(account)
    lease = claim_page(start_scan(durable.pk, stream="messages", scope_key="native-thread", context="backfill").pk)
    commit_page(lease, SyncPage((message(lease, mid="projected-mid", body="CANONICAL ORIGINAL"),)))
    row = ConversationMessage.objects.get()
    stub = transport_target(row, row.conversation, account, materialize=True)
    assert is_transport_projection(stub) and not exclude_transport_projections(InboxMessage.objects.all()).exists()
    deliver(client, durable, settings, mid="projected-mid", object_name="instagram", delete=True)
    stub.refresh_from_db()
    row.refresh_from_db()
    assert is_transport_projection(stub) and stub.extra["canonical_message_id"] == str(row.pk)
    assert not exclude_transport_projections(InboxMessage.objects.all()).exists()
    assert stub.body == row.body == "" and row.observation_state.retained_body == "CANONICAL ORIGINAL"
    assert row.incoming_generation is None and InboxMessage.objects.count() == ConversationMessage.objects.count() == 1
