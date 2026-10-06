"""Real shared send boundary; all provider calls are synthetic and offline."""

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import DatabaseError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.api_keys.services import issue_api_key
from apps.inbox import dm_send_gate as gate
from apps.inbox.models import DMSendAttempt, DMSendControl, InboxMessage, InboxReply
from apps.inbox.services import (
    ReplyStateError,
    create_reply_draft,
    discard_reply_draft,
    send_reply,
    send_reply_now,
    update_reply_draft,
)
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.exceptions import APIError, ProviderError

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def enrolled(inbox_account, user, org_owner):
    member = WorkspaceMembership.objects.create(user=user, workspace=inbox_account.workspace, workspace_role="owner")
    control = gate.enroll_dm_send_control(
        account_id=inbox_account.pk,
        workspace_id=inbox_account.workspace_id,
        platform=inbox_account.platform,
        account_platform_id=inbox_account.account_platform_id,
    )
    control = gate.set_dm_send_paused(
        account_id=inbox_account.pk,
        workspace_id=inbox_account.workspace_id,
        paused=False,
        expected_epoch=control.epoch,
    )
    return SimpleNamespace(
        account=inbox_account,
        user=user,
        member=member,
        control=control,
        authorize=gate.session_send_authorization(user),
    )


def message_for(account, **kwargs):
    return InboxMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform_message_id=f"synthetic-{uuid.uuid4()}",
        message_type="dm",
        sender_name="Synthetic Customer",
        sender_handle="synthetic-peer",
        body="Synthetic question",
        extra=kwargs.pop("extra", {"conversation_type": "direct", "classification_reason": "participants_pair"}),
        received_at=kwargs.pop("received_at", timezone.now()),
        **kwargs,
    )


def draft_for(enrolled):
    return create_reply_draft(message=message_for(enrolled.account), body="Synthetic answer", author=enrolled.user)


def send(enrolled, reply, **kwargs):
    return send_reply_now(reply, actor=enrolled.user, automated=True, authorization=enrolled.authorize, **kwargs)


def pause(enrolled, paused=True):
    enrolled.control.refresh_from_db()
    return gate.set_dm_send_paused(
        account_id=enrolled.account.pk,
        workspace_id=enrolled.account.workspace_id,
        paused=paused,
        expected_epoch=enrolled.control.epoch,
    )


def test_default_has_no_enrollment_or_attempts(inbox_account):
    assert not DMSendControl.objects.exists()
    assert not DMSendAttempt.objects.exists()
    assert gate.dm_send_status(inbox_account)["enrolled"] is False


def test_status_is_factual_and_initial_enrollment_preserves_unknown_history(enrolled):
    state = gate.dm_send_status(enrolled.account)
    assert state["coverage_from"] is not None
    assert state["legacy_coverage_incomplete"] is True
    assert state["external_consumer_queue"] == "unobservable"
    assert state["drafts_are_queued_sends"] is False
    assert "safe_for_testing" not in state
    assert state["tracked_unresolved"] == 0
    assert state["pause_committed"] is False


def test_success_commits_attempt_before_dispatch_and_keeps_sent_id(enrolled):
    reply = draft_for(enrolled)

    def fake_send(*args, **kwargs):
        assert connection.in_atomic_block
        attempt = DMSendAttempt.objects.get(reply=reply)
        assert attempt.outcome == "unknown"
        assert InboxReply.objects.get(pk=reply.pk).status == "unknown"
        return "synthetic-outbound-1"

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=fake_send) as provider:
        send(enrolled, reply)
    assert provider.call_count == 1
    reply.refresh_from_db()
    assert reply.status == "sent" and reply.platform_reply_id == "synthetic-outbound-1"
    assert DMSendAttempt.objects.get().outcome == "sent"
    with pytest.raises(ReplyStateError), patch("apps.inbox.services._dispatch_to_platform") as again:
        send(enrolled, reply)
    again.assert_not_called()


@pytest.mark.parametrize(
    "outcome",
    [
        TimeoutError("secret"),
        ValueError("raw response secret"),
        ProviderError("secret"),
        NotImplementedError(),
        "",
        None,
        "x" * 256,
    ],
)
def test_ambiguous_outcome_holds_account_and_cannot_retry_edit_or_delete(enrolled, outcome):
    reply = draft_for(enrolled)
    patch_args = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
    with patch("apps.inbox.services._dispatch_to_platform", **patch_args), pytest.raises(gate.DMSendUnknownError):
        send(enrolled, reply)
    reply.refresh_from_db()
    assert reply.status == "unknown" and reply.platform_reply_id == "" and reply.sent_at is None
    assert "secret" not in reply.send_error
    assert DMSendAttempt.objects.get().outcome == "unknown"
    for operation in [
        lambda: send(enrolled, reply),
        lambda: update_reply_draft(reply, body="changed"),
        lambda: discard_reply_draft(reply),
    ]:
        with pytest.raises(ReplyStateError):
            operation()
    pause(enrolled)
    pause(enrolled, False)
    with pytest.raises(gate.DMSendGateError, match="unknown"):
        draft_for(enrolled)
    # An old independently stored draft still cannot bypass the account hold.
    newer = InboxReply.objects.create(inbox_message=message_for(enrolled.account), body="Synthetic old draft")
    with pytest.raises(gate.DMSendUnknownError), patch("apps.inbox.services._dispatch_to_platform") as provider:
        send(enrolled, newer)
    provider.assert_not_called()
    assert DMSendAttempt.objects.count() == 1
    with pytest.raises(ProtectedError):
        reply.delete()


@pytest.mark.parametrize("phase", ["reply", "attempt", "commit"])
def test_db_failure_after_provider_acceptance_preserves_unknown_marker(enrolled, phase):
    reply = draft_for(enrolled)
    original_reply_save = InboxReply.save
    original_attempt_save = DMSendAttempt.save
    original_commit = connection.commit
    commits = 0

    def reply_save(obj, *args, **kwargs):
        if obj.status == "sent":
            raise DatabaseError("synthetic persistence failure")
        return original_reply_save(obj, *args, **kwargs)

    def attempt_save(obj, *args, **kwargs):
        if obj.outcome == "sent":
            raise DatabaseError("synthetic attempt failure")
        return original_attempt_save(obj, *args, **kwargs)

    def commit():
        nonlocal commits
        commits += 1
        # Legacy routing read transaction, durable marker, dispatch transaction.
        if commits == 2:
            raise DatabaseError("synthetic dispatch commit failure")
        return original_commit()

    patcher = {
        "reply": patch.object(InboxReply, "save", reply_save),
        "attempt": patch.object(DMSendAttempt, "save", attempt_save),
        "commit": patch.object(connection, "commit", side_effect=commit),
    }[phase]
    with (
        patcher,
        patch("apps.inbox.services._dispatch_to_platform", return_value="accepted-before-db-failure") as provider,
        pytest.raises(gate.DMSendUnknownError),
    ):
        send(enrolled, reply)
    assert provider.call_count == 1
    reply.refresh_from_db()
    assert reply.status == "unknown" and reply.platform_reply_id == ""
    assert DMSendAttempt.objects.get().outcome == "unknown"


@pytest.mark.parametrize("crash_point", ["after_marker", "provider_entry"])
def test_process_crash_preserves_durable_marker(enrolled, crash_point):
    reply = draft_for(enrolled)
    original = gate._prepare_attempt

    def crash_after_marker(*args):
        original(*args)
        raise SystemExit("synthetic crash")

    target = (
        "apps.inbox.dm_send_gate._prepare_attempt"
        if crash_point == "after_marker"
        else "apps.inbox.services._dispatch_to_platform"
    )
    failure = crash_after_marker if crash_point == "after_marker" else SystemExit("synthetic crash")
    with patch(target, side_effect=failure), pytest.raises(SystemExit):
        send(enrolled, reply)
    assert DMSendAttempt.objects.get().outcome == "unknown"
    assert InboxReply.objects.get(pk=reply.pk).status == "unknown"
    with pytest.raises(gate.DMSendUnknownError), patch("apps.inbox.services._dispatch_to_platform") as provider:
        send(enrolled, reply)
    provider.assert_not_called()


def test_outer_transaction_and_manual_autocommit_rejected_before_network(enrolled):
    reply = draft_for(enrolled)
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        with transaction.atomic(), pytest.raises(gate.DMSendGateError, match="outermost"):
            send(enrolled, reply)
        connection.set_autocommit(False)
        try:
            with pytest.raises(gate.DMSendGateError, match="outermost"):
                send(enrolled, reply)
        finally:
            connection.rollback()
            connection.set_autocommit(True)
    provider.assert_not_called()
    assert not DMSendAttempt.objects.exists()


def test_missing_current_authority_is_closed(enrolled):
    with (
        pytest.raises(gate.DMSendGateError, match="authorization"),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
    ):
        send_reply_now(draft_for(enrolled), actor=enrolled.user)
    provider.assert_not_called()


@pytest.mark.parametrize("mutation", ["pause", "body", "target", "member", "disconnected"])
def test_recheck_after_committed_marker_proves_no_dispatch(enrolled, mutation):
    reply = draft_for(enrolled)
    original = gate._prepare_attempt

    def mutate(*args):
        attempt = original(*args)
        if mutation == "pause":
            pause(enrolled)
        elif mutation == "body":
            InboxReply.objects.filter(pk=reply.pk).update(body="changed by stale caller")
        elif mutation == "target":
            InboxMessage.objects.filter(pk=reply.inbox_message_id).update(platform_message_id="changed-target")
        elif mutation == "member":
            enrolled.member.delete()
        else:
            SocialAccount.objects.filter(pk=enrolled.account.pk).update(connection_status="disconnected")
        return attempt

    with (
        patch.object(gate, "_prepare_attempt", side_effect=mutate),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(ReplyStateError),
    ):
        send(enrolled, reply)
    provider.assert_not_called()
    assert DMSendAttempt.objects.get().outcome == "not_sent"
    assert InboxReply.objects.get(pk=reply.pk).status == "failed"


@pytest.mark.parametrize("platform,name", [("facebook", "Facebook"), ("instagram_login", "Instagram (Direct)")])
def test_explicit_401_refusal_is_not_sent_and_can_retry_same_body(enrolled, platform, name):
    SocialAccount.objects.filter(pk=enrolled.account.pk).update(platform=platform)
    DMSendControl.objects.filter(pk=enrolled.control.pk).update(platform=platform)
    enrolled.account.refresh_from_db()
    reply = draft_for(enrolled)
    with (
        patch(
            "apps.inbox.services._dispatch_to_platform", side_effect=APIError("hidden", platform=name, status_code=401)
        ),
        pytest.raises(gate.DMSendGateError, match="provider_refused"),
    ):
        send(enrolled, reply)
    assert DMSendAttempt.objects.get().outcome == "not_sent"
    with patch("apps.inbox.services._dispatch_to_platform", return_value="synthetic-success"):
        send(enrolled, reply)
    assert list(DMSendAttempt.objects.order_by("created_at").values_list("outcome", flat=True)) == ["not_sent", "sent"]


@pytest.mark.parametrize("field", ["received_at", "created_at", "draft"])
def test_resume_rejects_old_targets_even_for_new_delayed_drafts(enrolled, field):
    old = enrolled.control.resume_cutoff - timedelta(seconds=1)
    pause(enrolled)
    pause(enrolled, False)
    reply = draft_for(enrolled)
    if field == "draft":
        InboxReply.objects.filter(pk=reply.pk).update(created_at=old)
    else:
        InboxMessage.objects.filter(pk=reply.inbox_message_id).update(**{field: old})
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(gate.DMSendGateError, match="predates resume"),
    ):
        send(enrolled, reply)
    provider.assert_not_called()
    assert not DMSendAttempt.objects.exists()


@pytest.mark.parametrize("field", ["platform", "account_platform_id", "workspace"])
def test_changed_identity_cannot_fall_back_to_legacy(enrolled, field):
    reply = draft_for(enrolled)
    value = "unsupported" if field == "platform" else "different-native-id"
    if field == "workspace":
        other = Workspace.objects.create(
            name="Synthetic other workspace", organization=enrolled.account.workspace.organization
        )
        value = other.pk
        InboxMessage.objects.filter(pk=reply.inbox_message_id).update(workspace=other)
    SocialAccount.objects.filter(pk=enrolled.account.pk).update(**{field: value})
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(ReplyStateError):
        send(enrolled, reply)
    provider.assert_not_called()
    enrolled.account.refresh_from_db()
    with pytest.raises(gate.DMSendGateError):
        gate.dm_send_status(enrolled.account)


def test_flags_do_not_release_paused_or_unknown_account(enrolled, settings):
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    settings.INBOX_REPLY_COORDINATION_ENABLED = False
    pause(enrolled)
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(gate.DMSendGateError, match="paused"),
    ):
        send(enrolled, draft_for(enrolled))
    provider.assert_not_called()


def test_optional_projection_and_sla_failure_keep_acceptance(enrolled):
    reply = draft_for(enrolled)
    with (
        patch("apps.inbox.services._dispatch_to_platform", return_value="synthetic-outbound"),
        patch("apps.inbox.conversations.record_reply", side_effect=DatabaseError("projection")),
        patch("apps.inbox.services._apply_post_send_side_effects", side_effect=DatabaseError("sla")),
    ):
        send(enrolled, reply)
    reply.refresh_from_db()
    assert reply.status == "sent"
    assert DMSendAttempt.objects.get().outcome == "sent"


def test_classic_unknown_keeps_row_and_cross_type_mutation_cannot_retry(enrolled):
    message = message_for(enrolled.account)
    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=TimeoutError),
        pytest.raises(gate.DMSendUnknownError),
    ):
        send_reply(message=message, body="Synthetic answer", author=enrolled.user, authorization=enrolled.authorize)
    reply = InboxReply.objects.get()
    assert reply.status == "unknown"
    InboxMessage.objects.filter(pk=message.pk).update(message_type="comment")
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(gate.DMSendUnknownError):
        send(enrolled, reply)
    provider.assert_not_called()


class SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


def surface_send(enrolled, surface):
    message = message_for(enrolled.account)
    reply = (
        create_reply_draft(message=message, body="Synthetic answer", author=enrolled.user)
        if surface.endswith("draft")
        else None
    )
    if surface.startswith("ui"):
        client = Client()
        client.force_login(enrolled.user)
        path = f"replies/{reply.pk}/send/" if reply else f"{message.pk}/reply/"
        return client.post(f"/workspace/{enrolled.account.workspace_id}/inbox/{path}", {"body": "Synthetic answer"})
    issued = issue_api_key(
        workspace=enrolled.account.workspace,
        social_accounts=[enrolled.account],
        issued_by=enrolled.user,
        name="synthetic-gate-test",
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
@pytest.mark.parametrize("state", ["paused", "unknown", "success"])
def test_all_actual_surfaces_use_common_gate(enrolled, surface, state):
    if state == "paused":
        pause(enrolled)
    with patch(
        "apps.inbox.services._dispatch_to_platform",
        **(
            {"side_effect": TimeoutError("secret response")}
            if state == "unknown"
            else {"return_value": "synthetic-outbound"}
        ),
    ) as provider:
        response = surface_send(enrolled, surface)
    assert response.status_code in (200, 201, 409), response.content
    if state == "paused":
        provider.assert_not_called()
        assert b"paused" in response.content
    elif state == "unknown":
        assert provider.call_count == 1
        assert b"unknown" in response.content
        assert b"do not retry" in response.content
        assert b"Reply not sent" not in response.content and b"Nothing was sent" not in response.content
        assert b"secret response" not in response.content
        assert InboxReply.objects.get().status == "unknown"
    else:
        assert provider.call_count == 1
        assert InboxReply.objects.get().status == "sent"


@pytest.mark.parametrize("mutation", ["revoke", "allowlist", "permission", "member", "inactive", "archived"])
def test_current_key_authority_rechecked_after_marker(enrolled, mutation):
    issued = issue_api_key(
        workspace=enrolled.account.workspace,
        social_accounts=[enrolled.account],
        issued_by=enrolled.user,
        name="synthetic",
        permissions=["reply_from_inbox"],
    )
    authorization = gate.key_send_authorization(issued.api_key)
    reply = draft_for(enrolled)
    original = gate._prepare_attempt

    def mutate(*args):
        attempt = original(*args)
        key = issued.api_key
        if mutation == "revoke":
            key.revoked_at = timezone.now()
            key.save(update_fields=["revoked_at"])
        elif mutation == "allowlist":
            key.social_accounts.clear()
        elif mutation == "permission":
            key.permissions = []
            key.save(update_fields=["permissions"])
        elif mutation == "member":
            enrolled.member.delete()
        elif mutation == "inactive":
            enrolled.user.is_active = False
            enrolled.user.save(update_fields=["is_active"])
        else:
            Workspace.objects.filter(pk=enrolled.account.workspace_id).update(is_archived=True)
        return attempt

    with (
        patch.object(gate, "_prepare_attempt", side_effect=mutate),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(ReplyStateError),
    ):
        send_reply_now(reply, actor=enrolled.user, automated=True, authorization=authorization)
    provider.assert_not_called()
    assert DMSendAttempt.objects.get().outcome == "not_sent"


def test_unknown_does_not_block_other_account(enrolled):
    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=TimeoutError),
        pytest.raises(gate.DMSendUnknownError),
    ):
        send(enrolled, draft_for(enrolled))
    other = SocialAccount.objects.create(
        workspace=enrolled.account.workspace,
        platform="facebook",
        account_platform_id="synthetic-other",
        account_name="Other",
    )
    reply = create_reply_draft(message=message_for(other), body="Synthetic other")
    with patch("apps.inbox.services._dispatch_to_platform", return_value="other-outbound"):
        send_reply_now(reply, actor=enrolled.user, authorization=enrolled.authorize)
    assert reply.status == "sent"


def test_status_view_requires_current_session_membership_and_is_readonly(enrolled, client):
    url = f"/workspace/{enrolled.account.workspace_id}/inbox/accounts/{enrolled.account.pk}/dm-send-status/"
    assert client.get(url).status_code == 302
    client.force_login(enrolled.user)
    assert client.get(url).json()["tracked_unresolved"] == 0
    assert client.post(url).status_code == 405
    enrolled.member.delete()
    assert client.get(url).status_code == 403


def test_disconnect_is_blocked_before_any_external_or_delete_effect(enrolled, client):
    client.force_login(enrolled.user)
    with (
        patch("apps.social_accounts.views.unsubscribe_account_webhooks") as unsubscribe,
        patch("providers.get_provider") as provider,
        patch.object(SocialAccount, "delete") as delete,
    ):
        response = client.post(
            reverse(
                "social_accounts:disconnect",
                kwargs={"workspace_id": enrolled.account.workspace_id, "account_id": enrolled.account.pk},
            )
        )
    assert response.status_code == 409, response.content
    unsubscribe.assert_not_called()
    provider.assert_not_called()
    delete.assert_not_called()


@pytest.mark.parametrize("enroll_second", [False, True])
def test_original_scoped_account_cannot_be_reassigned_before_service(enrolled, enroll_second):
    reply = draft_for(enrolled)
    other = SocialAccount.objects.create(
        workspace=enrolled.account.workspace,
        platform="facebook",
        account_platform_id="synthetic-other",
        account_name="Other",
    )
    if enroll_second:
        control = gate.enroll_dm_send_control(
            account_id=other.pk,
            workspace_id=other.workspace_id,
            platform=other.platform,
            account_platform_id=other.account_platform_id,
        )
        gate.set_dm_send_paused(
            account_id=other.pk, workspace_id=other.workspace_id, paused=False, expected_epoch=control.epoch
        )
    # The caller already looked up and authorized reply->message->account A.
    assert reply.inbox_message.social_account_id == enrolled.account.pk
    InboxMessage.objects.filter(pk=reply.inbox_message_id).update(social_account=other)
    with (
        pytest.raises(gate.DMSendGateError, match="originally selected"),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
    ):
        send(enrolled, reply)
    provider.assert_not_called()
    assert not DMSendAttempt.objects.exists()


@pytest.mark.parametrize("mutation", ["delete", "expire", "scope"])
def test_oauth_token_current_authority_rechecked_before_dispatch(enrolled, mutation):
    from oauth2_provider.models import get_access_token_model

    from apps.api.auth import _resolve_oauth_actor
    from apps.mcp.tests.test_oauth_auth import _mint_oauth_token

    enrolled.user.last_workspace_id = enrolled.account.workspace_id
    enrolled.user.save(update_fields=["last_workspace_id"])
    token = _mint_oauth_token(enrolled.user)
    actor = _resolve_oauth_actor(token)
    request = RequestFactory().post("/api/v1/mcp/", HTTP_AUTHORIZATION=f"Bearer {token}")
    authorization = gate.key_send_authorization(actor, request)
    original = gate._prepare_attempt

    def mutate(*args):
        attempt = original(*args)
        tokens = get_access_token_model().objects.filter(user=enrolled.user)
        if mutation == "delete":
            tokens.delete()
        elif mutation == "expire":
            tokens.update(expires=timezone.now() - timedelta(seconds=1))
        else:
            tokens.update(scope="unrelated")
        return attempt

    with (
        patch.object(gate, "_prepare_attempt", side_effect=mutate),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(gate.DMSendGateError, match="permission"),
    ):
        send_reply_now(draft_for(enrolled), actor=enrolled.user, automated=True, authorization=authorization)
    provider.assert_not_called()
    assert DMSendAttempt.objects.get().outcome == "not_sent"


def test_custom_role_from_other_organization_cannot_authorize(enrolled, organization):
    from apps.members.models import CustomRole
    from apps.organizations.models import Organization

    foreign = Organization.objects.create(name="Synthetic foreign organization")
    role = CustomRole.objects.create(organization=foreign, name="Foreign", permissions={"reply_from_inbox": True})
    enrolled.member.custom_role = role
    enrolled.member.save(update_fields=["custom_role"])
    with (
        pytest.raises(gate.DMSendGateError, match="permission"),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
    ):
        send(enrolled, draft_for(enrolled))
    provider.assert_not_called()


def test_pause_needs_outermost_commit_and_stale_epoch_is_not_acknowledged(enrolled):
    with transaction.atomic(), pytest.raises(gate.DMSendGateError, match="outermost"):
        pause(enrolled)
    assert not gate.dm_send_status(enrolled.account)["pause_committed"]
    with pytest.raises(gate.DMSendGateError, match="changed"):
        gate.set_dm_send_paused(
            account_id=enrolled.account.pk, workspace_id=enrolled.account.workspace_id, paused=True, expected_epoch=0
        )
    assert not gate.dm_send_status(enrolled.account)["pause_committed"]


def test_future_received_timestamp_is_not_dispatchable(enrolled):
    message = message_for(enrolled.account, received_at=timezone.now() + timedelta(seconds=10))
    reply = create_reply_draft(message=message, body="Synthetic answer")
    with (
        pytest.raises(gate.DMSendGateError, match="invalid timestamp"),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
    ):
        send(enrolled, reply)
    provider.assert_not_called()


def test_availability_never_advertises_pre_resume_target_as_sendable(enrolled):
    from apps.inbox.services import reply_send_availability

    reply = draft_for(enrolled)
    pause(enrolled)
    pause(enrolled, False)
    assert reply_send_availability(reply.inbox_message, reply=reply)["code"] == "old_target"
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(gate.DMSendGateError, match="predates resume"),
    ):
        send(enrolled, reply)
    provider.assert_not_called()


def test_unknown_reply_ui_immediately_removes_retry_and_discard(enrolled):
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=TimeoutError):
        response = surface_send(enrolled, "ui_draft")
    reply = InboxReply.objects.get()
    assert response.status_code == 200 and response["HX-Reply-Failed"] == "1"
    assert f"replies/{reply.pk}/send/".encode() not in response.content
    assert f"replies/{reply.pk}/discard/".encode() not in response.content
