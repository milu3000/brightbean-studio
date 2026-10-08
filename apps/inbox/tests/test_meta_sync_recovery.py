"""Fresh offline transports verify pinned routes and bounded provider failures."""

from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest
from django.utils import timezone

from apps.inbox.durable_sync import claim_page, commit_page, run_one_page, start_scan
from apps.inbox.meta_sync_adapter import MetaSyncAdapter
from apps.inbox.models import ConversationMessage, InboxSyncConnection
from apps.inbox.sync_contracts import SyncPage
from apps.inbox.sync_identity import SyncError
from apps.inbox.tests.test_durable_pages_recovery import checkpoint, message
from apps.inbox.tests.test_durable_pages_recovery import durable as _durable
from apps.inbox.tests.test_sync_ingestion_recovery import instagram

durable = _durable
pytestmark = pytest.mark.django_db


def metadata():
    return {"id": "thread-1", "participants": {"data": [{"id": "page-1"}, {"id": "peer-1"}]}}


@pytest.mark.parametrize(
    "shape,expected",
    [
        ("exact", "pagination_no_progress"),
        ("v26", "pagination_no_progress"),
        ("basic", "pagination_no_progress"),
        ("duplicate_after", "pagination_unverified"),
        ("duplicate_fields", "cursor_repeated"),
        ("changed_fields", "cursor_repeated"),
        ("changed_limit", "cursor_repeated"),
        ("unknown_param", "cursor_repeated"),
        ("other_route", "pagination_unverified"),
        ("nonempty", "cursor_repeated"),
        ("advance", ""),
        ("end", ""),
    ],
)
def test_instagram_empty_self_cursor_is_a_precise_hold_not_eof(durable, enroll_conversation_accounts, shape, expected):
    enroll_conversation_accounts(instagram(durable))
    cp = checkpoint(durable, context="bootstrap")
    lease = claim_page(cp.pk)
    commit_page(lease, SyncPage((message(lease, body="Kept original"),), "cursorA", False))
    if shape == "basic":
        type(cp).objects.filter(pk=cp.pk).update(content_fields_mode="basic")
    original = ConversationMessage.objects.values().get()
    cp.refresh_from_db()
    before = (cp.pages_committed, cp.scan_generation, cp.last_page_digest, cp.recent_cursor_digests)
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/v25.0/thread-1":
            return httpx.Response(200, json=metadata())
        assert request.url.params["after"] == "cursorA"
        query = list(request.url.params.multi_items())
        if shape == "duplicate_after":
            query.append(("after", "cursorA"))
        elif shape == "duplicate_fields":
            query.append(("fields", request.url.params["fields"]))
        elif shape in {"changed_fields", "changed_limit"}:
            key, value = ("fields", "id") if shape == "changed_fields" else ("limit", "25")
            query = [(name, value if name == key else old) for name, old in query]
        elif shape == "unknown_param":
            query.append(("unverified", "value"))
        elif shape == "advance":
            query = [(key, "cursorB" if key == "after" else value) for key, value in query]
        next_url = request.url.copy_with(params=query)
        if shape == "v26":
            next_url = next_url.copy_with(path="/v26.0/thread-1/messages")
        elif shape == "other_route":
            next_url = next_url.copy_with(path="/v25.0/another-thread/messages")
        rows = []
        if shape == "nonempty":
            rows = [{"id": "uncommitted-mid", "from": {"id": "peer-1"}, "message": "Must not be committed"}]
        data = {"data": rows}
        if shape != "end":
            data["paging"] = {
                "cursors": {"after": "cursorB" if shape == "advance" else "cursorA"},
                "next": str(next_url),
            }
        return httpx.Response(200, json=data)

    adapter = MetaSyncAdapter(transport=httpx.MockTransport(respond))
    result = run_one_page(cp.pk, adapter)
    assert len(requests) == 2 and result.last_error_code == expected
    assert ConversationMessage.objects.values().get() == original
    if expected == "pagination_no_progress":
        assert result.status == "blocked" and result.coverage == "partial"
        assert result.cursor == "cursorA" and result.restarts == 0 and result.retry_at is None
        assert (
            result.pages_committed,
            result.scan_generation,
            result.last_page_digest,
            result.recent_cursor_digests,
        ) == before
        assert run_one_page(cp.pk, adapter) is None and len(requests) == 2
    elif expected == "cursor_repeated":
        assert result.status == "retry" and result.restarts == 1 and result.cursor == ""
    elif expected:
        assert result.status == "blocked" and result.cursor == "cursorA"
    elif shape == "advance":
        assert result.status == "ready" and result.cursor == "cursorB" and result.coverage == "partial"
    else:
        assert result.status == "complete" and result.cursor == "" and result.coverage == "provider_edge_ended"


def test_real_fixed_message_routes_paginate_without_following_secret_next_url(durable):
    cp = checkpoint(durable)
    seen = []

    def respond(request):
        seen.append(str(request.url))
        assert request.url.host == "graph.facebook.com"
        if request.url.path == "/v25.0/thread-1":
            return httpx.Response(200, json=metadata())
        assert request.url.path == "/v25.0/thread-1/messages"
        number = 2 if request.url.params.get("after") == "cursorA" else 1
        data = {
            "data": [
                {
                    "id": f"mid-{number}",
                    "from": {"id": "peer-1"},
                    "to": {"data": [{"id": "page-1"}]},
                    "created_time": (timezone.now() - timedelta(minutes=1)).isoformat(),
                    "message": f"page {number}",
                }
            ]
        }
        if number == 1:
            data["paging"] = {
                "cursors": {"after": "cursorA"},
                "next": "https://graph.facebook.com/v25.0/thread-1/messages?after=cursorA&access_token=discard",
            }
        return httpx.Response(200, json=data)

    adapter = MetaSyncAdapter(transport=httpx.MockTransport(respond))
    run_one_page(cp.pk, adapter)
    result = run_one_page(cp.pk, adapter)
    assert result.status == "complete" and result.coverage == "provider_edge_ended"
    assert ConversationMessage.objects.count() == 2
    assert len(seen) == 4 and all("access_token" not in value for value in seen)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/v25.0/thread-1/messages?after=abc",
        "https://graph.facebook.com/v25.0/other/messages?after=abc",
        "https://graph.facebook.com/v26.0/thread-1/messages?after=abc",
    ],
)
def test_unproven_paging_route_blocks_without_partial_commit(durable, url):
    def respond(request):
        return httpx.Response(
            200,
            json=metadata()
            if request.url.path.endswith("/thread-1")
            else {
                "data": [],
                "paging": {"cursors": {"after": "abc"}, "next": url},
            },
        )

    result = run_one_page(checkpoint(durable).pk, MetaSyncAdapter(transport=httpx.MockTransport(respond)))
    assert result.status == "blocked" and result.last_error_code == "pagination_unverified"
    assert not ConversationMessage.objects.exists()


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "permission_unavailable"),
        (403, "permission_unavailable"),
        (404, "provider_unavailable"),
        (429, "rate_limited"),
    ],
)
def test_http_failures_keep_exact_safe_reason_and_never_mean_withdrawal(durable, status, code):
    adapter = MetaSyncAdapter(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, headers={"Retry-After": "150"}))
    )
    result = run_one_page(checkpoint(durable).pk, adapter)
    assert result.last_error_code == code and not ConversationMessage.objects.exists()


def test_revocation_after_metadata_stops_second_get(durable):
    seen = []

    def respond(request):
        seen.append(request.url.path)
        InboxSyncConnection.objects.filter(pk=durable.pk).update(enabled=False)
        return httpx.Response(200, json=metadata())

    with pytest.raises(SyncError, match="revoked"):
        run_one_page(checkpoint(durable).pk, MetaSyncAdapter(transport=httpx.MockTransport(respond)))
    assert seen == ["/v25.0/thread-1"] and not ConversationMessage.objects.exists()


def test_oversize_provider_page_is_not_sliced(durable):
    cp = start_scan(durable.pk, context="bootstrap")
    result = run_one_page(
        cp.pk,
        MetaSyncAdapter(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"data": [{"id": f"thread-{value}"} for value in range(101)]},
                )
            )
        ),
    )
    assert result.status == "blocked" and result.pages_committed == 0


def test_expired_page_lease_never_starts_http(durable):
    from apps.inbox.durable_sync import claim_page

    cp = checkpoint(durable)
    lease = claim_page(cp.pk)
    type(cp).objects.filter(pk=cp.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    seen = []
    adapter = MetaSyncAdapter(transport=httpx.MockTransport(lambda request: seen.append(request)))
    with pytest.raises(SyncError, match="lease_lost"):
        adapter.fetch(lease)
    assert seen == []


def test_slow_stream_and_unrequested_compression_do_not_extend_page_lease(durable):
    cp = start_scan(durable.pk, context="bootstrap")
    with patch("apps.inbox.meta_sync_adapter.time.monotonic", side_effect=[0.0, 21.0]):
        result = run_one_page(
            cp.pk,
            MetaSyncAdapter(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))),
        )
    assert result.status == "retry" and result.last_error_code == "provider_unavailable"
    type(cp).objects.filter(pk=cp.pk).update(retry_at=None)
    result = run_one_page(
        cp.pk,
        MetaSyncAdapter(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"Content-Encoding": "gzip"}, content=b"synthetic-invalid-compression"
                )
            )
        ),
    )
    assert result.status in {"retry", "blocked"} and result.last_error_code in {
        "provider_unavailable",
        "invalid_response",
    }


@pytest.mark.parametrize("created_time", [None, "", "invalid", "2026-10-07T01:00:00", "2999-01-01T00:00:00+00:00"])
def test_missing_or_unverified_provider_time_saves_quiet_undated_history(durable, created_time):
    from apps.inbox.canonical_send_target import validate_anchor
    from apps.inbox.dm_send_gate import DMSendGateError
    from apps.inbox.sync_contracts import calendar_months
    from apps.inbox.tests.test_durable_pages_recovery import prepare_live

    cp = prepare_live(durable)

    def respond(request):
        return httpx.Response(
            200,
            json=metadata()
            if request.url.path.endswith("/thread-1")
            else {
                "data": [
                    {
                        "id": "undated",
                        "from": {"id": "peer-1"},
                        "to": {"data": [{"id": "page-1"}]},
                        "created_time": created_time,
                        "message": "Saved without invented time",
                    }
                ]
            },
        )

    result = run_one_page(cp.pk, MetaSyncAdapter(transport=httpx.MockTransport(respond)))
    assert result.status == "complete"
    row = ConversationMessage.objects.get(platform_message_id="undated")
    assert row.body == "Saved without invented time" and row.occurred_at is None
    assert row.incoming_generation is None and row.observation_state.live_observed_at is None
    assert row.observation_state.expires_at == calendar_months(row.first_seen_at, 6)
    with pytest.raises(DMSendGateError, match="unavailable"):
        validate_anchor(row, row.conversation, durable.social_account)
