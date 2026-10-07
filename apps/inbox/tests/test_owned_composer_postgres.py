"""CI-only real PostgreSQL row-lock races for owner-bound composer actions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection

from apps.inbox.models import DMSendAttempt, SendOperation
from apps.inbox.tests.test_dispatch_ownership import clock as clock
from apps.inbox.tests.test_owned_composer_bridge import inputs, send
from apps.inbox.tests.test_owned_composer_bridge import owner as owner_fixture

owner = owner_fixture
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires real PostgreSQL row locks"),
]


@pytest.mark.parametrize("same_nonce", [True, False])
def test_competing_explicit_actions_dispatch_once_from_one_rendered_snapshot(owner, same_nonce):
    first = inputs(owner)
    values = [first, {**first, "action_nonce": first["action_nonce"] if same_nonce else str(uuid4())}]
    barrier, lock = Barrier(2), Lock()
    calls = []

    def provider(*args, **kwargs):
        kwargs["before_provider"]()
        with lock:
            calls.append(True)
        return "synthetic-owner-concurrent-send"

    def worker(value):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return str(send(owner, value).pk)
            except ValueError as exc:
                return getattr(exc, "code", str(exc))
        finally:
            close_old_connections()

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=provider),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        list(pool.map(worker, values))
    assert len(calls) == 1
    assert DMSendAttempt.objects.count() == 1 and SendOperation.objects.filter(status="confirmed").count() == 1
