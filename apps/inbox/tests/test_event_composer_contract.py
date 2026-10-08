"""Existing event references resolve into typed canonical actions without resend."""

import json
from unittest.mock import patch

import pytest
from django.test import Client

from apps.inbox.conversations import link_legacy_message
from apps.inbox.models import InboxMessage, InboxReply
from apps.inbox.tests.test_composer_surfaces_recovery import payload, rpc
from apps.inbox.tests.test_native_quote_recovery import native_provider
from apps.mcp import events
from apps.mcp.models import EventOutbox
from apps.mcp.tests import test_canonical_events_recovery as canonical_events
from apps.mcp.tests import test_events as legacy_events

base_context = legacy_events.context
enabled = legacy_events.enabled
params = legacy_events.params
verify = legacy_events.verify
subscription = legacy_events.subscription
flow = canonical_events.flow
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def context(base_context):
    # This isolated fixture represents an existing full inbox credential. No
    # runtime subscription or API grant is created/expanded by the application.
    key = base_context["api_key"]
    key.permissions = ["use_inbox", "reply_from_inbox"]
    key.save(update_fields=["permissions"])
    return base_context


@pytest.mark.parametrize("namespace", ["legacy", "canonical"])
def test_event_reference_read_save_send_and_identical_retry_dispatch_once(flow, namespace, settings):
    settings.INBOX_CONVERSATION_COMPOSER_ENABLED = True
    value = canonical_events.observation(flow)
    real_legacy = None
    with patch("apps.mcp.events._queue_outbox"):
        if namespace == "legacy":
            # The already-supported legacy producer emits the real incoming
            # UUID before canonical capture owns this account.
            flow.binding.enabled = False
            flow.binding.bootstrap_baseline_at = None
            flow.binding.save(update_fields=["enabled", "bootstrap_baseline_at"])
            real_legacy = InboxMessage.objects.create(
                workspace=flow.account.workspace,
                social_account=flow.account,
                platform_message_id=value.platform_message_id,
                message_type="dm",
                sender_name="Synthetic sender",
                sender_handle=value.sender_id,
                body=value.body,
                received_at=value.occurred_at,
                extra={
                    "conversation_id": value.conversation_id,
                    "participant_ids": list(value.participant_ids),
                    "sender_id": value.sender_id,
                    "message_recipient_id": value.recipient_id,
                },
            )
            events.enqueue_inbox_event(real_legacy)
            assert EventOutbox.objects.count() == 1
            flow.binding.enabled = True
            flow.binding.bootstrap_baseline_at = flow.baseline
            flow.binding.save(update_fields=["enabled", "bootstrap_baseline_at"])
        row = canonical_events.ingest(flow, value)
        if real_legacy is not None:
            link_legacy_message(row, real_legacy)
            row.refresh_from_db()
    assert EventOutbox.objects.count() == 1
    event = json.loads(EventOutbox.objects.get().payload)
    expected_id = real_legacy.pk if real_legacy is not None else row.pk
    assert event["name"] == "inbox.dm.received"
    assert event["data"]["message_id"] == str(expected_id)
    assert value.body not in json.dumps(event)
    token = flow.context["request"].META["HTTP_AUTHORIZATION"]
    client = Client(HTTP_AUTHORIZATION=token)
    incoming = rpc(client, "get_inbox_message", {"message_id": event["data"]["message_id"]})
    assert incoming["id"] == str(expected_id)
    assert incoming["body"] == value.body
    conversation_id = incoming["canonical_conversation_id"]
    assert conversation_id == str(row.conversation_id)
    # Old writes stay held and explain the machine-callable transition.
    refused = client.post(
        "/api/v1/mcp/",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "create_reply_draft",
                    "arguments": {"message_id": str(expected_id), "body": "Legacy write"},
                },
            }
        ),
        content_type="application/json",
        secure=True,
    ).json()
    data = refused["error"]["data"]
    assert data["error"] == "canonical_composer_required"
    assert data["conversation_id"] == conversation_id
    assert data["composer_tool"] == "get_inbox_conversation_composer"
    state = rpc(client, data["composer_tool"], {"conversation_id": conversation_id})
    action = {"conversation_id": conversation_id, **payload(state)}
    draft = rpc(client, data["draft_tool"], action)
    state = rpc(client, data["composer_tool"], {"conversation_id": conversation_id})
    action = {"conversation_id": conversation_id, **payload(state)}
    provider = native_provider("facebook")
    with (
        patch("apps.inbox.services.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
    ):
        sent = rpc(client, data["send_tool"], action)
        retry = rpc(client, data["send_tool"], action)
    assert sent["id"] == retry["id"] == draft["id"]
    assert sent["status"] == "sent" and provider._request.call_count == 1
    assert InboxReply.objects.filter(conversation_id=conversation_id).count() == 1
