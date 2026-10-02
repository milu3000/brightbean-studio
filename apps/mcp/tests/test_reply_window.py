"""Automated MCP replies cannot use Meta's human-only extended reply window."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apps.api_keys.services import issue_api_key
from apps.inbox.models import InboxMessage, InboxReply
from apps.inbox.services import create_reply_draft, send_reply_now, validate_automated_reply_window
from apps.mcp.handlers import _send_reply
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


@pytest.fixture
def context(user, organization):
    workspace = Workspace.objects.create(name="Automated reply safety", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    account = SocialAccount.objects.create(
        workspace=workspace, platform="instagram_login", account_platform_id="owned-ig", account_name="Offline IG"
    )
    key = issue_api_key(
        workspace=workspace,
        social_accounts=[account],
        issued_by=user,
        name="Offline test",
        permissions=["use_inbox", "reply_from_inbox"],
    ).api_key
    return {"api_key": key, "membership": membership, "workspace": workspace, "account": account}


@pytest.fixture
def message(context):
    return InboxMessage.objects.create(
        workspace=context["workspace"],
        social_account=context["account"],
        platform_message_id="inbound-1",
        message_type="dm",
        sender_name="Customer",
        sender_handle="customer-1",
        body="Question",
        received_at=NOW - timedelta(hours=1),
    )


def _args(message, mode):
    if mode == "new":
        return {"message_id": str(message.pk), "body": "Reply"}
    reply = create_reply_draft(message=message, body="Reply")
    if mode == "failed":
        reply.status = InboxReply.Status.FAILED
        reply.save(update_fields=["status"])
    return {"reply_id": str(reply.pk)}


@pytest.mark.parametrize("platform", ["facebook", "instagram", "instagram_login"])
@pytest.mark.parametrize("mode", ["new", "draft", "failed"])
@pytest.mark.parametrize(
    "received_at",
    [
        NOW - timedelta(hours=24),
        NOW - timedelta(hours=24, microseconds=1),
        datetime(1970, 1, 1, tzinfo=UTC),
        NOW + timedelta(microseconds=1),
        NOW + timedelta(minutes=5),
    ],
)
def test_invalid_window_cannot_reach_provider(context, message, platform, mode, received_at):
    context["account"].platform = platform
    context["account"].save(update_fields=["platform"])
    message.received_at = received_at
    message.save(update_fields=["received_at"])
    args = _args(message, mode)
    before = InboxReply.objects.count()
    with (
        patch("apps.inbox.services.timezone.now", return_value=NOW),
        patch("apps.inbox.services.get_provider") as provider,
        pytest.raises(JsonRpcError) as raised,
    ):
        _send_reply(args, context)
    assert raised.value.code == INVALID_PARAMS
    assert "24 hours" in raised.value.message
    assert InboxReply.objects.count() == before
    assert not InboxReply.objects.filter(status="sent").exists()
    provider.assert_not_called()


@pytest.mark.parametrize("mode", ["new", "draft", "failed"])
@pytest.mark.parametrize("age", [timedelta(0), timedelta(hours=24) - timedelta(microseconds=1)])
def test_recent_inbound_can_send_without_human_agent(context, message, mode, age):
    message.received_at = NOW - age
    message.save(update_fields=["received_at"])
    args = _args(message, mode)
    provider = MagicMock()
    provider.reply_to_message.return_value = SimpleNamespace(platform_message_id="outbound-1")
    with (
        patch("apps.inbox.services.timezone.now", return_value=NOW),
        patch("apps.inbox.services.get_provider", return_value=provider),
    ):
        _send_reply(args, context)
    assert provider.reply_to_message.call_args.kwargs["human_agent"] is False
    assert InboxReply.objects.get().status == "sent"


def test_window_rechecked_after_credentials_resolution(context, message):
    message.received_at = NOW - timedelta(hours=24) + timedelta(seconds=1)
    message.save(update_fields=["received_at"])
    clock = [NOW]
    provider = MagicMock()

    def resolve(_account):
        clock[0] += timedelta(seconds=2)
        return {}

    with (
        patch("apps.inbox.services.timezone.now", side_effect=lambda: clock[0]),
        patch("apps.publisher.engine._resolve_publish_credentials", side_effect=resolve),
        patch("apps.inbox.services.get_provider", return_value=provider),
        pytest.raises(JsonRpcError),
    ):
        _send_reply({"message_id": str(message.pk), "body": "Too late"}, context)
    provider.reply_to_message.assert_not_called()
    assert not InboxReply.objects.filter(status="sent").exists()


def test_explicit_human_service_path_keeps_human_agent_behavior(message):
    message.received_at = NOW - timedelta(hours=30)
    message.save(update_fields=["received_at"])
    reply = create_reply_draft(message=message, body="Human reply")
    provider = MagicMock()
    provider.reply_to_message.return_value = SimpleNamespace(platform_message_id="human-outbound-1")
    with (
        patch("apps.inbox.services.timezone.now", return_value=NOW),
        patch("apps.inbox.services.get_provider", return_value=provider),
    ):
        send_reply_now(reply)
    assert provider.reply_to_message.call_args.kwargs["human_agent"] is True


def test_naive_timestamp_is_not_guessed(message):
    message.received_at = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError, match="valid inbound message timestamp"):
        validate_automated_reply_window(message)


def test_old_comments_are_not_restricted(context, message):
    message.message_type = "comment"
    message.received_at = NOW - timedelta(days=20)
    message.save(update_fields=["message_type", "received_at"])
    provider = MagicMock()
    provider.reply_to_comment.return_value = SimpleNamespace(platform_message_id="comment-reply-1")
    with patch("apps.inbox.services.get_provider", return_value=provider):
        _send_reply({"message_id": str(message.pk), "body": "Comment reply"}, context)
    provider.reply_to_comment.assert_called_once()


def test_unsupported_automated_reply_is_failed_not_sent(context, message):
    with (
        patch("apps.inbox.services.timezone.now", return_value=NOW),
        patch("apps.inbox.services._dispatch_to_platform", side_effect=NotImplementedError),
        pytest.raises(JsonRpcError, match="does not support"),
    ):
        _send_reply({"message_id": str(message.pk), "body": "Cannot deliver"}, context)
    reply = InboxReply.objects.get()
    assert reply.status == "failed"
    assert reply.platform_reply_id == "" and reply.sent_at is None


@pytest.mark.parametrize("mode", ["new", "draft", "failed"])
def test_automated_reply_rechecks_account_connection(context, message, mode):
    args = _args(message, mode)
    # Keep the caller's account/message instances stale on purpose; the locked
    # send path must use the current account row, not cached provider access.
    SocialAccount.objects.filter(pk=context["account"].pk).update(connection_status="disconnected")
    with (
        patch("apps.inbox.services.timezone.now", return_value=NOW),
        patch("apps.inbox.services.get_provider") as provider,
        pytest.raises(JsonRpcError, match="not connected"),
    ):
        _send_reply(args, context)
    provider.assert_not_called()
    assert not InboxReply.objects.filter(status="sent").exists()
