"""Opt-in local ledger projection. Never reads a provider or changes inbox work."""

from django.core.management.base import BaseCommand, CommandError

from apps.inbox.conversations import enabled, record_reply, upsert_conversation_message
from apps.inbox.models import InboxMessage, InboxReply


class Command(BaseCommand):
    help = "Preview (or --apply) an idempotent, local-only DM conversation ledger backfill for one workspace."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", required=True, help="Exact workspace UUID to project")
        parser.add_argument("--account", help="Optional social account UUID within that workspace")
        parser.add_argument("--batch-size", type=int, default=200)
        parser.add_argument("--apply", action="store_true", help="Write ledger rows; default is a read-only preview")

    def handle(self, *args, **options):
        if not 1 <= options["batch_size"] <= 1000:
            raise CommandError("--batch-size must be between 1 and 1000.")
        if options["apply"] and not enabled():
            raise CommandError("Enable INBOX_CONVERSATION_V2_ENABLED before applying the local backfill.")
        from uuid import UUID

        try:
            workspace_id = UUID(options["workspace"])
            account_id = UUID(options["account"]) if options["account"] else None
        except (ValueError, TypeError) as exc:
            raise CommandError("Workspace and account must be valid UUIDs.") from exc
        messages = InboxMessage.objects.filter(workspace_id=workspace_id, message_type=InboxMessage.MessageType.DM)
        if account_id:
            messages = messages.filter(social_account_id=account_id)
        replies = InboxReply.objects.filter(inbox_message__in=messages, status=InboxReply.Status.SENT)
        counts = (messages.count(), replies.count())
        if not options["apply"]:
            self.stdout.write(
                f"Preview only: {counts[0]} local DMs and {counts[1]} sent/local reply records. No writes."
            )
            return
        for message in messages.select_related("social_account").order_by("pk").iterator(options["batch_size"]):
            extra = message.extra if isinstance(message.extra, dict) else {}
            sender = extra.get("sender") if isinstance(extra.get("sender"), dict) else {}
            upsert_conversation_message(
                message.social_account,
                platform_message_id=message.platform_message_id,
                sender_id=extra.get("sender_id") or sender.get("id") or "",
                sender_name=message.sender_name,
                body=message.body,
                extra=extra,
                occurred_at=message.received_at,
                source="legacy_backfill",
                legacy_message=message,
            )
        for reply in (
            replies.select_related("inbox_message__social_account").order_by("pk").iterator(options["batch_size"])
        ):
            record_reply(reply, source="legacy_backfill")
        self.stdout.write(
            self.style.SUCCESS(
                f"Projected {counts[0]} local DMs and {counts[1]} reply records. "
                "Remote history, inbox state, notifications and events were not changed."
            )
        )
