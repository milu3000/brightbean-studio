"""Full-text continuation stays inside the existing one-reference MCP reader."""

import json
from unittest.mock import patch

import pytest

from apps.inbox.models import ConversationReadState, InboxReply
from apps.inbox.tests.test_canonical_reads_rebuilt import proof, row
from apps.inbox.tests.test_inbox_intents import composer as composer
from apps.inbox.tests.test_inbox_intents import intent_context as intent_context
from apps.inbox.tests.test_inbox_intents import surface as surface
from apps.inbox.tests.test_simple_mcp_read import read
from apps.mcp.protocol import JsonRpcError

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_same_entry_reads_full_text_in_bounded_pages_without_side_effects(intent_context, direction):
    c, _client, key = intent_context
    text = "開始" + "Synthetic long body. " * 500 + "結尾"
    message = row(
        c,
        direction=direction,
        body=text,
        sender_id=c.account.account_platform_id if direction == "outbound" else c.conversation.peer_id,
        recipient_id=c.conversation.peer_id if direction == "outbound" else c.account.account_platform_id,
    )
    proof(c, message)
    before = (InboxReply.objects.count(), ConversationReadState.objects.count(), c.conversation.workflow_state)
    chunks, cursor, offset = [], None, 0
    with (
        patch("httpx.Client.send", side_effect=AssertionError("No provider GET")),
        patch("apps.inbox.services._dispatch_to_platform") as send,
    ):
        assert read(key, message.pk)["anchor_message"]["body_truncated"]
        for _ in range(6):
            kwargs = {"content": "body"}
            if cursor:
                kwargs["cursor"] = cursor
            result = read(key, message.pk, **kwargs)
            content = result["content"]
            assert content["body_offset"] == offset and len(content["body"]) <= 4000
            chunks.append(content["body"])
            offset += len(content["body"])
            assert result["reference"] is None and not result["reply_status"]["send_authorized"]
            assert not result["history"]["complete"]
            cursor = result["next_cursor"]
            if cursor is None:
                break
        send.assert_not_called()
    assert cursor is None and "".join(chunks) == text
    c.conversation.refresh_from_db()
    assert (InboxReply.objects.count(), ConversationReadState.objects.count(), c.conversation.workflow_state) == before


def test_full_text_requires_an_explicit_message_not_a_guessed_latest_body(intent_context):
    c, _client, key = intent_context
    with pytest.raises(JsonRpcError) as error:
        read(key, c.conversation.pk, content="body")
    assert error.value.data["error"] == "message_reference_required"


@pytest.mark.parametrize("change", ["other_message", "changed_body", "revoked"])
def test_body_cursor_rechecks_original_message_content_and_scope(intent_context, change):
    c, _client, key = intent_context
    first = row(c, body="Synthetic text " * 700)
    proof(c, first)
    cursor = read(key, first.pk, content="body")["next_cursor"]
    assert cursor
    target = first.pk
    if change == "other_message":
        second = row(c, body="Another text " * 700)
        proof(c, second)
        target = second.pk
    elif change == "changed_body":
        first.body = "Changed text " * 700
        first.save(update_fields=["body", "updated_at"])
    else:
        key.social_accounts.clear()
    with pytest.raises(JsonRpcError):
        read(key, target, content="body", cursor=cursor)


def test_full_text_never_uses_retained_withdrawn_content(intent_context):
    c, _client, key = intent_context
    c.row.body = "PRIVATE_WITHDRAWN_TEXT"
    c.row.is_deleted = True
    c.row.save(update_fields=["body", "is_deleted"])
    result = read(key, c.row.pk, content="body")
    assert result["content"]["is_deleted"] and not result["content"]["available"]
    assert "PRIVATE_WITHDRAWN_TEXT" not in json.dumps(result)


def test_read_only_key_needs_no_send_permission_and_cannot_reuse_another_keys_cursor(intent_context):
    from apps.api_keys.services import issue_api_key

    c, _client, key = intent_context
    message = row(c, body="Synthetic long text " * 500)
    proof(c, message)
    cursor = read(key, message.pk, content="body")["next_cursor"]
    readonly = issue_api_key(
        workspace=c.account.workspace,
        social_accounts=[c.account],
        issued_by=c.user,
        name="Synthetic read-only full text",
        permissions=["use_inbox"],
    ).api_key
    result = read(readonly, message.pk, content="body")
    assert result["content"]["body"] and result["reference"] is None
    assert not result["reply_status"]["send_authorized"]
    with pytest.raises(JsonRpcError):
        read(readonly, message.pk, content="body", cursor=cursor)
