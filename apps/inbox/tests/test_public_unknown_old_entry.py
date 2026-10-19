"""Same proved public root cannot bypass UNKNOWN using an older child ID."""

from unittest.mock import patch

import pytest

from apps.inbox.dm_send_gate import DMSendGateError, session_send_authorization
from apps.inbox.services import ReplyStateError, create_reply_draft, send_reply_now
from apps.inbox.tests.test_legacy_intents import post
from apps.inbox.tests.test_legacy_intents import public as public
from apps.inbox.tests.test_unified_feed import context as context
from apps.inbox.tests.test_unified_feed import legacy

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("same_root", [True, False])
def test_old_child_target_cannot_bypass_public_unknown(public, same_root):
    client, message, _reference = public
    message.extra = {"post_id": "synthetic-full-post", "parent_id": ""}
    message.save(update_fields=["extra"])
    reference = client.get(f"/api/v1/inbox/saved/targets/{message.pk}", secure=True).json()["reference"]

    def timeout(*args, **kwargs):
        kwargs["before_provider"]()
        raise TimeoutError("Synthetic uncertain public send")

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=timeout):
        assert post(client, "send", reference).status_code == 409
    uncertain = message.replies.get(status="unknown")
    child = legacy(
        message.social_account,
        extra={"post_id": "synthetic-full-post", "parent_id": message.platform_message_id if same_root else ""},
    )
    reply = create_reply_draft(message=child, body="Synthetic bypass via older entry", author=uncertain.author)
    with patch("apps.inbox.services._dispatch_to_platform", return_value="synthetic-bypass-receipt") as provider:
        if same_root:
            with pytest.raises((ReplyStateError, DMSendGateError), match="unknown"):
                send_reply_now(
                    reply, actor=uncertain.author, authorization=session_send_authorization(uncertain.author)
                )
        else:
            result = send_reply_now(
                reply, actor=uncertain.author, authorization=session_send_authorization(uncertain.author)
            )
            assert result.status == "sent"
    assert provider.call_count == (0 if same_root else 1)


def test_classic_public_send_keeps_unknown_receipt_and_blocks_old_retry(public, context):
    from apps.inbox.services import send_reply

    _client, message, _reference = public

    def timeout(*args, **kwargs):
        kwargs["before_provider"]()
        raise TimeoutError("Synthetic public platform timeout")

    authorization = session_send_authorization(context.user)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=timeout) as provider:
        with pytest.raises(DMSendGateError) as caught:
            send_reply(
                message=message,
                body="Synthetic classic public answer",
                author=context.user,
                authorization=authorization,
            )
        assert caught.value.code == "outcome_unknown"
        reply = message.replies.get()
        assert reply.status == "unknown" and reply.send_generation == 1
        assert reply.legacy_action_nonce is None
        with pytest.raises(DMSendGateError, match="original outcome"):
            send_reply_now(reply, actor=context.user, authorization=authorization)
    assert provider.call_count == 1


@pytest.fixture
def unknown_public_child(public, context):
    client, message, _reference = public
    message.extra = {"post_id": "synthetic-full-post", "parent_id": ""}
    message.save(update_fields=["extra"])
    reference = client.get(f"/api/v1/inbox/saved/targets/{message.pk}", secure=True).json()["reference"]

    def timeout(*args, **kwargs):
        kwargs["before_provider"]()
        raise TimeoutError("Synthetic uncertain public send")

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=timeout):
        assert post(client, "send", reference).status_code == 409
    child = legacy(
        message.social_account, extra={"post_id": "synthetic-full-post", "parent_id": message.platform_message_id}
    )
    reply = create_reply_draft(message=child, body="Synthetic old child answer", author=context.user)
    return client, message, child, reply


@pytest.mark.parametrize("surface", ["ui", "rest", "mcp"])
def test_same_root_unknown_is_enforced_at_real_old_surface(unknown_public_child, context, surface):
    import json

    from django.test import Client

    client, message, _child, reply = unknown_public_child
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        if surface == "ui":
            session = Client()
            session.force_login(context.user)
            response = session.post(f"/workspace/{message.workspace_id}/inbox/replies/{reply.pk}/send/", secure=True)
            assert response.status_code == 200 and response["HX-Reply-Failed"] == "1"
            assert b"unknown" in response.content.lower()
        elif surface == "rest":
            response = client.post(f"/api/v1/inbox/replies/{reply.pk}/send", secure=True)
            assert response.status_code == 409, response.content
            assert "unknown" in response.json()["detail"].lower()
        else:
            response = client.post(
                "/api/v1/mcp/",
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "send_reply", "arguments": {"reply_id": str(reply.pk)}},
                    }
                ),
                content_type="application/json",
                secure=True,
            )
            assert response.status_code == 200, response.content
            value = response.json()
            assert "unknown" in json.dumps(value).lower()
            assert "error" in value or value.get("result", {}).get("isError")
    provider.assert_not_called()
    reply.refresh_from_db()
    assert reply.status == "draft" and reply.send_generation == 0
    assert message.replies.get().status == "unknown"


def test_same_native_ids_in_other_authorized_account_are_not_held(unknown_public_child, context):
    from apps.inbox.tests.test_unified_feed import account

    _client, message, _child, _reply = unknown_public_child
    other = account(context, connection_status="connected")
    independent = legacy(other, platform_message_id=message.platform_message_id, extra=message.extra)
    reply = create_reply_draft(message=independent, body="Synthetic independent account answer", author=context.user)

    def accepted(*args, **kwargs):
        kwargs["before_provider"]()
        return "synthetic-other-account-receipt"

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        result = send_reply_now(reply, actor=context.user, authorization=session_send_authorization(context.user))
    assert result.status == "sent"
    assert result.inbox_message.social_account_id == other.pk
    assert message.replies.get().status == "unknown"
    provider.assert_called_once()


def test_old_rest_cannot_cross_key_account_allowlist(unknown_public_child, context):
    from apps.inbox.tests.test_unified_feed import account

    client, message, _child, _reply = unknown_public_child
    other = account(context, connection_status="connected")
    independent = legacy(other, platform_message_id=message.platform_message_id, extra=message.extra)
    reply = create_reply_draft(message=independent, body="Synthetic outside allowlist", author=context.user)
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response = client.post(f"/api/v1/inbox/replies/{reply.pk}/send", secure=True)
    assert response.status_code == 404, response.content
    provider.assert_not_called()
    reply.refresh_from_db()
    assert reply.status == "draft" and reply.send_generation == 0


def test_public_child_waits_for_account_lock_then_observes_committed_unknown(public, context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, get_ident

    from django.db import close_old_connections

    from apps.inbox import services
    from apps.inbox.models import InboxReply

    _client, message, _reference = public
    message.extra = {"post_id": "synthetic-full-post", "parent_id": ""}
    message.save(update_fields=["extra"])
    child = legacy(
        message.social_account, extra={"post_id": "synthetic-full-post", "parent_id": message.platform_message_id}
    )
    first_reply = create_reply_draft(message=message, body="Synthetic first uncertain action", author=context.user)
    child_reply = create_reply_draft(message=child, body="Synthetic competing child", author=context.user)
    entered, release, at_lock, finished = Event(), Event(), Event(), Event()
    second_thread = []
    original_lock = services.lock_dm_account

    def lock(*args, **kwargs):
        if second_thread and get_ident() == second_thread[0]:
            at_lock.set()
        return original_lock(*args, **kwargs)

    def provider(*args, **kwargs):
        kwargs["before_provider"]()
        entered.set()
        assert release.wait(timeout=10)
        raise TimeoutError("Synthetic lost public response")

    def worker(reply, second=False):
        close_old_connections()
        try:
            if second:
                second_thread.append(get_ident())
            try:
                send_reply_now(reply, actor=context.user, authorization=session_send_authorization(context.user))
            except DMSendGateError as exc:
                return exc.code
            return "unexpected_sent"
        finally:
            if second:
                finished.set()
            close_old_connections()

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=provider) as dispatch,
        patch.object(services, "lock_dm_account", side_effect=lock),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(worker, first_reply)
        try:
            assert entered.wait(timeout=10)
            assert InboxReply.objects.get(pk=first_reply.pk).status == "unknown"
            second = pool.submit(worker, child_reply, True)
            assert at_lock.wait(timeout=10)
            assert not finished.wait(timeout=0.2)
        finally:
            release.set()
        assert first.result(timeout=15) == "outcome_unknown"
        assert second.result(timeout=15) == "outcome_unknown"
    assert dispatch.call_count == 1
    child_reply.refresh_from_db()
    assert child_reply.status == "draft" and child_reply.send_generation == 0


def test_public_availability_uses_same_proved_root_hold_as_send(unknown_public_child):
    from apps.inbox.services import reply_send_availability

    _client, _message, child, reply = unknown_public_child
    availability = reply_send_availability(child, reply=reply)
    assert not availability["allowed"] and availability["code"] == "outcome_unknown"
    assert "DM sends" not in availability["reason"]


@pytest.mark.parametrize("verified_unsent", [False, True])
def test_old_failed_public_receipt_requires_verified_unsent_before_new_action(public, context, verified_unsent):
    from apps.inbox.models import InboxReply
    from apps.inbox.services import reply_send_availability

    client, message, _reference = public
    historic = InboxReply.objects.create(
        inbox_message=message,
        body="Synthetic historical failed action",
        status="failed",
        not_sent_verified=verified_unsent,
    )
    response = client.get(f"/api/v1/inbox/saved/targets/{message.pk}", secure=True)
    assert response.status_code == 200, response.content
    availability = reply_send_availability(message)

    def accepted(*args, **kwargs):
        kwargs["before_provider"]()
        return "synthetic-verified-next-receipt"

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        result = post(client, "send", response.json()["reference"], "Synthetic genuinely new action")
    if verified_unsent:
        assert availability["allowed"]
        assert result.status_code == 200, result.content
        provider.assert_called_once()
    else:
        assert not availability["allowed"] and availability["code"] == "outcome_unknown"
        assert result.status_code == 409, result.content
        assert response.json()["draft"]["id"] == str(historic.pk)
        provider.assert_not_called()
    historic.refresh_from_db()
    assert historic.status == "failed" and historic.body == "Synthetic historical failed action"


def test_reopen_prefers_original_unknown_receipt_over_later_child_draft(unknown_public_child):
    client, message, _child, child_reply = unknown_public_child
    original = message.replies.get(status="unknown")
    response = client.get(f"/api/v1/inbox/saved/targets/{message.pk}", secure=True)
    assert response.status_code == 200, response.content
    assert response.json()["draft"]["id"] == str(original.pk)
    status = post(client, "status", response.json()["reference"])
    assert status.json()["transport_state"] == "unknown"
    child_reply.refresh_from_db()
    assert child_reply.body == "Synthetic old child answer" and child_reply.status == "draft"


def test_earlier_other_reference_cannot_ignore_newly_accepted_own_reply(public):
    client, message, first_reference = public
    old_other_reference = client.get(f"/api/v1/inbox/saved/targets/{message.pk}", secure=True).json()["reference"]

    def accepted(*args, **kwargs):
        kwargs["before_provider"]()
        return "synthetic-own-context-receipt"

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        first = post(client, "send", first_reference)
        stale = post(client, "send", old_other_reference, "Synthetic reply based on old context")
    assert first.status_code == 200
    assert stale.status_code == 409
    assert provider.call_count == 1
