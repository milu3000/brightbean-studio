"""Synthetic current-owner actions use the real claim and receipt dispatcher."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.inbox import dm_send_gate as gate
from apps.inbox import reply_coordination as coordination
from apps.inbox import reply_dispatch as dispatch
from apps.inbox.canonical_access import session_read_scope
from apps.inbox.canonical_reads import read_conversation
from apps.inbox.composer_authorization import session_read_authorization
from apps.inbox.conversation_composer import composer_context, save_conversation_draft, send_conversation_reply
from apps.inbox.conversations import upsert_conversation_message
from apps.inbox.models import ConversationWorkState, InboxConversation, SendOperation
from apps.inbox.tests.test_dispatch_ownership import clock as clock
from apps.members.models import WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def incoming(ctx, *, outbound=False):
    ctx.clock.now += timedelta(seconds=1)
    ctx.row = upsert_conversation_message(
        ctx.account,
        platform_message_id=f"synthetic-{uuid4()}",
        sender_id=ctx.account.account_platform_id if outbound else "synthetic-peer",
        body="Synthetic message",
        extra={
            "conversation_id": "synthetic-thread",
            "message_recipient_id": "synthetic-peer" if outbound else ctx.account.account_platform_id,
            "participant_ids": [ctx.account.account_platform_id, "synthetic-peer"],
        },
        occurred_at=ctx.clock.now,
        source="webhook",
    )
    ctx.conversation = InboxConversation.objects.get(pk=ctx.row.conversation_id)
    if ctx.conversation.workflow_baseline_at:
        from apps.inbox.conversation_workflow import LiveObservation, observe_canonical_message

        observe_canonical_message(
            ctx.row,
            source="webhook",
            is_new=True,
            observation=LiveObservation(ctx.conversation.workflow_baseline_at, ctx.clock.now),
        )
        ctx.conversation.refresh_from_db()
    return ctx.row


@pytest.fixture
def owner(inbox_account, user, org_owner, settings, enroll_conversation_accounts, clock):
    for flag in (
        "INBOX_CONVERSATION_V2_ENABLED",
        "INBOX_REPLY_COORDINATION_ENABLED",
        "INBOX_REPLY_DISPATCH_ENABLED",
        "INBOX_CONVERSATION_COMPOSER_ENABLED",
        "INBOX_CONVERSATION_WORKFLOW_ENABLED",
        "INBOX_CANONICAL_READ_ENABLED",
    ):
        setattr(settings, flag, True)
    enroll_conversation_accounts(inbox_account, read=True)
    WorkspaceMembership.objects.create(user=user, workspace=inbox_account.workspace, workspace_role="owner")
    control = gate.enroll_dm_send_control(
        account_id=inbox_account.pk,
        workspace_id=inbox_account.workspace_id,
        platform=inbox_account.platform,
        account_platform_id=inbox_account.account_platform_id,
    )
    clock.now += timedelta(seconds=1)
    control = gate.set_dm_send_paused(
        account_id=inbox_account.pk, workspace_id=inbox_account.workspace_id, paused=False, expected_epoch=control.epoch
    )
    ctx = SimpleNamespace(
        account=inbox_account,
        user=user,
        clock=clock,
        control=control,
        authorization=gate.session_send_authorization(user),
        draft_authorization=session_read_authorization(user),
        scope=coordination.ReplyActorScope(
            f"user:{user.pk}", inbox_account.workspace_id, frozenset({inbox_account.pk}), True
        ),
    )
    incoming(ctx)
    ctx.ownership = dispatch.enroll_conversation_owner(
        ctx.scope,
        conversation_id=ctx.conversation.pk,
        social_account_id=inbox_account.pk,
        platform=inbox_account.platform,
        authorization=ctx.authorization,
    )
    clock.now += timedelta(seconds=1)
    state = ConversationWorkState.objects.get(conversation=ctx.conversation)
    ctx.ownership = dispatch.set_conversation_owner_paused(
        ctx.scope,
        conversation_id=ctx.conversation.pk,
        social_account_id=inbox_account.pk,
        platform=inbox_account.platform,
        authorization=ctx.authorization,
        expected_epoch=ctx.ownership.epoch,
        expected_revision=ctx.conversation.revision,
        expected_generation=state.generation,
        paused=False,
    )
    incoming(ctx)
    InboxConversation.objects.filter(pk=ctx.conversation.pk).update(
        workflow_state="needs_action", workflow_baseline_at=ctx.clock.now - timedelta(minutes=1)
    )
    incoming(ctx)
    return ctx


def observe(owner):
    return read_conversation(session_read_scope(owner.user, owner.account.workspace_id), owner.conversation.pk)[
        "composer_observation_token"
    ]


def inputs(owner, *, token=None, body="Synthetic response"):
    token = token or observe(owner)
    state = composer_context(
        owner.conversation,
        authorization=owner.draft_authorization,
        send_authorization=owner.authorization,
        observation_token=token,
    )
    return dict(
        message=owner.conversation,
        body=body,
        action_nonce=state["action_nonce"],
        expected_revision=state["composer_revision"],
        scope_token=state["scope_token"],
        author=owner.user,
        observation_token=token,
    )


def send(owner, arguments, *, automated=False):
    return send_conversation_reply(
        **arguments,
        draft_authorization=owner.draft_authorization,
        authorization=owner.authorization,
        automated=automated,
    )


def accepted(*args, **kwargs):
    kwargs["before_provider"]()
    return "synthetic-outgoing-" + str(uuid4())


def test_owned_send_uses_existing_operation_and_same_nonce_replays(owner):
    args = inputs(owner)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        reply = send(owner, args)
        replay = send(owner, args)
    assert reply.pk == replay.pk and reply.status == "sent"
    assert provider.call_count == 1
    operation = SendOperation.objects.get(reply=reply)
    assert operation.status == "confirmed" and operation.conversation_action_nonce == reply.action_nonce
    assert operation.ownership_id == owner.ownership.pk and operation.attempt.outcome == "sent"


def test_distinct_explicit_actions_can_follow_own_confirmed_reply(owner):
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        first = send(owner, inputs(owner))
        owner.clock.now += timedelta(seconds=1)
        second = send(owner, inputs(owner, body="Explicit follow up"))
    assert first.pk != second.pk and provider.call_count == 2
    assert SendOperation.objects.filter(status="confirmed").count() == 2


def test_stale_observation_preserves_text_and_requires_actual_newest_read(owner):
    args = inputs(owner)
    draft = save_conversation_draft(**args, authorization=owner.draft_authorization)
    incoming(owner)
    fresh = inputs(owner, token=args["observation_token"], body=draft.body)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        with pytest.raises(ValueError):
            send(owner, fresh)
        assert provider.call_count == 0
        latest = inputs(owner, body=draft.body)
        assert send(owner, latest).status == "sent"


def test_seen_new_incoming_rebinds_only_unattempted_draft_generation(owner):
    args = inputs(owner)
    draft = save_conversation_draft(**args, authorization=owner.draft_authorization)
    original = draft.conversation_incoming_generation
    incoming(owner)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted):
        sent = send(owner, inputs(owner, body=draft.body))
    owner.conversation.refresh_from_db()
    assert sent.conversation_incoming_generation == original + 1 == owner.conversation.incoming_generation
    assert owner.conversation.workflow_state == "waiting"


def test_incoming_during_dispatch_is_not_marked_handled(owner):
    newer = {}

    def provider(*args, **kwargs):
        kwargs["before_provider"]()
        incoming(owner)
        work = ConversationWorkState.objects.get(conversation=owner.conversation)
        newer.update(
            generation=work.generation,
            latest_incoming_id=work.latest_incoming_id,
            due_at=work.due_at,
            burst_started_at=work.burst_started_at,
        )
        return "synthetic-native-reply-raced"

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=provider):
        sent = send(owner, inputs(owner))
    owner.conversation.refresh_from_db()
    assert sent.status == "sent"
    assert sent.conversation_incoming_generation < owner.conversation.incoming_generation
    assert owner.conversation.workflow_state == "needs_action"
    work = ConversationWorkState.objects.get(conversation=owner.conversation)
    assert {key: getattr(work, key) for key in newer} == newer
    assert work.due_at is not None


@pytest.mark.parametrize(
    "change", ["principal", "owner_pause", "control_pause", "permission", "history_gap", "native_outgoing"]
)
def test_existing_owner_barriers_are_preserved(owner, change):
    args = inputs(owner)
    if change == "principal":
        owner.ownership.owner_scope = "user:" + str(uuid4())
        owner.ownership.save(update_fields=["owner_scope"])
    elif change == "owner_pause":
        owner.ownership.paused = True
        owner.ownership.save(update_fields=["paused"])
    elif change == "control_pause":
        owner.control.paused = True
        owner.control.save(update_fields=["paused"])
    elif change == "permission":
        WorkspaceMembership.objects.filter(user=owner.user, workspace=owner.account.workspace).delete()
    elif change == "history_gap":
        ConversationWorkState.objects.filter(conversation=owner.conversation).update(history_gap=True)
    elif change == "native_outgoing":
        incoming(owner, outbound=True)
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(ValueError):
        send(owner, args)
    provider.assert_not_called()
    assert not SendOperation.objects.filter(external_attempted_at__isnull=False).exists()


def test_verified_refusal_explicit_retire_allows_new_nonce_preserving_receipt(owner):
    from apps.inbox.conversation_composer import retire_failed_conversation_reply
    from providers.exceptions import APIError

    args = inputs(owner)
    with (
        patch(
            "apps.inbox.services._dispatch_to_platform",
            side_effect=APIError("synthetic refusal", platform="Facebook", status_code=403),
        ),
        pytest.raises(ValueError),
    ):
        send(owner, args)
    operation = SendOperation.objects.get()
    assert operation.status == "failed" and operation.attempt.outcome == "not_sent"
    state = composer_context(owner.conversation, authorization=owner.draft_authorization)
    assert state["can_retire_failed"]
    retire_failed_conversation_reply(
        message=owner.conversation,
        reply_id=operation.reply_id,
        expected_revision=state["composer_revision"],
        scope_token=state["scope_token"],
        authorization=owner.draft_authorization,
    )
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted):
        successor = send(owner, inputs(owner, body="Explicit new action after refusal"))
    operation.refresh_from_db()
    assert successor.action_nonce != operation.conversation_action_nonce
    assert operation.status == "failed" and operation.reply.retired_at is not None


def test_unknown_never_retries_or_retires(owner):
    from apps.inbox.conversation_composer import retire_failed_conversation_reply, retire_unattempted_conversation_draft

    args = inputs(owner)
    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=TimeoutError("synthetic timeout")),
        pytest.raises(ValueError),
    ):
        send(owner, args)
    operation = SendOperation.objects.get()
    assert operation.status == "outcome_unknown" and operation.attempt.outcome == "unknown"
    state = composer_context(owner.conversation, authorization=owner.draft_authorization)
    assert not state["can_retire_failed"] and not state["can_retire_draft"]
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        with pytest.raises(ValueError):
            send(owner, args)
        for retire in [retire_failed_conversation_reply, retire_unattempted_conversation_draft]:
            with pytest.raises(ValueError):
                retire(
                    message=owner.conversation,
                    reply_id=operation.reply_id,
                    expected_revision=state["composer_revision"],
                    scope_token=state["scope_token"],
                    authorization=owner.draft_authorization,
                )
    provider.assert_not_called()
    operation.refresh_from_db()
    assert operation.status == "outcome_unknown"


@pytest.mark.parametrize("automated,age", [(False, 0.5), (False, 2), (True, 0.5), (False, 8)])
def test_only_new_human_action_can_answer_old_incoming(owner, automated, age):
    # This actual verified latest incoming predates both resume boundaries.
    from apps.inbox.models import ConversationMessage

    ConversationMessage.objects.filter(conversation=owner.conversation).exclude(pk=owner.row.pk).delete()
    ConversationMessage.objects.filter(pk=owner.row.pk).update(
        occurred_at=owner.clock.now - timedelta(days=age), first_seen_at=owner.clock.now - timedelta(days=age)
    )
    owner.clock.now += timedelta(seconds=1)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        if automated:
            with pytest.raises(ValueError):
                send(owner, inputs(owner), automated=automated)
            provider.assert_not_called()
        else:
            assert send(owner, inputs(owner)).status == "sent"
            assert provider.call_count == 1
    owner.ownership.refresh_from_db()
    owner.control.refresh_from_db()
    assert owner.ownership.resume_cutoff is not None and owner.control.resume_cutoff is not None


@pytest.mark.parametrize("automated", [False, True])
def test_enrollment_after_history_does_not_require_a_fake_live_incoming(owner, automated):
    from apps.inbox.models import DMConversationOwnership

    # Recreate the real operator enrollment transition over existing history.
    # No message is inserted, edited or replayed to fabricate pending work.
    DMConversationOwnership.objects.filter(pk=owner.ownership.pk).delete()
    ConversationWorkState.objects.filter(conversation=owner.conversation).delete()
    owner.conversation.refresh_from_db()
    owner.ownership = dispatch.enroll_conversation_owner(
        owner.scope,
        conversation_id=owner.conversation.pk,
        social_account_id=owner.account.pk,
        platform=owner.account.platform,
        authorization=owner.authorization,
    )
    state = ConversationWorkState.objects.get(conversation=owner.conversation)
    assert state.latest_incoming_id is None
    owner.clock.now += timedelta(seconds=1)
    owner.ownership = dispatch.set_conversation_owner_paused(
        owner.scope,
        conversation_id=owner.conversation.pk,
        social_account_id=owner.account.pk,
        platform=owner.account.platform,
        authorization=owner.authorization,
        expected_epoch=owner.ownership.epoch,
        expected_revision=owner.conversation.revision,
        expected_generation=state.generation,
        paused=False,
    )
    owner.clock.now += timedelta(seconds=1)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        if automated:
            with pytest.raises(ValueError):
                send(owner, inputs(owner), automated=True)
            provider.assert_not_called()
        else:
            assert send(owner, inputs(owner)).status == "sent"
            assert provider.call_count == 1


def test_owner_epoch_change_rejects_earlier_rendered_history(owner):
    args = inputs(owner)
    owner.ownership.epoch += 1
    owner.ownership.save(update_fields=["epoch"])
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(ValueError):
        send(owner, args)
    provider.assert_not_called()


def test_local_claim_can_be_explicitly_retired_without_erasing_intent(owner):
    from apps.inbox.conversation_composer import retire_unattempted_conversation_draft

    args = inputs(owner)
    with (
        patch("apps.inbox.reply_dispatch.dispatch_reply", side_effect=ValueError("before dispatch")),
        pytest.raises(ValueError),
    ):
        send(owner, args)
    operation = SendOperation.objects.get()
    assert operation.status == "claimed" and operation.external_attempted_at is None
    state = composer_context(owner.conversation, authorization=owner.draft_authorization)
    assert state["can_retire_draft"]
    retire_unattempted_conversation_draft(
        message=owner.conversation,
        reply_id=operation.reply_id,
        expected_revision=state["composer_revision"],
        scope_token=state["scope_token"],
        authorization=owner.draft_authorization,
    )
    operation.refresh_from_db()
    assert operation.status == "superseded" and operation.reply.retired_at is not None
    assert operation.conversation_action_nonce and operation.reply.body == args["body"]
    owner.clock.now += timedelta(seconds=1)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted):
        assert send(owner, inputs(owner, body="Explicit successor")).status == "sent"


@pytest.mark.parametrize("changed", ["body", "quote", "generation", "human"])
def test_frozen_operation_fingerprint_rejects_payload_change(owner, changed):
    args = inputs(owner)
    with (
        patch("apps.inbox.reply_dispatch.dispatch_reply", side_effect=ValueError("before dispatch")),
        pytest.raises(ValueError),
    ):
        send(owner, args)
    operation = SendOperation.objects.get()
    if changed == "human":
        operation.human_observed_at = None
        operation.save(update_fields=["human_observed_at"])
    else:
        reply = operation.reply
        field, value = {
            "body": ("body", "changed"),
            "quote": ("quote_platform_message_id", "different-mid"),
            "generation": ("conversation_incoming_generation", reply.conversation_incoming_generation + 1),
        }[changed]
        setattr(reply, field, value)
        if changed == "quote":
            # Mutate the in-memory pinned facts to test the payload hash without
            # asking the DB to admit a deliberately invalid quote constraint.
            operation.reply = reply
        else:
            reply.save(update_fields=[field])
            operation.refresh_from_db()
    with pytest.raises(ValueError):
        coordination._validate_payload(operation)


def test_api_cannot_supply_human_mode_or_server_observation_metadata():
    from pydantic import ValidationError

    from apps.inbox.composer_surfaces import ConversationDraftInput

    base = {"body": "Synthetic", "action_nonce": str(uuid4()), "scope_token": "scope", "expected_revision": 0}
    for extra in ({"automated": False}, {"human": True}, {"human_observed_at": timezone.now().isoformat()}):
        with pytest.raises(ValidationError):
            ConversationDraftInput.model_validate({**base, **extra})


def test_manual_standard_response_does_not_require_human_agent_approval(owner):
    from apps.inbox.models import ConversationMessage

    ConversationMessage.objects.filter(conversation=owner.conversation).exclude(pk=owner.row.pk).delete()
    ConversationMessage.objects.filter(pk=owner.row.pk).update(occurred_at=owner.clock.now - timedelta(days=2))
    state = composer_context(
        owner.conversation,
        authorization=owner.draft_authorization,
        send_authorization=owner.authorization,
        observation_token=observe(owner),
    )
    assert state["send_availability"]["allowed"]
    assert state["can_save_draft"]
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        reply = send(owner, inputs(owner))
    provider.assert_called_once()
    assert reply.status == "sent"
    assert SendOperation.objects.get().status == "confirmed"


def test_manual_standard_reply_does_not_expire_at_local_24_hour_boundary(owner):
    from apps.inbox.models import ConversationMessage

    ConversationMessage.objects.filter(conversation=owner.conversation).exclude(pk=owner.row.pk).delete()
    ConversationMessage.objects.filter(pk=owner.row.pk).update(
        occurred_at=owner.clock.now - timedelta(hours=24) + timedelta(seconds=1)
    )
    external_requests = []

    def provider(*args, **kwargs):
        owner.clock.now += timedelta(seconds=2)
        kwargs["before_provider"]()
        external_requests.append(True)
        return "platform-accepted"

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=provider):
        assert send(owner, inputs(owner)).status == "sent"
    assert external_requests == [True]
    operation = SendOperation.objects.get()
    assert operation.status == "confirmed" and operation.attempt.outcome == "sent"


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_machine_owned_read_returns_actual_newest_page_then_send_is_idempotent(owner, transport):
    from django.test import Client

    from apps.api_keys.services import issue_api_key
    from apps.inbox.tests.test_composer_surfaces_recovery import payload, post, rpc

    issued = issue_api_key(
        workspace=owner.account.workspace,
        social_accounts=[owner.account],
        issued_by=owner.user,
        name="Synthetic owner contract",
        permissions=["use_inbox", "reply_from_inbox"],
    )
    owner.ownership.owner_scope = f"key:{issued.api_key.pk}"
    owner.ownership.save(update_fields=["owner_scope"])
    client = Client(HTTP_AUTHORIZATION=f"Bearer {issued.plaintext_token}")
    state = rpc(client, "get_inbox_conversation_composer", {"conversation_id": str(owner.conversation.pk)})
    assert state["observation_token"] and state["observed_conversation"]["messages"]
    assert state["send_availability"]["allowed"]
    value = payload(state, observation_token=state["observation_token"])
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        if transport == "rest":
            first = post(client, owner, "send", value)
            second = post(client, owner, "send", value)
            assert first.status_code == second.status_code == 200, (first.content, second.content)
            assert first.json()["id"] == second.json()["id"]
        else:
            args = {"conversation_id": str(owner.conversation.pk), **value}
            first = rpc(client, "send_inbox_conversation_reply", args)
            second = rpc(client, "send_inbox_conversation_reply", args)
            assert first["id"] == second["id"]
    assert provider.call_count == 1
    operation = SendOperation.objects.get()
    assert operation.human_observed_at is None and operation.status == "confirmed"


def test_owned_quote_uses_real_provider_payload_and_cancel_remains_default_none(owner):
    from apps.inbox.tests.test_native_quote_recovery import native_provider

    provider = native_provider("facebook")
    args = inputs(owner)
    args["quote_target_id"] = str(owner.row.pk)
    with (
        patch("apps.inbox.services.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
    ):
        quoted = send(owner, args)
    assert quoted.status == "sent"
    sent_payload = provider._request.call_args.kwargs["json"]
    assert sent_payload["reply_to"] == {"mid": owner.row.platform_message_id}
    operation = SendOperation.objects.get(reply=quoted)
    assert operation.conversation_action_nonce == quoted.action_nonce


def test_owned_action_schema_rollback_refuses_to_erase_nonce_evidence(owner):
    from importlib import import_module

    from django.apps import apps
    from django.db import connection

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted):
        sent = send(owner, inputs(owner))
    guard = import_module("apps.inbox.migrations.0019_owned_conversation_action").preserve_actions
    with connection.schema_editor(atomic=False) as editor, pytest.raises(RuntimeError, match="Preserve"):
        guard(apps, editor)
    assert SendOperation.objects.get(reply=sent).conversation_action_nonce == sent.action_nonce


@pytest.mark.parametrize("automated", [False, True])
def test_observed_human_target_may_be_newer_than_quiet_transport_target(owner, automated):
    older_target = owner.row.pk
    incoming(owner)
    # Unit proof of the boundary contract. Natural quiet backfill integration
    # separately verifies the producer advances only a proven current snapshot.
    ConversationWorkState.objects.filter(conversation=owner.conversation).update(latest_incoming_id=older_target)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        if automated:
            with pytest.raises(ValueError):
                send(owner, inputs(owner), automated=True)
            provider.assert_not_called()
        else:
            assert send(owner, inputs(owner)).status == "sent"
            assert provider.call_count == 1


@pytest.mark.parametrize("proof", ["receipt_native", "receipt_generation", "attempt_unsettled", "attempt_refused"])
def test_sequential_action_requires_exact_previous_own_receipt_proof(owner, proof):
    from apps.inbox.models import InboxReply

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted):
        sent = send(owner, inputs(owner))
    operation = SendOperation.objects.get(reply=sent)
    if proof == "receipt_native":
        InboxReply.objects.filter(pk=sent.pk).update(account_platform_id="other-native-account")
    elif proof == "receipt_generation":
        InboxReply.objects.filter(pk=sent.pk).update(connection_generation=uuid4())
    else:
        attempt = operation.attempt
        if proof == "attempt_unsettled":
            attempt.completed_at = None
            attempt.save(update_fields=["completed_at"])
        else:
            attempt.outcome = "not_sent"
            attempt.save(update_fields=["outcome"])
    owner.clock.now += timedelta(seconds=1)
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(ValueError):
        send(owner, inputs(owner, body="Next explicit action"))
    provider.assert_not_called()


def test_fresh_composer_scope_cannot_upgrade_an_observation_from_before_owner_epoch(owner):
    token = observe(owner)
    owner.ownership.epoch += 1
    owner.ownership.save(update_fields=["epoch"])
    args = inputs(owner, token=token)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        with pytest.raises(ValueError) as held:
            send(owner, args)
        assert getattr(held.value, "code", None) == "stale_observation"
        provider.assert_not_called()
        assert send(owner, inputs(owner)).status == "sent"
    assert provider.call_count == 1
