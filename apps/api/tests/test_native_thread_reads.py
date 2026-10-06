"""One transient platform snapshot with identical scoped REST and MCP reads."""

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.api.models import IdempotencyRecord
from apps.api.tests.test_inbox_thread_reads import client, message
from apps.api.tests.test_inbox_thread_reads import context as context  # noqa: F401
from apps.api_keys.models import ApiKeyAuditLog
from apps.inbox.models import ConversationMessage, InboxMessage, InboxReply, InternalNote
from apps.inbox.services import ReplyStateError, validate_automated_reply_window
from apps.mcp.tools import get_tool
from apps.social_accounts.models import SocialAccount

pytestmark = pytest.mark.django_db


def payload(context):
    return {
        "id": "synthetic-thread",
        "participants": {"data": [{"id": context.account.account_platform_id}, {"id": "synthetic-peer"}]},
        "messages": {
            "data": [
                {
                    "id": "native-outbound",
                    "from": {"id": context.account.account_platform_id},
                    "to": {"data": [{"id": "synthetic-peer"}]},
                    "message": "Native-only body never stored in BrightBean",
                    "created_time": timezone.now().isoformat(),
                }
            ]
        },
    }


def read_rest(context, anchor, **params):
    return client(context).post(
        f"/api/v1/inbox/{anchor.pk}/native-thread/read", params, content_type="application/json", secure=True
    )


def rpc(anchor, **params):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "read_native_inbox_thread", "arguments": {"message_id": str(anchor.pk), **params}},
    }


def read_mcp(context, anchor, **params):
    return client(context).post("/api/v1/mcp/", rpc(anchor, **params), content_type="application/json", secure=True)


def decoded(response):
    return json.loads(response.json()["result"]["content"][0]["text"])


@pytest.mark.parametrize("platform", ["facebook", "instagram_login"])
def test_platform_reply_is_transient_in_both_surfaces_with_capture_off(context, settings, platform):
    context.account.platform = platform
    context.account.save(update_fields=["platform"])
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    anchor = message(context, status="archived", offset=-60)
    draft = InboxReply.objects.create(inbox_message=anchor, body="An unsent local draft")
    before = list(InboxMessage.objects.values())
    with patch("apps.inbox.native_thread_reads._request_native_thread", return_value=payload(context)) as request:
        rest, mcp = read_rest(context, anchor), read_mcp(context, anchor)
    assert rest.status_code == mcp.status_code == 200
    first, second = rest.json(), decoded(mcp)
    first.pop("checked_at")
    second.pop("checked_at")
    assert first == second
    assert first["status"] == "observed"
    assert first["items"][0]["body"] == "Native-only body never stored in BrightBean"
    assert first["items"][0]["direction"] == "outbound"
    assert first["items"][0]["source"] == "platform_observed"
    assert first["newer_outbound_observed"] is True
    assert first["history_complete"] is first["persisted"] is False
    assert request.call_count == 2  # One explicit request per caller, no polling or replay cache.
    for response in (rest, mcp):
        assert "no-store" in response["Cache-Control"]
    assert list(InboxMessage.objects.values()) == before
    draft.refresh_from_db()
    assert draft.status == "draft"
    assert draft.body == "An unsent local draft"
    assert InboxReply.objects.count() == 1
    assert not ConversationMessage.objects.exists()
    assert not InternalNote.objects.exists()
    assert not IdempotencyRecord.objects.exists()
    audits = list(ApiKeyAuditLog.objects.values())
    assert len(audits) == 2
    assert "Native-only body" not in json.dumps(audits, default=str)


def test_missing_thread_id_never_falls_back_to_account_scan(context):
    anchor = message(context, native="")
    with patch("apps.inbox.native_thread_reads._request_native_thread") as request:
        rest, mcp = read_rest(context, anchor), read_mcp(context, anchor)
    assert rest.json()["status"] == decoded(mcp)["status"] == "unavailable"
    assert rest.json()["items"] == decoded(mcp)["items"] == []
    request.assert_not_called()


def test_current_key_account_allowlist_is_enforced_before_remote_read(context):
    account = SocialAccount.objects.create(
        workspace=context.workspace, platform="instagram_login", account_platform_id="other-own", account_name="Other"
    )
    foreign = message(context, account=account)
    with patch("apps.inbox.native_thread_reads._request_native_thread") as request:
        assert read_rest(context, foreign).status_code == 404
        assert "not found" in read_mcp(context, foreign).json()["error"]["message"].lower()
    request.assert_not_called()


def test_current_read_permission_is_required_without_send_permission(context):
    anchor = message(context)
    context.key.api_key.permissions = ["view_analytics"]
    context.key.api_key.save(update_fields=["permissions"])
    with patch("apps.inbox.native_thread_reads._request_native_thread") as request:
        assert read_rest(context, anchor).status_code == 403
        assert "Permission denied" in read_mcp(context, anchor).json()["error"]["message"]
    request.assert_not_called()


def test_revocation_during_remote_read_does_not_return_native_body(context):
    anchor = message(context)

    def revoke(*_args):
        context.key.api_key.social_accounts.clear()
        return payload(context)

    with patch("apps.inbox.native_thread_reads._request_native_thread", side_effect=revoke):
        response = read_rest(context, anchor)
    assert response.status_code in {404, 409}
    assert "Native-only body" not in response.content.decode()


@pytest.mark.parametrize("limit", [0, 101, -1, "invalid", "1", True, 1.5])
def test_invalid_limit_never_reaches_provider(context, limit):
    anchor = message(context)
    with patch("apps.inbox.native_thread_reads._request_native_thread") as request:
        assert read_rest(context, anchor, limit=limit).status_code == 422
        assert "error" in read_mcp(context, anchor, limit=limit).json()
    request.assert_not_called()


def test_mcp_never_accepts_a_caller_supplied_native_thread_or_url(context):
    anchor = message(context)
    with patch("apps.inbox.native_thread_reads._request_native_thread") as request:
        result = read_mcp(context, anchor, native_thread_id="another-thread", url="https://example.invalid").json()
    assert "error" in result
    request.assert_not_called()


def test_reading_old_native_history_does_not_extend_automated_reply_window(context):
    anchor = message(context, received_at=timezone.now() - timedelta(days=2))
    with patch("apps.inbox.native_thread_reads._request_native_thread", return_value=payload(context)):
        assert read_rest(context, anchor).json()["status"] == "observed"
    anchor.refresh_from_db()
    with pytest.raises(ReplyStateError):
        validate_automated_reply_window(anchor)
    assert not InboxReply.objects.exists()


def test_get_and_stored_reads_do_not_trigger_platform_refresh(context):
    anchor = message(context)
    with patch("apps.inbox.native_thread_reads._request_native_thread") as request:
        assert client(context).get(f"/api/v1/inbox/{anchor.pk}/native-thread/read", secure=True).status_code == 405
        assert client(context).get(f"/api/v1/inbox/{anchor.pk}", secure=True).status_code == 200
        assert client(context).get(f"/api/v1/inbox/{anchor.pk}/thread", secure=True).status_code == 200
    request.assert_not_called()


def test_legacy_mcp_batch_snapshot_is_not_cacheable(context):
    anchor = message(context)
    with patch("apps.inbox.native_thread_reads._request_native_thread", return_value=payload(context)):
        response = client(context).post("/api/v1/mcp/", [rpc(anchor)], content_type="application/json", secure=True)
    assert "no-store" in response["Cache-Control"]
    assert json.loads(response.json()[0]["result"]["content"][0]["text"])["status"] == "observed"


@pytest.mark.parametrize("exception", [ValueError, RuntimeError])
def test_unexpected_projection_error_never_reaches_error_text_or_logs(context, caplog, exception):
    anchor = message(context)
    secret_body = "private-native-body-must-never-be-logged"
    with (
        patch("apps.inbox.native_thread_reads._request_native_thread", return_value=payload(context)),
        patch("apps.inbox.native_thread_reads._project", side_effect=exception(secret_body)),
    ):
        rest, mcp = read_rest(context, anchor), read_mcp(context, anchor)
    assert rest.status_code == 502
    assert "error" in mcp.json()
    assert secret_body not in rest.content.decode() + mcp.content.decode() + caplog.text
    assert not ConversationMessage.objects.exists()


def test_message_tool_description_does_not_claim_complete_native_history():
    description = get_tool("get_inbox_message").description
    assert "empty replies array does not mean unanswered" in description
    assert "full reply thread" not in description
