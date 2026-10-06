"""REST and MCP expose one explicit follow-up intent without weakening send checks."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from apps.api.tests.test_inbox_thread_reads import client, message
from apps.api.tests.test_inbox_thread_reads import context as context_fixture
from apps.api_keys.services import issue_api_key
from apps.inbox.models import InboxReply
from apps.social_accounts.models import SocialAccount

context = context_fixture
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def sender(context):
    context.key = issue_api_key(
        workspace=context.workspace,
        social_accounts=[context.account],
        issued_by=context.user,
        name="Synthetic follow-up sender",
        permissions=["use_inbox", "reply_from_inbox"],
    )
    return context


def receipt(anchor, **values):
    return InboxReply.objects.create(
        inbox_message=anchor,
        body="Synthetic first reply",
        status="sent",
        sent_at=timezone.now(),
        platform_reply_id="synthetic-outbound-first",
        **values,
    )


def call(context, anchor, surface, *, body="Synthetic additional reply", parent=None, send=False, **extra):
    args = {"body": body, **extra}
    if parent is not None:
        args["follow_up_reply_id"] = str(parent.pk)
    if surface == "rest":
        response = client(context).post(
            f"/api/v1/inbox/{anchor.pk}/replies",
            data=json.dumps({**args, "send": send}),
            content_type="application/json",
            secure=True,
        )
        return response.status_code, response.json()
    response = client(context).post(
        "/api/v1/mcp/",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "send_reply" if send else "create_reply_draft",
                    "arguments": {"message_id": str(anchor.pk), **args},
                },
            }
        ),
        content_type="application/json",
        secure=True,
    )
    data = response.json()
    if "error" in data:
        return 422, data
    return 201, json.loads(data["result"]["content"][0]["text"])


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_explicit_follow_up_reuses_one_draft_then_sends_once(sender, surface):
    anchor = message(sender)
    parent = receipt(anchor)
    provider = Mock()
    provider.reply_to_message.return_value = SimpleNamespace(platform_message_id="synthetic-follow-up-sent")
    with patch("apps.inbox.services.get_provider", return_value=provider):
        denied, _ = call(sender, anchor, surface)
        assert denied >= 400
        status, first = call(sender, anchor, surface, parent=parent)
        assert status == 201, first
        status, repeated = call(sender, anchor, surface, parent=parent)
        assert status == 201 and first["id"] == repeated["id"]
        assert first["follow_up_reply_id"] == str(parent.pk)
        assert first["is_follow_up"] is True
        assert first["not_sent_verified"] is False
        provider.reply_to_message.assert_not_called()
        status, sent = call(sender, anchor, surface, parent=parent, send=True)
        assert status == 201, sent
        assert sent["id"] == first["id"] and sent["status"] == "sent"
        denied, _ = call(sender, anchor, surface, parent=parent, send=True)
        assert denied >= 400
        assert provider.reply_to_message.call_count == 1
    assert InboxReply.objects.filter(inbox_message=anchor).count() == 2


@pytest.mark.parametrize("surface", ["rest", "mcp"])
@pytest.mark.parametrize("scope", ["other_incoming", "other_account"])
def test_follow_up_parent_cannot_cross_incoming_or_allowlist(sender, surface, scope):
    anchor = message(sender)
    account = None
    if scope == "other_account":
        account = SocialAccount.objects.create(
            workspace=sender.workspace,
            platform="instagram_login",
            account_platform_id="synthetic-foreign-account",
            account_name="Synthetic foreign account",
        )
    foreign = message(sender, account=account)
    parent = receipt(foreign)
    with patch("apps.inbox.services.get_provider") as provider:
        denied, _ = call(sender, anchor, surface, parent=parent, send=True)
    assert denied >= 400
    provider.assert_not_called()
    assert not InboxReply.objects.filter(inbox_message=anchor).exists()


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_follow_up_never_overrides_uncertainty_or_group_identity(sender, surface):
    anchor = message(sender)
    parent = receipt(anchor)
    unresolved = InboxReply.objects.create(inbox_message=message(sender), body="Uncertain", status="unknown")
    with patch("apps.inbox.services.get_provider") as provider:
        denied, _ = call(sender, anchor, surface, parent=parent, send=True)
    assert denied >= 400
    provider.assert_not_called()
    assert InboxReply.objects.get(pk=unresolved.pk).status == "unknown"
    # Resolve only the synthetic fixture, not through a product uncertainty bypass.
    InboxReply.objects.filter(pk=unresolved.pk).update(status="sent", platform_reply_id="synthetic-proof")
    anchor.extra = {**anchor.extra, "conversation_type": "group", "classification_reason": "participants_group"}
    anchor.save(update_fields=["extra"])
    with patch("apps.inbox.services.get_provider") as provider:
        denied, _ = call(sender, anchor, surface, parent=parent, send=True)
    assert denied >= 400
    provider.assert_not_called()


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_drafting_permission_cannot_send_follow_up(context, surface):
    anchor = message(context)
    parent = receipt(anchor)
    status, draft = call(context, anchor, surface, parent=parent)
    assert status == 201, draft
    with patch("apps.inbox.services.get_provider") as provider:
        denied, _ = call(context, anchor, surface, parent=parent, send=True)
    assert denied >= 400
    provider.assert_not_called()


def test_mcp_existing_draft_cannot_replace_its_parent(sender):
    from apps.mcp.handlers import _send_reply
    from apps.mcp.protocol import JsonRpcError

    anchor = message(sender)
    draft = InboxReply.objects.create(inbox_message=anchor, body="Synthetic")
    parent = receipt(anchor)
    with pytest.raises(JsonRpcError, match="cannot be combined"):
        _send_reply(
            {"reply_id": str(draft.pk), "follow_up_reply_id": str(parent.pk)},
            {
                "api_key": sender.key.api_key,
                "membership": SimpleNamespace(effective_permissions={"reply_from_inbox": True}),
            },
        )
