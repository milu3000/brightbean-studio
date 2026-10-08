from django.db import migrations, models


def preserve_actions(apps, schema_editor):
    if (
        apps.get_model("inbox", "SendOperation")
        .objects.using(schema_editor.connection.alias)
        .filter(conversation_action_nonce__isnull=False)
        .exists()
    ):
        raise RuntimeError(
            "Owned conversation action nonces exist. Preserve their operation and receipt evidence during rollback."
        )


class Migration(migrations.Migration):
    dependencies = [("inbox", "0018_sync_optional_content_capability")]
    operations = [
        migrations.AddField("sendoperation", "conversation_action_nonce", models.UUIDField(null=True, blank=True)),
        migrations.AddField("sendoperation", "human_observed_at", models.DateTimeField(null=True, blank=True)),
        migrations.RemoveConstraint("sendoperation", "inbox_send_confirmed_target"),
        migrations.AddConstraint(
            "sendoperation",
            models.UniqueConstraint(
                fields=["conversation", "target_platform_message_id"],
                condition=models.Q(status="confirmed", conversation_action_nonce__isnull=True)
                & ~models.Q(target_platform_message_id=""),
                name="inbox_send_confirmed_target",
            ),
        ),
        migrations.AddConstraint(
            "sendoperation",
            models.UniqueConstraint(
                fields=["conversation", "conversation_action_nonce"], name="inbox_send_conversation_action"
            ),
        ),
        migrations.AddConstraint(
            "sendoperation",
            models.CheckConstraint(
                condition=models.Q(conversation_action_nonce__isnull=True) | models.Q(reply__isnull=False),
                name="inbox_send_action_has_reply",
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, preserve_actions),
    ]
