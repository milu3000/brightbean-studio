from django.db import migrations, models


def preserve_workflow(apps, schema_editor):
    Conversation = apps.get_model("inbox", "InboxConversation")
    if Conversation.objects.using(schema_editor.connection.alias).filter(
        models.Q(incoming_generation__gt=0) | models.Q(workflow_state__isnull=False)
    ).exists():
        raise RuntimeError("Observed workflow generations exist. Preserve activity and completion evidence on rollback.")


class Migration(migrations.Migration):
    dependencies = [("inbox", "0011_conversation_composer")]
    operations = [
        migrations.AddField("inboxconversation", "incoming_generation", models.PositiveBigIntegerField(default=0, db_default=0)),
        migrations.AddField("inboxconversation", "incoming_observed_at", models.DateTimeField(null=True, blank=True)),
        migrations.AddField("inboxconversation", "incoming_watermark_at", models.DateTimeField(null=True, blank=True)),
        migrations.AddField("inboxconversation", "workflow_state", models.CharField(
            max_length=20, null=True, blank=True, choices=[("needs_action", "Needs action"), ("waiting", "Waiting"), ("done", "Done")])),
        migrations.AddField("inboxconversation", "workflow_baseline_at", models.DateTimeField(null=True, blank=True)),
        migrations.AddField("inboxconversation", "workflow_completed_at", models.DateTimeField(null=True, blank=True)),
        migrations.AddField("inboxconversation", "workflow_completed_generation", models.PositiveBigIntegerField(default=0, db_default=0)),
        migrations.AddField("inboxconversation", "workflow_outbound_at", models.DateTimeField(null=True, blank=True)),
        migrations.AddField("inboxconversation", "workflow_order_uncertain", models.BooleanField(default=False, db_default=False)),
        migrations.AddField("inboxconversation", "workflow_reviewed_generation", models.PositiveBigIntegerField(default=0, db_default=0)),
        migrations.AddField("conversationmessage", "incoming_generation", models.PositiveBigIntegerField(null=True, blank=True)),
        migrations.RunPython(migrations.RunPython.noop, preserve_workflow),
    ]
