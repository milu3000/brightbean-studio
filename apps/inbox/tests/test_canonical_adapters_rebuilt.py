"""Actual REST/MCP contracts and independent per-user read acknowledgements."""

import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from apps.inbox import canonical_reads as reader
from apps.inbox.models import (
    ConversationMessage,
    ConversationObservationState,
    ConversationReadState,
    InboxConversation,
    InboxMessage,
)
from apps.inbox.tests.test_canonical_reads_rebuilt import context as _context
from apps.inbox.tests.test_canonical_reads_rebuilt import legacy, proof, row
from apps.mcp import conversation_tools
from apps.members.models import WorkspaceMembership
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db
context = _context


def client_for(context):
    return Client(HTTP_AUTHORIZATION="Bearer " + context.key.plaintext_token)


def rpc(context, name, args):
    result = client_for(context).post(
        "/api/v1/mcp/",
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
        ),
        content_type="application/json",
        secure=True,
    )
    assert result.status_code == 200, result.content
    result = result.json()
    return json.loads(result["result"]["content"][0]["text"]) if "result" in result else result


def incoming(context, generation=1, **kw):
    result = row(
        context,
        direction="inbound",
        sender_id="peer",
        recipient_id=context.account.account_platform_id,
        incoming_generation=generation,
        **kw,
    )
    InboxConversation.objects.filter(pk=context.conversation.pk).update(incoming_generation=generation)
    return result


def test_rest_and_mcp_primary_read_same_outgoing_only_source(context):
    message = row(context, body="OUTGOING CANONICAL")
    url = f"/api/v1/inbox-conversations/{context.conversation.pk}"
    response = client_for(context).get(url, secure=True)
    assert response.status_code == 200, response.content
    assert response.json()["messages"][0]["body"] == "OUTGOING CANONICAL"
    assert response["Cache-Control"] == "private, no-store"
    result = rpc(context, "get_conversation_messages", {"conversation_id": str(context.conversation.pk)})
    assert result["messages"][0]["id"] == str(message.pk)
    assert "read_ack_token" not in result
    assert not ConversationReadState.objects.exists() and not InboxMessage.objects.exists()
    result = rpc(context, "list_conversations", {"search": "OUTGOING"})
    assert result["conversations"][0]["id"] == str(context.conversation.pk)


def test_existing_incoming_id_preserves_dto_while_native_uuid_is_explicit(context):
    message = incoming(context, body="AUTHORITATIVE")
    anchor = legacy(context, message)
    client = client_for(context)
    for result in [
        client.get(f"/api/v1/inbox/{anchor.pk}", secure=True).json(),
        rpc(context, "get_inbox_message", {"message_id": str(anchor.pk)}),
    ]:
        assert result["id"] == str(anchor.pk)
        assert result["id_namespace"] == "inbox_message"
        assert result["body"] == "AUTHORITATIVE"
        assert result["status"] == anchor.status and result["replies"] == []
        assert result["canonical_conversation_id"] == str(context.conversation.pk)
    native = incoming(context, generation=2, body="NATIVE ONLY")
    count = InboxMessage.objects.count()
    result = rpc(context, "get_inbox_message", {"message_id": str(native.pk)})
    assert result["id"] == str(native.pk) and result["id_namespace"] == "canonical_message"
    assert result["body"] == "NATIVE ONLY" and result["reply_eligibility"]["allowed"] is False
    assert InboxMessage.objects.count() == count
    assert client.get(f"/api/v1/inbox/{native.pk}", secure=True).json()["id_namespace"] == "canonical_message"
    outgoing = row(context)
    assert "error" in rpc(context, "get_inbox_message", {"message_id": str(outgoing.pk)})


def test_old_list_thread_native_contract_explicit_upgrade_and_public_unchanged(context):
    anchor = legacy(context, incoming(context))
    client = client_for(context)
    result = client.get("/api/v1/inbox/", secure=True)
    assert result.status_code == 409 and result.json()["error"] == "canonical_upgrade_required"
    assert result.json()["canonical_tool"] == "list_conversations"
    with patch("apps.inbox.native_thread_reads.read_native_thread") as native:
        for tool in ["get_inbox_thread", "read_native_inbox_thread"]:
            result = rpc(context, tool, {"message_id": str(anchor.pk)})
            assert result["error"]["data"]["error"] == "canonical_upgrade_required"
        native.assert_not_called()
    public = InboxMessage.objects.create(
        workspace=context.account.workspace,
        social_account=context.account,
        platform_message_id="public-comment",
        message_type="comment",
        body="PUBLIC",
        received_at=timezone.now(),
    )
    result = client.get("/api/v1/inbox/", {"message_type": "comment"}, secure=True)
    assert result.status_code == 200 and result.json()["messages"][0]["id"] == str(public.pk)


@pytest.mark.parametrize("reason", ["withdrawn", "expired", "generation", "native_rebind"])
def test_default_mcp_content_and_attachments_redact_all_alternate_sources(context, settings, reason):
    message = incoming(
        context, body="PRIVATE BODY", attachments=[{"type": "image", "url": "https://example.com/PRIVATE-MEDIA"}]
    )
    anchor = legacy(context, message)
    anchor.extra.update(
        attachment_url="https://example.com/PRIVATE-ALTERNATE", image_url="https://example.com/PRIVATE-IMAGE"
    )
    anchor.save(update_fields=["extra"])
    connection = proof(context, message)
    if reason == "withdrawn":
        ConversationObservationState.objects.filter(message=message).update(withdrawn_at=timezone.now())
    elif reason == "expired":
        ConversationObservationState.objects.filter(message=message).update(
            expired_at=timezone.now() - timedelta(seconds=1)
        )
    elif reason == "generation":
        type(connection).objects.filter(pk=connection.pk).update(generation=uuid4())
    else:
        type(context.account).objects.filter(pk=context.account.pk).update(account_platform_id="rebound")
    for tool, args in [
        ("get_inbox_message", {"message_id": str(anchor.pk)}),
        ("get_conversation_messages", {"conversation_id": str(context.conversation.pk)}),
        ("get_reply_context", {"message_id": str(anchor.pk)}),
        ("get_conversation_attachments", {"message_id": str(message.pk)}),
    ]:
        assert "PRIVATE" not in json.dumps(rpc(context, tool, args))
    # Observed compatibility serializer has its own direct policy check even
    # before canonical presentation, so no raw body or URL can reappear there.
    settings.INBOX_CANONICAL_READ_ENABLED = False
    projected = conversation_tools._message(message)
    assert "PRIVATE" not in json.dumps(projected) and projected["content_available"] is False
    assert "PRIVATE" not in json.dumps(
        rpc(context, "get_conversation_messages", {"conversation_id": str(context.conversation.pk)})
    )


def test_observed_mode_uses_honest_order_direction_and_delivery(context, settings):
    settings.INBOX_CANONICAL_READ_ENABLED = False
    message = row(context, delivery_status="delivery_unverified")
    result = rpc(context, "get_conversation_messages", {"conversation_id": str(context.conversation.pk)})
    assert result["ordering"] == "first_seen_at_desc"
    assert result["items"][0]["direction"] == "outbound"
    assert result["items"][0]["delivery_status"] == "delivery_unverified"
    assert result["items"][0]["id"] == str(message.pk)


def test_flag_off_cannot_reopen_stale_legacy_after_ownership(context, settings):
    message = incoming(context)
    anchor = legacy(context, message)
    proof(context, message)
    settings.INBOX_CANONICAL_READ_ENABLED = False
    result = client_for(context).get(f"/api/v1/inbox/{anchor.pk}", secure=True)
    assert result.status_code == 409 and result.json()["error"] == "canonical_unavailable"
    assert "STALE LEGACY" not in result.content.decode()


def test_read_ack_uses_per_user_max_and_preserves_new_arrival_workflow(context):
    incoming(context, generation=1)
    page = reader.read_conversation(context.scope, context.conversation.pk)
    assert not ConversationReadState.objects.exists()
    assert reader.unread_conversation_count(context.scope) == 1
    incoming(context, generation=2)
    InboxConversation.objects.filter(pk=context.conversation.pk).update(workflow_state="needs_action")
    result = reader.acknowledge_read(context.scope, context.conversation.pk, page["read_ack_token"])
    assert result["read_state"] == {"read_generation": 1, "incoming_generation": 2, "unread": True}
    assert result["workflow_state"] == "needs_action"
    latest = reader.read_conversation(context.scope, context.conversation.pk)
    reader.acknowledge_read(context.scope, context.conversation.pk, latest["read_ack_token"])
    reader.acknowledge_read(context.scope, context.conversation.pk, page["read_ack_token"])
    assert reader.unread_conversation_count(context.scope) == 0
    from apps.accounts.models import User

    another = User.objects.create_user(email="another-reader@example.test", password="x")
    WorkspaceMembership.objects.create(user=another, workspace=context.account.workspace, workspace_role="owner")
    other_scope = reader.session_read_scope(another, context.account.workspace_id)
    assert reader.unread_conversation_count(other_scope) == 1
    with pytest.raises(reader.CanonicalReadError):
        reader.acknowledge_read(other_scope, context.conversation.pk, latest["read_ack_token"])


@pytest.mark.parametrize("tombstone", ["withdrawn", "expired", None])
def test_rendered_tombstone_acknowledges_metadata_with_exact_notification_cas(context, tombstone):
    message = incoming(context)
    if tombstone:
        proof(
            context,
            message,
            **(
                {"withdrawn_at": timezone.now()}
                if tombstone == "withdrawn"
                else {"expired_at": timezone.now() - timedelta(seconds=1)}
            ),
        )
    notice = Notification.objects.create(
        user=context.user,
        workspace=context.account.workspace,
        conversation=context.conversation,
        event_type="new_inbox_message",
        title="Inbox",
        source_revision=1,
    )
    page = reader.read_conversation(context.scope, context.conversation.pk)
    if tombstone:
        assert page["messages"][0]["body"] == ""
    assert page["read_ack_token"]
    Notification.objects.filter(pk=notice.pk).update(revision=2, source_revision=2)
    result = reader.acknowledge_read(context.scope, context.conversation.pk, page["read_ack_token"])
    notice.refresh_from_db()
    assert not notice.is_read and notice.read_revision == 0
    assert result["read_state"]["unread"] is False
    assert not notice.dismissed_at


def test_ack_bound_to_rendered_page_and_current_grants(context):
    old = incoming(context, generation=1, occurred_at=timezone.now() - timedelta(days=1))
    incoming(context, generation=2)
    newest = reader.read_conversation(context.scope, context.conversation.pk, limit=1)
    older = reader.read_conversation(context.scope, context.conversation.pk, limit=1, cursor=newest["next_cursor"])
    assert older["messages"][0]["id"] == str(old.pk)
    result = reader.acknowledge_read(context.scope, context.conversation.pk, older["read_ack_token"])
    assert result["read_state"]["read_generation"] == 1 and result["read_state"]["unread"]
    context.member.delete()
    with pytest.raises(reader.CanonicalReadError):
        reader.acknowledge_read(context.scope, context.conversation.pk, newest["read_ack_token"])


def test_rest_ack_is_explicit_post(context):
    incoming(context)
    client = client_for(context)
    url = f"/api/v1/inbox-conversations/{context.conversation.pk}"
    page = client.get(url, secure=True).json()
    assert not ConversationReadState.objects.exists()
    result = client.post(
        url + "/read",
        data=json.dumps({"read_ack_token": page["read_ack_token"]}),
        content_type="application/json",
        secure=True,
    )
    assert result.status_code == 200, result.content
    assert result.json()["read_state"]["unread"] is False


@pytest.mark.django_db(transaction=True)
def test_postgres_concurrent_read_ack_keeps_maximum(context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connection

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks are required")
    incoming(context, generation=1)
    first = reader.read_conversation(context.scope, context.conversation.pk)["read_ack_token"]
    incoming(context, generation=2)
    second = reader.read_conversation(context.scope, context.conversation.pk)["read_ack_token"]
    barrier = Barrier(2)

    def acknowledge(token):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return reader.acknowledge_read(context.scope, context.conversation.pk, token)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acknowledge, [second, first]))
    assert len(results) == 2
    assert ConversationReadState.objects.get(conversation=context.conversation, user=context.user).read_generation == 2


def test_legacy_body_search_never_matches_shadowed_raw_body(context):
    from apps.inbox.canonical_compat import legacy_body_search_query

    canonical = incoming(context, body="SAFE CANONICAL", is_deleted=True)
    anchor = legacy(context, canonical)
    # Same native ID retains the privacy shadow even if its FK is absent.
    ConversationMessage.objects.filter(pk=canonical.pk).update(legacy_message=None)
    unlinked = InboxMessage.objects.create(
        workspace=context.account.workspace,
        social_account=context.account,
        message_type="dm",
        platform_message_id="unlinked",
        body="ORIGINAL BODY",
        received_at=timezone.now(),
    )
    assert not InboxMessage.objects.filter(legacy_body_search_query("STALE LEGACY")).exists()
    assert list(
        InboxMessage.objects.filter(legacy_body_search_query("ORIGINAL BODY")).values_list("pk", flat=True)
    ) == [unlinked.pk]
    assert not InboxMessage.objects.filter(legacy_body_search_query("SAFE CANONICAL"), pk=anchor.pk).exists()


def test_ack_cannot_follow_conversation_into_another_already_allowed_account(context, enroll_conversation_accounts):
    from apps.social_accounts.models import SocialAccount

    second = SocialAccount.objects.create(
        workspace=context.account.workspace,
        platform=context.account.platform,
        account_platform_id="other-native-account",
        account_name="Other",
        oauth_access_token="synthetic",
    )
    enroll_conversation_accounts(second, read=True)
    message = incoming(context)
    page = reader.read_conversation(context.scope, context.conversation.pk)
    InboxConversation.objects.filter(pk=context.conversation.pk).update(social_account=second)
    ConversationMessage.objects.filter(pk=message.pk).update(social_account=second)
    with pytest.raises(reader.CanonicalReadError):
        reader.acknowledge_read(context.scope, context.conversation.pk, page["read_ack_token"])
    assert not ConversationReadState.objects.exists()


def test_mapped_get_retains_typed_attachment_contract_without_internal_ids(context):
    message = incoming(
        context, attachments=[{"id": "internal-provider-id", "type": "image", "url": "https://example.com/safe"}]
    )
    anchor = legacy(context, message)
    result = rpc(context, "get_inbox_message", {"message_id": str(anchor.pk)})
    assert result["attachments"][0]["url"] == "https://example.com/safe"
    assert "internal-provider-id" not in json.dumps(result)
    assert "id" not in result["attachments"][0]


def test_canonical_only_get_never_loads_restricted_archive_columns(context):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    message = incoming(context)
    proof(context, message, withdrawn_at=timezone.now(), retained_body="PRIVATE ARCHIVE")
    with CaptureQueriesContext(connection) as queries:
        value = reader.read_canonical_incoming_message(context.scope, message.pk)
    assert value["body"] == ""
    selects = " ".join(item["sql"] for item in queries if item["sql"].startswith("SELECT"))
    assert "retained_body" not in selects and "retained_attachments" not in selects


def test_planned_deadline_does_not_activate_expiry_in_default_read_search_or_compatibility(context):
    message = incoming(
        context, body="PLANNED DEADLINE VISIBLE", attachments=[{"type": "share", "url": "https://example.com/retained"}]
    )
    anchor = legacy(context, message)
    proof(context, message, expires_at=timezone.now() - timedelta(days=400))
    from apps.inbox.canonical_content import visible_content

    content = visible_content(message)
    assert content["available"] and not content["is_expired"]
    assert reader.list_conversations(context.scope, search="PLANNED DEADLINE")["conversations"]
    value = rpc(context, "get_inbox_message", {"message_id": str(anchor.pk)})
    assert value["body"] == "PLANNED DEADLINE VISIBLE" and value["attachments"][0]["url"]
    assert conversation_tools._message(message)["content_available"]
    ConversationObservationState.objects.filter(message=message).update(expired_at=timezone.now())
    assert visible_content(message)["is_expired"] and visible_content(message)["body"] == ""
    assert not reader.list_conversations(context.scope, search="PLANNED DEADLINE")["conversations"]


def test_explicit_row_expiry_without_sidecar_still_redacts(context):
    message = incoming(context, body="EXPLICITLY EXPIRED", content_status="expired")
    from apps.inbox.canonical_content import visible_content

    assert visible_content(message)["is_expired"] and not visible_content(message)["available"]


@pytest.fixture
def mixed_accounts(context):
    from apps.social_accounts.models import SocialAccount

    context.account.account_name = "IRL canonical"
    context.account.save(update_fields=["account_name"])
    account = SocialAccount.objects.create(
        workspace=context.account.workspace,
        platform="facebook",
        account_platform_id="synthetic-dj-page",
        account_name="DJ existing inbox",
        oauth_access_token="synthetic",
    )
    context.key.api_key.social_accounts.add(account)
    message = InboxMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform_message_id="synthetic-dj-message",
        message_type="dm",
        sender_name="Existing customer",
        sender_handle="synthetic-dj-peer",
        body="DJ original message",
        received_at=timezone.now(),
        extra={"conversation_type": "direct", "classification_reason": "participants_pair"},
    )
    return account, message


def session_feed(context, client, **params):
    from django.urls import reverse

    client.force_login(context.user)
    return client.get(reverse("inbox:feed", kwargs={"workspace_id": context.account.workspace_id}), params)


def test_mixed_scope_exposes_same_account_entry_points_on_ui_rest_and_mcp(context, mixed_accounts, client):
    account, original = mixed_accounts
    anchor = legacy(context, incoming(context, body="IRL canonical truth"))
    counts = (InboxMessage.objects.count(), ConversationReadState.objects.count())
    page = session_feed(context, client)
    assert page.status_code == 200
    assert "IRL canonical truth" in page.content.decode()
    assert "STALE LEGACY BODY" not in page.content.decode()
    sources = page.context["account_sources"]
    by_id = {item["id"]: item for item in sources}
    assert by_id[str(context.account.pk)]["source"] == "canonical"
    entry = by_id[str(account.pk)]
    assert entry["source"] == "legacy"
    assert entry["tool"] == "list_inbox_messages"
    assert entry["arguments"] == {"social_account_id": str(account.pk), "message_type": "dm"}
    assert entry["inbox_url"].replace("&", "&amp;") in page.content.decode()
    assert "DJ existing inbox" in page.content.decode()
    for result in [
        client_for(context).get("/api/v1/inbox-conversations/", secure=True).json(),
        rpc(context, "list_conversations", {}),
    ]:
        assert result["account_sources"] == sources
        assert [item["id"] for item in result["conversations"]] == [str(context.conversation.pk)]
    for result in [
        client_for(context).get("/api/v1/inbox/", secure=True).json(),
        rpc(context, "list_inbox_messages", {})["error"]["data"],
    ]:
        assert result["error"] == "canonical_upgrade_required"
        assert result["account_sources"] == sources
    legacy_page = client.get(entry["inbox_url"])
    assert legacy_page.status_code == 200
    assert [message.pk for message in legacy_page.context["inbox_messages"]] == [original.pk]
    assert legacy_page.context["active_filters"]["account"] == [str(account.pk)]
    assert "STALE LEGACY BODY" not in legacy_page.content.decode()
    for result in [
        client_for(context).get(entry["api"], secure=True).json(),
        rpc(context, entry["tool"], entry["arguments"]),
    ]:
        assert [item["id"] for item in result["messages"]] == [str(original.pk)]
        assert str(anchor.pk) not in json.dumps(result)
    assert counts == (InboxMessage.objects.count(), ConversationReadState.objects.count())


def test_global_canonical_rollout_does_not_change_empty_cohort_workspace(context, mixed_accounts, client):
    from apps.api_keys.services import issue_api_key
    from apps.workspaces.models import Workspace

    account, original = mixed_accounts
    workspace = Workspace.objects.create(name="No cohort", organization=context.account.workspace.organization)
    WorkspaceMembership.objects.create(user=context.user, workspace=workspace, workspace_role="owner")
    account.workspace = workspace
    account.save(update_fields=["workspace"])
    original.workspace = workspace
    original.save(update_fields=["workspace"])
    context.account = account
    context.key = issue_api_key(
        workspace=workspace,
        social_accounts=[account],
        issued_by=context.user,
        name="No cohort",
        permissions=["use_inbox"],
    )
    page = session_feed(context, client)
    assert page.status_code == 200 and page.context["canonical_mode"] is False
    assert [item.pk for item in page.context["inbox_messages"]] == [original.pk]
    assert "inbox-native-thread.js" in page.content.decode()
    for result in [
        client_for(context).get("/api/v1/inbox/", secure=True).json(),
        rpc(context, "list_inbox_messages", {}),
    ]:
        assert [item["id"] for item in result["messages"]] == [str(original.pk)]


@pytest.mark.parametrize("cohort_state", ["paused", "withdrawn", "reader_disabled"])
def test_owned_source_never_revives_legacy_and_unowned_account_remains_reachable(
    context, mixed_accounts, client, settings, cohort_state
):
    account, original = mixed_accounts
    incoming_row = incoming(context, body="CANONICAL ONLY")
    anchor = legacy(context, incoming_row)
    connection = proof(context, incoming_row)
    type(connection).objects.filter(pk=connection.pk).update(enabled=False)
    if cohort_state == "withdrawn":
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    elif cohort_state == "reader_disabled":
        settings.INBOX_CANONICAL_READ_ENABLED = False
    page = session_feed(context, client, account=str(context.account.pk))
    rest = client_for(context).get("/api/v1/inbox/", {"social_account_id": str(context.account.pk)}, secure=True)
    tool = rpc(context, "list_inbox_messages", {"social_account_id": str(context.account.pk)})
    assert "STALE LEGACY BODY" not in page.content.decode() + rest.content.decode() + json.dumps(tool)
    if cohort_state == "paused":
        assert page.status_code == 200 and "CANONICAL ONLY" in page.content.decode()
        assert rest.json()["error"] == tool["error"]["data"]["error"] == "canonical_upgrade_required"
    else:
        assert page.status_code == rest.status_code == 409
        assert rest.json()["error"] == tool["error"]["data"]["error"] == "canonical_unavailable"
        detail = client.get(f"/workspace/{context.account.workspace_id}/inbox/{anchor.pk}/")
        assert detail.status_code == 409
    legacy_page = session_feed(context, client, account=str(account.pk))
    assert legacy_page.status_code == 200
    assert [item.pk for item in legacy_page.context["inbox_messages"]] == [original.pk]
    for result in [
        client_for(context).get("/api/v1/inbox/", {"social_account_id": str(account.pk)}, secure=True).json(),
        rpc(context, "list_inbox_messages", {"social_account_id": str(account.pk)}),
    ]:
        assert [item["id"] for item in result["messages"]] == [str(original.pk)]


def test_account_source_discovery_respects_key_allowlist_and_workspace(context, mixed_accounts, client):
    from apps.workspaces.models import Workspace

    account, original = mixed_accounts
    context.key.api_key.social_accounts.remove(account)
    rest = client_for(context).get("/api/v1/inbox-conversations/", secure=True)
    assert rest.status_code == 200
    assert all(item["id"] != str(account.pk) for item in rest.json()["account_sources"])
    assert "DJ existing inbox" not in json.dumps(rpc(context, "list_conversations", {}))
    assert (
        client_for(context).get("/api/v1/inbox/", {"social_account_id": str(account.pk)}, secure=True).status_code
        == 404
    )
    assert "error" in rpc(context, "list_inbox_messages", {"social_account_id": str(account.pk)})
    foreign = Workspace.objects.create(name="Foreign", organization=context.account.workspace.organization)
    account.workspace = foreign
    account.save(update_fields=["workspace"])
    context.key.api_key.social_accounts.add(account)
    page = session_feed(context, client, account=str(account.pk))
    assert page.status_code == 404 and original.body not in page.content.decode()
    assert (
        client_for(context).get("/api/v1/inbox/", {"social_account_id": str(account.pk)}, secure=True).status_code
        == 404
    )
    assert "error" in rpc(context, "list_inbox_messages", {"social_account_id": str(account.pk)})


def test_revoked_membership_cannot_discover_any_account_source(context, mixed_accounts, client):
    context.member.delete()
    page = session_feed(context, client)
    assert page.status_code in {403, 404}
    assert "DJ existing inbox" not in page.content.decode()
    rest = client_for(context).get("/api/v1/inbox/", secure=True)
    assert rest.status_code == 401
    assert "DJ existing inbox" not in rest.content.decode()
    tool = client_for(context).post(
        "/api/v1/mcp/",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_inbox_messages", "arguments": {}},
            }
        ),
        content_type="application/json",
        secure=True,
    )
    assert tool.status_code == 401 and "DJ existing inbox" not in tool.content.decode()


def test_mixed_source_htmx_account_changes_reload_the_matching_reader(context, mixed_accounts, client):
    from django.urls import reverse

    account, _original = mixed_accounts
    client.force_login(context.user)
    url = reverse("inbox:feed", kwargs={"workspace_id": account.workspace_id})
    for source, selected in [("canonical", account.pk), ("legacy", context.account.pk)]:
        response = client.get(
            url, {"account": str(selected), "domain": "dm", "read_source": source}, HTTP_HX_REQUEST="true"
        )
        assert response.status_code == 200 and response.content == b""
        assert "HX-Redirect" in response
        redirected = client.get(response["HX-Redirect"])
        assert redirected.status_code == 200
        expected_script = "inbox-native-thread.js" if source == "canonical" else "inbox-canonical.js"
        assert expected_script in redirected.content.decode()
        assert str(selected) in response["HX-Redirect"]


@pytest.mark.parametrize("surface", ["session", "rest", "mcp"])
@pytest.mark.django_db(transaction=True)
def test_unowned_legacy_dm_draft_and_send_stay_available_during_other_account_rollout(
    context, mixed_accounts, client, surface
):
    from apps.inbox.models import InboxReply

    account, original = mixed_accounts
    context.key.api_key.permissions = ["use_inbox", "reply_from_inbox"]
    context.key.api_key.save(update_fields=["permissions"])
    client.force_login(context.user)
    root = f"/workspace/{account.workspace_id}/inbox/"
    detail = client.get(root + f"{original.pk}/")
    assert detail.status_code == 200 and original.body in detail.content.decode()
    if surface == "session":
        response = client.post(root + f"{original.pk}/reply/draft/", {"body": "Original-route draft"})
        assert response.status_code == 200 and b"Original-route draft" in response.content
    elif surface == "rest":
        response = client_for(context).post(
            f"/api/v1/inbox/{original.pk}/replies",
            data=json.dumps({"body": "Original-route draft"}),
            content_type="application/json",
            secure=True,
        )
        assert response.status_code == 201, response.content
    else:
        result = rpc(context, "create_reply_draft", {"message_id": str(original.pk), "body": "Original-route draft"})
        assert "error" not in result, result
    reply = InboxReply.objects.get(inbox_message=original)
    assert reply.status == "draft" and reply.conversation_id is None
    with patch("apps.inbox.services._dispatch_to_platform", return_value="synthetic-legacy-reply") as provider:
        if surface == "session":
            response = client.post(root + f"replies/{reply.pk}/send/")
            assert response.status_code == 200
        elif surface == "rest":
            response = client_for(context).post(f"/api/v1/inbox/replies/{reply.pk}/send", secure=True)
            assert response.status_code == 200, response.content
        else:
            result = rpc(context, "send_reply", {"reply_id": str(reply.pk)})
            assert "error" not in result, result
    provider.assert_called_once()
    reply.refresh_from_db()
    assert reply.status == "sent" and reply.platform_reply_id == "synthetic-legacy-reply"


def test_mixed_workspace_legacy_only_platform_filter_retains_original_feed(context, mixed_accounts, client):
    account, original = mixed_accounts
    account.platform = "instagram"
    account.save(update_fields=["platform"])
    page = session_feed(context, client, platform="instagram")
    assert page.status_code == 200
    assert [item.pk for item in page.context["inbox_messages"]] == [original.pk]
    assert page.context["active_filters"]["platform"] == ["instagram"]
    assert "inbox-native-thread.js" in page.content.decode()


def test_public_sections_never_include_canonical_dm_shadows(context, mixed_accounts, client):
    anchor = legacy(context, incoming(context))
    public = InboxMessage.objects.create(
        workspace=context.account.workspace,
        social_account=context.account,
        message_type="comment",
        platform_message_id="synthetic-public",
        body="Public comment",
        received_at=timezone.now(),
    )
    page = session_feed(context, client, domain="comment")
    assert page.status_code == 200
    assert [item.pk for item in page.context["inbox_messages"]] == [public.pk]
    assert str(anchor.pk) not in page.content.decode()
    for result in [
        client_for(context).get("/api/v1/inbox/", {"message_type": "comment"}, secure=True).json(),
        rpc(context, "list_inbox_messages", {"message_type": "comment"}),
    ]:
        assert [item["id"] for item in result["messages"]] == [str(public.pk)]


@pytest.mark.parametrize("withdrawal", ["enrollment", "reader"])
def test_unfiltered_mixed_held_workspace_keeps_other_account_links_discoverable(
    context, mixed_accounts, client, settings, withdrawal
):
    account, original = mixed_accounts
    message = incoming(context, body="HELD CANONICAL CONTENT")
    legacy(context, message)
    proof(context, message)
    if withdrawal == "enrollment":
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    else:
        settings.INBOX_CANONICAL_READ_ENABLED = False
    page = session_feed(context, client)
    assert page.status_code == 409
    assert "Some saved DM accounts are unavailable" in page.content.decode()
    sources = page.context["account_sources"]
    legacy_entry = next(item for item in sources if item["id"] == str(account.pk))
    assert legacy_entry["source"] == "legacy"
    assert legacy_entry["inbox_url"].replace("&", "&amp;") in page.content.decode()
    assert "DJ existing inbox" in page.content.decode()
    assert "HELD CANONICAL CONTENT" not in page.content.decode() and "STALE LEGACY BODY" not in page.content.decode()
    reached = client.get(legacy_entry["inbox_url"])
    assert reached.status_code == 200 and original.body in reached.content.decode()
    for result in [
        client_for(context).get("/api/v1/inbox/", secure=True).json(),
        rpc(context, "list_inbox_messages", {})["error"]["data"],
    ]:
        assert result["error"] == "canonical_unavailable"
        assert result["account_sources"] == sources
    result = client_for(context).get(legacy_entry["api"], secure=True)
    assert result.status_code == 200
    assert [item["id"] for item in result.json()["messages"]] == [str(original.pk)]


def claim_synthetic_source(account):
    from apps.inbox.models import InboxSyncConnection

    return InboxSyncConnection.objects.create(
        social_account=account,
        workspace=account.workspace,
        platform=account.platform,
        account_platform_id=account.account_platform_id,
        auth_fingerprint="synthetic",
        enabled=False,
        ownership_claimed_at=timezone.now(),
    )


@pytest.mark.parametrize("boundary", ["page", "render"])
def test_legacy_ui_discards_body_if_ownership_changes_before_response(context, mixed_accounts, client, boundary):
    from apps.inbox import views

    account, original = mixed_accounts
    target = "apps.inbox.views.presentation.inbox_page" if boundary == "page" else "apps.inbox.views.render"
    actual = views.presentation.inbox_page if boundary == "page" else views.render

    def claim_after_read(*args, **kwargs):
        value = actual(*args, **kwargs)
        claim_synthetic_source(account)
        return value

    with patch(target, side_effect=claim_after_read):
        response = session_feed(context, client, account=str(account.pk))
    assert response.status_code == 409
    assert original.body not in response.content.decode()


@pytest.mark.parametrize("surface", ["rest", "mcp"])
@pytest.mark.parametrize("change", ["ownership", "grant", "native_identity"])
def test_legacy_lists_recheck_scope_after_serializing_rows(context, mixed_accounts, surface, change):
    from apps.api.schemas import InboxMessageResponse

    account, original = mixed_accounts
    actual = InboxMessageResponse.from_message

    def revoke_after_read(*args, **kwargs):
        value = actual(*args, **kwargs)
        if change == "ownership":
            claim_synthetic_source(account)
        elif change == "grant":
            context.key.api_key.social_accounts.remove(account)
        else:
            type(account).objects.filter(pk=account.pk).update(account_platform_id="changed-native-owner")
        return value

    with patch("apps.api.schemas.InboxMessageResponse.from_message", side_effect=revoke_after_read):
        if surface == "rest":
            response = client_for(context).get("/api/v1/inbox/", {"social_account_id": str(account.pk)}, secure=True)
            assert response.status_code in {404, 409}
            assert original.body not in response.content.decode()
        else:
            result = rpc(context, "list_inbox_messages", {"social_account_id": str(account.pk)})
            assert "error" in result
            assert original.body not in json.dumps(result)


def test_healthy_selected_canonical_account_and_direct_reads_ignore_held_sibling(context, mixed_accounts, client):
    from django.urls import reverse

    sibling, _original = mixed_accounts
    claim_synthetic_source(sibling)
    message = incoming(context, body="Healthy canonical message")
    anchor = legacy(context, message)
    page = session_feed(context, client, account=str(context.account.pk))
    assert page.status_code == 200 and message.body in page.content.decode()
    for result in [
        client_for(context)
        .get("/api/v1/inbox-conversations/", {"social_account_id": str(context.account.pk)}, secure=True)
        .json(),
        rpc(context, "list_conversations", {"social_account_id": str(context.account.pk)}),
    ]:
        assert [item["id"] for item in result["conversations"]] == [str(context.conversation.pk)]
        assert [item["id"] for item in result["account_sources"]] == [str(context.account.pk)]
    detail_url = reverse(
        "inbox:conversation_detail",
        kwargs={"workspace_id": context.account.workspace_id, "conversation_id": context.conversation.pk},
    )
    detail = client.get(detail_url)
    assert detail.status_code == 200 and message.body in detail.content.decode()
    assert client.get(f"/workspace/{context.account.workspace_id}/inbox/{anchor.pk}/").status_code == 200
    for result in [
        client_for(context).get(f"/api/v1/inbox-conversations/{context.conversation.pk}", secure=True).json(),
        rpc(context, "get_conversation_messages", {"conversation_id": str(context.conversation.pk)}),
    ]:
        assert [item["body"] for item in result["messages"]] == [message.body]
    for identifier in [anchor.pk, message.pk]:
        assert client_for(context).get(f"/api/v1/inbox/{identifier}", secure=True).json()["body"] == message.body
        assert rpc(context, "get_inbox_message", {"message_id": str(identifier)})["body"] == message.body
    assert reader.read_message_body(context.scope, message.pk)["body"] == message.body
    reader.verify_composer_observation(
        context.scope, context.conversation.pk, detail.context["composer_observation_token"]
    )
    acknowledged = reader.acknowledge_read(context.scope, context.conversation.pk, detail.context["read_ack_token"])
    assert acknowledged["read_state"]["unread"] is False
    acknowledgement_url = reverse(
        "inbox:conversation_read_ack",
        kwargs={
            "workspace_id": context.account.workspace_id,
            "conversation_id": context.conversation.pk,
        },
    )
    response = client.post(acknowledgement_url, {"read_ack_token": detail.context["read_ack_token"]})
    assert response.status_code == 200 and "unread_count" not in response.json()
    # The held account is still held when explicitly selected, rather than skipped.
    assert session_feed(context, client, account=str(sibling.pk)).status_code == 409
    assert (
        client_for(context)
        .get("/api/v1/inbox-conversations/", {"social_account_id": str(sibling.pk)}, secure=True)
        .status_code
        == 409
    )


def test_healthy_canonical_platform_scope_does_not_include_held_sibling(context, mixed_accounts, client):
    sibling, _original = mixed_accounts
    sibling.platform = "instagram_login"
    sibling.save(update_fields=["platform"])
    claim_synthetic_source(sibling)
    message = incoming(context, body="Facebook saved message")
    page = session_feed(context, client, platform="facebook")
    assert page.status_code == 200 and message.body in page.content.decode()
    for result in [
        client_for(context).get("/api/v1/inbox-conversations/", {"platform": "facebook"}, secure=True).json(),
        rpc(context, "list_conversations", {"platform": "facebook"}),
    ]:
        assert [item["id"] for item in result["conversations"]] == [str(context.conversation.pk)]
        assert [item["id"] for item in result["account_sources"]] == [str(context.account.pk)]


def test_selected_read_ack_keeps_workspace_unread_count_for_other_readable_account(
    context, mixed_accounts, client, enroll_conversation_accounts
):
    from types import SimpleNamespace

    from django.urls import reverse

    sibling, _original = mixed_accounts
    enroll_conversation_accounts(sibling, read=True)
    other_conversation = InboxConversation.objects.create(
        workspace=sibling.workspace,
        social_account=sibling,
        platform=sibling.platform,
        platform_conversation_id="synthetic-other-readable-thread",
        peer_id="other-peer",
        identity_kind="platform",
        conversation_type="direct",
        classification_reason="participants_pair",
    )
    incoming(context, body="Read account A")
    incoming(SimpleNamespace(account=sibling, conversation=other_conversation), body="Unread account B")
    page = reader.read_conversation(context.scope, context.conversation.pk)
    assert reader.unread_conversation_count(context.scope) == 2
    client.force_login(context.user)
    url = reverse(
        "inbox:conversation_read_ack",
        kwargs={"workspace_id": context.account.workspace_id, "conversation_id": context.conversation.pk},
    )
    response = client.post(url, {"read_ack_token": page["read_ack_token"]})
    assert response.status_code == 200
    assert response.json()["read_state"]["unread"] is False
    assert response.json()["unread_count"] == 1
    assert reader.unread_conversation_count(context.scope) == 1
    assert not ConversationReadState.objects.filter(conversation=other_conversation).exists()
