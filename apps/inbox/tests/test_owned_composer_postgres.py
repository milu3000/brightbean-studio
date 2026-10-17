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


def test_accepted_then_settlement_failure_preserves_receipt_and_never_reposts(owner):
    """Optional bookkeeping failure cannot roll back the provider receipt."""
    value = inputs(owner)

    def provider(*args, **kwargs):
        kwargs["before_provider"]()
        return "synthetic-accepted-before-settlement-failure"

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=provider) as dispatch,
        patch(
            "apps.inbox.reply_dispatch.settle_dispatch_attempt",
            side_effect=RuntimeError("synthetic settlement failure"),
        ),
    ):
        first = send(owner, value)
        again = send(owner, value)
    assert dispatch.call_count == 1
    assert first.pk == again.pk and again.status == "sent"
    assert again.platform_reply_id == "synthetic-accepted-before-settlement-failure"
    attempt = DMSendAttempt.objects.get()
    assert attempt.outcome == "sent"
    assert SendOperation.objects.get().status == "outcome_unknown"
    from apps.inbox.accepted_projection import repair_missing_accepted_settlements

    with patch("apps.inbox.services._dispatch_to_platform") as no_dispatch:
        assert repair_missing_accepted_settlements() == {"repaired": 1, "held": 0, "failed": 0}
        assert repair_missing_accepted_settlements() == {"repaired": 0, "held": 0, "failed": 0}
        no_dispatch.assert_not_called()
    assert SendOperation.objects.get().status == "confirmed"


def test_projection_failure_does_not_erase_accepted_owned_receipt(owner):
    value = inputs(owner)

    def provider(*args, **kwargs):
        kwargs["before_provider"]()
        return "synthetic-accepted-before-projection-failure"

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=provider) as dispatch,
        patch("apps.inbox.conversations.record_reply", side_effect=RuntimeError("synthetic projection failure")),
    ):
        receipt = send(owner, value)
    assert dispatch.call_count == 1
    assert receipt.status == "sent"
    assert receipt.platform_reply_id == "synthetic-accepted-before-projection-failure"
    assert DMSendAttempt.objects.get().outcome == "sent"
    assert SendOperation.objects.get().status == "confirmed"


def test_database_commit_failure_after_acceptance_retains_unknown_and_never_reposts(owner):
    """Without a committed provider receipt, acceptance cannot be invented."""
    from django.db import OperationalError

    value = inputs(owner)
    original_commit = connection.commit
    provider_entered = False
    receipt_commit_failed = False

    def provider(*args, **kwargs):
        nonlocal provider_entered
        kwargs["before_provider"]()
        provider_entered = True
        return "synthetic-accepted-before-commit-failure"

    def commit():
        nonlocal receipt_commit_failed
        if provider_entered and not receipt_commit_failed:
            receipt_commit_failed = True
            raise OperationalError("synthetic receipt commit failure")
        return original_commit()

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=provider) as dispatch,
        patch.object(connection, "commit", side_effect=commit),
        pytest.raises(ValueError),
    ):
        send(owner, value)
    with patch("apps.inbox.services._dispatch_to_platform") as no_retry:
        with pytest.raises(ValueError):
            send(owner, value)
        no_retry.assert_not_called()
    assert dispatch.call_count == 1
    attempt = DMSendAttempt.objects.get()
    assert attempt.outcome == "unknown" and attempt.reply.status == "unknown"
    assert attempt.reply.platform_reply_id == ""
