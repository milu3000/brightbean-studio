"""Inactive principals cannot use MCP, including a previously cached API key."""

from unittest.mock import patch

import pytest

from apps.api.auth import _resolve_oauth_actor
from apps.api_keys.services import issue_api_key, verify_token
from apps.mcp.tests.test_oauth_auth import _make_user_with_workspace, _mint_oauth_token, _post, _rpc, _SecureClient

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "method, params",
    [
        ("ping", {}),
        ("tools/list", {}),
        (
            "tools/call",
            {"name": "get_inbox_message", "arguments": {"message_id": "00000000-0000-0000-0000-000000000000"}},
        ),
        (
            "tools/call",
            {"name": "send_reply", "arguments": {"message_id": "00000000-0000-0000-0000-000000000000", "body": "No"}},
        ),
        ("events/list", {}),
    ],
)
def test_inactive_user_rejected_before_dispatch(method, params):
    user, _, _ = _make_user_with_workspace("inactive-oauth@example.test", "owner")
    raw = _mint_oauth_token(user)
    assert _resolve_oauth_actor(raw) is not None
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert _resolve_oauth_actor(raw) is None
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {raw}")
    with patch("apps.inbox.services.get_provider") as provider:
        status, _ = _post(client, _rpc(method, params))
    assert status == 401
    provider.assert_not_called()


def test_reactivated_user_authenticates_normally():
    user, _, _ = _make_user_with_workspace("reactivated-oauth@example.test", "owner")
    raw = _mint_oauth_token(user)
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert _resolve_oauth_actor(raw) is None
    user.is_active = True
    user.save(update_fields=["is_active"])
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {raw}")
    status, body = _post(client, _rpc("ping"))
    assert status == 200 and body["result"] == {}


@pytest.mark.parametrize("state", ["inactive_user", "archived_workspace"])
@pytest.mark.parametrize("tool", ["get_inbox_message", "send_reply"])
def test_cached_api_key_cannot_outlive_active_principal(state, tool):
    user, workspace, account = _make_user_with_workspace("inactive-key@example.test", "owner")
    issued = issue_api_key(
        workspace=workspace,
        social_accounts=[account],
        issued_by=user,
        name="Offline auth regression",
        permissions=["use_inbox", "reply_from_inbox"],
    )
    # Warm the row cache before changing its related principal/workspace; their
    # saves do not trigger ApiKey's own post_save invalidation hook.
    assert verify_token(issued.plaintext_token) is not None
    if state == "inactive_user":
        user.is_active = False
        user.save(update_fields=["is_active"])
    else:
        workspace.is_archived = True
        workspace.save(update_fields=["is_archived"])
    assert verify_token(issued.plaintext_token) is None
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {issued.plaintext_token}")
    arguments = {"message_id": "00000000-0000-0000-0000-000000000000"}
    if tool == "send_reply":
        arguments["body"] = "Cannot send"
    with patch("apps.inbox.services.get_provider") as provider:
        status, _ = _post(client, _rpc("tools/call", {"name": tool, "arguments": arguments}))
    assert status == 401
    provider.assert_not_called()
