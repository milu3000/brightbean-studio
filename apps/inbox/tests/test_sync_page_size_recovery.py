"""Oversized Meta edges retry the same durable cursor under existing GET limits."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest
from django.utils import timezone

from apps.inbox.durable_sync import claim_page, commit_page, run_one_page, start_scan
from apps.inbox.meta_sync_adapter import MetaSyncAdapter
from apps.inbox.models import ConversationMessage, InboxSyncBudget, InboxSyncCheckpoint, InboxSyncConnection
from apps.inbox.sync_contracts import MAX_BYTES, SyncPage
from apps.inbox.sync_identity import SyncError
from apps.inbox.sync_scheduler import release_gets, reserve_gets
from apps.inbox.tests.test_durable_pages_recovery import checkpoint, message
from apps.inbox.tests.test_durable_pages_recovery import durable as _durable
from apps.inbox.tests.test_meta_sync_recovery import metadata

durable = _durable
pytestmark = pytest.mark.django_db


def oversized():
    return {"error": {"code": 1, "message": "Please reduce the amount of data you're asking for, then retry"}}


@pytest.fixture(params=["facebook", "instagram_login"])
def binding(request, durable, enroll_conversation_accounts):
    account = durable.social_account
    if account.platform != request.param:
        account.platform = request.param
        account.save(update_fields=["platform"])
        durable.platform = request.param
        durable.save(update_fields=["platform"])
        enroll_conversation_accounts(account)
    return durable


def test_smaller_budgeted_retry_preserves_cursor_media_dedup_and_coverage(binding):
    cp = checkpoint(binding)
    lease = claim_page(cp.pk)
    commit_page(
        lease,
        SyncPage(
            (message(lease, attachments=({"type": "image", "url": "https://scontent.xx.fbcdn.net/original.jpg"},)),),
            "cursorA",
            False,
        ),
    )
    saved = ConversationMessage.objects.get()
    media = saved.attachments
    requests = []
    occurred = saved.occurred_at

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/thread-1"):
            return httpx.Response(200, json=metadata())
        assert request.url.params["after"] == "cursorA"
        assert "attachments" in request.url.params["fields"] and "to{id}" in request.url.params["fields"]
        if request.url.params["limit"] == "50":
            return httpx.Response(500, json=oversized())
        assert request.url.params["limit"] == "25"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "m-1",
                        "from": {"id": "peer-1"},
                        "to": {"data": [{"id": "page-1"}]},
                        "created_time": occurred.isoformat(),
                        "message": "Recovered",
                        "attachments": {
                            "data": [{"image_data": {"url": "https://scontent.xx.fbcdn.net/original.jpg"}}]
                        },
                    }
                ],
                "paging": {
                    "cursors": {"after": "cursorB"},
                    "next": f"https://{request.url.host}/v25.0/thread-1/messages?after=cursorB&access_token=discard",
                },
            },
        )

    adapter = MetaSyncAdapter(transport=httpx.MockTransport(respond))
    with patch("apps.inbox.sync_scheduler._resolve_publish_credentials", return_value={"client_id": "synthetic"}):
        reservation = reserve_gets(binding.pk, adapter.required_gets("messages"))
        failed = run_one_page(cp.pk, adapter)
        release_gets(reservation)
        assert failed.status == "retry" and failed.last_error_code == "page_size_rejected"
        assert failed.cursor == "cursorA" and failed.pages_committed == 1 and failed.coverage == "partial"
        saved.refresh_from_db()
        assert saved.attachments == media and saved.body == "Hello"
        assert len(requests) == 2 and run_one_page(cp.pk, adapter) is None
        InboxSyncCheckpoint.objects.filter(pk=cp.pk).update(retry_at=timezone.now() - timedelta(seconds=1))
        reservation = reserve_gets(binding.pk, adapter.required_gets("messages"))
        result = run_one_page(cp.pk, adapter)
        release_gets(reservation)
        assert InboxSyncBudget.objects.get().gets_reserved == len(requests) == 4
        assert reserve_gets(binding.pk, 1) is None
    assert result.status == "ready" and result.cursor == "cursorB" and result.coverage == "partial"
    assert result.pages_committed == 2 and result.attempts == 0 and result.content_fields_mode == "extended"
    assert ConversationMessage.objects.count() == 1
    saved.refresh_from_db()
    assert saved.body == "Recovered" and saved.attachments[0]["url"].endswith("/original.jpg")
    assert all("access_token" not in str(request.url) for request in requests)


@pytest.mark.parametrize("stream", ["conversations", "messages"])
def test_minimum_page_rejection_stops_without_advancing_or_clearing_cursor(binding, stream):
    cp = start_scan(
        binding.pk, context="backfill", stream=stream, scope_key="account" if stream == "conversations" else "thread-1"
    )
    commit_page(claim_page(cp.pk), SyncPage((), "cursorA", False))
    limits = []

    def respond(request):
        if request.url.path.endswith("/thread-1"):
            return httpx.Response(200, json=metadata())
        assert request.url.params["after"] == "cursorA"
        limits.append(request.url.params["limit"])
        return httpx.Response(500, json=oversized())

    adapter = MetaSyncAdapter(transport=httpx.MockTransport(respond))
    for _ in range(5):
        result = run_one_page(cp.pk, adapter)
        assert result.cursor == "cursorA" and result.pages_committed == 1 and result.coverage == "partial"
        assert result.content_fields_mode == "extended"
        InboxSyncCheckpoint.objects.filter(pk=cp.pk).update(retry_at=timezone.now() - timedelta(seconds=1))
    assert limits == ["50", "25", "10", "5", "1"]
    assert result.status == "blocked" and result.last_error_code == "page_size_rejected"
    assert result.attempts == 5 and run_one_page(cp.pk, adapter) is None
    assert not ConversationMessage.objects.exists()


@pytest.mark.parametrize(
    "status,payload,expected",
    [
        (500, {"error": {"code": 1, "message": "Temporary service failure"}}, "provider_unavailable"),
        (500, {"error": {"code": 190, "message": "Please reduce the amount of data"}}, "provider_unavailable"),
        (403, oversized(), "permission_unavailable"),
        (429, oversized(), "rate_limited"),
    ],
)
def test_other_refusals_do_not_trigger_size_or_permission_downgrade(durable, status, payload, expected):
    cp = checkpoint(durable)
    seen = []

    def respond(request):
        seen.append(request)
        return (
            httpx.Response(200, json=metadata())
            if request.url.path.endswith("/thread-1")
            else httpx.Response(status, json=payload)
        )

    result = run_one_page(cp.pk, MetaSyncAdapter(transport=httpx.MockTransport(respond)))
    assert result.last_error_code == expected and result.content_fields_mode == "extended"
    assert result.pages_committed == 0 and len(seen) == 2


def test_non_json_500_and_metadata_size_rejection_remain_provider_failures(durable):
    cp = checkpoint(durable)
    for response in (httpx.Response(500, content=b"upstream unavailable"), httpx.Response(500, json=oversized())):
        result = run_one_page(
            cp.pk, MetaSyncAdapter(transport=httpx.MockTransport(lambda request, value=response: value))
        )
        assert result.status == "retry" and result.last_error_code == "provider_unavailable"
        InboxSyncCheckpoint.objects.filter(pk=cp.pk).update(retry_at=timezone.now() - timedelta(seconds=1))


@pytest.mark.parametrize(
    "status,expected_status,expected_error",
    [(500, "retry", "provider_unavailable"), (200, "blocked", "response_too_large")],
)
def test_oversized_response_stops_reading_without_advancing_saved_page(
    durable, status, expected_status, expected_error
):
    cp = checkpoint(durable)
    lease = claim_page(cp.pk)
    commit_page(
        lease,
        SyncPage(
            (message(lease, attachments=({"type": "image", "url": "https://scontent.xx.fbcdn.net/original.jpg"},)),),
            "cursorA",
            False,
        ),
    )
    original_message = ConversationMessage.objects.values().get()
    cp.refresh_from_db()
    original_commit = cp.last_committed_at
    consumed = []
    closed = []

    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            consumed.append("prefix")
            yield b"upstream unavailable"
            consumed.append("oversized")
            yield b" " * MAX_BYTES
            consumed.append("unread")
            yield b"must never be consumed"

        def close(self):
            closed.append(True)

    def respond(request):
        if request.url.path.endswith("/thread-1"):
            return httpx.Response(200, json=metadata())
        assert request.url.params["after"] == "cursorA"
        return httpx.Response(status, stream=OversizedStream())

    result = run_one_page(cp.pk, MetaSyncAdapter(transport=httpx.MockTransport(respond)))
    assert result.status == expected_status and result.last_error_code == expected_error
    assert result.cursor == "cursorA" and result.pages_committed == 1 and result.coverage == "partial"
    assert result.last_committed_at == original_commit and result.content_fields_mode == "extended"
    assert ConversationMessage.objects.values().get() == original_message
    assert consumed == ["prefix", "oversized"] and closed == [True]
    if status == 500:
        assert result.retry_at > timezone.now()


def test_modified_attempt_or_revocation_fences_reduced_page_before_http(durable):
    cp = checkpoint(durable)
    lease = claim_page(cp.pk)
    adapter = MetaSyncAdapter(transport=httpx.MockTransport(lambda request: pytest.fail("No HTTP is authorized")))
    with pytest.raises(SyncError, match="lease_lost"):
        adapter.fetch(replace(lease, attempts=4))
    InboxSyncConnection.objects.filter(pk=durable.pk).update(enabled=False)
    with pytest.raises(SyncError, match="revoked"):
        adapter.fetch(lease)
