"""Opt-in local ledger projection. Never reads a provider or changes inbox work."""

from django.core.management.base import BaseCommand, CommandError

from apps.inbox.conversation_policy import capture_allowed, enabled
from apps.inbox.conversations import _id, record_reply, upsert_conversation_message
from apps.inbox.models import InboxMessage, InboxReply
from apps.social_accounts.models import SocialAccount


class Command(BaseCommand):
    help = "Preview (or --apply) an idempotent, local-only DM conversation ledger backfill for one workspace."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", required=True, help="Exact workspace UUID to project")
        parser.add_argument("--account", help="Exact social account UUID; required with --apply")
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
        if options["apply"] and account_id is None:
            raise CommandError("--apply requires an exact --account enrolled for capture.")
        accounts = SocialAccount.objects.filter(workspace_id=workspace_id)
        if account_id is not None:
            accounts = accounts.filter(pk=account_id)
        enrolled = [account.pk for account in accounts if capture_allowed(account)]
        if options["apply"] and account_id not in enrolled:
            raise CommandError("Account is not enrolled for conversation capture in this workspace and platform.")
        messages = InboxMessage.objects.filter(
            workspace_id=workspace_id,
            social_account__workspace_id=workspace_id,
            social_account_id__in=enrolled,
            message_type=InboxMessage.MessageType.DM,
        )
        replies = InboxReply.objects.filter(inbox_message__in=messages, status=InboxReply.Status.SENT)
        eligible_messages = sum(
            bool(_id(mid))
            for mid in messages.values_list("platform_message_id", flat=True).iterator(options["batch_size"])
        )
        counts = (eligible_messages, replies.count())
        if not options["apply"]:
            self.stdout.write(
                f"Preview only: {counts[0]} eligible local DMs and {counts[1]} eligible sent/local reply records. No writes."
            )
            return
        projected_messages = projected_replies = 0
        for message in messages.select_related("social_account").order_by("pk").iterator(options["batch_size"]):
            extra = message.extra if isinstance(message.extra, dict) else {}
            sender = extra.get("sender") if isinstance(extra.get("sender"), dict) else {}
            row = upsert_conversation_message(
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
            projected_messages += row is not None
        for reply in (
            replies.select_related("inbox_message__social_account").order_by("pk").iterator(options["batch_size"])
        ):
            projected_replies += record_reply(reply, source="legacy_backfill") is not None
        self.stdout.write(
            self.style.SUCCESS(
                f"Projected {projected_messages} local DMs and {projected_replies} reply records "
                f"(initially eligible: {counts[0]} DMs, {counts[1]} replies). "
                "Remote history, inbox state, notifications and events were not changed."
            )
        )
