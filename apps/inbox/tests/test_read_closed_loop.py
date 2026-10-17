"""Saved receipt IDs and executable read-only continuation guidance."""

import json
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.inbox.models import InboxConversation, InboxReply
from apps.inbox.tests.test_canonical_reads_rebuilt import legacy as old_anchor
from apps.inbox.tests.test_canonical_reads_rebuilt import proof, row
from apps.inbox.tests.test_inbox_intents import composer as composer
from apps.inbox.tests.test_inbox_intents import intent_context as intent_context
from apps.inbox.tests.test_inbox_intents import surface as surface
from apps.inbox.tests.test_simple_mcp_read import read
from apps.inbox.tests.test_unified_feed import account, legacy
from apps.mcp.conversation_tools import _get_conversation_attachments
from apps.mcp.handlers import _get_inbox_message
from apps.mcp.protocol import JsonRpcError
from apps.mcp.saved_inbox_tools import _read_intent
from apps.members.models import WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def follow(key, request):
    handlers = {
        "read_inbox": _read_intent,
        "get_inbox_message": _get_inbox_message,
        "get_conversation_attachments": _get_conversation_attachments,
    }
    assert request["tool"] in handlers
    member = WorkspaceMembership.objects.get(user_id=key.issued_by_id, workspace_id=key.workspace_id)
    value = handlers[request["tool"]](request["arguments"], {"api_key": key, "membership": member})
    return json.loads(value["content"][0]["text"])


def receipt_for(c, anchor, conversation=None):
    fields = {}
    if conversation:
        fields = {
            "conversation": conversation,
            "action_nonce": uuid4(),
            "account_platform_id": c.account.account_platform_id,
            "recipient_id": conversation.peer_id or "peer",
            "platform_conversation_id": conversation.platform_conversation_id,
        }
    return InboxReply.objects.create(
        inbox_message=anchor,
        status="sent",
        body="PRIVATE_RAW_RECEIPT",
        platform_reply_id="synthetic-old-reply",
        sent_at=timezone.now(),
        **fields,
    )


@pytest.mark.parametrize("with_conversation", [False, True])
def test_old_receipt_reference_and_ready_next_reads_complete_saved_content(intent_context, with_conversation):
    c, _client, key = intent_context
    anchor = old_anchor(c, c.row)
    receipt = receipt_for(c, anchor, c.conversation if with_conversation else None)
    body = "Saved canonical reply " * 400
    media = [{"type": "share", "url": f"https://example.com/closed/{i}", "title": f"Synthetic {i}"} for i in range(8)]
    outgoing = row(c, platform_message_id=receipt.platform_reply_id, legacy_reply=receipt, body=body, attachments=media)
    proof(c, outgoing)
    with (
        patch("httpx.Client.send", side_effect=AssertionError("No platform access")),
        patch("apps.inbox.services._dispatch_to_platform") as send,
    ):
        value = read(key, receipt.pk, limit=1)
        older = follow(key, value["next_read"]["history"])
        assert older["conversation"]["messages"][0]["id"] == str(c.row.pk)
        assert older["reference"] is None and not older["history"]["complete"]
        assert value["anchor_message"]["id"] == str(outgoing.pk)
        assert value["anchor_receipt"]["id"] == str(receipt.pk)
        assert "PRIVATE_RAW_RECEIPT" not in json.dumps(value)
        request = value["anchor_message"]["next_read"]["body"]
        chunks = []
        for _ in range(5):
            part = follow(key, request)
            chunks.append(part["content"]["body"])
            request = part["next_read"].get("body")
            if request is None:
                break
        assert request is None and "".join(chunks) == body
        request = value["anchor_message"]["next_read"]["attachments"]
        found = []
        for _ in range(5):
            part = follow(key, request)
            found.extend(part["items"])
            if not part["next_cursor"]:
                break
            request = {"tool": request["tool"], "arguments": {**request["arguments"], "cursor": part["next_cursor"]}}
        assert [item["url"] for item in found] == [item["url"] for item in media]
        send.assert_not_called()


def test_legacy_public_receipt_detail_is_discoverable_without_source_guessing(intent_context):
    c, _client, key = intent_context
    other = account(c, connection_status="connected")
    key.social_accounts.add(other)
    message = legacy(other, body="Synthetic legacy body " * 200)
    receipt = InboxReply.objects.create(inbox_message=message, status="sent", body="Synthetic old public reply " * 200)
    value = read(key, receipt.pk)
    assert value["account"]["id"] == str(other.pk)
    detail = follow(key, value["anchor_receipt"]["next_read"]["detail"])
    assert detail["body"] == message.body
    assert any(item["id"] == str(receipt.pk) and item["body"] == receipt.body for item in detail["replies"])


def test_unprojected_receipt_exposes_metadata_not_private_body_fallback(intent_context):
    c, _client, key = intent_context
    receipt = receipt_for(c, old_anchor(c, c.row))
    value = read(key, receipt.pk)
    assert value["anchor_receipt"]["status"] == "sent"
    assert value["anchor_message"] is None and not value["history"]["complete"]
    assert "PRIVATE_RAW_RECEIPT" not in json.dumps(value)
    with pytest.raises(JsonRpcError):
        read(key, receipt.pk, content="body")


@pytest.mark.parametrize("different_account", [False, True])
def test_receipt_conflicting_conversation_or_brand_is_rejected(intent_context, different_account):
    c, _client, key = intent_context
    owner = account(c, connection_status="connected") if different_account else c.account
    key.social_accounts.add(owner)
    other = InboxConversation.objects.create(
        workspace=owner.workspace,
        social_account=owner,
        platform=owner.platform,
        identity_kind="platform",
        platform_conversation_id="synthetic-other-thread",
        peer_id="other-peer",
    )
    receipt = receipt_for(c, old_anchor(c, c.row), other)
    with pytest.raises(JsonRpcError) as error:
        read(key, receipt.pk)
    assert error.value.data["error"] == "ambiguous_reference"


def test_old_receipt_id_colliding_with_another_brands_native_id_is_ambiguous(intent_context):
    c, _client, key = intent_context
    receipt = receipt_for(c, old_anchor(c, c.row))
    other = account(c, connection_status="connected")
    key.social_accounts.add(other)
    legacy(other, platform_message_id=str(receipt.pk))
    with pytest.raises(JsonRpcError) as error:
        read(key, receipt.pk)
    assert error.value.data["error"] == "ambiguous_reference"


def test_old_receipt_outside_allowlist_is_unavailable(intent_context):
    c, _client, key = intent_context
    other = account(c, connection_status="connected")
    receipt = InboxReply.objects.create(inbox_message=legacy(other), body="PRIVATE_OTHER_BRAND")
    with pytest.raises(JsonRpcError) as error:
        read(key, receipt.pk)
    assert error.value.data["error"] == "not_found_or_denied"


def test_ready_attachment_request_does_not_pre_authorize_revoked_access(intent_context):
    c, _client, key = intent_context
    message = row(c, attachments=[{"type": "share", "url": f"https://example.com/{i}"} for i in range(5)])
    proof(c, message)
    request = read(key, message.pk)["anchor_message"]["next_read"]["attachments"]
    key.social_accounts.clear()
    with pytest.raises(JsonRpcError):
        follow(key, request)
