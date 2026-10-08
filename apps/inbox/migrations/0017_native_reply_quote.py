from django.db import migrations, models


def preserve_quotes(apps, schema_editor):
    if apps.get_model("inbox", "InboxReply").objects.using(schema_editor.connection.alias).filter(quote_target__isnull=False).exists():
        raise RuntimeError("Native quote identities exist. Preserve their action and receipt metadata on rollback.")


class Migration(migrations.Migration):
    dependencies = [("inbox", "0016_durable_sync_ingestion")]
    operations = [
        migrations.AddField("inboxreply", "quote_target", models.ForeignKey(to="inbox.conversationmessage", on_delete=models.PROTECT, null=True, blank=True, related_name="quoted_by_replies")),
        migrations.AddField("inboxreply", "quote_platform_message_id", models.CharField(max_length=255, blank=True, default="", db_default="")),
        migrations.AddField("inboxreply", "quote_platform_conversation_id", models.CharField(max_length=255, blank=True, default="", db_default="")),
        migrations.AddField("inboxreply", "quote_connection_generation", models.UUIDField(null=True, blank=True)),
        migrations.AddConstraint("inboxreply", models.CheckConstraint(condition=(
            models.Q(quote_target__isnull=True, quote_platform_message_id="", quote_platform_conversation_id="", quote_connection_generation__isnull=True)
            | (models.Q(quote_target__isnull=False, conversation__isnull=False)
               & ~models.Q(quote_platform_message_id="") & ~models.Q(quote_platform_conversation_id=""))
        ), name="inbox_reply_quote_identity")),
        migrations.RunPython(migrations.RunPython.noop, preserve_quotes),
    ]
