"""Opt-in V2 DM reads preserve work state and enforce scoped, bounded history."""

import json
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.inbox.conversation_capabilities import conversation_capabilities
from apps.inbox.models import ConversationMessage, ConversationSyncState, InboxConversation, InboxMessage
from apps.mcp.protocol import JsonRpcError
from apps.mcp.tests.test_inbox_tools import (
    MCP_URL,
    _call,
    _result_json,
    _SecureClient,
)
from apps.mcp.tests.test_inbox_tools import (
    account as _account,
)
from apps.mcp.tests.test_inbox_tools import (
    full_client as _full_client,
)
from apps.mcp.tests.test_inbox_tools import (
    memberships as _memberships,
)
from apps.mcp.tests.test_inbox_tools import (
    other_account as _other_account,
)
from apps.mcp.tests.test_inbox_tools import (
    user as _user,
)
from apps.mcp.tests.test_inbox_tools import (
    workspace as _workspace,
)
from apps.mcp.tools import Tool, all_tools, get_tool
from apps.social_accounts.models import SocialAccount

pytestmark = pytest.mark.django_db
TOOLS = {"list_conversations", "get_conversation_messages", "get_reply_context", "get_conversation_attachments"}
V2_TOOLS = TOOLS | {"get_reply_coordination"}
account = _account
full_client = _full_client
memberships = _memberships
other_account = _other_account
user = _user
workspace = _workspace


@pytest.fixture(autouse=True)
def flag(settings, account, other_account):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    # Enroll even the account outside the caller's allowlist so authorization
    # tests cannot accidentally pass merely because rollout scope excluded it.
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = enrollment(account, other_account)
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = enrollment(account, other_account)


def enrollment(*accounts):
    return json.dumps(
        [
            {
                "workspace_id": str(account.workspace_id),
                "social_account_id": str(account.pk),
                "platform": account.platform,
            }
            for account in accounts
        ]
    )


@pytest.fixture
def conversation(account):
    return InboxConversation.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform=account.platform,
        platform_conversation_id="verified-conversation",
        peer_id="customer-1",
        identity_kind="platform",
    )


def observation(conversation, mid, *, body="Hello", direction="inbound", **values):
    return ConversationMessage.objects.create(
        workspace=conversation.workspace,
        social_account=conversation.social_account,
        platform=conversation.platform,
        conversation=conversation,
        platform_message_id=mid,
        direction=direction,
        body=body,
        occurred_at=values.pop("occurred_at", timezone.now()),
        sources=["poll"],
        **values,
    )


def call(client, name, args):
    status, body = _call(client, name, args)
    assert status == 200
    assert "error" not in body, body
    return _result_json(body)


@contextmanager
def reassign_before_select(model, account, **changes):
    """Mutate after SQL construction, immediately before its database read."""
    changed = False

    def execute(execute, sql, params, many, context):
        nonlocal changed
        if not changed and sql.lstrip().upper().startswith("SELECT") and f'FROM "{model._meta.db_table}"' in sql:
            changed = True
            SocialAccount.objects.filter(pk=account.pk).update(**changes)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(execute):
        yield
    assert changed, "The intended protected query was not exercised"


@pytest.mark.parametrize("changed_field", ["workspace", "platform"])
@pytest.mark.parametrize("tool", ["list", "messages", "context", "unassigned", "sync"])
def test_pinned_enrollment_survives_scope_to_query_reassignment(
    full_client, conversation, organization, changed_field, tool
):
    from apps.mcp import conversation_tools
    from apps.workspaces.models import Workspace

    account = conversation.social_account
    original = InboxMessage.objects.create(
        workspace=conversation.workspace,
        social_account=account,
        platform_message_id="interleaving-target",
        message_type="dm",
        received_at=timezone.now(),
    )
    # A second namespace already exists under this account ID, but only its
    # original Facebook identity is enrolled when _scope captures the grant.
    if changed_field == "platform":
        conversation.platform = "instagram_login"
        conversation.save(update_fields=["platform"])
        changes = {"platform": "instagram_login"}
    else:
        changes = {"workspace_id": Workspace.objects.create(name="Reassigned", organization=organization).pk}
    observation(conversation, "interleaving-target", legacy_message=original, body="Unenrolled namespace content")
    unassigned = observation(conversation, "interleaving-unassigned", body="Unenrolled namespace content")
    unassigned.conversation = None
    unassigned.save(update_fields=["conversation"])
    ConversationSyncState.objects.create(
        workspace=conversation.workspace,
        social_account=account,
        platform=conversation.platform,
        stream="dm",
        status="failed",
        last_error_code="unenrolled-sync-detail",
    )
    original_scope = conversation_tools._scope

    def scope_then_reassign(context):
        scoped = original_scope(context)
        SocialAccount.objects.filter(pk=account.pk).update(**changes)
        return scoped

    with patch.object(conversation_tools, "_scope", side_effect=scope_then_reassign):
        if tool == "sync":
            from apps.api_keys.models import ApiKey

            context = {
                "api_key": ApiKey.objects.get(name="full"),
                "membership": SimpleNamespace(effective_permissions={"use_inbox": True}),
            }
            _, _, scoped = conversation_tools._scope(context)
            data = conversation_tools._sync(account, scoped)
            assert data["status"] == "unknown"
            assert data["last_error_code"] == ""
            return
        name, args = {
            "list": ("list_conversations", {}),
            "messages": ("get_conversation_messages", {"conversation_id": str(conversation.pk)}),
            "context": ("get_reply_context", {"message_id": str(original.pk)}),
            "unassigned": (
                "get_conversation_messages",
                {"social_account_id": str(account.pk), "unassigned_only": True},
            ),
        }[tool]
        _, result = _call(full_client, name, args)
    # A final fresh-scope check now refuses the entire stale response.
    assert "error" in result
    assert "Unenrolled namespace content" not in json.dumps(result)
    assert "unenrolled-sync-detail" not in json.dumps(result)


@pytest.mark.parametrize("changed_field", ["workspace", "platform"])
@pytest.mark.parametrize("query", ["messages", "unassigned_count", "sync"])
def test_each_ledger_query_revalidates_identity_inside_sql(
    full_client, conversation, organization, changed_field, query
):
    from apps.workspaces.models import Workspace

    account = conversation.social_account
    changes = (
        {"platform": "instagram_login"}
        if changed_field == "platform"
        else {"workspace_id": Workspace.objects.create(name="Moved at query", organization=organization).pk}
    )
    # The initially authorized conversation can be read first; later queries
    # must not follow the account into another namespace.
    future_platform = changes.get("platform", account.platform)
    if query != "sync":
        ConversationMessage.objects.create(
            workspace=conversation.workspace,
            social_account=account,
            platform=future_platform,
            conversation=conversation if query == "messages" else None,
            platform_message_id="late-private-row",
            body="Late namespace content",
        )
    ConversationSyncState.objects.create(
        workspace=conversation.workspace,
        social_account=account,
        platform=future_platform,
        stream="dm",
        status="failed",
        last_error_code="late-private-sync",
    )
    protected_model = ConversationSyncState if query == "sync" else ConversationMessage
    with reassign_before_select(protected_model, account, **changes):
        _, result = _call(
            full_client,
            "list_conversations" if query == "unassigned_count" else "get_conversation_messages",
            {} if query == "unassigned_count" else {"conversation_id": str(conversation.pk)},
        )
    assert "error" in result
    assert "Late namespace content" not in json.dumps(result)
    assert "late-private-sync" not in json.dumps(result)


def test_flag_hides_catalog_and_rejects_cached_calls(settings, full_client):
    assert {tool.name for tool in all_tools()} >= TOOLS
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    assert not TOOLS & {tool.name for tool in all_tools()}
    assert get_tool("list_conversations") is None
    _, body = _call(full_client, "list_conversations", {})
    assert "error" in body
    assert get_tool("list_inbox_messages") is not None


@pytest.mark.parametrize("mode", ["empty", "shadow", "read_without_capture", "disjoint", "malformed"])
def test_catalog_requires_valid_capture_and_read_intersection(settings, full_client, account, other_account, mode):
    cached = [get_tool(name) for name in TOOLS]
    settings.INBOX_REPLY_COORDINATION_ENABLED = True
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = enrollment(account)
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = enrollment(account)
    if mode == "empty":
        settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = "[]"
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = "[]"
    elif mode == "shadow":
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = "[]"
    elif mode == "read_without_capture":
        settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = "[]"
    elif mode == "disjoint":
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = enrollment(other_account)
    else:
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = "not valid JSON"

    response = full_client.post(
        MCP_URL,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert not V2_TOOLS & {tool["name"] for tool in response.json()["result"]["tools"]}
    assert not V2_TOOLS & {tool.name for tool in all_tools()}
    for tool in cached:
        assert get_tool(tool.name) is None
        _, result = _call(full_client, tool.name, {})
        assert "error" in result
        # A previously resolved handler must also fail before any data read.
        with CaptureQueriesContext(connection) as queries, pytest.raises(JsonRpcError, match="not enabled"):
            tool.handler({}, {"membership": SimpleNamespace(effective_permissions={"use_inbox": True})})
        assert len(queries) == 0
    assert get_tool("list_inbox_messages") is not None


def test_tool_registry_preserves_legacy_constructor_and_wire_shape(settings):
    tool = Tool("example", "Example", {"type": "object"}, lambda args, context: {})
    assert tool.is_enabled()
    assert tool.to_mcp_dict() == {"name": "example", "description": "Example", "inputSchema": {"type": "object"}}
    gated = Tool("gated", "Gated", {}, tool.handler, "INBOX_CONVERSATION_V2_ENABLED", enabled_predicate=lambda: False)
    assert not gated.is_enabled()
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    assert tool.is_enabled()
    assert not Tool("disabled", "Disabled", {}, tool.handler, "INBOX_CONVERSATION_V2_ENABLED").is_enabled()


def test_capture_only_account_is_hidden_from_all_read_paths(settings, full_client, conversation, other_account):
    from apps.api_keys.models import ApiKey

    ApiKey.objects.get(name="full").social_accounts.add(other_account)
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = enrollment(conversation.social_account)
    foreign = InboxConversation.objects.create(
        workspace=other_account.workspace,
        social_account=other_account,
        platform=other_account.platform,
        platform_conversation_id="shadow-thread",
        identity_kind="platform",
    )
    original = InboxMessage.objects.create(
        workspace=other_account.workspace,
        social_account=other_account,
        platform_message_id="shadow-inbound",
        message_type="dm",
        received_at=timezone.now(),
    )
    observation(foreign, "shadow-inbound", legacy_message=original, body="Capture-only private history")
    row = observation(foreign, "shadow-unassigned", direction="outbound")
    row.conversation = None
    row.save(update_fields=["conversation"])
    ConversationSyncState.objects.create(
        workspace=other_account.workspace,
        social_account=other_account,
        platform=other_account.platform,
        stream="dm",
        status="failed",
        last_error_code="private-sync-detail",
    )

    listed = call(full_client, "list_conversations", {})
    assert [item["id"] for item in listed["items"]] == [str(conversation.pk)]
    assert listed["unassigned_message_count"] == 0
    for name, args, message in [
        ("list_conversations", {"social_account_id": str(other_account.pk)}, "Account not found"),
        ("get_conversation_messages", {"conversation_id": str(foreign.pk)}, "Conversation not found"),
        (
            "get_conversation_messages",
            {"social_account_id": str(other_account.pk), "unassigned_only": True},
            "Account not found",
        ),
        ("get_reply_context", {"message_id": str(original.pk)}, "Inbox message not found"),
    ]:
        _, result = _call(full_client, name, args)
        assert result["error"]["message"] == message
        assert "Capture-only" not in json.dumps(result)
        assert "private-sync-detail" not in json.dumps(result)


@pytest.mark.parametrize(
    "revoked_setting", ["INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS", "INBOX_CONVERSATION_V2_READ_ACCOUNTS"]
)
def test_enrollment_revocation_blocks_existing_rows_and_saved_cursors(
    settings, full_client, conversation, other_account, revoked_setting
):
    original = InboxMessage.objects.create(
        workspace=conversation.workspace,
        social_account=conversation.social_account,
        platform_message_id="revoke-target",
        message_type="dm",
        received_at=timezone.now(),
    )
    observation(conversation, "revoke-target", legacy_message=original)
    observation(conversation, "revoke-second")
    InboxConversation.objects.create(
        workspace=conversation.workspace,
        social_account=conversation.social_account,
        platform=conversation.platform,
        platform_conversation_id="second-thread",
        identity_kind="platform",
    )
    listed = call(full_client, "list_conversations", {"limit": 1})
    history = call(full_client, "get_conversation_messages", {"conversation_id": str(conversation.pk), "limit": 1})
    assert listed["next_cursor"] and history["next_cursor"]
    setattr(settings, revoked_setting, enrollment(other_account))
    assert get_tool("list_conversations") is not None
    with CaptureQueriesContext(connection) as queries:
        assert call(full_client, "list_conversations", {})["items"] == []
        for name, args in [
            ("list_conversations", {"cursor": listed["next_cursor"]}),
            (
                "get_conversation_messages",
                {"conversation_id": str(conversation.pk), "cursor": history["next_cursor"]},
            ),
            (
                "get_conversation_messages",
                {"social_account_id": str(conversation.social_account_id), "unassigned_only": True},
            ),
            ("get_reply_context", {"message_id": str(original.pk)}),
        ]:
            _, result = _call(full_client, name, args)
            assert "error" in result
    protected_tables = [
        model._meta.db_table for model in (InboxConversation, ConversationMessage, ConversationSyncState, InboxMessage)
    ]
    assert not any(f'"{table}"' in query["sql"] for query in queries for table in protected_tables)


def test_bidirectional_context_keeps_archive_and_never_creates_work(full_client, conversation):
    original = InboxMessage.objects.create(
        workspace=conversation.workspace,
        social_account=conversation.social_account,
        platform_message_id="in-1",
        message_type="dm",
        body="Question",
        status="archived",
        received_at=timezone.now() - timedelta(minutes=10),
    )
    target = observation(conversation, "in-1", legacy_message=original)
    outgoing = observation(conversation, "out-1", direction="outbound", body="Already answered on IG")
    result = call(full_client, "get_reply_context", {"message_id": str(original.pk)})
    assert result["context_status"] == "available"
    assert result["target"]["id"] == str(target.pk)
    assert {item["id"] for item in result["items"]} == {str(target.pk), str(outgoing.pk)}
    assert result["newer_outbound_observed"] is True
    assert result["send_preconditions_enforced"] is False
    assert result["sync"]["history_complete"] is False
    original.refresh_from_db()
    assert original.status == "archived"
    assert InboxMessage.objects.count() == 1
    assert original.replies.count() == 0


def test_missing_ledger_is_explicit_not_inferred_as_unreplied(full_client, account):
    original = InboxMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform_message_id="legacy",
        message_type="dm",
        received_at=timezone.now(),
    )
    result = call(full_client, "get_reply_context", {"message_id": str(original.pk)})
    assert result["context_status"] == "not_imported"
    assert result["newer_outbound_observed"] is None
    assert result["target"] is None
    assert not ConversationMessage.objects.exists()


def test_unassigned_target_is_retained_without_guessing(full_client, conversation):
    original = InboxMessage.objects.create(
        workspace=conversation.workspace,
        social_account=conversation.social_account,
        platform_message_id="unassigned",
        message_type="dm",
        received_at=timezone.now(),
    )
    row = observation(conversation, "unassigned", legacy_message=original)
    row.conversation = None
    row.save(update_fields=["conversation"])
    result = call(full_client, "get_reply_context", {"message_id": str(original.pk)})
    assert result["context_status"] == "unassigned"
    assert result["target"]["conversation_id"] is None
    assert result["conversation"] is None
    assert result["items"] == []


def test_unassigned_native_outgoing_is_readable_only_in_explicit_account_scope(full_client, conversation):
    row = observation(conversation, "native-without-thread", direction="outbound")
    row.conversation = None
    row.save(update_fields=["conversation"])
    assert call(full_client, "list_conversations", {})["unassigned_message_count"] == 1
    data = call(
        full_client,
        "get_conversation_messages",
        {"social_account_id": str(conversation.social_account_id), "unassigned_only": True},
    )
    assert data["conversation"] is None
    assert data["unassigned_only"] is True
    assert [item["id"] for item in data["items"]] == [str(row.pk)]
    assert data["items"][0]["direction"] == "outbound"
    _, error = _call(
        full_client, "get_conversation_messages", {"social_account_id": str(conversation.social_account_id)}
    )
    assert "error" in error


def test_unknown_outgoing_time_does_not_claim_no_newer_reply(full_client, conversation):
    original = InboxMessage.objects.create(
        workspace=conversation.workspace,
        social_account=conversation.social_account,
        platform_message_id="target-with-unknown-reply",
        message_type="dm",
        received_at=timezone.now(),
    )
    observation(conversation, "target-with-unknown-reply", legacy_message=original)
    row = observation(conversation, "unknown-time", direction="outbound")
    row.occurred_at = None
    row.save(update_fields=["occurred_at"])
    assert call(full_client, "get_reply_context", {"message_id": str(original.pk)})["newer_outbound_observed"] is None


def test_local_unverified_sent_record_is_not_provider_reply_proof(full_client, conversation):
    from apps.inbox.models import InboxReply

    original = InboxMessage.objects.create(
        workspace=conversation.workspace,
        social_account=conversation.social_account,
        platform_message_id="target-local-only",
        message_type="dm",
        received_at=timezone.now(),
    )
    observation(conversation, "target-local-only", legacy_message=original)
    reply = InboxReply.objects.create(inbox_message=original, body="Local only", status="sent", sent_at=timezone.now())
    observation(conversation, None, direction="outbound", delivery_status="delivery_unverified", legacy_reply=reply)
    data = call(full_client, "get_reply_context", {"message_id": str(original.pk)})
    assert data["newer_outbound_observed"] is None
    assert any(item["delivery_status"] == "delivery_unverified" for item in data["items"])


def test_scope_and_foreign_conversation_are_indistinguishable(full_client, conversation, other_account):
    foreign = InboxConversation.objects.create(
        workspace=other_account.workspace,
        social_account=other_account,
        platform=other_account.platform,
        platform_conversation_id="verified-conversation",
        identity_kind="platform",
    )
    observation(foreign, "foreign", body="Private other brand")
    listed = call(full_client, "list_conversations", {})
    assert [item["id"] for item in listed["items"]] == [str(conversation.pk)]
    _, body = _call(full_client, "get_conversation_messages", {"conversation_id": str(foreign.pk)})
    assert body["error"]["message"] == "Conversation not found"
    assert "Private other brand" not in json.dumps(body)


def test_corrupt_cross_account_attribution_is_not_trusted(full_client, conversation, other_account):
    original = InboxMessage.objects.create(
        workspace=conversation.workspace,
        social_account=conversation.social_account,
        platform_message_id="cross-fk",
        message_type="dm",
        received_at=timezone.now(),
    )
    row = observation(conversation, "cross-fk", legacy_message=original)
    foreign = InboxConversation.objects.create(
        workspace=other_account.workspace,
        social_account=other_account,
        platform=other_account.platform,
        platform_conversation_id="foreign-secret-thread",
        peer_id="foreign-private-peer",
        identity_kind="platform",
    )
    row.conversation = foreign
    row.save(update_fields=["conversation"])
    data = call(full_client, "get_reply_context", {"message_id": str(original.pk)})
    assert data["context_status"] == "unassigned"
    assert data["conversation"] is None
    assert data["target"]["conversation_id"] is None
    assert "foreign-secret-thread" not in json.dumps(data)
    assert "foreign-private-peer" not in json.dumps(data)
    assert str(foreign.pk) not in json.dumps(data)


def test_corrupt_legacy_fk_cannot_mix_two_allowlisted_accounts(full_client, conversation, other_account):
    from apps.api_keys.models import ApiKey

    ApiKey.objects.get(name="full").social_accounts.add(other_account)
    original = InboxMessage.objects.create(
        workspace=conversation.workspace,
        social_account=conversation.social_account,
        platform_message_id="legacy-a",
        message_type="dm",
        received_at=timezone.now(),
    )
    foreign = InboxConversation.objects.create(
        workspace=other_account.workspace,
        social_account=other_account,
        platform=other_account.platform,
        platform_conversation_id="thread-b",
        identity_kind="platform",
    )
    observation(foreign, "native-b", body="Must not appear under account A", legacy_message=original)
    data = call(full_client, "get_reply_context", {"message_id": str(original.pk)})
    assert data["context_status"] == "not_imported"
    assert data["target"] is None
    assert "Must not appear" not in json.dumps(data)


@pytest.mark.parametrize("changed_field", ["workspace", "platform"])
def test_stale_account_workspace_and_platform_never_leak(full_client, conversation, organization, changed_field):
    from apps.workspaces.models import Workspace

    account = conversation.social_account
    original = InboxMessage.objects.create(
        workspace=conversation.workspace,
        social_account=account,
        platform_message_id="before-reassignment",
        message_type="dm",
        received_at=timezone.now(),
    )
    observation(conversation, "before-reassignment", legacy_message=original)
    if changed_field == "workspace":
        account.workspace = Workspace.objects.create(name="Moved", organization=organization)
    else:
        account.platform = "instagram_login"
    account.save(update_fields=[changed_field])
    assert call(full_client, "list_conversations", {})["items"] == []
    for name, args in [
        ("get_conversation_messages", {"conversation_id": str(conversation.pk)}),
        ("get_conversation_messages", {"social_account_id": str(account.pk), "unassigned_only": True}),
        ("get_reply_context", {"message_id": str(original.pk)}),
    ]:
        _, result = _call(full_client, name, args)
        assert "error" in result


def test_platform_reenrollment_never_exposes_stale_ledger_or_sync(settings, full_client, conversation, other_account):
    account = conversation.social_account
    original = InboxMessage.objects.create(
        workspace=conversation.workspace,
        social_account=account,
        platform_message_id="old-platform-target",
        message_type="dm",
        received_at=timezone.now(),
    )
    observation(conversation, "old-platform-target", legacy_message=original, body="Stale platform private content")
    unassigned = observation(conversation, "old-platform-unassigned")
    unassigned.conversation = None
    unassigned.save(update_fields=["conversation"])
    ConversationSyncState.objects.create(
        workspace=conversation.workspace,
        social_account=account,
        platform=conversation.platform,
        stream="dm",
        status="success",
        last_success_at=timezone.now(),
    )
    account.platform = "instagram_login"
    account.save(update_fields=["platform"])
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = enrollment(account, other_account)
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = enrollment(account, other_account)

    listed = call(full_client, "list_conversations", {})
    assert listed["items"] == []
    assert listed["unassigned_message_count"] == 0
    _, result = _call(full_client, "get_conversation_messages", {"conversation_id": str(conversation.pk)})
    assert result["error"]["message"] == "Conversation not found"
    page = call(
        full_client, "get_conversation_messages", {"social_account_id": str(account.pk), "unassigned_only": True}
    )
    context = call(full_client, "get_reply_context", {"message_id": str(original.pk)})
    assert context["context_status"] == "not_imported"
    assert context["target"] is None
    for result in (page, context):
        assert result["items"] == []
        assert result["sync"]["status"] == "unknown"
        assert result["sync"]["last_success_at"] is None
        assert "Stale platform private content" not in json.dumps(result)


def test_current_workspace_is_required_even_when_moved_account_is_reenrolled(
    settings, full_client, conversation, other_account, organization
):
    from apps.workspaces.models import Workspace

    account = conversation.social_account
    original = InboxMessage.objects.create(
        workspace=conversation.workspace,
        social_account=account,
        platform_message_id="moved-legacy-target",
        message_type="dm",
        received_at=timezone.now(),
    )
    observation(conversation, "moved-legacy-target", legacy_message=original)
    account.workspace = Workspace.objects.create(name="New owner", organization=organization)
    account.save(update_fields=["workspace"])
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = enrollment(account, other_account)
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = enrollment(account, other_account)
    assert call(full_client, "list_conversations", {})["items"] == []
    for name, args in [
        ("get_conversation_messages", {"conversation_id": str(conversation.pk)}),
        ("get_conversation_messages", {"social_account_id": str(account.pk), "unassigned_only": True}),
        ("get_reply_context", {"message_id": str(original.pk)}),
    ]:
        _, result = _call(full_client, name, args)
        assert "error" in result


@pytest.mark.parametrize("changed_field", ["workspace", "platform"])
def test_saved_cursors_reject_account_reassignment_even_after_reenrollment(
    settings, full_client, conversation, other_account, organization, changed_field
):
    from apps.api_keys.models import ApiKey
    from apps.workspaces.models import Workspace

    ApiKey.objects.get(name="full").social_accounts.add(other_account)
    InboxConversation.objects.create(
        workspace=conversation.workspace,
        social_account=conversation.social_account,
        platform=conversation.platform,
        platform_conversation_id="another-accessible-thread",
        identity_kind="platform",
    )
    for index in range(2):
        observation(conversation, f"still-readable-{index}")
        unassigned = observation(conversation, f"unassigned-still-readable-{index}")
        unassigned.conversation = None
        unassigned.save(update_fields=["conversation"])
    saved = []
    for name, args in [
        ("list_conversations", {}),
        ("get_conversation_messages", {"conversation_id": str(conversation.pk)}),
        (
            "get_conversation_messages",
            {"social_account_id": str(conversation.social_account_id), "unassigned_only": True},
        ),
    ]:
        page = call(full_client, name, {**args, "limit": 1})
        assert page["next_cursor"]
        saved.append((name, args, page["next_cursor"]))

    # With a platform change the effective account IDs are unchanged. Only a
    # cursor bound to the full enrollment identity rejects the previous grant.
    if changed_field == "workspace":
        other_account.workspace = Workspace.objects.create(name="Reassigned", organization=organization)
    else:
        other_account.platform = "instagram_login"
    other_account.save(update_fields=[changed_field])
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = enrollment(conversation.social_account, other_account)
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = enrollment(conversation.social_account, other_account)
    for name, args, cursor in saved:
        _, result = _call(full_client, name, {**args, "cursor": cursor})
        assert "cursor" in result["error"]["message"]
        assert call(full_client, name, args)["items"]


def test_message_cursor_is_signed_and_bound_to_conversation(full_client, conversation, account):
    for index in range(3):
        observation(conversation, f"p-{index}")
    page = call(full_client, "get_conversation_messages", {"conversation_id": str(conversation.pk), "limit": 1})
    cursor = page["next_cursor"]
    assert cursor
    other = InboxConversation.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform=account.platform,
        platform_conversation_id="other",
        identity_kind="platform",
    )
    for scope, token in [(other, cursor), (conversation, cursor + "tampered")]:
        _, body = _call(full_client, "get_conversation_messages", {"conversation_id": str(scope.pk), "cursor": token})
        assert "cursor" in body["error"]["message"]


def test_cursor_cannot_cross_principals_or_survive_allowlist_change(
    full_client, conversation, user, workspace, other_account
):
    from apps.api_keys.services import issue_api_key

    for index in range(3):
        observation(conversation, f"grant-{index}")
    data = call(full_client, "get_conversation_messages", {"conversation_id": str(conversation.pk), "limit": 1})
    key = issue_api_key(
        workspace=workspace,
        social_accounts=[conversation.social_account],
        issued_by=user,
        name="other-principal",
        permissions=["use_inbox"],
    )
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
    _, result = _call(
        client, "get_conversation_messages", {"conversation_id": str(conversation.pk), "cursor": data["next_cursor"]}
    )
    assert "cursor" in result["error"]["message"]
    own_page = call(client, "get_conversation_messages", {"conversation_id": str(conversation.pk), "limit": 1})
    key.api_key.social_accounts.add(other_account)
    _, result = _call(
        client,
        "get_conversation_messages",
        {"conversation_id": str(conversation.pk), "cursor": own_page["next_cursor"]},
    )
    assert "cursor" in result["error"]["message"]


def test_keyset_snapshot_does_not_shift_after_new_arrival(full_client, conversation):
    expected = [observation(conversation, f"p-{index}") for index in range(5)]
    page = call(full_client, "get_conversation_messages", {"conversation_id": str(conversation.pk), "limit": 2})
    seen = [item["id"] for item in page["items"]]
    new = observation(conversation, "new-arrival")
    while page["next_cursor"]:
        page = call(
            full_client,
            "get_conversation_messages",
            {"conversation_id": str(conversation.pk), "limit": 2, "cursor": page["next_cursor"]},
        )
        seen.extend(item["id"] for item in page["items"])
    assert set(seen) == {str(row.pk) for row in expected}
    assert len(seen) == len(set(seen))
    assert str(new.pk) not in seen


def test_response_is_bounded_and_sanitizes_attachment_projection(full_client, conversation):
    for index in range(30):
        observation(
            conversation,
            f"big-{index}",
            body="訊息" * 3000,
            attachments=[
                {"type": "image", "url": "https://cdninstagram.com/file?access_token=private", "title": "x" * 500}
            ],
        )
    data = call(full_client, "get_conversation_messages", {"conversation_id": str(conversation.pk), "limit": 100})
    encoded = json.dumps(data)
    assert len(encoded) <= 65536
    assert data["has_more"]
    assert data["next_cursor"]
    assert all(item["body_truncated"] for item in data["items"])
    assert "access_token" not in encoded
    assert "private" not in encoded


def test_deleted_message_never_exposes_stale_content(full_client, conversation):
    observation(
        conversation,
        "deleted",
        is_deleted=True,
        body="Withdrawn",
        attachments=[{"type": "image", "url": "https://cdninstagram.com/stale"}],
    )
    item = call(full_client, "get_conversation_messages", {"conversation_id": str(conversation.pk)})["items"][0]
    assert item["body"] == ""
    assert not item["content_available"]
    assert all(not attachment["url"] for attachment in item["attachments"])


def test_comment_success_is_not_dm_freshness(full_client, conversation):
    ConversationSyncState.objects.create(
        workspace=conversation.workspace,
        social_account=conversation.social_account,
        platform=conversation.platform,
        stream="comment",
        status="success",
        last_success_at=timezone.now(),
        coverage="partial",
    )
    data = call(full_client, "get_conversation_messages", {"conversation_id": str(conversation.pk)})
    assert data["sync"]["status"] == "unknown"
    assert data["sync"]["last_success_at"] is None


@pytest.mark.parametrize(
    "platform,implemented", [("facebook", True), ("instagram_login", True), ("instagram", False), ("threads", False)]
)
def test_platform_capabilities_do_not_forge_threads_dm_support(platform, implemented):
    capabilities = conversation_capabilities(platform)
    assert capabilities["native_outgoing_observation"] is implemented
    assert capabilities["dm_history_complete"] is False
    assert capabilities["account_permissions_verified"] is False
    assert capabilities["v2_send_preconditions"] is False
    if not implemented:
        assert capabilities["dm_history_adapter"] == "not_implemented"


@pytest.mark.parametrize("limit", [0, 101, True, "2"])
def test_invalid_limits_fail_closed(full_client, limit):
    _, result = _call(full_client, "list_conversations", {"limit": limit})
    assert "error" in result


def test_read_tools_require_use_inbox(user, memberships, workspace, account):
    from apps.api_keys.services import issue_api_key

    key = issue_api_key(workspace=workspace, social_accounts=[account], issued_by=user, name="no-inbox", permissions=[])
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
    _, result = _call(client, "list_conversations", {})
    assert "Permission denied" in result["error"]["message"]


def test_oauth_actor_can_use_all_three_tools_and_unassigned_query(user, memberships, conversation):
    from apps.mcp.tests.test_oauth_auth import _mint_oauth_token

    user.last_workspace_id = conversation.workspace_id
    user.save(update_fields=["last_workspace_id"])

    original = InboxMessage.objects.create(
        workspace=conversation.workspace,
        social_account=conversation.social_account,
        platform_message_id="oauth-inbound",
        message_type="dm",
        received_at=timezone.now(),
    )
    target = observation(conversation, "oauth-inbound", legacy_message=original)
    observation(conversation, "oauth-outbound", direction="outbound")
    raw = _mint_oauth_token(user)
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert call(client, "list_conversations", {})["items"][0]["id"] == str(conversation.pk)
    page = call(client, "get_conversation_messages", {"conversation_id": str(conversation.pk), "limit": 1})
    assert page["has_more"]
    next_page = call(
        client, "get_conversation_messages", {"conversation_id": str(conversation.pk), "cursor": page["next_cursor"]}
    )
    assert next_page["items"][0]["id"] == str(target.pk)
    assert call(client, "get_reply_context", {"message_id": str(original.pk)})["context_status"] == "available"
    assert (
        call(
            client,
            "get_conversation_messages",
            {"social_account_id": str(conversation.social_account_id), "unassigned_only": True},
        )["items"]
        == []
    )


def test_oauth_revocation_blocks_saved_cursor_before_data_read(user, memberships, conversation):
    from oauth2_provider.models import get_access_token_model

    from apps.mcp.tests.test_oauth_auth import _mint_oauth_token

    user.last_workspace_id = conversation.workspace_id
    user.save(update_fields=["last_workspace_id"])

    observation(conversation, "oauth-revoke-1")
    observation(conversation, "oauth-revoke-2")
    raw = _mint_oauth_token(user)
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {raw}")
    page = call(client, "get_conversation_messages", {"conversation_id": str(conversation.pk), "limit": 1})
    get_access_token_model().objects.filter(user=user).delete()
    status, _result = _call(
        client, "get_conversation_messages", {"conversation_id": str(conversation.pk), "cursor": page["next_cursor"]}
    )
    assert status == 401


@pytest.mark.parametrize("newest_first", [False, True])
def test_synthetic_burst_share_and_final_text_keep_context_and_source_time(full_client, conversation, newest_first):
    """Anonymous scenario; no real person's text, identity or asset is copied."""
    base = timezone.now() - timedelta(minutes=30)
    events = [
        ("prior-outgoing", "outbound", "An earlier response", []),
        ("detail-one", "inbound", "First detail", []),
        ("detail-two", "inbound", "A related question", []),
        ("detail-three", "inbound", "One more detail", []),
        (
            "shared-card",
            "inbound",
            "",
            [
                {
                    "type": "share",
                    "url": "https://www.instagram.com/p/synthetic-test-card/",
                    "title": "Example event card",
                }
            ],
        ),
        ("final-question", "inbound", "A question about that card", []),
    ]
    stored = {}
    originals = {}
    indexed = list(enumerate(events))
    for index, (mid, direction, body, attachments) in reversed(indexed) if newest_first else indexed:
        occurred = base + timedelta(seconds=index)
        original = None
        if direction == "inbound":
            original = InboxMessage.objects.create(
                workspace=conversation.workspace,
                social_account=conversation.social_account,
                platform_message_id=mid,
                message_type="dm",
                body=body,
                received_at=occurred,
            )
            originals[mid] = original
        stored[mid] = observation(
            conversation,
            mid,
            body=body,
            direction=direction,
            attachments=attachments,
            occurred_at=occurred,
            legacy_message=original,
            sender_id="page-1" if direction == "outbound" else "synthetic-peer",
            recipient_id="synthetic-peer" if direction == "outbound" else "page-1",
        )
    target = originals["final-question"]
    data = call(full_client, "get_reply_context", {"message_id": str(target.pk), "limit": 20})
    assert data["target"]["id"] == str(stored["final-question"].pk)
    assert data["ordering"] == "first_seen_at_desc"
    assert not data["has_more"]
    assert len(data["items"]) == 6
    chronological = sorted(data["items"], key=lambda item: item["occurred_at"])
    assert [item["platform_message_id"] for item in chronological] == [item[0] for item in events]
    assert [item["direction"] for item in chronological] == [item[1] for item in events]
    assert chronological[4]["body"] == ""
    assert chronological[4]["attachments"][0]["type"] == "share"
    assert chronological[-1]["legacy_message_id"] == str(target.pk)
    assert data["send_preconditions_enforced"] is False
    assert InboxMessage.objects.count() == 5
    assert target.replies.count() == 0
