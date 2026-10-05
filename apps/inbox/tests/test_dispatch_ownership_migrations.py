"""Additive ownership schema and reversal guards preserve send evidence."""

import uuid

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.inbox import dm_send_gate as gate
from apps.inbox.models import (
    DMConversationOwnership,
    DMSendAttempt,
    InboxConversation,
    InboxReply,
    SendOperation,
)
from apps.inbox.tests.test_dispatch_ownership import clock as clock  # noqa: F401
from apps.inbox.tests.test_dispatch_ownership import owned as owned  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
PREVIOUS = [("inbox", "0007_dm_send_gate")]


@pytest.fixture(autouse=True)
def restore_current_schema(transactional_db):
    heads = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        yield
    finally:
        MigrationExecutor(connection).migrate(heads)


def test_forward_migration_preserves_legacy_reply_and_local_operation_states(inbox_message):
    executor = MigrationExecutor(connection)
    heads = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(PREVIOUS)
        old = executor.loader.project_state(PREVIOUS).apps
        reply_model = old.get_model("inbox", "InboxReply")
        conversation_model = old.get_model("inbox", "InboxConversation")
        operation_model = old.get_model("inbox", "SendOperation")
        replies = [
            reply_model.objects.create(
                inbox_message_id=inbox_message.pk,
                body=f"Synthetic {status}",
                status=status,
                platform_reply_id="synthetic-prior-outbound" if status == "sent" else "",
                send_error="synthetic prior evidence" if status in {"failed", "unknown"} else "",
                sent_at=timezone.now() if status == "sent" else None,
            )
            for status in ["draft", "sent", "failed", "unknown"]
        ]
        before_replies = list(reply_model.objects.order_by("pk").values())
        for status in ["prepared", "claimed", "confirmed", "failed", "outcome_unknown", "superseded"]:
            conversation = conversation_model.objects.create(
                workspace_id=inbox_message.workspace_id,
                social_account_id=inbox_message.social_account_id,
                platform=inbox_message.social_account.platform,
                peer_id=f"synthetic-{status}",
                identity_kind="verified_peer",
            )
            operation_model.objects.create(
                conversation=conversation,
                workspace_id=inbox_message.workspace_id,
                social_account_id=inbox_message.social_account_id,
                platform=inbox_message.social_account.platform,
                actor_scope="user:synthetic-prior-owner",
                idempotency_key=f"synthetic-{status}",
                payload_fingerprint="f" * 64,
                body=f"Synthetic {status}",
                expected_revision=4,
                expected_generation=7,
                status=status,
                claim_token=uuid.uuid4() if status != "prepared" else None,
                fencing_token=5,
                external_attempted_at=timezone.now() if status == "outcome_unknown" else None,
            )
        before_operations = list(operation_model.objects.order_by("pk").values())
        MigrationExecutor(connection).migrate(heads)
        assert list(InboxReply.objects.order_by("pk").values()) == before_replies
        old_fields = list(before_operations[0])
        assert list(SendOperation.objects.order_by("pk").values(*old_fields)) == before_operations
        assert not DMConversationOwnership.objects.exists()
        assert not SendOperation.objects.exclude(
            ownership=None, reply=None, attempt=None, owner_epoch=0, target_platform_message_id=""
        ).exists()
        assert set(InboxReply.objects.values_list("pk", flat=True)) == {reply.pk for reply in replies}
    finally:
        MigrationExecutor(connection).migrate(heads)


def test_reverse_refuses_persisted_ownership_even_without_any_dispatch(owned):
    with pytest.raises(RuntimeError, match="Cannot reverse"):
        MigrationExecutor(connection).migrate(PREVIOUS)
    assert DMConversationOwnership.objects.get(pk=owned.ownership.pk).owner_scope == owned.scope.actor_id
    assert not DMSendAttempt.objects.exists()


@pytest.mark.parametrize("link", ["reply", "attempt", "attempt_operation"])
def test_reverse_refuses_dispatch_links_even_if_no_ownership_row_survives(inbox_account, inbox_message, link):
    conversation = InboxConversation.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform=inbox_account.platform,
        peer_id="synthetic-migration-peer",
        identity_kind="verified_peer",
    )
    reply = InboxReply.objects.create(inbox_message=inbox_message, body="Synthetic", status="unknown")
    operation = SendOperation.objects.create(
        conversation=conversation,
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform=inbox_account.platform,
        actor_scope="user:synthetic-principal",
        idempotency_key="synthetic-reverse-guard",
        payload_fingerprint="f" * 64,
        body="Synthetic",
        expected_revision=0,
        expected_generation=0,
        status="outcome_unknown",
    )
    if link == "reply":
        operation.reply = reply
        operation.save(update_fields=["reply"])
    else:
        control = gate.enroll_dm_send_control(
            account_id=inbox_account.pk,
            workspace_id=inbox_account.workspace_id,
            platform=inbox_account.platform,
            account_platform_id=inbox_account.account_platform_id,
        )
        attempt = DMSendAttempt.objects.create(
            control=control,
            reply=reply,
            epoch=control.epoch,
            fingerprint="f" * 64,
            operation=operation if link == "attempt_operation" else None,
        )
        if link == "attempt":
            operation.attempt = attempt
            operation.save(update_fields=["attempt"])
    assert not DMConversationOwnership.objects.exists()
    with pytest.raises(RuntimeError, match="Cannot reverse"):
        MigrationExecutor(connection).migrate(PREVIOUS)
    operation.refresh_from_db()
    assert operation.status == "outcome_unknown"
    assert InboxReply.objects.get(pk=reply.pk).status == "unknown"
