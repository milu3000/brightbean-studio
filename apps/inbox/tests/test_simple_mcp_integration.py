"""Extra saved-content and history integration characterization."""

import json
from unittest.mock import patch

import pytest

from apps.inbox import canonical_reads as reader
from apps.inbox.models import ConversationReadState, InboxReply
from apps.inbox.tests.test_canonical_reads_rebuilt import proof, row
from apps.inbox.tests.test_inbox_intents import composer as composer
from apps.inbox.tests.test_inbox_intents import intent_context as intent_context
from apps.inbox.tests.test_inbox_intents import surface as surface
from apps.inbox.tests.test_simple_mcp_read import read
from apps.mcp.conversation_tools import _get_conversation_attachments
from apps.mcp.handlers import _get_inbox_message
from apps.mcp.protocol import JsonRpcError
from apps.members.models import WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def context_for(key):
    return {
        "api_key": key,
        "membership": WorkspaceMembership.objects.get(user_id=key.issued_by_id, workspace_id=key.workspace_id),
    }


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_long_text_service_and_legacy_detail_tool_keep_their_existing_contracts(intent_context, direction):
    c, _client, key = intent_context
    body = "開始" + "Synthetic long body. " * 300 + "結尾"
    message = row(
        c,
        direction=direction,
        body=body,
        sender_id=c.account.account_platform_id if direction == "outbound" else c.conversation.peer_id,
        recipient_id=c.conversation.peer_id if direction == "outbound" else c.account.account_platform_id,
    )
    proof(c, message)
    before = (InboxReply.objects.count(), ConversationReadState.objects.count())
    with (
        patch("httpx.Client.send", side_effect=AssertionError("Reads must not fetch the platform")),
        patch("apps.inbox.services._dispatch_to_platform") as send,
    ):
        data = read(key, message.pk)
        assert data["anchor_message"]["body_truncated"]
        chunks, cursor = [], None
        for _ in range(10):
            chunk = reader.read_message_body(reader.key_read_scope(key), message.pk, cursor=cursor)
            assert len(chunk["body"]) <= 2000
            chunks.append(chunk["body"])
            cursor = chunk["next_cursor"]
            if cursor is None:
                break
        assert cursor is None and "".join(chunks) == body
        if direction == "outbound":
            # The legacy message-detail contract remains incoming-only; the
            # common read_inbox body mode supplies the new bounded continuation.
            with pytest.raises(JsonRpcError):
                _get_inbox_message({"message_id": str(message.pk)}, context_for(key))
        send.assert_not_called()
    assert (InboxReply.objects.count(), ConversationReadState.objects.count()) == before


def test_attachment_previews_continue_via_existing_mcp_without_platform_fetch(intent_context):
    c, _client, key = intent_context
    attachments = [
        {"type": "share", "url": f"https://example.com/media/{i}", "title": f"Synthetic {i}"} for i in range(8)
    ]
    message = row(c, attachments=attachments)
    proof(c, message)
    items, cursor = [], None
    with (
        patch("httpx.Client.send", side_effect=AssertionError("Reads must not fetch the platform")),
        patch("apps.inbox.services._dispatch_to_platform") as send,
    ):
        assert read(key, message.pk)["anchor_message"]["attachments_truncated"]
        for _ in range(5):
            args = {"message_id": str(message.pk), "limit": 3}
            if cursor:
                args["cursor"] = cursor
            result = _get_conversation_attachments(args, context_for(key))
            data = json.loads(result["content"][0]["text"])
            assert len(data["items"]) <= 3
            items.extend(data["items"])
            cursor = data["next_cursor"]
            if cursor is None:
                break
        send.assert_not_called()
    assert cursor is None
    assert [item["url"] for item in items] == [item["url"] for item in attachments]
    assert len({item["url"] for item in items}) == 8


def test_three_history_pages_keep_native_replies_and_unknown_coverage_without_state_changes(intent_context):
    c, _client, key = intent_context
    expected = {str(c.row.pk)}
    for i in range(8):
        message = row(c, platform_message_id=f"synthetic-native-reply-{i}")
        proof(c, message)
        expected.add(str(message.pk))
    before = (InboxReply.objects.count(), ConversationReadState.objects.count(), c.conversation.workflow_state)
    seen, cursor = [], None
    with (
        patch("httpx.Client.send", side_effect=AssertionError("Reads must not fetch the platform")),
        patch("apps.inbox.services._dispatch_to_platform") as send,
    ):
        for _ in range(5):
            args = {"limit": 3}
            if cursor:
                args["cursor"] = cursor
            result = read(key, c.conversation.pk, **args)
            assert not result["history"]["complete"]
            assert result["history"]["coverage"]["coverage"] == "unknown"
            assert result["existing_replies"] == []
            assert "unanswered" not in result
            seen.extend(item["id"] for item in result["conversation"]["messages"])
            cursor = result["history"]["next_cursor"]
            if cursor is None:
                break
        send.assert_not_called()
    assert cursor is None and set(seen) == expected and len(seen) == len(expected)
    c.conversation.refresh_from_db()
    assert (InboxReply.objects.count(), ConversationReadState.objects.count(), c.conversation.workflow_state) == before
