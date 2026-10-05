"""Media metadata stays visible without downloading or inventing missing media."""

from unittest.mock import Mock

import pytest

from providers.exceptions import APIError
from providers.facebook import FacebookProvider
from providers.instagram_login import InstagramLoginProvider
from providers.meta_inbox_content import merge_message_extra, message_content_status, normalize_attachments

CDN = "https://scontent.cdninstagram.com/preview.jpg"


def test_unsupported_content_is_explicit_without_fabricating_an_attachment():
    extra = {"message": {"is_unsupported": True}}
    assert normalize_attachments(extra) == []
    assert message_content_status(extra) == "unsupported"


def test_six_unidentified_photos_keep_six_metadata_items_without_replay_growth():
    extra = {"message": {"attachments": [{"type": "image", "payload": {}} for _ in range(6)]}}
    assert len(normalize_attachments(extra)) == 6
    merged = merge_message_extra(extra, extra)
    assert len(normalize_attachments(merged)) == 6
    assert all(item["availability"] == "unavailable" for item in normalize_attachments(merged))


def test_payload_preview_url_is_safely_retained_without_fetch():
    extra = {"attachments": [{"type": "image", "payload": {"preview_url": CDN}}]}
    item = normalize_attachments(extra)[0]
    assert item["preview_url"] == CDN and item["url"] == ""
    assert item["availability_reason"] == "preview_only"


@pytest.mark.parametrize(
    "preview", ["https://external.example.com/tracker", "http://127.0.0.1/a", CDN + "?access_token=secret"]
)
def test_untrusted_payload_preview_url_remains_unavailable(preview):
    item = normalize_attachments({"attachments": [{"type": "image", "payload": {"preview_url": preview}}]})[0]
    assert item["preview_url"] == "" and item["availability"] == "unavailable"


def test_metadata_missing_is_not_claimed_as_removed_or_unsupported():
    assert normalize_attachments({}) == []
    assert message_content_status({}) == "no_metadata"
    assert message_content_status({}, "Synthetic text") == "text"


@pytest.mark.parametrize("empty", [[], {"data": []}, {}])
def test_empty_attachment_edges_do_not_invent_an_attachment(empty):
    extra = {"attachments": empty, "shares": empty}
    assert normalize_attachments(extra) == []
    assert message_content_status(extra, "Synthetic text") == "text"


@pytest.mark.parametrize("reason", [[], {}, True, 100])
def test_untrusted_availability_reason_is_sanitized(reason):
    item = normalize_attachments({"attachments": [{"type": "image", "availability_reason": reason}]})[0]
    assert item["availability_reason"] == "missing_url"


def test_withdrawal_remains_explicit_and_does_not_resurrect_urls():
    extra = merge_message_extra({"attachments": [{"type": "image", "url": CDN}]}, {"message": {"is_deleted": True}})
    assert message_content_status(extra) == "removed"
    assert all(not item["url"] and not item["preview_url"] for item in normalize_attachments(extra))


def test_successful_rich_poll_replaces_old_fallback_warning_without_dropping_photo():
    old = merge_message_extra({}, {"content_fetch_status": "basic_fallback"})
    fresh = merge_message_extra(
        old, {"content_fetch_status": "fields_requested", "attachments": [{"type": "image", "url": CDN}]}
    )
    assert message_content_status(fresh) == "link_provided"
    assert len(normalize_attachments(fresh)) == 1
    assert normalize_attachments(fresh)[0]["url"] == CDN


def test_unsafe_url_reason_does_not_disclose_the_private_url():
    items = normalize_attachments({"attachments": [{"type": "share", "url": "https://example.com/p?token=secret"}]})
    assert items[0]["availability_reason"] == "unsafe_url"
    assert "secret" not in str(items)


@pytest.mark.parametrize("provider_type", [FacebookProvider, InstagramLoginProvider])
def test_polled_content_field_fallback_keeps_safe_reason(provider_type):
    error = APIError(
        "field", status_code=400, raw_response={"error": {"code": 100, "message": "nonexisting field (attachments)"}}
    )
    message = {"id": "synthetic-mid", "from": {"id": "synthetic-peer"}, "created_time": "2026-10-05T00:00:00+0000"}

    def response(body):
        return Mock(json=lambda: body)

    provider = provider_type({"page_id": "synthetic-owner", "ig_user_id": "synthetic-owner"})
    if provider_type is FacebookProvider:
        provider._request = Mock(
            side_effect=[response({"data": [{"id": "synthetic-thread"}]}), error, response({"data": [message]})]
        )
    else:
        provider._request = Mock(
            side_effect=[error, response({"data": [{"id": "synthetic-thread", "messages": {"data": [message]}}]})]
        )
    messages = provider._fetch_direct_messages("synthetic")
    assert messages[0].extra["content_fetch_status"] == "basic_fallback"
    assert message_content_status(messages[0].extra) == "fields_unavailable"
    assert "nonexisting" not in str(messages[0].extra)
