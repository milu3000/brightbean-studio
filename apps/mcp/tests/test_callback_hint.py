"""Host-only, authenticated setup diagnostics never authorize callback access."""

import json
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from apps.mcp.models import EventSubscription
from apps.mcp.modern import CAPABILITIES_KEY, MODERN_PROTOCOL_VERSION, VERSION_KEY
from apps.mcp.tests import test_events
from apps.mcp.tests.test_modern_contract import rpc
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

context = test_events.context
enabled = test_events.enabled
params = test_events.params
verify = test_events.verify
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def unconfigured(settings):
    settings.MCP_EVENTS_ALLOWED_CALLBACK_HOSTS = ["private-configured.example.test"]


def test_hint_is_only_normalized_candidate_host(context, params, verify, settings):
    params["delivery"]["url"] = "https://CALLBACK.EXAMPLE.TEST:443/private-path-token?token=private-query-token"
    with patch("socket.getaddrinfo") as dns, patch("apps.mcp.transport._log_mcp_audit") as audit:
        response = rpc(context, "events/subscribe", params)
    assert response.status_code == 200  # Valid RPC, callback application error.
    assert audit.call_args.kwargs["status_code"] == 400
    error = response.json()["error"]
    assert error["code"] == -32015
    assert error["data"] == {
        "reason": "callback_host_not_allowed",
        "candidateHost": "callback.example.test",
        "requiresApproval": True,
    }
    serialized = response.content.decode()
    for forbidden in (
        "https://",
        ":443",
        "private-path-token",
        "private-query-token",
        test_events.SECRET,
        "private-configured",
    ):
        assert forbidden not in serialized
    assert settings.MCP_EVENTS_ALLOWED_CALLBACK_HOSTS == ["private-configured.example.test"]
    assert not EventSubscription.objects.exists()
    verify.assert_not_called()
    dns.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "https://private-user:private-password@callback.example.test/path?secret=query",
        "http://callback.example.test/private-token",
        "https://127.0.0.1/private-token",
        "https://localhost/private-token",
        "https://callback.example.test/path#private-fragment",
        "https://callback.example.test/\nprivate-token",
        "https://callback.example.test\\@private-host.test/path",
    ],
)
def test_malformed_or_sensitive_url_not_reflected(context, params, verify, url):
    params["delivery"]["url"] = url
    response = rpc(context, "events/subscribe", params)
    error = response.json()["error"]
    assert error["data"] == {"reason": "invalid_callback_url"}
    assert "candidateHost" not in response.content.decode()
    assert "private-" not in response.content.decode()
    assert test_events.SECRET not in response.content.decode()
    verify.assert_not_called()


@pytest.mark.parametrize(
    "state",
    [
        "foreign_workspace",
        "unallowed_account",
        "revoked_key",
        "key_permission_removed",
        "inactive_owner",
        "archived_workspace",
    ],
)
def test_no_hint_without_current_scope(context, params, verify, state):
    if state in {"foreign_workspace", "unallowed_account"}:
        workspace = context["workspace"]
        if state == "foreign_workspace":
            workspace = Workspace.objects.create(name="Other", organization=workspace.organization)
        account = SocialAccount.objects.create(
            workspace=workspace, platform="facebook", account_platform_id="foreign", account_name="Other"
        )
        if state == "foreign_workspace":
            context["api_key"].social_accounts.add(account)
        params["arguments"]["social_account_id"] = str(account.pk)
    elif state == "revoked_key":
        context["api_key"].revoked_at = timezone.now()
        context["api_key"].save(update_fields=["revoked_at"])
    elif state == "key_permission_removed":
        context["api_key"].permissions = []
        context["api_key"].save(update_fields=["permissions"])
    elif state == "inactive_owner":
        user = context["api_key"].issued_by
        user.is_active = False
        user.save(update_fields=["is_active"])
    else:
        context["workspace"].is_archived = True
        context["workspace"].save(update_fields=["is_archived"])
    response = rpc(context, "events/subscribe", params)
    assert response.status_code in {400, 401, 403}
    assert "candidateHost" not in response.content.decode()
    assert "callback.example.test" not in response.content.decode()
    assert "private-configured" not in response.content.decode()
    assert not EventSubscription.objects.exists()
    verify.assert_not_called()


@pytest.mark.parametrize("invalid", ["secret", "metadata", "event_name"])
def test_hint_requires_valid_subscribe_request(context, params, verify, invalid):
    kwargs = {}
    if invalid == "secret":
        params["delivery"]["secret"] = "not-a-signing-key"
    elif invalid == "metadata":
        kwargs["meta"] = {VERSION_KEY: MODERN_PROTOCOL_VERSION}
    else:
        params["name"] = "not.an.event"
    response = rpc(context, "events/subscribe", params, **kwargs)
    assert response.status_code == 400
    assert "candidateHost" not in response.content.decode()
    assert not EventSubscription.objects.exists()
    verify.assert_not_called()


def test_unauthenticated_request_has_no_callback_diagnostics(params):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "events/subscribe",
        "params": {**params, "_meta": {VERSION_KEY: MODERN_PROTOCOL_VERSION, CAPABILITIES_KEY: {}}},
    }
    response = Client().post(
        "/api/v1/mcp",
        json.dumps(body),
        content_type="application/json",
        secure=True,
        HTTP_MCP_PROTOCOL_VERSION=MODERN_PROTOCOL_VERSION,
        HTTP_MCP_METHOD="events/subscribe",
    )
    assert response.status_code == 401
    for forbidden in ("candidateHost", "callback.example.test", "private-configured", test_events.SECRET):
        assert forbidden not in response.content.decode()
