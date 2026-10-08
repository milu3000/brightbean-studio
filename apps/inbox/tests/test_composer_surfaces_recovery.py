"""REST/MCP share typed canonical actions, current grants and receipt identity."""

import json
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.test import Client

from apps.api_keys.services import issue_api_key
from apps.inbox.models import InboxReply
from apps.inbox.tests.test_conversation_composer_recovery import composer as composer_fixture
from apps.inbox.tests.test_native_quote_recovery import native_provider, quote_row

composer = composer_fixture
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def surface(composer):
    issued = issue_api_key(
        workspace=composer.account.workspace,
        social_accounts=[composer.account],
        issued_by=composer.user,
        name="Synthetic composer test",
        permissions=["use_inbox", "reply_from_inbox"],
    )
    client = Client(HTTP_AUTHORIZATION=f"Bearer {issued.plaintext_token}")
    return client, issued.api_key


def get_state(client, composer):
    response = client.get(f"/api/v1/inbox/conversations/{composer.conversation.pk}/composer", secure=True)
    assert response.status_code == 200, response.content
    assert response.headers["Cache-Control"] == "private, no-store"
    return response.json()


def payload(state, **kwargs):
    return {
        "body": "Synthetic surface answer",
        "action_nonce": state["action_nonce"],
        "expected_revision": state["composer_revision"],
        "scope_token": state["scope_token"],
        **kwargs,
    }


def post(client, composer, action, value):
    return client.post(
        f"/api/v1/inbox/conversations/{composer.conversation.pk}/{action}",
        data=json.dumps(value),
        content_type="application/json",
        secure=True,
    )


def rpc(client, name, value):
    response = client.post(
        "/api/v1/mcp/",
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": value}}
        ),
        content_type="application/json",
        secure=True,
    )
    assert response.status_code == 200, response.content
    result = response.json()
    assert "error" not in result, result
    assert not result["result"]["isError"], result
    return json.loads(result["result"]["content"][0]["text"])


def test_rest_draft_mcp_clear_rest_send_omits_quote(composer, surface):
    client, _key = surface
    quote = quote_row(composer)
    draft = post(client, composer, "draft", payload(get_state(client, composer), quote_target_id=str(quote.pk)))
    assert draft.status_code == 200, draft.content
    state = rpc(client, "get_inbox_conversation_composer", {"conversation_id": str(composer.conversation.pk)})
    assert state["quote_target_id"] == str(quote.pk)
    cleared = rpc(
        client,
        "save_inbox_conversation_draft",
        {"conversation_id": str(composer.conversation.pk), **payload(state, quote_target_id=None)},
    )
    assert cleared["quote_target_id"] is None
    provider = native_provider("facebook")
    with (
        patch("apps.inbox.services.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
    ):
        sent = post(client, composer, "send", payload(get_state(client, composer)))
    assert sent.status_code == 200, sent.content
    assert sent.json()["status"] == "sent"
    assert "reply_to" not in provider._request.call_args.kwargs["json"]


def test_mcp_quoted_send_rest_same_nonce_replay_calls_provider_once(composer, surface):
    client, _key = surface
    quote = quote_row(composer, "outbound")
    value = payload(get_state(client, composer), quote_target_id=str(quote.pk))
    provider = native_provider("facebook")
    with (
        patch("apps.inbox.services.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
    ):
        sent = rpc(client, "send_inbox_conversation_reply", {"conversation_id": str(composer.conversation.pk), **value})
        replay = post(client, composer, "send", value)
    assert replay.status_code == 200, replay.content
    assert replay.json()["id"] == sent["id"]
    assert provider._request.call_count == 1
    assert provider._request.call_args.kwargs["json"]["reply_to"] == {"mid": quote.platform_message_id}


@pytest.mark.parametrize(
    "extra", [{"reply_to": {"mid": "raw-native-id"}}, {"quote_target_id": "raw-native-id"}, {"expected_revision": True}]
)
def test_untyped_payload_or_raw_provider_quote_id_rejected(composer, surface, extra):
    client, _key = surface
    response = post(client, composer, "draft", payload(get_state(client, composer), **extra))
    assert response.status_code == 422, response.content
    assert not InboxReply.objects.exists()


def test_draft_only_key_can_save_but_not_send(composer):
    issued = issue_api_key(
        workspace=composer.account.workspace,
        social_accounts=[composer.account],
        issued_by=composer.user,
        name="Synthetic draft only",
        permissions=["use_inbox"],
    )
    client = Client(HTTP_AUTHORIZATION=f"Bearer {issued.plaintext_token}")
    value = payload(get_state(client, composer))
    assert post(client, composer, "draft", value).status_code == 200
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response = post(client, composer, "send", value)
    assert response.status_code == 409, response.content
    provider.assert_not_called()


def test_retire_draft_surface_preserves_nonce_and_requires_current_revision(composer, surface):
    client, _key = surface
    created = post(client, composer, "draft", payload(get_state(client, composer))).json()
    state = get_state(client, composer)
    stale = {"reply_id": created["id"], "kind": "draft", "scope_token": state["scope_token"], "expected_revision": 0}
    assert post(client, composer, "retire", stale).status_code == 409
    retired = post(client, composer, "retire", {**stale, "expected_revision": state["composer_revision"]})
    assert retired.status_code == 200, retired.content
    assert InboxReply.objects.filter(pk=created["id"], retired_at__isnull=False).exists()
    successor = post(client, composer, "draft", payload(get_state(client, composer), action_nonce=str(uuid4())))
    assert successor.status_code == 200, successor.content
    assert successor.json()["id"] != created["id"]


def test_old_legacy_create_route_cannot_bypass_active_canonical_action(composer, surface):
    client, _key = surface
    created = post(client, composer, "draft", payload(get_state(client, composer))).json()
    reply = InboxReply.objects.get(pk=created["id"])
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response = client.post(
            f"/api/v1/inbox/{reply.inbox_message_id}/replies",
            data=json.dumps({"body": "Different bypass", "send": True}),
            content_type="application/json",
            secure=True,
        )
    assert response.status_code in {404, 409, 422}, response.content
    provider.assert_not_called()
    assert InboxReply.objects.count() == 1
