"""Verified unknown/group content stays usable without inventing a thread."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.inbox import canonical_reads as reader
from apps.inbox import unassigned_reads
from apps.inbox.canonical_content import visible_content
from apps.inbox.models import (
    ConversationMessage,
    ConversationObservationState,
    ConversationReadState,
    InboxConversation,
    InboxMessage,
    InboxReply,
    InboxSyncConnection,
)
from apps.inbox.tests.test_basic_recovery import browser as _browser
from apps.inbox.tests.test_basic_recovery import get, link, url
from apps.inbox.tests.test_canonical_adapters_rebuilt import client_for, rpc
from apps.inbox.tests.test_canonical_reads_rebuilt import context as _context
from apps.inbox.tests.test_canonical_reads_rebuilt import row

context = _context
browser = _browser
pytestmark = pytest.mark.django_db


def unassigned(context, *, proof=True, **overrides):
    message = row(
        context,
        conversation=None,
        direction="unknown",
        sender_id="unknown-peer",
        conversation_type="group",
        **overrides,
    )
    if proof:
        connection, _ = InboxSyncConnection.objects.get_or_create(
            social_account=context.account,
            defaults={
                "workspace": context.account.workspace,
                "platform": context.account.platform,
                "account_platform_id": context.account.account_platform_id,
                "webhook_target_id": context.account.webhook_target_id,
                "auth_fingerprint": "synthetic-signed-proof",
                "ownership_claimed_at": timezone.now(),
            },
        )
        ConversationObservationState.objects.create(
            message=message,
            connection_generation=connection.generation,
            last_observed_at=timezone.now(),
            expires_at=timezone.now() - timedelta(days=1),
        )
    return message


def test_verified_unassigned_group_body_is_visible_without_thread_or_action(context, browser):
    before = InboxConversation.objects.count()
    message = unassigned(context, body="UNKNOWN GROUP CONTENT", occurred_at=None)
    value = reader.read_unassigned_message(context.scope, message.pk)
    assert value["conversation_id"] is None and value["message"]["conversation_id"] is None
    assert value["message"]["body"] == "UNKNOWN GROUP CONTENT" and value["message"]["timestamp_missing"]
    assert not value["send_authorized"] and not value["read_tracking_available"]
    assert value["message"]["incoming_generation"] is None
    assert visible_content(message)["body"] == "UNKNOWN GROUP CONTENT"
    listing = reader.list_unassigned_messages(context.scope, search="GROUP CONTENT")
    assert listing["messages"][0]["id"] == str(message.pk)
    assert listing["ordering"] == "first_observed_desc_not_send_time"
    assert reader.list_conversations(context.scope)["conversations"] == []
    assert InboxConversation.objects.count() == before
    assert (
        not InboxMessage.objects.exists()
        and not InboxReply.objects.exists()
        and not ConversationReadState.objects.exists()
    )
    detail = get(browser, url(context, "basic_unassigned_detail", message_id=message.pk))
    assert detail.status_code == 200 and "UNKNOWN GROUP CONTENT" in detail.content.decode()
    assert "Time unavailable" in detail.content.decode() and "Read only" in detail.content.decode()
    result = rpc(context, "get_unassigned_message", {"message_id": str(message.pk)})
    assert result["message"]["body"] == "UNKNOWN GROUP CONTENT"
    result = rpc(
        context, "get_conversation_messages", {"social_account_id": str(context.account.pk), "unassigned_only": True}
    )
    assert result["messages"][0]["id"] == str(message.pk)
    client = client_for(context)
    assert client.get("/api/v1/inbox-conversations/unassigned", secure=True).json()["messages"][0]["id"] == str(
        message.pk
    )
    assert (
        client.get(f"/api/v1/inbox-conversations/unassigned/{message.pk}", secure=True).json()["conversation_id"]
        is None
    )


@pytest.mark.parametrize("restriction", ["withdrawn", "expired", "generation", "native", "unproved"])
def test_unassigned_current_provenance_and_applied_privacy_are_required(context, restriction):
    message = unassigned(
        context,
        proof=restriction != "unproved",
        body="PRIVATE UNASSIGNED",
        attachments=[{"type": "share", "url": "https://example.com/PRIVATE"}],
    )
    if restriction == "withdrawn":
        ConversationObservationState.objects.filter(message=message).update(withdrawn_at=timezone.now())
    elif restriction == "expired":
        ConversationObservationState.objects.filter(message=message).update(expired_at=timezone.now())
    elif restriction == "generation":
        ConversationObservationState.objects.filter(message=message).update(connection_generation=uuid4())
    elif restriction == "native":
        type(context.account).objects.filter(pk=context.account.pk).update(account_platform_id="other-native")
    if restriction in {"withdrawn", "expired"}:
        value = reader.read_unassigned_message(context.scope, message.pk)
        assert value["message"]["body"] == "" and value["message"]["attachments"] == []
    else:
        with pytest.raises(reader.CanonicalReadError):
            reader.read_unassigned_message(context.scope, message.pk)
    if restriction != "native":
        assert reader.list_unassigned_messages(context.scope, search="PRIVATE")["messages"] == []


def test_unassigned_body_media_continuations_and_native_assignment_stop_old_routes(context, browser):
    message = unassigned(
        context,
        body="G" * 6000 + " END GROUP",
        attachments=[
            {"type": "share", "url": f"https://example.com/{i}", "title": f"GROUP MEDIA {i}"} for i in range(6)
        ],
    )
    detail = get(browser, url(context, "basic_unassigned_detail", message_id=message.pk))
    response = get(browser, link(detail, "Read full text"))
    for _index in range(6):
        if not response.context["next_url"]:
            break
        response = get(browser, link(response, "Continue reading"))
    assert "END GROUP" in response.content.decode()
    media = get(browser, link(detail, "All attachments"))
    assert "GROUP MEDIA 0" in media.content.decode()
    media = get(browser, link(media, "Continue reading"))
    assert "GROUP MEDIA 5" in media.content.decode()
    first = reader.read_unassigned_message_body(context.scope, message.pk)
    ConversationMessage.objects.filter(pk=message.pk).update(
        conversation=context.conversation, updated_at=timezone.now()
    )
    with pytest.raises(reader.CanonicalReadError):
        reader.read_unassigned_message_body(context.scope, message.pk, cursor=first["next_cursor"])
    assert reader.list_unassigned_messages(context.scope)["messages"] == []


def test_unassigned_paging_account_isolation_and_revocation(context):
    for _ in range(4):
        unassigned(context)
    page = reader.list_unassigned_messages(context.scope, limit=2)
    older = reader.list_unassigned_messages(context.scope, limit=2, cursor=page["next_cursor"])
    assert set(item["id"] for item in older["messages"]).isdisjoint(item["id"] for item in page["messages"])
    with pytest.raises(reader.CanonicalReadError):
        reader.list_unassigned_messages(context.scope, social_account_id=uuid4())
    context.member.delete()
    with pytest.raises(reader.CanonicalReadError):
        reader.list_unassigned_messages(context.scope, cursor=page["next_cursor"], limit=2)


def test_unassigned_policy_change_during_projection_fails_before_return(context):
    message = unassigned(context, body="PRIVATE RACE")
    project = unassigned_reads._project
    changed = False

    def withdraw(row, account):
        nonlocal changed
        value = project(row, account)
        if not changed:
            ConversationObservationState.objects.filter(message=message).update(withdrawn_at=timezone.now())
            changed = True
        return value

    with patch.object(unassigned_reads, "_project", side_effect=withdraw), pytest.raises(reader.CanonicalReadError):
        reader.read_unassigned_message(context.scope, message.pk)


def test_unassigned_unknown_direction_does_not_ignore_exact_legacy_tombstone(context):
    message = unassigned(context, body="PRIVATE LEGACY SHADOW")
    InboxMessage.objects.create(
        workspace=context.account.workspace,
        social_account=context.account,
        platform_message_id=message.platform_message_id,
        message_type="dm",
        body="PRIVATE ORIGINAL",
        received_at=timezone.now(),
        extra={"message": {"is_deleted": True}},
    )
    assert reader.read_unassigned_message(context.scope, message.pk)["message"]["body"] == ""
    assert reader.list_unassigned_messages(context.scope, search="PRIVATE")["messages"] == []
