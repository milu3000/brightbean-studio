"""Scoped, bounded attachment metadata pages; no provider or media requests."""

import json

import pytest

from apps.mcp.tests.test_conversation_tools import account as account  # noqa: F401
from apps.mcp.tests.test_conversation_tools import call, observation
from apps.mcp.tests.test_conversation_tools import conversation as conversation  # noqa: F401
from apps.mcp.tests.test_conversation_tools import flag as flag  # noqa: F401
from apps.mcp.tests.test_conversation_tools import full_client as full_client  # noqa: F401
from apps.mcp.tests.test_conversation_tools import memberships as memberships  # noqa: F401
from apps.mcp.tests.test_conversation_tools import other_account as other_account  # noqa: F401
from apps.mcp.tests.test_conversation_tools import user as user  # noqa: F401
from apps.mcp.tests.test_conversation_tools import workspace as workspace  # noqa: F401
from apps.mcp.tests.test_inbox_tools import _call

pytestmark = pytest.mark.django_db


def attachments(n):
    return [{"type": "image", "url": f"https://scontent.cdninstagram.com/synthetic-{i}.jpg"} for i in range(n)]


def test_history_truncation_links_to_complete_retained_metadata_pages(full_client, conversation):
    row = observation(conversation, "synthetic-six", attachments=attachments(6))
    history = call(full_client, "get_conversation_messages", {"conversation_id": str(conversation.pk)})
    item = history["items"][0]
    assert len(item["attachments"]) == 3 and item["attachments_truncated"]
    assert item["attachment_metadata_count"] == 6 and item["attachments_tool"] == "get_conversation_attachments"
    first = call(full_client, "get_conversation_attachments", {"message_id": str(row.pk), "limit": 4})
    second = call(
        full_client,
        "get_conversation_attachments",
        {"message_id": str(row.pk), "limit": 4, "cursor": first["next_cursor"]},
    )
    assert len(first["items"]) == 4 and len(second["items"]) == 2 and not second["has_more"]
    assert first["media_fetched"] is False and first["platform_media_complete"] is False


@pytest.mark.parametrize("mutation", ["other_message", "changed", "withdrawn"])
def test_cursor_cannot_cross_messages_or_stale_content(full_client, conversation, mutation):
    row = observation(conversation, "synthetic-page", attachments=attachments(6))
    page = call(full_client, "get_conversation_attachments", {"message_id": str(row.pk), "limit": 2})
    if mutation == "other_message":
        row = observation(conversation, "synthetic-other", attachments=attachments(6))
    elif mutation == "withdrawn":
        row.is_deleted = True
        row.save(update_fields=["is_deleted", "updated_at"])
    else:
        row.attachments = attachments(7)
        row.save(update_fields=["attachments", "updated_at"])
    _, result = _call(
        full_client, "get_conversation_attachments", {"message_id": str(row.pk), "cursor": page["next_cursor"]}
    )
    assert "error" in result
    if mutation == "withdrawn":
        fresh = call(full_client, "get_conversation_attachments", {"message_id": str(row.pk)})
        assert fresh["is_deleted"] and all(not item["url"] for item in fresh["items"])


def test_revoked_read_enrollment_denies_attachment_pages(full_client, conversation, settings):
    row = observation(conversation, "synthetic-page", attachments=attachments(6))
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    _, result = _call(full_client, "get_conversation_attachments", {"message_id": str(row.pk)})
    assert "error" in result


def test_attachment_pages_hide_provider_ids_and_unsafe_urls(full_client, conversation):
    row = observation(
        conversation,
        "synthetic-page",
        attachments=[
            {"type": "share", "id": "private-attachment-id", "url": "https://example.com/?token=private-token"}
        ],
    )
    result = call(full_client, "get_conversation_attachments", {"message_id": str(row.pk)})
    assert result["items"][0]["availability_reason"] == "unsafe_url"
    assert "private-token" not in json.dumps(result) and "private-attachment-id" not in json.dumps(result)


def test_first_oversized_attachment_is_bounded_without_truncating_a_url(full_client, conversation):
    url = "https://scontent.cdninstagram.com/" + "圖" * 8100
    row = observation(conversation, "synthetic-large", attachments=[{"type": "image", "url": url}])
    result = call(full_client, "get_conversation_attachments", {"message_id": str(row.pk)})
    assert len(json.dumps(result)) < 65536
    assert result["attachment_metadata_count"] == 1 and not result["has_more"]
    item = result["items"][0]
    assert item["metadata_truncated"] and item["availability_reason"] == "size_limited"
    assert item["url"] in {"", url} and item["preview_url"] in {"", url}
