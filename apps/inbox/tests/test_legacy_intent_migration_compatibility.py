"""Forward migration preserves synthetic historical replies and inbox state."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.inbox.models import InboxReply

pytestmark = pytest.mark.django_db(transaction=True)


def test_0020_preserves_existing_draft_sent_unknown_and_message_state(inbox_message, restore_migrations):
    inbox_message.status = "resolved"
    inbox_message.save(update_fields=["status"])
    executor = MigrationExecutor(connection)
    executor.migrate([("inbox", "0019_owned_conversation_action")])
    historical = executor.loader.project_state([("inbox", "0019_owned_conversation_action")]).apps.get_model(
        "inbox", "InboxReply"
    )
    for status in ("draft", "sent", "unknown"):
        historical.objects.create(
            inbox_message_id=inbox_message.pk,
            body="Synthetic old " + status,
            status=status,
            platform_reply_id="synthetic-old-receipt" if status == "sent" else "",
            sent_at=timezone.now() if status == "sent" else None,
        )
    fields = [field.attname for field in historical._meta.concrete_fields]
    before = list(historical.objects.order_by("pk").values(*fields))
    MigrationExecutor(connection).migrate([("inbox", "0020_legacy_action_reference")])
    assert list(InboxReply.objects.order_by("pk").values(*fields)) == before
    assert not InboxReply.objects.exclude(legacy_action_nonce=None).exists()
    assert not InboxReply.objects.exclude(legacy_context_digest="").exists()
    inbox_message.refresh_from_db()
    assert inbox_message.status == "resolved"
