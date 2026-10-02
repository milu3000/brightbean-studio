"""Read-only rollout guard for changing an existing installation to password login."""

from django.contrib.auth.hashers import identify_hasher
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.accounts.models import User


class Command(BaseCommand):
    help = "Fail if active Google-linked users cannot sign in with a password. Does not modify users or send email."

    def add_arguments(self, parser):
        parser.add_argument("--show-emails", action="store_true", help="List affected addresses in this local console.")

    def handle(self, *args, **options):
        users = (
            User.objects.filter(is_active=True)
            .filter(Q(socialaccount__provider="google") | Q(oauth_connections__provider="google"))
            .distinct()
        )
        affected = []
        for user in users.iterator():
            try:
                if not user.has_usable_password():
                    raise ValueError
                identify_hasher(user.password)
            except ValueError:
                affected.append(user.email)
        if affected:
            if options["show_emails"]:
                for email in sorted(affected):
                    self.stdout.write(email)
            raise CommandError(
                f"{len(affected)} active Google-linked account(s) have no usable password. "
                "Keep AUTH_GOOGLE_LOGIN_ENABLED=true while these users set their own passwords "
                "through password reset or authenticated password setup. Verify email delivery and "
                "password login, rerun this command, then disable Google. No changes were made."
            )
        self.stdout.write(
            self.style.SUCCESS("No active Google-linked accounts need password migration. No changes made.")
        )
