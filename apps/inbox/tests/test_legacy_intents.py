"""Synthetic public actions reuse stored targets and durable receipts."""

import json
from unittest.mock import patch

import pytest
from django.test import Client

from apps.api_keys.services import issue_api_key
from apps.inbox.models import InboxReply
from apps.inbox.tests.test_unified_feed import account, legacy
from apps.inbox.tests.test_unified_feed import context as context

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def public(context):
    other = account(context, connection_status="connected")
    message = legacy(other)
    issued = issue_api_key(
        workspace=other.workspace,
        social_accounts=[other],
        issued_by=context.user,
        name="Synthetic public intent",
        permissions=["use_inbox", "reply_from_inbox"],
    )
    client = Client(HTTP_AUTHORIZATION="Bearer " + issued.plaintext_token)
    response = client.get(f"/api/v1/inbox/saved/targets/{message.pk}", secure=True)
    assert response.status_code == 200, response.content
    return client, message, response.json()["reference"]


def post(client, action, reference, body="Synthetic public answer"):
    payload = {"reference": reference}
    if action != "status":
        payload["body"] = body
    return client.post(
        f"/api/v1/inbox/saved/{action}", json.dumps(payload), content_type="application/json", secure=True
    )


def accepted(*args, **kwargs):
    kwargs["before_provider"]()
    return "synthetic-public-receipt"


def test_public_save_reopen_send_replay_and_status(public):
    client, message, reference = public
    saved = post(client, "draft", reference)
    assert saved.status_code == 200, saved.content
    reopened = client.get(f"/api/v1/inbox/saved/targets/{message.pk}", secure=True)
    assert reopened.json()["draft"]["body"] == "Synthetic public answer"
    reference = reopened.json()["reference"]
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        first = post(client, "send", reference)
        again = post(client, "send", reference)
    assert first.status_code == again.status_code == 200, (first.content, again.content)
    assert first.json()["receipt"]["id"] == again.json()["receipt"]["id"]
    assert provider.call_count == 1
    assert post(client, "status", reference).json()["transport_state"] == "provider_accepted"


def test_public_unknown_cannot_repeat_or_change_body(public):
    client, _message, reference = public

    def unknown(*args, **kwargs):
        kwargs["before_provider"]()
        raise TimeoutError("Synthetic transport timeout")

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=unknown) as provider:
        first = post(client, "send", reference)
        again = post(client, "send", reference)
        changed = post(client, "draft", reference, "Changed text")
    assert first.status_code == again.status_code == changed.status_code == 409
    assert provider.call_count == 1
    assert post(client, "status", reference).json()["transport_state"] == "unknown"
    assert InboxReply.objects.get(legacy_action_nonce__isnull=False).body == "Synthetic public answer"


def test_public_new_activity_requires_explicit_read(public):
    client, message, reference = public
    message.body = "Updated synthetic original"
    message.save(update_fields=["body"])
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response = post(client, "send", reference)
    assert response.status_code == 409
    provider.assert_not_called()


def test_public_status_assignment_changes_preserve_draft_reference(public):
    client, message, reference = public
    message.status = "resolved"
    message.save(update_fields=["status"])
    saved = post(client, "draft", reference)
    assert saved.status_code == 200, saved.content


def test_public_pretransport_failure_is_verified_unsent(public):
    client, _message, reference = public
    with patch(
        "apps.inbox.services._dispatch_to_platform", side_effect=NotImplementedError("Synthetic unsupported adapter")
    ) as provider:
        response = post(client, "send", reference)
        again = post(client, "send", reference)
    assert response.status_code == again.status_code == 409
    assert provider.call_count == 1
    reply = InboxReply.objects.get(legacy_action_nonce__isnull=False)
    assert reply.status == "failed" and reply.not_sent_verified


def test_public_accepted_replay_survives_later_activity(public):
    client, message, reference = public
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        first = post(client, "send", reference)
        message.body = "Later changed synthetic original"
        message.save(update_fields=["body"])
        again = post(client, "send", reference)
    assert first.status_code == again.status_code == 200, (first.content, again.content)
    assert provider.call_count == 1


def test_public_sequential_explicit_reads_create_distinct_actions(public):
    client, message, reference = public
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        first = post(client, "send", reference)
        reopened = client.get(f"/api/v1/inbox/saved/targets/{message.pk}", secure=True)
        second = post(client, "send", reopened.json()["reference"], "Synthetic next answer")
    assert first.status_code == second.status_code == 200, (first.content, second.content)
    assert first.json()["receipt"]["id"] != second.json()["receipt"]["id"]
    assert provider.call_count == 2


def test_public_old_service_cannot_resend_unknown(public):
    from apps.inbox.dm_send_gate import DMSendGateError
    from apps.inbox.services import send_reply_now

    client, _message, reference = public

    def unknown(*args, **kwargs):
        kwargs["before_provider"]()
        raise TimeoutError("Synthetic lost response")

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=unknown) as provider:
        assert post(client, "send", reference).status_code == 409
        reply = InboxReply.objects.get(legacy_action_nonce__isnull=False)
        with pytest.raises(DMSendGateError, match="original outcome"):
            send_reply_now(reply, authorization=lambda account: None)
    assert provider.call_count == 1


def test_public_receipt_write_failure_preserves_unknown_marker(public):
    client, _message, reference = public
    original = InboxReply.save

    def fail_receipt(reply, *args, **kwargs):
        if reply.status == "sent":
            raise RuntimeError("Synthetic receipt database interruption")
        return original(reply, *args, **kwargs)

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider,
        patch.object(InboxReply, "save", fail_receipt),
    ):
        first = post(client, "send", reference)
        again = post(client, "send", reference)
    assert first.status_code == again.status_code == 409
    assert provider.call_count == 1
    assert post(client, "status", reference).json()["transport_state"] == "unknown"


def test_public_simultaneous_same_reference_has_one_transport(public):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    client, _message, reference = public
    barrier = Barrier(2)

    def worker():
        close_old_connections()
        try:
            own_client = Client(**client.defaults)
            barrier.wait(timeout=10)
            response = post(own_client, "send", reference)
            return response.status_code, response.json()
        finally:
            close_old_connections()

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [pool.submit(worker) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert all(status in {200, 409} for status, value in results), results
    assert any(status == 200 for status, value in results), results
    assert len({value["receipt"]["id"] for status, value in results if status == 200}) == 1
    assert provider.call_count == 1
    assert post(client, "status", reference).json()["transport_state"] == "provider_accepted"


def test_public_mid_read_revocation_discards_draft(public):
    from apps.inbox.legacy_intents import read_action
    from apps.members.models import WorkspaceMembership

    client, message, reference = public
    assert post(client, "draft", reference).status_code == 200

    def revoke(target):
        result = read_action(target)
        WorkspaceMembership.objects.filter(workspace=message.workspace).delete()
        return result

    with patch("apps.inbox.legacy_intents.read_action", side_effect=revoke):
        response = client.get(f"/api/v1/inbox/saved/targets/{message.pk}", secure=True)
    assert response.status_code != 200
    assert b"Synthetic public answer" not in response.content


@pytest.mark.parametrize("verified", [True, False])
def test_legacy_dm_facade_retains_route_checks_and_replays_receipt(public, verified):
    client, message, _reference = public
    message.message_type = "dm"
    message.sender_handle = "synthetic-peer"
    message.extra = (
        {
            "conversation_id": "synthetic-legacy-direct",
            "conversation_type": "direct",
            "classification_reason": "participants_pair",
        }
        if verified
        else {"conversation_type": "group"}
    )
    message.save(update_fields=["message_type", "sender_handle", "extra"])
    response = client.get(f"/api/v1/inbox/saved/targets/{message.pk}", secure=True)
    assert response.status_code == 200, response.content
    reference = response.json()["reference"]
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        first = post(client, "send", reference)
        again = post(client, "send", reference)
    if verified:
        assert first.status_code == again.status_code == 200, (first.content, again.content)
        assert provider.call_count == 1
        assert post(client, "status", reference).json()["transport_state"] == "provider_accepted"
    else:
        assert first.status_code == again.status_code == 409, (first.content, again.content)
        provider.assert_not_called()
