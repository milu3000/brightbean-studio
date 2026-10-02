"""Offline MCP Events contract, isolation, revocation and outbox regression tests."""

import base64
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import connection, transaction
from django.test import Client
from django.utils import timezone

from apps.api.auth import VirtualMembership
from apps.api_keys.services import issue_api_key
from apps.inbox.models import InboxMessage
from apps.mcp import events
from apps.mcp.event_delivery import CallbackError
from apps.mcp.models import EventOutbox, EventSubscription
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.tasks import MAX_ATTEMPTS, process_delivery
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

# Deliberately public deterministic fixture, never a real signing credential.
SECRET = "whsec_" + base64.b64encode(b"offline-fixture-not-a-real-key!!!").decode()
ROTATED_SECRET = "whsec_" + base64.b64encode(b"offline-replacement-fixture-key!!").decode()
URL = "https://callback.example.test/mcp/events"


@pytest.fixture(autouse=True)
def enabled(settings):
    settings.MCP_EVENTS_ENABLED = True
    settings.MCP_EVENTS_ALLOWED_CALLBACK_HOSTS = ["callback.example.test"]


@pytest.fixture
def context(user, organization):
    workspace = Workspace.objects.create(name="Events", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    account = SocialAccount.objects.create(
        workspace=workspace, platform="facebook", account_platform_id="events-account", account_name="Events"
    )
    issued = issue_api_key(
        workspace=workspace, social_accounts=[account], issued_by=user, name="events", permissions=["use_inbox"]
    )
    return {
        "api_key": issued.api_key,
        "workspace": workspace,
        "membership": membership,
        "request": SimpleNamespace(META={"HTTP_AUTHORIZATION": f"Bearer {issued.plaintext_token}"}),
        "account": account,
    }


@pytest.fixture
def params(context):
    return {
        "name": events.EVENT_NAME,
        "arguments": {"social_account_id": str(context["account"].pk)},
        "delivery": {"mode": "webhook", "url": URL, "secret": SECRET},
    }


@pytest.fixture
def verify():
    with patch("apps.mcp.events.verify_callback") as mock:
        yield mock


@pytest.fixture
def subscription(context, params, verify):
    result = events.subscribe(params, context)
    return EventSubscription.objects.get(pk=result["id"])


def create_message(context, **kwargs):
    defaults = {
        "workspace": context["workspace"],
        "social_account": context["account"],
        "platform_message_id": "inbound-1",
        "sender_name": "Someone",
        "body": "private text must not be delivered",
        "message_type": "dm",
        "received_at": timezone.now(),
    }
    return InboxMessage.objects.create(**(defaults | kwargs))


def queued(context):
    message = create_message(context)
    events.enqueue_inbox_event(message)
    return EventOutbox.objects.get(), message


@pytest.mark.django_db
class TestSubscriptions:
    def test_catalog_schema_minimal_scope(self, context):
        result = events.list_events({}, context)
        definition = result["events"][0]
        assert definition["name"] == "inbox.dm.received"
        assert definition["inputSchema"]["required"] == ["social_account_id"]
        assert set(definition["payloadSchema"]["properties"]) == {"message_id", "workspace_id", "social_account_id"}

    def test_idempotent_subscribe_and_refresh(self, context, params, verify):
        first = events.subscribe(params, context)
        second = events.subscribe(dict(reversed(list(params.items()))), context)
        assert first["id"] == second["id"]
        assert second["cursor"] is None and second["truncated"] is False
        assert EventSubscription.objects.count() == 1
        verify.assert_called_once()

    def test_callback_verification_failure_never_activates(self, context, params, verify):
        verify.side_effect = CallbackError("challenge_failed")
        with pytest.raises(JsonRpcError) as raised:
            events.subscribe(params, context)
        assert raised.value.code == -32015
        assert raised.value.data == {"reason": "challenge_failed"}
        assert not EventSubscription.objects.exists()

    def test_revocation_during_callback_verification_prevents_activation(self, context, params, verify):
        verify.side_effect = lambda *a: context["api_key"].social_accounts.clear()
        with pytest.raises(JsonRpcError):
            events.subscribe(params, context)
        assert not EventSubscription.objects.exists()

    @pytest.mark.parametrize("ttl", [False, 0, -1, "1000", 1.5])
    def test_invalid_ttl(self, context, params, verify, ttl):
        params["ttlMs"] = ttl
        with pytest.raises(JsonRpcError):
            events.subscribe(params, context)
        verify.assert_not_called()

    @pytest.mark.parametrize("ttl", [1000, 200000000, None])
    def test_ttl_is_bounded(self, context, params, verify, ttl):
        params["ttlMs"] = ttl
        before = timezone.now()
        result = events.subscribe(params, context)
        sub = EventSubscription.objects.get(pk=result["id"])
        assert sub.expires_at > before
        assert sub.expires_at <= timezone.now() + timedelta(seconds=86400)
        if ttl == 1000:
            assert sub.expires_at <= timezone.now() + timedelta(seconds=1)

    @pytest.mark.parametrize(
        "changes",
        [{"name": "wrong"}, {"arguments": {}}, {"arguments": {"social_account_id": "bad"}}, {"cursor": "replay"}],
    )
    def test_invalid_params_no_callback(self, context, params, verify, changes):
        with pytest.raises(JsonRpcError):
            events.subscribe(params | changes, context)
        verify.assert_not_called()

    def test_wrong_account_and_workspace_fail_closed(self, context, params, verify, organization):
        foreign_ws = Workspace.objects.create(name="Foreign", organization=organization)
        account = SocialAccount.objects.create(
            workspace=foreign_ws, platform="facebook", account_platform_id="foreign", account_name="Foreign"
        )
        # Even a corrupt cross-workspace allowlist cannot escape workspace checks.
        context["api_key"].social_accounts.add(account)
        params["arguments"]["social_account_id"] = str(account.pk)
        with pytest.raises(JsonRpcError):
            events.subscribe(params, context)
        verify.assert_not_called()

    def test_permission_filtered_discovery(self, context):
        context["membership"] = VirtualMembership({}, context["workspace"], context["api_key"].issued_by)
        assert events.list_events({}, context) == {"events": []}

    def test_encrypted_secret_and_callback_storage(self, subscription):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT signing_secret, callback_url FROM mcp_server_eventsubscription WHERE id = %s", [subscription.pk]
            )
            stored_secret, stored_callback = cursor.fetchone()
        assert SECRET not in stored_secret and stored_secret != SECRET
        assert URL not in stored_callback
        assert subscription.signing_secret == SECRET

    def test_secret_rotation_and_new_challenge(self, context, params, subscription, verify):
        params["delivery"]["secret"] = ROTATED_SECRET
        result = events.subscribe(params, context)
        assert result["id"] == subscription.pk
        subscription.refresh_from_db()
        assert subscription.signing_secret == ROTATED_SECRET
        assert subscription.previous_secret == SECRET
        assert subscription.previous_secret_until > timezone.now()
        assert verify.call_count == 2

    def test_expired_verification_cache_reverifies(self, context, params, subscription, verify):
        EventSubscription.objects.filter(pk=subscription.pk).update(verified_at=timezone.now() - timedelta(minutes=10))
        events.subscribe(params, context)
        assert verify.call_count == 2

    def test_unsubscribe_idempotent_cancels_outbox(self, context, params, subscription):
        delivery, _ = queued(context)
        assert events.unsubscribe(params, context) == {}
        assert events.unsubscribe(params, context) == {}
        subscription.refresh_from_db()
        delivery.refresh_from_db()
        assert not subscription.active and subscription.signing_secret == ""
        assert delivery.status == EventOutbox.Status.CANCELLED

    def test_unsubscribe_survives_disabled_feature_and_removed_allowlist(self, context, params, subscription, settings):
        settings.MCP_EVENTS_ENABLED = False
        settings.MCP_EVENTS_ALLOWED_CALLBACK_HOSTS = []
        assert events.unsubscribe(params, context) == {}
        subscription.refresh_from_db()
        assert not subscription.active

    def test_api_key_expiry_caps_granted_subscription(self, context, params, verify):
        expiry = timezone.now() + timedelta(minutes=2)
        context["api_key"].expires_at = expiry
        context["api_key"].save()
        result = events.subscribe(params, context)
        assert EventSubscription.objects.get(pk=result["id"]).expires_at == expiry

    def test_other_key_cannot_unsubscribe(self, context, params, subscription):
        issued = issue_api_key(
            workspace=context["workspace"],
            social_accounts=[context["account"]],
            issued_by=context["api_key"].issued_by,
            name="other",
            permissions=["use_inbox"],
        )
        other = context | {"api_key": issued.api_key}
        assert events.unsubscribe(params, other) == {}
        subscription.refresh_from_db()
        assert subscription.active

    def test_resubscribe_cannot_resurrect_pending_delivery(self, context, params, subscription, verify):
        delivery, _ = queued(context)
        events.unsubscribe(params, context)
        events.subscribe(params, context)
        delivery.refresh_from_db()
        assert delivery.status == EventOutbox.Status.CANCELLED
        subscription.refresh_from_db()
        assert subscription.generation != delivery.generation


@pytest.mark.django_db
class TestOutbox:
    def test_minimal_occurrence_time_payload_and_duplicate_suppression(self, context, subscription):
        delivery, message = queued(context)
        events.enqueue_inbox_event(message)
        assert EventOutbox.objects.count() == 1
        payload = json.loads(delivery.payload)
        assert payload["timestamp"] == message.received_at.isoformat().replace("+00:00", "Z")
        assert payload["eventId"] == delivery.event_id
        assert payload["data"]["message_id"] == str(message.pk)
        assert "private text" not in delivery.payload and "Someone" not in delivery.payload

    @pytest.mark.parametrize(
        "values",
        [
            {"message_type": "comment"},
            {"extra": {"is_echo": True}},
            {"extra": {"direction": "outbound"}},
            {"received_at": timezone.now() - timedelta(days=2)},
        ],
    )
    def test_nonmatching_echo_and_old_events_suppressed(self, context, subscription, values):
        events.enqueue_inbox_event(create_message(context, **values))
        assert not EventOutbox.objects.exists()

    def test_rollback_leaves_no_outbox_or_callback(self, context, subscription, django_capture_on_commit_callbacks):
        with (
            patch("apps.mcp.tasks.deliver_event") as worker,
            django_capture_on_commit_callbacks(execute=True),
            pytest.raises(ValueError),
            transaction.atomic(),
        ):
            message = create_message(context)
            events.enqueue_inbox_event(message)
            raise ValueError("roll back")
        assert not InboxMessage.objects.filter(platform_message_id="inbound-1").exists()
        assert not EventOutbox.objects.exists()
        worker.assert_not_called()

    def test_worker_queued_only_after_commit(self, context, subscription, django_capture_on_commit_callbacks):
        with patch("apps.mcp.tasks.deliver_event") as worker, django_capture_on_commit_callbacks(execute=True):
            delivery, _ = queued(context)
            worker.assert_not_called()
        worker.assert_called_once_with(str(delivery.pk))

    def test_queue_failure_retains_recoverable_row(self, context, subscription, django_capture_on_commit_callbacks):
        with (
            patch("apps.mcp.tasks.deliver_event", side_effect=RuntimeError("offline")),
            django_capture_on_commit_callbacks(execute=True),
        ):
            delivery, _ = queued(context)
        assert EventOutbox.objects.get(pk=delivery.pk).status == "pending"

    def test_success_only_once(self, context, subscription):
        delivery, _ = queued(context)
        with patch("apps.mcp.tasks.post_signed", return_value=SimpleNamespace(status=204)) as send:
            process_delivery(delivery.pk)
            process_delivery(delivery.pk)
        assert send.call_count == 1
        delivery.refresh_from_db()
        assert delivery.status == "delivered" and delivery.attempts == 1

    def test_retry_preserves_event_id_and_body(self, context, subscription):
        delivery, _ = queued(context)
        with patch("apps.mcp.tasks.post_signed", return_value=SimpleNamespace(status=503)) as send:
            process_delivery(delivery.pk)
            process_delivery(delivery.pk)  # retry not yet due
            assert send.call_count == 1
            EventOutbox.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now())
            send.return_value = SimpleNamespace(status=200)
            process_delivery(delivery.pk)
        assert send.call_args_list[0].args[3:5] == send.call_args_list[1].args[3:5]
        delivery.refresh_from_db()
        assert delivery.attempts == 2 and delivery.status == "delivered"

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 413])
    def test_permanent_responses_not_retried(self, context, subscription, code):
        delivery, _ = queued(context)
        with patch("apps.mcp.tasks.post_signed", return_value=SimpleNamespace(status=code)):
            process_delivery(delivery.pk)
        delivery.refresh_from_db()
        assert delivery.status == "failed"

    def test_410_stops_subscription(self, context, subscription):
        delivery, _ = queued(context)
        with patch("apps.mcp.tasks.post_signed", return_value=SimpleNamespace(status=410)):
            process_delivery(delivery.pk)
        subscription.refresh_from_db()
        assert not subscription.active and subscription.stopped_reason == "receiver_gone"

    def test_retries_exhaust(self, context, subscription):
        delivery, _ = queued(context)
        with patch("apps.mcp.tasks.post_signed", return_value=SimpleNamespace(status=503)) as send:
            for _ in range(MAX_ATTEMPTS + 2):
                EventOutbox.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now())
                process_delivery(delivery.pk)
        delivery.refresh_from_db()
        assert send.call_count == MAX_ATTEMPTS
        assert delivery.status == "failed"

    @pytest.mark.parametrize(
        "revoke", ["key", "permissions", "membership", "user", "workspace", "allowlist", "account", "expiry"]
    )
    def test_revoked_access_prevents_delivery(self, context, subscription, revoke):
        delivery, _ = queued(context)
        key = context["api_key"]
        if revoke == "key":
            key.revoked_at = timezone.now()
            key.save()
        elif revoke == "permissions":
            key.permissions = []
            key.save()
        elif revoke == "membership":
            context["membership"].delete()
        elif revoke == "user":
            key.issued_by.is_active = False
            key.issued_by.save()
        elif revoke == "workspace":
            context["workspace"].is_archived = True
            context["workspace"].save()
        elif revoke == "allowlist":
            key.social_accounts.clear()
        elif revoke == "account":
            context["account"].connection_status = "disconnected"
            context["account"].save()
        elif revoke == "expiry":
            EventSubscription.objects.filter(pk=subscription.pk).update(expires_at=timezone.now())
        with patch("apps.mcp.tasks.post_signed") as send:
            process_delivery(delivery.pk)
        send.assert_not_called()
        delivery.refresh_from_db()
        assert delivery.status == "cancelled"

    def test_message_account_reassignment_prevents_delivery(self, context, subscription):
        delivery, message = queued(context)
        other = SocialAccount.objects.create(
            workspace=context["workspace"], platform="facebook", account_platform_id="other", account_name="Other"
        )
        message.social_account = other
        message.save()
        with patch("apps.mcp.tasks.post_signed") as send:
            process_delivery(delivery.pk)
        send.assert_not_called()

    def test_impossible_future_timestamp_suppressed(self, context, subscription):
        events.enqueue_inbox_event(create_message(context, received_at=timezone.now() + timedelta(days=1)))
        assert not EventOutbox.objects.exists()

    def test_expired_secret_purged_without_messages(self, subscription):
        from apps.mcp.tasks import recover_event_outbox

        EventSubscription.objects.filter(pk=subscription.pk).update(expires_at=timezone.now())
        recover_event_outbox.now()
        subscription.refresh_from_db()
        assert not subscription.active and subscription.signing_secret == ""

    def test_recovery_reschedules_due_row_once(self, context, subscription):
        from background_task.models import Task

        from apps.mcp.tasks import recover_event_outbox

        delivery, _ = queued(context)
        recover_event_outbox.now()
        recover_event_outbox.now()
        jobs = Task.objects.filter(task_name="apps.mcp.tasks.deliver_event")
        assert jobs.count() == 1
        assert str(delivery.pk) in jobs.get().task_params

    def test_rotation_window_signs_two_keys(self, context, subscription):
        delivery, _ = queued(context)
        subscription.previous_secret = ROTATED_SECRET
        subscription.previous_secret_until = timezone.now() + timedelta(minutes=5)
        subscription.save()
        with patch("apps.mcp.tasks.post_signed", return_value=SimpleNamespace(status=204)) as send:
            process_delivery(delivery.pk)
        assert send.call_args.kwargs["previous_secret"] == ROTATED_SECRET

    def test_transient_network_error_is_retried(self, context, subscription):
        delivery, _ = queued(context)
        with patch("apps.mcp.tasks.post_signed", side_effect=CallbackError("timeout")):
            process_delivery(delivery.pk)
        delivery.refresh_from_db()
        assert delivery.status == "pending" and delivery.attempts == 1
        assert delivery.last_error == "timeout"

    def test_disabling_events_stops_network(self, context, subscription, settings):
        delivery, _ = queued(context)
        settings.MCP_EVENTS_ENABLED = False
        with patch("apps.mcp.tasks.post_signed") as send:
            process_delivery(delivery.pk)
        send.assert_not_called()


@pytest.mark.django_db
class TestTransport:
    def rpc(self, context, method, params=None):
        client = Client(HTTP_AUTHORIZATION=context["request"].META["HTTP_AUTHORIZATION"])
        response = client.post(
            "/api/v1/mcp/",
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}),
            content_type="application/json",
            secure=True,
        )
        assert response.status_code == 200
        return response.json()

    def test_legacy_initialize_unchanged(self, context):
        result = self.rpc(context, "initialize", {"protocolVersion": "2025-03-26"})["result"]
        assert result["protocolVersion"] == "2025-03-26"
        assert "events" not in result["capabilities"]

    def test_modern_discover_and_initialize(self, context):
        result = self.rpc(context, "server/discover")["result"]
        assert "2026-07-28" in result["supportedVersions"]
        assert result["capabilities"]["events"] == {}
        result = self.rpc(context, "initialize", {"protocolVersion": "2026-07-28"})["result"]
        assert result["protocolVersion"] == "2026-07-28"
        assert "events" in result["capabilities"]

    def test_event_methods_auth_and_dispatch(self, context, params, verify):
        assert self.rpc(context, "events/list")["result"]["events"]
        assert self.rpc(context, "events/subscribe", params)["result"]["id"].startswith("sub_")
        assert self.rpc(context, "events/unsubscribe", params)["result"] == {}

    def test_feature_disabled_not_advertised(self, context, settings):
        settings.MCP_EVENTS_ENABLED = False
        assert self.rpc(context, "server/discover")["error"]["code"] == -32601
        assert self.rpc(context, "events/list")["error"]["code"] == -32601


@pytest.mark.django_db
class TestOAuthSubscriptions:
    def oauth_context(self, context, app=None, suffix="1"):
        from oauth2_provider.models import get_access_token_model, get_application_model

        from apps.api.auth import OAuthMcpActor

        app_model = get_application_model()
        app = app or app_model.objects.create(
            name="Offline event client", client_type="public", authorization_grant_type="authorization-code"
        )
        raw = "offline-oauth-fixture-" + suffix
        token = get_access_token_model().objects.create(
            user=context["api_key"].issued_by,
            application=app,
            token=raw,
            scope="mcp",
            expires=timezone.now() + timedelta(hours=1),
        )
        actor = OAuthMcpActor(user=context["api_key"].issued_by, membership=context["membership"])
        return context | {
            "api_key": actor,
            "request": SimpleNamespace(META={"HTTP_AUTHORIZATION": f"Bearer {raw}"}),
        }, token

    def test_token_refresh_keeps_subscription_identity(self, context, params, verify):
        ctx, first_token = self.oauth_context(context)
        first = events.subscribe(params, ctx)
        new_ctx, second_token = self.oauth_context(context, first_token.application, suffix="2")
        second = events.subscribe(params, new_ctx)
        assert first["id"] == second["id"]
        sub = EventSubscription.objects.get(pk=first["id"])
        assert sub.oauth_token_checksum == second_token.token_checksum
        assert sub.oauth_token_checksum == hashlib.sha256(b"offline-oauth-fixture-2").hexdigest()

    def test_oauth_expiry_caps_subscription(self, context, params, verify):
        ctx, token = self.oauth_context(context)
        result = events.subscribe(params, ctx)
        assert EventSubscription.objects.get(pk=result["id"]).expires_at == token.expires

    def test_oauth_workspace_switch_can_cancel_owned_prior_workspace(self, context, params, verify):
        ctx, token = self.oauth_context(context)
        result = events.subscribe(params, ctx)
        original = context["workspace"]
        other_ws = Workspace.objects.create(name="Other workspace", organization=original.organization)
        other_membership = WorkspaceMembership.objects.create(
            user=token.user, workspace=other_ws, workspace_role="owner"
        )
        from apps.api.auth import OAuthMcpActor

        other = ctx | {
            "api_key": OAuthMcpActor(user=token.user, membership=other_membership),
            "workspace": other_ws,
            "membership": other_membership,
        }
        events.unsubscribe(params, other)
        assert not EventSubscription.objects.get(pk=result["id"]).active

    def test_distinct_oauth_apps_cannot_unsubscribe(self, context, params, verify):
        ctx, _ = self.oauth_context(context)
        result = events.subscribe(params, ctx)
        other, _ = self.oauth_context(context, suffix="2")
        events.unsubscribe(params, other)
        assert EventSubscription.objects.get(pk=result["id"]).active

    def test_other_oauth_user_same_app_cannot_cancel(self, context, params, verify):
        from oauth2_provider.models import get_access_token_model

        from apps.accounts.models import User
        from apps.api.auth import OAuthMcpActor

        ctx, token = self.oauth_context(context)
        result = events.subscribe(params, ctx)
        other_user = User.objects.create_user(
            email="other-events@example.test", password="offline", name="Other", tos_accepted_at=timezone.now()
        )
        other_membership = WorkspaceMembership.objects.create(
            user=other_user, workspace=context["workspace"], workspace_role="owner"
        )
        other_raw = "offline-other-user-token"
        get_access_token_model().objects.create(
            user=other_user,
            application=token.application,
            token=other_raw,
            scope="mcp",
            expires=timezone.now() + timedelta(hours=1),
        )
        other = ctx | {
            "api_key": OAuthMcpActor(user=other_user, membership=other_membership),
            "membership": other_membership,
            "request": SimpleNamespace(META={"HTTP_AUTHORIZATION": f"Bearer {other_raw}"}),
        }
        events.unsubscribe(params, other)
        assert EventSubscription.objects.get(pk=result["id"]).active

    def test_oauth_revocation_prevents_delivery(self, context, params, verify):
        ctx, token = self.oauth_context(context)
        events.subscribe(params, ctx)
        delivery, _ = queued(context)
        token.delete()
        with patch("apps.mcp.tasks.post_signed") as send:
            process_delivery(delivery.pk)
        send.assert_not_called()
        delivery.refresh_from_db()
        assert delivery.status == "cancelled"

    def test_oauth_wrong_scope_rejected(self, context, params, verify):
        ctx, token = self.oauth_context(context)
        token.scope = "read"
        token.save()
        with pytest.raises(JsonRpcError) as raised:
            events.subscribe(params, ctx)
        assert raised.value.code == INVALID_PARAMS
        verify.assert_not_called()


@pytest.mark.django_db(transaction=True)
class TestPostgresConcurrency:
    """Real PostgreSQL row locks; SQLite cannot prove these guarantees."""

    @pytest.fixture(autouse=True)
    def require_postgresql(self):
        if connection.vendor != "postgresql":
            pytest.skip("Requires PostgreSQL row-level locks")

    @staticmethod
    def run_in_thread(function, *args):
        from django.db import close_old_connections

        close_old_connections()
        try:
            return function(*args)
        finally:
            close_old_connections()

    def test_concurrent_workers_send_once(self, context, subscription):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        with patch("apps.mcp.events._queue_outbox"):
            delivery, _ = queued(context)
        sending, release = Event(), Event()

        def send(*args, **kwargs):
            sending.set()
            assert release.wait(10)
            return SimpleNamespace(status=204)

        with (
            patch("apps.mcp.tasks.post_signed", side_effect=send) as callback,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(self.run_in_thread, process_delivery, delivery.pk)
            try:
                assert sending.wait(10)
                second = pool.submit(self.run_in_thread, process_delivery, delivery.pk)
            finally:
                release.set()
            first.result(timeout=15)
            second.result(timeout=15)
        assert callback.call_count == 1
        delivery.refresh_from_db()
        assert delivery.status == "delivered" and delivery.attempts == 1

    def test_unsubscribe_serializes_with_in_flight_delivery(self, context, params, subscription):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        with patch("apps.mcp.events._queue_outbox"):
            delivery, _ = queued(context)
        sending, release, cancelling, cancelled = Event(), Event(), Event(), Event()

        def send(*args, **kwargs):
            sending.set()
            assert release.wait(10)
            return SimpleNamespace(status=204)

        def unsubscribe():
            cancelling.set()
            result = events.unsubscribe(params, context)
            cancelled.set()
            return result

        with patch("apps.mcp.tasks.post_signed", side_effect=send), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.run_in_thread, process_delivery, delivery.pk)
            try:
                assert sending.wait(10)
                second = pool.submit(self.run_in_thread, unsubscribe)
                assert cancelling.wait(10)
                assert not cancelled.wait(0.2)
            finally:
                release.set()
            first.result(timeout=15)
            assert second.result(timeout=15) == {}
        assert cancelled.is_set()
        subscription.refresh_from_db()
        assert not subscription.active
        with patch("apps.mcp.events._queue_outbox"), patch("apps.mcp.tasks.post_signed") as callback:
            events.enqueue_inbox_event(create_message(context, platform_message_id="after-unsubscribe"))
            process_delivery(delivery.pk)
        callback.assert_not_called()
        assert EventOutbox.objects.count() == 1

    def test_concurrent_fanout_deduplicates(self, context, subscription):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        message = create_message(context)
        barrier = Barrier(2)

        def enqueue():
            barrier.wait(timeout=10)
            events.enqueue_inbox_event(message)

        with patch("apps.mcp.events._queue_outbox"), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.run_in_thread, enqueue) for _ in range(2)]
            for future in futures:
                future.result(timeout=15)
        assert EventOutbox.objects.count() == 1
