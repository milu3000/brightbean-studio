"""Offline, bounded recovery of Instagram Login's oversized legacy DM poll."""

from datetime import UTC, datetime
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest

from providers.exceptions import APIError, RateLimitError, TokenExpiredError
from providers.instagram_login import API_BASE, InstagramLoginProvider
from providers.meta_inbox_content import BASIC_MESSAGE_FIELDS, CONTENT_MESSAGE_FIELDS, message_content_status
from providers.meta_inbox_paging import MAX_POLL_REQUESTS, PAGE_LIMITS, is_page_size_rejection

TOKEN = "synthetic-only"
OWNER = "own"
PEER = "peer"
CDN = "https://scontent.cdninstagram.com/photo.jpg"
SCOPE = {
    "workspace_id": "10000000-0000-4000-8000-000000000001",
    "social_account_id": "20000000-0000-4000-8000-000000000001",
    "platform": "instagram_login",
}


@pytest.fixture(autouse=True)
def capture_off(settings):
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []


def response(data, *, path=None, after=None, paging=None):
    payload = {"data": data}
    if after is not None:
        payload["paging"] = {
            "next": f"{API_BASE}/{path}?" + urlencode({"after": after, "access_token": "discard-this-token"}),
            "cursors": {"after": after},
        }
    elif paging is not None:
        payload["paging"] = paging
    return Mock(json=lambda: payload)


def oversize(*, status=500, code=1):
    return APIError(
        "Synthetic provider refused this page",
        status_code=status,
        raw_response={"error": {"code": code, "message": "Please reduce the amount of data you're asking for"}},
    )


def unsupported():
    return APIError(
        "Synthetic unsupported content field",
        status_code=400,
        raw_response={"error": {"code": 100, "message": "Tried accessing nonexisting field (attachments)"}},
    )


def conversation(cid="thread", *, participants=True):
    result = {"id": cid}
    if participants:
        result["participants"] = {"data": [{"id": OWNER, "name": "Private owner"}, {"id": PEER}]}
    return result


def message(mid="incoming", sender=PEER, **extra):
    return {
        "id": mid,
        "from": {"id": sender},
        "message": "Synthetic message",
        "created_time": "2026-10-07T00:00:00+0000",
        **extra,
    }


def provider(*responses):
    result = InstagramLoginProvider({"ig_user_id": OWNER, "conversation_v2_scope": SCOPE})
    result._request = Mock(side_effect=responses)
    return result


@pytest.mark.parametrize("status", [400, 500])
def test_specific_graph_page_size_rejection_is_shared(status):
    error = oversize(status=status)
    assert is_page_size_rejection(error.status_code, error.raw_response["error"])


@pytest.mark.parametrize("code", [True, False, "1", 1.0, None, [], {}, 4, 100, 190, 200, 613])
def test_page_size_detector_rejects_unsafe_and_unrelated_codes(code):
    assert not is_page_size_rejection(500, oversize(code=code).raw_response["error"])


@pytest.mark.parametrize("status", [True, "500", 500.0, 200, 401, 403, 429, 502, None])
def test_page_size_detector_rejects_other_http_statuses(status):
    assert not is_page_size_rejection(status, oversize().raw_response["error"])


@pytest.mark.parametrize("text", [None, {}, [], "Server failed", "x" * 4097 + "please reduce the amount of data"])
def test_page_size_detector_requires_bounded_message(text):
    assert not is_page_size_rejection(500, {"code": 1, "message": text})


def test_successful_nested_fast_path_keeps_one_request_and_content():
    raw = message(attachments={"data": [{"id": "photo", "image_data": {"url": CDN}}]})
    client = provider(response([{**conversation(), "messages": {"data": [raw]}}]))
    result = client._fetch_direct_messages(TOKEN)
    assert len(result) == 1
    assert result[0].extra["inbox_attachments"][0]["url"] == CDN
    assert client._request.call_count == 1
    assert client._request.call_args.kwargs["params"] == {
        "fields": f"id,participants,messages{{{CONTENT_MESSAGE_FIELDS}}}"
    }


def test_nested_refusal_segments_both_edges_and_preserves_media_classification():
    first = message(attachments={"data": [{"id": "photo", "image_data": {"url": CDN}}]})
    client = provider(
        oversize(),
        response([conversation()], path="me/conversations", after="list-after"),
        response([first, message("outbound", OWNER)], path="thread/messages", after="message-after"),
        response([message(), message("second")]),
        response([conversation("other")]),
        response([message("third")]),
    )
    since = datetime(2026, 10, 1, tzinfo=UTC)
    result = client._fetch_direct_messages(TOKEN, since)
    assert [item.platform_message_id for item in result] == ["incoming", "second", "third"]
    assert result[0].extra["inbox_attachments"][0]["url"] == CDN
    assert all(item.extra["conversation_type"] == "direct" for item in result)
    assert all(
        not {"participants", "participant_ids", "message_recipient_id", "direction"} & item.extra.keys()
        for item in result
    )
    calls = client._request.call_args_list
    assert [call.args[1] for call in calls] == [
        f"{API_BASE}/me/conversations",
        f"{API_BASE}/me/conversations",
        f"{API_BASE}/thread/messages",
        f"{API_BASE}/thread/messages",
        f"{API_BASE}/me/conversations",
        f"{API_BASE}/other/messages",
    ]
    assert calls[1].kwargs["params"]["fields"] == "id,participants{id}"
    assert calls[3].kwargs["params"]["after"] == "message-after"
    assert calls[4].kwargs["params"]["after"] == "list-after"
    assert "after" not in calls[5].kwargs["params"]
    for call in calls:
        assert call.args[0] == "GET"
        assert call.kwargs["access_token"] == TOKEN
        assert call.kwargs["params"]["since"] == int(since.timestamp())
        assert "access_token" not in call.kwargs["params"]
        assert ",to" not in call.kwargs["params"]["fields"]


def test_message_resize_keeps_same_after_since_and_content_fields():
    client = provider(
        oversize(),
        response([conversation()]),
        response([message("first")], path="thread/messages", after="same-cursor"),
        oversize(),
        oversize(status=400),
        response([message("second")]),
    )
    since = datetime(2026, 10, 1, tzinfo=UTC)
    result = client._fetch_direct_messages(TOKEN, since)
    assert [item.platform_message_id for item in result] == ["first", "second"]
    retries = client._request.call_args_list[3:]
    assert [call.kwargs["params"]["limit"] for call in retries] == [50, 25, 10]
    for call in retries:
        assert call.args[1] == f"{API_BASE}/thread/messages"
        assert call.kwargs["params"] == {
            "since": int(since.timestamp()),
            "after": "same-cursor",
            "fields": CONTENT_MESSAGE_FIELDS,
            "limit": call.kwargs["params"]["limit"],
        }


def test_conversation_resize_keeps_the_conversation_cursor():
    client = provider(
        oversize(),
        response([], path="me/conversations", after="list-after"),
        oversize(),
        response([conversation()]),
        response([message()]),
    )
    assert len(client._fetch_direct_messages(TOKEN)) == 1
    failed, retry = client._request.call_args_list[2:4]
    assert failed.kwargs["params"] == {"fields": "id,participants{id}", "after": "list-after", "limit": 50}
    assert retry.kwargs["params"] == {"fields": "id,participants{id}", "after": "list-after", "limit": 25}


@pytest.mark.parametrize("status,code", [(500, 2), (500, 190), (400, 200), (401, 1), (403, 1), (429, 1)])
def test_unrelated_initial_errors_never_segment_or_resize(status, code):
    error = oversize(status=status, code=code)
    client = provider(error)
    with pytest.raises(APIError) as caught:
        client._fetch_direct_messages(TOKEN)
    assert caught.value is error
    assert client._request.call_count == 1


@pytest.mark.parametrize(
    "error", [APIError("ordinary server error", status_code=500), RateLimitError("quota"), TokenExpiredError("expired")]
)
def test_non_size_errors_during_recovery_are_not_retried(error):
    client = provider(oversize(), response([conversation()]), error)
    with pytest.raises(type(error)) as caught:
        client._fetch_direct_messages(TOKEN)
    assert caught.value is error
    assert client._request.call_count == 3


@pytest.mark.parametrize("edge", ["conversations", "messages"])
def test_minimum_page_refusal_propagates_the_provider_error(edge):
    original = oversize()
    replies = [original]
    if edge == "messages":
        replies.append(response([conversation()]))
    client = provider(*replies, *[original for _ in PAGE_LIMITS])
    with pytest.raises(APIError) as caught:
        client._fetch_direct_messages(TOKEN)
    assert caught.value is original
    attempts = client._request.call_args_list[len(replies) :]
    assert [call.kwargs["params"]["limit"] for call in attempts] == list(PAGE_LIMITS)


def test_unsupported_later_page_deduplicates_without_erasing_prior_attachments():
    rich = message(attachments={"data": [{"id": "photo", "image_data": {"url": CDN}}]})
    client = provider(
        oversize(),
        response([conversation()]),
        response([rich], path="thread/messages", after="next-page"),
        unsupported(),
        response([message(), message("second")], path="thread/messages", after="last-page"),
        response([message("third")]),
    )
    result = client._fetch_direct_messages(TOKEN)
    assert [item.platform_message_id for item in result] == ["incoming", "second", "third"]
    assert result[0].extra["inbox_attachments"][0]["url"] == CDN
    assert result[0].extra["content_fetch_status"] == "basic_fallback"
    assert message_content_status(result[0].extra, result[0].text) == "fields_unavailable"
    assert client._request.call_args_list[4].kwargs["params"] == {
        "fields": BASIC_MESSAGE_FIELDS,
        "after": "next-page",
        "limit": 50,
    }
    assert client._request.call_args_list[5].kwargs["params"]["fields"] == BASIC_MESSAGE_FIELDS


@pytest.mark.parametrize("participants", [None, {"data": [{"id": OWNER}, {"id": PEER}], "paging": {"next": "unread"}}])
def test_missing_or_partial_participants_stay_unknown(participants):
    thread = {"id": "thread", "participants": participants}
    client = provider(oversize(), response([thread]), response([message(to={"data": [{"id": OWNER}]})]))
    result = client._fetch_direct_messages(TOKEN)
    assert result[0].extra["conversation_type"] == "unknown"
    assert "participant_ids" not in result[0].extra
    assert "message_recipient_id" not in result[0].extra


def test_enrolled_segmented_poll_retains_verified_outbound_history(settings):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = [SCOPE]
    client = provider(oversize(), response([conversation()]), response([message(), message("sent", OWNER)]))
    result = client._fetch_direct_messages(TOKEN)
    assert [item.platform_message_id for item in result] == ["incoming", "sent"]
    assert result[1].extra["direction"] == "outbound"
    assert result[1].extra["participant_ids"] == [OWNER, PEER]
    assert result[1].extra["message_recipient_id"] == PEER
    assert result[1].extra["conversation_type"] == "direct"
    assert client._request.call_args.kwargs["params"]["fields"] == CONTENT_MESSAGE_FIELDS + ",to"


def test_enrollment_revocation_discards_earlier_outbound_and_identity(settings):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = [SCOPE]
    client = provider()
    replies = [
        oversize(),
        response([conversation()]),
        response([message("first"), message("sent-first", OWNER)], path="thread/messages", after="page2"),
        unsupported(),
        response([message("last"), message("sent-last", OWNER)]),
    ]

    def fetch(*args, **kwargs):
        result = replies.pop(0)
        if len(replies) == 1:
            settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
        if isinstance(result, Exception):
            raise result
        return result

    client._request.side_effect = fetch
    result = client._fetch_direct_messages(TOKEN)
    assert [item.platform_message_id for item in result] == ["first", "last"]
    assert all(not {"participant_ids", "message_recipient_id", "direction"} & item.extra.keys() for item in result)
    assert ",to" in client._request.call_args_list[2].kwargs["params"]["fields"]
    assert client._request.call_args_list[-1].kwargs["params"]["fields"] == BASIC_MESSAGE_FIELDS


def test_budget_includes_nested_field_retry_and_every_segmented_request():
    client = provider()
    count = 0

    def fetch(method, url, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            raise unsupported()
        if count == 2:
            raise oversize()
        if count == 3:
            return response([conversation()])
        return response([message(str(count))], path="thread/messages", after=f"cursor-{count}")

    client._request.side_effect = fetch
    with pytest.raises(APIError, match="request budget exhausted"):
        client._fetch_direct_messages(TOKEN)
    assert count == MAX_POLL_REQUESTS == 40
    assert client._request.call_count == 40


@pytest.mark.parametrize("edge", ["conversations", "messages"])
def test_repeated_cursor_fails_instead_of_returning_partial_success(edge):
    path = "me/conversations" if edge == "conversations" else "thread/messages"
    page = response([] if edge == "conversations" else [message()], path=path, after="repeated")
    replies = [oversize()]
    if edge == "messages":
        replies.append(response([conversation()]))
    client = provider(*replies, page, page)
    with pytest.raises(APIError, match="repeated cursor"):
        client._fetch_direct_messages(TOKEN)
    assert client._request.call_count == len(replies) + 2


@pytest.mark.parametrize(
    "paging",
    [
        {"next": "https://untrusted.example.com/v25.0/thread/messages?after=cursor"},
        {"next": f"{API_BASE}/other/messages?after=cursor"},
        {"next": f"{API_BASE}/thread/messages?after=cursor&after=second"},
        {"next": f"{API_BASE}/thread/messages?after=cursor", "cursors": {"after": "different"}},
        {"next": f"{API_BASE}/thread/messages?before=cursor"},
        {"next": f"{API_BASE}/thread/messages?after=invalid%20cursor"},
        {"next": f"{API_BASE}/thread/messages?after=cursor#fragment"},
        {"next": {}},
        {"next": ""},
        [],
    ],
)
def test_untrusted_or_broken_cursor_is_never_followed(paging):
    client = provider(oversize(), response([conversation()]), response([message()], paging=paging))
    with pytest.raises(APIError, match="pagination"):
        client._fetch_direct_messages(TOKEN)
    assert client._request.call_count == 3
    assert client._request.call_args.args[1] == f"{API_BASE}/thread/messages"


def test_known_owner_and_version_alias_are_validated_but_never_followed():
    advertised = f"https://graph.instagram.com/v26.0/{OWNER}/conversations?after=next&access_token=discard"
    client = provider(oversize(), response([], paging={"next": advertised}), response([]))
    assert client._fetch_direct_messages(TOKEN) == []
    assert client._request.call_args.args[1] == f"{API_BASE}/me/conversations"
    assert client._request.call_args.kwargs["params"] == {"fields": "id,participants{id}", "limit": 50, "after": "next"}


@pytest.mark.parametrize("edge", ["conversations", "messages"])
@pytest.mark.parametrize(
    "filter_query,has_since",
    [
        ("since=1", False),
        ("since=1", True),
        ("since=1790812800&since=1790812800", True),
        ("until=1790812800", False),
        ("until=1790812800", True),
    ],
)
def test_cursor_cannot_change_pinned_time_slice(edge, filter_query, has_since):
    path = "me/conversations" if edge == "conversations" else "thread/messages"
    replies = [oversize()]
    if edge == "messages":
        replies.append(response([conversation()]))
    replies.append(response([], paging={"next": f"{API_BASE}/{path}?after=next&{filter_query}"}))
    client = provider(*replies)
    since = datetime(2026, 10, 1, tzinfo=UTC) if has_since else None
    with pytest.raises(APIError, match="invalid pagination cursor or route"):
        client._fetch_direct_messages(TOKEN, since)
    assert client._request.call_count == len(replies)


def test_matching_cursor_since_keeps_original_fields_limit_and_token():
    since = datetime(2026, 10, 1, tzinfo=UTC)
    query = urlencode(
        {
            "after": "next",
            "since": int(since.timestamp()),
            "fields": "id",
            "limit": 1,
            "access_token": "discard-this-token",
        }
    )
    client = provider(
        oversize(),
        response([conversation()]),
        response([message("first")], paging={"next": f"{API_BASE}/thread/messages?{query}"}),
        response([message("second")]),
    )
    assert [item.platform_message_id for item in client._fetch_direct_messages(TOKEN, since)] == ["first", "second"]
    assert client._request.call_args.args[1] == f"{API_BASE}/thread/messages"
    assert client._request.call_args.kwargs == {
        "access_token": TOKEN,
        "params": {"after": "next", "since": int(since.timestamp()), "fields": CONTENT_MESSAGE_FIELDS, "limit": 50},
    }


def test_explicitly_truncated_page_without_cursor_fails_closed():
    client = provider(oversize(), response([], paging={"has_more": True}))
    with pytest.raises(APIError, match="missing continuation cursor"):
        client._fetch_direct_messages(TOKEN)
    assert client._request.call_count == 2


@pytest.mark.parametrize("cid", ["../wrong", "a/b", "..", "x?fields=token", "https://evil.example", {}, None])
def test_bad_conversation_ids_fail_without_constructing_provider_routes(cid):
    client = provider(oversize(), response([{"id": cid}]))
    with pytest.raises(APIError, match="invalid conversation identity"):
        client._fetch_direct_messages(TOKEN)
    assert client._request.call_count == 2


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": {}}, {"data": [None]}, {"data": [{}] * 51}])
def test_malformed_segmented_page_is_not_successful_empty_poll(payload):
    client = provider(oversize(), Mock(json=lambda: payload))
    with pytest.raises(APIError, match="invalid page payload"):
        client._fetch_direct_messages(TOKEN)


def test_incomplete_dm_walk_is_failed_even_when_comment_stream_succeeds():
    client = provider(
        oversize(),
        response([conversation()]),
        response([message()], path="thread/messages", after="unfinished"),
        APIError("Synthetic interrupted page", status_code=500),
    )
    client._fetch_media_comments = Mock(return_value=["synthetic comment"])
    assert client.get_messages(TOKEN) == ["synthetic comment"]
    assert client.last_inbox_stream_results["dm"] == {
        "status": "failed",
        "coverage": "unknown",
        "error_code": "provider_error",
    }
    assert client.last_inbox_stream_results["comment"]["status"] == "success"
