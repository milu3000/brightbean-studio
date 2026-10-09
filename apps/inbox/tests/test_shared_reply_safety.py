"""Common DM dispatch uses existing receipts without enabling any capture."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, get_ident
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.utils import timezone

from apps.inbox import reply_safety, services
from apps.inbox.dm_send_gate import DMSendGateError, session_send_authorization
from apps.inbox.models import DMSendAttempt, DMSendControl, InboxMessage, InboxReply
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from providers.exceptions import APIError

pytestmark = pytest.mark.django_db(transaction=True)


def canonical(dm):
    from apps.inbox.models import ConversationMessage, InboxConversation

    conversation = InboxConversation.objects.create(
        workspace=dm.account.workspace,
        social_account=dm.account,
        platform=dm.account.platform,
        platform_conversation_id="conversation-1",
        peer_id="peer-1",
        identity_kind="platform",
        conversation_type="direct",
        classification_reason="participants_pair",
    )
    return ConversationMessage.objects.create(
        workspace=dm.account.workspace,
        social_account=dm.account,
        platform=dm.account.platform,
        platform_message_id=dm.message.platform_message_id,
        direction="inbound",
        sender_id="peer-1",
        recipient_id=dm.account.account_platform_id,
        legacy_message=dm.message,
        conversation=conversation,
        conversation_type="direct",
        classification_reason="participants_pair",
    )


@pytest.mark.parametrize(
    "mutation", ["row_platform", "conversation_platform", "conversation_workspace", "ambiguous_peer", "peer"]
)
def test_canonical_direct_evidence_must_match_current_account_scope(dm, mutation):
    from apps.workspaces.models import Workspace

    row = canonical(dm)
    if mutation == "row_platform":
        row.platform = "instagram_login"
        row.save(update_fields=["platform"])
    else:
        conversation = row.conversation
        if mutation == "conversation_platform":
            conversation.platform = "instagram_login"
        elif mutation == "conversation_workspace":
            conversation.workspace = Workspace.objects.create(
                name="Other", organization=dm.account.workspace.organization
            )
        elif mutation == "ambiguous_peer":
            conversation.peer_ambiguous = True
        else:
            conversation.peer_id = "wrong-peer"
        conversation.save()
    reply = draft(dm)
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError, match="no longer verifies"),
    ):
        send(dm, reply)
    provider.assert_not_called()


@pytest.mark.parametrize("kind,reason", [("group", "participants_group"), ("unknown", "identity_conflict")])
def test_old_canonical_direct_cannot_hide_current_group_or_conflict_summary(dm, kind, reason):
    canonical(dm)
    dm.message.extra = {**dm.message.extra, "conversation_type": kind, "classification_reason": reason}
    dm.message.save(update_fields=["extra"])
    reply = draft(dm)
    assert services.reply_send_availability(dm.message, reply=reply)["code"] == "not_verified_direct"
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError, match="one-to-one"),
    ):
        send(dm, reply)
    provider.assert_not_called()


def test_legacy_failed_receipt_cannot_retry_edit_discard_or_spawn_another_intent(dm):
    reply = draft(dm)
    InboxReply.objects.filter(pk=reply.pk).update(status="failed", send_error="Old generic provider exception")
    reply.refresh_from_db()
    original = InboxReply.objects.values().get(pk=reply.pk)
    assert reply_safety.is_unresolved_reply(reply)
    assert services.reply_send_availability(dm.message, reply=reply)["code"] == "legacy_outcome_unverified"
    for action in (
        lambda: send(dm, reply),
        lambda: services.update_reply_draft(reply, body="Changed"),
        lambda: services.discard_reply_draft(reply),
        lambda: draft(dm),
    ):
        with (
            patch("apps.inbox.services._dispatch_to_platform") as provider,
            pytest.raises(services.ReplyStateError, match="no verified delivery"),
        ):
            action()
        provider.assert_not_called()
    assert InboxReply.objects.values().get(pk=reply.pk) == original


def test_current_service_definitive_failure_proof_survives_draft_edit(dm):
    reply = draft(dm)
    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=NotImplementedError),
        pytest.raises(DMSendGateError),
    ):
        send(dm, reply)
    reply.refresh_from_db()
    assert reply.status == "failed" and not reply_safety.is_unresolved_reply(reply)
    proof = reply.send_error
    assert reply.not_sent_verified
    services.update_reply_draft(reply, body="Reviewed answer")
    assert reply.send_error == proof
    assert reply.not_sent_verified
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted):
        send(dm, reply)
    assert reply.status == "sent" and reply.body == "Reviewed answer"


def test_account_workspace_move_does_not_release_earlier_unknown(dm):
    from apps.workspaces.models import Workspace

    first = draft(dm)
    InboxReply.objects.filter(pk=first.pk).update(status="unknown")
    workspace = Workspace.objects.create(name="Moved", organization=dm.account.workspace.organization)
    SocialAccount.objects.filter(pk=dm.account.pk).update(workspace=workspace)
    dm.account.refresh_from_db()
    WorkspaceMembership.objects.create(user=dm.user, workspace=workspace, workspace_role="owner")
    newer = InboxMessage.objects.create(
        workspace=workspace,
        social_account=dm.account,
        platform_message_id="new-scope-incoming",
        message_type="dm",
        sender_handle="other-peer",
        sender_name="Other",
        body="New question",
        received_at=timezone.now(),
        extra={"conversation_type": "direct", "classification_reason": "participants_pair"},
    )
    alternate = InboxReply.objects.create(inbox_message=newer, body="New answer")
    assert services.reply_send_availability(newer, reply=alternate)["code"] == "outcome_unknown"
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError, match="unknown"),
    ):
        send(dm, alternate)
    provider.assert_not_called()


@pytest.fixture
def dm(inbox_account, user, org_owner):
    member = WorkspaceMembership.objects.create(user=user, workspace=inbox_account.workspace, workspace_role="owner")
    message = InboxMessage.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform_message_id="incoming-1",
        message_type="dm",
        sender_name="Synthetic customer",
        sender_handle="peer-1",
        body="Question",
        received_at=timezone.now(),
        extra={
            "conversation_id": "conversation-1",
            "conversation_type": "direct",
            "classification_reason": "participants_pair",
        },
    )
    return SimpleNamespace(
        message=message, account=inbox_account, user=user, member=member, authorization=session_send_authorization(user)
    )


def draft(dm, body="Answer"):
    return services.create_reply_draft(message=dm.message, body=body, author=dm.user)


def send(dm, reply, **kwargs):
    return services.send_reply_now(reply, actor=dm.user, authorization=dm.authorization, automated=True, **kwargs)


def accepted(*args, **kwargs):
    kwargs["before_provider"]()
    return "outbound-1"


@pytest.mark.parametrize("extra", [{}, {"conversation_type": "group", "classification_reason": "participants_group"}])
def test_unverified_and_group_targets_refused_without_provider_or_receipt(dm, extra):
    dm.message.extra = extra
    dm.message.save(update_fields=["extra"])
    reply = draft(dm)
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError, match="one-to-one"),
    ):
        send(dm, reply)
    provider.assert_not_called()
    reply.refresh_from_db()
    assert reply.status == "draft"
    assert not DMSendControl.objects.exists()
    assert not DMSendAttempt.objects.exists()


@pytest.mark.parametrize("platform", ["facebook", "instagram_login"])
def test_direct_summary_dispatches_without_enrollment_or_capture(dm, platform, settings):
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    dm.account.platform = platform
    dm.account.save(update_fields=["platform"])
    reply = draft(dm)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        send(dm, reply)
    assert provider.call_count == 1
    assert reply.status == "sent"
    assert reply.platform_reply_id == "outbound-1"
    assert not DMSendControl.objects.exists()
    assert not DMSendAttempt.objects.exists()


def test_raw_participant_evidence_must_agree_with_recipient(dm):
    dm.message.extra = {
        "participant_ids": [dm.account.account_platform_id, "peer-1"],
        "sender_id": "peer-1",
        "recipient_id": "another-peer",
    }
    dm.message.save(update_fields=["extra"])
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError, match="recipient"),
    ):
        send(dm, draft(dm))
    provider.assert_not_called()


@pytest.mark.parametrize(
    "marker",
    [
        {"is_deleted": True},
        {"message": {"is_deleted": True}},
        {"is_echo": True},
        {"is_self": True},
        {"direction": "outbound"},
        {"message": {"is_echo": True}},
    ],
)
def test_uncaptured_deleted_or_outbound_target_cannot_send(dm, marker):
    dm.message.extra.update(marker)
    dm.message.save(update_fields=["extra"])
    reply = draft(dm)
    assert services.reply_send_availability(dm.message, reply=reply)["code"] == "not_incoming"
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError, match="not an incoming"),
    ):
        send(dm, reply)
    provider.assert_not_called()
    assert InboxReply.objects.get(pk=reply.pk).status == "draft"


@pytest.mark.parametrize("kind,reason", [("group", "participants_group"), ("unknown", "identity_conflict")])
def test_conflicting_native_thread_evidence_cannot_be_bypassed_with_newest_direct_row(dm, kind, reason):
    prior = InboxMessage.objects.get(pk=dm.message.pk)
    prior.pk = None
    prior.platform_message_id = "conflicting-prior"
    prior.extra = {**prior.extra, "conversation_type": kind, "classification_reason": reason}
    prior.save()
    reply = draft(dm)
    assert services.reply_send_availability(dm.message, reply=reply)["code"] == "thread_identity_conflict"
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError, match="conflicting"),
    ):
        send(dm, reply)
    provider.assert_not_called()


def test_unknown_cannot_escape_via_target_type_change_and_another_draft(dm):
    first = draft(dm)
    InboxReply.objects.filter(pk=first.pk).update(status="unknown")
    InboxMessage.objects.filter(pk=dm.message.pk).update(message_type="comment")
    dm.message.refresh_from_db()
    assert services.reply_send_availability(dm.message)["code"] == "outcome_unknown"
    with pytest.raises(services.ReplyStateError, match="unknown"):
        draft(dm, "Another reply")
    alternate = InboxReply.objects.create(inbox_message=dm.message, body="Historic duplicate")
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(services.ReplyStateError, match="unknown"),
    ):
        send(dm, alternate)
    provider.assert_not_called()
    assert InboxReply.objects.get(pk=first.pk).status == "unknown"


@pytest.mark.parametrize("surface", ["ui_classic", "ui_draft", "rest_new", "rest_draft", "mcp_new", "mcp_draft"])
@pytest.mark.parametrize("state", ["group", "unknown_type", "uncertain_delivery"])
def test_actual_ui_rest_mcp_surfaces_share_direct_evidence_and_unknown_receipt_rules(dm, surface, state):
    from apps.inbox.tests.test_dm_send_gate import surface_send

    if state in {"group", "unknown_type"}:
        dm.message.extra = (
            {"conversation_type": "group", "classification_reason": "participants_group"} if state == "group" else {}
        )
        dm.message.save(update_fields=["extra"])

    def uncertain(*args, **kwargs):
        kwargs["before_provider"]()
        raise TimeoutError("Private provider diagnostic")

    with (
        patch("apps.inbox.tests.test_dm_send_gate.message_for", return_value=dm.message),
        patch("apps.inbox.services._dispatch_to_platform", side_effect=uncertain) as provider,
    ):
        response = surface_send(dm, surface)
    assert response.status_code in {200, 409}, response.content
    if state == "uncertain_delivery":
        assert provider.call_count == 1
        assert InboxReply.objects.get().status == "unknown"
        assert b"unknown" in response.content
        assert b"do not retry" in response.content
    else:
        provider.assert_not_called()
        assert InboxReply.objects.get().status == "draft"
        assert b"one-to-one" in response.content
    assert b"Private provider diagnostic" not in response.content
    assert not DMSendControl.objects.exists()
    assert not DMSendAttempt.objects.exists()


def test_matching_create_reuses_one_draft_changed_text_requires_explicit_edit(dm):
    first = draft(dm)
    assert draft(dm).pk == first.pk
    with pytest.raises(services.ReplyStateError, match="Open and edit"):
        draft(dm, "Different answer")
    assert InboxReply.objects.count() == 1
    first.refresh_from_db()
    assert first.body == "Answer"


@pytest.mark.parametrize("status", ["sent", "unknown"])
def test_terminal_receipt_blocks_alternate_draft_and_send(dm, status):
    first = draft(dm)
    InboxReply.objects.filter(pk=first.pk).update(status=status)
    with pytest.raises(services.ReplyStateError):
        draft(dm)
    alternate = InboxReply.objects.create(inbox_message=dm.message, body="Other intent")
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(services.ReplyStateError):
        send(dm, alternate)
    provider.assert_not_called()
    assert InboxReply.objects.get(pk=first.pk).status == status


def test_unknown_holds_account_even_when_native_thread_and_peer_change(dm):
    reply = draft(dm)
    InboxReply.objects.filter(pk=reply.pk).update(status="unknown")
    newer = InboxMessage.objects.get(pk=dm.message.pk)
    newer.pk = None
    newer.platform_message_id = "incoming-2"
    newer.save()
    assert services.reply_send_availability(newer)["code"] == "outcome_unknown"
    newer.extra = {**newer.extra, "conversation_id": "different-native-thread"}
    newer.save(update_fields=["extra"])
    assert services.reply_send_availability(newer)["code"] == "outcome_unknown"
    newer.sender_handle = "other-verified-peer"
    newer.save(update_fields=["sender_handle"])
    assert services.reply_send_availability(newer)["code"] == "outcome_unknown"


def test_unknown_hold_survives_loss_of_classification_and_native_metadata(dm):
    first = draft(dm)
    InboxReply.objects.filter(pk=first.pk).update(status="unknown")
    newer = InboxMessage.objects.get(pk=dm.message.pk)
    newer.pk = None
    newer.platform_message_id = "incoming-2"
    newer.extra.pop("conversation_id")
    newer.save()
    assert services.reply_send_availability(newer)["code"] == "outcome_unknown"
    InboxMessage.objects.filter(pk=dm.message.pk).update(extra={})
    assert services.reply_send_availability(newer)["code"] == "outcome_unknown"


def test_unknown_never_editable_discardable_or_retryable(dm):
    reply = draft(dm)

    def timeout(*args, **kwargs):
        kwargs["before_provider"]()
        raise TimeoutError("Provider may have accepted")

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=timeout),
        pytest.raises(DMSendGateError, match="unknown"),
    ):
        send(dm, reply)
    reply.refresh_from_db()
    assert reply.status == "unknown"
    assert reply.platform_reply_id == ""
    for action in (
        lambda: services.update_reply_draft(reply, body="New"),
        lambda: services.discard_reply_draft(reply),
        lambda: send(dm, reply),
    ):
        with pytest.raises(services.ReplyStateError):
            action()


@pytest.mark.parametrize("result", [None, "", " " * 3, "x" * 256])
def test_missing_or_invalid_provider_receipt_remains_unknown(dm, result):
    reply = draft(dm)

    def provider(*args, **kwargs):
        kwargs["before_provider"]()
        return result

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=provider),
        pytest.raises(DMSendGateError, match="unknown"),
    ):
        send(dm, reply)
    assert InboxReply.objects.get(pk=reply.pk).status == "unknown"


def test_worker_crash_after_marker_keeps_unknown_committed(dm):
    reply = draft(dm)
    original = reply_safety._prepare_receipt

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit("Worker stopped")

    with patch.object(reply_safety, "_prepare_receipt", side_effect=crash), pytest.raises(SystemExit):
        send(dm, reply)
    assert InboxReply.objects.get(pk=reply.pk).status == "unknown"


def test_receipt_write_failure_after_acceptance_cannot_erase_unknown(dm):
    reply = draft(dm)
    original = InboxReply.save

    def failing_save(self, *args, **kwargs):
        if self.status == "sent":
            raise DatabaseError("Simulated receipt failure")
        return original(self, *args, **kwargs)

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted),
        patch.object(InboxReply, "save", failing_save),
        pytest.raises(DMSendGateError, match="unknown"),
    ):
        send(dm, reply)
    assert InboxReply.objects.get(pk=reply.pk).status == "unknown"


def test_outer_transaction_cannot_hide_pre_network_marker(dm):
    reply = draft(dm)
    with (
        transaction.atomic(),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError, match="outermost"),
    ):
        send(dm, reply)
    provider.assert_not_called()
    assert InboxReply.objects.get(pk=reply.pk).status == "draft"


def test_explicit_auth_refusal_is_failed_and_same_receipt_can_retry(dm):
    reply = draft(dm)

    def refused(*args, **kwargs):
        kwargs["before_provider"]()
        raise APIError("Denied", platform="Facebook", status_code=403)

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=refused), pytest.raises(DMSendGateError):
        send(dm, reply)
    assert InboxReply.objects.get(pk=reply.pk).status == "failed"
    assert draft(dm).pk == reply.pk
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted):
        send(dm, reply)
    assert InboxReply.objects.count() == 1
    assert reply.status == "sent"


def test_revoked_authorization_rechecked_immediately_before_provider(dm):
    reply = draft(dm)

    def resolve_then_enter(*args, **kwargs):
        dm.member.delete()
        kwargs["before_provider"]()
        pytest.fail("Provider entered after authorization was revoked")

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=resolve_then_enter),
        pytest.raises(DMSendGateError),
    ):
        send(dm, reply)
    assert InboxReply.objects.get(pk=reply.pk).status == "failed"


def test_stale_target_or_account_cannot_dispatch(dm):
    reply = draft(dm)
    _ = reply.inbox_message.social_account
    SocialAccount.objects.filter(pk=dm.account.pk).update(account_platform_id="changed-identity")
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError, match="changed"),
    ):
        send(dm, reply)
    provider.assert_not_called()


def test_convenience_send_retains_failed_and_unknown_receipts(dm):
    def timeout(*args, **kwargs):
        kwargs["before_provider"]()
        raise TimeoutError()

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=timeout), pytest.raises(DMSendGateError):
        services.send_reply(message=dm.message, body="Answer", author=dm.user, authorization=dm.authorization)
    assert InboxReply.objects.get().status == "unknown"


def test_read_only_availability_does_not_mutate_or_enroll(dm):
    before = list(InboxMessage.objects.values())
    assert services.reply_send_availability(dm.message)["allowed"] is True
    reply = draft(dm)
    assert services.reply_send_availability(dm.message)["code"] == "existing_draft"
    assert services.reply_send_availability(dm.message, reply=reply)["allowed"] is True
    assert list(InboxMessage.objects.values()) == before
    assert not DMSendControl.objects.exists()


def test_unenrolled_status_reports_shared_uncertainty_truthfully(dm):
    from apps.inbox.dm_send_gate import dm_send_status

    first = draft(dm)
    assert dm_send_status(dm.account)["shared_unknown_reply_count"] == 0
    InboxReply.objects.filter(pk=first.pk).update(status="unknown")
    state = dm_send_status(dm.account)
    assert state["enrolled"] is False
    assert state["shared_unknown_reply_count"] == 1
    assert "tracked_unresolved" not in state


@pytest.mark.parametrize("enabled", [False, "true", 1, None])
def test_emergency_stop_is_exact_boolean_and_preserves_existing_unknown(dm, settings, enabled):
    reply = draft(dm)
    settings.INBOX_DM_SENDS_ENABLED = enabled
    assert services.reply_send_availability(dm.message, reply=reply)["code"] == "sending_paused"
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError, match="temporarily paused"),
    ):
        send(dm, reply)
    provider.assert_not_called()
    assert InboxReply.objects.get(pk=reply.pk).status == "draft"
    InboxReply.objects.filter(pk=reply.pk).update(status="unknown")
    with pytest.raises(DMSendGateError, match="unknown"):
        send(dm, reply)
    assert InboxReply.objects.get(pk=reply.pk).status == "unknown"


def test_emergency_stop_rechecked_after_credentials_before_http(dm, settings):
    reply = draft(dm)

    def stopped(*args, **kwargs):
        settings.INBOX_DM_SENDS_ENABLED = False
        kwargs["before_provider"]()
        pytest.fail("HTTP entered after emergency stop")

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=stopped),
        pytest.raises(DMSendGateError, match="temporarily paused"),
    ):
        send(dm, reply)
    assert InboxReply.objects.get(pk=reply.pk).status == "failed"


@pytest.mark.parametrize("status", ["draft", "unknown", "sent"])
@pytest.mark.parametrize("source", ["poll", "webhook"])
def test_non_dm_mid_collision_preserves_dm_identity_evidence_and_receipt(dm, status, source):
    from apps.inbox.tasks import InboxSyncEngine
    from apps.inbox.tests.test_ingestion_events import _polled
    from apps.inbox.webhooks import _create_if_new

    first = draft(dm)
    InboxReply.objects.filter(pk=first.pk).update(status=status)
    before = InboxMessage.objects.values().get(pk=dm.message.pk)
    if source == "poll":
        InboxSyncEngine()._upsert_message(
            dm.account, _polled(mid=dm.message.platform_message_id, message_type="comment")
        )
    else:
        _create_if_new(
            dm.account, dm.message.platform_message_id, "comment", "Wrong sender", "other-peer", "Public comment", {}
        )
    assert InboxMessage.objects.values().get(pk=dm.message.pk) == before
    assert InboxReply.objects.get(pk=first.pk).status == status


def test_unsupported_dm_never_becomes_locally_sent(dm):
    dm.account.platform = "instagram"
    dm.account.save(update_fields=["platform"])
    reply = draft(dm)
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError, match="does not support"),
    ):
        send(dm, reply)
    provider.assert_not_called()
    assert InboxReply.objects.get(pk=reply.pk).status == "draft"


def _thread(function):
    close_old_connections()
    try:
        return function()
    finally:
        close_old_connections()


def test_postgres_concurrent_drafts_reuse_one_target(dm):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    at_account_lock = Barrier(2)
    original_lock = services.lock_dm_account

    def lock(*args, **kwargs):
        at_account_lock.wait(timeout=10)
        return original_lock(*args, **kwargs)

    with patch.object(services, "lock_dm_account", side_effect=lock), ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(_thread, lambda: draft(dm)) for _ in range(2)]
        assert len({result.result(timeout=10).pk for result in results}) == 1
    assert InboxReply.objects.count() == 1


def test_postgres_committed_unknown_visible_and_second_send_cannot_enter(dm):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks and independent committed connections")
    reply = draft(dm)
    entered, release, second_at_lock, second_finished = Event(), Event(), Event(), Event()
    second_thread = []
    original_lock = reply_safety.lock_dm_account

    def lock(*args, **kwargs):
        if second_thread and get_ident() == second_thread[0]:
            second_at_lock.set()
        return original_lock(*args, **kwargs)

    def slow(*args, **kwargs):
        kwargs["before_provider"]()
        entered.set()
        assert release.wait(10)
        return "outbound-1"

    def second():
        second_thread.append(get_ident())
        try:
            copy = InboxReply.objects.select_related("inbox_message__social_account").get(pk=reply.pk)
            with pytest.raises(services.ReplyStateError):
                send(dm, copy)
        finally:
            second_finished.set()

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=slow) as provider,
        patch.object(reply_safety, "lock_dm_account", side_effect=lock),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(_thread, lambda: send(dm, reply))
        try:
            assert entered.wait(10)
            assert InboxReply.objects.get(pk=reply.pk).status == "unknown"
            other = pool.submit(_thread, second)
            assert second_at_lock.wait(10)
            assert not second_finished.wait(0.2)
        finally:
            release.set()
        first.result(timeout=15)
        other.result(timeout=15)
    assert provider.call_count == 1


@pytest.mark.parametrize("platform", ["facebook", "instagram_login"])
def test_legacy_manual_standard_reply_lets_platform_check_eligibility(dm, platform):
    from datetime import timedelta

    dm.account.platform = platform
    dm.account.missing_scopes = []  # Absence of missing OAuth scopes is not feature approval.
    dm.account.save(update_fields=["platform", "missing_scopes"])
    dm.message.received_at = timezone.now() - timedelta(days=2)
    dm.message.save(update_fields=["received_at"])
    reply = draft(dm)
    assert services.reply_send_availability(dm.message, reply=reply)["allowed"]
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        services.send_reply_now(reply, actor=dm.user, authorization=dm.authorization, automated=False)
    provider.assert_called_once()
    reply.refresh_from_db()
    assert reply.status == "sent" and reply.send_generation == 1


def test_final_provider_entry_cannot_turn_manual_flag_into_human_agent_approval(dm):
    from datetime import timedelta

    dm.message.received_at = timezone.now() - timedelta(days=2)
    with patch("apps.inbox.services.get_provider") as provider:
        services._dispatch_to_platform(dm.message, "Synthetic", automated=False)
    provider.return_value.reply_to_message.assert_called_once()
    assert provider.return_value.reply_to_message.call_args.kwargs["human_agent"] is False


def test_comment_reply_does_not_acquire_a_dm_window_restriction(dm):
    from datetime import timedelta

    dm.message.message_type = "comment"
    dm.message.received_at = timezone.now() - timedelta(days=90)
    assert services.validate_meta_reply_window(dm.message, automated=False) is None
