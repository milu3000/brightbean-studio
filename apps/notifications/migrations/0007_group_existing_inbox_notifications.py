"""Add derived grouping links; original content, read state and deliveries stay intact."""
from uuid import UUID
from django.db import migrations
from django.db.models import F


def forwards(apps, schema_editor):
    from apps.notifications.identity_v1 import event_identity, message_identity
    Notification = apps.get_model("notifications", "Notification")
    Message = apps.get_model("inbox", "InboxMessage")
    Canonical = apps.get_model("inbox", "ConversationMessage")
    Event = apps.get_model("notifications", "InboxNotificationEvent")
    alias = schema_editor.connection.alias
    notifications = Notification.objects.using(alias)
    notifications.update(last_event_at=F("created_at"))
    notifications.filter(is_read=True).update(read_revision=F("revision"))
    groups = {}
    for n in notifications.filter(event_type="new_inbox_message").order_by("-created_at").iterator(chunk_size=500):
        if n.title.startswith("You were assigned "):
            continue
        data = n.data if isinstance(n.data, dict) else {}
        try:
            key = UUID(str(data.get("message_id", "")))
        except (TypeError, ValueError):
            continue
        message = Message.objects.using(alias).select_related("social_account").filter(pk=key).first()
        if message is None or str(message.workspace_id) != str(data.get("workspace_id")) or message.workspace_id != message.social_account.workspace_id:
            continue
        row = Canonical.objects.using(alias).select_related("conversation").filter(legacy_message_id=key).first()
        conversation = row.conversation if row and row.conversation_id else None
        subject = message_identity(message, conversation)
        notifications.filter(pk=n.pk).update(workspace_id=message.workspace_id, inbox_message_id=key, conversation_id=conversation.pk if conversation else None)
        groups.setdefault((n.user_id, subject), []).append((n, message, conversation))
    for (_, subject), records in groups.items():
        newest, message, conversation = records[0]
        master = next((n for n, _, _ in records if not n.is_read), newest)
        keys = {event_identity(m.social_account_id, m.social_account.platform, m.platform_message_id, m.message_type) for _, m, _ in records}
        notifications.filter(pk=master.pk).update(subject_key=subject, revision=len(keys), event_count=len(keys), read_revision=len(keys) if master.is_read else 0, inbox_message_id=message.pk, conversation_id=conversation.pk if conversation else None, latest_message_at=message.received_at, last_event_at=newest.created_at)
        notifications.filter(pk__in=[n.pk for n, _, _ in records if n.pk != master.pk]).update(superseded_by_id=master.pk)
        Event.objects.using(alias).bulk_create([Event(notification_id=master.pk, user_id=master.user_id, event_key=key) for key in keys], ignore_conflicts=True)


def backwards(apps, schema_editor):
    alias = schema_editor.connection.alias
    # Only derived links/receipts are removed. Operational rollback after live
    # activity should retain this schema rather than reversing it.
    apps.get_model("notifications", "InboxNotificationEvent").objects.using(alias).all().delete()
    apps.get_model("notifications", "Notification").objects.using(alias).update(subject_key=None, superseded_by=None, workspace=None, inbox_message=None, conversation=None, revision=1, read_revision=0, source_revision=0, event_count=1, latest_message_at=None, last_event_at=F("created_at"))


class Migration(migrations.Migration):
    dependencies = [("notifications", "0006_inboxnotificationevent_notification_conversation_and_more")]
    operations = [migrations.RunPython(forwards, backwards)]
