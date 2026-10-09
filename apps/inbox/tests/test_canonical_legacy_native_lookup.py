"""Unlinked legacy IDs may read only the same proven native incoming message."""

import json
from copy import copy
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.inbox import canonical_compat, canonical_reads
from apps.inbox.models import (
    ConversationMessage,
    ConversationObservationState,
    ConversationReadState,
    ConversationSyncIdentity,
    InboxConversation,
    InboxMessage,
)
from apps.inbox.tests.test_canonical_adapters_rebuilt import client_for, incoming, rpc
from apps.inbox.tests.test_canonical_reads_rebuilt import context as _context
from apps.inbox.tests.test_canonical_reads_rebuilt import legacy, proof
from apps.mcp.handlers import _get_inbox_message
from apps.mcp.protocol import JsonRpcError
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db
context = _context


@pytest.fixture
def unlinked(context):
    message = incoming(context, body="Canonical incoming text")
    anchor = legacy(context, message)
    InboxMessage.objects.filter(pk=anchor.pk).update(status="archived")
    ConversationMessage.objects.filter(pk=message.pk).update(legacy_message=None)
    message.refresh_from_db()
    connection = proof(context, message)
    return message, anchor, connection


def read(context, anchor):
    result = _get_inbox_message(
        {"message_id": str(anchor.pk)},
        {"api_key": context.key.api_key, "membership": context.member},
    )
    return json.loads(result["content"][0]["text"])


def test_exact_native_identity_reads_existing_dto_without_writes(context, unlinked):
    message, anchor, sync = unlinked
    before = list(type(sync).objects.values())
    conversation_before = list(InboxConversation.objects.values())
    with CaptureQueriesContext(connection) as queries:
        result = read(context, anchor)
        assert canonical_reads.resolve_legacy_conversation(context.scope, anchor.pk) == context.conversation.pk
    assert not any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries)
    assert result["id"] == str(anchor.pk) and result["id_namespace"] == "inbox_message"
    assert result["body"] == message.body and result["status"] == "archived"
    assert result["canonical_conversation_id"] == str(context.conversation.pk)
    assert result["reply_eligibility"]["allowed"] is False and result["replies"] == []
    message.refresh_from_db()
    assert message.legacy_message_id is None
    assert not ConversationReadState.objects.exists() and InboxMessage.objects.count() == 1
    assert list(type(sync).objects.values()) == before
    assert list(InboxConversation.objects.values()) == conversation_before


def test_rest_mcp_and_reply_context_share_the_native_identity_bridge(context, unlinked):
    message, anchor, _sync = unlinked
    rest = client_for(context).get(f"/api/v1/inbox/{anchor.pk}", secure=True)
    assert rest.status_code == 200, rest.content
    assert rest.json()["body"] == message.body
    assert rpc(context, "get_inbox_message", {"message_id": str(anchor.pk)})["body"] == message.body
    result = rpc(context, "get_reply_context", {"message_id": str(anchor.pk)})
    assert result["conversation"]["id"] == str(context.conversation.pk)
    assert "read_ack_token" not in result
    result = rpc(context, "get_inbox_thread", {"message_id": str(anchor.pk)})
    assert result["error"]["data"]["error"] == "canonical_upgrade_required"


@pytest.mark.parametrize("fault", ["native_id", "empty_id", "direction", "unassigned", "platform", "linked_elsewhere"])
def test_same_content_and_time_never_replace_exact_incoming_identity(context, unlinked, fault):
    message, anchor, _sync = unlinked
    if fault == "linked_elsewhere":
        other_anchor = InboxMessage.objects.create(
            workspace=context.account.workspace,
            social_account=context.account,
            platform_message_id="other-legacy-native-id",
            message_type="dm",
            received_at=message.occurred_at,
        )
        changes = {"legacy_message": other_anchor}
    else:
        changes = {
            "native_id": {"platform_message_id": "different-native-id"},
            "empty_id": {"platform_message_id": ""},
            "direction": {"direction": "outbound"},
            "unassigned": {"conversation": None},
            "platform": {"platform": "instagram_login"},
        }[fault]
    ConversationMessage.objects.filter(pk=message.pk).update(**changes)
    if fault == "empty_id":
        InboxMessage.objects.filter(pk=anchor.pk).update(platform_message_id="")
    with pytest.raises(JsonRpcError) as error:
        read(context, anchor)
    assert error.value.data["error"] == "canonical_unavailable"


def test_existing_conflicting_link_cannot_be_bypassed_by_native_match(context, unlinked):
    _message, anchor, _sync = unlinked
    conflict = incoming(context, body="Conflicting identity", legacy_message=anchor)
    proof(context, conflict)
    with pytest.raises(JsonRpcError) as error:
        read(context, anchor)
    assert error.value.data["error"] == "canonical_unavailable"


@pytest.mark.parametrize("foreign_workspace", [False, True])
def test_native_id_collision_cannot_cross_account_or_workspace(
    context, unlinked, enroll_conversation_accounts, foreign_workspace
):
    message, anchor, _sync = unlinked
    workspace = (
        Workspace.objects.create(organization=context.account.workspace.organization, name="Other workspace")
        if foreign_workspace
        else context.account.workspace
    )
    foreign = copy(context)
    foreign.account = SocialAccount.objects.create(
        workspace=workspace,
        platform=context.account.platform,
        account_platform_id="other-own-account",
        account_name="Other account",
    )
    enroll_conversation_accounts(foreign.account, read=True)
    context.key.api_key.social_accounts.add(foreign.account)
    foreign.conversation = InboxConversation.objects.create(
        workspace=workspace,
        social_account=foreign.account,
        platform=foreign.account.platform,
        platform_conversation_id="other-native-thread",
        identity_kind="platform",
    )
    collision = incoming(foreign, platform_message_id=message.platform_message_id, body="Foreign private body")
    proof(foreign, collision)
    assert read(context, anchor)["body"] == message.body
    ConversationMessage.objects.filter(pk=message.pk).update(platform_message_id="local-no-longer-matching")
    with pytest.raises(JsonRpcError) as error:
        read(context, anchor)
    assert error.value.data["error"] == "canonical_unavailable"
    assert "Foreign private body" not in str(error.value)


@pytest.mark.parametrize(
    "fault",
    [
        "observation_generation",
        "conversation_generation",
        "connection_generation",
        "native_account",
        "read_enrollment",
        "canonical_flag",
    ],
)
def test_revoked_provenance_or_read_scope_never_reopens_legacy_content(context, unlinked, settings, fault):
    message, anchor, sync = unlinked
    if fault == "observation_generation":
        ConversationObservationState.objects.filter(message=message).update(connection_generation=uuid4())
    elif fault == "conversation_generation":
        ConversationSyncIdentity.objects.filter(conversation=context.conversation).update(connection_generation=uuid4())
    elif fault == "connection_generation":
        type(sync).objects.filter(pk=sync.pk).update(generation=uuid4())
    elif fault == "native_account":
        SocialAccount.objects.filter(pk=context.account.pk).update(account_platform_id="different-native-account")
    elif fault == "read_enrollment":
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    else:
        settings.INBOX_CANONICAL_READ_ENABLED = False
    with pytest.raises(JsonRpcError) as error:
        read(context, anchor)
    assert "Canonical incoming text" not in str(error.value)
    assert "STALE LEGACY BODY" not in str(error.value)


def test_unlinked_native_match_requires_saved_provenance(context):
    message = incoming(context)
    anchor = legacy(context, message)
    ConversationMessage.objects.filter(pk=message.pk).update(legacy_message=None)
    with pytest.raises(JsonRpcError) as error:
        read(context, anchor)
    assert error.value.data["error"] == "canonical_unavailable"


@pytest.mark.parametrize("source", ["canonical_withdrawn", "canonical_expired", "legacy_withdrawn", "legacy_expired"])
def test_exact_match_keeps_all_saved_content_restrictions(context, unlinked, source):
    message, anchor, _sync = unlinked
    if source.startswith("canonical"):
        field = "withdrawn_at" if source.endswith("withdrawn") else "expired_at"
        ConversationObservationState.objects.filter(message=message).update(**{field: timezone.now()})
    else:
        extra = {"is_deleted": True} if source.endswith("withdrawn") else {"inbox_content_status": "expired"}
        InboxMessage.objects.filter(pk=anchor.pk).update(extra=extra)
    result = read(context, anchor)
    assert result["body"] == ""
    assert "STALE LEGACY BODY" not in json.dumps(result)


def test_group_identity_is_preserved_without_direct_inference(context, unlinked):
    _message, anchor, _sync = unlinked
    InboxConversation.objects.filter(pk=context.conversation.pk).update(
        conversation_type="group", classification_reason="participants_group", peer_id=""
    )
    result = read(context, anchor)
    assert result["conversation_type"] == "group" and result["participants_status"] == "group_observed"
    assert result["reply_eligibility"]["allowed"] is False


@pytest.mark.parametrize("fault", ["legacy_identity", "competing_link", "generation"])
def test_identity_is_rechecked_after_projection(context, unlinked, fault):
    message, anchor, sync = unlinked
    competitor = incoming(context)
    proof(context, competitor)
    original = canonical_compat._metadata

    def mutate(*args):
        result = original(*args)
        if fault == "legacy_identity":
            InboxMessage.objects.filter(pk=anchor.pk).update(platform_message_id="changed-during-read")
        elif fault == "competing_link":
            ConversationMessage.objects.filter(pk=competitor.pk).update(legacy_message=anchor)
        else:
            type(sync).objects.filter(pk=sync.pk).update(generation=uuid4())
        return result

    with patch.object(canonical_compat, "_metadata", side_effect=mutate), pytest.raises(JsonRpcError) as error:
        read(context, anchor)
    assert error.value.data["error"] in {"stale_revision", "stale_scope"}
    message.refresh_from_db()
    assert message.legacy_message_id is None
