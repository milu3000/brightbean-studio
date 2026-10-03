"""Opt-in native outbound history and independent Meta inbox stream results."""

from unittest.mock import Mock

import pytest

from providers.exceptions import APIError
from providers.facebook import FacebookProvider
from providers.instagram_login import InstagramLoginProvider
from providers.meta_inbox_content import BASIC_MESSAGE_FIELDS, CONTENT_MESSAGE_FIELDS, polled_message_extra

PROVIDERS = [FacebookProvider, InstagramLoginProvider]
OWNER = "owner-id"
PEER = "peer-id"


def _message(message_id="native-outbound", sender_id=OWNER, **fields):
    return {
        "id": message_id,
        "from": {"id": sender_id, "name": "A display name", "username": "a-handle"},
        "message": "Hello",
        "created_time": "2026-10-03T08:00:00+0000",
        **fields,
    }


def _provider(provider_type, messages, *, participants=None, credentials=None):
    provider = provider_type(credentials if credentials is not None else {"page_id": OWNER, "ig_user_id": OWNER})
    conversation = {"id": "provider-conversation-id", "messages": {"data": messages}}
    if participants is not None:
        conversation["participants"] = participants
    if provider_type is FacebookProvider:
        provider._request = Mock(
            side_effect=[Mock(json=lambda: {"data": [conversation]}), Mock(json=lambda: {"data": messages})]
        )
    else:
        provider._request = Mock(return_value=Mock(json=lambda: {"data": [conversation]}))
    return provider


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_disabled_preserves_legacy_skip_and_request_fields(settings, provider_type):
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    provider = _provider(
        provider_type,
        [_message("inbound", PEER, to={"data": [{"id": OWNER}]}), _message()],
        participants={"data": [{"id": OWNER}, {"id": PEER}]},
    )

    messages = provider._fetch_direct_messages("synthetic")

    assert [message.platform_message_id for message in messages] == ["inbound"]
    assert messages[0].extra == {
        "conversation_id": "provider-conversation-id",
        "sender_id": PEER,
        "inbox_attachments": [],
    }
    calls = provider._request.call_args_list
    if provider_type is FacebookProvider:
        assert calls[0].kwargs["params"] == {}
        assert calls[1].kwargs["params"]["fields"] == CONTENT_MESSAGE_FIELDS
    else:
        assert calls[0].kwargs["params"]["fields"] == f"id,participants,messages{{{CONTENT_MESSAGE_FIELDS}}}"


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_enabled_keeps_native_outbound_and_exact_pair_identity(settings, provider_type):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    provider = _provider(
        provider_type,
        [_message("inbound", PEER), _message()],
        participants={"data": [{"id": OWNER, "name": "Private owner name"}, {"id": PEER, "username": "handle"}]},
    )

    messages = provider._fetch_direct_messages("synthetic")

    assert [message.platform_message_id for message in messages] == ["inbound", "native-outbound"]
    inbound, outbound = messages
    assert inbound.extra["message_recipient_id"] == OWNER
    assert "direction" not in inbound.extra
    assert outbound.extra == {
        "conversation_id": "provider-conversation-id",
        "sender_id": OWNER,
        "message_recipient_id": PEER,
        "participant_ids": [OWNER, PEER],
        "direction": "outbound",
        "inbox_attachments": [],
    }
    assert "recipient_id" not in inbound.extra
    assert "recipient_id" not in outbound.extra
    assert ",to" in provider._request.call_args.kwargs["params"]["fields"]
    if provider_type is FacebookProvider:
        assert provider._request.call_args_list[0].kwargs["params"] == {"fields": "id,participants"}


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_message_to_single_recipient_is_sufficient_evidence(settings, provider_type):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    provider = _provider(provider_type, [_message(to={"data": [{"id": PEER, "name": "Not retained"}]})])

    extra = provider._fetch_direct_messages("synthetic")[0].extra

    assert extra["message_recipient_id"] == PEER
    assert extra["direction"] == "outbound"
    assert "to" not in extra
    assert "participant_ids" not in extra


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize(
    ("participants", "to"),
    [
        (None, None),
        ({"data": [{"username": "owner"}, {"username": "peer"}]}, None),
        ({"data": [{"id": OWNER}, {"id": PEER}, {"id": "third"}]}, {"data": [{"id": PEER}]}),
        (None, {"data": [{"id": PEER}, {"id": "third"}]}),
        (None, {"data": [{"username": "a-handle"}]}),
        (None, {"data": []}),
        ({"data": [{"id": OWNER}, {"id": PEER}], "paging": {"next": "more"}}, None),
        ({"data": [{"id": "other-owner"}, {"id": PEER}]}, None),
        ({"data": [{"id": OWNER}, {"id": PEER}]}, {"data": [{"id": "different-peer"}]}),
        ({"data": [{"id": OWNER}, {"id": PEER}]}, {"data": [{"id": PEER}, {"id": "third"}]}),
        ({"data": [{"id": OWNER}, {"id": PEER}]}, {"data": [{"id": False}]}),
        ({"data": [{"id": OWNER}, {"id": PEER}, {"id": PEER}]}, None),
        ({"data": [{"id": OWNER}, {"id": None}]}, None),
        (None, {"data": [{"id": OWNER}]}),
    ],
)
def test_unknown_ambiguous_and_group_recipient_is_never_guessed(settings, provider_type, participants, to):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    fields = {"to": to} if to is not None else {}
    provider = _provider(provider_type, [_message(**fields)], participants=participants)

    messages = provider._fetch_direct_messages("synthetic")

    assert len(messages) == 1
    assert messages[0].extra["direction"] == "outbound"
    assert "message_recipient_id" not in messages[0].extra
    assert "recipient_id" not in messages[0].extra
    assert messages[0].extra["conversation_id"] == "provider-conversation-id"
    # Do not leave a misleading pair for downstream identity fallback either.
    assert len(messages[0].extra.get("participant_ids", [])) != 2


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_unknown_own_id_never_marks_outbound(settings, provider_type):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    provider = _provider(provider_type, [_message()], credentials={})

    messages = provider._fetch_direct_messages("synthetic")

    assert len(messages) == 1
    assert "direction" not in messages[0].extra
    assert "message_recipient_id" not in messages[0].extra


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_unsupported_attachment_fallback_preserves_requested_identity_fields(settings, provider_type):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    provider = _provider(provider_type, [_message(to={"data": [{"id": PEER}]})])
    request = provider._request
    error = APIError(
        "unsupported attachment field",
        raw_response={"error": {"code": 100, "message": "Tried accessing nonexisting field (attachments)"}},
    )
    if provider_type is FacebookProvider:
        responses = list(request.side_effect)
        provider._request = Mock(side_effect=[responses[0], error, responses[1]])
        expected_fields = BASIC_MESSAGE_FIELDS + ",to"
    else:
        provider._request = Mock(side_effect=[error, request.return_value])
        expected_fields = f"id,participants,messages{{{BASIC_MESSAGE_FIELDS},to}}"

    assert provider._fetch_direct_messages("synthetic")[0].extra["message_recipient_id"] == PEER
    assert provider._request.call_args.kwargs["params"]["fields"] == expected_fields


def test_identity_projection_does_not_copy_raw_participants_or_recipient_metadata():
    extra = polled_message_extra(
        _message(to={"data": [{"id": PEER, "access_token": "not-a-real-token"}]}),
        conversation_id="conversation",
        sender_id=OWNER,
        own_id=OWNER,
        participant_ids=[OWNER, PEER],
    )
    assert set(extra) == {
        "conversation_id",
        "sender_id",
        "direction",
        "participant_ids",
        "message_recipient_id",
        "inbox_attachments",
    }
    assert "not-a-real-token" not in str(extra)


@pytest.mark.parametrize("provider_type", PROVIDERS)
@pytest.mark.parametrize("failed_stream", ["dm", "comment"])
def test_stream_failure_is_preserved_when_other_stream_returns_messages(provider_type, failed_stream):
    provider = provider_type()
    dm = Mock(return_value=["a-dm"])
    comments = Mock(return_value=["a-comment"])
    (dm if failed_stream == "dm" else comments).side_effect = APIError("private raw error")
    provider._fetch_direct_messages = dm
    setattr(
        provider, "_fetch_post_comments" if provider_type is FacebookProvider else "_fetch_media_comments", comments
    )

    assert provider.get_messages("synthetic") == (["a-comment"] if failed_stream == "dm" else ["a-dm"])

    successful_stream = "comment" if failed_stream == "dm" else "dm"
    assert provider.last_inbox_stream_results == {
        failed_stream: {"status": "failed", "coverage": "unknown", "error_code": "provider_error"},
        successful_stream: {"status": "success", "coverage": "partial", "error_code": ""},
    }


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_empty_bounded_fetch_is_partial_and_stream_results_reset_each_call(provider_type):
    provider = provider_type()
    provider._fetch_direct_messages = Mock(side_effect=APIError("dm failed"))
    comments = Mock(return_value=[])
    setattr(
        provider, "_fetch_post_comments" if provider_type is FacebookProvider else "_fetch_media_comments", comments
    )
    with pytest.raises(APIError, match="dm failed"):
        provider.get_messages("synthetic")
    assert provider.last_inbox_stream_results["dm"]["status"] == "failed"
    assert provider.last_inbox_stream_results["comment"] == {
        "status": "success",
        "coverage": "partial",
        "error_code": "",
    }

    provider.last_inbox_stream_results["stale"] = {"status": "failed"}
    provider._fetch_direct_messages = Mock(return_value=[])
    assert provider.get_messages("synthetic") == []
    assert provider.last_inbox_stream_results == {
        "dm": {"status": "success", "coverage": "partial", "error_code": ""},
        "comment": {"status": "success", "coverage": "partial", "error_code": ""},
    }


@pytest.mark.parametrize("provider_type", PROVIDERS)
def test_both_stream_failures_report_unknown_coverage_before_raising(provider_type):
    provider = provider_type()
    provider._fetch_direct_messages = Mock(side_effect=APIError("dm failed"))
    setattr(
        provider,
        "_fetch_post_comments" if provider_type is FacebookProvider else "_fetch_media_comments",
        Mock(side_effect=APIError("comments failed")),
    )

    with pytest.raises(APIError, match="dm failed"):
        provider.get_messages("synthetic")

    assert provider.last_inbox_stream_results == {
        "dm": {"status": "failed", "coverage": "unknown", "error_code": "provider_error"},
        "comment": {"status": "failed", "coverage": "unknown", "error_code": "provider_error"},
    }
