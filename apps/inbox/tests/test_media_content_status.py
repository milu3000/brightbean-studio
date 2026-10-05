"""Incomplete content state is retained separately from real metadata counts."""

import pytest
from django.utils import timezone

from apps.inbox.conversations import upsert_conversation_message
from apps.mcp.conversation_tools import _message

pytestmark = pytest.mark.django_db
PHOTO = {"type": "image", "url": "https://scontent.cdninstagram.com/synthetic-photo.jpg"}


@pytest.fixture(autouse=True)
def capture(settings, inbox_account, enroll_conversation_accounts):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    enroll_conversation_accounts(inbox_account, read=True)


def observe(account, extra, body=""):
    return upsert_conversation_message(
        account,
        platform_message_id="synthetic-content",
        sender_id="synthetic-peer",
        body=body,
        occurred_at=timezone.now(),
        source="poll",
        extra={
            "conversation_id": "synthetic-thread",
            "participant_ids": [account.account_platform_id, "synthetic-peer"],
            "message_recipient_id": account.account_platform_id,
            **extra,
        },
    )


def test_partial_content_survives_canonical_projection_and_text_only_poll(inbox_account):
    row = observe(inbox_account, {"is_unsupported": True, "attachments": [PHOTO]})
    assert row.content_status == "partial" and len(row.attachments) == 1
    assert _message(row)["content_status"] == "partial"
    row = observe(inbox_account, {}, body="Synthetic text")
    assert row.content_status == "partial" and len(row.attachments) == 1


def test_failed_content_request_does_not_add_a_fake_counted_attachment(inbox_account):
    observe(inbox_account, {"attachments": [PHOTO]})
    row = observe(inbox_account, {"content_fetch_status": "basic_fallback"})
    result = _message(row)
    assert result["content_status"] == "fields_unavailable"
    assert result["attachment_metadata_count"] == 1
    row = observe(inbox_account, {"content_fetch_status": "fields_requested", "attachments": [PHOTO]})
    assert row.content_status == "link_provided" and len(row.attachments) == 1


@pytest.mark.parametrize(
    "extra,status",
    [
        ({"is_unsupported": True}, "unsupported"),
        ({"content_fetch_status": "basic_fallback"}, "fields_unavailable"),
        ({}, "no_metadata"),
    ],
)
def test_status_only_observation_has_zero_retained_attachment_entries(inbox_account, extra, status):
    row = observe(inbox_account, extra)
    result = _message(row)
    assert row.content_status == status and result["content_status"] == status
    assert result["attachment_metadata_count"] == 0 and result["attachments"] == []


def test_withdrawn_content_cannot_return_through_a_later_successful_poll(inbox_account):
    observe(inbox_account, {"attachments": [PHOTO]})
    row = observe(inbox_account, {"is_deleted": True})
    row = observe(inbox_account, {"content_fetch_status": "fields_requested", "attachments": [PHOTO]})
    assert row.content_status == "removed" and row.attachments == [] and row.body == ""


def test_partial_evidence_survives_fallback_then_rich_request_without_explicit_support(inbox_account):
    row = observe(inbox_account, {"is_unsupported": True, "attachments": [PHOTO]})
    row = observe(inbox_account, {"content_fetch_status": "basic_fallback"})
    assert row.content_status == "partial"
    row = observe(inbox_account, {"content_fetch_status": "fields_requested", "attachments": [PHOTO]})
    assert row.content_status == "partial"
    row = observe(
        inbox_account, {"is_unsupported": False, "content_fetch_status": "fields_requested", "attachments": [PHOTO]}
    )
    assert row.content_status == "link_provided"


@pytest.mark.parametrize(
    "canonical,local", [("link_provided", "partial"), ("text", "unsupported"), ("partial", "link_provided")]
)
def test_merging_content_status_never_erases_known_incompleteness(canonical, local):
    from providers.meta_inbox_content import merge_content_status_evidence

    assert merge_content_status_evidence(local, canonical, body="Synthetic", attachments=[PHOTO]) == "partial"
