"""Additive delivery metadata preserves old rows and old-writer DB defaults."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.inbox.models import InboxReply

pytestmark = pytest.mark.django_db(transaction=True)


def test_delivery_metadata_migration_preserves_rows_and_accepts_old_orm_writes(inbox_message, restore_migrations):
    before = ("inbox", "0009_conversation_classification")
    after = ("inbox", "0010_inboxreply_follow_up_of")
    executor = MigrationExecutor(connection)
    executor.migrate([before])
    old_apps = executor.loader.project_state([before]).apps
    old_reply_model = old_apps.get_model("inbox", "InboxReply")
    original = old_reply_model.objects.create(
        inbox_message_id=inbox_message.pk,
        body="Preserved historic failed text",
        status="failed",
        send_error="Historic provider uncertainty",
    )
    original_values = old_reply_model.objects.values().get(pk=original.pk)
    executor = MigrationExecutor(connection)
    executor.migrate([after])
    new_reply_model = executor.loader.project_state([after]).apps.get_model("inbox", "InboxReply")
    migrated = new_reply_model.objects.values().get(pk=original.pk)
    assert {key: migrated[key] for key in original_values} == original_values
    assert migrated["follow_up_of_id"] is None
    assert migrated["is_follow_up"] is False and migrated["not_sent_verified"] is False
    assert migrated["send_generation"] == 0
    # The old model omits all four new columns. DB defaults preserve inserts
    # during a rolling deployment; sending still requires current guard code.
    old_writer = old_reply_model.objects.create(inbox_message_id=inbox_message.pk, body="Old writer draft")
    readback = new_reply_model.objects.get(pk=old_writer.pk)
    assert readback.follow_up_of_id is None and not readback.is_follow_up and not readback.not_sent_verified
    assert readback.send_generation == 0


@pytest.mark.parametrize("metadata", ["orphan_intent", "parent_link", "verified_not_sent", "send_generation"])
def test_reverse_cannot_erase_delivery_safety_metadata(inbox_message, restore_migrations, metadata):
    parent = InboxReply.objects.create(inbox_message=inbox_message, body="Original known reply", status="sent")
    values = {"inbox_message": inbox_message, "body": "Retained delivery metadata"}
    if metadata == "orphan_intent":
        values["is_follow_up"] = True
    elif metadata == "parent_link":
        values["follow_up_of"] = parent
    elif metadata == "verified_not_sent":
        values["not_sent_verified"] = True
    else:
        values["send_generation"] = 1
    reply = InboxReply.objects.create(**values)
    # Later unused schema additions may reverse before this guard raises.
    # Read the protected historical columns rather than the latest ORM fields.
    historical_reply = (
        MigrationExecutor(connection)
        .loader.project_state([("inbox", "0010_inboxreply_follow_up_of")])
        .apps.get_model("inbox", "InboxReply")
    )
    before = list(historical_reply.objects.order_by("pk").values())
    with pytest.raises(RuntimeError, match="Cannot reverse reply delivery metadata"):
        MigrationExecutor(connection).migrate([("inbox", "0009_conversation_classification")])
    assert list(historical_reply.objects.order_by("pk").values()) == before
    reply = historical_reply.objects.get(pk=reply.pk)
    assert (reply.is_follow_up, bool(reply.follow_up_of_id), reply.not_sent_verified) == (
        metadata == "orphan_intent",
        metadata == "parent_link",
        metadata == "verified_not_sent",
    )
    assert reply.send_generation == (1 if metadata == "send_generation" else 0)
