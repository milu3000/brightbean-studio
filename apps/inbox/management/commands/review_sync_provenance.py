"""Print a bounded provenance preview; applying requires its exact reviewed digest."""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.inbox.sync_identity import SyncError
from apps.inbox.sync_provenance import apply_provenance, preview_provenance


class Command(BaseCommand):
    help = "Preview native source proof for existing canonical rows; never enroll or activate sync."

    def add_arguments(self, parser):
        parser.add_argument("connection_id")
        parser.add_argument("--after", default="")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--expected-fingerprint", default="")

    def handle(self, *args, **options):
        kwargs = {"after": options["after"], "limit": options["limit"]}
        try:
            if options["apply"]:
                if not options["expected_fingerprint"]:
                    raise CommandError("--apply requires the exact reviewed --expected-fingerprint")
                result = apply_provenance(
                    options["connection_id"], expected_fingerprint=options["expected_fingerprint"], **kwargs
                )
            else:
                result = preview_provenance(options["connection_id"], **kwargs)
        except SyncError as exc:
            raise CommandError(exc.code) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
