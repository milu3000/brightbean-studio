"""Unlinked legacy default reads respect applied markers without inventing expiry."""

import json

import pytest
from django.utils import timezone

from apps.api.schemas import InboxMessageResponse
from apps.inbox.canonical_compat import legacy_body_search_query
from apps.inbox.canonical_content import legacy_content_restriction
from apps.inbox.canonical_send_target import canonical_projection_view
from apps.inbox.models import InboxMessage
from apps.inbox.tests.test_canonical_adapters_rebuilt import client_for, rpc
from apps.inbox.tests.test_canonical_reads_rebuilt import context as _context
from apps.inbox.tests.test_canonical_reads_rebuilt import legacy, row

context = _context
pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "marker,expected",
    [
        ({"is_deleted": True}, "removed"),
        ({"message": {"is_deleted": True}}, "removed"),
        ({"content_status": "expired"}, "expired"),
        ({"inbox_content_status": "expired"}, "expired"),
    ],
)
def test_unlinked_legacy_read_and_search_redact_applied_restrictions(context, settings, marker, expected):
    settings.INBOX_CANONICAL_READ_ENABLED = False
    message = InboxMessage.objects.create(
        workspace=context.account.workspace,
        social_account=context.account,
        platform_message_id="unlinked",
        message_type="dm",
        body="PRIVATE ORIGINAL",
        received_at=timezone.now(),
        extra={
            "attachments": [{"type": "image", "url": "https://example.com/PRIVATE-ATTACHMENT"}],
            "image_url": "https://example.com/PRIVATE-ALTERNATE",
            **marker,
        },
    )
    stored_body = message.body
    value = InboxMessageResponse.from_message(message).model_dump(mode="json")
    assert value["body"] == "" and value["attachments"] == [] and value["content_status"] == expected
    assert "PRIVATE" not in json.dumps(value)
    assert not InboxMessage.objects.filter(legacy_body_search_query("PRIVATE ORIGINAL")).exists()
    direct = client_for(context).get(f"/api/v1/inbox/{message.pk}", secure=True)
    assert direct.status_code == 200 and "PRIVATE" not in direct.content.decode()
    assert "PRIVATE" not in json.dumps(rpc(context, "get_inbox_message", {"message_id": str(message.pk)}))
    message.refresh_from_db()
    assert message.body == stored_body and message.extra == {
        "attachments": [{"type": "image", "url": "https://example.com/PRIVATE-ATTACHMENT"}],
        "image_url": "https://example.com/PRIVATE-ALTERNATE",
        **marker,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"is_deleted": False},
        {"is_deleted": "true"},
        {"is_deleted": "false"},
        {"is_deleted": 1},
        {"message": {"is_deleted": "true"}},
        {"message": {"is_deleted": 1}},
        {"message": None},
        {"content_status": True},
        {"content_status": "EXPIRED"},
        {"expires_at": "2000-01-01"},
    ],
)
def test_missing_or_malformed_markers_never_create_restriction_or_drop_search(context, extra):
    message = InboxMessage.objects.create(
        workspace=context.account.workspace,
        social_account=context.account,
        platform_message_id="ordinary",
        message_type="dm",
        body="VISIBLE ORIGINAL",
        received_at=timezone.now(),
        extra=extra,
    )
    assert legacy_content_restriction(extra) == ""
    assert canonical_projection_view(message).body == "VISIBLE ORIGINAL"
    assert InboxMessage.objects.filter(legacy_body_search_query("VISIBLE ORIGINAL")).get().pk == message.pk


def test_legacy_applied_marker_cannot_be_undone_by_live_shadow(context):
    canonical = row(context, direction="inbound", body="CURRENT COPY")
    message = legacy(context, canonical)
    message.extra = {"message": {"is_deleted": True}}
    message.save(update_fields=["extra"])
    view = canonical_projection_view(message)
    assert view.body == "" and view.content_status == "removed"
    result = rpc(context, "get_inbox_message", {"message_id": str(message.pk)})
    assert result["body"] == "" and result["content_status"] == "removed"
    from apps.inbox import canonical_reads as reader

    assert reader.read_conversation(context.scope, context.conversation.pk)["messages"][0]["body"] == ""
    assert reader.list_conversations(context.scope, search="CURRENT COPY")["conversations"] == []
    from apps.inbox.models import ConversationMessage

    ConversationMessage.objects.filter(pk=canonical.pk).update(legacy_message=None)
    assert reader.read_message_body(context.scope, canonical.pk)["body"] == ""
