"""The saved UI/REST/MCP DTO describes evidence, never invented completeness."""

import json
from datetime import timedelta

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.inbox import canonical_reads as reader
from apps.inbox.models import (
    ConversationObservationState,
    ConversationReadState,
    ConversationSyncState,
    InboxConversation,
    InboxReply,
)
from apps.inbox.tests.test_canonical_adapters_rebuilt import client_for, rpc
from apps.inbox.tests.test_canonical_reads_rebuilt import context as _context
from apps.inbox.tests.test_canonical_reads_rebuilt import legacy, proof, row

context = _context
pytestmark = pytest.mark.django_db


def test_ui_rest_mcp_share_namespaced_persisted_evidence_without_read_writes(context):
    message = row(
        context,
        platform_message_id="native-message",
        conversation_type="direct",
        classification_reason="participants_pair",
        sources=["webhook", "poll", "poll"],
    )
    success = timezone.now() - timedelta(hours=3)
    attempt = timezone.now()
    ConversationSyncState.objects.create(
        workspace=context.account.workspace,
        social_account=context.account,
        platform=context.account.platform,
        stream="dm",
        status="failed",
        coverage="partial",
        last_success_at=success,
        last_attempt_at=attempt,
        last_error_code="PRIVATE PROVIDER DIAGNOSTIC",
    )
    with CaptureQueriesContext(connection) as queries:
        response = client_for(context).get(f"/api/v1/inbox-conversations/{context.conversation.pk}", secure=True)
        assert response.status_code == 200, response.content
        rest = response.json()
        mcp = rpc(context, "get_conversation_messages", {"conversation_id": str(context.conversation.pk)})
        listing = rpc(context, "list_conversations", {})
    writes = [item["sql"] for item in queries if item["sql"].lstrip().split()[0] in {"UPDATE", "INSERT", "DELETE"}]
    # Existing authentication bookkeeping is distinct from inbox/read/work state.
    assert all(
        sql.startswith(('UPDATE "api_keys_api_key" SET "last_used_at"', 'INSERT INTO "api_keys_audit_log"'))
        for sql in writes
    ), writes
    assert rest["messages"] == mcp["messages"]
    assert rest["conversation"] == mcp["conversation"] == listing["conversations"][0]
    assert rest["source"] == mcp["source"] == "canonical"
    assert rest["persisted"] is mcp["persisted"] is True
    assert "read_ack_token" not in mcp
    assert not ConversationReadState.objects.exists() and not InboxReply.objects.exists()
    conversation = rest["conversation"]
    assert conversation["id_namespace"] == "canonical_conversation"
    assert conversation["id"] == str(context.conversation.pk)
    assert conversation["platform_conversation_id"] == "synthetic-thread"
    assert conversation["identity_kind"] == "platform"
    assert conversation["participants_status"] == "pair_verified"
    assert conversation["sync"] == rest["coverage"]
    assert conversation["sync"]["scope"] == "account_dm_stream"
    assert conversation["sync"]["source"] == "legacy_poll_stream"
    assert conversation["sync"]["last_success_at"] == success.isoformat()
    assert conversation["sync"]["last_attempt_at"] == attempt.isoformat()
    assert conversation["sync"]["status"] == "failed"
    assert conversation["sync"]["conversation_freshness"] == "unknown"
    assert conversation["sync"]["history_complete"] is False
    projected = rest["messages"][0]
    assert projected["id_namespace"] == "canonical_message"
    assert projected["id"] == str(message.pk) and projected["conversation_id"] == conversation["id"]
    assert projected["platform_message_id"] == "native-message"
    assert projected["platform"] == context.account.platform
    assert projected["social_account_id"] == str(context.account.pk)
    assert projected["classification_reason"] == "participants_pair"
    assert projected["participants_status"] == "pair_verified"
    assert projected["sources"] == ["poll", "webhook"]
    assert projected["first_seen_at"] == message.first_seen_at.isoformat()
    assert projected["updated_at"] == message.updated_at.isoformat()
    assert projected["content_available"] is True and projected["content_completeness"] == "unknown"
    assert projected["media_fetched"] is projected["platform_media_complete"] is False
    assert "PRIVATE PROVIDER DIAGNOSTIC" not in json.dumps(rest)
    browser = Client()
    browser.force_login(context.user)
    ui = browser.get(
        reverse(
            "inbox:conversation_detail",
            kwargs={"workspace_id": context.account.workspace_id, "conversation_id": context.conversation.pk},
        ),
        secure=True,
    )
    assert ui.status_code == 200, ui.content
    assert ui.context["canonical_conversation"] == conversation
    assert {key: ui.context["canonical_messages"][0][key] for key in projected} == projected


@pytest.mark.parametrize(
    "kind,reason,status,projected_kind",
    [
        ("unknown", "participants_missing", "missing", "unknown"),
        ("unknown", "participants_incomplete", "incomplete", "unknown"),
        ("unknown", "participants_invalid", "invalid", "unknown"),
        ("unknown", "identity_conflict", "conflict", "unknown"),
        ("unknown", "participant_endpoints_conflict", "conflict", "unknown"),
        ("group", "participants_group", "group_observed", "group"),
        ("direct", "participants_missing", "missing", "unknown"),
        ("unknown", "participants_pair", "unknown", "unknown"),
    ],
)
def test_missing_or_conflicting_participants_never_become_direct(context, kind, reason, status, projected_kind):
    InboxConversation.objects.filter(pk=context.conversation.pk).update(
        conversation_type=kind, classification_reason=reason
    )
    row(context, conversation_type=kind, classification_reason=reason)
    value = reader.read_conversation(context.scope, context.conversation.pk)
    for projected in [value["conversation"], value["messages"][0]]:
        assert projected["conversation_type"] == projected_kind
        assert projected["classification_reason"] == reason
        assert projected["participants_status"] == status
        assert "participants" not in projected and "participant_ids" not in projected
    assert value["conversation"]["send_authorized"] is False
    assert value["conversation"]["peer_name"] != "peer"


def test_missing_provider_identity_and_sync_are_explicit_not_filled_from_legacy(context):
    InboxConversation.objects.filter(pk=context.conversation.pk).update(
        platform_conversation_id=None, identity_kind="verified_peer"
    )
    message = row(context, platform_message_id="", occurred_at=None, body="", sources=[])
    anchor = legacy(context, message)
    anchor.extra = {"conversation_id": "UNVERIFIED-LEGACY-THREAD", "participants": ["PRIVATE PARTICIPANT"]}
    anchor.save(update_fields=["extra"])
    ConversationSyncState.objects.create(
        workspace=context.account.workspace,
        social_account=context.account,
        platform=context.account.platform,
        stream="comment",
        status="success",
        last_success_at=timezone.now(),
    )
    value = reader.read_conversation(context.scope, context.conversation.pk)
    projected = value["undated_messages"][0]
    assert projected["platform_message_id"] is None
    assert projected["sources"] == [] and projected["timestamp_missing"] is True
    assert projected["content_available"] is False and projected["content_completeness"] == "unknown"
    assert projected["attachment_metadata_count"] == 0
    assert value["conversation"]["platform_conversation_id"] is None
    assert value["conversation"]["identity_kind"] == "verified_peer"
    assert value["coverage"]["status"] == value["coverage"]["coverage"] == "unknown"
    assert value["coverage"]["last_success_at"] is value["coverage"]["last_attempt_at"] is None
    assert "UNVERIFIED-LEGACY-THREAD" not in json.dumps(value) and "PRIVATE PARTICIPANT" not in json.dumps(value)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unrecognized_reason_and_ambiguous_pair_do_not_assert_direct(context, ambiguous):
    InboxConversation.objects.filter(pk=context.conversation.pk).update(
        peer_ambiguous=ambiguous, classification_reason="participants_pair" if ambiguous else "SECRET DIAGNOSTIC"
    )
    row(context)
    value = reader.read_conversation(context.scope, context.conversation.pk)["conversation"]
    assert value["conversation_type"] == "unknown" and value["send_authorized"] is False
    assert value["participants_status"] == ("conflict" if ambiguous else "unknown")
    assert "SECRET" not in json.dumps(value)


@pytest.mark.parametrize("legacy_id", [False, True])
def test_incoming_compatibility_uses_one_consistent_conversation_classification(context, legacy_id):
    message = row(context, direction="inbound", sender_id="peer")
    target = legacy(context, message) if legacy_id else message
    # The individual old observation has less evidence than the conversation.
    assert message.conversation_type == "unknown" and message.classification_reason == "participants_missing"
    for value in [
        client_for(context).get(f"/api/v1/inbox/{target.pk}", secure=True).json(),
        rpc(context, "get_inbox_message", {"message_id": str(target.pk)}),
    ]:
        assert value["conversation_type"] == "direct" and value["classification_reason"] == "participants_pair"
        assert value["participants_status"] == "pair_verified"
        assert value["id"] == str(target.pk)
        assert value["id_namespace"] == ("inbox_message" if legacy_id else "canonical_message")
        assert value["canonical_conversation_id"] == str(context.conversation.pk)
        assert value["reply_eligibility"]["allowed"] is False


@pytest.mark.parametrize("restriction", ["withdrawn", "expired", "legacy_withdrawn"])
def test_restricted_and_retained_content_cannot_claim_available_or_complete(context, restriction):
    message = row(context, direction="inbound", sender_id="peer", body="PRIVATE BODY")
    anchor = legacy(context, message)
    proof(context, message, retained_body="PRIVATE RETAINED BODY", retained_attachments=[{"url": "PRIVATE MEDIA"}])
    if restriction == "legacy_withdrawn":
        anchor.extra = {"is_deleted": True}
        anchor.save(update_fields=["extra"])
    else:
        ConversationObservationState.objects.filter(message=message).update(**{restriction + "_at": timezone.now()})
    for value in [
        client_for(context).get(f"/api/v1/inbox-conversations/{context.conversation.pk}", secure=True).json(),
        rpc(context, "get_conversation_messages", {"conversation_id": str(context.conversation.pk)}),
    ]:
        projected = value["messages"][0]
        assert projected["content_available"] is False and projected["content_completeness"] == "unavailable"
        assert projected["body"] == "" and projected["attachments"] == []
        assert projected["attachment_metadata_count"] == 0
        assert projected["platform_media_complete"] is False
        assert "PRIVATE" not in json.dumps(value)


@pytest.mark.parametrize("sources", [["poll", "SECRET SOURCE", {"access_token": "SECRET"}], "SECRET", None])
def test_sources_are_bounded_labels_not_raw_provider_metadata(context, sources):
    message = row(context)
    # Project malformed historical JSON without requiring a schema/data rewrite.
    message.sources = sources
    projected = reader.project_message(message)
    assert projected["sources"] == (["poll"] if isinstance(sources, list) else [])
    assert "SECRET" not in json.dumps(projected)


@pytest.mark.parametrize("status", ["partial", "fields_unavailable", "unsupported"])
def test_provider_content_gaps_remain_partial_even_with_readable_text(context, status):
    message = row(context, content_status=status, body="Known partial text")
    projected = reader.project_message(message)
    assert projected["content_status"] == status
    assert projected["content_completeness"] == "partial"
    assert projected["attachment_metadata_count"] == 0 and projected["platform_media_complete"] is False


def test_bounded_preview_reports_partial_without_leaking_attachment_metadata(context):
    message = row(
        context,
        body="中" * 20000,
        attachments=[
            {
                "type": "image",
                "url": f"https://example.com/image-{index}.jpg",
                "access_token": "SECRET",
                "participants": ["PRIVATE"],
            }
            for index in range(5)
        ],
    )
    projected = reader.project_message(message)
    assert projected["body_truncated"] is projected["attachments_truncated"] is True
    assert projected["content_completeness"] == "partial" and projected["attachment_metadata_count"] == 5
    assert reader._size(projected) <= 10000
    assert "SECRET" not in json.dumps(projected) and "PRIVATE" not in json.dumps(projected)
