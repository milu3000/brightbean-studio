"""Transient native observations must not become history, receipts or send grants."""

import json
import time
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from django.core import signing
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.api_keys.services import issue_api_key
from apps.inbox import native_thread_reads as reads
from apps.inbox.models import ConversationMessage, DMSendAttempt, DMSendControl, InboxMessage, InboxReply, InternalNote
from apps.inbox.tests.test_shared_reply_safety import dm as dm  # noqa: F401
from apps.members.models import CustomRole
from apps.social_accounts.models import SocialAccount

pytestmark = pytest.mark.django_db


def payload(dm, *, own=None):
    own = own or dm.account.account_platform_id
    return {
        "id": "conversation-1",
        "participants": {"data": [{"id": own}, {"id": "peer-1"}]},
        "messages": {
            "data": [
                {
                    "id": "native-out-1",
                    "message": "Native answer",
                    "from": {"id": own},
                    "to": {"data": [{"id": "peer-1"}]},
                    "created_time": timezone.now().isoformat(),
                },
                {
                    "id": "native-in-1",
                    "message": "Earlier customer question",
                    "from": {"id": "peer-1"},
                    "to": {"data": [{"id": own}]},
                    "created_time": (timezone.now() - timedelta(minutes=1)).isoformat(),
                },
            ],
        },
    }


def read(dm, data=None, *, authorization=None, limit=50, side_effect=None, continuation=None):
    with patch.object(reads, "_request_native_thread", return_value=data, side_effect=side_effect) as request:
        result = reads.read_native_thread(
            dm.message,
            authorization=authorization or reads.session_read_authorization(dm.user),
            limit=limit,
            continuation=continuation,
        )
    return result, request


def key_for(dm, permissions=None):
    return issue_api_key(
        workspace=dm.account.workspace,
        social_accounts=[dm.account],
        issued_by=dm.user,
        name="Synthetic native read",
        permissions=["use_inbox"] if permissions is None else permissions,
    ).api_key


@pytest.mark.parametrize("platform", ["facebook", "instagram_login"])
def test_read_returns_both_directions_without_any_writes_or_send_permission(dm, platform):
    dm.account.platform = platform
    dm.account.save(update_fields=["platform"])
    data = payload(dm)
    message_before = InboxMessage.objects.values().get(pk=dm.message.pk)
    account_before = SocialAccount.objects.values().get(pk=dm.account.pk)
    with CaptureQueriesContext(connection) as queries:
        result, request = read(dm, data)
    assert not any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries)
    assert result["status"] == "observed" and result["reason_code"] == "bounded_snapshot"
    assert result["history_complete"] is False and result["persisted"] is False
    assert result["newer_outbound_observed"] is True
    assert result["anchor_message_id"] == str(dm.message.pk) and result["platform"] == platform
    assert [item["direction"] for item in result["items"]] == ["inbound", "outbound"]
    assert {item["source"] for item in result["items"]} == {"platform_observed"}
    assert result["items"][-1]["platform_message_id"] == "native-out-1"
    assert not {"author", "status", "reply_id", "delivery_confirmed", "send_precondition"} & result["items"][-1].keys()
    assert "send_precondition" not in result and "eligible_to_send" not in result
    request.assert_called_once()
    assert request.call_args.args[1] == "conversation-1"
    assert InboxMessage.objects.values().get(pk=dm.message.pk) == message_before
    assert SocialAccount.objects.values().get(pk=dm.account.pk) == account_before
    for model in (InboxReply, InternalNote, ConversationMessage, DMSendAttempt, DMSendControl):
        assert not model.objects.exists()


def test_account_level_gateway_identity_is_accepted_but_never_inferred(dm):
    dm.account.webhook_target_id = "gateway-own-id"
    dm.account.save(update_fields=["webhook_target_id"])
    result, _ = read(dm, payload(dm, own="gateway-own-id"))
    assert result["status"] == "observed" and result["items"][-1]["direction"] == "outbound"
    data = payload(dm, own="gateway-own-id")
    data["messages"]["data"][0]["from"]["id"] = dm.account.account_platform_id
    result, _ = read(dm, data)
    assert result["reason_code"] == "message_scope_unverified" and not result["items"]


@pytest.mark.parametrize(
    "native",
    [None, "", 123, True, [], {}, "a/b", "a\\b", "a?b", "a#b", "a%b", ".", "..", "a\nb", "a b", "a\x7fb", "x" * 256],
)
def test_missing_or_malformed_native_id_never_calls_http(dm, native):
    dm.message.extra["conversation_id"] = native
    dm.message.save(update_fields=["extra"])
    result, request = read(dm)
    assert result["reason_code"] == "missing_native_thread" and not result["items"]
    request.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        "comment",
        "unsupported",
        "disconnected",
        "expired",
        "missing_token",
        "group",
        "unknown",
        "conflict",
        "missing_peer",
        "conflicting_peer",
        "malformed_peer",
        "outbound",
        "own_peer",
        "incomplete_local_participants",
    ],
)
def test_ineligible_anchor_never_calls_http(dm, change):
    if change == "comment":
        dm.message.message_type = "comment"
    elif change == "unsupported":
        dm.account.platform = "instagram"
    elif change == "disconnected":
        dm.account.connection_status = "disconnected"
    elif change == "expired":
        dm.account.token_expires_at = timezone.now() - timedelta(seconds=1)
    elif change == "missing_token":
        dm.account.oauth_access_token = ""
    elif change in {"group", "unknown"}:
        dm.message.extra["conversation_type"] = change
    elif change == "conflict":
        dm.message.extra["classification_reason"] = "identity_conflict"
    elif change == "missing_peer":
        dm.message.sender_handle = ""
    elif change == "conflicting_peer":
        dm.message.extra["sender_id"] = "other-peer"
        dm.message.extra["sender"] = {"id": "peer-1"}
    elif change == "malformed_peer":
        dm.message.extra["sender_id"] = {"id": []}
    elif change == "outbound":
        dm.message.extra["direction"] = "outbound"
    elif change == "own_peer":
        dm.message.sender_handle = dm.account.account_platform_id
    else:
        dm.message.extra["participants"] = {"data": [{"id": "peer-1"}]}
    dm.account.save()
    dm.message.save()
    result, request = read(dm)
    assert result["status"] == "unavailable" and not result["items"]
    request.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        "wrong_thread",
        "numeric_thread",
        "missing_participants",
        "group",
        "no_own",
        "no_peer",
        "incomplete",
        "previous",
        "total_mismatch",
        "duplicate",
        "bad_type",
        "unknown_sender",
        "unknown_recipient",
        "missing_from",
        "missing_to",
        "group_to",
        "partial_to",
        "conflicting_mid",
    ],
)
def test_remote_scope_ambiguity_never_releases_any_content(dm, change):
    data = payload(dm)
    participants = data["participants"]
    row = data["messages"]["data"][0]
    if change == "wrong_thread":
        data["id"] = "foreign-thread"
    elif change == "numeric_thread":
        data["id"] = 100
    elif change == "missing_participants":
        data.pop("participants")
    elif change == "group":
        participants["data"].append({"id": "private-third-party"})
    elif change == "no_own":
        participants["data"][0]["id"] = "foreign-own"
    elif change == "no_peer":
        participants["data"][1]["id"] = "foreign-peer"
    elif change in {"incomplete", "previous"}:
        participants["paging"] = {"next" if change == "incomplete" else "previous": "https://must-not-follow.example"}
    elif change == "total_mismatch":
        participants["summary"] = {"total_count": 3}
    elif change == "duplicate":
        participants["data"] = [{"id": "peer-1"}, {"id": "peer-1"}]
    elif change == "bad_type":
        participants["data"] = None
    elif change == "unknown_sender":
        row["from"]["id"] = "private-third-party"
    elif change == "unknown_recipient":
        row["to"]["data"][0]["id"] = "private-third-party"
    elif change == "missing_from":
        row.pop("from")
    elif change == "missing_to":
        row.pop("to")
    elif change == "group_to":
        row["to"]["data"].append({"id": "third-party"})
    elif change == "partial_to":
        row["to"]["paging"] = {"next": "https://must-not-follow.example"}
    else:
        data["messages"]["data"].append({**deepcopy(row), "message": "Conflicting content for same MID"})
    result, request = read(dm, data)
    assert result["status"] == "unavailable" and result["items"] == []
    assert "Native answer" not in str(result) and "Earlier customer" not in str(result)
    request.assert_called_once()


@pytest.mark.parametrize(
    "change",
    [
        "member",
        "inactive",
        "permission",
        "archive",
        "account_id",
        "workspace",
        "account_token",
        "gateway",
        "status",
        "native_id",
        "anchor_mid",
        "anchor_peer",
        "anchor_time",
        "anchor_account",
        "anchor_delete",
    ],
)
def test_scope_and_authority_are_checked_again_after_network(dm, change):
    data = payload(dm)

    def mutate(*args):
        if change == "member":
            dm.member.delete()
        elif change == "inactive":
            type(dm.user).objects.filter(pk=dm.user.pk).update(is_active=False)
        elif change == "permission":
            role = CustomRole.objects.create(
                organization=dm.account.workspace.organization, name="No inbox", permissions={"reply_from_inbox": True}
            )
            type(dm.member).objects.filter(pk=dm.member.pk).update(custom_role=role)
        elif change == "archive":
            type(dm.account.workspace).objects.filter(pk=dm.account.workspace_id).update(is_archived=True)
        elif change in {"account_id", "account_token", "gateway", "status"}:
            key, value = {
                "account_id": ("account_platform_id", "changed"),
                "account_token": ("oauth_access_token", "changed-secret"),
                "gateway": ("webhook_target_id", "changed"),
                "status": ("connection_status", "disconnected"),
            }[change]
            SocialAccount.objects.filter(pk=dm.account.pk).update(**{key: value})
        elif change in {"workspace", "anchor_account"}:
            from apps.workspaces.models import Workspace

            other = Workspace.objects.create(name="Other", organization=dm.account.workspace.organization)
            if change == "workspace":
                SocialAccount.objects.filter(pk=dm.account.pk).update(workspace=other)
            else:
                other_account = SocialAccount.objects.create(
                    workspace=other, platform="facebook", account_platform_id="other-own"
                )
                InboxMessage.objects.filter(pk=dm.message.pk).update(social_account=other_account)
        elif change == "native_id":
            InboxMessage.objects.filter(pk=dm.message.pk).update(extra={**dm.message.extra, "conversation_id": "other"})
        elif change == "anchor_mid":
            InboxMessage.objects.filter(pk=dm.message.pk).update(platform_message_id="changed")
        elif change == "anchor_peer":
            InboxMessage.objects.filter(pk=dm.message.pk).update(sender_handle="changed")
        elif change == "anchor_time":
            InboxMessage.objects.filter(pk=dm.message.pk).update(received_at=timezone.now())
        else:
            InboxMessage.objects.filter(pk=dm.message.pk).delete()
        return data

    with pytest.raises(reads.NativeThreadReadError) as error:
        read(dm, side_effect=mutate)
    assert error.value.code in {"stale", "authorization_revoked"}
    assert not InboxReply.objects.exists() and not InternalNote.objects.exists()


@pytest.mark.parametrize("fault", ["permission", "allowlist", "revoked", "expired", "rotated", "issuer"])
@pytest.mark.parametrize("during_read", [False, True])
def test_api_key_current_grants_identity_and_allowlist_are_required(dm, fault, during_read):
    key = key_for(dm)
    authorization = reads.key_read_authorization(key)

    def change(*args):
        if fault == "allowlist":
            key.social_accounts.clear()
        else:
            values = {
                "permission": {"permissions": ["reply_from_inbox"]},
                "revoked": {"revoked_at": timezone.now()},
                "expired": {"expires_at": timezone.now() - timedelta(seconds=1)},
                "rotated": {"token_hash": "changed"},
                "issuer": {"issued_by": None},
            }[fault]
            type(key).objects.filter(pk=key.pk).update(**values)
        return payload(dm)

    if not during_read:
        change()
    with (
        patch.object(reads, "_request_native_thread", side_effect=change) as request,
        pytest.raises(reads.NativeThreadReadError, match="permission"),
    ):
        reads.read_native_thread(dm.message, authorization=authorization)
    assert request.call_count == int(during_read)


def test_read_only_key_and_role_can_read_without_reply_permissions(dm):
    role = CustomRole.objects.create(
        organization=dm.account.workspace.organization,
        name="Inbox reader",
        permissions={"use_inbox": True, "reply_from_inbox": False},
    )
    dm.member.custom_role = role
    dm.member.save(update_fields=["custom_role"])
    key = key_for(dm)
    result, _ = read(dm, payload(dm), authorization=reads.key_read_authorization(key))
    assert result["status"] == "observed"


@pytest.mark.parametrize("fault", ["delete", "expire", "scope", "workspace", "request_bearer", "allowlist"])
def test_oauth_current_token_and_account_authority_rechecked_after_http(dm, fault):
    from oauth2_provider.models import get_access_token_model

    from apps.api.auth import _resolve_oauth_actor
    from apps.mcp.tests.test_oauth_auth import _mint_oauth_token

    dm.user.last_workspace_id = dm.account.workspace_id
    dm.user.save(update_fields=["last_workspace_id"])
    token = _mint_oauth_token(dm.user)
    actor = _resolve_oauth_actor(token)
    request = RequestFactory().post("/api/v1/mcp/", HTTP_AUTHORIZATION=f"Bearer {token}")
    authorization = reads.key_read_authorization(actor, request)
    original_resolver = _resolve_oauth_actor

    def mutate(*args):
        tokens = get_access_token_model().objects.filter(user=dm.user)
        if fault == "delete":
            tokens.delete()
        elif fault == "expire":
            tokens.update(expires=timezone.now() - timedelta(seconds=1))
        elif fault == "scope":
            tokens.update(scope="unrelated")
        elif fault == "workspace":
            dm.member.delete()
        elif fault == "request_bearer":
            request.META["HTTP_AUTHORIZATION"] = "Bearer changed"
            del request.headers
        return payload(dm)

    calls = 0

    def resolve(value):
        nonlocal calls
        calls += 1
        current = original_resolver(value)
        if fault == "allowlist" and calls > 1 and current is not None:
            current.social_accounts = SimpleNamespace(all=lambda: SocialAccount.objects.none())
        return current

    with patch("apps.api.auth._resolve_oauth_actor", side_effect=resolve), pytest.raises(reads.NativeThreadReadError):
        read(dm, authorization=authorization, side_effect=mutate)


def test_untrusted_media_is_normalized_and_text_is_plain_bounded_data(dm):
    data = payload(dm)
    row = data["messages"]["data"][0]
    row["message"] = '<img src=x onerror="alert(1)">' + "x" * reads.MAX_BODY_CHARACTERS
    row["attachments"] = {
        "data": [
            {"type": "image", "url": "javascript:alert(1)", "preview_url": "https://127.0.0.1/image", "secret": "drop"},
            {"type": "image", "url": "https://cdninstagram.com/x?access_token=private", "title": "<script>x</script>"},
            {"type": "image", "url": "https://cdninstagram.com/safe-image", "private_key": "drop"},
            {"type": "video", "url": "https://public.example/video", "preview_url": "https://outside.example/tracker"},
        ],
        "paging": {"next": "https://must-not-follow.example"},
    }
    result, request = read(dm, data)
    item = result["items"][-1]
    assert len(item["body"]) == reads.MAX_BODY_CHARACTERS and item["body_truncated"] is True
    assert item["attachments_truncated"] is True and result["coverage"]["truncated"] is True
    assert item["attachments"][0]["url"] == item["attachments"][0]["preview_url"] == ""
    assert item["attachments"][1]["url"] == ""
    assert item["attachments"][2]["preview_url"] == "https://cdninstagram.com/safe-image"
    assert item["attachments"][3]["url"] == "https://public.example/video"
    assert item["attachments"][3]["preview_url"] == ""
    assert "private_key" not in str(result) and "access_token" not in str(result)
    request.assert_called_once()


@pytest.mark.parametrize("flag,status", [("is_deleted", "removed"), ("is_unsupported", "partial")])
def test_deleted_and_unsupported_provider_content_stays_honest(dm, flag, status):
    data = payload(dm)
    row = data["messages"]["data"][0]
    row[flag] = True
    row["attachments"] = {"data": [{"type": "image", "url": "https://cdninstagram.com/x"}]}
    result, _ = read(dm, data)
    item = result["items"][-1]
    assert item["content_status"] == status
    if flag == "is_deleted":
        assert item["body"] == "" and item["attachments"] == []


def test_excess_provider_rows_fail_closed_without_losing_a_page(dm):
    data = payload(dm)
    template = data["messages"]["data"][0]
    data["messages"]["data"] = [{**template, "id": f"mid-{i:03}"} for i in range(130)]
    data["messages"]["paging"] = {"next": "https://must-not-follow.example"}
    result, request = read(dm, data, limit=7)
    assert result["status"] == "unavailable" and result["items"] == []
    assert result["coverage"]["scanned_count"] == 130 and result["coverage"]["returned_count"] == 0
    assert result["more_available"] and result["coverage"]["truncated"]
    assert result["older_continuation"] is None
    request.assert_called_once()
    assert request.call_args.args[2] == 7


@pytest.mark.parametrize("fault", ["mid", "naive_time", "future_time", "body", "row", "time_type"])
def test_invalid_rows_fail_closed_with_no_cursor_advancement(dm, fault):
    data = payload(dm)
    row = data["messages"]["data"][0]
    if fault == "mid":
        row["id"] = {"id": "unsafe"}
    elif fault == "naive_time":
        row["created_time"] = "2026-01-01T00:00:00"
    elif fault == "future_time":
        row["created_time"] = (timezone.now() + timedelta(days=1)).isoformat()
    elif fault == "body":
        row["message"] = {"html": "No"}
    elif fault == "row":
        data["messages"]["data"][0] = []
    else:
        row["created_time"] = True
    result, _ = read(dm, data)
    assert result["status"] == "unavailable" and result["items"] == []
    assert result["coverage"]["skipped_count"] == 1 and result["older_continuation"] is None
    assert result["coverage"]["truncated"] and not result["history_complete"]


def test_empty_snapshot_never_proves_no_send_or_complete_history(dm):
    data = payload(dm)
    data["messages"]["data"] = []
    result, _ = read(dm, data)
    assert result["status"] == "observed" and result["reason_code"] == "no_messages_observed"
    assert not result["history_complete"] and not result["newer_outbound_observed"]
    assert not result["items"] and not InboxReply.objects.exists()


@pytest.mark.parametrize("limit", [0, 101, True, "5", -1, None, 1.5])
def test_invalid_limits_are_rejected_without_provider(dm, limit):
    with patch.object(reads, "_request_native_thread") as request, pytest.raises(reads.NativeThreadReadError) as error:
        reads.read_native_thread(dm.message, authorization=reads.session_read_authorization(dm.user), limit=limit)
    assert error.value.code == "invalid_limit"
    request.assert_not_called()


def test_old_thread_is_readable_without_changing_automated_reply_window(dm):
    from apps.inbox.services import ReplyStateError, reply_send_availability, validate_automated_reply_window

    dm.message.received_at = timezone.now() - timedelta(days=30)
    dm.message.save(update_fields=["received_at"])
    before = reply_send_availability(dm.message)
    with pytest.raises(ReplyStateError, match="24 hours"):
        validate_automated_reply_window(dm.message)
    result, _ = read(dm, payload(dm))
    dm.message.refresh_from_db()
    assert result["newer_outbound_observed"] is True and result["status"] == "observed"
    assert reply_send_availability(dm.message) == before
    with pytest.raises(ReplyStateError, match="24 hours"):
        validate_automated_reply_window(dm.message)


@pytest.mark.parametrize(
    "platform,host", [("facebook", "graph.facebook.com"), ("instagram_login", "graph.instagram.com")]
)
def test_transport_is_one_streamed_get_with_no_redirects_sends_or_body_logs(dm, platform, host, caplog):
    dm.account.platform = platform
    dm.account.save(update_fields=["platform"])
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET" and request.url.host == host
        assert request.url.path == "/v25.0/conversation-1"
        assert "messages.limit(20)" in request.url.params["fields"]
        assert request.headers["Authorization"] == "Bearer tok"
        assert "tok" not in str(request.url)
        return httpx.Response(200, json=payload(dm))

    client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
    with patch.object(reads.httpx, "Client", return_value=client):
        result = reads.read_native_thread(dm.message, authorization=reads.session_read_authorization(dm.user))
    assert result["status"] == "observed" and len(requests) == 1
    assert "Native answer" not in caplog.text and "Earlier customer" not in caplog.text


@pytest.mark.parametrize(
    "status,body,code",
    [
        (429, b"PRIVATE_RESPONSE_TOKEN", "rate_limited"),
        (403, b"PRIVATE_RESPONSE_TOKEN", "platform_permission_unavailable"),
        (302, b"PRIVATE_RESPONSE_TOKEN", "provider_unavailable"),
        (500, b"PRIVATE_RESPONSE_TOKEN", "provider_unavailable"),
        (200, b"PRIVATE_RESPONSE_TOKEN", "invalid_response"),
        (200, b'{"id":"first","id":"second"}', "invalid_response"),
        (200, b"x" * (reads.MAX_RESPONSE_BYTES + 1), "response_too_large"),
    ],
    ids=["rate-limit", "denied", "redirect", "server-error", "invalid-json", "duplicate-json-member", "oversized-body"],
)
def test_transport_failures_never_log_body_retry_refresh_or_follow_links(dm, status, body, code, caplog):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, content=body, headers={"Location": "https://must-not-follow.example"})

    client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
    before = SocialAccount.objects.values().get(pk=dm.account.pk)
    with patch.object(reads.httpx, "Client", return_value=client):
        result = reads.read_native_thread(dm.message, authorization=reads.session_read_authorization(dm.user))
    assert result["reason_code"] == code and not result["items"]
    assert len(requests) == 1 and "PRIVATE_RESPONSE_TOKEN" not in caplog.text + str(result)
    assert SocialAccount.objects.values().get(pk=dm.account.pk) == before


def test_exception_diagnostics_are_not_reflected_and_authority_still_rechecked(dm):
    result, request = read(dm, side_effect=RuntimeError("secret-provider-body"))
    assert result["reason_code"] == "provider_unavailable" and "secret-provider-body" not in str(result)
    request.assert_called_once()


@pytest.mark.parametrize("local_kind", [None, "unknown"])
@pytest.mark.parametrize("remote_kind", ["direct", "missing", "group"])
def test_legacy_anchor_without_classification_requires_fresh_complete_provider_pair(dm, local_kind, remote_kind):
    dm.message.extra = {"conversation_id": "conversation-1", "sender_id": "peer-1"}
    if local_kind:
        dm.message.extra.update(conversation_type=local_kind, classification_reason="participants_missing")
    dm.message.sender_handle = "not-an-identity-handle"
    dm.message.save(update_fields=["extra", "sender_handle"])
    data = payload(dm)
    if remote_kind == "missing":
        data.pop("participants")
    elif remote_kind == "group":
        data["participants"]["data"].append({"id": "third-party"})
    result, request = read(dm, data)
    request.assert_called_once()
    assert result["status"] == ("observed" if remote_kind == "direct" else "unavailable")
    assert bool(result["items"]) == (remote_kind == "direct")
    assert not result["history_complete"] and not result["persisted"]


@pytest.mark.parametrize("kind,reason", [("group", "participants_group"), ("unknown", "identity_conflict")])
def test_existing_canonical_conflicts_hold_reads_even_with_history_gate_off(dm, settings, kind, reason):
    from apps.inbox.tests.test_shared_reply_safety import canonical

    settings.INBOX_CONVERSATION_V2_ENABLED = False
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    row = canonical(dm)
    row.body = "PRIVATE_STORED_BODY"
    row.attachments = [{"url": "https://private.example/attachment"}]
    row.save(update_fields=["body", "attachments"])
    conversation = row.conversation
    conversation.conversation_type, conversation.classification_reason = kind, reason
    conversation.save(update_fields=["conversation_type", "classification_reason"])
    with CaptureQueriesContext(connection) as queries:
        result, request = read(dm, payload(dm))
    assert result["reason_code"] == "unverified_thread" and not result["items"]
    request.assert_not_called()
    canonical_queries = [query["sql"] for query in queries if 'FROM "inbox_conversation_message"' in query["sql"]]
    assert canonical_queries
    assert all('"body"' not in query and '"attachments"' not in query for query in canonical_queries)


def test_canonical_identity_change_during_read_discards_remote_body(dm):
    from apps.inbox.tests.test_shared_reply_safety import canonical

    row = canonical(dm)

    def mutate(*args):
        type(row.conversation).objects.filter(pk=row.conversation_id).update(conversation_type="group")
        return payload(dm)

    with pytest.raises(reads.NativeThreadReadError) as error:
        read(dm, side_effect=mutate)
    assert error.value.code == "stale"


def test_output_budget_preserves_every_row_with_explicit_text_and_media_truncation(dm):
    data = payload(dm)
    template = data["messages"]["data"][0]
    template["message"] = "中" * reads.MAX_BODY_CHARACTERS
    template["attachments"] = {
        "data": [
            {"id": str(index), "type": "image", "url": f"https://cdninstagram.com/{index}?q=" + "z" * 7000}
            for index in range(30)
        ]
    }
    data["messages"]["data"] = [
        {**deepcopy(template), "id": f"mid-{index:03}"} for index in range(reads.PROVIDER_PAGE_LIMIT)
    ]
    data["messages"]["paging"] = {
        "next": "https://graph.facebook.com/v25.0/conversation-1/messages?after=older-cursor",
        "cursors": {"after": "older-cursor"},
    }
    result, _ = read(dm, data)
    assert len(json.dumps(result).encode()) < reads.MAX_RESULT_BYTES
    assert {item["platform_message_id"] for item in result["items"]} == {
        f"mid-{index:03}" for index in range(reads.PROVIDER_PAGE_LIMIT)
    }
    assert result["items"][-1]["body_truncated"] is True
    assert result["items"][-1]["attachments_truncated"] is True
    assert result["coverage"]["output_truncated"] is True
    assert result["coverage"]["output_omitted_count"] == 0
    assert result["older_continuation"]
    assert result["more_available"] is True and not result["history_complete"]
    # Worst-case envelope that places the whole result inside an MCP text block.
    envelope = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(result)}]}}
    assert len(json.dumps(envelope).encode()) < 64 * 1024


def test_account_token_expiring_during_the_read_does_not_release_content(dm):
    start = timezone.now()
    dm.account.token_expires_at = start + timedelta(seconds=1)
    dm.account.save(update_fields=["token_expires_at"])
    with patch.object(reads.timezone, "now", return_value=start) as now:

        def later(*args):
            now.return_value = start + timedelta(seconds=2)
            return payload(dm)

        result, _ = read(dm, side_effect=later)
    assert result["reason_code"] == "account_unavailable" and not result["items"]


def test_missing_authorization_never_calls_provider(dm):
    with patch.object(reads, "_request_native_thread") as request, pytest.raises(reads.NativeThreadReadError) as error:
        reads.read_native_thread(dm.message, authorization=None)
    assert error.value.code == "authorization_required"
    request.assert_not_called()


@pytest.mark.parametrize("kind,reason", [("group", "participants_group"), ("unknown", "identity_conflict")])
@pytest.mark.parametrize("during_read", [False, True])
def test_conflicting_same_thread_sibling_holds_disclosure_before_and_after_fetch(dm, kind, reason, during_read):
    def add(*args):
        InboxMessage.objects.create(
            workspace=dm.account.workspace,
            social_account=dm.account,
            platform_message_id="sibling-conflict",
            message_type="dm",
            sender_handle="peer-1",
            body="Private sibling content",
            received_at=timezone.now(),
            extra={"conversation_id": "conversation-1", "conversation_type": kind, "classification_reason": reason},
        )
        return payload(dm)

    if during_read:
        with pytest.raises(reads.NativeThreadReadError) as error:
            read(dm, side_effect=add)
        assert error.value.code == "stale"
    else:
        add()
        result, request = read(dm, payload(dm))
        assert result["reason_code"] == "unverified_thread" and not result["items"]
        request.assert_not_called()


def test_sibling_classification_in_other_native_thread_does_not_expand_scope(dm):
    InboxMessage.objects.create(
        workspace=dm.account.workspace,
        social_account=dm.account,
        platform_message_id="other-thread",
        message_type="dm",
        sender_handle="peer-1",
        body="Private other thread",
        received_at=timezone.now(),
        extra={
            "conversation_id": "other-native",
            "conversation_type": "group",
            "classification_reason": "participants_group",
        },
    )
    result, request = read(dm, payload(dm))
    assert result["status"] == "observed"
    request.assert_called_once()


def paged_payload(dm, *, page=0, count=20, more=True):
    data = payload(dm)
    row = data["messages"]["data"][0]
    data["messages"]["data"] = [
        {
            **deepcopy(row),
            "id": f"page-{page}-message-{index}",
            "created_time": (timezone.now() - timedelta(minutes=page * 30 + index + 1)).isoformat(),
            "message": "中" * 10000,
        }
        for index in range(count)
    ]
    if more:
        host = "graph.facebook.com" if dm.account.platform == "facebook" else "graph.instagram.com"
        data["messages"]["paging"] = {
            "next": f"https://{host}/v25.0/conversation-1/messages?after=older-page-{page + 1}&access_token=PRIVATE_NEXT_TOKEN",
            "cursors": {"after": f"older-page-{page + 1}"},
        }
    return data


def test_older_pages_preserve_every_identity_despite_large_bodies_without_writes(dm):
    observed = []
    continuation, page_keys = None, set()
    with CaptureQueriesContext(connection) as queries:
        for page in range(3):
            data = paged_payload(dm, page=page, more=page < 2)
            result, request = read(dm, data, continuation=continuation)
            assert result["status"] == "observed" and len(result["items"]) == 20
            assert result["coverage"]["output_omitted_count"] == 0
            assert result["coverage"]["body_truncated_count"] == 20
            assert result["page_key"] not in page_keys
            assert len(json.dumps(result).encode()) < reads.MAX_RESULT_BYTES
            assert "PRIVATE_NEXT_TOKEN" not in str(result)
            assert request.call_args.args[3] == (None if page == 0 else f"older-page-{page}")
            assert result["older_history_status"] == ("available" if page < 2 else "not_indicated")
            assert result["history_complete"] is False and result["persisted"] is False
            observed.extend(item["platform_message_id"] for item in result["items"])
            page_keys.add(result["page_key"])
            continuation = result["older_continuation"]
    assert continuation is None
    assert len(observed) == len(set(observed)) == 60
    assert not any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries)
    assert not ConversationMessage.objects.exists() and not InboxReply.objects.exists()


@pytest.mark.parametrize("limit", [1, 7, 20, 50, 100])
def test_provider_page_alignment_preserves_public_requested_limit(dm, limit):
    data = paged_payload(dm, count=min(limit, reads.PROVIDER_PAGE_LIMIT))
    data["messages"]["summary"] = {"total_count": 200}
    result, request = read(dm, data, limit=limit)
    assert result["status"] == "observed" and result["older_continuation"]
    assert len(result["items"]) == min(limit, reads.PROVIDER_PAGE_LIMIT)
    assert result["coverage"]["requested_limit"] == limit
    assert result["coverage"]["provider_page_limit"] == min(limit, reads.PROVIDER_PAGE_LIMIT)
    assert request.call_args.args[2] == min(limit, reads.PROVIDER_PAGE_LIMIT)


@pytest.mark.parametrize("cursor", ["", "https://graph.facebook.com/other", {}, [], 1, True, "x" * 6145])
def test_arbitrary_continuations_are_rejected_before_provider_read(dm, cursor):
    with patch.object(reads, "_request_native_thread") as request, pytest.raises(reads.NativeThreadReadError) as error:
        reads.read_native_thread(
            dm.message, authorization=reads.session_read_authorization(dm.user), continuation=cursor
        )
    assert error.value.code == "invalid_continuation"
    request.assert_not_called()


def test_expired_and_tampered_continuations_are_rejected_before_provider_read(dm):
    result, _ = read(dm, paged_payload(dm))
    token = result["older_continuation"]
    for candidate, timestamp in [(token + "x", time.time()), (token, time.time() + reads.CONTINUATION_MAX_AGE + 2)]:
        with (
            patch("django.core.signing.time.time", return_value=timestamp),
            patch.object(reads, "_request_native_thread") as request,
            pytest.raises(reads.NativeThreadReadError) as error,
        ):
            reads.read_native_thread(
                dm.message, authorization=reads.session_read_authorization(dm.user), continuation=candidate
            )
        assert error.value.code == "invalid_continuation"
        request.assert_not_called()


@pytest.mark.parametrize("change", ["token", "platform", "thread", "anchor", "limit", "caller_credential"])
def test_signed_continuations_pin_scope_and_stable_credentials_without_granting_access(dm, change):
    key = key_for(dm)
    authorization = reads.key_read_authorization(key)
    first, _ = read(dm, paged_payload(dm), authorization=authorization)
    message, limit = dm.message, 50
    if change == "token":
        dm.account.oauth_access_token = "rotated-platform-token"
        dm.account.save(update_fields=["oauth_access_token"])
    elif change == "platform":
        dm.account.platform = "instagram_login"
        dm.account.save(update_fields=["platform"])
    elif change == "thread":
        dm.message.extra["conversation_id"] = "different-conversation"
        dm.message.save(update_fields=["extra"])
    elif change == "anchor":
        message = InboxMessage.objects.create(
            workspace=dm.account.workspace,
            social_account=dm.account,
            platform_message_id="different-anchor",
            message_type="dm",
            sender_handle="peer-1",
            body="Different saved anchor",
            received_at=timezone.now(),
            extra=deepcopy(dm.message.extra),
        )
    elif change == "limit":
        limit = 19
    else:
        authorization = reads.key_read_authorization(key_for(dm))
    with patch.object(reads, "_request_native_thread") as request, pytest.raises(reads.NativeThreadReadError) as error:
        reads.read_native_thread(
            message, authorization=authorization, limit=limit, continuation=first["older_continuation"]
        )
    assert error.value.code == "stale_continuation"
    request.assert_not_called()


def test_continuation_never_overrides_revoked_authority(dm):
    first, _ = read(dm, paged_payload(dm))
    dm.member.delete()
    with patch.object(reads, "_request_native_thread") as request, pytest.raises(reads.NativeThreadReadError) as error:
        reads.read_native_thread(
            dm.message,
            authorization=reads.session_read_authorization(dm.user),
            continuation=first["older_continuation"],
        )
    assert error.value.code == "authorization_revoked"
    request.assert_not_called()


@pytest.mark.parametrize(
    "fault", ["repeat_cursor", "repeat_row", "newer_row", "wrong_order", "invalid_row", "excess_rows"]
)
def test_unsafe_or_nonprogressing_older_pages_release_no_content_or_cursor(dm, fault):
    first_data = paged_payload(dm)
    first, _ = read(dm, first_data)
    older = paged_payload(dm, page=1)
    rows = older["messages"]["data"]
    if fault == "repeat_cursor":
        older["messages"]["paging"]["cursors"]["after"] = "older-page-1"
    elif fault == "repeat_row":
        rows[0]["id"] = first_data["messages"]["data"][-1]["id"]
    elif fault == "newer_row":
        rows[0]["created_time"] = timezone.now().isoformat()
    elif fault == "wrong_order":
        rows.reverse()
    elif fault == "invalid_row":
        rows[-1]["id"] = None
    else:
        rows.append({**deepcopy(rows[-1]), "id": "excess-mid"})
    result, _ = read(dm, older, continuation=first["older_continuation"])
    assert result["status"] == "unavailable" and result["items"] == []
    assert result["older_continuation"] is None and result["page_key"] is None


@pytest.mark.parametrize(
    "paging",
    [
        {"next": "https://must-not-follow.example"},
        {"previous": "https://must-not-follow.example", "cursors": {"after": "cursor"}},
        {"cursors": {"after": "cursor"}},
        {"next": "https://must-not-follow.example", "cursors": {"after": "https://must-not-follow.example"}},
        {"next": "https://must-not-follow.example", "cursors": {"after": "x" * 1025}},
    ],
)
def test_other_more_available_signals_never_substitute_for_older_cursor(dm, paging):
    data = payload(dm)
    data["messages"]["paging"] = paging
    result, _ = read(dm, data)
    assert result["status"] == "observed" and len(result["items"]) == 2
    assert result["older_continuation"] is None and not result["history_complete"]
    assert result["older_history_status"] != "available"


@pytest.mark.parametrize(
    "platform,host", [("facebook", "graph.facebook.com"), ("instagram_login", "graph.instagram.com")]
)
def test_older_transport_uses_two_fixed_same_thread_gets_and_no_next_url(dm, platform, host, caplog):
    dm.account.platform = platform
    dm.account.save(update_fields=["platform"])
    first, _ = read(dm, paged_payload(dm))
    older = paged_payload(dm, page=1)
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET" and request.url.host == host
        assert request.headers["Authorization"] == "Bearer tok"
        assert "access_token" not in request.url.params and "tok" not in str(request.url)
        if len(requests) == 1:
            assert request.url.path == "/v25.0/conversation-1"
            assert dict(request.url.params) == {"fields": "id,participants{id}"}
            return httpx.Response(200, json={"id": older["id"], "participants": older["participants"]})
        assert request.url.path == "/v25.0/conversation-1/messages"
        assert request.url.params["after"] == "older-page-1" and request.url.params["limit"] == "20"
        assert request.url.params["fields"] == reads._MESSAGE_FIELDS
        return httpx.Response(200, json=older["messages"])

    client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
    with patch.object(reads.httpx, "Client", return_value=client):
        result = reads.read_native_thread(
            dm.message,
            authorization=reads.session_read_authorization(dm.user),
            continuation=first["older_continuation"],
        )
    assert result["status"] == "observed" and len(requests) == 2 and result["older_continuation"]
    assert "PRIVATE_NEXT_TOKEN" not in caplog.text + str(result)


def test_older_transport_rejects_changed_participants_before_messages_get(dm):
    first, _ = read(dm, paged_payload(dm))
    requests = []
    data = payload(dm)
    data["participants"]["data"].append({"id": "private-third-party"})

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=data)

    client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
    with patch.object(reads.httpx, "Client", return_value=client):
        result = reads.read_native_thread(
            dm.message,
            authorization=reads.session_read_authorization(dm.user),
            continuation=first["older_continuation"],
        )
    assert result["reason_code"] == "participants_unverified" and not result["items"]
    assert result["older_continuation"] is None and len(requests) == 1


def test_cursor_contains_no_platform_credential_or_message_content_and_is_bounded(dm):
    data = paged_payload(dm)
    data["messages"]["paging"]["cursors"]["after"] = "x" * reads.MAX_PROVIDER_CURSOR_LENGTH
    data["messages"]["paging"]["next"] = (
        "https://graph.facebook.com/v25.0/conversation-1/messages?after=" + "x" * reads.MAX_PROVIDER_CURSOR_LENGTH
    )
    result, _ = read(dm, data)
    token = result["older_continuation"]
    assert len(token) <= reads.MAX_CONTINUATION_LENGTH
    decoded = signing.loads(token, salt=reads.CONTINUATION_SALT)
    assert set(decoded) == {"scope", "after", "oldest", "previous_ids"}
    assert dm.account.oauth_access_token not in json.dumps(decoded)
    assert "中" not in json.dumps(decoded, ensure_ascii=False)
    assert "page-0-message-0" not in json.dumps(decoded)
    assert len(json.dumps(result).encode()) < reads.MAX_RESULT_BYTES
    envelope = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(result)}]}}
    assert len(json.dumps(envelope).encode()) < 64 * 1024


@pytest.mark.parametrize("fault", ["revocation", "account_token", "anchor_thread"])
def test_older_page_rechecks_authority_and_identity_after_second_get(dm, fault):
    first, _ = read(dm, paged_payload(dm))
    older = paged_payload(dm, page=1)
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"id": older["id"], "participants": older["participants"]})
        if fault == "revocation":
            dm.member.delete()
        elif fault == "account_token":
            SocialAccount.objects.filter(pk=dm.account.pk).update(oauth_access_token="rotated")
        else:
            InboxMessage.objects.filter(pk=dm.message.pk).update(extra={"conversation_id": "other-thread"})
        return httpx.Response(200, json=older["messages"])

    client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
    with patch.object(reads.httpx, "Client", return_value=client), pytest.raises(reads.NativeThreadReadError) as error:
        reads.read_native_thread(
            dm.message,
            authorization=reads.session_read_authorization(dm.user),
            continuation=first["older_continuation"],
        )
    assert error.value.code == ("authorization_revoked" if fault == "revocation" else "stale")
    assert len(requests) == 2 and not ConversationMessage.objects.exists()


@pytest.mark.parametrize("flag", ["truncated", "is_truncated"])
def test_provider_reported_omissions_cannot_be_skipped_with_a_cursor(dm, flag):
    data = paged_payload(dm)
    data["messages"][flag] = True
    result, _ = read(dm, data)
    assert result["status"] == "observed" and len(result["items"]) == 20
    assert result["older_history_status"] == "unavailable"
    assert result["older_continuation"] is None and result["history_complete"] is False


@pytest.mark.parametrize("total", [True, "20", -1, 19])
def test_contradictory_provider_summary_does_not_advance(dm, total):
    data = paged_payload(dm)
    data["messages"]["summary"] = {"total_count": total}
    result, _ = read(dm, data)
    assert result["status"] == "observed" and len(result["items"]) == 20
    assert result["older_history_status"] == "unavailable"
    assert result["older_continuation"] is None


@pytest.mark.parametrize("more", [False, True])
def test_initial_unordered_observations_remain_visible_without_older_cursor(dm, more):
    data = paged_payload(dm, more=more)
    data["messages"]["data"].reverse()
    result, _ = read(dm, data)
    assert result["status"] == "observed" and len(result["items"]) == 20
    assert [item["occurred_at"] for item in result["items"]] == sorted(row["occurred_at"] for row in result["items"])
    assert result["older_history_status"] == "unavailable" and result["older_continuation"] is None
    assert result["coverage"]["output_omitted_count"] == 0 and result["history_complete"] is False


@pytest.mark.parametrize("fault", ["paging", "next", "cursors", "summary"])
def test_initial_unknown_pagination_keeps_valid_rows_but_cannot_advance(dm, fault):
    data = paged_payload(dm)
    if fault == "paging":
        data["messages"]["paging"] = []
    elif fault == "next":
        data["messages"]["paging"]["next"] = []
    elif fault == "cursors":
        data["messages"]["paging"]["cursors"] = []
    else:
        data["messages"]["summary"] = []
    result, _ = read(dm, data)
    assert result["status"] == "observed" and len(result["items"]) == 20
    assert result["older_history_status"] == "unavailable" and result["older_continuation"] is None


@pytest.mark.parametrize("platform", ["facebook", "instagram_login"])
@pytest.mark.parametrize(
    "fault",
    [
        "external_host",
        "other_provider_host",
        "wrong_thread",
        "wrong_version",
        "nested_shape",
        "userinfo",
        "fragment",
        "http",
        "duplicate_after",
        "mismatched_after",
        "missing_after",
        "time_filter",
        "raw_newline",
    ],
)
def test_older_cursor_requires_provider_evidence_of_exact_supported_route(dm, platform, fault):
    dm.account.platform = platform
    dm.account.save(update_fields=["platform"])
    data = paged_payload(dm)
    host = "graph.facebook.com" if platform == "facebook" else "graph.instagram.com"
    route = f"https://{host}/v25.0/conversation-1/messages"
    url = route + "?after=older-page-1&access_token=PRIVATE_NEXT_TOKEN"
    if fault == "external_host":
        url = url.replace(host, "evil.example")
    elif fault == "other_provider_host":
        url = url.replace(host, "graph.instagram.com" if platform == "facebook" else "graph.facebook.com")
    elif fault == "wrong_thread":
        url = url.replace("conversation-1/messages", "foreign-thread/messages")
    elif fault == "wrong_version":
        url = url.replace("v25.0", "v99.0")
    elif fault == "nested_shape":
        url = url.replace("/messages?", "?fields=messages&")
    elif fault == "userinfo":
        url = url.replace("https://", "https://private-user:private-password@")
    elif fault == "fragment":
        url += "#fragment"
    elif fault == "http":
        url = url.replace("https:", "http:")
    elif fault == "duplicate_after":
        url += "&%61fter=older-page-1"
    elif fault == "mismatched_after":
        url = url.replace("after=older-page-1", "after=other-cursor")
    elif fault == "missing_after":
        url = route + "?access_token=PRIVATE_NEXT_TOKEN"
    elif fault == "time_filter":
        url += "&since=123"
    else:
        url += "\n"
    data["messages"]["paging"]["next"] = url
    result, request = read(dm, data)
    assert result["status"] == "observed" and len(result["items"]) == 20
    assert result["older_history_status"] == "unavailable" and result["older_continuation"] is None
    assert result["more_available"] and not result["history_complete"]
    assert "PRIVATE_NEXT_TOKEN" not in str(result) and "private-password" not in str(result)
    request.assert_called_once()
