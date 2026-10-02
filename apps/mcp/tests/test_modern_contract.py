"""Dated official MCP 2.0 schema + HTTP lifecycle conformance regressions.

The callback transport is mocked. No real subscription or signing key is used.
"""

import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import Client
from jsonschema import Draft202012Validator

from apps.mcp.modern import CAPABILITIES_KEY, MODERN_PROTOCOL_VERSION, SERVER_INFO_KEY, VERSION_KEY
from apps.mcp.tests import test_events

context = test_events.context
enabled = test_events.enabled
params = test_events.params
verify = test_events.verify

SCHEMA = json.loads((Path(__file__).parents[1] / "schemas" / "mcp-2026-07-28.json").read_text())
pytestmark = pytest.mark.django_db


def rpc(context, method, arguments=None, *, meta=None, headers=None, id_=1, body=None):
    parameters = dict(arguments or {})
    parameters["_meta"] = {VERSION_KEY: MODERN_PROTOCOL_VERSION, CAPABILITIES_KEY: {}} if meta is None else meta
    message = {"jsonrpc": "2.0", "id": id_, "method": method, "params": parameters}
    default_headers = {"HTTP_MCP_PROTOCOL_VERSION": MODERN_PROTOCOL_VERSION, "HTTP_MCP_METHOD": method}
    if method == "tools/call":
        default_headers["HTTP_MCP_NAME"] = parameters.get("name", "missing")
    default_headers.update(headers or {})
    client = Client(HTTP_AUTHORIZATION=context["request"].META["HTTP_AUTHORIZATION"])
    return client.post(
        "/api/v1/mcp",
        json.dumps(message if body is None else body),
        content_type="application/json",
        secure=True,
        **default_headers,
    )


def assert_schema(result, definition):
    Draft202012Validator({**SCHEMA, "$ref": f"#/$defs/{definition}"}).validate(result)
    assert result["resultType"] == "complete"
    assert result["_meta"][SERVER_INFO_KEY]["name"] == "brightbean-studio"


@pytest.mark.parametrize(
    "method,definition",
    [("server/discover", "DiscoverResult"), ("tools/list", "ListToolsResult"), ("ping", "EmptyResult")],
)
def test_official_complete_result_schemas(context, method, definition):
    response = rpc(context, method)
    assert response.status_code == 200
    result = response.json()["result"]
    assert_schema(result, definition)
    if method != "ping":
        assert result["cacheScope"] == "private"
        assert result["ttlMs"] == 0
    assert response.headers["Cache-Control"] == "private, no-store"


def test_tool_call_success_and_error_are_typed(context):
    success = rpc(context, "tools/call", {"name": "list_accounts", "arguments": {}})
    assert success.status_code == 200
    assert_schema(success.json()["result"], "CallToolResult")
    with patch("apps.mcp.transport.get_tool") as tool:
        tool.return_value.input_schema = {"type": "object"}
        tool.return_value.handler.return_value = {"content": [{"type": "text", "text": "Unavailable"}], "isError": True}
        response = rpc(context, "tools/call", {"name": "synthetic_tool", "arguments": {}})
    assert_schema(response.json()["result"], "CallToolResult")
    assert response.json()["result"]["isError"] is True


def test_full_event_lifecycle_has_complete_results(context, params, verify):
    for method, arguments in [("events/list", {}), ("events/subscribe", params), ("events/unsubscribe", params)]:
        response = rpc(context, method, arguments)
        assert response.status_code == 200, response.content
        assert_schema(response.json()["result"], "EmptyResult")
    verify.assert_called_once()


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        False,
        {},
        {VERSION_KEY: MODERN_PROTOCOL_VERSION},
        {CAPABILITIES_KEY: {}},
        {VERSION_KEY: 3, CAPABILITIES_KEY: {}},
        {VERSION_KEY: MODERN_PROTOCOL_VERSION, CAPABILITIES_KEY: []},
        {VERSION_KEY: MODERN_PROTOCOL_VERSION, CAPABILITIES_KEY: {"roots": True}},
        {VERSION_KEY: MODERN_PROTOCOL_VERSION, CAPABILITIES_KEY: {}, "io.modelcontextprotocol/logLevel": []},
    ],
)
def test_invalid_metadata_rejected_before_handler(context, metadata):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": metadata}}
    with patch("apps.mcp.transport.dispatch") as dispatch:
        response = rpc(context, "tools/list", body=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602
    dispatch.assert_not_called()


@pytest.mark.parametrize(
    "header,value",
    [
        ("HTTP_MCP_METHOD", "events/subscribe"),
        ("HTTP_MCP_METHOD", ""),
        ("HTTP_MCP_PROTOCOL_VERSION", "2025-03-26"),
        ("HTTP_MCP_PROTOCOL_VERSION", ""),
        ("HTTP_MCP_NAME", "other"),
        ("HTTP_MCP_NAME", "=?base64?!!!?="),
        ("HTTP_MCP_NAME", " padded "),
    ],
)
def test_header_mismatches_do_not_execute(context, header, value):
    with patch("apps.mcp.transport.dispatch") as dispatch:
        response = rpc(context, "tools/call", {"name": "list_accounts"}, headers={header: value})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    dispatch.assert_not_called()


def test_base64_header_decoded_before_comparison(context):
    encoded = "=?base64?" + base64.b64encode(b"list_accounts").decode() + "?="
    response = rpc(context, "tools/call", {"name": "list_accounts"}, headers={"HTTP_MCP_NAME": encoded})
    assert response.status_code == 200
    assert_schema(response.json()["result"], "CallToolResult")


def test_unknown_version_returns_supported_versions(context):
    response = rpc(
        context,
        "server/discover",
        meta={VERSION_KEY: "2099-01-01", CAPABILITIES_KEY: {}},
        headers={"HTTP_MCP_PROTOCOL_VERSION": "2099-01-01"},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == -32022
    assert error["data"] == {"requested": "2099-01-01", "supported": [MODERN_PROTOCOL_VERSION, "2025-03-26"]}


@pytest.mark.parametrize("method", ["unknown/method", "initialize", "events/poll", "events/stream"])
def test_unsupported_modern_methods_are_404(context, method):
    response = rpc(context, method)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == -32601


@pytest.mark.parametrize("params", [None, [], False, 0, "bad"])
def test_malformed_params_return_400_not_500(context, params):
    response = rpc(context, "tools/call", body={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602


def test_modern_batch_cannot_execute_any_side_effect(context):
    with patch("apps.mcp.transport.dispatch") as dispatch:
        response = rpc(context, "events/subscribe", body=[{"jsonrpc": "2.0", "id": 1, "method": "events/subscribe"}])
    assert response.status_code == 400
    dispatch.assert_not_called()


def test_notification_cannot_create_subscription(context, params, verify):
    body = {
        "jsonrpc": "2.0",
        "method": "events/subscribe",
        "params": {**params, "_meta": {VERSION_KEY: MODERN_PROTOCOL_VERSION, CAPABILITIES_KEY: {}}},
    }
    response = rpc(context, "events/subscribe", body=body)
    assert response.status_code == 400
    verify.assert_not_called()


def test_unknown_notification_has_no_response(context):
    response = rpc(
        context,
        "notifications/cancelled",
        body={"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 77}},
    )
    assert response.status_code == 202
    assert response.content == b""


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "https://evil.example",
        "https://testserver.evil.example",
        "https://testserver/path",
        "https://user@testserver",
    ],
)
def test_untrusted_origins_rejected(context, origin):
    with patch("apps.mcp.transport.dispatch") as dispatch:
        response = rpc(context, "tools/list", headers={"HTTP_ORIGIN": origin})
    assert response.status_code == 403
    dispatch.assert_not_called()


def test_same_origin_or_exact_allowlist(context, settings):
    assert rpc(context, "ping", headers={"HTTP_ORIGIN": "https://testserver"}).status_code == 200
    settings.MCP_ALLOWED_ORIGINS = ["https://approved.example"]
    assert rpc(context, "ping", headers={"HTTP_ORIGIN": "https://approved.example"}).status_code == 200
    assert rpc(context, "ping", headers={"HTTP_ORIGIN": "https://approved.example.evil"}).status_code == 403


def test_disabling_feature_retains_fallback_and_modern_cancellation(context, params, verify, settings):
    created = rpc(context, "events/subscribe", params)
    assert created.status_code == 200
    settings.MCP_EVENTS_ENABLED = False
    discover = rpc(context, "server/discover")
    assert discover.json()["error"]["code"] == -32601
    cancelled = rpc(context, "events/unsubscribe", params)
    assert cancelled.status_code == 200
    assert_schema(cancelled.json()["result"], "EmptyResult")


@pytest.mark.parametrize(
    "field,value",
    [
        ("ttlMs", True),
        ("ttlMs", "1000"),
        ("ttlMs", 1.1),
        ("ttlMs", -1),
        ("maxAgeMs", []),
        ("maxAgeMs", False),
        ("cursor", {}),
        ("unexpected", "value"),
    ],
)
def test_event_parameter_types_never_reach_callback(context, params, verify, field, value):
    response = rpc(context, "events/subscribe", {**params, field: value})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602
    verify.assert_not_called()


def test_max_age_is_accepted_without_replay(context, params, verify):
    response = rpc(context, "events/subscribe", {**params, "cursor": None, "maxAgeMs": 300000})
    assert response.status_code == 200
    assert response.json()["result"]["cursor"] is None
    assert response.json()["result"]["truncated"] is False


@pytest.mark.parametrize("version", ["2025-03-26", "2025-11-25", "2026-07-28"])
def test_legacy_initialize_never_negotiates_modern(context, version):
    response = rpc(
        context,
        "initialize",
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {}},
        },
        headers={"HTTP_MCP_PROTOCOL_VERSION": version},
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert "events" not in result["capabilities"]
    assert "resultType" not in result


@pytest.mark.parametrize("cursor", [None, 1, "arbitrary"])
def test_tools_list_cannot_silently_ignore_cursor(context, cursor):
    response = rpc(context, "tools/list", {"cursor": cursor})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602


def test_mocked_inbound_delivery_fetch_reply_and_echo_lifecycle(context, params, verify):
    """Exercise the complete server chain, not a claim of live ChatGPT waking."""
    from types import SimpleNamespace

    from django.core.cache import cache
    from django.utils import timezone

    from apps.inbox.models import InboxMessage, InboxReply
    from apps.inbox.tasks import InboxSyncEngine
    from apps.inbox.tests.test_ingestion_events import _messaging, _polled
    from apps.inbox.webhooks import _handle_facebook_messaging
    from apps.mcp.models import EventOutbox
    from apps.mcp.tasks import process_delivery

    key = context["api_key"]
    key.permissions = ["use_inbox", "reply_from_inbox"]
    key.save(update_fields=["permissions"])
    cache.clear()
    subscribed = rpc(context, "events/subscribe", params)
    assert subscribed.status_code == 200
    account = context["account"]
    # Advance the occurrence time past subscription creation at millisecond
    # precision, without relying on sleeps or rewriting the stored timestamp.
    now = timezone.now()
    with patch("apps.inbox.tasks.InboxSyncEngine._notify_new_message"):
        _handle_facebook_messaging(account, _messaging(mid="customer-new", timestamp=now.timestamp() * 1000))
    message = InboxMessage.objects.get()
    outbox = EventOutbox.objects.get()
    payload = json.loads(outbox.payload)
    assert payload["data"]["message_id"] == str(message.pk)
    assert set(payload["data"]) == {"message_id", "workspace_id", "social_account_id"}
    with patch("apps.mcp.tasks.post_signed", return_value=SimpleNamespace(status=204)) as callback:
        process_delivery(outbox.pk)
    outbox.refresh_from_db()
    assert outbox.status == "delivered"
    assert callback.call_args.args[3] == payload["eventId"]
    fetched = rpc(context, "tools/call", {"name": "get_inbox_message", "arguments": {"message_id": str(message.pk)}})
    assert fetched.status_code == 200
    assert json.loads(fetched.json()["result"]["content"][0]["text"])["id"] == str(message.pk)
    with patch("apps.inbox.services.get_provider") as provider:
        provider.return_value.reply_to_message.return_value = SimpleNamespace(platform_message_id="our-reply")
        sent = rpc(
            context,
            "tools/call",
            {"name": "send_reply", "arguments": {"message_id": str(message.pk), "body": "Approved test response"}},
        )
    assert sent.status_code == 200, sent.content
    assert InboxReply.objects.get().platform_reply_id == "our-reply"
    assert provider.return_value.reply_to_message.call_args.kwargs["human_agent"] is False
    with patch.object(InboxSyncEngine, "_notify_new_message") as notify:
        _handle_facebook_messaging(account, _messaging(mid="our-reply", sender="unexpected-echo-sender"))
        InboxSyncEngine()._upsert_message(account, _polled(mid="our-reply", sender="unexpected-echo-sender"))
    assert InboxMessage.objects.count() == 1
    assert EventOutbox.objects.count() == 1
    notify.assert_not_called()
    assert rpc(context, "events/unsubscribe", params).status_code == 200


@pytest.mark.parametrize("id_value", [None, True, False, [], {}, 1.2])
def test_invalid_request_ids_use_official_error_schema(context, id_value):
    response = rpc(context, "ping", id_=id_value)
    assert response.status_code == 400
    assert "id" not in response.json()
    Draft202012Validator({**SCHEMA, "$ref": "#/$defs/JSONRPCErrorResponse"}).validate(response.json())


@pytest.mark.parametrize("body", [[], [{"jsonrpc": "2.0", "id": 1, "method": "ping"}]])
def test_batch_error_uses_official_error_schema(context, body):
    response = rpc(context, "ping", body=body)
    assert response.status_code == 400
    assert "id" not in response.json()
    Draft202012Validator({**SCHEMA, "$ref": "#/$defs/JSONRPCErrorResponse"}).validate(response.json())


@pytest.mark.parametrize("notification", [False, True])
def test_disabled_modern_requests_cannot_fall_through_and_execute(context, settings, notification):
    settings.MCP_EVENTS_ENABLED = False
    body = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "send_reply", "arguments": {}, "_meta": {VERSION_KEY: "2099-01-01", CAPABILITIES_KEY: {}}},
    }
    if not notification:
        body["id"] = 1
    with patch("apps.mcp.transport.dispatch") as dispatch:
        response = rpc(
            context,
            "tools/call",
            headers={"HTTP_MCP_NAME": "wrong", "HTTP_MCP_PROTOCOL_VERSION": "2099-01-01"},
            body=body,
        )
    assert response.status_code == 400
    dispatch.assert_not_called()
    Draft202012Validator({**SCHEMA, "$ref": "#/$defs/JSONRPCErrorResponse"}).validate(response.json())
