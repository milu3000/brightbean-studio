"""Supplemental PR9 intent checked against exact frozen formal dispatcher."""

from contextlib import suppress
from datetime import timedelta
from unittest.mock import patch

import pytest

from apps.inbox.models import DMSendAttempt, InboxReply
from apps.inbox.tests.test_dispatch_ownership import REJECTED, claim, deliver
from apps.inbox.tests.test_dispatch_ownership import clock as clock
from apps.inbox.tests.test_dispatch_ownership import owned as owned

pytestmark = pytest.mark.django_db(transaction=True)


def test_real_acceptance_after_local_lease_expiry_preserves_receipt_without_repost(owned):
    operation = claim(owned)

    def accepted(*args, **kwargs):
        kwargs["before_provider"]()
        owned.clock.now = operation.lease_expires_at + timedelta(seconds=1)
        return "synthetic-formal-lease-receipt"

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        result = deliver(owned, operation)
        with suppress(REJECTED):
            deliver(owned, operation)
    reply = InboxReply.objects.get(pk=result.reply_id)
    assert reply.status == "sent" and reply.platform_reply_id == "synthetic-formal-lease-receipt"
    assert DMSendAttempt.objects.get(reply=reply).outcome == "sent"
    assert provider.call_count == 1
