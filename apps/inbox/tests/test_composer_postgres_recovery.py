"""PostgreSQL CI proofs; SQLite passes cannot establish these locking claims."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection

from apps.inbox.dm_send_gate import DMSendGateError
from apps.inbox.models import InboxReply
from apps.inbox.tests.test_conversation_composer_recovery import composer as composer_fixture
from apps.inbox.tests.test_conversation_composer_recovery import inputs, send_conversation_reply

composer = composer_fixture
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires real PostgreSQL row locks"),
]


def test_concurrent_same_nonce_has_one_provider_attempt(composer):
    value = inputs(composer)
    rendezvous, counter_lock = Barrier(2), Lock()
    dispatches = []

    def accepted(*args, **kwargs):
        kwargs["before_provider"]()
        with counter_lock:
            dispatches.append(True)
        return "synthetic-concurrent-native-mid"

    def worker():
        close_old_connections()
        try:
            rendezvous.wait(timeout=10)
            try:
                return str(send_conversation_reply(**value, automated=True).pk)
            except DMSendGateError as exc:
                return exc.code
        finally:
            close_old_connections()

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        results = list(executor.map(lambda _: worker(), range(2)))
        final = send_conversation_reply(**value, automated=True)
    assert str(final.pk) in results
    assert final.status == "sent" and len(dispatches) == 1
    assert (
        InboxReply.objects.filter(conversation=composer.conversation, action_nonce=value["action_nonce"]).count() == 1
    )
