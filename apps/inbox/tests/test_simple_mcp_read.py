"""One existing MCP read call, one reference, no account/source choreography."""

import json
from unittest.mock import patch

import pytest

from apps.inbox.models import ConversationReadState, InboxMessage, InboxReply
from apps.inbox.tests.test_inbox_intents import composer as composer
from apps.inbox.tests.test_inbox_intents import intent_context as intent_context
from apps.inbox.tests.test_inbox_intents import surface as surface
from apps.inbox.tests.test_unified_feed import account, legacy
from apps.mcp.protocol import JsonRpcError
from apps.mcp.saved_inbox_tools import _read_intent
from apps.members.models import WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def read(key, reference, **optional):
    member = WorkspaceMembership.objects.get(user_id=key.issued_by_id, workspace_id=key.workspace_id)
    result = _read_intent({"target_id": str(reference), **optional}, {"api_key": key, "membership": member})
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("reference", ["conversation_uuid", "message_uuid", "native_conversation", "native_message"])
def test_one_id_returns_account_thread_reply_state_and_honest_coverage(intent_context, reference):
    c, _client, key = intent_context
    value = {
        "conversation_uuid": c.conversation.pk,
        "message_uuid": c.row.pk,
        "native_conversation": c.conversation.platform_conversation_id,
        "native_message": c.row.platform_message_id,
    }[reference]
    before = (
        InboxReply.objects.count(),
        InboxMessage.objects.count(),
        ConversationReadState.objects.count(),
        c.conversation.workflow_state,
    )
    with (
        patch("apps.inbox.native_thread_reads.read_native_thread") as native,
        patch("apps.inbox.services._dispatch_to_platform") as send,
    ):
        result = read(key, value)
    assert result["account"]["id"] == str(c.account.pk)
    assert result["thread"]["id"] == str(c.conversation.pk)
    assert result["conversation"]["messages"][0]["id"] == str(c.row.pk)
    assert result["reply_status"]["advisory_only"] and not result["reply_status"]["send_authorized"]
    assert not result["history"]["complete"] and result["history"]["limitations"]
    c.conversation.refresh_from_db()
    assert (
        InboxReply.objects.count(),
        InboxMessage.objects.count(),
        ConversationReadState.objects.count(),
        c.conversation.workflow_state,
    ) == before
    native.assert_not_called()
    send.assert_not_called()


def test_native_reference_shared_by_allowed_brands_is_explicitly_ambiguous(intent_context):
    c, _client, key = intent_context
    other = account(c, connection_status="connected")
    legacy(other, platform_message_id=c.row.platform_message_id)
    key.social_accounts.add(other)
    with pytest.raises(JsonRpcError) as error:
        read(key, c.row.platform_message_id)
    assert error.value.data["error"] == "ambiguous_reference"


def test_outside_allowlist_does_not_confuse_a_valid_native_reference(intent_context):
    c, _client, key = intent_context
    other = account(c, connection_status="connected")
    legacy(other, platform_message_id=c.row.platform_message_id)
    assert read(key, c.row.platform_message_id)["account"]["id"] == str(c.account.pk)


def test_legacy_reference_returns_existing_replies_without_account_lookup(intent_context):
    c, _client, key = intent_context
    other = account(c, connection_status="connected")
    message = legacy(other, platform_message_id="synthetic-comment-ref")
    key.social_accounts.add(other)
    receipt = InboxReply.objects.create(
        inbox_message=message, body="Synthetic existing reply", status="sent", platform_reply_id="accepted"
    )
    result = read(key, message.platform_message_id)
    assert result["account"]["id"] == str(other.pk)
    assert result["existing_replies"][0]["id"] == str(receipt.pk)
    assert result["existing_replies"][0]["body"] == receipt.body
    assert result["history"]["coverage"]["outbound_coverage"] == "brightbean_replies_only"
    assert not result["history"]["complete"]


def test_outbound_message_id_also_reads_its_original_conversation(intent_context):
    from apps.inbox.tests.test_canonical_reads_rebuilt import proof, row

    c, _client, key = intent_context
    outgoing = row(c, platform_message_id="synthetic-native-outgoing", body="Synthetic platform-side reply")
    proof(c, outgoing)
    result = read(key, outgoing.pk)
    assert result["thread"]["id"] == str(c.conversation.pk)
    assert any(
        item["id"] == str(outgoing.pk) and item["direction"] == "outbound"
        for item in result["conversation"]["messages"]
    )


def test_older_saved_content_only_needs_same_reference_and_returned_cursor(intent_context):
    from apps.inbox.tests.test_canonical_reads_rebuilt import proof, row

    c, _client, key = intent_context
    outgoing = row(c, body="Synthetic newer reply")
    proof(c, outgoing)
    first = read(key, c.conversation.platform_conversation_id, limit=1)
    assert first["history"]["next_cursor"]
    older = read(key, c.conversation.platform_conversation_id, cursor=first["history"]["next_cursor"], limit=1)
    ids = [item["id"] for page in (first, older) for item in page["conversation"]["messages"]]
    assert len(ids) == len(set(ids)) == 2
    assert not older["history"]["complete"] and older["reference"] is None


def test_mid_read_account_removal_discards_the_response(intent_context):
    from apps.inbox import inbox_intents

    c, _client, key = intent_context
    original = inbox_intents._read_summary

    def revoke(*args):
        result = original(*args)
        key.social_accounts.clear()
        return result

    with patch("apps.inbox.inbox_intents._read_summary", side_effect=revoke), pytest.raises(JsonRpcError):
        read(key, c.conversation.pk)


def test_read_schema_has_only_one_required_argument():
    from apps.inbox.inbox_intents import InboxReadInput

    assert InboxReadInput.model_json_schema()["required"] == ["target_id"]


def test_canonical_summary_includes_old_linked_receipt_without_raw_body_fallback(intent_context):
    from apps.inbox.tests.test_canonical_reads_rebuilt import legacy as old_anchor
    from apps.inbox.tests.test_canonical_reads_rebuilt import proof, row

    c, _client, key = intent_context
    anchor = old_anchor(c, c.row)
    receipt = InboxReply.objects.create(
        inbox_message=anchor, status="sent", body="PRIVATE_RAW_RECEIPT_BODY", platform_reply_id="synthetic-old-sent"
    )
    outgoing = row(c, platform_message_id="synthetic-old-sent", body="Canonical visible reply", legacy_reply=receipt)
    proof(c, outgoing)
    result = read(key, c.row.pk)
    assert result["existing_replies"][0]["id"] == str(receipt.pk)
    assert result["existing_replies"][0]["status"] == "sent"
    assert "Canonical visible reply" in json.dumps(result)
    assert "PRIVATE_RAW_RECEIPT_BODY" not in json.dumps(result)


def test_old_message_reference_includes_anchor_even_when_latest_page_is_newer(intent_context):
    from apps.inbox.tests.test_canonical_reads_rebuilt import proof, row

    c, _client, key = intent_context
    outgoing = row(c, body="Synthetic much newer reply")
    proof(c, outgoing)
    result = read(key, c.row.pk, limit=1)
    assert result["anchor_message"]["id"] == str(c.row.pk)
    assert result["conversation"]["messages"][0]["id"] == str(outgoing.pk)
    assert "composer_observation_token" not in result["conversation"]


def test_legacy_message_uuid_is_converted_inside_read_and_keeps_canonical_anchor(intent_context):
    from apps.inbox.tests.test_canonical_reads_rebuilt import legacy as old_anchor

    c, _client, key = intent_context
    message = old_anchor(c, c.row)
    result = read(key, message.pk)
    assert result["target_id"] == str(c.conversation.pk)
    assert result["anchor_message"]["id"] == str(c.row.pk)
    assert "STALE LEGACY BODY" not in json.dumps(result)


def test_withdrawn_anchor_never_uses_retained_body_as_read_fallback(intent_context):
    c, _client, key = intent_context
    c.row.body = "PRIVATE_WITHDRAWN_BODY"
    c.row.is_deleted = True
    c.row.save(update_fields=["body", "is_deleted"])
    result = read(key, c.row.pk)
    assert result["anchor_message"]["is_deleted"]
    assert "PRIVATE_WITHDRAWN_BODY" not in json.dumps(result)


def test_many_legacy_messages_in_one_native_thread_are_deduplicated_as_one_account(intent_context):
    c, _client, key = intent_context
    other = account(c, connection_status="connected")
    key.social_accounts.add(other)
    for _ in range(4):
        legacy(other, kind="dm", extra={"conversation_id": "synthetic-legacy-thread"})
    result = read(key, "synthetic-legacy-thread")
    assert result["account"]["id"] == str(other.pk)
    assert len(result["conversation"]["messages"]) == 4
    assert not result["history"]["complete"]


def test_native_alias_uses_existing_account_source_without_caller_probing(intent_context):
    from apps.inbox.models import InboxConversation

    c, _client, key = intent_context
    other = account(c, connection_status="connected")
    key.social_accounts.add(other)
    legacy(other, kind="dm", extra={"conversation_id": "synthetic-not-cut-over"})
    InboxConversation.objects.create(
        workspace=other.workspace,
        social_account=other,
        platform=other.platform,
        identity_kind="platform",
        platform_conversation_id="synthetic-not-cut-over",
        conversation_type="unknown",
        classification_reason="participants_missing",
    )
    result = read(key, "synthetic-not-cut-over")
    assert result["account"]["id"] == str(other.pk)
    assert result["conversation"]["outbound_coverage"] == "brightbean_replies_only"
