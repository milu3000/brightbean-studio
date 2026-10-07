from django.db import migrations, models


def preserve_intents(apps, schema_editor):
    Reply = apps.get_model("inbox", "InboxReply")
    if Reply.objects.using(schema_editor.connection.alias).filter(conversation__isnull=False).exists():
        raise RuntimeError("Conversation actions exist. Preserve their nonce and receipt identity during rollback.")


class Migration(migrations.Migration):
    dependencies = [("inbox", "0010_inboxreply_follow_up_of")]
    operations = [
        migrations.AddField("inboxreply", "conversation", models.ForeignKey(
            to="inbox.inboxconversation", on_delete=models.PROTECT, null=True, blank=True, related_name="composer_replies")),
        migrations.AddField("inboxreply", "action_nonce", models.UUIDField(null=True, blank=True)),
        migrations.AddField("inboxreply", "account_platform_id", models.CharField(max_length=255, blank=True, default="", db_default="")),
        migrations.AddField("inboxreply", "recipient_id", models.CharField(max_length=255, blank=True, default="", db_default="")),
        migrations.AddField("inboxreply", "platform_conversation_id", models.CharField(max_length=255, blank=True, default="", db_default="")),
        migrations.AddField("inboxreply", "connection_generation", models.UUIDField(null=True, blank=True)),
        migrations.AddField("inboxreply", "conversation_incoming_generation", models.PositiveBigIntegerField(default=0, db_default=0)),
        migrations.AddField("inboxreply", "retired_at", models.DateTimeField(null=True, blank=True)),
        migrations.AddField("inboxconversation", "active_reply", models.ForeignKey(
            to="inbox.inboxreply", on_delete=models.SET_NULL, null=True, blank=True, related_name="active_composer_slots")),
        migrations.AddField("inboxconversation", "composer_revision", models.PositiveBigIntegerField(default=0, db_default=0)),
        migrations.AddConstraint("inboxreply", models.UniqueConstraint(
            fields=["conversation", "action_nonce"], name="inbox_reply_conversation_nonce")),
        migrations.AddConstraint("inboxreply", models.CheckConstraint(
            condition=(models.Q(conversation__isnull=True, action_nonce__isnull=True)
                       | (models.Q(conversation__isnull=False, action_nonce__isnull=False)
                          & ~models.Q(account_platform_id="") & ~models.Q(recipient_id="")
                          & ~models.Q(platform_conversation_id=""))),
            name="inbox_reply_conversation_intent")),
        migrations.RunPython(migrations.RunPython.noop, preserve_intents),
    ]
