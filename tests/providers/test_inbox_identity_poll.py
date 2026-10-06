"""Ordinary Facebook polling verifies identity without V2 history capture."""

from unittest.mock import Mock

import pytest

from providers.exceptions import APIError
from providers.facebook import FacebookProvider


def response(data):
    return Mock(json=lambda: {"data": data})


def native_message(mid, sender):
    return {"id": mid, "from": {"id": sender}, "message": "Synthetic", "created_time": "2026-10-06T00:00:00+0000"}


@pytest.mark.parametrize(
    "participants,expected",
    [
        ({"data": [{"id": "own"}, {"id": "peer"}]}, "direct"),
        ({"data": [{"id": "own"}, {"id": "peer"}, {"id": "other"}]}, "group"),
        ({"data": [{"id": "own"}, {"id": "peer"}], "paging": {"next": "unread-next-page"}}, "unknown"),
        (None, "unknown"),
    ],
)
def test_bounded_identity_summary_is_available_with_capture_off(settings, participants, expected):
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
    provider = FacebookProvider({"page_id": "own"})

    def request(method, url, **kwargs):
        if url.endswith("/conversations"):
            assert kwargs["params"] == {"fields": "id,participants{id}"}
            return response([{"id": "thread", "participants": participants}])
        assert ",to" not in kwargs["params"]["fields"]
        return response([native_message("incoming", "peer"), native_message("outgoing", "own")])

    provider._request = Mock(side_effect=request)
    result = provider._fetch_direct_messages("synthetic-only")
    assert [item.platform_message_id for item in result] == ["incoming"]
    assert result[0].extra["conversation_type"] == expected
    assert "participant_ids" not in result[0].extra
    assert "participants" not in result[0].extra
    assert "message_recipient_id" not in result[0].extra
    assert provider._request.call_count == 2


def test_unsupported_identity_field_falls_back_to_inbound_with_unknown_identity(settings):
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    provider = FacebookProvider({"page_id": "own"})
    provider._request = Mock(
        side_effect=[
            APIError(
                "field unavailable",
                status_code=400,
                raw_response={"error": {"code": 100, "message": "Tried accessing nonexisting field (participants)"}},
            ),
            response([{"id": "thread"}]),
            response([native_message("incoming", "peer")]),
        ]
    )
    result = provider._fetch_direct_messages("synthetic-only")
    assert provider._request.call_args_list[1].kwargs["params"] == {"fields": "id"}
    assert result[0].extra["conversation_type"] == "unknown"
    assert result[0].extra["classification_reason"] == "participants_missing"
    assert provider._request.call_count == 3


@pytest.mark.parametrize("code", [4, 190, 200])
def test_auth_and_quota_failures_do_not_retry_or_expand_permissions(code):
    provider = FacebookProvider({"page_id": "own"})
    provider._request = Mock(side_effect=APIError("denied", raw_response={"error": {"code": code}}))
    with pytest.raises(APIError):
        provider._fetch_direct_messages("synthetic-only")
    assert provider._request.call_count == 1


@pytest.mark.parametrize("status", [None, 401, 403, 429, 500])
def test_field_like_body_with_non_field_http_status_never_retries(status):
    provider = FacebookProvider({"page_id": "own"})
    provider._request = Mock(
        side_effect=APIError(
            "denied",
            status_code=status,
            raw_response={"error": {"code": 100, "message": "Unsupported participants field"}},
        )
    )
    with pytest.raises(APIError):
        provider._fetch_direct_messages("synthetic-only")
    assert provider._request.call_count == 1
