"""Attachment projections stay safe and identical across REST and MCP."""

import json
import uuid
from types import SimpleNamespace

import pytest
from django.test import Client
from django.utils import timezone

from apps.api.schemas import AttachmentResponse, InboxMessageResponse
from apps.api_keys import services
from apps.inbox.models import InboxMessage
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


def _attachment(**overrides):
    data = {
        "type": "share",
        "url": "https://www.instagram.com/p/shared/",
        "title": "Shared post",
        "preview_url": "",
        "availability": "available",
        **overrides,
    }
    data.setdefault("availability_reason", "link_provided" if data["url"] else "missing_url")
    return data


def _message_stub(**overrides):
    return SimpleNamespace(
        **{
            "id": uuid.uuid4(),
            "workspace_id": uuid.uuid4(),
            "social_account_id": uuid.uuid4(),
            "social_account": SimpleNamespace(platform="instagram"),
            "message_type": "dm",
            "conversation_type": "unknown",
            "classification_reason": "participants_missing",
            "type_display": "Message · type unconfirmed",
            "status": "unread",
            "sentiment": "neutral",
            "sender_name": "Ada",
            "sender_handle": "ada",
            "body": "",
            "content_type": "attachment",
            "content_preview": "Shared content",
            "attachments": [_attachment()],
            "related_post_id": None,
            "received_at": timezone.now(),
            "created_at": timezone.now(),
            "extra": {"access_token": "private-provider-token"},
            **overrides,
        }
    )


def test_schema_exposes_only_typed_attachment_metadata():
    attachment = _attachment(id="internal-id", raw={"access_token": "private-provider-token"})
    data = InboxMessageResponse.from_message(_message_stub(attachments=[attachment])).model_dump(mode="json")

    assert data["body"] == ""
    assert data["content_type"] == "attachment"
    assert data["content_preview"] == "Shared content"
    assert data["attachments"] == [_attachment()]
    assert "extra" not in data
    assert "private-provider-token" not in json.dumps(data)
    assert "internal-id" not in json.dumps(data)


def test_schema_new_fields_have_backward_compatible_defaults():
    old_payload = InboxMessageResponse.from_message(_message_stub()).model_dump()
    for field in (
        "attachments",
        "content_type",
        "content_preview",
        "content_status",
        "conversation_type",
        "classification_reason",
        "type_display",
    ):
        old_payload.pop(field)

    response = InboxMessageResponse(**old_payload)

    assert response.attachments == []
    assert response.content_type == "unknown"
    assert response.content_preview == ""
    assert AttachmentResponse().availability == "unavailable"
    assert response.conversation_type == "unknown" and response.content_status == "no_metadata"


@pytest.fixture
def attachment_account(db, organization):
    workspace = Workspace.objects.create(name="Attachments WS", organization=organization)
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="attachments-account",
        account_name="Attachments",
        connection_status="connected",
    )


@pytest.fixture
def attachment_client(db, user, organization, attachment_account):
    workspace = attachment_account.workspace
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=[attachment_account],
        issued_by=user,
        name="attachment-read",
        permissions=["use_inbox"],
    )
    return Client(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("body", "attachments", "content_type", "preview"),
    [
        ("", [_attachment()], "attachment", "Shared content"),
        ("Look at this", [_attachment(type="image")], "mixed", "Look at this"),
        ("", [_attachment(url="", availability="unavailable")], "attachment", "Shared content"),
        ("", [], "unknown", "Non-text message"),
        ("Text only", [], "text", "Text only"),
    ],
)
def test_rest_and_mcp_list_and_get_share_safe_attachment_projection(
    attachment_client, attachment_account, body, attachments, content_type, preview
):
    message = InboxMessage.objects.create(
        workspace=attachment_account.workspace,
        social_account=attachment_account,
        platform_message_id="shared-message",
        message_type="dm",
        sender_name="Ada",
        body=body,
        received_at=timezone.now(),
        extra={"inbox_attachments": attachments, "access_token": "private-provider-token"},
    )
    rest = attachment_client.get(f"/api/v1/inbox/{message.id}", secure=True)
    assert rest.status_code == 200
    detail = rest.json()
    assert detail["body"] == body
    assert detail["content_type"] == content_type
    assert detail["content_preview"] == preview
    assert detail["attachments"] == attachments
    assert "extra" not in detail
    assert "private-provider-token" not in rest.content.decode()

    rest_list = attachment_client.get("/api/v1/inbox/", secure=True)
    assert rest_list.status_code == 200
    assert rest_list.json()["messages"] == [detail]

    for name, arguments in (
        ("get_inbox_message", {"message_id": str(message.id)}),
        ("list_inbox_messages", {}),
    ):
        mcp = attachment_client.post(
            "/api/v1/mcp/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            ),
            content_type="application/json",
            secure=True,
        )
        assert mcp.status_code == 200
        data = json.loads(mcp.json()["result"]["content"][0]["text"])
        assert (data["messages"][0] if name == "list_inbox_messages" else data) == detail
        assert "private-provider-token" not in mcp.content.decode()
