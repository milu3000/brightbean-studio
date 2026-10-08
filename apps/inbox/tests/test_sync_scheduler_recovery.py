"""Persisted scheduler budgets and five-minute-worker cutover proofs."""

from datetime import timedelta
from unittest.mock import Mock, patch

import httpx
import pytest
from django.core import serializers
from django.utils import timezone

from apps.inbox.durable_sync import auth_fingerprint, claim_page, commit_page, start_scan
from apps.inbox.meta_sync_adapter import MetaSyncAdapter
from apps.inbox.models import ConversationMessage, InboxSyncBudget, InboxSyncConnection, InboxSyncReceipt
from apps.inbox.sync_contracts import ConversationObservation, SyncPage
from apps.inbox.sync_scheduler import public_messages_only, release_gets, reserve_gets, run_sync_cycle
from apps.inbox.tests.test_durable_pages_recovery import durable as _durable
from apps.inbox.tests.test_durable_pages_recovery import stalled_live_pair
from apps.inbox.tests.test_meta_sync_recovery import metadata
from apps.inbox.tests.test_sync_ingestion_recovery import instagram
from apps.inbox.tests.test_sync_unassigned_recovery import deliver
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


def complete_listings(binding, now):
    for context in ("live", "repair"):
        cp = start_scan(binding.pk, context=context, now=now)
        commit_page(claim_page(cp.pk, now=now), SyncPage(observed_at=now), now=now)


def test_stalled_head_rounds_attribute_signed_unassigned_mids_and_enqueue_once(
    durable, shared_app, enroll_conversation_accounts, settings, client, user
):
    from apps.api_keys.services import issue_api_key
    from apps.mcp import events
    from apps.mcp.models import EventOutbox, EventSubscription
    from apps.mcp.tests.test_events import SECRET, URL
    from apps.members.models import OrgMembership, WorkspaceMembership

    account = instagram(durable)
    enroll_conversation_accounts(account, read=True)
    settings.INBOX_CANONICAL_READ_ENABLED = settings.MCP_EVENTS_ENABLED = True
    settings.MCP_EVENTS_ALLOWED_CALLBACK_HOSTS = ["callback.example.test"]
    now = timezone.now()
    cp, history, _ = stalled_live_pair(durable, now=now)
    baseline = durable.bootstrap_baseline_at
    original_history = serializers.serialize("json", [history])
    complete_listings(durable, now)
    OrgMembership.objects.get_or_create(
        user=user, organization=account.workspace.organization, defaults={"org_role": "owner"}
    )
    membership = WorkspaceMembership.objects.create(user=user, workspace=account.workspace, workspace_role="owner")
    issued = issue_api_key(
        workspace=account.workspace,
        social_accounts=[account],
        issued_by=user,
        name="offline",
        permissions=["use_inbox"],
    )
    with patch("apps.mcp.events.verify_callback"):
        result = events.subscribe(
            {
                "name": events.EVENT_NAME,
                "arguments": {"social_account_id": str(account.pk)},
                "delivery": {"mode": "webhook", "url": URL, "secret": SECRET},
            },
            {"api_key": issued.api_key, "workspace": account.workspace, "membership": membership},
        )
    EventSubscription.objects.filter(pk=result["id"]).update(started_at=baseline)
    rows, requests, heads = [], [], []

    def provider(request):
        requests.append(request)
        if request.url.path.endswith("/conversations"):
            return httpx.Response(200, json={"data": [metadata()]})
        if request.url.path == "/v25.0/thread-1":
            return httpx.Response(200, json=metadata())
        assert request.url.path == "/v25.0/thread-1/messages"
        after = request.url.params.get("after")
        if not after:
            heads.append(timezone.now())
        next_url = request.url.copy_set_param("after", "cursorA")
        return httpx.Response(
            200,
            json={
                "data": [] if after else list(reversed(rows)),
                "paging": {"cursors": {"after": "cursorA"}, "next": str(next_url)},
            },
        )

    adapter = MetaSyncAdapter(transport=httpx.MockTransport(provider))
    for round_number, minutes in enumerate((0, 5, 15)):
        if round_number == 2:
            # Listing + head + continuation cost five GETs against a four-GET
            # window. The next tick discovers the stall; only the tick after
            # that can refresh the head again. Never borrow quota for freshness.
            continuation_at = now + timedelta(minutes=10)
            with patch("django.utils.timezone.now", return_value=continuation_at):
                run_sync_cycle(adapter=adapter, now=continuation_at)
                cp.refresh_from_db()
                assert cp.status == "blocked" and cp.last_error_code == "pagination_no_progress"
                assert EventOutbox.objects.count() == 2 and len(heads) == 2
                assert InboxSyncBudget.objects.get().gets_reserved == 3
                history.refresh_from_db()
                assert serializers.serialize("json", [history]) == original_history
        instant = now + timedelta(minutes=minutes)
        with patch("django.utils.timezone.now", return_value=instant):
            mid = f"fresh-signed-{round_number}"
            assert deliver(client, durable, settings, mid=mid, object_name="instagram").status_code == 200
            unattributed = ConversationMessage.objects.get(platform_message_id=mid)
            assert unattributed.conversation_id is None and unattributed.incoming_generation is None
            assert InboxSyncReceipt.objects.get(platform_message_id=mid).status == "unassigned"
            assert EventOutbox.objects.count() == round_number
            rows.append(
                {
                    "id": mid,
                    "from": {"id": "peer-1"},
                    "to": {"data": [{"id": "page-1"}]},
                    "message": unattributed.body,
                    "created_time": unattributed.occurred_at.isoformat(),
                }
            )
            if not round_number:
                rows.extend(
                    [
                        {
                            "id": "old-history",
                            "from": {"id": "peer-1"},
                            "to": {"data": [{"id": "page-1"}]},
                            "message": "Old history stays quiet",
                            "created_time": (baseline - timedelta(days=1)).isoformat(),
                        },
                        {
                            "id": "native-outbound",
                            "from": {"id": "page-1"},
                            "to": {"data": [{"id": "peer-1"}]},
                            "message": "Our reply stays quiet",
                            "created_time": (instant - timedelta(seconds=1)).isoformat(),
                        },
                    ]
                )
            cycle = run_sync_cycle(adapter=adapter, now=instant)
            assert cycle["pages"] >= 1
            unattributed.refresh_from_db()
            assert unattributed.conversation.platform_conversation_id == "thread-1"
            assert unattributed.incoming_generation == round_number + 1
            assert unattributed.occurred_at.isoformat() == next(row["created_time"] for row in rows if row["id"] == mid)
            assert InboxSyncReceipt.objects.get(platform_message_id=mid).status == "processed"
            assert EventOutbox.objects.filter(canonical_message=unattributed).count() == 1
            assert EventOutbox.objects.count() == round_number + 1
            history.refresh_from_db()
            assert serializers.serialize("json", [history]) == original_history
            budget = InboxSyncBudget.objects.get()
            assert budget.gets_reserved == (4 if round_number == 0 else 3)
            assert budget.account_spend[str(account.pk)] <= 4 and budget.active_leases == []
    assert heads == [now, now + timedelta(minutes=5), now + timedelta(minutes=15)]
    assert len(requests) == 13
    assert sum(request.url.path == "/v25.0/thread-1" for request in requests) == 5
    assert ConversationMessage.objects.filter(incoming_generation__isnull=False).count() == 3
    durable.refresh_from_db()
    assert durable.bootstrap_baseline_at == baseline


def test_stalled_rollover_cannot_bypass_reserved_get_quota(durable, shared_app, enroll_conversation_accounts):
    enroll_conversation_accounts(instagram(durable))
    now = timezone.now()
    cp, history, _ = stalled_live_pair(durable, now=now)
    complete_listings(durable, now)
    original = serializers.serialize("json", [history])
    for _ in range(2):
        release_gets(reserve_gets(durable.pk, 2))
    requests = []
    adapter = MetaSyncAdapter(transport=httpx.MockTransport(lambda request: requests.append(request)))
    result = run_sync_cycle(adapter=adapter, now=now)
    cp.refresh_from_db()
    assert cp.status == "ready" and cp.cursor == "" and cp.coverage == "partial"
    assert result["pages"] == 0 and result["held"] > 0 and requests == []
    assert InboxSyncBudget.objects.get().gets_reserved == 4
    history.refresh_from_db()
    assert serializers.serialize("json", [history]) == original


@pytest.mark.parametrize("participants,kind", [([], "unknown"), (["page-1", "peer-1", "third-peer"], "group")])
def test_new_head_still_requires_fresh_direct_identity_for_action(
    durable, shared_app, enroll_conversation_accounts, participants, kind
):
    from apps.inbox.canonical_send_target import validate_anchor
    from apps.inbox.dm_send_gate import DMSendGateError

    enroll_conversation_accounts(instagram(durable), read=True)
    now = timezone.now()
    _cp, history, _ = stalled_live_pair(durable, now=now)
    original = serializers.serialize("json", [history])
    complete_listings(durable, now)

    def provider(request):
        if request.url.path == "/v25.0/thread-1":
            return httpx.Response(
                200, json={"id": "thread-1", "participants": {"data": [{"id": value} for value in participants]}}
            )
        assert request.url.path == "/v25.0/thread-1/messages" and "after" not in request.url.params
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "not-direct",
                        "from": {"id": "peer-1"},
                        "to": {"data": [{"id": "page-1"}]},
                        "message": "Identity remains constrained",
                        "created_time": (now - timedelta(seconds=1)).isoformat(),
                    }
                ]
            },
        )

    run_sync_cycle(adapter=MetaSyncAdapter(transport=httpx.MockTransport(provider)), now=now)
    row = ConversationMessage.objects.get(platform_message_id="not-direct")
    assert row.conversation_type == row.conversation.conversation_type == kind
    assert row.observation_state.actionable_observed_at is None
    with pytest.raises(DMSendGateError, match="verified incoming"):
        validate_anchor(row, row.conversation, durable.social_account)
    history.refresh_from_db()
    assert serializers.serialize("json", [history]) == original
