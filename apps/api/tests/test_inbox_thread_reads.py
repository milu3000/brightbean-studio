"""One stored history projection, with unchanged account permission boundaries."""

import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.api_keys.services import issue_api_key
from apps.inbox.models import ConversationMessage, DMConversationOwnership, DMSendControl, InboxMessage, InboxReply
from apps.inbox.thread_reads import read_stored_thread
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def context():
    user = User.objects.create_user(email="thread-reader@example.com", password="test", tos_accepted_at=timezone.now())
    org = Organization.objects.create(name="Synthetic thread org")
    workspace = Workspace.objects.create(organization=org, name="Synthetic thread workspace")
    OrgMembership.objects.create(organization=org, user=user, org_role="owner")
    WorkspaceMembership.objects.create(workspace=workspace, user=user, workspace_role="owner")
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram_login",
        account_platform_id="synthetic-own",
        account_name="Synthetic account",
        oauth_access_token="synthetic-test-only",
        connection_status="connected",
    )
    key = issue_api_key(
        workspace=workspace, social_accounts=[account], issued_by=user, name="Thread reader", permissions=["use_inbox"]
    )
    return SimpleNamespace(user=user, org=org, workspace=workspace, account=account, key=key)


def message(context, *, account=None, native="synthetic-thread", offset=0, **overrides):
    account = account or context.account
    values = {
        "workspace": account.workspace,
        "social_account": account,
        "platform_message_id": str(uuid4()),
        "message_type": "dm",
        "sender_name": "Synthetic peer",
        "sender_handle": "synthetic-peer",
        "body": "Synthetic question",
        "received_at": timezone.now() + timedelta(seconds=offset),
        "extra": {
            "conversation_id": native,
            "sender_id": "synthetic-peer",
            "conversation_type": "direct",
            "classification_reason": "participants_pair",
        },
    }
    values.update(overrides)
    return InboxMessage.objects.create(**values)


def client(context):
    return Client(HTTP_AUTHORIZATION=f"Bearer {context.key.plaintext_token}")


def rest(context, anchor, **query):
    return client(context).get(f"/api/v1/inbox/{anchor.pk}/thread", query, secure=True)


def mcp(context, anchor, **params):
    response = client(context).post(
        "/api/v1/mcp/",
        content_type="application/json",
        secure=True,
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_inbox_thread", "arguments": {"message_id": str(anchor.pk), **params}},
            }
        ),
    )
    data = response.json()
    return json.loads(data["result"]["content"][0]["text"]) if "result" in data else data


def test_rest_mcp_share_exact_thread_and_do_not_change_work_or_capture(context):
    anchor, newer = message(context), message(context, offset=1)
    message(context, native="different-native")
    foreign_account = SocialAccount.objects.create(
        workspace=context.workspace, platform="instagram_login", account_platform_id="other-own", account_name="Other"
    )
    foreign = message(context, account=foreign_account)
    reply = InboxReply.objects.create(inbox_message=anchor, body="Saved draft")
    before = list(InboxMessage.objects.order_by("pk").values("pk", "status", "extra"))
    response = rest(context, anchor)
    assert response.status_code == 200
    data = response.json()
    assert data == mcp(context, anchor)
    assert [row["id"] for row in data["messages"]] == [str(newer.pk), str(anchor.pk)]
    assert data["history_complete"] is False
    assert data["outbound_coverage"] == "brightbean_replies_only"
    assert data["messages"][1]["replies"][0]["id"] == str(reply.pk)
    assert data["messages"][1]["reply_eligibility"]["requires_current_authorization"] is True
    assert rest(context, foreign).status_code == 404
    assert "not found" in mcp(context, foreign)["error"]["message"].lower()
    assert list(InboxMessage.objects.order_by("pk").values("pk", "status", "extra")) == before
    assert not ConversationMessage.objects.exists()
    assert not DMSendControl.objects.exists()
    assert not DMConversationOwnership.objects.exists()


@pytest.mark.parametrize("native", [None, "", [], {}, True, "thread with spaces", "x" * 256])
def test_missing_or_invalid_native_id_never_groups_by_peer(context, native):
    anchor = message(context, native=native)
    message(context, native=native, offset=1)
    data = rest(context, anchor).json()
    assert data["grouping"] == "single_message"
    assert [row["id"] for row in data["messages"]] == [str(anchor.pk)]


def test_cursor_pages_older_rows_without_repeating_after_new_arrival(context):
    oldest, anchor, newest = message(context, offset=-2), message(context, offset=-1), message(context)
    first = rest(context, anchor, limit=1).json()
    assert first["messages"][0]["id"] == str(newest.pk)
    message(context, offset=1)
    second = rest(context, anchor, limit=1, cursor=first["next_cursor"]).json()
    third = rest(context, anchor, limit=1, cursor=second["next_cursor"]).json()
    assert second["messages"][0]["id"] == str(anchor.pk)
    assert third["messages"][0]["id"] == str(oldest.pk)
    assert third["next_cursor"] is None


def test_cursor_binds_actor_anchor_and_current_identity(context):
    anchor = message(context)
    message(context, offset=1)
    cursor = rest(context, anchor, limit=1).json()["next_cursor"]
    other = issue_api_key(
        workspace=context.workspace,
        social_accounts=[context.account],
        issued_by=context.user,
        name="Other reader",
        permissions=["use_inbox"],
    )
    with pytest.raises(ValueError, match="current caller"):
        read_stored_thread(anchor, actor_id=other.api_key.id, cursor=cursor)
    anchor.extra["conversation_id"] = "moved-thread"
    anchor.save(update_fields=["extra"])
    assert rest(context, anchor, cursor=cursor).status_code == 422
    context.key.api_key.social_accounts.clear()
    assert rest(context, anchor, cursor=cursor).status_code == 404


def test_history_projection_does_not_depend_on_presentation_or_capture(context, settings):
    settings.INBOX_CONVERSATION_PRESENTATION_ENABLED = False
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    anchor = message(context)
    message(context, offset=1)
    assert len(rest(context, anchor).json()["messages"]) == 2


def test_account_workspace_move_revokes_stale_key_message_and_thread_reads(context):
    anchor = message(context)
    foreign_workspace = Workspace.objects.create(organization=context.org, name="Another workspace")
    SocialAccount.objects.filter(pk=context.account.pk).update(workspace=foreign_workspace)
    assert rest(context, anchor).status_code == 404
    assert client(context).get(f"/api/v1/inbox/{anchor.pk}", secure=True).status_code == 404
    assert "not found" in mcp(context, anchor)["error"]["message"].lower()


def test_unicode_payloads_are_bounded_with_explicit_continuation(context):
    anchor = message(context, body="文" * 30000)
    for _ in range(8):
        InboxReply.objects.create(inbox_message=anchor, body="回" * 10000)
    for offset in range(1, 20):
        message(context, body="文" * 30000, offset=offset)
    result = read_stored_thread(anchor, actor_id=context.key.api_key.pk, limit=50)
    assert len(json.dumps(result, ensure_ascii=True)) <= 65536
    assert result["next_cursor"] is not None
    assert result["response_truncated"] is True
    assert all(row["body_truncated"] for row in result["messages"])


@pytest.mark.parametrize("cursor", [True, {}, [], "x" * 5000, "not-a-signed-cursor"])
def test_bad_cursor_never_returns_another_scope(context, cursor):
    with pytest.raises(ValueError):
        read_stored_thread(message(context), actor_id=context.key.api_key.pk, cursor=cursor)
