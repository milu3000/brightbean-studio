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
