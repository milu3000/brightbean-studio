from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.inbox.conversations import record_reply
from apps.inbox.durable_sync import claim_page, commit_page, start_scan
from apps.inbox.models import ConversationMessage, InboxMessage, InboxReply
from apps.inbox.sync_contracts import SyncPage
from apps.inbox.tests.test_durable_pages_recovery import durable as _durable
from apps.inbox.tests.test_durable_pages_recovery import message

durable = _durable
pytestmark = pytest.mark.django_db


def receipt(binding, *, provider_id="sent-id", valid_generation=True):
    lease = claim_page(start_scan(binding.pk, context="backfill", stream="messages", scope_key="thread-1").pk)
    commit_page(lease, SyncPage((message(lease),)))
    incoming = ConversationMessage.objects.get(platform_message_id="m-1")
    legacy = InboxMessage.objects.create(
        social_account=binding.social_account,
        workspace=binding.workspace,
        platform_message_id="m-1",
        message_type="dm",
        sender_handle="peer-1",
        received_at=incoming.occurred_at,
        extra={"sender_id": "peer-1", "participant_ids": ["page-1", "peer-1"]},
    )
    incoming.legacy_message = legacy
    incoming.save()
    return InboxReply.objects.create(
        inbox_message=legacy,
        action_nonce=uuid4(),
        conversation=incoming.conversation,
        account_platform_id="page-1",
        recipient_id="peer-1",
        platform_conversation_id="thread-1",
        connection_generation=binding.generation if valid_generation else None,
        body="Accepted text",
        status="sent",
        send_generation=1,
        platform_reply_id=provider_id,
        sent_at=timezone.now() - timedelta(seconds=1),
    )


def test_confirmed_app_receipt_is_visible_before_native_echo(durable):
    reply = receipt(durable)
    row = record_reply(reply)
    assert row.observation_state.connection_generation == durable.generation
    assert row.delivery_status == "provider_accepted" and row.sources == ["app_send"]
    assert row.incoming_generation is None and row.body == "Accepted text"
    lease = claim_page(start_scan(durable.pk, context="repair", stream="messages", scope_key="thread-1").pk)
    commit_page(
        lease,
        SyncPage(
            (
                replace(
                    message(lease, mid="sent-id"),
                    sender_id="page-1",
                    recipient_id="peer-1",
                    body="Accepted text",
                    occurred_at=reply.sent_at,
                ),
            )
        ),
    )
    row.refresh_from_db()
    assert row.delivery_status == "observed" and row.direction == "outbound"
    assert ConversationMessage.objects.filter(platform_message_id="sent-id").count() == 1


def test_wrong_generation_receipt_never_gets_current_provenance(durable):
    row = record_reply(receipt(durable, valid_generation=False))
    assert not hasattr(row, "observation_state")


def test_selected_send_intent_does_not_guess_same_peer_native_placement(durable):
    reply = receipt(durable)
    lease = claim_page(start_scan(durable.pk, context="backfill", stream="messages", scope_key="thread-2").pk)
    commit_page(lease, SyncPage((message(lease, mid="m-2"),)))
    row = record_reply(reply)
    assert (
        row.conversation_id == reply.conversation_id
        and row.observation_state.connection_generation == durable.generation
    )
    assert row.conversation_attribution == "" and row.delivery_status == "provider_accepted"
