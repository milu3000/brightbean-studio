"""Non-text Meta message contracts, URL safety and compatible polling."""

from unittest.mock import Mock

import pytest

from providers.exceptions import APIError, RateLimitError, TokenExpiredError
from providers.facebook import FacebookProvider
from providers.instagram_login import InstagramLoginProvider
from providers.meta_inbox_content import (
    BASIC_MESSAGE_FIELDS,
    CONTENT_MESSAGE_FIELDS,
    merge_message_extra,
    normalize_attachments,
    request_with_content_fields,
    safe_attachment_url,
)

URL = "https://www.instagram.com/p/shared-post/"
CDN = "https://scontent.cdninstagram.com/photo.jpg"


def webhook(kind="ig_post", *, url=URL):
    return {
        "message": {
            "attachments": [{"type": kind, "payload": {"url": url, "title": "A post", "ig_post_media_id": "media-1"}}]
        }
    }


@pytest.mark.parametrize(
    "kind", ["share", "ig_post", "post", "reel", "ig_reel", "story", "ig_story", "story_mention", "fallback"]
)
def test_webhook_share_types(kind):
    attachment = normalize_attachments(webhook(kind))[0]
    assert attachment["type"] == "share"
    assert attachment["url"] == URL
    assert attachment["title"] == "A post"
    assert attachment["availability"] == "available"


def test_graph_shapes_and_optional_metadata():
    extra = {
        "attachments": {
            "data": [
                {"id": "photo", "image_data": {"url": CDN, "preview_url": CDN + "?size=small"}},
                {"name": "document.pdf", "file_url": "https://files.example.com/document.pdf"},
                {"video_data": {"url": "https://video.fbcdn.net/video.mp4"}},
            ]
        },
        "shares": {"data": [{"id": "post", "name": "Shared post", "type": "ig_post", "url": URL}]},
    }
    items = normalize_attachments(extra)
    assert [item["type"] for item in items] == ["image", "file", "video", "share"]
    assert items[0]["preview_url"] == CDN + "?size=small"
    assert items[-1]["title"] == "Shared post"


def test_new_and_legacy_transition_deduplicates_by_url_even_if_one_has_id():
    raw = {
        "message": {
            "attachments": [
                {"type": "share", "payload": {"url": URL}},
                {"type": "ig_post", "payload": {"url": URL, "ig_post_media_id": "media-1", "title": "Hello"}},
            ]
        }
    }
    assert len(normalize_attachments(raw)) == 1
    assert normalize_attachments(raw)[0]["title"] == "Hello"


def test_distinct_cdn_asset_ids_survive_and_rotating_signature_updates():
    def data(asset, signature):
        return {
            "type": "image",
            "payload": {"url": f"https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id={asset}&signature={signature}"},
        }

    extra = {"attachments": [data("first", "old"), data("second", "other"), data("first", "new")]}
    items = normalize_attachments(extra)
    assert len(items) == 2
    assert "signature=new" in items[0]["url"]


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "data:image/svg+xml,hello",
        "http://example.com/a",
        "//example.com/a",
        "https://user:secret@example.com/a",
        "https://127.0.0.1/a",
        "https://127.1/a",
        "https://0x7f.0.0.1/a",
        "https://[::1]/a",
        "https://localhost/a",
        "https://service.internal/a",
        "https://example.com:22/a",
        "https://example.com\\@localhost/a",
        "https://example.com/a\n",
        "https://example.com/a?access_token=secret",
        "https://example.com/a?%61ccess_token=secret",
        "https://example.com/a?client_secret=secret",
        "https://example.com/a#access_token=secret",
        None,
        {},
    ],
)
def test_unsafe_urls_are_not_links_or_previews(value):
    assert safe_attachment_url(value) == ""
    assert safe_attachment_url(value, preview=True) == ""


def test_external_images_are_explicit_links_not_automatic_tracking_previews():
    item = normalize_attachments(
        {"attachments": [{"type": "image", "payload": {"url": "https://external.example.com/pixel"}}]}
    )[0]
    assert item["url"] == "https://external.example.com/pixel"
    assert item["preview_url"] == ""


def test_host_suffix_spoof_cannot_be_preview():
    assert safe_attachment_url("https://cdninstagram.com.evil.com/a", preview=True) == ""
    assert safe_attachment_url("https://evilcdninstagram.com/a", preview=True) == ""


def test_missing_and_malformed_values_are_bounded_and_truthful():
    assert (
        normalize_attachments({"attachments": [None, "bad", {"type": []}, {"type": "share", "payload": {}}]})[-1][
            "availability"
        ]
        == "unavailable"
    )
    items = normalize_attachments({"shares": {"data": [{"url": f"https://example.com/{i}"} for i in range(100)]}})
    assert len(items) == 30
    assert normalize_attachments(None) == []


@pytest.mark.parametrize("reverse", [False, True])
def test_merge_retains_richer_metadata_in_both_orders(reverse):
    rich = webhook()
    basic = {"conversation_id": "conversation", "sender_id": "sender"}
    first, second = (basic, rich) if reverse else (rich, basic)
    merged = merge_message_extra(first, second)
    assert normalize_attachments(merged)[0]["url"] == URL
    assert merged["sender_id"] == "sender"
    assert len(normalize_attachments(merge_message_extra(merged, second))) == 1


def test_signed_url_refresh_is_not_replaced_by_original_webhook():
    old = {"attachments": [{"id": "a", "type": "image", "url": CDN + "?oh=old"}]}
    new = {"attachments": [{"id": "a", "type": "image", "url": CDN + "?oh=new"}]}
    assert normalize_attachments(merge_message_extra(old, new))[0]["url"].endswith("oh=new")


def test_explicit_deletion_is_not_undone_by_basic_poll():
    deleted = merge_message_extra(webhook(), {"message": {"is_deleted": True}})
    result = normalize_attachments(merge_message_extra(deleted, {"conversation_id": "c"}))
    assert result == []


def test_graph_story_link_is_a_share_but_reply_context_is_not_new_attachment():
    assert normalize_attachments({"story": {"id": "s", "link": URL}})[0]["url"] == URL
    assert normalize_attachments({"message": {"reply_to": {"story": {"id": "s", "url": URL}}}}) == []


@pytest.mark.parametrize(
    "field", ["shares", "attachments", "image_data", "video_data", "file_url", "name", "type", "url"]
)
def test_specific_unsupported_field_falls_back_once(field):
    error = APIError(
        "field",
        status_code=400,
        raw_response={
            "error": {
                "code": 100,
                "message": f"Tried accessing nonexisting field ({field}) on node type (ShadowIGMessage)",
            }
        },
    )
    request = Mock(side_effect=[error, "response"])
    assert (
        request_with_content_fields(
            request,
            "https://graph.instagram.com/message",
            access_token="synthetic",
            params={"fields": CONTENT_MESSAGE_FIELDS, "since": 1},
            basic_fields=BASIC_MESSAGE_FIELDS,
        )
        == "response"
    )
    assert request.call_count == 2
    assert request.call_args.kwargs["params"] == {"fields": BASIC_MESSAGE_FIELDS, "since": 1}


@pytest.mark.parametrize(
    "error",
    [
        APIError("permission", status_code=403, raw_response={"error": {"code": 200}}),
        APIError(
            "other invalid input", status_code=400, raw_response={"error": {"code": 100, "message": "Invalid sender"}}
        ),
        TokenExpiredError("expired"),
        RateLimitError("quota"),
    ],
)
def test_no_fallback_hides_auth_permission_or_quota_errors(error):
    request = Mock(side_effect=error)
    with pytest.raises(type(error)):
        request_with_content_fields(
            request,
            "https://graph.instagram.com/message",
            access_token="synthetic",
            params={"fields": CONTENT_MESSAGE_FIELDS},
            basic_fields=BASIC_MESSAGE_FIELDS,
        )
    assert request.call_count == 1


@pytest.mark.parametrize("provider_type", [FacebookProvider, InstagramLoginProvider])
def test_provider_poll_requests_content_and_preserves_it(provider_type):
    provider = provider_type({"page_id": "owner", "ig_user_id": "owner"})
    message = {
        "id": "m",
        "from": {"id": "customer", "username": "Customer"},
        "created_time": "2026-10-03T00:19:05+0000",
        "shares": {"data": [{"id": "p", "name": "Post", "type": "ig_post", "url": URL}]},
    }
    if provider_type is FacebookProvider:
        provider._request = Mock(
            side_effect=[Mock(json=lambda: {"data": [{"id": "c"}]}), Mock(json=lambda: {"data": [message]})]
        )
    else:
        provider._request = Mock(
            return_value=Mock(json=lambda: {"data": [{"id": "c", "messages": {"data": [message]}}]})
        )
    messages = provider._fetch_direct_messages("synthetic")
    assert messages[0].text == ""
    assert normalize_attachments(messages[0].extra)[0]["url"] == URL
    assert "shares{id,name,type,url}" in provider._request.call_args.kwargs["params"]["fields"]


def test_replayed_webhook_cannot_replace_refreshed_poll_url():
    original = {"message": {"attachments": [{"type": "image", "payload": {"id": "a", "url": CDN + "?oh=old"}}]}}
    refresh = {"attachments": {"data": [{"id": "a", "image_data": {"url": CDN + "?oh=new"}}]}}
    state = merge_message_extra({}, original)
    state = merge_message_extra(state, refresh)
    state = merge_message_extra(state, original)
    assert normalize_attachments(state)[0]["url"].endswith("oh=new")


def test_replayed_poll_cannot_replace_refreshed_webhook_url():
    original = {"attachments": {"data": [{"id": "a", "image_data": {"url": CDN + "?oh=old"}}]}}
    refresh = {"message": {"attachments": [{"type": "image", "payload": {"id": "a", "url": CDN + "?oh=new"}}]}}
    state = merge_message_extra({}, original)
    state = merge_message_extra(state, refresh)
    state = merge_message_extra(state, original)
    assert normalize_attachments(state)[0]["url"].endswith("oh=new")
