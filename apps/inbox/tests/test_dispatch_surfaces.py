"""Actual REST/MCP request adapters with synthetic identities and no network."""

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import RequestFactory

from apps.api.auth import _resolve_oauth_actor
from apps.api_keys.services import issue_api_key
from apps.inbox import reply_dispatch as dispatch
from apps.inbox.dispatch_access import dispatch_actor
from apps.inbox.models import ConversationWorkState, DMSendAttempt, InboxConversation, SendOperation
from apps.inbox.tests.test_dispatch_ownership import clock as clock  # noqa: F401
from apps.inbox.tests.test_dispatch_ownership import incoming, owner_cas, set_paused
from apps.inbox.tests.test_dispatch_ownership import owned as owned  # noqa: F401
from apps.inbox.tests.test_dm_send_gate import SecureClient
from apps.mcp.tests.test_oauth_auth import _mint_oauth_token
from apps.mcp.tools import all_tools

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def api_owned(owned):
    issued = issue_api_key(
        workspace=owned.account.workspace,
        social_accounts=[owned.account],
        issued_by=owned.user,
        name="synthetic-dispatch-owner",
        permissions=["use_inbox", "reply_from_inbox"],
    )
    dispatch.transfer_conversation_owner(
        owned.scope,
        **owner_cas(owned),
        new_owner_scope=f"key:{issued.api_key.pk}",
        authorization=owned.authorization,
    )
    owned.scope, owned.authorization = dispatch_actor(issued.api_key, None)
    owned.ownership.refresh_from_db()
    owned.clock.now += timedelta(seconds=1)
    owned.ownership = set_paused(owned, False)
    owned.row = incoming(owned)
    owned.api_key = issued.api_key
    owned.client = SecureClient(HTTP_AUTHORIZATION=f"Bearer {issued.plaintext_token}")
    return owned


def payload(owner):
    convo = InboxConversation.objects.get(pk=owner.row.conversation_id)
    state = ConversationWorkState.objects.get(conversation=convo)
    return {
        "conversation_id": str(convo.pk),
        "social_account_id": str(owner.account.pk),
        "platform": owner.account.platform,
        "target_message_id": str(owner.row.pk),
        "expected_revision": convo.revision,
        "expected_generation": state.generation,
        "expected_owner_epoch": owner.ownership.epoch,
        "body": "Synthetic V2 answer",
        "idempotency_key": "synthetic-surface-key",
    }


def call(owner, surface, action, args):
    if surface == "mcp":
        names = {
            "prepare": "prepare_conversation_reply",
            "claim": "claim_conversation_reply",
            "dispatch": "dispatch_conversation_reply",
            "get": "get_conversation_reply_operation",
        }
        response = owner.client.post(
            "/api/v1/mcp/",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": names[action], "arguments": args},
                }
            ),
            content_type="application/json",
        )
        data = response.json()
        if "result" in data:
            return response, json.loads(data["result"]["content"][0]["text"])
        return response, data
    args = dict(args)
    path = "/api/v1/conversation-replies/"
    if action == "prepare":
        path += "prepare"
    else:
        path += args.pop("operation_id")
        if action != "get":
            path += "/" + action
    response = (
        owner.client.get(path)
        if action == "get"
        else owner.client.post(path, json.dumps(args), content_type="application/json")
    )
    return response, response.json()


def claimed(owner, surface):
    response, prepared = call(owner, surface, "prepare", payload(owner))
    assert response.status_code == 200, prepared
    assert prepared["status"] == "prepared", prepared
    owner.clock.now += timedelta(seconds=6)
    response, result = call(owner, surface, "claim", {"operation_id": prepared["operation_id"]})
    assert response.status_code == 200 and result["status"] == "claimed", result
    return result


def dispatch_payload(result):
    return {
        "operation_id": result["operation_id"],
        "claim_token": result["claim_token"],
        "fencing_token": result["fencing_token"],
        "expected_owner_epoch": result["owner_epoch"],
        "acknowledge_observed_state": True,
    }


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_real_request_lifecycle_and_readback_do_not_expose_claim_or_body(api_owned, surface):
    result = claimed(api_owned, surface)
    with patch("apps.inbox.services._dispatch_to_platform", return_value="synthetic-v2-sent") as provider:
        response, sent = call(api_owned, surface, "dispatch", dispatch_payload(result))
    assert response.status_code == 200 and sent["status"] == "confirmed", sent
    assert provider.call_count == 1
    op = SendOperation.objects.get(pk=result["operation_id"])
    assert op.reply.status == "sent" and op.attempt.outcome == "sent"
    response, readback = call(api_owned, surface, "get", {"operation_id": str(op.pk)})
    assert response.status_code == 200 and readback["status"] == "confirmed", readback
    for secret in ("body", "claim_token", "idempotency_key", "owner_scope", "actor_scope"):
        assert secret not in readback
    assert readback["freshness_complete"] is False and readback["external_atomicity"] is False


@pytest.mark.parametrize("surface", ["rest", "mcp"])
@pytest.mark.parametrize("permission", ["use_inbox", "reply_from_inbox"])
def test_removed_current_permission_blocks_dispatch(api_owned, surface, permission):
    result = claimed(api_owned, surface)
    api_owned.api_key.permissions = [p for p in api_owned.api_key.permissions if p != permission]
    api_owned.api_key.save(update_fields=["permissions"])
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response, data = call(api_owned, surface, "dispatch", dispatch_payload(result))
    provider.assert_not_called()
    assert response.status_code in {200, 403, 409}, data
    assert "status" not in data or data["status"] != "confirmed"
    assert not DMSendAttempt.objects.exists()


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_observed_history_acknowledgement_is_required(api_owned, surface):
    result = claimed(api_owned, surface)
    args = dispatch_payload(result)
    args["acknowledge_observed_state"] = False
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response, data = call(api_owned, surface, "dispatch", args)
    provider.assert_not_called()
    assert response.status_code in {200, 409}, data
    assert not DMSendAttempt.objects.exists()


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_unknown_is_reported_and_never_replayed_as_another_send(api_owned, surface):
    result = claimed(api_owned, surface)
    with patch(
        "apps.inbox.services._dispatch_to_platform", side_effect=TimeoutError("private-provider-text")
    ) as provider:
        response, data = call(api_owned, surface, "dispatch", dispatch_payload(result))
    assert provider.call_count == 1
    assert "private-provider-text" not in json.dumps(data)
    op = SendOperation.objects.get(pk=result["operation_id"])
    assert op.status == "outcome_unknown" and op.attempt.outcome == "unknown"
    with patch("apps.inbox.services._dispatch_to_platform") as replay:
        call(api_owned, surface, "dispatch", dispatch_payload(result))
    replay.assert_not_called()


def test_default_off_hides_tools_and_rejects_stale_direct_route(api_owned, settings):
    settings.INBOX_REPLY_DISPATCH_ENABLED = False
    assert not any(t.name == "dispatch_conversation_reply" for t in all_tools())
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response, _ = call(api_owned, "rest", "prepare", payload(api_owned))
    assert response.status_code == 404
    provider.assert_not_called()


def test_same_production_key_authorizer_can_tighten_hold_when_all_rollout_flags_are_off(api_owned, settings):
    settings.INBOX_REPLY_DISPATCH_ENABLED = False
    settings.INBOX_REPLY_COORDINATION_ENABLED = False
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        owner = set_paused(api_owned, True)
    assert owner.paused is True
    provider.assert_not_called()


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_caller_cannot_supply_another_principal(api_owned, surface):
    data = payload(api_owned) | {"actor_scope": "oauth:another-principal"}
    response, result = call(api_owned, surface, "prepare", data)
    assert response.status_code in {200, 422}, result
    assert not SendOperation.objects.exists()


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_another_key_of_the_same_user_cannot_claim_or_read_private_operation(api_owned, surface):
    response, prepared = call(api_owned, surface, "prepare", payload(api_owned))
    assert response.status_code == 200 and prepared["status"] == "prepared", prepared
    other = issue_api_key(
        workspace=api_owned.account.workspace,
        social_accounts=[api_owned.account],
        issued_by=api_owned.user,
        name="synthetic-different-principal",
        permissions=["use_inbox", "reply_from_inbox"],
    )
    api_owned.client = SecureClient(HTTP_AUTHORIZATION=f"Bearer {other.plaintext_token}")
    api_owned.clock.now += timedelta(seconds=6)
    for action in ("claim", "get"):
        _, denied = call(api_owned, surface, action, {"operation_id": prepared["operation_id"]})
        assert "claim_token" not in denied and "status" not in denied, denied
    assert SendOperation.objects.get(pk=prepared["operation_id"]).status == "prepared"


@pytest.fixture
def oauth_owned(api_owned):
    api_owned.user.last_workspace_id = api_owned.account.workspace_id
    api_owned.user.save(update_fields=["last_workspace_id"])
    token = _mint_oauth_token(api_owned.user)
    actor = _resolve_oauth_actor(token)
    request = RequestFactory().post("/api/v1/mcp/", HTTP_AUTHORIZATION=f"Bearer {token}")
    dispatch.transfer_conversation_owner(
        api_owned.scope,
        **owner_cas(api_owned),
        new_owner_scope=f"oauth:{api_owned.user.pk}",
        authorization=api_owned.authorization,
    )
    api_owned.scope, api_owned.authorization = dispatch_actor(actor, request)
    api_owned.ownership.refresh_from_db()
    api_owned.clock.now += timedelta(seconds=1)
    api_owned.ownership = set_paused(api_owned, False)
    api_owned.row = incoming(api_owned)
    api_owned.client = SecureClient(HTTP_AUTHORIZATION=f"Bearer {token}")
    return api_owned


def test_oauth_mcp_runs_same_owned_lifecycle(oauth_owned):
    result = claimed(oauth_owned, "mcp")
    with patch("apps.inbox.services._dispatch_to_platform", return_value="synthetic-oauth-mid") as provider:
        _, sent = call(oauth_owned, "mcp", "dispatch", dispatch_payload(result))
    assert sent["status"] == "confirmed", sent
    assert provider.call_count == 1


def test_oauth_revocation_after_marker_prevents_actual_provider_entry(oauth_owned):
    from oauth2_provider.models import get_access_token_model

    from apps.inbox import dm_send_gate as gate

    result = claimed(oauth_owned, "mcp")
    original = gate._prepare_attempt

    def mark_then_revoke(*args, **kwargs):
        attempt = original(*args, **kwargs)
        get_access_token_model().objects.filter(user=oauth_owned.user).delete()
        return attempt

    with (
        patch.object(gate, "_prepare_attempt", side_effect=mark_then_revoke),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
    ):
        _, denied = call(oauth_owned, "mcp", "dispatch", dispatch_payload(result))
    provider.assert_not_called()
    assert "error" in denied
    operation = SendOperation.objects.get(pk=result["operation_id"])
    assert operation.attempt.outcome == "not_sent" and operation.status == "failed"
