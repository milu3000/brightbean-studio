"""Additive migration retains existing lifecycle rows and guards reverse loss."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.inbox import dm_send_gate as gate
from apps.inbox.models import DMSendControl, InboxReply

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("restore_migrations")]


def test_existing_sent_draft_failed_records_are_unchanged(inbox_message):
    records = [
        InboxReply.objects.create(
            inbox_message=inbox_message,
            body=status,
            status=status,
            platform_reply_id="synthetic-sent" if status == "sent" else "",
        )
        for status in ("sent", "draft", "failed")
    ]
    heads = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        MigrationExecutor(connection).migrate([("inbox", "0006_reply_coordination")])
        MigrationExecutor(connection).migrate(heads)
        for reply in records:
            before = (reply.status, reply.body, reply.platform_reply_id, reply.created_at)
            reply.refresh_from_db()
            assert (reply.status, reply.body, reply.platform_reply_id, reply.created_at) == before
        assert not DMSendControl.objects.exists()
    finally:
        MigrationExecutor(connection).migrate(heads)


def test_reverse_refuses_to_delete_enrolled_safety_history(inbox_account):
    control = gate.enroll_dm_send_control(
        account_id=inbox_account.pk,
        workspace_id=inbox_account.workspace_id,
        platform=inbox_account.platform,
        account_platform_id=inbox_account.account_platform_id,
    )
    with pytest.raises(RuntimeError, match="Cannot reverse"):
        MigrationExecutor(connection).migrate([("inbox", "0006_reply_coordination")])
    assert DMSendControl.objects.get(pk=control.pk).paused
