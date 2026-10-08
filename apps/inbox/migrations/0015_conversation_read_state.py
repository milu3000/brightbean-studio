from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("inbox", "0014_reply_content_compaction"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(name="ConversationReadState", fields=[
        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
        ("read_generation", models.PositiveBigIntegerField(default=0)),
        ("updated_at", models.DateTimeField(auto_now=True)),
        ("conversation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="read_states", to="inbox.inboxconversation")),
        ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="inbox_read_states", to=settings.AUTH_USER_MODEL)),
    ], options={"db_table": "inbox_conversation_read_state", "constraints": [models.UniqueConstraint(
        fields=("conversation", "user"), name="inbox_read_user_convo_unique")]} )]
