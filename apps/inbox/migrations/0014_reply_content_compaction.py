import uuid

from django.db import migrations, models


def preserve_archives(apps, schema_editor):
    alias = schema_editor.connection.alias
    if (apps.get_model("inbox", "InboxArchiveIdentity").objects.using(alias).exists()
            or apps.get_model("inbox", "InboxReplyContentRecovery").objects.using(alias).exists()
            or apps.get_model("inbox", "InboxReply").objects.using(alias).filter(content_compacted_at__isnull=False).exists()):
        raise RuntimeError("Archive identities or restricted receipt content exist. Preserve them during rollback.")


class Migration(migrations.Migration):
    dependencies = [("inbox", "0013_durable_sync_foundation")]
    operations = [
        migrations.AddField("inboxreply", "content_compacted_at", models.DateTimeField(null=True, blank=True)),
        migrations.CreateModel(name="InboxReplyContentRecovery", fields=[
            ("reply", models.OneToOneField(to="inbox.inboxreply", on_delete=models.PROTECT, primary_key=True, serialize=False, related_name="restricted_content")),
            ("body", models.TextField(blank=True, default="")),
            ("send_error", models.TextField(blank=True, default="")),
            ("canonical_body", models.TextField(blank=True, default="")),
            ("canonical_attachments", models.JSONField(default=list, blank=True)),
            ("reason", models.CharField(max_length=30)),
            ("archived_at", models.DateTimeField(auto_now_add=True)),
        ], options={"db_table": "inbox_reply_content_recovery"}),
        migrations.CreateModel(name="InboxArchiveIdentity", fields=[
            ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ("social_account", models.ForeignKey(to="social_accounts.socialaccount", on_delete=models.PROTECT, related_name="inbox_archives")),
            ("workspace", models.ForeignKey(to="workspaces.workspace", on_delete=models.PROTECT)),
            ("platform", models.CharField(max_length=30)),
            ("account_platform_id", models.CharField(max_length=255)),
            ("webhook_target_id", models.CharField(max_length=255, blank=True, default="")),
            ("archived_connection", models.ForeignKey(to="inbox.inboxsyncconnection", on_delete=models.PROTECT, null=True, blank=True)),
            ("connection_generation", models.UUIDField(null=True, blank=True)),
            ("archived_at", models.DateTimeField(auto_now_add=True)),
        ], options={"db_table": "inbox_archive_identity", "ordering": ["-archived_at", "-id"]}),
        migrations.RunPython(migrations.RunPython.noop, preserve_archives),
    ]
