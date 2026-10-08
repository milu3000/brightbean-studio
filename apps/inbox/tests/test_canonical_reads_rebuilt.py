"""Fresh offline verification of the reconstructed canonical reader."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db.models import F
from django.utils import timezone

from apps.api_keys.services import issue_api_key
from apps.inbox import canonical_reads as reader
from apps.inbox.canonical_content import visible_content
from apps.inbox.models import (
    ConversationMessage,
    ConversationObservationState,
    ConversationReadState,
    ConversationSyncIdentity,
    InboxArchiveIdentity,
    InboxConversation,
    InboxMessage,
    InboxReply,
    InboxSyncConnection,
)
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount

pytestmark = pytest.mark.django_db


@pytest.fixture
def context(settings, inbox_account, user, org_owner, enroll_conversation_accounts):
    settings.INBOX_CANONICAL_READ_ENABLED = True
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    enroll_conversation_accounts(inbox_account, read=True)
    member = WorkspaceMembership.objects.create(user=user, workspace=inbox_account.workspace, workspace_role="owner")
    conversation = InboxConversation.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform=inbox_account.platform,
        identity_kind="platform",
        platform_conversation_id="synthetic-thread",
        peer_id="peer",
        conversation_type="direct",
        classification_reason="participants_pair",
    )
    key = issue_api_key(
        workspace=inbox_account.workspace,
        social_accounts=[inbox_account],
        issued_by=user,
        name="Synthetic reader",
        permissions=["use_inbox"],
    )
    return SimpleNamespace(
        account=inbox_account,
        conversation=conversation,
        member=member,
        user=user,
        key=key,
        scope=reader.session_read_scope(user, inbox_account.workspace_id),
    )


def row(context, *, occurred_at="now", **overrides):
    values = dict(
        workspace=context.account.workspace,
        social_account=context.account,
        platform=context.account.platform,
        conversation=context.conversation,
        platform_message_id=str(uuid4()),
        direction="outbound",
        sender_id=context.account.account_platform_id,
        sender_name="Own account",
        body="Canonical own message",
        occurred_at=timezone.now() if occurred_at == "now" else occurred_at,
    )
    values.update(overrides)
    message = ConversationMessage.objects.create(**values)
    InboxConversation.objects.filter(pk=message.conversation_id).update(revision=F("revision") + 1)
    return message


def legacy(context, message):
    record = InboxMessage.objects.create(
        workspace=context.account.workspace,
        social_account=context.account,
        platform_message_id=message.platform_message_id,
        message_type="dm",
        sender_name="Legacy peer",
        body="STALE LEGACY BODY",
        received_at=message.occurred_at or timezone.now(),
        extra={"conversation_id": "synthetic-thread"},
    )
    message.legacy_message = record
    message.save(update_fields=["legacy_message", "updated_at"])
    return record


def proof(context, message, **overrides):
    connection, _ = InboxSyncConnection.objects.get_or_create(
        social_account=context.account,
        defaults={
            "workspace": context.account.workspace,
            "platform": context.account.platform,
            "account_platform_id": context.account.account_platform_id,
            "webhook_target_id": context.account.webhook_target_id,
            "auth_fingerprint": "synthetic-request-fence",
            "enabled": False,
        },
    )
    ConversationSyncIdentity.objects.get_or_create(
        conversation=context.conversation,
        defaults={"connection": connection, "connection_generation": connection.generation},
    )
    state = {
        "connection_generation": connection.generation,
        "content_fingerprint": "synthetic",
        "last_observed_at": timezone.now(),
        "expires_at": timezone.now() + timedelta(days=150),
    }
    state.update(overrides)
    ConversationObservationState.objects.create(message=message, **state)
    return connection


def test_outgoing_only_search_and_read_use_one_saved_source_without_anchor(context):
    message = row(context, body="Outbound unique needle")
    assert reader.list_conversations(context.scope, search="unique needle")["conversations"][0]["id"] == str(
        context.conversation.pk
    )
    page = reader.read_conversation(context.scope, context.conversation.pk)
    assert page["messages"][0]["id"] == str(message.pk)
    assert page["conversation"]["legacy_anchor_id"] is None
    assert page["conversation"]["send_authorized"] is False
    assert not InboxMessage.objects.exists() and not ConversationReadState.objects.exists()


def test_known_time_keyset_and_undated_lane_are_distinct(context):
    now = timezone.now()
    old = row(context, occurred_at=now - timedelta(days=2))
    first, second = row(context, occurred_at=now), row(context, occurred_at=now)
    unknown = [row(context, occurred_at=None) for _ in range(3)]
    page = reader.read_conversation(context.scope, context.conversation.pk, limit=2)
    assert [item["id"] for item in page["messages"]] == sorted([str(first.pk), str(second.pk)])
    older = reader.read_conversation(context.scope, context.conversation.pk, limit=2, cursor=page["next_cursor"])
    assert [item["id"] for item in older["messages"]] == [str(old.pk)]
    tail = reader.read_conversation(context.scope, context.conversation.pk, limit=2, cursor=page["undated_next_cursor"])
    assert tail["messages"] == []
    assert {item["id"] for item in page["undated_messages"] + tail["undated_messages"]} == {
        str(item.pk) for item in unknown
    }
    assert all(item["occurred_at"] is None for item in tail["undated_messages"])


@pytest.mark.parametrize("search", ["inbound phrase", "outbound phrase", "Peer Name", "Page", "peer"])
def test_search_covers_both_directions_and_safe_names(context, search):
    row(context, body="outbound phrase")
    row(context, direction="inbound", sender_id="peer", sender_name="Peer Name", body="inbound phrase")
    assert len(reader.list_conversations(context.scope, search=search)["conversations"]) == 1


@pytest.mark.parametrize("fault", ["withdrawn", "expired", "generation", "native"])
def test_shared_content_and_all_canonical_projections_reject_restricted_body(context, fault):
    message = row(
        context, body="SECRET CAPTURE", attachments=[{"type": "image", "url": "https://example.com/private.jpg"}]
    )
    proof(context, message, retained_body="PRIVATE ARCHIVE")
    if fault == "withdrawn":
        ConversationObservationState.objects.filter(message=message).update(withdrawn_at=timezone.now())
    elif fault == "expired":
        ConversationObservationState.objects.filter(message=message).update(
            expired_at=timezone.now() - timedelta(seconds=1)
        )
    elif fault == "generation":
        ConversationObservationState.objects.filter(message=message).update(connection_generation=uuid4())
    else:
        SocialAccount.objects.filter(pk=context.account.pk).update(account_platform_id="another-native")
    content = visible_content(message)
    assert content["body"] == "" and content["attachments"] == [] and content["available"] is False
    if fault == "native":
        with pytest.raises(reader.CanonicalReadError):
            reader.read_conversation(context.scope, context.conversation.pk)
        return
    page = reader.read_conversation(context.scope, context.conversation.pk)
    assert "SECRET" not in json.dumps(page) and "private.jpg" not in json.dumps(page)
    assert not reader.list_conversations(context.scope, search="SECRET")["conversations"]
    if fault != "generation":
        assert reader.read_message_attachments(context.scope, message.pk)["items"] == []
    message.refresh_from_db()
    assert message.body == "SECRET CAPTURE"


@pytest.mark.parametrize(
    "field", ["social_account_id", "workspace_id", "platform", "conversation_id", "platform_message_id"]
)
def test_fresh_content_lookup_pins_the_original_authorized_source(context, field):
    message = row(context, body="Original body")
    values = {"body": "REBOUND PRIVATE BODY"}
    if field == "social_account_id":
        account = SocialAccount.objects.create(
            workspace=context.account.workspace,
            platform=context.account.platform,
            account_platform_id="other",
            account_name="Other",
        )
        values[field] = account.pk
    elif field == "workspace_id":
        from apps.workspaces.models import Workspace

        values[field] = Workspace.objects.create(organization=context.account.workspace.organization, name="Other").pk
    elif field == "platform":
        values[field] = "instagram_login"
    elif field == "conversation_id":
        values[field] = None
    else:
        values[field] = "different-native-message"
    ConversationMessage.objects.filter(pk=message.pk).update(**values)
    assert visible_content(message)["body"] == ""


@pytest.mark.parametrize(
    "change", ["membership", "active_user", "enrollment", "allowlist", "key_revoke", "key_credential", "account_native"]
)
def test_actor_and_account_grants_are_rechecked_after_reading(context, settings, change):
    row(context)
    scope = reader.key_read_scope(context.key.api_key)
    original, changed = reader.project_message, False

    def mutate(message):
        nonlocal changed
        if not changed:
            changed = True
            if change == "membership":
                context.member.delete()
            elif change == "active_user":
                type(context.user).objects.filter(pk=context.user.pk).update(is_active=False)
            elif change == "enrollment":
                settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
            elif change == "allowlist":
                context.key.api_key.social_accounts.clear()
            elif change == "key_revoke":
                type(context.key.api_key).objects.filter(pk=context.key.api_key.pk).update(revoked_at=timezone.now())
            elif change == "key_credential":
                type(context.key.api_key).objects.filter(pk=context.key.api_key.pk).update(token_hash="replaced")
            else:
                SocialAccount.objects.filter(pk=context.account.pk).update(account_platform_id="replacement")
        return original(message)

    with patch.object(reader, "project_message", side_effect=mutate), pytest.raises(reader.CanonicalReadError):
        reader.read_conversation(scope, context.conversation.pk)


def test_token_rotation_is_not_a_saved_history_permission(context):
    old, newer = row(context), row(context)
    proof(context, old)
    proof(context, newer)
    page = reader.read_conversation(context.scope, context.conversation.pk, limit=1)
    context.account.oauth_access_token = "synthetic-refreshed"
    context.account.save(update_fields=["oauth_access_token"])
    assert reader.read_conversation(context.scope, context.conversation.pk, limit=1, cursor=page["next_cursor"])[
        "messages"
    ][0]["id"] == str(old.pk)


def test_unknown_model_content_status_is_publicly_normalized(context):
    message = row(context, content_status="unknown", body="Known text")
    assert visible_content(message)["content_status"] == "text"


@pytest.mark.parametrize("kind", ["group", "unknown"])
def test_non_direct_threads_are_readable_but_do_not_gain_send_authorization(context, kind):
    row(context)
    InboxConversation.objects.filter(pk=context.conversation.pk).update(conversation_type=kind, peer_id="")
    conversation = reader.read_conversation(context.scope, context.conversation.pk)["conversation"]
    assert conversation["conversation_type"] == kind and conversation["send_authorized"] is False


def test_history_cursor_pins_scope_and_page_size_but_survives_new_activity(context):
    row(context)
    row(context)
    cursor = reader.read_conversation(context.scope, context.conversation.pk, limit=1)["next_cursor"]
    with pytest.raises(reader.CanonicalReadError):
        reader.read_conversation(context.scope, context.conversation.pk, limit=2, cursor=cursor)
    newest = row(context)
    older = reader.read_conversation(context.scope, context.conversation.pk, limit=1, cursor=cursor)
    assert older["messages"] and all(item["id"] != str(newest.pk) for item in older["messages"])
    InboxConversation.objects.filter(pk=context.conversation.pk).update(platform_conversation_id="different-thread")
    with pytest.raises(reader.CanonicalReadError):
        reader.read_conversation(context.scope, context.conversation.pk, limit=1, cursor=cursor)


def test_legacy_shadow_outgoing_reply_link_cannot_import_other_account_content(context):
    other = SocialAccount.objects.create(
        workspace=context.account.workspace,
        platform=context.account.platform,
        account_platform_id="other-own",
        account_name="Other",
    )
    foreign = InboxMessage.objects.create(
        workspace=context.account.workspace,
        social_account=other,
        platform_message_id="foreign",
        message_type="dm",
        sender_name="Peer",
        body="Foreign",
        received_at=timezone.now(),
    )
    reply = InboxReply.objects.create(inbox_message=foreign, body="Foreign", status="sent")
    message = row(context, body="FOREIGN BODY", legacy_reply=reply)
    assert visible_content(message)["body"] == ""
    assert reader.read_conversation(context.scope, context.conversation.pk)["messages"] == []
    assert not reader.list_conversations(context.scope, search="FOREIGN")["conversations"]


def test_archived_disconnected_history_uses_proof_without_capture_grants(context, settings):
    message = row(context)
    connection = proof(context, message)
    InboxArchiveIdentity.objects.create(
        social_account=context.account,
        workspace=context.account.workspace,
        platform=context.account.platform,
        account_platform_id=context.account.account_platform_id,
        archived_connection=connection,
        connection_generation=connection.generation,
    )
    context.account.connection_status, context.account.oauth_access_token = "disconnected", ""
    context.account.save(update_fields=["connection_status", "oauth_access_token"])
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    page = reader.read_conversation(context.scope, context.conversation.pk)
    assert page["conversation"]["archived"] is True and page["messages"][0]["body"] == message.body


def test_read_response_bounds_disclose_truncation_and_keep_a_cursor(context):
    for _ in range(10):
        row(context, body="中" * 20000)
    result = reader.read_conversation(context.scope, context.conversation.pk, limit=100)
    assert len(json.dumps(result)) < 60000 and result["next_cursor"]
    assert all(item["body_truncated"] for item in result["messages"])


def test_deferred_identity_cannot_adopt_current_scope(context):
    message = row(context, body="PRIVATE NEW SCOPE")
    deferred = ConversationMessage.objects.only("pk").get(pk=message.pk)
    assert visible_content(deferred)["body"] == ""
    assert visible_content(deferred)["available"] is False


@pytest.fixture
def relocated_receipt(context):
    from copy import copy

    inbound = row(context, direction="inbound", sender_id="peer", recipient_id=context.account.account_platform_id)
    anchor = legacy(context, inbound)
    connection = proof(context, inbound)
    reply = InboxReply.objects.create(
        inbox_message=anchor,
        conversation=context.conversation,
        action_nonce=uuid4(),
        account_platform_id=context.account.account_platform_id,
        recipient_id="peer",
        platform_conversation_id="synthetic-thread",
        connection_generation=connection.generation,
        body="Accepted native text",
        status="sent",
        send_generation=1,
        sent_at=timezone.now(),
        platform_reply_id="sent-native-id",
    )
    second = InboxConversation.objects.create(
        workspace=context.account.workspace,
        social_account=context.account,
        platform=context.account.platform,
        platform_conversation_id="second-native-thread",
        peer_id="peer",
        conversation_type="direct",
        identity_kind="platform",
    )
    moved_context = copy(context)
    moved_context.conversation = second
    outgoing = row(
        moved_context,
        platform_message_id="sent-native-id",
        sender_id=context.account.account_platform_id,
        recipient_id="peer",
        direction="outbound",
        body="Accepted native text",
        legacy_reply=reply,
        conversation_type="direct",
        conversation_attribution="platform",
        delivery_status="observed",
    )
    proof(moved_context, outgoing)
    return outgoing, reply, second, connection


def test_exact_native_receipt_placement_is_readable_without_mutating_send_intent(context, relocated_receipt):
    from apps.inbox.canonical_content import native_receipt_relocation_allowed

    outgoing, reply, second, _connection = relocated_receipt
    assert native_receipt_relocation_allowed(outgoing, reply)
    assert visible_content(outgoing)["body"] == "Accepted native text"
    value = reader.read_conversation(context.scope, second.pk)
    assert value["messages"][0]["id"] == str(outgoing.pk)
    assert value["messages"][0]["legacy_reply_id"] == str(reply.pk)
    reply.refresh_from_db()
    assert reply.conversation_id == context.conversation.pk


@pytest.mark.parametrize(
    "change", ["status", "mid", "recipient", "sender", "generation", "placement", "delivery", "attempt"]
)
def test_native_receipt_relocation_requires_every_proof(context, relocated_receipt, change):
    from apps.inbox.models import DMSendAttempt, DMSendControl

    outgoing, reply, _second, connection = relocated_receipt
    if change in {"status", "mid", "recipient", "generation"}:
        field, value = {
            "status": ("status", "unknown"),
            "mid": ("platform_reply_id", "other-id"),
            "recipient": ("recipient_id", "other-peer"),
            "generation": ("connection_generation", uuid4()),
        }[change]
        InboxReply.objects.filter(pk=reply.pk).update(**{field: value})
    elif change == "attempt":
        control = DMSendControl.objects.create(
            social_account=context.account,
            workspace=context.account.workspace,
            platform=context.account.platform,
            account_platform_id=context.account.account_platform_id,
            coverage_from=timezone.now(),
            coverage_version="synthetic",
        )
        DMSendAttempt.objects.create(control=control, reply=reply, epoch=1, fingerprint="synthetic", outcome="unknown")
    else:
        field, value = {
            "sender": ("sender_id", "foreign-own"),
            "placement": ("conversation_attribution", "verified_peer"),
            "delivery": ("delivery_status", "provider_accepted"),
        }[change]
        ConversationMessage.objects.filter(pk=outgoing.pk).update(**{field: value})
    assert visible_content(outgoing)["available"] is False
    assert reader.read_conversation(context.scope, outgoing.conversation_id)["messages"] == []
