"""The UI, REST and MCP list call the same saved application service."""

import json
from unittest.mock import patch

import pytest
from django.core.serializers.json import DjangoJSONEncoder
from django.test import RequestFactory
from django.urls import reverse

from apps.api.routers.saved_inbox import SavedInboxFilters
from apps.api.routers.saved_inbox import list_inbox as rest_list
from apps.inbox import canonical_reads as reader
from apps.inbox import saved_reads
from apps.inbox.tests.test_unified_feed import account, legacy
from apps.inbox.tests.test_unified_feed import context as context
from apps.mcp.saved_inbox_tools import list_inbox as mcp_list
from apps.mcp.tools import get_tool

pytestmark = pytest.mark.django_db


def test_same_saved_rows_in_ui_rest_and_mcp(context, client):
    other = account(context)
    message = legacy(other, status="unread")
    context.key.api_key.social_accounts.add(other)
    args = {"account": str(other.pk)}
    request = RequestFactory().get("/api/v1/inbox/saved/", args)
    request.api_key = context.key.api_key
    request.auth = context.key.api_key
    request.workspace_membership = context.member
    request.user = context.user
    client.force_login(context.user)
    url = reverse("inbox:feed", kwargs={"workspace_id": other.workspace_id})
    with (
        patch("apps.inbox.native_thread_reads.read_native_thread") as native,
        patch("apps.inbox.services._dispatch_to_platform") as provider,
    ):
        ui = client.get(url, args)
        rest = rest_list(request, SavedInboxFilters(**args))
        mcp = mcp_list(args, {"api_key": context.key.api_key, "membership": context.member})
    assert ui.status_code == 200 and rest.status_code == 200
    rest_payload = json.loads(rest.content)
    mcp_payload = json.loads(mcp["content"][0]["text"])
    assert rest_payload == mcp_payload
    assert rest_payload["rows"] == json.loads(json.dumps(ui.context["unified_rows"], cls=DjangoJSONEncoder))
    assert [item["id"] for item in ui.context["unified_rows"]] == [str(message.pk)]
    expected = saved_reads.list_payload(saved_reads.read_list(context.scope, saved_reads.list_parameters(args)))
    assert rest_payload == expected
    assert rest_payload["filters"]["domain"] == "all"
    message.refresh_from_db()
    assert message.status == "unread"
    native.assert_not_called()
    provider.assert_not_called()


def test_catalog_presents_shared_list_without_source_choice(context):
    tool = get_tool("list_inbox")
    assert tool is not None
    assert tool.input_schema["properties"]["domain"]["default"] == "all"
    assert "source" not in tool.input_schema["properties"]
    assert tool.handler is mcp_list


def test_serialization_rechecks_snapshot_after_projection(context):
    other = account(context)
    message = legacy(other)
    result = saved_reads.read_list(context.scope, saved_reads.list_parameters({"account": str(other.pk)}))
    message.status = "resolved"
    message.save(update_fields=["status"])
    with pytest.raises(reader.CanonicalReadError):
        saved_reads.list_payload(result)


def test_key_cannot_select_account_outside_allowlist(context):
    other = account(context)
    legacy(other)
    with pytest.raises(reader.CanonicalReadError):
        saved_reads.read_list(
            reader.key_read_scope(context.key.api_key), saved_reads.list_parameters({"account": str(other.pk)})
        )


def test_rest_route_is_registered_under_existing_api_auth(context, client):
    other = account(context)
    message = legacy(other)
    context.key.api_key.social_accounts.add(other)
    response = client.get(
        "/api/v1/inbox/saved/",
        {"account": str(other.pk)},
        secure=True,
        HTTP_AUTHORIZATION="Bearer " + context.key.plaintext_token,
    )
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["rows"]] == [str(message.pk)]
    assert response["Cache-Control"] == "private, no-store"
