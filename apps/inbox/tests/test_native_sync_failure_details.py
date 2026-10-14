"""Synthetic reproduction of the reported Meta refusal and cursor stall."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from apps.inbox.tasks import InboxSyncEngine, _provider_failure_details
from apps.inbox.tests.test_durable_pages_recovery import durable as durable
from apps.inbox.tests.test_sync import connected_account as connected_account
from apps.inbox.tests.test_sync import workspace as workspace
from providers.exceptions import APIError
from providers.meta_inbox_paging import InboxPagingError, _pages


def refused():
    return APIError(
        "PRIVATE platform body",
        platform="Instagram (Direct)",
        status_code=400,
        raw_response={"error": {"code": 100, "error_subcode": 33, "message": "PRIVATE token", "fbtrace_id": "PRIVATE"}},
    )


def test_reported_meta_refusal_preserves_numeric_reason_without_private_body():
    assert _provider_failure_details(refused()) == (400, "request_rejected,code=100,subcode=33")


def test_repeated_cursor_is_bounded_and_is_not_success_or_unknown():
    url = "https://graph.instagram.com/v25.0/thread/messages"
    page = {"data": [{"id": "synthetic-mid"}], "paging": {"next": url + "?after=same", "cursors": {"after": "same"}}}
    request = Mock(return_value=Mock(json=lambda: page))
    pages = _pages(request, url, access_token="synthetic-only", params={}, platform="Instagram (Direct)")
    assert next(pages)[0] == page["data"]
    with pytest.raises(InboxPagingError) as caught:
        next(pages)
    assert request.call_count == 2
    assert _provider_failure_details(caught.value) == (None, "cursor_repeated")


@pytest.mark.django_db
def test_failed_poll_keeps_saved_history_and_logs_actual_numeric_refusal(connected_account, inbox_message, caplog):
    connected_account.platform = "instagram_login"
    connected_account.save(update_fields=["platform"])
    inbox_message.social_account = connected_account
    inbox_message.workspace = connected_account.workspace
    inbox_message.save(update_fields=["social_account", "workspace"])
    before = (inbox_message.body, inbox_message.status, inbox_message.platform_message_id)
    provider = MagicMock()
    provider.credentials = {}
    provider.last_inbox_stream_results = {
        "dm": {"status": "failed", "coverage": "unknown", "error_code": "provider_error"}
    }
    provider.get_messages.side_effect = refused()
    with patch("apps.inbox.tasks.get_provider", return_value=provider):
        InboxSyncEngine().sync_all()
    inbox_message.refresh_from_db()
    assert (inbox_message.body, inbox_message.status, inbox_message.platform_message_id) == before
    assert "status=400 reason=request_rejected,code=100,subcode=33" in caplog.text
    assert "PRIVATE" not in caplog.text
    assert provider.get_messages.call_count >= 1


@pytest.mark.django_db(transaction=True)
def test_canonical_refusal_keeps_prior_page_and_actual_error_without_claiming_complete(durable, caplog):
    import httpx

    from apps.inbox.durable_sync import claim_page, commit_page, run_one_page
    from apps.inbox.meta_sync_adapter import MetaSyncAdapter
    from apps.inbox.models import ConversationMessage
    from apps.inbox.sync_contracts import SyncPage
    from apps.inbox.tests.test_durable_pages_recovery import checkpoint, message

    cp = checkpoint(durable)
    lease = claim_page(cp.pk)
    commit_page(lease, SyncPage((message(lease, body="Synthetic already saved history"),), "nextA", False))
    adapter = MetaSyncAdapter(
        transport=httpx.MockTransport(lambda request: httpx.Response(400, json=refused().raw_response))
    )
    result = run_one_page(cp.pk, adapter)
    assert result.status == "blocked" and result.coverage == "partial" and result.cursor == "nextA"
    assert result.last_error_code == "invalid_response"
    assert list(ConversationMessage.objects.values_list("body", flat=True)) == ["Synthetic already saved history"]
    assert "Meta inbox GET refused: status=400 code=100 subcode=33" in caplog.text
    assert "PRIVATE" not in caplog.text
