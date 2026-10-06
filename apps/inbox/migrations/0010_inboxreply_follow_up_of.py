from django.db import migrations, models


def preserve_delivery_metadata(apps, schema_editor):
    reply = apps.get_model("inbox", "InboxReply")
    if reply.objects.using(schema_editor.connection.alias).filter(
        models.Q(is_follow_up=True)
        | models.Q(follow_up_of__isnull=False)
        | models.Q(not_sent_verified=True)
        | models.Q(send_generation__gt=0)
    ).exists():
        raise RuntimeError("Cannot reverse reply delivery metadata while follow-up intent or definitive failure proof exists. Preserve the safety history.")


class Migration(migrations.Migration):
    dependencies = [("inbox", "0009_conversation_classification")]

    operations = [
        migrations.AddField(
            model_name="inboxreply",
            name="follow_up_of",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name="follow_up_reply",
                to="inbox.inboxreply",
            ),
        ),
        migrations.AddField(
            model_name="inboxreply",
            name="is_follow_up",
            field=models.BooleanField(default=False, db_default=False),
        ),
        migrations.AddField(
            model_name="inboxreply",
            name="not_sent_verified",
            field=models.BooleanField(default=False, db_default=False),
        ),
        migrations.AddField(
            model_name="inboxreply",
            name="send_generation",
            field=models.PositiveBigIntegerField(default=0, db_default=0),
        ),
        migrations.RunPython(migrations.RunPython.noop, preserve_delivery_metadata),
    ]
