"""Fresh offline proofs for explicit conversation actions and preserved receipts."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.inbox.composer_authorization import session_read_authorization
from apps.inbox.conversation_composer import (
    composer_context,
    retire_failed_conversation_reply,
    save_conversation_draft,
)
from apps.inbox.conversation_composer import (
    send_conversation_reply as _send_conversation_reply,
)
from apps.inbox.dm_send_gate import DMSendGateError, session_send_authorization
from apps.inbox.models import ConversationMessage, InboxConversation, InboxMessage, InboxReply
from apps.members.models import WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def composer(inbox_account, user, org_owner, settings):
    WorkspaceMembership.objects.create(user=user, workspace=inbox_account.workspace, workspace_role="owner")
    settings.INBOX_CONVERSATION_COMPOSER_ENABLED = True
    settings.INBOX_CONVERSATION_WORKFLOW_ENABLED = True
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    identity = {
        "workspace_id": str(inbox_account.workspace_id),
        "social_account_id": str(inbox_account.pk),
        "platform": inbox_account.platform,
    }
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = [identity]
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = [identity]
    conversation = InboxConversation.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform=inbox_account.platform,
        platform_conversation_id="synthetic-native-thread",
        peer_id="synthetic-peer",
        identity_kind="platform",
        conversation_type="direct",
        classification_reason="participants_pair",
        workflow_state="needs_action",
    )
    row = ConversationMessage.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform=inbox_account.platform,
        conversation=conversation,
        platform_message_id="synthetic-native-inbound",
        direction="inbound",
        conversation_attribution="platform",
        conversation_type="direct",
        classification_reason="participants_pair",
        sender_id="synthetic-peer",
        recipient_id=inbox_account.account_platform_id,
        occurred_at=timezone.now() - timedelta(minutes=1),
        body="Synthetic customer question",
        delivery_status="observed",
    )
    return SimpleNamespace(
        account=inbox_account,
        user=user,
        conversation=conversation,
        row=row,
        authorization=session_send_authorization(user),
    )


def inputs(composer, *, body="Synthetic answer", nonce=None):
    context = composer_context(composer.conversation, authorization=composer.authorization)
    return dict(
        message=composer.conversation,
        body=body,
        action_nonce=nonce or context["action_nonce"],
        expected_revision=context["composer_revision"],
        scope_token=context["scope_token"],
        authorization=composer.authorization,
        author=composer.user,
    )


def accepted(*args, **kwargs):
    kwargs["before_provider"]()
    return "synthetic-outgoing-" + str(uuid4())


def send_conversation_reply(**kwargs):
    kwargs.setdefault("draft_authorization", session_read_authorization(kwargs.get("author")))
    return _send_conversation_reply(**kwargs)


def test_context_has_no_writes_and_canonical_only_send_uses_empty_transport_projection(composer):
    params = inputs(composer)
    assert InboxMessage.objects.count() == InboxReply.objects.count() == 0
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        reply = send_conversation_reply(**params, automated=True)
    assert provider.call_count == 1
    assert reply.status == "sent"
    target = reply.inbox_message
    assert target.body == "" and target.status == "archived"
    assert target.extra["transport_projection"] is True
    composer.row.refresh_from_db()
    composer.conversation.refresh_from_db()
    assert composer.row.legacy_message_id == target.pk
    assert composer.conversation.active_reply_id is None
    assert composer.conversation.workflow_state == "waiting"


def test_distinct_sequential_actions_and_same_nonce_replay(composer):
    first = inputs(composer)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        reply1 = send_conversation_reply(**first, automated=True)
        replay = send_conversation_reply(**first, automated=True)
        reply2 = send_conversation_reply(**inputs(composer, body="Second distinct message"), automated=True)
    assert reply1.pk == replay.pk and reply2.pk != reply1.pk
    assert provider.call_count == 2
    assert InboxReply.objects.filter(conversation=composer.conversation).count() == 2


def test_frozen_nonce_cannot_change_body(composer):
    params = inputs(composer)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        send_conversation_reply(**params)
        with pytest.raises(DMSendGateError, match="frozen action"):
            send_conversation_reply(**{**params, "body": "Different"})
    assert provider.call_count == 1


def test_only_active_draft_is_edited_under_revision(composer):
    first = inputs(composer)
    reply = save_conversation_draft(**first)
    with pytest.raises(DMSendGateError, match="another tab"):
        save_conversation_draft(**{**first, "body": "Stale edit"})
    current = inputs(composer, body="Current edit")
    edited = save_conversation_draft(**current)
    assert edited.pk == reply.pk and edited.body == "Current edit"
    with pytest.raises(DMSendGateError, match="active conversation"):
        save_conversation_draft(**{**inputs(composer), "action_nonce": str(uuid4())})


def test_unknown_never_retires_or_creates_successor(composer):
    params = inputs(composer)
    reply = save_conversation_draft(**params)
    InboxReply.objects.filter(pk=reply.pk).update(status="unknown", send_generation=1)
    context = composer_context(composer.conversation, authorization=composer.authorization)
    with pytest.raises(DMSendGateError):
        retire_failed_conversation_reply(
            message=composer.conversation,
            reply_id=reply.pk,
            expected_revision=context["composer_revision"],
            scope_token=context["scope_token"],
            authorization=composer.authorization,
        )
    with pytest.raises(DMSendGateError):
        save_conversation_draft(**{**inputs(composer), "action_nonce": str(uuid4())})
    reply.refresh_from_db()
    assert reply.status == "unknown" and reply.retired_at is None


def test_verified_refusal_can_retire_after_withdrawal_preserving_nonce_and_receipt(composer):
    params = inputs(composer)
    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=NotImplementedError),
        pytest.raises(DMSendGateError),
    ):
        send_conversation_reply(**params)
    reply = InboxReply.objects.get(conversation=composer.conversation)
    assert reply.status == "failed" and reply.not_sent_verified
    ConversationMessage.objects.filter(pk=composer.row.pk).update(is_deleted=True, body="", content_status="deleted")
    context = composer_context(composer.conversation, authorization=composer.authorization)
    retired = retire_failed_conversation_reply(
        message=composer.conversation,
        reply_id=reply.pk,
        expected_revision=context["composer_revision"],
        scope_token=context["scope_token"],
        authorization=composer.authorization,
    )
    assert retired.retired_at and str(retired.action_nonce) == params["action_nonce"]
    assert retired.status == "failed" and retired.send_generation == 1
    with pytest.raises(DMSendGateError):
        send_conversation_reply(**params)


def test_verified_refusal_successor_requires_explicit_retirement_and_new_nonce(composer):
    params = inputs(composer)
    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=NotImplementedError),
        pytest.raises(DMSendGateError),
    ):
        send_conversation_reply(**params)
    receipt = InboxReply.objects.get(conversation=composer.conversation)
    with pytest.raises(DMSendGateError, match="Explicitly retire"):
        send_conversation_reply(**inputs(composer))
    context = composer_context(composer.conversation, authorization=composer.authorization)
    retire_failed_conversation_reply(
        message=composer.conversation,
        reply_id=receipt.pk,
        expected_revision=context["composer_revision"],
        scope_token=context["scope_token"],
        authorization=composer.authorization,
    )
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        successor = send_conversation_reply(**inputs(composer), automated=True)
    assert successor.pk != receipt.pk and provider.call_count == 1
    assert InboxReply.objects.filter(pk=receipt.pk, status="failed", retired_at__isnull=False).exists()


def test_multiple_legacy_drafts_require_explicit_adoption_and_all_rows_survive(composer):
    from apps.inbox.canonical_send_target import transport_target

    target = transport_target(composer.row, composer.conversation, composer.account, materialize=True)
    old1 = InboxReply.objects.create(inbox_message=target, body="Historical one")
    old2 = InboxReply.objects.create(inbox_message=target, body="Historical two")
    with pytest.raises(DMSendGateError, match="Select a historical draft"):
        save_conversation_draft(**inputs(composer))
    reply = save_conversation_draft(**inputs(composer), adopt_reply_id=str(old2.pk))
    assert reply.pk == old2.pk
    old1.refresh_from_db()
    assert old1.body == "Historical one" and old1.conversation_id is None
    assert InboxReply.objects.count() == 2


@pytest.mark.parametrize("mutation", ["group", "unknown", "peer", "native", "own", "disconnected"])
def test_changed_scope_or_identity_holds_without_provider(composer, mutation):
    params = inputs(composer)
    if mutation in {"group", "unknown"}:
        InboxConversation.objects.filter(pk=composer.conversation.pk).update(conversation_type=mutation)
    elif mutation == "peer":
        InboxConversation.objects.filter(pk=composer.conversation.pk).update(peer_id="other-peer")
    elif mutation == "native":
        InboxConversation.objects.filter(pk=composer.conversation.pk).update(platform_conversation_id="other-thread")
    elif mutation == "own":
        type(composer.account).objects.filter(pk=composer.account.pk).update(account_platform_id="other-own")
    else:
        type(composer.account).objects.filter(pk=composer.account.pk).update(connection_status="disconnected")
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(DMSendGateError):
        send_conversation_reply(**params)
    provider.assert_not_called()


def test_outbound_only_does_not_create_fake_incoming(composer):
    ConversationMessage.objects.filter(pk=composer.row.pk).update(direction="outbound")
    with pytest.raises(DMSendGateError):
        save_conversation_draft(**inputs(composer))
    assert not InboxMessage.objects.exists() and not InboxReply.objects.exists()


def test_conflicting_existing_provider_identity_is_not_adopted(composer):
    InboxMessage.objects.create(
        workspace=composer.account.workspace,
        social_account=composer.account,
        platform_message_id=composer.row.platform_message_id,
        message_type="dm",
        sender_handle="other-peer",
        sender_name="Other",
        body="Keep original",
        received_at=composer.row.occurred_at,
    )
    with pytest.raises(DMSendGateError):
        save_conversation_draft(**inputs(composer))
    assert InboxMessage.objects.get().body == "Keep original" and not InboxReply.objects.exists()


def test_draft_remains_editable_after_anchor_withdrawn_but_send_is_held(composer):
    save_conversation_draft(**inputs(composer))
    ConversationMessage.objects.filter(pk=composer.row.pk).update(is_deleted=True, body="")
    params = inputs(composer, body="Keep composing")
    edited = save_conversation_draft(**params)
    assert edited.body == "Keep composing"
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(DMSendGateError):
        send_conversation_reply(**inputs(composer, body="Keep composing"))
    provider.assert_not_called()


def test_revoked_permission_holds_even_same_nonce_receipt(composer):
    params = inputs(composer)
    save_conversation_draft(**params)
    WorkspaceMembership.objects.filter(user=composer.user, workspace=composer.account.workspace).delete()
    with pytest.raises(DMSendGateError):
        save_conversation_draft(**params)


def test_new_incoming_during_send_prevents_waiting(composer):
    params = inputs(composer)

    def newer(*args, **kwargs):
        kwargs["before_provider"]()
        InboxConversation.objects.filter(pk=composer.conversation.pk).update(incoming_generation=1)
        return "synthetic-outgoing"

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=newer):
        send_conversation_reply(**params)
    composer.conversation.refresh_from_db()
    assert composer.conversation.workflow_state == "needs_action"


@pytest.mark.parametrize("source", ["poll", "webhook", "comment_edit"])
def test_late_legacy_writers_never_overwrite_transport_projection(composer, source):
    from apps.inbox.tasks import InboxSyncEngine
    from apps.inbox.webhooks import _create_if_new, _upsert_facebook_comment

    reply = save_conversation_draft(**inputs(composer))
    target = reply.inbox_message
    original = InboxMessage.objects.values().get(pk=target.pk)
    with patch("apps.inbox.tasks.InboxSyncEngine._notify_new_message") as notification:
        if source == "poll":
            InboxSyncEngine()._upsert_message(
                composer.account,
                SimpleNamespace(
                    platform_message_id=target.platform_message_id,
                    message_type="dm",
                    sender_id=target.sender_handle,
                    sender_name="Provider name",
                    text="Later provider text",
                    extra=target.extra,
                    timestamp=target.received_at,
                ),
            )
        elif source == "webhook":
            _create_if_new(
                account=composer.account,
                platform_message_id=target.platform_message_id,
                message_type="dm",
                sender_name="Provider name",
                sender_id=target.sender_handle,
                body="Later provider text",
                extra=target.extra,
                received_at=target.received_at,
            )
        else:
            _upsert_facebook_comment(
                composer.account,
                {
                    "comment_id": target.platform_message_id,
                    "verb": "edited",
                    "from": {"id": target.sender_handle},
                    "message": "Wrong domain",
                },
            )
    assert InboxMessage.objects.values().get(pk=target.pk) == original
    notification.assert_not_called()


def test_inbox_member_can_draft_without_send_permission(composer):
    from apps.members.models import CustomRole

    role = CustomRole.objects.create(
        organization=composer.account.workspace.organization,
        name="Synthetic draft only",
        permissions={"use_inbox": True, "reply_from_inbox": False},
    )
    WorkspaceMembership.objects.filter(user=composer.user, workspace=composer.account.workspace).update(
        custom_role=role
    )
    draft_auth = session_read_authorization(composer.user)
    context = composer_context(composer.conversation, authorization=draft_auth)
    assert context["conversation"].pk == composer.conversation.pk
    params = dict(
        message=composer.conversation,
        body="Draft only",
        action_nonce=context["action_nonce"],
        expected_revision=context["composer_revision"],
        scope_token=context["scope_token"],
        author=composer.user,
    )
    reply = save_conversation_draft(**params, authorization=draft_auth)
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(DMSendGateError):
        _send_conversation_reply(**params, draft_authorization=draft_auth, authorization=composer.authorization)
    provider.assert_not_called()
    reply.refresh_from_db()
    assert reply.status == "draft" and reply.send_generation == 0


def test_workflow_flag_off_does_not_leave_successful_composer_slot_stuck(composer, settings):
    settings.INBOX_CONVERSATION_WORKFLOW_ENABLED = False
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted):
        first = send_conversation_reply(**inputs(composer))
        second = send_conversation_reply(**inputs(composer, body="Second action"))
    assert first.pk != second.pk
    composer.conversation.refresh_from_db()
    assert composer.conversation.active_reply_id is None
    assert composer.conversation.workflow_state == "needs_action"


def test_historical_draft_preview_is_bounded_but_explicit_adoption_is_not_first_page_only(composer):
    from apps.inbox.canonical_send_target import transport_target

    target = transport_target(composer.row, composer.conversation, composer.account, materialize=True)
    rows = [InboxReply.objects.create(inbox_message=target, body=f"Historical {index}") for index in range(12)]
    state = composer_context(composer.conversation, authorization=composer.authorization)
    assert len(state["legacy_drafts"]) == 10 and state["has_legacy_drafts"] and state["requires_legacy_adoption"]
    selected = composer_context(composer.conversation, authorization=composer.authorization, adopt_reply_id=rows[-1].pk)
    assert selected["adopt_reply"].pk == rows[-1].pk
    assert not selected["requires_legacy_adoption"]
    params = inputs(composer)
    params["adopt_reply_id"] = rows[-1].pk
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted):
        send_conversation_reply(**params)
    state = composer_context(composer.conversation, authorization=composer.authorization)
    assert state["has_legacy_drafts"] and not state["requires_legacy_adoption"]
    assert state["send_availability"]["allowed"]
    assert InboxReply.objects.count() == 12


def test_verified_not_sent_legacy_receipt_is_preserved_without_unavailable_draft_adoption(composer):
    from apps.inbox.canonical_send_target import transport_target

    target = transport_target(composer.row, composer.conversation, composer.account, materialize=True)
    refused = InboxReply.objects.create(
        inbox_message=target, body="Historical refusal", status="failed", not_sent_verified=True
    )
    state = composer_context(composer.conversation, authorization=composer.authorization)
    assert not state["requires_legacy_adoption"] and state["send_availability"]["allowed"]
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted):
        assert send_conversation_reply(**inputs(composer)).status == "sent"
    refused.refresh_from_db()
    assert refused.body == "Historical refusal" and refused.status == "failed" and refused.conversation_id is None
