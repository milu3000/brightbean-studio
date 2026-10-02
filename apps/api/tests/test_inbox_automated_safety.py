"""REST cannot bypass the same automated Meta safety checks as MCP."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.api.tests.test_inbox_router import _SecureClient
from apps.api_keys.services import issue_api_key, verify_token
from apps.inbox.models import InboxReply
from apps.mcp.tests.test_reply_window import NOW
from apps.mcp.tests.test_reply_window import context as context_fixture
from apps.mcp.tests.test_reply_window import message as message_fixture

context = context_fixture
message = message_fixture
pytestmark = pytest.mark.django_db


@pytest.fixture
def credential(context):
    return issue_api_key(
        workspace=context["workspace"],
        social_accounts=[context["account"]],
        issued_by=context["api_key"].issued_by,
        name="Offline REST safety",
        permissions=["use_inbox", "reply_from_inbox"],
    )


def _send(credential, message, mode):
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {credential.plaintext_token}")
    if mode == "new":
        url = f"/api/v1/inbox/{message.pk}/replies"
        data = {"body": "Offline reply", "send": True}
    else:
        reply = InboxReply.objects.create(inbox_message=message, body="Offline draft")
        url = f"/api/v1/inbox/replies/{reply.pk}/send"
        data = {}
    return client.post(url, data=json.dumps(data), content_type="application/json")


@pytest.mark.parametrize("mode", ["new", "draft"])
@pytest.mark.parametrize(
    "received_at",
    [
        NOW - timedelta(hours=24),
        datetime(1970, 1, 1, tzinfo=UTC),
        NOW + timedelta(minutes=1),
    ],
)
def test_rest_rejects_invalid_automated_window(credential, message, mode, received_at):
    message.received_at = received_at
    message.save(update_fields=["received_at"])
    with (
        patch("apps.inbox.services.timezone.now", return_value=NOW),
        patch("apps.inbox.services.get_provider") as provider,
    ):
        response = _send(credential, message, mode)
    assert response.status_code == 409, response.content
    assert "24 hours" in response.json()["detail"]
    assert not InboxReply.objects.filter(status="sent").exists()
    provider.assert_not_called()


@pytest.mark.parametrize("mode", ["new", "draft"])
def test_rest_recent_message_uses_no_human_agent_exemption(credential, message, mode):
    provider = MagicMock()
    provider.reply_to_message.return_value = SimpleNamespace(platform_message_id="rest-outbound-1")
    with (
        patch("apps.inbox.services.timezone.now", return_value=NOW),
        patch("apps.inbox.services.get_provider", return_value=provider),
    ):
        response = _send(credential, message, mode)
    assert response.status_code in (200, 201), response.content
    assert response.json()["status"] == "sent"
    assert provider.reply_to_message.call_args.kwargs["human_agent"] is False


@pytest.mark.parametrize("mode", ["new", "draft"])
def test_rest_unsupported_send_cannot_claim_delivery(credential, message, mode):
    with (
        patch("apps.inbox.services.timezone.now", return_value=NOW),
        patch("apps.inbox.services._dispatch_to_platform", side_effect=NotImplementedError),
    ):
        response = _send(credential, message, mode)
    assert response.status_code == 502, response.content
    assert "does not support" in response.json()["detail"]
    reply = InboxReply.objects.get()
    assert reply.status == "failed" and reply.sent_at is None


@pytest.mark.parametrize("mode", ["new", "draft"])
@pytest.mark.parametrize("state", ["revoked", "inactive", "archived"])
def test_rest_rejects_revoked_or_inactive_credentials(credential, message, context, mode, state):
    assert verify_token(credential.plaintext_token) is not None
    if state == "revoked":
        credential.api_key.revoked_at = timezone.now()
        credential.api_key.save(update_fields=["revoked_at"])
    elif state == "inactive":
        user = context["api_key"].issued_by
        user.is_active = False
        user.save(update_fields=["is_active"])
    else:
        context["workspace"].is_archived = True
        context["workspace"].save(update_fields=["is_archived"])
    with patch("apps.inbox.services.get_provider") as provider:
        response = _send(credential, message, mode)
    assert response.status_code == 401, response.content
    provider.assert_not_called()


@pytest.mark.parametrize("mode", ["new", "draft"])
def test_rest_rejects_disconnected_account(credential, message, context, mode):
    context["account"].connection_status = "disconnected"
    context["account"].save(update_fields=["connection_status"])
    with (
        patch("apps.inbox.services.timezone.now", return_value=NOW),
        patch("apps.inbox.services.get_provider") as provider,
    ):
        response = _send(credential, message, mode)
    assert response.status_code == 409, response.content
    assert "not connected" in response.json()["detail"]
    provider.assert_not_called()
