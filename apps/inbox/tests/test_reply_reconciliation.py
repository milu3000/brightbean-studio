"""Explicit human recovery is audited, scoped, compare-and-swap and offline."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier, Event
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.utils import timezone

from apps.inbox import reply_reconciliation as recovery
from apps.inbox import reply_safety
from apps.inbox.dm_send_gate import DMSendGateError, enroll_dm_send_control
from apps.inbox.models import (
    ConversationMessage,
    InboxConversation,
    InboxMessage,
    InboxReply,
    InternalNote,
    SendOperation,
)
from apps.inbox.tests.test_shared_reply_safety import accepted, draft, send
from apps.inbox.tests.test_shared_reply_safety import dm as dm  # noqa: F401
from apps.members.models import CustomRole
from apps.social_accounts.models import SocialAccount

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def unresolved(dm):
    reply = draft(dm)
    reply.status = "unknown"
    reply.send_error = "Original delivery outcome unknown"
    reply.save(update_fields=["status", "send_error", "updated_at"])
    return reply


def resolve(dm, reply, outcome="not_sent", **overrides):
    arguments = dict(
        reply=reply,
        actor=dm.user,
        expected_updated_at=reply.updated_at,
        expected_send_generation=reply.send_generation,
        outcome=outcome,
        confirmed=True,
    )
    if outcome == "sent":
        arguments.update(platform_reply_id="manually-verified-mid", sent_at=timezone.now())
    arguments.update(overrides)
    return recovery.reconcile_reply_outcome(**arguments)


@pytest.mark.parametrize("prior_status", ["unknown", "failed"])
@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
def test_manual_review_retains_receipt_and_diagnostic_without_sending(dm, unresolved, prior_status, outcome):
    unresolved.status = prior_status
    unresolved.send_error = 'private-provider-response {"access_token":"synthetic-secret"}'
    unresolved.save(update_fields=["status", "send_error", "updated_at"])
    before = (unresolved.pk, unresolved.body, unresolved.author_id, unresolved.send_error, unresolved.created_at)
    with (
        patch("apps.inbox.services.get_provider") as provider,
        patch("apps.inbox.services._dispatch_to_platform") as dispatch,
        patch("apps.inbox.conversations.record_reply") as projection,
    ):
        result = resolve(dm, unresolved, outcome)
    provider.assert_not_called()
    dispatch.assert_not_called()
    projection.assert_not_called()
    assert not ConversationMessage.objects.exists()
    assert (result.pk, result.body, result.author_id, result.send_error, result.created_at) == before
    assert result.status == ("sent" if outcome == "sent" else "failed")
    assert result.not_sent_verified is (outcome == "not_sent")
    assert result.platform_reply_id == ("manually-verified-mid" if outcome == "sent" else "")
    note = InternalNote.objects.get(inbox_message=dm.message)
    assert note.author_id == dm.user.pk
    assert str(unresolved.pk) in note.body and f"{prior_status} → {result.status}" in note.body
    assert "Manually" in note.body
    assert "private-provider-response" not in note.body and "synthetic-secret" not in note.body


def test_only_explicit_later_send_retries_confirmed_not_sent(dm, unresolved):
    with patch("apps.inbox.services._dispatch_to_platform") as dispatch:
        result = resolve(dm, unresolved)
    dispatch.assert_not_called()
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as dispatch:
        send(dm, result)
    dispatch.assert_called_once()
    assert result.status == "sent" and not result.not_sent_verified


@pytest.mark.parametrize("confirmed", [False, None, "true", 1])
@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
def test_both_outcomes_require_exact_explicit_human_confirmation(dm, unresolved, confirmed, outcome):
    with pytest.raises(DMSendGateError) as raised:
        resolve(dm, unresolved, outcome, confirmed=confirmed)
    assert raised.value.code == "reconciliation_confirmation_required"
    assert InboxReply.objects.get(pk=unresolved.pk).status == "unknown"
    assert not InternalNote.objects.exists()


@pytest.mark.parametrize(
    "change", ["use_inbox", "manage_workspace_settings", "reply_from_inbox", "inactive", "archived", "removed"]
)
def test_current_actor_authority_is_rechecked(dm, unresolved, change):
    if change in {"use_inbox", "manage_workspace_settings", "reply_from_inbox"}:
        role = CustomRole.objects.create(
            organization=dm.account.workspace.organization,
            name="Restricted review",
            permissions={
                "use_inbox": change != "use_inbox",
                "manage_workspace_settings": change != "manage_workspace_settings",
                "reply_from_inbox": change != "reply_from_inbox",
            },
        )
        dm.member.custom_role = role
        dm.member.save(update_fields=["custom_role"])
    elif change == "inactive":
        dm.user.is_active = False
        dm.user.save(update_fields=["is_active"])
    elif change == "archived":
        dm.account.workspace.is_archived = True
        dm.account.workspace.save(update_fields=["is_archived"])
    else:
        dm.member.delete()
    assert recovery.reconciliation_availability(unresolved, actor=dm.user)["allowed"] is False
    with pytest.raises(DMSendGateError) as raised:
        resolve(dm, unresolved)
    assert raised.value.code == "reconciliation_denied"
    assert not InternalNote.objects.exists()


def test_stale_receipt_version_cannot_be_reconciled(dm, unresolved):
    stale = unresolved.updated_at
    InboxReply.objects.filter(pk=unresolved.pk).update(updated_at=stale + timedelta(seconds=1))
    with pytest.raises(DMSendGateError) as raised:
        resolve(dm, unresolved, expected_updated_at=stale.isoformat())
    assert raised.value.code == "reconciliation_stale"
    assert not InternalNote.objects.exists()


@pytest.mark.parametrize("generation", [None, True, False, "0", -1, 1])
def test_review_requires_exact_integer_send_generation(dm, unresolved, generation):
    with pytest.raises(DMSendGateError) as raised:
        resolve(dm, unresolved, expected_send_generation=generation)
    assert raised.value.code == "reconciliation_stale"
    assert not InternalNote.objects.exists()


def test_frozen_clock_aba_cannot_resume_old_sender_or_reuse_old_review_form(dm):
    from apps.inbox.dm_send_gate import capture_send_snapshot

    reply = draft(dm)
    original = reply_safety._prepare_receipt
    prepared = {}
    frozen = timezone.now()

    def prepare_a_then_b(*args, **kwargs):
        generation_a = original(*args, **kwargs)
        receipt_a = InboxReply.objects.get(pk=reply.pk)
        snapshot_a = capture_send_snapshot(receipt_a)
        resolve(dm, receipt_a, "not_sent")
        receipt_b = InboxReply.objects.get(pk=reply.pk)
        snapshot_b = capture_send_snapshot(receipt_b)
        generation_b = original(receipt_b, snapshot_b, dm.authorization, True, None)
        receipt_b.refresh_from_db()
        assert receipt_a.updated_at == receipt_b.updated_at == frozen
        assert snapshot_a.fingerprint == snapshot_b.fingerprint
        assert generation_a == 1 and generation_b == 2
        with pytest.raises(DMSendGateError) as stale_review:
            resolve(dm, receipt_a, "not_sent")
        assert stale_review.value.code == "reconciliation_stale"
        prepared.update(reply=receipt_b, generation=generation_b)
        return generation_a

    with (
        patch("django.utils.timezone.now", return_value=frozen),
        patch.object(reply_safety, "_prepare_receipt", side_effect=prepare_a_then_b),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError) as stale_sender,
    ):
        send(dm, reply)
    assert stale_sender.value.code == "receipt_changed"
    provider.assert_not_called()
    assert InboxReply.objects.get(pk=reply.pk).status == "unknown"
    assert InboxReply.objects.get(pk=reply.pk).send_generation == 2
    assert InternalNote.objects.count() == 1

    def timeout(*args, **kwargs):
        kwargs["before_provider"]()
        raise TimeoutError("Only B entered HTTP")

    # Resume exactly B's already committed preparation, not a new attempt.
    with (
        patch.object(reply_safety, "_prepare_receipt", return_value=prepared["generation"]),
        patch("apps.inbox.services._dispatch_to_platform", side_effect=timeout) as provider,
        pytest.raises(DMSendGateError, match="unknown"),
    ):
        send(dm, prepared["reply"])
    provider.assert_called_once()
    assert InboxReply.objects.get(pk=reply.pk).send_generation == 2


def test_prepare_returns_its_committed_generation_not_a_later_shared_instance_value(dm):
    from apps.inbox.dm_send_gate import capture_send_snapshot

    reply = draft(dm)
    snapshot = capture_send_snapshot(reply)
    original_atomic = transaction.atomic

    @contextmanager
    def mutate_after_commit(*args, **kwargs):
        with original_atomic(*args, **kwargs):
            yield
        if kwargs.get("durable"):
            # A caller may share and refresh this ORM instance after commit.
            reply.send_generation = 999

    with patch.object(reply_safety.transaction, "atomic", side_effect=mutate_after_commit):
        generation = reply_safety._prepare_receipt(reply, snapshot, dm.authorization, True, None)
    assert generation == 1
    assert reply.send_generation == 999
    assert InboxReply.objects.get(pk=reply.pk).send_generation == 1


def test_generation_is_checked_again_immediately_before_provider_entry(dm):
    reply = draft(dm)

    def changed(*args, **kwargs):
        InboxReply.objects.filter(pk=reply.pk).update(send_generation=999)
        kwargs["before_provider"]()
        pytest.fail("HTTP entered with a stale send generation")

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=changed),
        pytest.raises(DMSendGateError) as raised,
    ):
        send(dm, reply)
    assert raised.value.code == "receipt_changed"
    # The artificial in-transaction change rolled back; the durable marker stays.
    current = InboxReply.objects.get(pk=reply.pk)
    assert current.status == "unknown" and current.send_generation == 1


def test_postgres_old_sender_cannot_dispatch_after_review_and_new_retry(dm):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks and distinct committed connections")
    reply = draft(dm)
    original = reply_safety._prepare_receipt
    a_marked, b_marked, release_a, release_b = Event(), Event(), Event(), Event()
    frozen = timezone.now()

    def pause_prepared(*args, **kwargs):
        generation = original(*args, **kwargs)
        (a_marked if generation == 1 else b_marked).set()
        assert (release_a if generation == 1 else release_b).wait(15)
        return generation

    def sender():
        close_old_connections()
        try:
            return send(dm, InboxReply.objects.get(pk=reply.pk))
        finally:
            close_old_connections()

    def timeout(*args, **kwargs):
        kwargs["before_provider"]()
        raise TimeoutError("B may have been accepted")

    with (
        patch("django.utils.timezone.now", return_value=frozen),
        patch.object(reply_safety, "_prepare_receipt", side_effect=pause_prepared),
        patch("apps.inbox.services._dispatch_to_platform", side_effect=timeout) as provider,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(sender)
        try:
            assert a_marked.wait(15)
            reviewed = InboxReply.objects.get(pk=reply.pk)
            resolve(dm, reviewed, "not_sent")
            second = pool.submit(sender)
            assert b_marked.wait(15)
            current = InboxReply.objects.get(pk=reply.pk)
            assert current.updated_at == reviewed.updated_at == frozen
            assert current.send_generation == 2
            release_a.set()
            with pytest.raises(DMSendGateError) as stale:
                first.result(timeout=15)
            assert stale.value.code == "receipt_changed"
            provider.assert_not_called()
        finally:
            release_a.set()
            release_b.set()
        with pytest.raises(DMSendGateError, match="unknown"):
            second.result(timeout=15)
    provider.assert_called_once()
    assert InboxReply.objects.get(pk=reply.pk).send_generation == 2


@pytest.mark.parametrize("state", ["draft", "sent", "known_failed"])
def test_already_resolved_or_unattempted_receipt_cannot_be_overwritten(dm, unresolved, state):
    InboxReply.objects.filter(pk=unresolved.pk).update(
        status="failed" if state == "known_failed" else state, not_sent_verified=state == "known_failed"
    )
    with pytest.raises(DMSendGateError) as raised:
        resolve(dm, unresolved)
    assert raised.value.code == "reconciliation_not_required"
    assert not InternalNote.objects.exists()


@pytest.mark.parametrize("managed", ["control", "bound_operation", "target_operation"])
def test_managed_delivery_paths_cannot_be_bypassed(dm, unresolved, managed):
    if managed == "control":
        enroll_dm_send_control(
            account_id=dm.account.pk,
            workspace_id=dm.account.workspace_id,
            platform=dm.account.platform,
            account_platform_id=dm.account.account_platform_id,
        )
    else:
        conversation = InboxConversation.objects.create(
            workspace=dm.account.workspace,
            social_account=dm.account,
            platform=dm.account.platform,
            peer_id="peer-1",
            identity_kind="verified_peer",
        )
        target = ConversationMessage.objects.create(
            workspace=dm.account.workspace,
            social_account=dm.account,
            platform=dm.account.platform,
            conversation=conversation,
            legacy_message=dm.message,
        )
        SendOperation.objects.create(
            workspace=dm.account.workspace,
            social_account=dm.account,
            platform=dm.account.platform,
            conversation=conversation,
            actor_scope=f"user:{dm.user.pk}",
            idempotency_key="managed",
            payload_fingerprint="a" * 64,
            body="Coordinated",
            expected_revision=0,
            expected_generation=0,
            reply=unresolved if managed == "bound_operation" else None,
            target=target if managed == "target_operation" else None,
        )
    with pytest.raises(DMSendGateError) as raised:
        resolve(dm, unresolved)
    assert raised.value.code == "reconciliation_managed"
    assert InboxReply.objects.get(pk=unresolved.pk).status == "unknown"
    assert not InternalNote.objects.exists()


@pytest.mark.parametrize(
    "mid,stamp",
    [
        ("", "valid"),
        ("contains whitespace", "valid"),
        ("x" * 256, "valid"),
        ("mid", "naive"),
        ("mid", "future"),
        ("mid", "missing"),
    ],
)
def test_sent_requires_valid_provider_receipt_and_aware_nonfuture_time(dm, unresolved, mid, stamp):
    value = {
        "valid": timezone.now(),
        "naive": timezone.now().replace(tzinfo=None),
        "future": timezone.now() + timedelta(days=1),
        "missing": None,
    }[stamp]
    with pytest.raises(DMSendGateError) as raised:
        resolve(dm, unresolved, "sent", platform_reply_id=mid, sent_at=value)
    assert raised.value.code == "reconciliation_receipt_invalid"
    assert not InternalNote.objects.exists()


@pytest.mark.parametrize("seconds", [2, 60, 3600])
def test_manual_sent_cannot_predate_receipt_creation(dm, unresolved, seconds):
    with pytest.raises(DMSendGateError) as raised:
        resolve(dm, unresolved, "sent", sent_at=unresolved.created_at - timedelta(seconds=seconds))
    assert raised.value.code == "reconciliation_receipt_invalid"
    assert not InternalNote.objects.exists()


def test_manual_sent_accepts_provider_second_precision(dm, unresolved):
    result = resolve(dm, unresolved, "sent", sent_at=unresolved.created_at.replace(microsecond=0))
    assert result.status == "sent"


@pytest.mark.parametrize("changed", ["id", "time"])
def test_manual_review_never_overwrites_existing_provider_evidence(dm, unresolved, changed):
    stamp = timezone.now() - timedelta(microseconds=1)
    unresolved.platform_reply_id, unresolved.sent_at = "already-recorded-mid", stamp
    unresolved.save(update_fields=["platform_reply_id", "sent_at", "updated_at"])
    with pytest.raises(DMSendGateError) as raised:
        resolve(
            dm,
            unresolved,
            "sent",
            platform_reply_id="replacement-mid" if changed == "id" else "already-recorded-mid",
            sent_at=stamp if changed == "id" else timezone.now(),
        )
    assert raised.value.code == "reconciliation_receipt_conflict"
    unresolved.refresh_from_db()
    assert unresolved.platform_reply_id == "already-recorded-mid" and unresolved.sent_at == stamp


@pytest.mark.parametrize("same_account", [False, True])
def test_manual_provider_receipt_uniqueness_is_account_scoped(dm, unresolved, same_account):
    account = (
        dm.account
        if same_account
        else SocialAccount.objects.create(
            workspace=dm.account.workspace, platform="facebook", account_platform_id="other-page"
        )
    )
    message = InboxMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform_message_id="another-incoming",
        received_at=timezone.now(),
    )
    InboxReply.objects.create(
        inbox_message=message,
        body="Other sent receipt",
        status="sent",
        platform_reply_id="manually-verified-mid",
        sent_at=timezone.now(),
    )
    if same_account:
        with pytest.raises(DMSendGateError) as raised:
            resolve(dm, unresolved, "sent")
        assert raised.value.code == "reconciliation_receipt_conflict"
    else:
        assert resolve(dm, unresolved, "sent").status == "sent"


def test_audit_failure_rolls_back_manual_resolution(dm, unresolved):
    with (
        patch.object(InternalNote.objects, "create", side_effect=DatabaseError("Audit unavailable")),
        pytest.raises(DatabaseError),
    ):
        resolve(dm, unresolved)
    current = InboxReply.objects.get(pk=unresolved.pk)
    assert current.status == "unknown" and not current.not_sent_verified


@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
def test_original_invocation_cannot_overwrite_manual_result_or_enter_provider(dm, outcome):
    reply = draft(dm)
    original = reply_safety._prepare_receipt

    def reviewed(*args, **kwargs):
        generation = original(*args, **kwargs)
        receipt = InboxReply.objects.get(pk=reply.pk)
        resolve(dm, receipt, outcome)
        return generation

    with (
        patch.object(reply_safety, "_prepare_receipt", side_effect=reviewed),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError) as raised,
    ):
        send(dm, reply)
    assert raised.value.code == "receipt_changed"
    provider.assert_not_called()
    current = InboxReply.objects.get(pk=reply.pk)
    assert current.status == ("sent" if outcome == "sent" else "failed")
    assert current.not_sent_verified is (outcome == "not_sent")
    assert InternalNote.objects.count() == 1


def test_postgres_competing_reviews_commit_only_one_audit_and_outcome(dm, unresolved):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL account locks and distinct committed connections")
    at_lock = Barrier(2)
    original_lock = recovery.lock_dm_account

    def lock(*args, **kwargs):
        at_lock.wait(timeout=10)
        return original_lock(*args, **kwargs)

    def review(outcome):
        close_old_connections()
        try:
            try:
                return resolve(dm, unresolved, outcome)
            except DMSendGateError:
                return None
        finally:
            close_old_connections()

    with patch.object(recovery, "lock_dm_account", side_effect=lock), ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(review, outcome) for outcome in ("sent", "not_sent")]
        assert sum(result.result(timeout=15) is not None for result in results) == 1
    assert InternalNote.objects.count() == 1
