"""Persisted scheduler budgets and five-minute-worker cutover proofs."""

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from apps.inbox.durable_sync import auth_fingerprint
from apps.inbox.models import InboxSyncBudget, InboxSyncConnection
from apps.inbox.sync_contracts import ConversationObservation, SyncPage
from apps.inbox.sync_scheduler import public_messages_only, release_gets, reserve_gets, run_sync_cycle
from apps.inbox.tests.test_durable_pages_recovery import durable as _durable
from apps.social_accounts.models import SocialAccount

durable = _durable
pytestmark = pytest.mark.django_db


@pytest.fixture
def shared_app():
    with patch("apps.inbox.sync_scheduler._resolve_publish_credentials", return_value={"client_id": "synthetic-app"}):
        yield


def extra_accounts(binding, enroll, count):
    accounts = [binding.social_account]
    connections = [binding]
    for index in range(count):
        account = SocialAccount.objects.create(
            workspace=binding.workspace,
            platform=binding.platform,
            account_platform_id=f"page-{index + 2}",
            oauth_access_token="synthetic-token",
        )
        accounts.append(account)
        connections.append(
            InboxSyncConnection.objects.create(
                social_account=account,
                workspace=binding.workspace,
                platform=account.platform,
                account_platform_id=account.account_platform_id,
                auth_fingerprint=auth_fingerprint(account),
                enabled=True,
            )
        )
    enroll(*accounts)
    return connections


def test_account_app_and_concurrency_budgets_persist(durable, enroll_conversation_accounts, shared_app):
    conns = extra_accounts(durable, enroll_conversation_accounts, 5)
    first = reserve_gets(conns[0].pk, 2)
    second = reserve_gets(conns[1].pk, 2)
    assert first and second and reserve_gets(conns[2].pk, 1) is None
    release_gets(first)
    release_gets(second)
    first = reserve_gets(conns[0].pk, 2)
    release_gets(first)
    assert reserve_gets(conns[0].pk, 1) is None
    for conn in conns[2:4]:
        reservation = reserve_gets(conn.pk, 2)
        assert reservation
        release_gets(reservation)
    assert reserve_gets(conns[4].pk, 1) is None
    budget = InboxSyncBudget.objects.get()
    assert budget.gets_reserved == 10 and max(budget.account_spend.values()) == 4


def test_crash_slot_expires_but_spend_never_refunds_or_clock_rollback_resets(durable, shared_app):
    now = timezone.now()
    old = reserve_gets(durable.pk, 2, now=now)
    assert reserve_gets(durable.pk, 1, now=now + timedelta(seconds=20)) is None
    new = reserve_gets(durable.pk, 2, now=now + timedelta(seconds=91))
    assert new and new != old
    release_gets(old)  # stale worker cannot release the new reservation
    assert reserve_gets(durable.pk, 1, now=now - timedelta(hours=1)) is None
    assert InboxSyncBudget.objects.get().gets_reserved == 4
    release_gets(new)
    assert reserve_gets(durable.pk, 1, now=now + timedelta(minutes=5))


def test_shared_breaker_and_connection_backoff_precede_budget(durable, shared_app):
    with patch("apps.inbox.sync_scheduler.quota.quota_blocked_until", return_value=timezone.now() + timedelta(hours=1)):
        assert reserve_gets(durable.pk, 1) is None
    durable.retry_at = timezone.now() + timedelta(hours=1)
    durable.save()
    assert reserve_gets(durable.pk, 1) is None
    assert not InboxSyncBudget.objects.exists()


def test_existing_worker_advances_discovery_and_message_edges_same_cycle(durable, shared_app):
    adapter = Mock()
    adapter.required_gets.side_effect = lambda stream: 1 if stream == "conversations" else 2
    adapter.fetch.side_effect = lambda lease: (
        SyncPage((ConversationObservation("thread-1", ("page-1", "peer-1")),))
        if lease.stream == "conversations"
        else SyncPage()
    )
    result = run_sync_cycle(adapter=adapter)
    assert result["pages"] == 2
    assert durable.checkpoints.filter(status="complete").count() == 2
    assert InboxSyncBudget.objects.get().gets_reserved == 3
    with (
        patch("apps.inbox.sync_scheduler.run_sync_cycle") as durable_cycle,
        patch("apps.inbox.tasks.InboxSyncEngine.sync_all"),
        patch("apps.inbox.tasks.InboxSyncEngine.check_sla"),
    ):
        from apps.inbox.tasks import InboxSyncEngine

        InboxSyncEngine().run_cycle()
        durable_cycle.assert_called_once()


def test_default_off_does_no_queue_or_provider_work(durable, settings):
    settings.INBOX_DURABLE_SYNC_ENABLED = False
    adapter = Mock()
    assert run_sync_cycle(adapter=adapter) == {"pages": 0, "held": 0}
    adapter.fetch.assert_not_called()


def test_public_poll_never_calls_combined_dm_getter(durable):
    provider = Mock()
    provider._fetch_post_comments.return_value = ["public-comment"]
    assert public_messages_only(durable.social_account, provider, None) == ["public-comment"]
    provider.get_messages.assert_not_called()
    provider._fetch_direct_messages.assert_not_called()
