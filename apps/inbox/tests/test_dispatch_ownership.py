"""Adversarial V2 dispatcher acceptance with synthetic, offline provider calls.

SQLite checks state transitions only. Actual inter-connection lock ordering is
covered separately in test_dispatch_ownership_postgres.py on PostgreSQL.
"""

import json
import uuid
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import DatabaseError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client, RequestFactory
from django.utils import timezone

from apps.accounts.models import User
from apps.api_keys.services import issue_api_key
from apps.inbox import dm_send_gate as gate
from apps.inbox import reply_coordination as coordination
from apps.inbox import reply_dispatch as dispatch
from apps.inbox.conversations import upsert_conversation_message
from apps.inbox.models import (
    ConversationMessage,
    ConversationWorkState,
    DMConversationOwnership,
    DMSendAttempt,
    InboxConversation,
    InboxMessage,
    InboxReply,
    SendOperation,
)
from apps.inbox.services import ReplyStateError, create_reply_draft, send_reply_now
from apps.inbox.tests.test_dm_send_gate import SecureClient
from apps.members.models import WorkspaceMembership
from providers.exceptions import APIError, ProviderError

pytestmark = pytest.mark.django_db(transaction=True)
REJECTED = (ReplyStateError, coordination.ReplyCoordinationError)


@pytest.fixture
def clock():
    value = SimpleNamespace(now=timezone.now())
    with patch("django.utils.timezone.now", side_effect=lambda: value.now):
        yield value


def incoming(owner, *, mid=None, peer="synthetic-peer", outbound=False):
    owner.clock.now += timedelta(seconds=1)
    mid = mid or f"synthetic-{uuid.uuid4()}"
    legacy = None
    if not outbound:
        legacy = InboxMessage.objects.create(
            workspace=owner.account.workspace,
            social_account=owner.account,
            platform_message_id=mid,
            message_type="dm",
            sender_name="Synthetic customer",
            sender_handle=peer,
            body="Synthetic question",
            extra={
                "sender_id": peer,
                "message_recipient_id": owner.account.account_platform_id,
                "participant_ids": [owner.account.account_platform_id, peer],
            },
            received_at=owner.clock.now,
        )
    return upsert_conversation_message(
        owner.account,
        platform_message_id=mid,
        sender_id=owner.account.account_platform_id if outbound else peer,
        body="Synthetic native answer" if outbound else "Synthetic question",
        extra={
            "message_recipient_id": peer if outbound else owner.account.account_platform_id,
            "participant_ids": [owner.account.account_platform_id, peer],
        },
        occurred_at=owner.clock.now,
        source="webhook",
        legacy_message=legacy,
    )


def identity(owner):
    return {
        "conversation_id": owner.row.conversation_id,
        "social_account_id": owner.account.pk,
        "platform": owner.account.platform,
    }


def owner_cas(owner):
    owner.ownership.refresh_from_db()
    conversation = InboxConversation.objects.get(pk=owner.row.conversation_id)
    state = ConversationWorkState.objects.get(conversation=conversation)
    return {
        **identity(owner),
        "expected_epoch": owner.ownership.epoch,
        "expected_revision": conversation.revision,
        "expected_generation": state.generation,
    }


def set_paused(owner, paused=True):
    return dispatch.set_conversation_owner_paused(
        owner.scope,
        **owner_cas(owner),
        paused=paused,
        authorization=owner.authorization,
    )


@pytest.fixture
def owned(inbox_account, user, org_owner, settings, enroll_conversation_accounts, clock):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    settings.INBOX_REPLY_COORDINATION_ENABLED = True
    settings.INBOX_REPLY_DISPATCH_ENABLED = True
    enroll_conversation_accounts(inbox_account, read=True)
    member = WorkspaceMembership.objects.create(user=user, workspace=inbox_account.workspace, workspace_role="owner")
    control = gate.enroll_dm_send_control(
        account_id=inbox_account.pk,
        workspace_id=inbox_account.workspace_id,
        platform=inbox_account.platform,
        account_platform_id=inbox_account.account_platform_id,
    )
    clock.now += timedelta(seconds=1)
    control = gate.set_dm_send_paused(
        account_id=inbox_account.pk,
        workspace_id=inbox_account.workspace_id,
        paused=False,
        expected_epoch=control.epoch,
    )
    owner = SimpleNamespace(
        account=inbox_account,
        user=user,
        member=member,
        control=control,
        clock=clock,
        scope=coordination.ReplyActorScope(
            f"user:{user.pk}", inbox_account.workspace_id, frozenset({inbox_account.pk}), True
        ),
        authorization=gate.session_send_authorization(user),
    )
    owner.row = incoming(owner)
    owner.ownership = dispatch.enroll_conversation_owner(
        owner.scope, **identity(owner), authorization=owner.authorization
    )
    assert owner.ownership.paused is True
    clock.now += timedelta(seconds=1)
    owner.ownership = set_paused(owner, False)
    owner.row = incoming(owner)
    return owner


def prepare(owner, **overrides):
    conversation = InboxConversation.objects.get(pk=owner.row.conversation_id)
    state = ConversationWorkState.objects.get(conversation=conversation)
    args = {
        **identity(owner),
        "expected_revision": conversation.revision,
        "expected_generation": state.generation,
        "target_message_id": owner.row.pk,
        "body": "Synthetic owned answer",
        "idempotency_key": f"synthetic-{uuid.uuid4()}",
    }
    args.update(overrides)
    return coordination.prepare_reply(owner.scope, **args)


def claim(owner, operation=None):
    operation = operation or prepare(owner)
    state = ConversationWorkState.objects.get(conversation_id=operation.conversation_id)
    owner.clock.now = max(owner.clock.now, state.due_at)
    return coordination.claim_reply(owner.scope, operation_id=operation.pk)


def deliver(owner, operation, **overrides):
    args = {
        "operation_id": operation.pk,
        "claim_token": operation.claim_token,
        "fencing_token": operation.fencing_token,
        "expected_owner_epoch": owner.ownership.epoch,
        "authorization": owner.authorization,
        "actor": owner.user,
        "acknowledge_observed_state": True,
    }
    args.update(overrides)
    return dispatch.dispatch_reply(owner.scope, **args)


def assert_unknown(operation):
    operation.refresh_from_db()
    assert operation.status == "outcome_unknown"
    assert operation.reply_id is not None and operation.attempt_id is not None
    assert operation.external_attempted_at is not None
    assert operation.reply.status == "unknown"
    assert operation.attempt.outcome == "unknown"
    assert operation.attempt.reply_id == operation.reply_id
    assert operation.reply.inbox_message_id == operation.target.legacy_message_id
    assert not operation.reply.platform_reply_id
    assert operation.reply.sent_at is None


def test_no_implicit_ownership_or_dispatch_enrollment(inbox_account, settings):
    assert not getattr(settings, "INBOX_REPLY_DISPATCH_ENABLED", False)
    assert not DMConversationOwnership.objects.exists()
    assert not SendOperation.objects.exists()
    assert not DMSendAttempt.objects.exists()


@pytest.mark.parametrize(
    "setting,value",
    [
        ("INBOX_REPLY_DISPATCH_ENABLED", False),
        ("INBOX_REPLY_DISPATCH_ENABLED", "true"),
        ("INBOX_REPLY_DISPATCH_ENABLED", 1),
        ("INBOX_CONVERSATION_V2_ENABLED", False),
        ("INBOX_REPLY_COORDINATION_ENABLED", False),
        ("INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS", []),
    ],
)
def test_disabled_dispatch_fails_closed_without_releasing_persisted_owner(owned, settings, setting, value):
    operation = claim(owned)
    setattr(settings, setting, value)
    # Simulate a historic duplicate row predating common draft coordination.
    # New callers cannot create a second intent beside the claimed operation.
    with pytest.raises(REJECTED):
        create_reply_draft(message=owned.row.legacy_message, body="Bypass attempt", author=owned.user)
    old_draft = InboxReply.objects.create(
        inbox_message=owned.row.legacy_message, body="Bypass attempt", author=owned.user
    )
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        with pytest.raises(REJECTED):
            deliver(owned, operation)
        with pytest.raises(REJECTED):
            send_reply_now(old_draft, actor=owned.user, authorization=owned.authorization)
    provider.assert_not_called()
    assert DMConversationOwnership.objects.get().pk == owned.ownership.pk
    assert not DMSendAttempt.objects.exists()


def test_explicit_observed_state_acknowledgment_is_required(owned):
    operation = claim(owned)
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(REJECTED):
        deliver(owned, operation, acknowledge_observed_state=False)
    provider.assert_not_called()
    assert not DMSendAttempt.objects.exists()


def test_actual_operation_reply_attempt_chain_is_durable_before_provider(owned):
    operation = claim(owned)

    def provider(message, body, *, automated, before_provider):
        before_provider()
        assert connection.in_atomic_block
        assert automated is True
        assert message.pk == owned.row.legacy_message_id
        assert body == operation.body
        assert_unknown(operation)
        assert operation.ownership_id == owned.ownership.pk
        assert operation.owner_epoch == owned.ownership.epoch
        return "synthetic-owned-outbound"

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=provider) as send:
        result = deliver(owned, operation)
    assert send.call_count == 1
    result.refresh_from_db()
    assert result.pk == operation.pk and result.status == "confirmed"
    assert result.reply.status == "sent" and result.reply.platform_reply_id == "synthetic-owned-outbound"
    assert result.attempt.outcome == "sent" and result.attempt.reply_id == result.reply_id
    assert result.attempt.completed_at is not None
    with pytest.raises(ProtectedError):
        result.delete()
    assert ConversationWorkState.objects.get(conversation_id=operation.conversation_id).active_operation_id is None


@pytest.mark.parametrize("projection_failure", [False, True])
def test_new_idempotency_key_cannot_send_consumed_incoming_again(owned, projection_failure):
    operation = claim(owned)
    projection = patch("apps.inbox.conversations.record_reply", side_effect=DatabaseError("synthetic projection"))
    with patch("apps.inbox.services._dispatch_to_platform", return_value="synthetic-outbound") as provider:
        if projection_failure:
            with projection:
                deliver(owned, operation)
        else:
            deliver(owned, operation)
        # Active-operation uniqueness alone is insufficient once confirmed.
        # A second key/body must not acquire a new operation for this input.
        with pytest.raises(REJECTED):
            prepare(owned, body="A different draft", idempotency_key="different-key-after-success")
    assert provider.call_count == 1
    assert SendOperation.objects.count() == 1
    assert DMSendAttempt.objects.count() == 1


def test_same_idempotency_key_returns_original_terminal_outcome_without_redelivery(owned):
    operation = claim(owned, prepare(owned, idempotency_key="stable-key"))
    with patch("apps.inbox.services._dispatch_to_platform", return_value="synthetic-outbound") as provider:
        deliver(owned, operation)
        replay = coordination.prepare_reply(
            owned.scope,
            **identity(owned),
            expected_revision=operation.expected_revision,
            expected_generation=operation.expected_generation,
            target_message_id=operation.target_id,
            body=operation.body,
            idempotency_key="stable-key",
        )
        assert replay.pk == operation.pk and replay.status == "confirmed"
        try:
            result = deliver(owned, operation)
        except REJECTED:
            pass
        else:
            assert result.pk == operation.pk and result.status == "confirmed"
    assert provider.call_count == 1
    assert DMSendAttempt.objects.count() == 1


def test_independent_legacy_drafts_for_owned_target_cannot_dispatch(owned):
    first = create_reply_draft(message=owned.row.legacy_message, body="Independent draft 0")
    with pytest.raises(REJECTED):
        create_reply_draft(message=owned.row.legacy_message, body="Independent draft 1")
    # A preexisting second row still cannot bypass ownership or receipts.
    drafts = [first, InboxReply.objects.create(inbox_message=owned.row.legacy_message, body="Independent draft 1")]
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        for draft in drafts:
            with pytest.raises(REJECTED):
                send_reply_now(draft, actor=owned.user, automated=True, authorization=owned.authorization)
    provider.assert_not_called()
    assert not DMSendAttempt.objects.exists()


@pytest.mark.parametrize("field", ["claim_token", "fencing_token", "expected_owner_epoch"])
def test_stale_fence_or_owner_epoch_rejected_before_marker(owned, field):
    operation = claim(owned)
    bad = uuid.uuid4() if field == "claim_token" else 9999
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(REJECTED):
        deliver(owned, operation, **{field: bad})
    provider.assert_not_called()
    assert not DMSendAttempt.objects.exists()


@pytest.mark.parametrize("mutation", ["generation", "revision", "body", "lease"])
def test_stale_generation_revision_payload_or_lease_rejected_before_marker(owned, mutation):
    operation = claim(owned)
    if mutation == "generation":
        state = ConversationWorkState.objects.get(conversation_id=operation.conversation_id)
        state.generation += 1
        state.save(update_fields=["generation"])
    elif mutation == "revision":
        conversation = InboxConversation.objects.get(pk=operation.conversation_id)
        conversation.revision += 1
        conversation.save(update_fields=["revision"])
    elif mutation == "body":
        SendOperation.objects.filter(pk=operation.pk).update(body="Tampered body")
    else:
        owned.clock.now = operation.lease_expires_at
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(REJECTED):
        deliver(owned, operation)
    provider.assert_not_called()
    assert not DMSendAttempt.objects.exists()


@pytest.mark.parametrize("activity", ["new_inbound", "native_outbound", "human_handoff"])
def test_new_activity_invalidates_claim_before_any_provider_attempt(owned, activity):
    operation = claim(owned)
    generation = operation.expected_generation
    if activity == "human_handoff":
        set_paused(owned)
    else:
        incoming(owned, outbound=activity == "native_outbound")
    operation.refresh_from_db()
    state = ConversationWorkState.objects.get(conversation_id=operation.conversation_id)
    assert state.generation > generation
    assert operation.status == "superseded"
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(REJECTED):
        deliver(owned, operation)
    provider.assert_not_called()
    assert not DMSendAttempt.objects.exists()


def test_owner_pause_resume_never_revives_old_work(owned):
    operation = claim(owned)
    old_epoch = owned.ownership.epoch
    set_paused(owned)
    owned.clock.now += timedelta(seconds=1)
    resumed = set_paused(owned, False)
    assert resumed.epoch > old_epoch
    state = ConversationWorkState.objects.get(conversation_id=operation.conversation_id)
    assert state.due_at is None and state.active_operation_id is None
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        with pytest.raises(REJECTED):
            deliver(owned, operation)
        with pytest.raises(REJECTED):
            prepare(owned)
    provider.assert_not_called()


def test_different_principal_cannot_claim_an_owned_conversation(owned):
    original_scope = owned.scope
    owned.scope = replace(original_scope, actor_id="user:other-authenticated-principal")
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(REJECTED):
        prepare(owned)
    provider.assert_not_called()
    assert not SendOperation.objects.exists()


@pytest.mark.parametrize(
    "outcome", [TimeoutError("secret"), ValueError("secret"), ProviderError("secret"), "", None, "x" * 256]
)
def test_uncertain_provider_outcomes_hold_entire_chain_and_never_expire(owned, outcome):
    operation = claim(owned)
    args = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
    with patch("apps.inbox.services._dispatch_to_platform", **args) as provider, pytest.raises(REJECTED):
        deliver(owned, operation)
    assert provider.call_count == 1
    assert_unknown(operation)
    assert "secret" not in operation.reply.send_error and "secret" not in operation.outcome_code
    owned.clock.now += timedelta(days=30)
    with patch("apps.inbox.services._dispatch_to_platform") as retry:
        with pytest.raises(REJECTED):
            deliver(owned, operation)
        with pytest.raises(REJECTED):
            coordination.claim_reply(owned.scope, operation_id=operation.pk)
        with pytest.raises(REJECTED):
            prepare(owned, idempotency_key="new-key-is-not-reconciliation")
    retry.assert_not_called()
    assert_unknown(operation)
    assert DMSendAttempt.objects.count() == 1


@pytest.mark.parametrize("phase", ["reply", "attempt", "operation", "commit"])
def test_post_acceptance_database_failure_preserves_all_durable_unknown_markers(owned, phase):
    operation = claim(owned)
    seen_provider = False
    originals = {InboxReply: InboxReply.save, DMSendAttempt: DMSendAttempt.save, SendOperation: SendOperation.save}
    original_commit = connection.commit

    def failing_save(obj, *args, **kwargs):
        accepted = getattr(obj, "status", None) in {"sent", "confirmed"} or getattr(obj, "outcome", None) == "sent"
        if accepted:
            raise DatabaseError("synthetic post-acceptance persistence failure")
        return originals[type(obj)](obj, *args, **kwargs)

    def commit():
        if seen_provider:
            raise DatabaseError("synthetic post-acceptance commit failure")
        return original_commit()

    def provider(*args, **kwargs):
        nonlocal seen_provider
        seen_provider = True
        return "synthetic-accepted-before-db-failure"

    patcher = (
        patch.object(connection, "commit", side_effect=commit)
        if phase == "commit"
        else patch.object(
            {"reply": InboxReply, "attempt": DMSendAttempt, "operation": SendOperation}[phase], "save", failing_save
        )
    )
    with (
        patcher,
        patch("apps.inbox.services._dispatch_to_platform", side_effect=provider) as send,
        pytest.raises(REJECTED),
    ):
        deliver(owned, operation)
    assert send.call_count == 1
    assert_unknown(operation)
    with patch("apps.inbox.services._dispatch_to_platform") as retry, pytest.raises(REJECTED):
        deliver(owned, operation)
    retry.assert_not_called()


def test_process_crash_inside_provider_cannot_erase_attempt_or_enable_retry(owned):
    operation = claim(owned)
    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=SystemExit("synthetic crash")),
        pytest.raises(SystemExit),
    ):
        deliver(owned, operation)
    assert_unknown(operation)
    with patch("apps.inbox.services._dispatch_to_platform") as retry, pytest.raises(REJECTED):
        deliver(owned, operation)
    retry.assert_not_called()


@pytest.mark.parametrize("flags_disabled", [False, True])
def test_unknown_attempt_survives_pause_and_owner_transfer(owned, settings, flags_disabled):
    operation = claim(owned)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=TimeoutError), pytest.raises(REJECTED):
        deliver(owned, operation)
    if flags_disabled:
        settings.INBOX_CONVERSATION_V2_ENABLED = False
        settings.INBOX_REPLY_COORDINATION_ENABLED = False
        settings.INBOX_REPLY_DISPATCH_ENABLED = False
        settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    set_paused(owned)
    new_user = User.objects.create_user(email="synthetic-new-owner@example.com", password="test-only")
    WorkspaceMembership.objects.create(user=new_user, workspace=owned.account.workspace, workspace_role="owner")
    replacement = replace(owned.scope, actor_id=f"user:{new_user.pk}")
    dispatch.transfer_conversation_owner(
        owned.scope,
        **owner_cas(owned),
        new_owner_scope=replacement.actor_id,
        authorization=owned.authorization,
    )
    owned.ownership.refresh_from_db()
    assert owned.ownership.paused is True and owned.ownership.owner_scope == replacement.actor_id
    assert_unknown(operation)
    owned.scope = replacement
    owned.user = new_user
    owned.authorization = gate.session_send_authorization(new_user)
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        with pytest.raises(REJECTED):
            deliver(owned, operation)
        with pytest.raises(REJECTED):
            prepare(owned)
    provider.assert_not_called()
    with pytest.raises(ProtectedError):
        operation.reply.delete()
    with pytest.raises(ProtectedError):
        operation.delete()


def test_outer_transaction_rejected_before_network_or_marker(owned):
    operation = claim(owned)
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        with transaction.atomic(), pytest.raises(REJECTED):
            deliver(owned, operation)
        connection.set_autocommit(False)
        try:
            with pytest.raises(REJECTED):
                deliver(owned, operation)
        finally:
            connection.rollback()
            connection.set_autocommit(True)
    provider.assert_not_called()
    assert not DMSendAttempt.objects.exists()


def old_surface_send(owner, surface):
    message = owner.row.legacy_message
    reply = (
        create_reply_draft(message=message, body="Synthetic old surface answer", author=owner.user)
        if surface.endswith("draft")
        else None
    )
    if surface.startswith("ui"):
        client = Client()
        client.force_login(owner.user)
        path = f"replies/{reply.pk}/send/" if reply else f"{message.pk}/reply/"
        return client.post(f"/workspace/{owner.account.workspace_id}/inbox/{path}", {"body": "Synthetic answer"})
    issued = issue_api_key(
        workspace=owner.account.workspace,
        social_accounts=[owner.account],
        issued_by=owner.user,
        name="synthetic-owned-bypass-test",
        permissions=["use_inbox", "reply_from_inbox"],
    )
    client = SecureClient(HTTP_AUTHORIZATION=f"Bearer {issued.plaintext_token}")
    if surface.startswith("rest"):
        path = f"replies/{reply.pk}/send" if reply else f"{message.pk}/replies"
        return client.post(
            f"/api/v1/inbox/{path}",
            data=json.dumps({} if reply else {"body": "Synthetic answer", "send": True}),
            content_type="application/json",
        )
    args = {"reply_id": str(reply.pk)} if reply else {"message_id": str(message.pk), "body": "Synthetic answer"}
    return client.post(
        "/api/v1/mcp/",
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "send_reply", "arguments": args}}
        ),
        content_type="application/json",
    )


@pytest.mark.parametrize("surface", ["ui_classic", "ui_draft", "rest_new", "rest_draft", "mcp_new", "mcp_draft"])
@pytest.mark.parametrize("coordination_enabled", [True, False])
def test_actual_legacy_ui_rest_mcp_cannot_bypass_persisted_owner(owned, settings, surface, coordination_enabled):
    settings.INBOX_REPLY_COORDINATION_ENABLED = coordination_enabled
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response = old_surface_send(owned, surface)
    assert response.status_code in (200, 201, 409), response.content
    provider.assert_not_called()
    assert not DMSendAttempt.objects.exists()
    assert not InboxReply.objects.filter(status="sent").exists()
    assert b"owner" in response.content.lower() or b"v2" in response.content.lower(), response.content


def test_explicitly_unowned_conversation_retains_existing_account_gate_behavior(owned):
    other = incoming(owned, peer="different-unowned-peer")
    reply = create_reply_draft(message=other.legacy_message, body="Explicit legacy behavior", author=owned.user)
    with patch("apps.inbox.services._dispatch_to_platform", return_value="synthetic-unowned-outbound") as provider:
        send_reply_now(reply, actor=owned.user, automated=True, authorization=owned.authorization)
    assert provider.call_count == 1
    reply.refresh_from_db()
    assert reply.status == "sent"
    assert not DMConversationOwnership.objects.filter(conversation_id=other.conversation_id).exists()


def test_process_crash_after_committed_marker_cannot_enable_retry(owned):
    operation = claim(owned)
    original = gate._prepare_attempt

    def crash_after_marker(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit("synthetic process exit after durable commit")

    with (
        patch.object(gate, "_prepare_attempt", side_effect=crash_after_marker),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(SystemExit),
    ):
        deliver(owned, operation)
    provider.assert_not_called()
    assert_unknown(operation)
    owned.clock.now += timedelta(days=30)
    with patch("apps.inbox.services._dispatch_to_platform") as retry, pytest.raises(REJECTED):
        deliver(owned, operation)
    retry.assert_not_called()


@pytest.mark.parametrize("mutation", ["member", "inactive", "account", "pause", "incoming", "native"])
def test_changes_after_durable_marker_are_rechecked_before_dispatch(owned, mutation):
    operation = claim(owned)
    original = gate._prepare_attempt
    new_row = None

    def change_after_marker(*args, **kwargs):
        nonlocal new_row
        attempt = original(*args, **kwargs)
        assert_unknown(operation)
        if mutation == "member":
            owned.member.delete()
        elif mutation == "inactive":
            owned.user.is_active = False
            owned.user.save(update_fields=["is_active"])
        elif mutation == "account":
            owned.account.account_platform_id = "different-account-identity"
            owned.account.save(update_fields=["account_platform_id"])
        elif mutation == "pause":
            set_paused(owned)
        else:
            new_row = incoming(owned, outbound=mutation == "native")
        return attempt

    with (
        patch.object(gate, "_prepare_attempt", side_effect=change_after_marker),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(REJECTED),
    ):
        deliver(owned, operation)
    provider.assert_not_called()
    operation.refresh_from_db()
    assert operation.status in {"failed", "outcome_unknown"}
    assert operation.reply_id and operation.attempt_id
    assert operation.attempt.outcome in {"not_sent", "unknown"}
    assert operation.attempt.reply_id == operation.reply_id
    assert operation.reply.status in {"failed", "unknown"}
    if mutation == "incoming":
        state = ConversationWorkState.objects.get(conversation_id=operation.conversation_id)
        assert state.latest_incoming_id == new_row.pk
        assert state.generation > operation.expected_generation
        assert state.due_at is not None
        assert state.active_operation_id is None


def test_revocation_during_credentials_resolution_is_rechecked_at_provider_boundary(owned):
    operation = claim(owned)

    def resolve_credentials(*args, **kwargs):
        owned.member.delete()
        return {}

    with (
        patch("apps.publisher.engine._resolve_publish_credentials", side_effect=resolve_credentials),
        patch("apps.inbox.services.get_provider") as provider_factory,
        pytest.raises(REJECTED),
    ):
        deliver(owned, operation)
    provider_factory.return_value.reply_to_message.assert_not_called()
    operation.refresh_from_db()
    assert operation.status == "failed"
    assert operation.attempt.outcome == "not_sent"
    assert operation.reply.status == "failed"


@pytest.fixture
def key_owned(owned):
    issued = issue_api_key(
        workspace=owned.account.workspace,
        social_accounts=[owned.account],
        issued_by=owned.user,
        name="synthetic-owned-principal",
        permissions=["use_inbox", "reply_from_inbox"],
    )
    authorization = gate.key_send_authorization(issued.api_key)
    new_scope = replace(owned.scope, actor_id=f"key:{issued.api_key.pk}")
    dispatch.transfer_conversation_owner(
        owned.scope,
        **owner_cas(owned),
        new_owner_scope=new_scope.actor_id,
        authorization=owned.authorization,
    )
    owned.scope, owned.authorization, owned.api_key = new_scope, authorization, issued.api_key
    owned.plaintext_token = issued.plaintext_token
    owned.clock.now += timedelta(seconds=1)
    owned.ownership = set_paused(owned, False)
    owned.row = incoming(owned)
    return owned


@pytest.mark.parametrize("mutation", ["revoke", "allowlist", "permission", "inbox_permission"])
def test_current_key_authority_is_rechecked_after_marker(key_owned, mutation):
    owned = key_owned
    operation = claim(owned)
    original = gate._prepare_attempt

    def change_after_marker(*args, **kwargs):
        attempt = original(*args, **kwargs)
        if mutation == "revoke":
            owned.api_key.revoked_at = owned.clock.now
            owned.api_key.save(update_fields=["revoked_at"])
        elif mutation in {"permission", "inbox_permission"}:
            owned.api_key.permissions = [] if mutation == "permission" else ["reply_from_inbox"]
            owned.api_key.save(update_fields=["permissions"])
        else:
            owned.api_key.social_accounts.clear()
        return attempt

    with (
        patch.object(gate, "_prepare_attempt", side_effect=change_after_marker),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(REJECTED),
    ):
        deliver(owned, operation)
    provider.assert_not_called()
    operation.refresh_from_db()
    assert operation.status == "failed"
    assert operation.attempt.outcome == "not_sent"
    assert operation.reply.status == "failed"


def test_known_provider_refusal_settles_entire_chain_without_claiming_delivery(owned):
    operation = claim(owned)
    refusal = APIError("synthetic refusal", platform="Facebook", status_code=403)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=refusal) as provider, pytest.raises(REJECTED):
        deliver(owned, operation)
    assert provider.call_count == 1
    operation.refresh_from_db()
    assert operation.status == "failed"
    assert operation.reply.status == "failed"
    assert operation.attempt.outcome == "not_sent"
    assert not operation.reply.platform_reply_id


def test_missing_legacy_target_mapping_fails_closed(owned):
    operation = claim(owned)
    ConversationMessage.objects.filter(pk=owned.row.pk).update(legacy_message=None)
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(REJECTED):
        deliver(owned, operation)
    provider.assert_not_called()
    assert not DMSendAttempt.objects.exists()


def test_confirmed_app_reply_allows_only_a_genuinely_new_incoming_generation(owned):
    first = claim(owned)
    with patch(
        "apps.inbox.services._dispatch_to_platform",
        side_effect=["synthetic-first-outbound", "synthetic-second-outbound"],
    ) as provider:
        deliver(owned, first)
        state = ConversationWorkState.objects.get(conversation_id=first.conversation_id)
        assert state.owner_paused is False
        assert state.due_at is None
        owned.row = incoming(owned)
        second = claim(owned)
        assert second.target_id != first.target_id
        assert second.expected_generation > first.expected_generation
        assert second.fencing_token > first.fencing_token
        deliver(owned, second)
    assert provider.call_count == 2
    assert set(SendOperation.objects.values_list("status", flat=True)) == {"confirmed"}
    assert DMSendAttempt.objects.filter(outcome="sent").count() == 2


@pytest.mark.parametrize("mutation", ["delete", "expire", "scope"])
def test_current_oauth_bearer_is_rechecked_after_marker(owned, mutation):
    from oauth2_provider.models import get_access_token_model

    from apps.api.auth import _resolve_oauth_actor
    from apps.mcp.tests.test_oauth_auth import _mint_oauth_token

    owned.user.last_workspace_id = owned.account.workspace_id
    owned.user.save(update_fields=["last_workspace_id"])
    bearer = _mint_oauth_token(owned.user)
    actor = _resolve_oauth_actor(bearer)
    request = RequestFactory().post("/api/v1/mcp/", HTTP_AUTHORIZATION=f"Bearer {bearer}")
    authorization = gate.key_send_authorization(actor, request)
    new_scope = replace(owned.scope, actor_id=f"oauth:{owned.user.pk}")
    dispatch.transfer_conversation_owner(
        owned.scope,
        **owner_cas(owned),
        new_owner_scope=new_scope.actor_id,
        authorization=owned.authorization,
    )
    owned.scope, owned.authorization = new_scope, authorization
    owned.clock.now += timedelta(seconds=1)
    owned.ownership = set_paused(owned, False)
    owned.row = incoming(owned)
    operation = claim(owned)
    original = gate._prepare_attempt

    def change_after_marker(*args, **kwargs):
        attempt = original(*args, **kwargs)
        tokens = get_access_token_model().objects.filter(user=owned.user)
        if mutation == "delete":
            tokens.delete()
        elif mutation == "expire":
            tokens.update(expires=owned.clock.now - timedelta(seconds=1))
        else:
            tokens.update(scope="unrelated")
        return attempt

    with (
        patch.object(gate, "_prepare_attempt", side_effect=change_after_marker),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(REJECTED),
    ):
        deliver(owned, operation)
    provider.assert_not_called()
    operation.refresh_from_db()
    assert operation.status == "failed"
    assert operation.attempt.outcome == "not_sent"
    assert operation.reply.status == "failed"


def test_actual_provider_adapter_keeps_automated_semantics_and_exact_recipient(owned):
    operation = claim(owned)
    with (
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
        patch("apps.inbox.services.get_provider") as provider_factory,
    ):
        provider_factory.return_value.reply_to_message.return_value = SimpleNamespace(
            platform_message_id="synthetic-leaf-outbound"
        )
        delivered = deliver(owned, operation)
    provider_factory.return_value.reply_to_message.assert_called_once()
    kwargs = provider_factory.return_value.reply_to_message.call_args.kwargs
    assert kwargs["human_agent"] is False
    assert kwargs["message_id"] == owned.row.platform_message_id
    assert kwargs["text"] == operation.body
    assert kwargs["extra"]["recipient_id"] == owned.row.sender_id
    assert delivered.status == "confirmed"
    assert delivered.reply.status == "sent" and delivered.attempt.outcome == "sent"


@pytest.mark.parametrize("withdrawal", ["all_flags_and_enrollment", "disconnected"])
def test_persisted_hold_can_be_tightened_after_rollout_withdrawal_but_not_resumed(owned, settings, withdrawal):
    operation = claim(owned)
    if withdrawal == "all_flags_and_enrollment":
        settings.INBOX_CONVERSATION_V2_ENABLED = False
        settings.INBOX_REPLY_COORDINATION_ENABLED = False
        settings.INBOX_REPLY_DISPATCH_ENABLED = False
        settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    else:
        owned.account.connection_status = "disconnected"
        owned.account.save(update_fields=["connection_status"])
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        held = set_paused(owned)
        assert held.paused is True
        with pytest.raises(REJECTED):
            set_paused(owned, False)
        with pytest.raises(REJECTED):
            deliver(owned, operation)
    provider.assert_not_called()
    operation.refresh_from_db()
    assert operation.status == "superseded"
    owned.ownership.refresh_from_db()
    assert owned.ownership.paused is True
    assert not DMSendAttempt.objects.exists()
