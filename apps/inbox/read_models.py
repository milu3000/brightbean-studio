"""Per-user read progress independent from workflow, drafts and delivery."""

from django.conf import settings
from django.db import models


class ConversationReadState(models.Model):
    conversation = models.ForeignKey("inbox.InboxConversation", on_delete=models.CASCADE, related_name="read_states")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="inbox_read_states")
    read_generation = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inbox_conversation_read_state"
        constraints = [models.UniqueConstraint(fields=["conversation", "user"], name="inbox_read_user_convo_unique")]
